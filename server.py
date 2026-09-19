"""浏览器前端:把 agent 跑在一个 HTTP 服务后面。

本机自用 —— 没有登录。会话存在同目录的 sessions.db(SQLite)里,活得过
进程重启;每个会话一把锁,所以几个会话可以同时跑,也可以切走、切回来
接着看。

七个端点:

    GET  /                        页面
    GET  /sessions                会话列表(带上"在跑"和"在等你确认")
    GET  /session/<id>/events     重放。?since=<游标> 增量拉,省略即全量
    POST /session                 建会话
    POST /session/<id>/delete     删会话
    POST /ask                     请求体是 JSON {"session": "...", "query": "..."},
                                  响应是一条 NDJSON 流(一行一个 JSON)
    POST /answer                  请求体是 JSON {"id": "...", "allow": true},回答
                                  /ask 那条流里挂出来的 ask 事件 —— 见 make_ask。
                                  它是**唯一能授权**的入口,所以门看得比 /ask 还紧
    OPTIONS /*                    预检。跨源那道门就架在这儿,见 do_OPTIONS

**为什么不用 SSE(EventSource):** 它只能发 GET,查询就得塞进 URL。
改成 fetch() 读响应流,格式走 NDJSON —— 解析是几行 JS,还省掉 `data:`
那层包装。反正两端都是自己的,不用迁就 EventSource 的约束。

**为什么 POST 处理里直接跑 agent_loop:** emit 就是"往响应写一行再
flush",所以不需要队列、不需要第二个线程。一个请求一个线程
(ThreadingHTTPServer),这条流开着直到本轮跑完。

**协议用 HTTP/1.0(默认),故意不发 Content-Length:** 这样响应体的结束
由连接关闭来标记,浏览器那边读到 EOF 就是本轮结束。发 Content-Length
就得先把整轮跑完才知道长度,那就没有流了。

**两张表的分工**(细节见 sessions.py):events 是流水账,重放页面用的,
写进去就不再改;messages 是快照,每轮整体重写,因为压缩器会把它换成
别的形状。**页面上看到的那一份就是 events 里存的那一份** —— 重放出来
必须跟你记忆里那次对话一致,所以库里不存"更完整"的版本。

**两把锁,别搞混:**

    STORE 里那把      护 sqlite 连接,圈住单条 SQL(毫秒级)
    session_lock(sid) 护"这个会话的一轮",圈住整个 agent 循环(分钟级)

反过来写就废了:拿 STORE 的锁去圈一整轮,多个会话又变回全局串行,
多会话白做;而不拿会话锁去跑一轮,两条线会同时改同一份 history。
"""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent import agent_loop
from app import MODEL, SYSTEM, make_compactor
from config import MAX_ROUNDS
from context import ContextCompactor
from hooks import trigger_hooks
from sessions import SessionStore
from tools import build_tools
from tools.todo import TodoManager

PORT = 8765
PAGE = Path(__file__).parent / "ui" / "index.html"

STORE = SessionStore()

# 每个会话一把锁。**只增不删**:删掉的瞬间,等在它上面的线程手里还攥着
# 旧那把,而下一个请求拿到的是新的 —— 两把锁护同一份状态,等于没护。
# 让它涨:本机工具,会话数是几十。
LOCKS: dict[str, threading.Lock] = {}

# 每个会话一份任务清单。也一样只增不删,理由同上(而且清单本来就跨轮次
# 活着:模型每三轮会被提醒更新一次)。为什么不是全局一份,见 tools/todo.py。
TODOS: dict[str, TodoManager] = {}

# 护上面两个容器的**增删**。它不护任何别的东西 —— 尤其不护 SQL。
REGISTRY = threading.Lock()


def session_lock(sid: str) -> threading.Lock:
	with REGISTRY:
		lock = LOCKS.get(sid)
		if lock is None:
			lock = LOCKS[sid] = threading.Lock()
		return lock


def is_running(sid: str) -> bool:
	"""这个会话在不在跑。

	不另存一个 RUNNING 集合:轮次从头到尾攥着那把锁,所以 locked() 就是
	答案。少一份需要同步的状态 —— 那种状态迟早会漂。从没跑过的会话没有
	锁对象,那就不在跑。
	"""
	with REGISTRY:
		lock = LOCKS.get(sid)
	return lock is not None and lock.locked()


def todo_for(sid: str) -> TodoManager:
	with REGISTRY:
		todo = TODOS.get(sid)
		if todo is None:
			todo = TODOS[sid] = TodoManager()
		return todo


def clean_query(raw) -> str:
	"""洗掉坏字节再去空白。

	stdin 也好、socket 也好,坏字节凑不成合法序列时会被 surrogateescape
	兜成孤代理项,那东西编码不进 API 请求体,会在 SDK 内部炸成
	UnicodeEncodeError(不是 APIError,捕不到)。
	"""
	return str(raw).encode("utf-8", "replace").decode("utf-8").strip()


def route(path: str) -> tuple[list[str], dict]:
	"""把路径切成段,顺带取出查询串。

	不用正则:正则很容易写成前缀匹配,然后静默吃掉 /session/x 这种漏了
	动词的路径。段数判断让它们老老实实落到 404,将来要加
	/session/<id>/stop 也只是多一个分支。

	切出来的段是**外面来的**,只许进 SQL 的参数位,永远不许进 f-string。
	"""
	url = urlsplit(path)
	return [part for part in url.path.split("/") if part], parse_qs(url.query)


def is_local_origin(origin: str) -> bool:
	"""只放行同机来源。

	**不能回 "*"**:回了的话,你浏览器里随便开着的哪个网页都能 POST 过来
	指挥这个 agent 跑 bash,而且还能把结果读走。这个 agent 手里是真 shell,
	不能对任意网页开门。

	顺带说清楚一件事:CORS 只管"能不能读响应"。跨源的简单请求(POST +
	text/plain)不管有没有这个头,请求本身都会发出去、命令都会跑。所以要
	真挡住,得让请求**必须过预检** —— 见 do_OPTIONS 和页面那边的
	Content-Type: application/json。
	"""
	return origin.startswith(("http://localhost:", "http://127.0.0.1:"))


def origin_allowed(headers) -> bool:
	"""改状态的两个 POST 都要先过这道,服务端自己查。

	do_OPTIONS 那道预检只挡得住"浏览器会先发预检"的请求。攻击页面改用
	text/plain 发,就退化成简单请求 —— 根本不预检,直接打到这儿。这个洞在
	/ask 上一直存在(见 is_local_origin 上面那段),而 /answer 一加就变得
	严重了:那是**授权**入口,别的网页替你点一下"允许"就够。

	所以不靠浏览器自觉,自己看一眼 Origin。

	没有 Origin 的放行:那不是浏览器(curl、本机脚本),能这么发的人本来
	就有这台机器的权限,拦它没有意义。
	"""
	origin = headers.get("Origin")
	return origin is None or is_local_origin(origin)


def ndjson_emit(wfile):
	"""造一个把事件写进响应流的 emit。

	每条后面 flush:不 flush 的话全攒在缓冲区里,页面要等整轮结束才一次
	看到全部 —— 那就退化成了"不做流式"。
	"""
	def emit(event: dict) -> None:
		line = json.dumps(event, ensure_ascii=False) + "\n"
		wfile.write(line.encode("utf-8"))
		wfile.flush()
	return emit


def emit_quietly(emit, event: dict) -> bool:
	"""往响应流写一条,写不出去就返回 False。

	页面关掉之后 wfile 那头已经断了,写会抛 BrokenPipeError。那不是错误,
	是"人走了" —— 不该让它掀翻整个 agent 循环。
	"""
	try:
		emit(event)
		return True
	except OSError:
		return False


def recording_emit(sid: str, emit):
	"""先落库,再进流。事件带上库给的游标 seq。

	seq 是给页面去重用的:同一条事件可能从两条路到达(直播流、切回来时的
	重放或轮询),两边各画一次就会重复。有了只增的 seq,页面一条规则
	(画过的不再画)就管住了,不用在两边各写一套状态机。

	**不原地改**传进来的那个 event:那是替调用方改数据。谁下次重用同一个
	dict,就会带上上一次的 seq。
	"""
	def wrapped(event: dict) -> None:
		seq = STORE.append_event(sid, event)
		if seq is not None:
			event = {**event, "seq": seq}
		emit(event)
	return wrapped


def quiet(emit):
	"""包成"写不出去也不吭声"的版本,交给 agent 循环和压缩器。

	为什么必须有这一层:agent_loop 和压缩器都是直接调 emit 的,那里没有
	try。页面一关(或者切走之后那条流断了),wfile.write 抛 OSError 会一路
	掀翻整个循环 —— **今天就是这样**:关掉页面等于杀掉这一轮。

	而"切走的会话继续跑完"要的正好相反,所以交给循环的必须是安静版。
	代价说清楚:一个被忘掉的标签页会把这一轮的钱烧完,边界是现成的
	(MAX_ROUNDS、ASK_TIMEOUT、侧栏上看得到的"在跑")。

	make_ask 拿的**不是**这份:它靠 emit_quietly 的返回值判断"还有人能回答
	吗",给它安静版的话,页面一关就没人回答,而 agent 会在那儿干等 300 秒。
	"""
	return lambda event: emit_quietly(emit, event)


# 等一个确认最多等多久。超了算拒绝。
#
# 想短一点也行,但注意代价不对称:等太久只是页面刷不出新一轮(这期间
# 同一个会话的请求全是 409),放行放错是把机器交出去。所以宁可等。
ASK_TIMEOUT = 300.0

# 挂起的确认。key 是 ask 事件的 id,value 是那个槽。
#
# 为什么需要这张表:agent 循环跑在 POST /ask 那条线程里,它要停下来等人;
# 而人的回答从另一条连接(POST /answer)进来 —— 两条线程之间没有别的
# 交汇点。Event 负责"停",allow 负责"答案"。
#
# 槽里记着 session 只是为了 /sessions 能报出"哪个会话在等你确认"。
# 认槽始终只看 rid —— 它就是那张能力凭证,/answer 不需要知道会话。
PENDING: dict[str, dict] = {}


def make_ask(emit, sid: str):
	"""造一个把问题推给浏览器、然后挂起等回答的确认器。

	和 emit 一样按请求建:它绑在那条响应流上,而流是每请求一条。这也正好
	对应"一轮只有一个确认在飞"——同一轮里工具是顺序跑的。

	超时和断连都算拒绝,不放行:否则"关掉页面"就成了提权手段。
	"""
	def ask(question: str) -> bool:
		rid = uuid.uuid4().hex
		slot = {"event": threading.Event(), "allow": False, "session": sid}
		PENDING[rid] = slot
		try:
			if not emit_quietly(emit, {"kind": "ask", "id": rid,
			                           "question": question}):
				return False          # 流已经断了,没人能回答
			if not slot["event"].wait(ASK_TIMEOUT):
				emit_quietly(emit, {"kind": "note", "source": "permission",
				                    "text": f"no answer in {ASK_TIMEOUT:.0f}s, denied"})
				return False
			return slot["allow"]
		finally:
			# 无论哪条路径出去都要清,不然 PENDING 会一直涨。
			PENDING.pop(rid, None)
	return ask


def trim_dangling_tool_use(history: list) -> int:
	"""剪掉结尾那些"带了 tool_use、结果还没回填"的消息,返回剪掉几条。

	agent_loop 自己在循环顶部检查这个位置(agent.py 里那段注释),但它保证
	的是"它自己返回时 messages 停在完整回合上",保证不了"它被掀翻时也是"。
	异常或 Ctrl+C 可以落在 assistant 已经追加、tool_result 还没拼好的那一
	瞬间 —— 那时候结尾就是一条悬空的 assistant。

	以前这没事:进程一死,内存里那份 history 跟着没了,悬空消息也一起没了。
	**现在它活得过重启**,下次提问直接把这段发给 API,就是 400
	tool_use ids without tool_result,而用户完全看不出为什么。

	判定复用压缩器的(ContextCompactor.has_tool_use),它认 dict 和 pydantic
	两种形状 —— 从库里读回来的是 dict,活路径上是 pydantic 对象。
	"""
	before = len(history)
	while history and ContextCompactor.has_tool_use(history[-1]):
		history.pop()
	return before - len(history)


class Handler(BaseHTTPRequestHandler):
	def _cors(self):
		"""同机来源就回一个 Allow-Origin,别的什么都不回。

		三个 GET 也必须调它。忘了的话,从 IDE 内置预览(另一个源)打开页面
		时侧栏是空的、会话切不动,而直接开 8765 一切正常 —— 最难查的那种
		"只在某一种打开方式下坏"。
		"""
		origin = self.headers.get("Origin")
		if origin and is_local_origin(origin):
			self.send_header("Access-Control-Allow-Origin", origin)

	def _send_json(self, obj) -> None:
		body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
		self.send_response(200)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self._cors()
		self.end_headers()
		self.wfile.write(body)

	def do_OPTIONS(self):
		"""预检。这是道真门,不是走过场。

		页面从 IDE 的预览服务(另一个源)发请求时,浏览器先发这个。坏页面
		发来的预检带着它自己的 Origin,而 _cors() 不会回 Allow-Origin ——
		预检不过,后面那个 POST 根本不会发出去。

		前提是页面必须**触发**预检,也就是不能被当成简单请求。所以页面那边
		发的是 application/json,不是 text/plain。
		"""
		self.send_response(204)
		self._cors()
		self.send_header("Access-Control-Allow-Methods", "GET, POST")
		self.send_header("Access-Control-Allow-Headers", "content-type")
		self.send_header("Access-Control-Max-Age", "600")
		self.end_headers()

	def do_GET(self):
		parts, query = route(self.path)
		if not parts:
			self._page()
		elif parts == ["sessions"]:
			self._get_sessions()
		elif len(parts) == 3 and parts[0] == "session" and parts[2] == "events":
			self._get_events(parts[1], query)
		else:
			self.send_error(404)

	def _page(self):
		# 每次请求现读,改完 HTML 刷一下页面就生效,不用重启服务
		body = PAGE.read_bytes()
		self.send_response(200)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(body)

	def _get_sessions(self):
		with REGISTRY:
			# 先复制再遍历:PENDING 会被别的线程改大小,而迭代一个正在被改的
			# dict 会抛 RuntimeError: dictionary changed size during
			# iteration —— GIL 不保护这个。
			waiting = {slot["session"] for slot in list(PENDING.values())}
		self._send_json({"sessions": [
			{**row, "running": is_running(row["id"]),
			 "waiting": row["id"] in waiting}
			for row in STORE.list_sessions()
		]})

	def _get_events(self, sid: str, query: dict):
		"""重放。库里的 events 就是页面当时渲染过的那一份,一条条喂回去即可。"""
		if not STORE.session_exists(sid):
			self.send_error(404, "no such session")
			return

		try:
			since = int(query.get("since", ["0"])[0])
			if since < 0:
				raise ValueError
		except ValueError:
			# 不能悄悄当 0 处理:那等于把整个会话重画一遍,页面看起来像
			# "重放重复了" —— 最难查的那种。
			self.send_error(400, "since must be a non-negative integer")
			return

		events = []
		for seq, stored in STORE.events_since(sid, since):
			event = {**stored, "seq": seq}
			if event.get("kind") == "ask":
				# 这条确认**可能还活着**:切走再切回来时那一轮还在跑,
				# PENDING 里的槽还在,点下去是真有用的。所以判活得查表 ——
				# 不能因为"它是从库里读出来的"就当成历史。
				event["live"] = event.get("id") in PENDING
			events.append(event)
		self._send_json({"events": events, "running": is_running(sid)})

	def _json_body(self, expect: str):
		"""读并解析请求体。解析不了就自己回 400 并返回 None。

		走 JSON 而不是裸文本,是为了强制预检 —— 见 do_OPTIONS。
		"""
		length = int(self.headers.get("Content-Length", 0))
		raw = self.rfile.read(length) if length else b""
		try:
			body = json.loads(raw or b"{}")
		except ValueError:
			self.send_error(400, f"body must be JSON: {expect}")
			return None
		if not isinstance(body, dict):
			self.send_error(400, f"body must be JSON object: {expect}")
			return None
		return body

	def do_POST(self):
		# 先过门,再读体 —— 被拒的请求不该有机会往 agent 那边递东西。
		if not origin_allowed(self.headers):
			self.send_error(403, "cross-origin POST refused")
			return
		parts, _ = route(self.path)
		if parts == ["ask"]:
			self._post_ask()
		elif parts == ["answer"]:
			self._post_answer()
		elif parts == ["session"]:
			self._post_session()
		elif len(parts) == 3 and parts[0] == "session" and parts[2] == "delete":
			self._post_delete(parts[1])
		else:
			self.send_error(404)

	def _post_session(self):
		row = STORE.create_session()
		self._send_json({"id": row["id"], "title": row["title"]})

	def _post_delete(self, sid: str):
		# 正在跑的会话不许删:那一轮还攥着锁、还要写回 messages,删了它下次
		# 写回就是外键错误,而用户看到的是"聊到一半的东西没了"。
		if is_running(sid):
			self.send_error(409, "this session has a turn running")
			return
		STORE.delete_session(sid)
		# 进程里那两份(LOCKS/TODOS)不跟着收:它们只增不删,理由见上面。
		# 剩下几个没人用的 dict,在本机工具里不值得为它引入删除的竞态。
		self._send_json({"ok": True})

	def _post_answer(self):
		"""回答一条挂起的确认。这是唯一能授权的入口,所以只看 id 认槽。"""
		body = self._json_body('{"id": "...", "allow": true}')
		if body is None:
			return

		slot = PENDING.get(str(body.get("id", "")))
		if slot is None:
			# 已经超时清了,或者 id 是编的。不是错误 —— 页面可能只是点慢了,
			# 那一轮早就按拒绝往下走了。
			self.send_error(409, "no such pending question")
			return

		slot["allow"] = bool(body.get("allow"))
		slot["event"].set()

		out = b'{"ok": true}'
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(out)))
		self._cors()
		self.end_headers()
		self.wfile.write(out)

	def _post_ask(self):
		body = self._json_body('{"session": "...", "query": "..."}')
		if body is None:
			return
		sid = str(body.get("session") or "")
		query = clean_query(body.get("query", ""))
		if not query:
			self.send_error(400, "empty query")
			return
		if not STORE.session_exists(sid):
			self.send_error(404, "no such session")
			return

		lock = session_lock(sid)
		if not lock.acquire(blocking=False):
			# 含义跟以前不同了:以前是"有一轮在跑",现在是"**这个会话**有
			# 一轮在跑"。别的会话照跑不误。
			self.send_error(409, "this session already has a turn running")
			return
		try:
			self._run_turn(sid, query)
		finally:
			lock.release()

	def _run_turn(self, sid: str, query: str):
		"""跑一轮。全程攥着这个会话的锁(由 _post_ask 拿着并负责释放)。"""
		# 读历史放在发响应头之前:读不出来还能回一个干净的状态码,而不是
		# 已经 200 了才发现手里没有上下文。
		#
		# 每轮都从库里读,不在内存里留一份。看着像浪费(几十毫秒),其实是
		# 拿它换掉"内存那份和库里那份什么时候会不一致"这个问题 —— 而那个
		# 问题一旦存在,答案就是"在你想不到的时候"。顺带,重启存活是免费的。
		try:
			history = STORE.load_messages(sid)
		except Exception as e:
			self.send_error(500, f"cannot load session: {e}")
			return

		self.send_response(200)
		self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self._cors()
		self.end_headers()

		raw = ndjson_emit(self.wfile)
		# emit 落库 + 进流;交给循环的那份是安静版(页面走了也不掀翻这一轮)。
		# make_ask 拿的必须是不安静的那份,理由见 quiet() 的注释。
		emit = recording_emit(sid, raw)
		silent = quiet(emit)

		trigger_hooks("UserPromptSubmit", query)
		history.append({"role": "user", "content": query})

		# 用户这条和 title 先落地:会话立刻出现在侧栏里,而且这一轮就算
		# 中途崩了,至少留着"问的是什么"。轮末会被整体重写覆盖掉。
		STORE.touch(sid, query)
		STORE.append_message(sid, history[-1])
		emit({"kind": "you", "text": query})

		try:
			reply = agent_loop(history,
			                   active_request=query,
			                   system=SYSTEM,
			                   tools=build_tools(todo_for(sid)),
			                   model=MODEL,
			                   max_rounds=MAX_ROUNDS,
			                   compactor=make_compactor(silent),
			                   ask=make_ask(emit, sid),
			                   emit=silent)
			emit_quietly(emit, {"kind": "reply", "text": reply})
		except Exception as e:
			# 兜底:异常不该把 history 一起带走,也不该让流断在半截
			# 而没有下文 —— 前端会一直转圈。
			emit_quietly(emit, {"kind": "reply",
			                    "text": f"Error: {type(e).__name__}: {e}"})
		finally:
			dropped = trim_dangling_tool_use(history)
			if dropped:
				emit_quietly(emit, {"kind": "note", "source": "round",
				                    "text": f"这一轮被打断,{dropped} 条没有结果的消息没有存"})
			try:
				STORE.replace_messages(sid, history)
			except Exception as e:
				# 这一轮没存上,用户必须知道(刷新会退回上一轮)。流可能已经
				# 断了,所以走 emit_quietly —— 库里那条 note 照样落得下。
				emit_quietly(emit, {"kind": "note", "source": "store",
				                    "text": f"这一轮的上下文没存上(刷新会退回上一轮): "
				                            f"{type(e).__name__}: {e}"})

	def log_message(self, fmt, *args):
		# 默认实现往 stderr 打一行每个请求。前端本来就是终端,不需要它
		# 再复述一遍。
		pass


if __name__ == "__main__":
	print(f"http://localhost:{PORT}/")
	ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
