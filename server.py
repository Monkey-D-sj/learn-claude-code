"""浏览器前端:把 agent 跑在一个 HTTP 服务后面。

本机自用 —— 没有登录。会话存在同目录的 sessions.db(SQLite)里,活得过
进程重启;每个会话一把锁,所以几个会话可以同时跑,也可以切走、切回来
接着看。

八个端点:

    GET  /                        页面
    GET  /sessions                会话列表(带上"在跑"和"在等你确认")
    GET  /session/<id>/turns      轮次 + 每轮的原始消息,外加事件游标 cursor ——
                                  刷新页面时重建轮次展示走这条,不走事件重放
    GET  /session/<id>/events     重放。?since=<游标> 增量拉,省略即全量;
                                  ?legacy=1 只要没有 turn_id 的那些(旧版记录)
    POST /session                 建会话
    POST /session/<id>/delete     删会话
    POST /ask                     请求体是 JSON {"session": "...", "query": "..."},
                                  响应是一条 NDJSON 流(一行一个 JSON)
    POST /answer                  请求体是 JSON {"id": "..."},外加 "allow":
                                  true(权限确认)或 "text": "..."(模型提问),
                                  回答 /ask 那条流里挂出来的 ask 事件 —— 见
                                  make_ask / make_ask_text。它是**唯一能授权**
                                  的入口,所以门看得比 /ask 还紧
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

**表的分工见 sessions.py。这里只记跟页面有关的那条**:页面上看到的那一份
就是 events 里存的那一份 —— 重放出来必须跟你记忆里那次对话一致,所以
库里不存"更完整"的版本。

**一轮的生命周期**(`_run_turn`):开轮时建 Turn + 存用户那条(一个短事务),
跑的过程中 record 逐条记原始消息、emit 逐条落事件,收尾时把最终上下文和
Turn 终态放同一个事务。数据库事务一律不包住模型请求和工具执行。

**两把锁,别搞混:**

    STORE 里那把      护 sqlite 连接,圈住单条 SQL(毫秒级)
    session_lock(sid) 护"这个会话的一轮",圈住整个 agent 循环(分钟级)

反过来写就废了:拿 STORE 的锁去圈一整轮,多个会话又变回全局串行,
多会话白做;而不拿会话锁去跑一轮,两条线会同时改同一份 history。
"""

import hashlib
import json
import os
import sys
import threading
import uuid
from itertools import count
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import dblock
from agent import TurnOutcome, agent_loop
from app import MODEL, build_system, make_compactor
from config import MAX_ROUNDS, MEMORY_PATH, USER_MEMORY_PATH, WORKDIR
from context import ContextCompactor
from hooks import trigger_hooks
from sessions import (DB_PATH, INTERRUPT_CHECKPOINT_FAILED,
                      INTERRUPT_PERSIST_FAILED, CheckpointConflict,
                      PersistError, SessionStore, TurnStateConflict)
from tools import build_tools
from tools.memory import load_memory
from tools.compress import bind_recall, make_recall
from tools.todo import TodoManager
import usage

# 端口可以用环境变量顶掉(AGENT_PORT),理由同 sessions.DB_PATH:进程级测试
# 要起真的服务,不能占着你的 8765。默认值没变 —— ui/index.html 里还有一份
# 写死的 8765(改要一起改),dev.py 也认 8765。
PORT = int(os.environ.get("AGENT_PORT") or "8765")
PAGE = Path(__file__).parent / "ui" / "index.html"
PET_IMAGE = Path(__file__).parent / "ui" / "assets" / "whale-girl.png"

# **这个库的排他锁和 STORE 都不是 import 时装上的**,理由见 open_store()。
#
# STORE 先摆一个 None:测试里 monkeypatch 换掉它(见 tests/test_ask.py 的
# soon fixture),真跑起来由 main() 装。
STORE: "SessionStore | None" = None
DB_LOCK: "dblock.DbLock | None" = None

# 代码指纹的缓存(见 _code_fingerprint)。进程活着的期间代码不会变 ——
# 要变就得重启,那时这份缓存也跟着没了。
_CODE_FP: "str | None" = None


def open_store():
	"""拿到这个库的排他锁,然后才建会话库(含迁移)。返回那把锁。

	**顺序是这一步的全部意义**:SessionStore.__init__ 会跑迁移,而迁移是往
	库里**写**(至少 PRAGMA journal_mode,真要升级时还有 DDL)。第二个实例
	如果先 import 建了 STORE、再发现自己拿不到锁,库已经被动过了 —— 而"没
	拿到锁的实例一个字都不许写"正是启动独占要保证的事。

	所以模块级不再是 `STORE = SessionStore()`:import 这个模块(测试、一次性
	检查脚本、REPL)不该有能力打开、更不该有能力迁移你的会话库。同一个理由
	把 reap_running 从 __init__ 挪到了 __main__(见它的注释),这是第二半。
	"""
	global STORE, DB_LOCK
	DB_LOCK = dblock.acquire(DB_PATH)
	STORE = SessionStore()
	return DB_LOCK

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


def recording_emit(sid: str, emit, turn: dict):
	"""先落库,再进流。事件带上库给的游标 seq,以及它属于哪一轮。

	seq 是给页面去重用的:同一条事件可能从两条路到达(直播流、切回来时的
	重放或轮询),两边各画一次就会重复。有了只增的 seq,页面一条规则
	(画过的不再画)就管住了,不用在两边各写一套状态机。

	turn_id/turn_no 是给页面**分组**用的:这一轮的内容要落进同一个轮次
	容器。turn_no 也一起带上,是因为轮询可能先收到这一轮的第二条事件 ——
	页面那时得能凭空把容器建出来,而容器的标题就是轮号。

	**不原地改**传进来的那个 event:那是替调用方改数据。谁下次重用同一个
	dict,就会带上上一次的 seq。
	"""
	def wrapped(event: dict) -> None:
		event = {**event, "turn_id": turn["id"], "turn_no": turn["turn_no"]}
		# 碎片只走直播:一轮几千条,落库就是几千行,而完整的那一份紧接着
		# 就到,那条才是要重放的。kind 自己就是标记,不另加 partial 字段
		# —— 两个标记就有对不上的一天。
		if event.get("kind") != "delta":
			seq = STORE.append_event(sid, event)
			if seq is not None:
				event = {**event, "seq": seq}
		emit(event)
	return wrapped


def make_recorder(turn_id: str, start_no: int = 1):
	"""造一个"记一条原始消息"的回调,交给 agent 循环。

	turn_id 由服务端绑死在这儿,循环那头只管说"产生了什么" —— 它不知道
	自己在哪个会话、第几轮,也不该知道。

	message_no 从 2 开始:1 是用户那条,建轮的时候已经写进去了(begin_turn)。
	号是内存里数的,写失败会留下空号 —— 允许,这一版明确不重编号。

	**返回值必须原样交出去。** 它就是那一行的行号,而循环拿它当号拼在结果
	正文的尾巴上(tools/compress.py 的 recall 按这个号查回原文)。吞掉它的话号发不出来
	—— 表现是模型看不见任何号、compress 和 recall 一起变成哑的,而且不报错。

	**这里一律严格写**(strict=True):循环记的三种(assistant 响应、工具结果、
	控制消息)都是恢复关键的那三种,少一条,模型接下来看到的历史就是缺的。
	写不进去就抛 PersistError,一路传到 _run_turn 把这一轮标成 interrupted ——
	而不是像页面事件那样"少一条就算了"。热路径那一档(events)没变。

	start_no 是**恢复**用的:续跑那一轮接着库里最大的号往下发。不接着发
	(比如又从 2 开始)会撞 UNIQUE(turn_id, message_no) —— 那是报错,还算
	好的;更坏的是号重复之后,模型手里那个 m 号指向了**另一条**消息,而且
	不报错。

	.last_no 是这一轮的**覆盖水位**:已经落库的最大 message_no。快照存的时候
	把它一起写上(见 _checkpoint),恢复判定就靠它跟 turn_messages 比出"还有
	没有尾部"。所以它必须严格跟着成功的写入走 —— 写失败了要抛,不能把它
	往前推。
	"""
	next_no = count(start_no + 1)

	class Recorder:
		def __init__(self):
			self.last_no = start_no

		def __call__(self, kind: str, role: str, content,
		             tool_use_id: str | None = None) -> int:
			no = next(next_no)
			row = STORE.append_turn_message(turn_id, no, kind, role, content,
			                                strict=True, close_exec=tool_use_id)
			self.last_no = no
			return row

	return Recorder()


def quiet(emit):
	"""包成"写不出去也不吭声"的版本,交给 agent 循环和压缩器。

	为什么必须有这一层:agent_loop 和压缩器都是直接调 emit 的,那里没有
	try。页面一关(或者切走之后那条流断了),wfile.write 抛 OSError 会一路
	掀翻整个循环 —— **今天就是这样**:关掉页面等于杀掉这一轮。

	而"切走的会话继续跑完"要的正好相反,所以交给循环的必须是安静版。
	代价说清楚:一个被忘掉的标签页会把这一轮的钱烧完,边界是现成的
	(MAX_ROUNDS、ASK_TIMEOUT、侧栏上看得到的"在跑")。

	两个提问器(make_ask / make_ask_text)拿的**不是**这份,而它们底下共用的
	_ask_and_wait 靠 emit_quietly 的返回值判断"还有人能回答吗"。给它安静版
	的话,页面一关就没人回答,而 agent 会在那儿干等 300 秒。
	"""
	return lambda event: emit_quietly(emit, event)


# 一个问题最多等多久。超了算没答上。
#
# 想短一点也行,但注意代价不对称:等太久只是页面刷不出新一轮(这期间
# 同一个会话的请求全是 409),放行放错是把机器交出去。所以宁可等。
#
# 模型提问(ask 工具)共用这一个超时。它没这么强的方向性 —— 没答上就是
# 没答上,模型收到一句报错然后自己拿主意 —— 但为此多一个旋钮不值当,
# 而侧栏那个"在等你回答"本来就看得见。
ASK_TIMEOUT = 300.0

# 挂起的问题。key 是 ask 事件的 id,value 是那个槽。
#
# 为什么需要这张表:agent 循环跑在 POST /ask 那条线程里,它要停下来等人;
# 而人的回答从另一条连接(POST /answer)进来 —— 两条线程之间没有别的
# 交汇点。Event 负责"停",槽里那个字段负责"答案"。
#
# 槽里记着 session 只是为了 /sessions 能报出"哪个会话在等你",
# 记 question/turn_id 是为了 /turns 能把它补回去(见 _get_turns)。
# mode 是为了页面知道画什么(是/否按钮还是输入框),也是 /answer 分派
# 两种回答的依据。认槽始终只看 rid —— 它就是那张能力凭证,/answer 不
# 需要知道会话,也不知道自己答的是哪一种,那是槽自己说了算。
#
# 两种问题(权限确认 / 模型提问)**共用这一张表**:鉴权、超时、清理、
# "谁在等"的统计只有一份。分头写的话,一边加了闸另一边会漏 —— 而漏的
# 那边不报错,只会留下一个永远清不掉的槽。
PENDING: dict[str, dict] = {}

# **这一轮的结果没存进库**的记录。key 是 sid,value 是补写所需的那份参数
# (turn_id / 终态 / 最终上下文)。
#
# 为什么留在进程里而不是库里:库正是写不进去的那一方。它只活到"这个会话的下
# 一次请求"为止 —— 那一刻先把这份补写进去(**不重跑模型、不重跑工具**,手上
# 就有最终上下文),补进去了才允许开新的一轮;补不进去就回 503。少了这道闸,
# 下一轮会拿那份**旧上下文**继续跑,而模型完全看不出上一轮发生过什么。
UNSAVED: dict[str, dict] = {}


def _ask_and_wait(emit, sid: str, turn_id: str, mode: str,
                  **fields) -> tuple[dict | None, str]:
	"""把一条 ask 挂到流上,然后挂住等回答。返回 (槽, 没答上的原因)。

	槽是 None 就表示没人答上,第二项是给人看的原因(写进 record);答上了
	时第二项是空字符串。

	mode 决定页面画什么,也决定 /answer 往回写哪个字段 —— 见 PENDING。

	这里拿到的是**不安静**的 emit,不能换成 quiet():emit_quietly 的返回值
	就是"还有人能回答吗",安静版把失败吞掉了,于是页面关掉之后这儿会干等
	满 300 秒才走。
	"""
	rid = uuid.uuid4().hex
	slot = {"event": threading.Event(), "mode": mode, "session": sid,
	        "turn_id": turn_id, **fields}
	PENDING[rid] = slot
	try:
		if not emit_quietly(emit, {"kind": "ask", "id": rid, "mode": mode,
		                           **fields}):
			return None, "流已经断了,没人能回答"
		if not slot["event"].wait(ASK_TIMEOUT):
			emit_quietly(emit, {"kind": "note", "source": "ask",
			                    "text": f"no answer in {ASK_TIMEOUT:.0f}s"})
			return None, f"等满 {ASK_TIMEOUT:.0f}s 没人回答"
		return slot, ""
	finally:
		# 无论哪条路径出去都要清,不然 PENDING 会一直涨。
		PENDING.pop(rid, None)


def make_ask(emit, sid: str, turn_id: str, record):
	"""造一个把问题推给浏览器、然后挂起等回答的**权限确认器**。

	和 emit 一样按请求建:它绑在那条响应流上,而流是每请求一条。这也正好
	对应"一轮只有一个确认在飞"——同一轮里工具是顺序跑的。

	超时和断连都算拒绝,不放行:否则"关掉页面"就成了提权手段。

	record 是记原始消息的那个回调。确认本身不是消息,但**这个决定要记**:
	不记的话,刷新之后这一轮的记录里就完全看不出"它当时问过你、你是怎么
	答的",而这恰恰是回头看时最想知道的一件事(比如那个要读仓库外面文件
	的调用,你到底放没放行)。

	模型提问那个(make_ask_text)不记 —— 问题在模型那条 tool_use 里、回答在
	紧随其后的 tool_result 里,agent_loop 两头都记了,再记一条是同一个决定
	在库里存两遍。
	"""
	def ask(question: str) -> bool:
		slot, why = _ask_and_wait(emit, sid, turn_id, "confirm",
		                          question=question)
		if slot is None:
			allowed, verdict = False, f"{why},按拒绝处理"
		else:
			allowed = bool(slot.get("allow"))
			verdict = "已允许" if allowed else "已拒绝"
		record("control", "user", f"[permission] {question} → {verdict}")
		return allowed
	return ask


def make_ask_text(emit, sid: str, turn_id: str):
	"""造一个模型提问用的提问器:推到浏览器,挂起等一段文字。

	返回 None = 没人答上(超时 / 页面关了),**不是空字符串** —— 空回答是
	一句合法的回答,模型得分得清"人说了句空的"和"根本没人在",见 tools/ask.py
	那条报错。空字符串从 /answer 那头就进不来。

	跟 make_ask 一样按请求建,理由也一样:它绑在那条响应流上。
	"""
	def ask_text(question: str, options: list[str]) -> str | None:
		slot, _ = _ask_and_wait(emit, sid, turn_id, "question",
		                        question=question, options=options)
		return None if slot is None else slot.get("answer")
	return ask_text


def _interrupted_text(marked: bool) -> str:
	"""中断那句话说给用户听。分两种:标上了没有,因为能做的事不一样。"""
	if marked:
		return ("这一轮被中断了(关键记录没存进库,不能再往下跑)。库里停在"
		        "上一个完整回合的检查点,可以继续,也可以放弃。")
	return ("这一轮被中断了,而且中断状态也没写进库(库现在写不进去)。"
	        "这一轮在库里还是 running,下次启动会被收成中断。别再改工作区,"
	        "先看看库为什么写不进去。")


def _code_fingerprint() -> str:
	"""相关源文件的内容指纹。

	**不用 git HEAD 代表代码版本**:这个仓库的工作区经常有未提交的改动,
	而"改了代码、重启、点继续"正是最需要拦住的一种不兼容。指纹取的是
	真正会影响 agent 行为的那几个文件,读一次缓存一份 —— 进程活着的期间
	代码不会变(要变就得重启,那时缓存也跟着没了)。
	"""
	global _CODE_FP
	if _CODE_FP is None:
		digest = hashlib.sha256()
		for name in ("agent.py", "app.py", "context.py", "sessions.py",
		             "server.py", "tools/__init__.py"):
			try:
				digest.update(Path(__file__).resolve().parent.joinpath(
					name).read_bytes())
			except OSError:
				digest.update(b"?")
		_CODE_FP = digest.hexdigest()[:16]
	return _CODE_FP


def recovery_signature(system: str, tools: list) -> str:
	"""恢复兼容性签名:模型、提示词、工具、工作目录、代码,合成一个短串。

	存进快照,恢复时再算一遍比对。它**不是**校验和(不保证内容没坏),
	它回答的是另一个问题:这一轮接着跑,模型看到的还是当时那一套吗。
	不一致就拒绝、把原因显示出来,而不是静默换一套继续。
	"""
	material = json.dumps({
		"protocol": 1,
		"model": MODEL,
		"system": system,
		"tools": [[t.name, t.to_wire()] for t in tools],
		"cwd": str(WORKDIR),
		"code": _code_fingerprint(),
	}, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _signature_tools():
	"""算签名用的工具集:只要 schema,不要任何每轮才有的东西。

	所以 ask 和 todo 随便给个不动的实现 —— 它们的 schema 跟谁绑着无关。
	少写这一句的话,算签名就得多造一条响应流出来,而那意味着签名这件事
	被绑在了"正在跑的一轮"上。
	"""
	return build_tools(TodoManager(), lambda question, options: None)


def restore_todos(sid: str, items) -> tuple[int, int]:
	"""把快照里的待办清单装回这个会话。返回(装回去几条,丢掉几条)。

	**要校验,不能直接塞进 manager。** 这份内容来自库里,而库里的东西
	原则上是**不可信资料**(它最初来自模型的一次工具调用)。直接赋值等于
	绕过了 TodoManager.update 那一整套校验,往后每一次 render 都在拿
	一份没人检查过的结构去拼字符串。

	丢掉的东西不吭声是不行的:那是"我再打开这个会话,清单少了两条" ——
	所以数字交回调用方,由它发一条事件说清楚。
	"""
	todo = todo_for(sid)
	kept, dropped = [], 0
	for item in items if isinstance(items, list) else []:
		if isinstance(item, dict) and isinstance(item.get("content"), str) \
				and item.get("status") in ("pending", "in_progress", "completed"):
			kept.append({"content": item["content"], "status": item["status"]})
		else:
			dropped += 1
	todo.items = kept
	return len(kept), dropped


def _interrupt_note(info: dict) -> str:
	"""放弃时写进上下文的那段话。

	**它必须引用库里的证据,不能凭印象写。** 这段话的唯一用处是让模型
	下一轮知道"这个世界有一块是不确定的";说得比证据更狠(把所有工具都
	说成可能跑了)会让它缩手缩脚,说得比证据更轻(说成"什么都没发生")
	会让它踩在别人的半成品上继续做。

	所以:已经开始、没有结果的那些工具,连名字和参数一起列出来 —— 那是
	库里的行;而"可能已经生效、没有被回滚"这句必须写,否则模型会默认
	那些操作是干净的。
	"""
	lines = [f"用户结束了这次被中断的任务(第 {info.get('turn_no')} 轮)。"]
	unknown = info.get("unknown_tools") or []
	if unknown:
		lines.append("以下操作**已经开始执行、但没有留下结果**,它们做没做成"
		             "无法确定,而且**没有被回滚**:")
		for item in unknown:
			lines.append(f"- {item['name']}({json.dumps(item['input'], ensure_ascii=False)})")
		lines.append("继续之前先确认它们的实际效果(读文件、看状态),不要假设"
		             "它们没发生,也不要重复执行。")
	else:
		lines.append("没有已经开始却没有结果的操作。")
	lines.append("这是用户的选择,不是失败原因;接着做别的事情时把它当成背景。")
	return "\n".join(lines)


def _save_failure_text(conflict: bool, detail: str) -> str:
	"""保存失败那句人话。两种情况分开说 —— 它们要用户做的事不一样。"""
	if conflict:
		return (f"这一轮在库里已经是终态(状态冲突),这次算出来的工作上下文"
		        f"**没有**写进去:下一轮会从库里已有的那份接着跑,可能少了这一轮的"
		        f"记录。这不是库坏了:{detail}")
	return (f"这一轮跑完了,但结果没存进库(刷新会退回上一轮):{detail}。"
	        f"库里这一轮还停在 running;下一次问这个会话会先试着把它补上,"
	        f"补不上就不会开新的一轮。")


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


	# 续跑时写给模型的那句。它得说清两件事:这是接着跑(不是新任务),
	# 以及不要重做已经做完的部分 —— 否则模型看到一段没头没尾的历史,
	# 最自然的反应是从头再来一遍,而那些工具是有副作用的。
RESUME_NOTE = ("服务在这一轮中断的地方继续了(同一个任务,不是新任务)。"
               "前面已经完成的部分仍然有效,不要去重做它们;"
               "从当前状态接着往下做,或者直接给出结论。")


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

	def _send_json(self, obj, status: int = 200) -> None:
		body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
		self.send_response(status)
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
		elif parts == ["assets", "whale-girl.png"]:
			self._pet_image()
		elif parts == ["sessions"]:
			self._get_sessions()
		elif len(parts) == 3 and parts[0] == "session" and parts[2] == "events":
			self._get_events(parts[1], query)
		elif len(parts) == 3 and parts[0] == "session" and parts[2] == "turns":
			self._get_turns(parts[1])
		elif (len(parts) == 5 and parts[0] == "session" and parts[2] == "turn"
		      and parts[4] == "review"):
			self._get_review(parts[1], parts[3])
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

	def _pet_image(self):
		body = PET_IMAGE.read_bytes()
		self.send_response(200)
		self.send_header("Content-Type", "image/png")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "public, max-age=86400")
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

	def _get_turns(self, sid: str):
		"""这一页要的东西:每一轮,连同它自己的原始消息。

		刷新之后轮次展示走这条,不走事件重放 —— 事件是过程(工具在跑、
		重试、旁注),而轮次要的是"问的是什么、答的是什么、调了什么工具",
		那些在 turn_messages 里是完整的。两边都画一遍就得写一套去重规则,
		而这种规则迟早会漏。

		返回里带一个 cursor:它是"截到哪条事件为止"。页面拿它当起点,只画
		之后的新事件,于是刷新前后不会重画同一批东西。
		"""
		if not STORE.session_exists(sid):
			self.send_error(404, "no such session")
			return
		payload = STORE.list_turns(sid)
		payload["running"] = is_running(sid)

		# 每一轮花了多少。账本里的归属键就是上面那个 turn.id —— 写的时候是
		# usage.span(session=sid, turn=turn["id"]),读的时候同一个字符串,不用
		# 再对一次。
		#
		# 整份账本读一遍(read_session),不是每轮读一遍:结果一样,后者把
		# 同一个文件读 N 遍,而 N 随会话长度涨。
		#
		# 发的是**渲染好的那一行**(usage.turn_line),不是几个裸数字让页面自己
		# 拼。金额和命中率的规矩(None 和 0 不同、币种跟着记录走、命中率的分母
		# 是输入总量)写两遍就会漂,而漂了不报错 —— 只是页面上的数和报表上的数
		# 不一样。
		ledger = usage.read_session(sid)
		for turn in payload["turns"]:
			turn["usage"] = usage.turn_line(ledger.get(turn["id"], []))

		# 挂起中的问题要一起给。它是**唯一**没法从库里重建的东西:ask 不是
		# 一条消息,库里没有它,而页面拿到的 cursor 已经越过它那条事件了 ——
		# 不补这一下,刷新之后页面上就没有那个按钮或那个输入框,而 agent
		# 那头正挂在上面等满 300 秒然后往下走。
		#
		# mode 和 options 必须跟着来:少了它们,一个提问会被画成是/否两个
		# 按钮 —— 而按钮点下去发的是 allow,到了服务端被 mode 挡回来,
		# 于是人看着页面上有个能点的东西,点了什么也没发生。
		#
		# 别的直播事件都不用补:thinking / tool_call / 工具输出都能在
		# turn_messages 里找到,旁注(retry、压缩)丢了也不影响读。
		with REGISTRY:
			payload["pending"] = [
				{"kind": "ask", "id": rid, "mode": slot["mode"],
				 "question": slot["question"], "options": slot.get("options", []),
				 "turn_id": slot["turn_id"]}
				for rid, slot in list(PENDING.items()) if slot["session"] == sid
			]

		# "这一轮的结果没存进库"那份记录也要一起给,理由跟上面一样:它在库里
		# 读不出来 —— 保存失败的话那一轮**还停在 running**(终态那一次写就没
		# 成功),刷新之后页面只会显示"运行中",而真相是它已经跑完了、只是
		# 没存上。少了这一句,用户会以为它还在干活,或者以为自己的活白干了。
		#
		# 只在**这一轮**还在库里挂着的时候报:补写成功之后库里就是终态了,
		# 那份记录也没了(见 _flush_unsaved),两条路不会同时说话。
		unsaved = UNSAVED.get(sid)
		if unsaved:
			for turn in payload["turns"]:
				if turn["id"] == unsaved["turn_id"]:
					turn["unsaved"] = {"model_status": unsaved["status"],
					                   "error": unsaved["error"]}
		self._send_json(payload)

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

		# 旧版记录 = 这一版之前留下的、不带 turn_id 的事件。它们只能靠重放
		# 画出来(那会儿还没有 turn_messages 可读),而新的事件都由轮次
		# 接口负责,重放一遍就是重复。
		#
		# 在服务端筛而不是把全部事件推给页面让它自己挑:这个参数的存在
		# 意味着页面本来就不该看见那些。
		legacy_only = query.get("legacy", ["0"])[0] == "1"

		events = []
		for seq, stored in STORE.events_since(sid, since):
			if legacy_only and stored.get("turn_id"):
				continue
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
		elif (len(parts) == 5 and parts[0] == "session" and parts[2] == "turn"
		      and parts[4] == "resume"):
			self._post_resume(parts[1], parts[3])
		elif (len(parts) == 5 and parts[0] == "session" and parts[2] == "turn"
		      and parts[4] == "abandon"):
			self._post_abandon(parts[1], parts[3])
		else:
			self.send_error(404)

	def _post_session(self):
		# 两份记忆的快照都在**建会话这一刻**读一次,然后跟着这个会话走到底。
		# 读文件的事在这儿做,不在 sessions.py 里 —— 那个模块只干存取,
		# 理由见它文件头。
		#
		# 这个会话后面写进去的记忆,不会反过来改它自己那两份快照,所以一个
		# 会话从头到尾的 system prompt 逐字节不变 —— 而 system 是前缀缓存
		# 的锚点,变一个字后面整段历史都要重算。代价是写入下个会话才生效。
		row = STORE.create_session(load_memory(MEMORY_PATH),
		                           load_memory(USER_MEMORY_PATH))
		self._send_json({"id": row["id"], "title": row["title"]})

	def _post_delete(self, sid: str):
		"""删会话。**跟执行抢同一把会话锁** —— 这是 R4 修的那件事。

		以前是先问 is_running()、再删,而问和删之间没有互斥区间:
		"删除线程看到空闲 → 执行线程取得锁并开轮 → 删除线程把会话删掉",
		于是那一轮开始写回时命中的是一个不存在的会话(外键错误),而用户
		看到的是"聊到一半的东西没了"。

		现在两条路都必须先攥住同一个 sid 的那把锁:删除拿不到就返回冲突
		(确实有一轮在跑),拿到了就等于"从这一刻起不会有新的轮开起来"。
		检查和操作在同一个互斥区间里,中间没有缝。

		顺序也反过来了:先拿锁,再查存在性。先查的话,查完到拿锁之间会话
		可能已经被别的删除请求删掉了 —— 而那种"删一个已经不在的东西"报
		200 挺好(幂等),报 404 也行,但绝不能是"查到了、删的时候没有"。
		"""
		lock = session_lock(sid)
		if not lock.acquire(blocking=False):
			self.send_error(409, "this session has a turn running")
			return
		try:
			if not STORE.session_exists(sid):
				self.send_error(404, "no such session")
				return
			STORE.delete_session(sid)
		finally:
			lock.release()
		# 进程里那两份(LOCKS/TODOS)不跟着收:它们只增不删,理由见上面。
		# 剩下几个没人用的 dict,在本机工具里不值得为它引入删除的竞态。
		self._send_json({"ok": True})

	def _review_info(self, sid: str, tid: str) -> dict:
		"""算一份"这一轮现在什么状况"。锁内调用,页面和两个入口都用它。

		签名现算:比对的是**现在**这套模型/提示词/工具/代码,所以它必须
		在请求这一侧算,不能存起来复用 —— 存起来就等于拿旧代码跟旧快照比,
		永远一致。
		"""
		system = build_system(*STORE.get_memory_snapshots(sid))
		return STORE.checkpoint_info(sid, tid, MAX_ROUNDS,
		                             recovery_signature(system,
		                                                _signature_tools()))

	def _get_review(self, sid: str, tid: str):
		"""这一轮的中断详情:页面拿它画"继续 / 放弃"和那段证据清单。

		**只读。** 点开看看不该改变任何东西 —— 用户可能只是想先搞清楚
		发生过什么再决定。
		"""
		if not STORE.session_exists(sid):
			self.send_error(404, "no such session")
			return
		self._send_json({"ok": True, "info": self._review_info(sid, tid)})

	def _post_resume(self, sid: str, tid: str):
		"""继续一次被中断的任务。

		顺序是:锁 → 补写上一轮欠的 → **在锁内重新算一遍**可恢复性 →
		版本比对 → 写状态 → 跑。

		重新算那一遍不能省。页面显示"可以继续"是几秒前的事,而这中间
		可能有人在另一个标签页里放弃了它、或者它已经被恢复过一次 ——
		那时候再拿页面传来的判断往下走,就是在一个已经变了的状态上执行。
		锁在手里只保证"从现在起没人能再改",不保证"从我算完到现在没人改过",
		所以每次判断都要落在**当前**这份数据上,而版本号就是那道闸。
		"""
		body = self._json_body('{"version": 1}')
		if body is None:
			return
		try:
			expected = int(body.get("version"))
		except (TypeError, ValueError):
			self.send_error(400, "version must be an integer")
			return

		lock = session_lock(sid)
		if not lock.acquire(blocking=False):
			self.send_error(409, "this session already has a turn running")
			return
		try:
			if not STORE.session_exists(sid):
				self.send_error(404, "no such session")
				return
			if not self._flush_unsaved(sid):
				self.send_error(503, "previous turn result is not saved yet")
				return
			info = self._review_info(sid, tid)
			if not info.get("resumable"):
				self._send_json({"ok": False, "reason": info.get("reason"),
				                 "detail": info.get("detail"),
				                 "info": info}, 409)
				return
			runtime = info["runtime"]
			snapshot = STORE.load_context(sid)
			try:
				started = STORE.resume_turn(sid, tid, expected, snapshot,
				                            RESUME_NOTE, runtime)
			except (CheckpointConflict, TurnStateConflict) as e:
				self._send_json({"ok": False, "reason": "conflict",
				                 "detail": str(e)}, 409)
				return

			# 待办清单是从库里装回来的,而这个进程可能刚起来(内存里那份
			# 是空的)。装不回去的条数要说出来 —— 悄悄少两条,模型下一轮
			# 就会按一份少了东西的清单干活。
			kept, dropped = restore_todos(sid, runtime.get("todos"))
			turn = {"id": tid, "turn_no": info["turn_no"]}
			self.send_response(200)
			self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
			self.send_header("Cache-Control", "no-store")
			self._cors()
			self.end_headers()
			emit = recording_emit(sid, ndjson_emit(self.wfile), turn)
			if dropped:
				emit_quietly(emit, {"kind": "note", "source": "store",
				                    "text": f"待办清单里有 {dropped} 条认不出来,"
				                            f"只装回了 {kept} 条"})
			emit_quietly(emit, {"kind": "note", "source": "resume",
			                    "text": f"从第 {info['turn_no']} 轮的检查点继续"
			                            f"(用了 {info['rounds_used']}/{MAX_ROUNDS} 回合预算)"})
			# 起跑线三个数各有各的来处,也不能互相替代:
			#   rounds_start  取快照里那个数和库里那个数的**较大值** ——
			#                 库里的数只增不减(每次请求前预留),而快照可能
			#                 比真实进度旧。取小的那侧等于白送额度。
			#   start_no      record 接着库里最大的号往下发,不能从头开始
			self._drive(sid, turn, started["messages"], 
			            STORE.get_memory_snapshots(sid),
			            runtime.get("active_request") or "",
			            record=make_recorder(tid,
			                                 start_no=started["message_no"]),
			            emit=emit,
			            start_rounds=max(int(runtime.get("rounds") or 0),
			                             int(info.get("rounds_used") or 0)),
			            rounds_since_todo=int(runtime.get("rounds_since_todo") or 0),
			            first_time=False)
		finally:
			lock.release()

	def _post_abandon(self, sid: str, tid: str):
		"""放弃一次中断的任务。

		**不可续跑的时候更要能放弃** —— 那正是唯一出路。所以这里不查
		resumable,只要求"它确实是中断状态"。

		放弃 ≠ 撤销:文件改过的还在。所以写进上下文的那段话是从库里的证据
		生成的(见 _interrupt_note),页面那边也要照这个口径说,不能画成
		"回到之前"。
		"""
		body = self._json_body('{"version": 1}')
		if body is None:
			return
		try:
			expected = int(body.get("version"))
		except (TypeError, ValueError):
			self.send_error(400, "version must be an integer")
			return

		lock = session_lock(sid)
		if not lock.acquire(blocking=False):
			self.send_error(409, "this session already has a turn running")
			return
		try:
			if not STORE.session_exists(sid):
				self.send_error(404, "no such session")
				return
			info = self._review_info(sid, tid)
			if info.get("status") != "interrupted":
				self._send_json({"ok": False, "reason": info.get("reason"),
				                 "detail": info.get("detail") or
				                           "这一轮不是中断状态", "info": info}, 409)
				return
			snapshot = STORE.load_context(sid)
			try:
				out = STORE.abandon_turn(sid, tid, expected,
				                         _interrupt_note(info), snapshot)
			except (CheckpointConflict, TurnStateConflict) as e:
				self._send_json({"ok": False, "reason": "conflict",
				                 "detail": str(e)}, 409)
				return
			self._send_json({"ok": True, "version": out["version"]})
		finally:
			lock.release()

	def _post_answer(self):
		"""回答一条挂起的问题。确认和提问都走这儿,靠槽自己的 mode 分派。

		不开第二个端点:挂起、超时、清理、"谁在等"这套东西只该有一份,而它
		已经在 PENDING 里了。这也是必须认槽才分派的原因 —— 请求体长什么样
		不能说了算,否则谁都拿 allow 往一个提问的槽里塞。

		只看 id 认槽,槽就是那张能力凭证。
		"""
		body = self._json_body('{"id": "...", "allow": true | "text": "..."}')
		if body is None:
			return

		slot = PENDING.get(str(body.get("id", "")))
		if slot is None:
			# 已经超时清了,或者 id 是编的。不是错误 —— 页面可能只是点慢了,
			# 那一轮早就按拒绝往下走了。
			self.send_error(409, "no such pending question")
			return

		if slot["mode"] == "question":
			text = clean_query(body.get("text", ""))
			if not text:
				# 空回答在**清槽之前**拒掉:模型那边"人说了句空的"和"没人答"
				# 是两件事,放一个空字符串过去就把这个区别抹了。
				self.send_error(400, "empty answer")
				return
			slot["answer"] = text
		else:
			slot["allow"] = bool(body.get("allow"))
		slot["event"].set()

		out = b'{"ok": true}'
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(out)))
		self._cors()
		self.end_headers()
		self.wfile.write(out)

	def _flush_unsaved(self, sid: str) -> bool:
		"""把上一轮没存进库的那份补写进去。补上了(或者本来就没有)返回 True。

		**它不重跑模型、也不重跑工具** —— 手上就有最终上下文和终态,补的是写。
		"恢复保存时工具执行次数不增加"那条验收说的就是这件事。

		补写在开新的一轮**之前**,而且在同一把会话锁里:补不上就不开新轮
		(调用方回 503),因为开了的话模型会拿着一份少了上一轮的上下文往下跑,
		而它看不出少了什么。
		"""
		pending = UNSAVED.get(sid)
		if pending is None:
			return True
		try:
			STORE.finish_turn(sid, pending["turn_id"], pending["status"],
			                  pending["error"], pending["messages"])
		except Exception:
			# 还是写不进去。记录留着,下一次请求再试 —— 补写这件事本身是幂等
			# 的(同一个事务里的 upsert + 条件更新)。
			return False
		UNSAVED.pop(sid, None)
		return True

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
			# 拿到锁之后再查一次存在性 —— 上面那次(pre-lock)是给明显不合法
			# 的 id 一个快一点的 404;这一次才是权威的:删除要拿同一把锁,
			# 所以"锁在我手里"就等于"此刻没人能把它删掉"。少了这一下,
			# 删除先拿到锁那条路径上,这一轮会走到 begin_turn 才撞外键,
			# 用户拿到的是一句 500 "cannot start turn" —— 而事实是会话
			# 已经没了。
			if not STORE.session_exists(sid):
				self.send_error(404, "no such session")
				return
			# 上一轮的结果没存进库的话,先把那份补上再开新的一轮。补不进去就
			# 不开 —— 503 而不是 200:这不是"这个会话忙",是"后端现在不能
			# 接着往下跑"。用户重发一次就再试一遍。
			if not self._flush_unsaved(sid):
				self.send_error(503, "previous turn result is not saved yet")
				return
			# 有还没处理的中断任务时,不开新的一轮。理由见 unresolved_interrupt:
			# 那份中断的快照是这个会话**唯一**的恢复点,而新任务的第一次
			# checkpoint 就会把它盖掉。用户先点继续或者放弃,再问新的。
			#
			# 没有这个闸的话,表现是:进程重启 → 用户没注意那一轮的提示,
			# 直接问了下一个问题 → 上一轮的工作上下文被覆盖,而页面上那轮
			# 还写着"中断、可以继续",点下去发现恢复不了。不如一开始就拦住。
			pending = STORE.unresolved_interrupt(sid)
			if pending is not None:
				# **这条消息只能用 ASCII。** send_error 的第二段是 HTTP 状态行
				# 里的 reason phrase,而状态行是按 latin-1 编码写出去的 ——
				# 一个中文就会让 send_response 抛 UnicodeEncodeError,浏览器
				# 那边看到的是"连接被断开、没有任何响应"(实测栽的就是这个)。
				# 人话放在页面那边:它按这个 409 再读一次轮次,把中断那一轮
				# 连同两个按钮画出来。
				self.send_error(
					409, "this session has an unfinished interrupted turn"
					     f" (turn {pending['turn_id']}, no {pending['turn_no']}):"
					     " resume or abandon it first")
				return
			self._run_turn(sid, query)
		finally:
			lock.release()

	def _run_turn(self, sid: str, query: str):
		"""跑一轮。全程攥着这个会话的锁(由 _post_ask 拿着并负责释放)。

		整段的顺序是:读上下文 + 开轮(都在发响应头之前)、跑循环、收尾。
		两头那两个数据库动作是短的,中间跑模型和工具的那段一个事务都不开。
		"""
		# 读历史 + 开轮放在发响应头之前:这两件事任一失败还能回一个干净的
		# 状态码,而不是已经 200 了才发现手里没有上下文、库里也没有这一轮。
		#
		# 每轮都从库里读,不在内存里留一份。看着像浪费(几十毫秒),其实是
		# 拿它换掉"内存那份和库里那份什么时候会不一致"这个问题 —— 而那个
		# 问题一旦存在,答案就是"在你想不到的时候"。顺带,重启存活是免费的。
		try:
			history = STORE.load_context(sid)
			# 这一会话建会话时冻下的那两份记忆,不是现读文件 —— 现读的话,
			# 会话中途的一次 memory 写入会把它自己这个会话的 system 也换掉,
			# 而 system 一变,前面所有轮次的缓存全废。见 _post_session。
			memories = STORE.get_memory_snapshots(sid)
			turn = STORE.begin_turn(sid, query)
		except Exception as e:
			self.send_error(500, f"cannot start turn: {type(e).__name__}: {e}")
			return

		self.send_response(200)
		self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self._cors()
		self.end_headers()

		raw = ndjson_emit(self.wfile)
		# emit 落库 + 进流;交给循环的那份是安静版(页面走了也不掀翻这一轮)。
		# make_ask 拿的必须是不安静的那份,理由见 quiet() 的注释。
		emit = recording_emit(sid, raw, turn)
		# 用户那条 begin_turn 已经写进 turn_messages 了(它是开轮那个短事务的
		# 一部分);这里只把它接到要发给模型的上下文尾巴上。
		#
		# 这一步留在**这里**而不是 _drive 里:恢复那条路要的是另一份历史
		# (从快照读出来的),它不该再被追加一次用户输入 —— 追加了的话,
		# "继续"就变成了一条新的用户提问,而这个会话的历史里会多出一句
		# 用户从没说过的话。
		history.append({"role": "user", "content": query})
		# 这一条走安静版:页面正好在这时关掉的话,不安静的那版会抛
		# OSError,把整轮带走 —— 而"切走了照跑"要的正好相反。
		emit_quietly(emit, {"kind": "you", "text": query})
		self._drive(sid, turn, history, memories, query,
		            record=make_recorder(turn["id"]), emit=emit)

	def _drive(self, sid: str, turn: dict, history: list, memories: tuple,
	           active_request: str, record, emit,
	           start_rounds: int = 0, rounds_since_todo: int = 0,
	           first_time: bool = True) -> None:
		"""跑这一轮,然后收尾。**新提问和恢复走的是同一条路。**

		两边不同的只有:历史从哪儿来、号从几号接着发、计数从多少接着数、
		以及要不要再触发一次 UserPromptSubmit —— 全在这几个参数里。别处
		一模一样,所以必须共用:任何一处"收尾不一样",迟早会长成两种自己
		会漂的行为,而恢复那条路平时没人走,漂了也看不出来。

		整段的顺序(收尾那段尤其别动):
			跑循环 → 成功就 finish_turn(上下文和终态同事务)
			       → 关键记录存不上就 mark_interrupted(只动 turns)
			       → 最后才发 reply
		"""
		silent = quiet(emit)
		tools = build_tools(todo_for(sid),
		                    # 提问器绑在这一轮这条流上,所以每轮现造。
		                    # 跟 ask= 那份不同:那个的答案是是/否(权限),
		                    # 这个是一段文字(模型提问)。
		                    make_ask_text(emit, sid, turn["id"]))
		system = build_system(*memories)
		# 恢复兼容性签名:把"模型当时看到的这一套"压成一个短串,写进快照。
		# 恢复时拿现在的再算一遍比对 —— 中间换过模型、改过提示词、动过
		# 工具集或代码,恢复出来的就不是同一个任务了,那种"接着跑"比停下
		# 来更糟:模型会拿着一份不是自己的历史继续做决定。
		frozen = {
			"format": 1,
			"active_request": active_request,
			"model": MODEL,
			"max_rounds": MAX_ROUNDS,
			"cwd": str(WORKDIR),
			"signature": recovery_signature(system, tools),
		}

		def checkpoint(messages: list, loop_state: dict, compacted: bool) -> None:
			# 循环只知道自己那两个数(第几回合、多久没更新待办),别的都从
			# 这儿补 —— 它不知道也不该知道模型名、system、工作目录这些。
			self._checkpoint(sid, turn, record, messages,
			                 {**frozen, **loop_state,
			                  "todos": todo_for(sid).items}, compacted)

		# 兜底那份:正常路径下会被覆盖。事先摆一个失败,是为了万一控制流
		# 以预料之外的方式跳出去,收尾时手里也有个说得通的终态,而不是
		# NameError —— 那会让这一轮永远停在 running。
		outcome = TurnOutcome("failed", "", "这一轮没有跑完")
		# "模型跑出什么"和"存没存上"分开记:后者在下面的 finally 里被改写。
		# 放在 try 外面,是因为 finally 要写它们,而 finally 在任何一条路径上
		# 都会跑到 —— 只写在 except 里的话,正常跑完那条路上它们是未定义的。
		saved, save_error = True, ""
		# 中断是第三种收尾,跟"跑失败"和"没存上"都不一样:它意味着**库里
		# 有一份值得接着用的状态**,而手上这份内存里的历史不可信。
		interrupted, interrupt_reason = False, ""
		try:
			# 只有新提问才触发 —— 恢复时再触发一遍,等于把用户那条指令
			# 又提交了一次,而它的 hook 可能不是纯观察的。
			if first_time:
				trigger_hooks("UserPromptSubmit", active_request)

			# 归属:这一轮里所有的 API 调用都带上 session / turn。压缩器和 vision
			# 都在这一层里面,所以它们自动跟着 —— 传参是传不到工具 handler 里的
			# (agent_loop 只给 handler 传 **block.input)。
			#
			# recall 那个工具同理:它得查"本会话"的库,还要按预览那套渲染,两样
			# 都是 handler 够不着的东西 —— 所以绑一层环境递进去。压缩器先拿出来,
			# 是因为取回器要用它渲染(大块原文落盘 + 头尾预览),跟外面这个
			# 是同一个实例。
			compactor = make_compactor(silent)
			with usage.span(session=sid, turn=turn["id"]):
				with bind_recall(make_recall(STORE, sid, compactor)):
					outcome = agent_loop(
						history,
						active_request=active_request,
						system=system,
						tools=tools,
						model=MODEL,
						max_rounds=MAX_ROUNDS,
						compactor=compactor,
						ask=make_ask(emit, sid, turn["id"], record),
						emit=silent,
						record=record,
						checkpoint=checkpoint,
						# 两阶段标记的前一半。只给有副作用的工具写,哪些
						# 算有副作用由循环自己按 ToolDesc 判断(agent.py 里
						# 的 side_effects)。
						begin_exec=lambda uid, name, data:
							STORE.begin_tool_exec(turn["id"], uid, name, data),
						# 调用前先占额度,理由见 sessions.reserve_round。
						reserve_round=lambda:
							STORE.reserve_round(turn["id"], MAX_ROUNDS),
						rounds_start=start_rounds,
						rounds_since_todo_start=rounds_since_todo)
		except PersistError as e:
			# 恢复关键的一条记录没落库(assistant 响应、工具结果、控制消息、
			# 或者回合快照)。**停在这儿,而且绝不把内存里这份历史存下去** ——
			# 它已经缺了一块,存下去等于拿一份残史盖掉最后一个可信快照。
			# 库里那一轮标 interrupted,恢复入口据此决定能不能续、要不要核对。
			interrupted, interrupt_reason = True, f"{type(e).__name__}: {e}"
			outcome = TurnOutcome("interrupted", f"Stopped: {e}", interrupt_reason)
		except Exception as e:
			# 兜底:异常不该把 history 一起带走,也不该让流断在半截
			# 而没有下文 —— 前端会一直转圈。这一轮记 failed。
			outcome = TurnOutcome("failed", f"Error: {type(e).__name__}: {e}",
			                      f"{type(e).__name__}: {e}")
		finally:
			if interrupted:
				# 只动 turns。上下文一个字都不写 —— 手上这份历史没验证过,
				# 而库里那份是最后一个可信点(见 mark_interrupted)。
				marked = STORE.mark_interrupted(sid, turn["id"],
				                                INTERRUPT_PERSIST_FAILED,
				                                interrupt_reason)
				emit_quietly(emit, {"kind": "note", "source": "store",
				                    "text": _interrupted_text(marked)})
			else:
				dropped = trim_dangling_tool_use(history)
				if dropped:
					emit_quietly(emit, {"kind": "note", "source": "round",
					                    "text": f"这一轮被打断,{dropped} 条没有结果的消息没有存"})
				try:
					# 最终上下文和 Turn 终态同一个事务。分两次写的话,中间那个
					# 窗口里下一轮会读到少了一整轮的历史,而且不报错。
					STORE.finish_turn(sid, turn["id"], outcome.status,
					                  outcome.error, history)
				except Exception as e:
					# **模型跑出什么,和这一轮存没存上,是两件事。** 这里只改后
					# 一件:保存失败不能跟着 outcome.status 一起发出去 —— 页面会
					# 把它画成普通的"完成",而库里那一轮还是 running、工作上下文
					# 还是旧的,用户接着问就静默地少了一整轮。
					saved, save_error = False, f"{type(e).__name__}: {e}"
					# 状态冲突单独说:那不是"库坏了",是这一轮在库里已经是终态
					# (被 reap 收过)。它重试也没用(finish_turn 会一直抛),所以
					# 不进 UNSAVED 那道闸 —— 进了的话这个会话就永远问不下去了。
					conflict = isinstance(e, TurnStateConflict)
					if not conflict:
						UNSAVED[sid] = {"turn_id": turn["id"],
						                "status": outcome.status,
						                "error": outcome.error,
						                "messages": history}
					emit_quietly(emit, {"kind": "note", "source": "store",
					                    "text": _save_failure_text(conflict, save_error)})

		# reply 排在收尾**之后**发。反过来的话,页面收到 reply 时库里的
		# status 还是 running,而它的游标已经越过这条事件 —— 刷新也补不
		# 回来,那一轮会一直显示"运行中"。
		#
		# 花费跟着一起发,理由同上一句:这一轮的所有调用都发生在上面,
		# 到这儿账已经记完了。放在这里面,页面不用为一个数字再跑一趟 ——
		# 而且那一趟还得解决"什么时候去要"的问题,而"这一轮刚结束"正好
		# 就是这里。
		#
		# **status 发的是"这个页面该显示成什么",不是模型的心气。** 保存失败
		# 时发 unsaved:发 completed 就等于告诉页面"存好了,可以接着聊",而
		# 库里那一轮还是 running、上下文还是旧的。中断发 interrupted,而且
		# 带上原因 —— 页面据此画"继续 / 放弃"两个按钮,以及一句人话。
		emit_quietly(emit, {"kind": "reply", "text": outcome.text,
		                    "status": ("interrupted" if interrupted
		                               else outcome.status if saved else "unsaved"),
		                    "model_status": outcome.status,
		                    "interrupted": interrupted,
		                    "save_error": save_error, "saved": saved,
		                    "usage": usage.turn_line(
			                    usage.read_turn(sid, turn["id"]))})

		# 同一行也打到终端。**一处生成,两处显示**:这一行是
		# usage.turn_line 渲染好的,页面和终端拿的是同一个字符串 —— 各写一遍
		# 会在钱、命中率、币种这些地方慢慢分家,而那正是这一行要回答的问题。
		#
		# 位置在收尾之后:这一轮所有的调用都发生在上面,到这儿账才记完。
		# 例外是"保存失败"那条路(finish_turn 抛了)—— 那也不影响这一行,
		# 账在调用发生时就一笔笔记下了(见 usage.meter),不靠收尾补。
		#
		# 状态词只在**不是正常完成**时补上:终端上没有页面上那个轮次框,
		# 一行数字孤零零地摆着,看不出这一轮是跑完了还是被打断了。
		line = usage.turn_line(usage.read_turn(sid, turn["id"]))
		if line:
			mark = "" if outcome.status == "completed" and not interrupted else (
				" · 中断" if interrupted
				else " · 没入库" if not saved else " · 失败")
			print(f"[第 {turn['turn_no']} 轮] {line}{mark}", flush=True)

	def _checkpoint(self, sid: str, turn: dict, record, messages: list,
	                runtime: dict, compacted: bool) -> None:
		"""每个完整回合存一份快照。**存不上就抛 PersistError,这一轮停下。**

		跟以前那版(压缩器回调,存不上只发一条旁注)的差别是有意的。这份
		快照现在是恢复的唯一基础,涵盖的是"到这个完整回合为止"的历史。
		存不上还接着跑,后面那些回合就全在"没有恢复点"的状态里 —— 进程一崩
		丢掉的是好几个回合的工作,而且没有任何迹象。代价是:一次写失败会
		让这一轮停下来,而以前它会跑完。这个交换在这一版是划算的,因为
		"停下来"现在有出路了(库里那份能续跑),以前没有。

		水位取 record.last_no,不另外数:它就是"这个 turn 里已经落库的最大
		message_no",而快照正文里包含的正是这些记录。两者在同一个地方往前
		走,才不会出现"正文里有、水位说没有"这种自相矛盾的快照。
		"""
		try:
			STORE.save_checkpoint(sid, turn["id"], record.last_no, messages,
			                      runtime, compacted=compacted)
		except Exception as e:
			raise PersistError(
				f"这一轮的回合快照没存上({type(e).__name__}: {e})—— 停在这儿,"
				f"库里保留的是上一个完整回合那份") from e

	def log_message(self, fmt, *args):
		# 默认实现往 stderr 打一行每个请求。这个服务是本机自用的,前端
		# 就在同一个终端里跑,不需要它再复述一遍。
		pass


if __name__ == "__main__":
	# 顺序是:排他锁 → 迁移 → 清理 → 收请求。
	#
	# 第一件就是拿锁:拿不到直接退出,**在这之前一个字节都不写库**。这是
	# "第二个实例不能改第一个实例正在跑的任务"的唯一保证 —— 端口不算保证,
	# 换个端口、或者 Windows 上 SO_REUSEADDR 那种语义,都能两个进程绑同一个库。
	try:
		open_store()
	except dblock.AlreadyRunning as e:
		print(str(e))
		print("一个库只能有一个 server —— 两个一起跑会互相改状态:")
		print("后来的那个一启动就会把所有 running 的轮收成失败,而它分不出"
		      "那是别人正在跑的。")
		print("先把那个关掉(dev.py 里叫它别起新的那个情况也一样),再起这个。")
		sys.exit(1)

	# 收拾上一次没跑完的轮:进程被杀时那一轮就没有最后那次状态写,库里会
	# 留着一条永远 running 的行,页面上的轮次框也就一直显示"运行中"。放在
	# 这儿而不是 SessionStore.__init__ 里,理由见 reap_running 的注释。
	# 现在有排他锁兜着,"留下的"确实只可能是死进程留下的。
	reaped = STORE.reap_running()
	if reaped:
		print(f"上次没跑完的 {reaped} 轮已标记为失败")
	print(f"http://localhost:{PORT}/")
	try:
		ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
	finally:
		# 正常退出也交还那把锁(进程死掉时内核也会放,这一步只是"早一点" +
		# 让 Ctrl+C 之后能立刻再起一个)。
		DB_LOCK.release()
