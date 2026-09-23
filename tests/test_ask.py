"""ask 工具的特征化测试:工具逻辑、接线、以及那条挂起/回答的通道。

盯五件事:

  一、回答**原样**交回模型 —— 不管它是点按钮来的还是敲进去的。
  二、`None` 和 `""` 是两件事:`None` 是"没人答上"(超时 / 页面关了),
     `""` 是一句(空的)回答。抹平了的话,模型分不出"人不在"和
     "人没说话",而它该做的下一步完全不同
  三、选项的闸:压成一行、去空、封顶 6 个
  四、**每轮现造**:ask 的 handler 绑着"这一轮的问题往哪条流上问",两次
     build 拿到同一个 ToolDesc 的话,第二个会话的问题会推到第一个会话的
     页面上去 —— 而两边都不报错
  五、子 agent 拿不到(它的 SYSTEM 头一句就是"nobody can answer questions"),
     以及服务端那条通道:事件带 mode/options、回答写回槽、断流当场算没人答上
     (不是干等满 300 秒)

跑法: uv run pytest
"""

import importlib
import inspect
import threading
import time

import pytest

from tools import build_tools
from tools.todo import TodoManager

# 用 importlib 而不是 `import tools.ask as A`:`as` 形式取的是**包上的属性**
# 而不是 sys.modules,而属性会被同名变量覆掉。现在两个写法都对(模块里没有
# 叫 ask 的变量),但哪天有人加一个,`as` 那句会静默地拿到 ToolDesc ——
# tools/memory.py 当初就是这么栽的,见它文件末尾。importlib 不看属性。
A = importlib.import_module("tools.ask")


@pytest.fixture
def asked():
	"""一个把问题记下来、按脚本回答的前端。返回 (提问器, 收到的调用列表)。"""
	seen = []

	def make(answer):
		def ask_user(question, options):
			seen.append((question, options))
			return answer
		return ask_user
	return make, seen


# ---------- 一、回答原样交回 ----------

def test_回答原样交回(asked):
	make, seen = asked
	assert A.run_ask(make("SQLite"), "用哪种方式存?") == "SQLite"
	assert seen == [("用哪种方式存?", [])], seen


def test_问题两端空白削掉(asked):
	make, seen = asked
	A.run_ask(make("好"), "  用哪种?  ")
	assert seen[0][0] == "用哪种?", seen


def test_选中的选项原文交回_不是下标(asked):
	"""页面按钮上的字得在**到这儿之前**换成原文,不能递下标过来。

	下标是两边各存一半的约定:模型收到的若是 "2",它得知道那是哪个列表的
	第 2 项 —— 而那个列表在它的 tool_use 里、在页面的事件里、在这个函数的
	参数里各有一份,三份对不上时不报错。
	"""
	make, seen = asked
	assert A.run_ask(make("JSON 文件"), "用哪种?", ["SQLite", "JSON 文件"]) == "JSON 文件"
	assert seen == [("用哪种?", ["SQLite", "JSON 文件"])], seen


def test_空回答不是没人答上(asked):
	""""" 是一句回答,None 是没人在。两者都得能到这儿的出口。

	(现在的页面给不出 "" —— 服务端在 /answer 就拒了。留着这条是因为**这道
	区分是契约**:哪天有人给前端加上"空就是没答",得先看见这儿写着不该那么做。)
	"""
	make, _ = asked
	assert A.run_ask(make(""), "问") == ""
	assert A.run_ask(make(None), "问").startswith("Error:")


def test_没人答上要报错_并说清楚下一步(asked):
	make, _ = asked
	out = A.run_ask(make(None), "问")
	assert out.startswith("Error: no answer came back"), out
	# 不说"别再问一遍",模型会以为是自己问法不对,换个说法重问 —— 而真正
	# 的原因是人不在,重问还是没人答。
	assert "final message" in out, out


def test_问题空的就报错(asked):
	make, seen = asked
	assert A.run_ask(make("好"), "   ").startswith("Error:")
	assert seen == [], "问题都没成形,不该去烦前端"


# ---------- 二、选项的闸 ----------

def test_选项去空_压成一行(asked):
	make, seen = asked
	A.run_ask(make("好"), "问", ["  甲  ", "", "  ", "乙\n丙"])
	assert seen[0][1] == ["甲", "乙 丙"], seen


def test_没给选项就是空列表(asked):
	make, seen = asked
	A.run_ask(make("好"), "问")
	A.run_ask(make("好"), "问", None)
	assert seen == [("问", []), ("问", [])], seen


def test_选项超过6个就拒(asked):
	make, seen = asked
	out = A.run_ask(make("好"), "问", [f"选项{i}" for i in range(A.MAX_OPTIONS + 1)])
	assert f"over the {A.MAX_OPTIONS} cap" in out, out
	assert seen == [], "被拒了还去问人"


def test_正好6个能过(asked):
	make, seen = asked
	opts = [f"选项{i}" for i in range(A.MAX_OPTIONS)]
	A.run_ask(make("好"), "问", opts)
	assert seen[0][1] == opts


# ---------- 三、工具本身的形状 ----------

def test_handler的签名是agent_loop要的那种(asked):
	"""agent.py 按 handler(**block.input) 调,所以 partial 之后剩下的参数名
	必须跟 schema 严丝合缝。"""
	make, _ = asked
	tool = A.make_ask_tool(make("好"))
	params = inspect.signature(tool.handler).parameters
	assert list(params) == ["question", "options"], list(params)
	assert params["options"].default is None

	assert set(tool.input_schema["properties"]) == {"question", "options"}
	assert tool.input_schema["required"] == ["question"]
	assert tool.name == "ask"


def test_描述说了别拿它问权限():
	"""这条不是文风:模型很自然会写 ask("may I run this?"),而权限那条路上
	harness 本来就会问人 —— 白跑一轮,还得等人点两下。"""
	desc = A.make_ask_tool(lambda q, o: "").description
	assert "permission" in desc and "automatically" in desc, desc


# ---------- 四、接线:每轮现造 ----------

def test_进工具集():
	names = [t.name for t in build_tools(TodoManager(), lambda q, o: "")]
	assert "ask" in names, names


def test_每次build都绑当时那个提问器():
	"""这条是"每轮现造"的判据。

	ask 的 handler 闭包住"这一轮的问题往哪条流上问"(服务端那份绑着
	emit/sid/turn_id)。两次 build 要是拿到同一个 ToolDesc,第二个会话的
	问题就推到第一个会话的页面上去了 —— 而两边都不报错。
	"""
	def make(tag):
		return lambda question, options: tag

	first = [t for t in build_tools(TodoManager(), make("A")) if t.name == "ask"][0]
	second = [t for t in build_tools(TodoManager(), make("B")) if t.name == "ask"][0]
	assert first is not second, "两次 build 拿到同一个 ToolDesc"
	assert first.handler("问") == "A"
	assert second.handler("问") == "B"


def test_ask_user没有默认值():
	"""有默认值的话,忘了传的那个前端不会报错,只会拿到一个错的提问器 ——
	而"错"的样子是页面一直转圈,或者 ask 在那个前端静默地永远不能用。"""
	params = inspect.signature(build_tools).parameters
	assert params["ask_user"].default is inspect.Parameter.empty
	assert params["todo"].default is inspect.Parameter.empty


def test_子agent拿不到ask(monkeypatch):
	import tools.subagent as subagent
	from agent import TurnOutcome

	seen = {}

	def fake_loop(messages, **kwargs):
		seen.update(kwargs)
		return TurnOutcome("completed", "结论")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	assert subagent.run_task("去看看") == "结论"

	names = [t.name for t in seen["tools"]]
	assert "ask" not in names, names


def test_子agent的提问器签名是对的():
	"""它轮不到(ask 已经在 _DENIED 里),但签名必须对。

	随手把 _deny_all 传过去是能过的 —— 那个只收一个参数。哪天 deny 名单
	动了一下,炸出来的是 TypeError,而不是"没人答得上"。
	"""
	import tools.subagent as subagent
	assert subagent._nobody_to_ask("问", ["甲"]) is None
	assert list(inspect.signature(subagent._nobody_to_ask).parameters) == \
		["question", "options"]


# ---------- 六、服务端那条通道 ----------

def start_ask(make, emit_args, call_args):
	"""把提问器丢进一根线程跑,主线程等它把事件推出来。

	没有 HTTP —— 验的是提问器和 PENDING 之间那套交接,不是路由。

	提问器是两段式的:make(emit, ...) 造出来,再拿 (question, ...) 调。两段
	的参数个数两种提问器还不一样(权限确认那份多一个 record),所以分开给。
	"""
	events, arrived, box = [], threading.Event(), {}

	def emit(event):
		events.append(event)
		arrived.set()

	def run():
		box["value"] = make(emit, *emit_args)(*call_args)

	thread = threading.Thread(target=run, daemon=True)
	thread.start()
	assert arrived.wait(5), "提问器没把事件推出来"
	return events, box, thread


def answer_slot(server, events, box, thread, **fields):
	"""按 /answer 的方式回填那个槽,再等线跑完,返回提问器的返回值。"""
	slot = server.PENDING[events[0]["id"]]
	slot.update(fields)
	slot["event"].set()
	thread.join(5)
	assert not thread.is_alive(), "回了答,提问器还挂着"
	return box["value"]


@pytest.fixture
def soon(monkeypatch):
	"""超时缩到几秒 —— 这几项里它是兜底,不是要验的东西。"""
	import server
	monkeypatch.setattr(server, "ASK_TIMEOUT", 5.0)
	return server


def test_服务端_事件带mode和选项(soon):
	events, box, thread = start_ask(soon.make_ask_text, ("s1", "t1"),
	                                ("用哪种?", ["甲", "乙"]))
	assert events[0]["kind"] == "ask"
	assert events[0]["mode"] == "question", events[0]
	assert events[0]["options"] == ["甲", "乙"], events[0]
	assert events[0]["question"] == "用哪种?"
	answer_slot(soon, events, box, thread, answer="甲")


def test_服务端_回答写进槽里_拿回来的是那段文字(soon):
	events, box, thread = start_ask(soon.make_ask_text, ("s1", "t1"), ("问", []))
	slot = soon.PENDING[events[0]["id"]]
	assert slot["session"] == "s1" and slot["turn_id"] == "t1"
	assert answer_slot(soon, events, box, thread, answer="用 redis") == "用 redis"


def test_服务端_答完就把槽清了(soon):
	"""不清的话 PENDING 会一直涨,而且侧栏那个会话会一直显示"在等你回答"。"""
	events, box, thread = start_ask(soon.make_ask_text, ("s1", "t1"), ("问", []))
	rid = events[0]["id"]
	answer_slot(soon, events, box, thread, answer="甲")
	assert rid not in soon.PENDING, "槽没清掉"


def test_服务端_断流当场算没人答上(soon):
	"""写不出去 = 页面关了 = 没人能回答。必须**当场**返回,不是干等满
	ASK_TIMEOUT —— 那段时间里这个会话的锁还攥着,别的请求全是 409。

	测时间是必须的:不看时间的话,"等满超时再返回 None"和"当场返回 None"
	返回值一模一样,换个实现也照样过。
	"""
	calls = []

	def dead_emit(event):
		calls.append(event)
		raise BrokenPipeError("页面走了")

	started = time.monotonic()
	assert soon.make_ask_text(dead_emit, "s1", "t1")("问", []) is None
	elapsed = time.monotonic() - started

	assert calls[0]["mode"] == "question", calls[0]
	assert calls[0]["id"] not in soon.PENDING, "断流那条路径没清槽"
	assert elapsed < soon.ASK_TIMEOUT, f"等了 {elapsed:.1f}s,说明它在干等"


class _FakeHandler:
	"""让 _get_turns / _post_answer 能按非绑定方法调起来。

	不建 socket、不发请求 —— 验的是"挂起中的问题被还原成什么样"和"哪个字段
	能往哪个槽里写",不是路由。

	send_error 只记不抛:有的用例就是要看它报了哪个码,所以每个用例自己
	assert handler.error is None。
	"""

	def __init__(self, body=None, fail_write=False):
		self.json = None
		self.body = body
		self.error = None
		self.wrote = b""
		self.headers = {}
		self.fail_write = fail_write
		self.wfile = self          # _post_answer 成功时往 self.wfile 写

	def _json_body(self, expect):
		return self.body

	def _send_json(self, obj):
		self.json = obj

	def send_error(self, code, msg=None):
		self.error = (code, msg)

	def _cors(self):
		pass

	def send_response(self, code):
		pass

	def send_header(self, name, value):
		pass

	def end_headers(self):
		pass

	def write(self, data):
		if self.fail_write:
			raise BrokenPipeError("页面走了")
		self.wrote += data

	def flush(self):
		pass


def test_服务端_刷新后挂起的问题带着mode和选项(soon, monkeypatch, tmp_path):
	"""刷新之后页面拿 pending 重建那个框。

	少了 mode,一个提问会被画成是/否两个按钮,而按钮点下去发的是 allow,
	到了服务端被 mode 挡回来 —— 人看着页面上有个能点的东西,点了什么也
	不发生。少了 options,框是对的但没有按钮。
	"""
	import sessions
	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(soon, "STORE", store)
	sid = store.create_session("", "")["id"]
	soon.PENDING.clear()
	try:
		events, box, thread = start_ask(soon.make_ask_text, (sid, "t1"),
		                                ("用哪种?", ["甲", "乙"]))
		handler = _FakeHandler()
		soon.Handler._get_turns(handler, sid)
		assert handler.json["pending"] == [
			{"kind": "ask", "id": events[0]["id"], "mode": "question",
			 "question": "用哪种?", "options": ["甲", "乙"], "turn_id": "t1"}
		], handler.json["pending"]
		assert handler.json["running"] is False
		assert handler.error is None, handler.error
		answer_slot(soon, events, box, thread, answer="甲")
	finally:
		soon.PENDING.clear()


def test_服务端_整轮把提问器绑在这一轮这条流上(soon, monkeypatch, tmp_path):
	"""_run_turn 里那行接线,是上面所有服务端用例都够不着的一行。

	传错成旁边那个 silent 是最像的错法 —— 类型对、能跑、测试全过,只是
	页面关掉之后 _ask_and_wait 会以为**还有人能回答**,然后干等满 300 秒。
	"""
	import sessions
	from agent import TurnOutcome

	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(soon, "STORE", store)
	sid = store.create_session("", "")["id"]

	spy = {}
	real = soon.make_ask_text

	def spy_make(emit, s, t):
		spy.update(emit=emit, sid=s, turn_id=t)
		return real(emit, s, t)

	monkeypatch.setattr(soon, "make_ask_text", spy_make)

	seen = {}

	def fake_loop(messages, **kwargs):
		seen.update(kwargs)
		return TurnOutcome("completed", "结论")

	monkeypatch.setattr(soon, "agent_loop", fake_loop)

	handler = _FakeHandler()
	soon.Handler._run_turn(handler, sid, "问题")

	turn = store.list_turns(sid)["turns"][-1]
	assert spy["sid"] == sid, spy["sid"]
	assert spy["turn_id"] == turn["id"], (spy["turn_id"], turn["id"])
	assert "ask" in [t.name for t in seen["tools"]], seen["tools"]
	assert b'"kind": "reply"' in handler.wrote, "这一轮的结果没进响应流"

	# emit 必须是**不安静**的那份。安静版把写失败吞掉,而 _ask_and_wait 正是
	# 靠那个失败判断"还有人能回答吗"。
	handler.fail_write = True
	try:
		spy["emit"]({"kind": "ask", "id": "x", "mode": "question", "question": "问"})
	except OSError:
		pass
	else:
		raise AssertionError("拿到的是安静版 emit —— 页面一关它会干等满 ASK_TIMEOUT")


def test_服务端_allow往提问的槽里塞不动它(soon):
	"""请求体说了不算,槽自己的 mode 说了算。

	不按 mode 分派的话,一个网页(或者一个写错的脚本)拿 allow 就能把模型的
	提问答成"是" —— 而一问一答那个通道里根本没有"是"这个答案,模型收到的
	会是一段空白,它没有任何办法发现自己被答非所问了。
	"""
	events, box, thread = start_ask(soon.make_ask_text, ("s1", "t1"), ("问", []))
	rid = events[0]["id"]

	wrong = _FakeHandler({"id": rid, "allow": True})
	soon.Handler._post_answer(wrong)
	assert wrong.error is not None, "拿 allow 把模型的提问答掉了"
	assert soon.PENDING[rid].get("answer") is None
	assert not soon.PENDING[rid]["event"].is_set(), \
		"槽被 set 了 —— 提问器会带着一个空答案醒过来,而模型会以为人答了空话"

	# 换成 text 才走得通。不再插一句"这时提问器还没醒" —— 槽刚 set 完,
	# 那根线随时会跑完,那种断言是竞态的。
	right = _FakeHandler({"id": rid, "text": "用 redis"})
	soon.Handler._post_answer(right)
	assert right.error is None, right.error
	thread.join(5)
	assert box.get("value") == "用 redis", box


def test_服务端_text往确认的槽里塞_按拒绝处理(soon):
	"""反过来的方向是安全的,而且必须保持安全:确认的槽只读 allow,没给就是
	False,也就是拒绝。放宽成"有 text 也算过"就是把权限那道门拆了。"""
	events, box, thread = start_ask(
		soon.make_ask, ("s1", "t1", lambda *a: None), ("问",))

	handler = _FakeHandler({"id": events[0]["id"], "text": "好的"})
	soon.Handler._post_answer(handler)
	assert handler.error is None, handler.error
	thread.join(5)
	assert box["value"] is False, box


def test_服务端_权限确认还走原来的形状(soon):
	"""两种问题共用 _ask_and_wait,但 confirm 那一侧的契约不许跟着变:
	事件里没有 options,槽里写的是 allow,返回的是 bool。"""
	def record(kind, role, content):
		pass

	events, box, thread = start_ask(soon.make_ask, ("s1", "t1", record), ("问",))
	assert events[0]["mode"] == "confirm", events[0]
	assert "options" not in events[0], events[0]
	assert answer_slot(soon, events, box, thread, allow=True) is True
