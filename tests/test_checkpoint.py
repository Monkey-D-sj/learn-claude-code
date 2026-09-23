"""Checkpoint:回合快照、两阶段标记、严格写入、续跑与放弃。

这一版新增的能力全在这几条里,而且每条都对着一个**具体的错法**:

  一、快照落在**完整回合**边界上,水位同时推进 —— 只存正文不存水位,
     那份快照说不清自己涵盖到哪儿,恢复时只能整份不信
  二、快照是**当场序列化**的:调用方接着改内存里那份列表,库里那份不该
     跟着变(它是"那一刻"的证据)
  三、有副作用的工具在动手**之前**落一条标记,结果落库时同一个事务收口。
     崩在中间时,"开始过、结果未知"必须能和"压根没开始"分开
  四、**开始过、没有结果**的操作挡住自动续跑 —— 那正是两阶段标记存在的
     理由:没有这一条,一条跑了一半的 bash 会看起来完全没发生过
  五、关键记录写不进去时**停下来标中断**,而**不把内存里那份历史存下去**
     (它缺了一块,存了就是拿残史盖掉最后一个可信点)
  六、续跑:号接着发、计数接着数、待办装回来、版本对得上、尾部挡住
  七、放弃:不回滚,但把"哪些操作结果不确定"写进上下文,之后能开新轮

跑法: uv run pytest
"""

import json
import sqlite3

import pytest

import server
import sessions
import usage
from agent import TurnOutcome


def raw(db, sql, args=()):
	conn = sqlite3.connect(db)
	try:
		return conn.execute(sql, args).fetchall()
	finally:
		conn.close()


class _Handler:
	"""把 handler 的几个方法按非绑定方式调起来:不建 socket、不走路由。

	跟 test_save_failure.py 的那个是同一套路,多接了恢复和放弃两个入口。
	"""

	_post_ask = server.Handler._post_ask
	_run_turn = server.Handler._run_turn
	_drive = server.Handler._drive
	_checkpoint = server.Handler._checkpoint
	_post_resume = server.Handler._post_resume
	_post_abandon = server.Handler._post_abandon
	_review_info = server.Handler._review_info
	_get_review = server.Handler._get_review
	_flush_unsaved = server.Handler._flush_unsaved

	def __init__(self, body=None):
		self.body = body
		self.json = None
		self.status_code = None
		self.error = None
		self.wrote = b""
		self.headers = {}
		self.wfile = self

	def _json_body(self, expect):
		return self.body

	def _send_json(self, obj, status=200):
		self.json, self.status_code = obj, status

	def send_error(self, code, msg=None):
		self.error = (code, msg)

	def _cors(self):
		pass

	def send_response(self, code):
		self.status_code = code

	def send_header(self, name, value):
		pass

	def end_headers(self):
		pass

	def write(self, data):
		self.wrote += data

	def flush(self):
		pass

	def events(self) -> list[dict]:
		return [json.loads(line) for line in self.wrote.decode("utf-8").splitlines()
		        if line.strip()]

	def reply(self) -> dict:
		return [e for e in self.events() if e["kind"] == "reply"][-1]


@pytest.fixture(autouse=True)
def _clean_unsaved():
	server.UNSAVED.clear()
	yield
	server.UNSAVED.clear()


@pytest.fixture
def env(monkeypatch, tmp_path):
	"""临时库 + 一个会话,装进 server.STORE。返回 (db_path, store, sid)。"""
	db = tmp_path / "sessions.db"
	store = sessions.SessionStore(db)
	monkeypatch.setattr(server, "STORE", store)
	return db, store, store.create_session("项目记忆", "用户记忆")["id"]


def _finish(store, turn, messages, text="做完了"):
	"""让这一轮正常收尾:终态和上下文同一事务(跟 _drive 里那一步一致)。"""
	record = server.make_recorder(turn["id"])
	record("assistant_response", "assistant", [{"type": "text", "text": text}])
	return record


def _scripted(**hooks):
	"""造一个替身循环:按 hooks 说的写记录、落标记、存快照,然后返回。

	hooks 里的每一步都是**真的调**那一层的接口(record / checkpoint /
	begin_exec),所以验的是接线,不是替身自己的行为。
	"""
	seen = {}

	def loop(messages, **kw):
		seen.update(kw)
		seen["rounds"] = seen.get("rounds", 0) + 1
		step = hooks.get("each") or hooks.get("only")
		if step:
			step(messages, kw, seen)
		return hooks.get("outcome") or TurnOutcome("completed", "做完了")

	return loop, seen


def _one_round(messages, kw, seen):
	"""一个最普通的回合:assistant 回复 + 三条工具结果(两条有副作用、一条只读),
	然后存快照。水位应该是 5(用户那条 1,加四条记录)。"""
	kw["record"]("assistant_response", "assistant",
	             [{"type": "text", "text": "先看一下"}])
	for uid, name in (("tu1", "bash"), ("tu2", "write_file")):
		kw["begin_exec"](uid, name, {"x": uid})
		kw["record"]("tool_result", "user",
		             [{"type": "tool_result", "tool_use_id": uid,
		               "content": "ok"}], tool_use_id=uid)
	kw["record"]("tool_result", "user",
	             [{"type": "tool_result", "tool_use_id": "tu3", "content": "文件内容"}],
	             tool_use_id="tu3")
	messages.append({"role": "assistant",
	                 "content": [{"type": "text", "text": "先看一下"}]})
	messages.append({"role": "user", "content": [
		{"type": "tool_result", "tool_use_id": uid, "content": "ok"}
		for uid in ("tu1", "tu2", "tu3")]})
	kw["checkpoint"](messages, {"rounds": seen["rounds"],
	                            "rounds_since_todo": 1}, False)


# ---------------------------------------------------------------- 快照与水位

def test_每个完整回合存一份_水位和运行状态一起(env, monkeypatch):
	db, store, sid = env

	def step(messages, kw, seen):
		_one_round(messages, kw, seen)
		# 存完立刻读回来 —— 轮末那次收尾会把这份覆盖掉,而这里要看的是
		# **回合边界**上那一份长什么样
		seen["snapshot"] = raw(db, "SELECT covered_message_no, version,"
		                           " runtime_json, last_compacted_at"
		                           " FROM session_contexts")[0]

	loop, seen = _scripted(each=step)
	monkeypatch.setattr(server, "agent_loop", loop)

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	assert handler.error is None, handler.error

	covered, version, runtime_json, compacted_at = seen["snapshot"]
	# 水位 = 已经落库的最大 message_no:用户那条 1、assistant 那条 2、
	# 三条工具结果 3/4/5
	assert covered == 5, seen["snapshot"]
	assert raw(db, "SELECT COALESCE(MAX(message_no), 0) FROM turn_messages") == [(5,)]
	assert raw(db, "SELECT checkpoint_turn_id FROM session_contexts")[0][0] == \
		store.list_turns(sid)["turns"][0]["id"]

	runtime = json.loads(runtime_json)
	assert runtime["rounds"] == 1, runtime
	assert runtime["active_request"] == "干活", runtime
	assert runtime["max_rounds"] == server.MAX_ROUNDS, runtime
	# 签名和 model 是恢复判定的依据,必须在快照里
	assert runtime["signature"] and runtime["model"] == server.MODEL, runtime
	assert runtime["todos"] == [], runtime

	# 普通回合的保存**不该**碰"压过"的时间:那是压缩档的事
	assert compacted_at is None, seen["snapshot"]
	# 而轮末那次(权威的最终历史)复用水位、留住运行状态,但版本必须往前走 ——
	# 版本是"这份快照换过几版",不是消息数
	final = raw(db, "SELECT covered_message_no, version, runtime_json"
	                " FROM session_contexts")[0]
	assert final[0] == 5 and final[1] > version, (final, seen["snapshot"])
	assert json.loads(final[2])["rounds"] == 1, final


def test_压缩那次保存_会写上压过的时间(env, monkeypatch):
	db, store, sid = env
	loop, seen = _scripted(
		only=lambda messages, kw, seen: kw["checkpoint"](
			messages, {"rounds": 1, "rounds_since_todo": 0}, **{"True": True})
		if False else kw["checkpoint"](messages, {"rounds": 1}, True))
	monkeypatch.setattr(server, "agent_loop", loop)

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	# 轮末那次不带 compacted,而 COALESCE 要把它留住
	assert raw(db, "SELECT last_compacted_at IS NOT NULL"
	               " FROM session_contexts") == [(1,)]


def test_快照当场序列化_之后改内存那份不影响它(env, monkeypatch):
	"""C08。存的是"那一刻"的正文;调用方接着往列表上追加,库里那份不该跟着变。

	不然的话,一份快照会在它自己不知情的情况下长出后面几个回合的内容 ——
	而水位还停在存的那一刻,于是"正文里有、水位说没有",恢复判定读谁都错。
	"""
	db, store, sid = env
	box = {}

	def step(messages, kw, seen):
		kw["checkpoint"](messages, {"rounds": 1}, False)
		box["stored"] = store.load_context(sid)
		messages.append({"role": "assistant",
		                 "content": [{"type": "text", "text": "后来才有的"}]})

	loop, seen = _scripted(each=step)
	monkeypatch.setattr(server, "agent_loop", loop)
	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()

	assert box["stored"] == [{"role": "user", "content": "干活"}], box["stored"]


# ---------------------------------------------------------------- 两阶段标记

def test_有副作用的工具_动手之前先落标记_结果同事务收口(env, monkeypatch):
	db, store, sid = env
	loop, seen = _scripted(each=_one_round)
	monkeypatch.setattr(server, "agent_loop", loop)
	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()

	rows = raw(db, "SELECT tool_use_id, name, finished_at IS NOT NULL,"
	               " message_id FROM tool_execs ORDER BY tool_use_id")
	assert [r[0] for r in rows] == ["tu1", "tu2"], rows
	assert all(r[2] for r in rows), "结果落库了却没把标记收口"
	# 只读的那条(tu3)不该留下标记:重发一次无害,不必多一条写
	assert len(rows) == 2, rows
	# message_id 指向的正是那条结果 —— 查得到,不是"记了个号"
	mid = rows[0][3]
	assert raw(db, "SELECT kind FROM turn_messages WHERE id = ?", (mid,)) \
		== [("tool_result",)]


def test_开始过没有结果的操作_挡住自动续跑(env, monkeypatch):
	"""这一条是两阶段标记存在的全部理由。

	handler 跑了一半、进程没了:库里只有那条 started。而它**没有**对应的
	原始消息,所以"尾部记录"那条判据看都看不到它 —— 少一条检查,恢复就会
	把这条已经动过机器的 bash 再跑一遍。
	"""
	db, store, sid = env
	turn = store.begin_turn(sid, "干活")
	store.save_checkpoint(sid, turn["id"], 1, [{"role": "user", "content": "干活"}],
	                      {"rounds": 1, "signature": "sig"})
	store.begin_tool_exec(turn["id"], "tu9", "bash", {"command": "rm -rf build"})
	store.reap_running()

	info = store.checkpoint_info(sid, turn["id"], 100, "sig")
	assert info["resumable"] is False, info
	assert info["reason"] == "unknown_tool_result", info
	assert info["unknown_tools"][0]["name"] == "bash", info["unknown_tools"]
	assert info["unknown_tools"][0]["input"] == {"command": "rm -rf build"}


def test_标记写不进去就不执行(env):
	"""严格写:标记落不了库 = 不知道它做没做,所以宁可不做。"""
	store, sid = env[1], env[2]
	turn = store.begin_turn(sid, "干活")
	store._conn.execute("DROP TABLE tool_execs")
	with pytest.raises(sessions.PersistError):
		store.begin_tool_exec(turn["id"], "tu1", "bash", {"command": "ls"})


# ---------------------------------------------------------------- 严格写入

def test_关键记录写不进去_停下来标中断_不覆盖快照(env, monkeypatch):
	db, store, sid = env
	def step(messages, kw, seen):
		kw["checkpoint"](messages, {"rounds": 1}, False)      # 先有一份可信快照
		kw["record"]("assistant_response", "assistant",
		             [{"type": "text", "text": "写不进去的一句"}])

	loop, seen = _scripted(each=step)
	monkeypatch.setattr(server, "agent_loop", loop)
	monkeypatch.setattr(store, "append_turn_message",
	                    lambda *a, **kw: (_ for _ in ()).throw(
		                    sessions.PersistError("库写不进去了")))

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()

	turn = store.list_turns(sid)["turns"][-1]
	assert turn["status"] == "interrupted", turn
	assert turn["interrupt_reason"] == "persist_failed", turn
	# **上下文一个字没动**:还是那个回合的快照,不是内存里那份缺一块的历史
	assert store.load_context(sid) == [{"role": "user", "content": "干活"}]
	# 页面上要说清是中断,而且不能说成普通完成
	reply = handler.reply()
	assert reply["status"] == "interrupted" and reply["interrupted"] is True, reply
	# 不进补写闸:那份内存里的历史不许被"补"进库
	assert sid not in server.UNSAVED, server.UNSAVED


def test_回合快照写不进去_也停下来(env, monkeypatch):
	db, store, sid = env
	loop, seen = _scripted(each=lambda m, kw, s: kw["checkpoint"](m, {"rounds": 1}, False))
	monkeypatch.setattr(server, "agent_loop", loop)
	monkeypatch.setattr(store, "save_checkpoint",
	                    lambda *a, **kw: (_ for _ in ()).throw(
		                    RuntimeError("disk I/O error")))

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	turn = store.list_turns(sid)["turns"][-1]
	assert turn["status"] == "interrupted", turn
	assert turn["interrupt_reason"] == "persist_failed", turn


# ---------------------------------------------------------------- 恢复

def _version(store) -> int:
    """库里那份快照现在是第几版。页面就是从这儿拿的,用例也别写死。"""
    return store._conn.execute(
        "SELECT version FROM session_contexts").fetchone()[0]


def _dead_turn(store, sid, runtime=None, messages=None, covered=1, reserve=0):
	"""造一个"跑到一半被杀"的轮次:有快照、没有尾部记录。

	reserve 是"被杀之前已经预留了几次模型调用"—— 额度那一条用例要它,
	而且它必须在 reap 之前做(预留要求这一轮还是 running)。
	"""
	turn = store.begin_turn(sid, "干活")
	store.save_checkpoint(
		sid, turn["id"], covered,
		messages if messages is not None else [{"role": "user", "content": "干活"}],
		runtime or {"rounds": 1, "rounds_since_todo": 0, "signature": "sig",
		            "active_request": "干活", "max_rounds": 100,
		            "todos": [{"content": "第一步", "status": "in_progress"}]})
	for _ in range(reserve):
		assert store.reserve_round(turn["id"], 100) is True
	store.reap_running()
	return turn


def test_续跑_号接着发_计数接着数_待办装回来(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)

	# 签名要跟快照里那份对上,否则恢复判定会拒绝(那是另一条用例)
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")
	loop, seen = _scripted()
	monkeypatch.setattr(server, "agent_loop", loop)

	handler = _Handler({"version": _version(store)})
	handler._post_resume(sid, dead["id"])
	assert handler.error is None, handler.error

	turn = store.list_turns(sid)["turns"][-1]
	assert turn["status"] == "completed", turn
	# 号接着发,不从 2 重来:用户 1、恢复说明 2(替身循环不写自己的回复)
	nos = [m["message_no"] for m in turn["messages"]]
	assert nos == [1, 2], nos
	# 恢复说明进了上下文,而且模型看得见它是同一轮的一部分
	texts = [b["text"] for b in turn["messages"][1]["content"]]
	assert any("继续" in t for t in texts), texts
	# 计数从快照里接着数(不是从 0),额度也是接着用的
	assert seen["rounds_start"] == 1, seen.get("rounds_start")
	# 待办装回来了 —— 不装的话模型一睁眼看到的是"No todos"
	from server import todo_for
	assert [i["content"] for i in todo_for(sid).items] == ["第一步"]
	# 而回复事件照常发出去
	assert handler.reply()["status"] == "completed", handler.reply()


def test_续跑_不新增用户消息_也不重跑已完成的工具(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	before = raw(db, "SELECT COUNT(*) FROM turn_messages")[0][0]
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")
	loop, seen = _scripted()
	monkeypatch.setattr(server, "agent_loop", loop)

	handler = _Handler({"version": _version(store)})
	handler._post_resume(sid, dead["id"])
	assert handler.error is None, handler.error
	# 只多了一条(那条恢复说明),没有把"继续"当成新的用户问题追加进去
	assert raw(db, "SELECT COUNT(*) FROM turn_messages")[0][0] == before + 1, \
		raw(db, "SELECT message_no, kind FROM turn_messages")
	# 历史里没有第二条 user_input
	kinds = [r[0] for r in raw(db, "SELECT kind FROM turn_messages ORDER BY message_no")]
	assert kinds == ["user_input", "control"], kinds


def test_尾部有记录就不给续跑(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	# 水位之后又落了一条(比如 assistant 写进去了、结果还没回填)
	store.append_turn_message(dead["id"], 2, "assistant_response", "assistant",
	                          [{"type": "text", "text": "写了一半"}])
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")

	info = store.checkpoint_info(sid, dead["id"], 100, "sig")
	assert info["resumable"] is False and info["reason"] == "unresolved_tail", info

	handler = _Handler({"version": _version(store)})
	handler._post_resume(sid, dead["id"])
	assert handler.status_code == 409, handler.status_code
	assert handler.json["reason"] == "unresolved_tail", handler.json
	# 一次模型调用都没发生:状态没动,还是中断
	assert store.list_turns(sid)["turns"][-1]["status"] == "interrupted"


def test_版本或者签名对不上就拒绝(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")

	# 页面拿的是旧版本号
	handler = _Handler({"version": 99})
	handler._post_resume(sid, dead["id"])
	assert handler.status_code == 409 and handler.json["reason"] == "conflict", handler.json
	assert store.list_turns(sid)["turns"][-1]["status"] == "interrupted"

	# 代码/模型/提示词换过了
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "别的")
	info = store.checkpoint_info(sid, dead["id"], 100, "别的")
	assert info["reason"] == "incompatible", info


def test_后面又开过新轮的不给续跑(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	store.begin_turn(sid, "新问题")
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")
	assert store.checkpoint_info(sid, dead["id"], 100, "sig")["reason"] \
		== "has_later_turn"


def test_额度用完了不给续跑(env, monkeypatch):
	"""额度看的是**库里那个只增不减的数**(每次请求前预留),不是快照里的。

	快照是回合边界上存的,而崩溃完全可能落在"请求发出去了、快照还没存"
	之间 —— 只看快照的话,那一次调用像没发生过,恢复等于白送一笔。
	"""
	db, store, sid = env
	# 预留必须在 running 的时候做(它就是"发请求前那一步"),所以顺序是:
	# 开轮 → 存快照 → 预留 → 被杀
	dead = _dead_turn(store, sid, reserve=1)
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")
	assert store.checkpoint_info(sid, dead["id"], 1, "sig")["reason"] \
		== "rounds_exhausted"


def test_未完成的中断任务挡住新提问(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	loop, seen = _scripted()
	monkeypatch.setattr(server, "agent_loop", loop)

	handler = _Handler({"session": sid, "query": "换个问题"})
	handler._post_ask()
	assert handler.error and handler.error[0] == 409, handler.error
	assert "resume or abandon" in handler.error[1], handler.error
	# **状态行只能装 ASCII**(latin-1),这条消息里出现一个中文,浏览器看到的
	# 就是"连接被断开、没有任何响应" —— 而它本该是一句 409
	assert handler.error[1].isascii(), handler.error
	# 新轮没开起来:还是那一轮
	assert len(store.list_turns(sid)["turns"]) == 1


# ---------------------------------------------------------------- 放弃

def test_放弃_不回滚但把不确定写进上下文(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	# 一个已经开始、没有结果的操作
	store.begin_tool_exec(dead["id"], "tu9", "bash", {"command": "npm publish"})
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")

	handler = _Handler({"version": _version(store)})
	handler._post_abandon(sid, dead["id"])
	assert handler.error is None, handler.error
	assert handler.json["ok"] is True, handler.json

	turn = store.list_turns(sid)["turns"][-1]
	assert turn["status"] == "failed", turn
	note = turn["messages"][-1]
	assert note["kind"] == "control", note
	text = "".join(b["text"] for b in note["content"])
	# 证据必须来自库里那一行,而不是"可能有一些操作"
	assert "npm publish" in text and "没有被回滚" in text, text
	# 而且它进了**有效上下文** —— 下一个轮次从这儿接着跑
	assert all(isinstance(m, dict) for m in store.load_context(sid)), store.load_context(sid)

	# 放弃之后新提问能开起来(闸解除了)
	monkeypatch.setattr(server, "agent_loop", _scripted()[0])
	again = _Handler({"session": sid, "query": "那换个活"})
	again._post_ask()
	assert again.error is None, again.error
	assert len(store.list_turns(sid)["turns"]) == 2


def test_放弃也要版本对得上(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")
	handler = _Handler({"version": 5})
	handler._post_abandon(sid, dead["id"])
	assert handler.status_code == 409 and handler.json["reason"] == "conflict", handler.json
	assert store.list_turns(sid)["turns"][-1]["status"] == "interrupted"


def test_查看详情是只读的(env, monkeypatch):
	db, store, sid = env
	dead = _dead_turn(store, sid)
	before = raw(db, "SELECT messages_json, version FROM session_contexts")[0], \
		raw(db, "SELECT status FROM turns")[0][0]
	monkeypatch.setattr(server, "recovery_signature", lambda system, tools: "sig")

	handler = _Handler()
	handler._get_review(sid, dead["id"])
	assert handler.error is None, handler.error
	assert handler.json["info"]["resumable"] is True, handler.json
	# 看一遍什么都没改
	assert (raw(db, "SELECT messages_json, version FROM session_contexts")[0],
	        raw(db, "SELECT status FROM turns")[0][0]) == before


# ---------------------------------------------------------------- 路由

def test_三个新入口在路由上都挂上了(monkeypatch):
	"""do_GET / do_POST 的分派。

	不测这一段的话,段数或者动词写错一个字符,表现是页面上一句"后端返回
	404" —— 而三个入口里有两个是用户卡住时唯一的出路。
	"""
	from types import SimpleNamespace

	calls = []
	fake = SimpleNamespace(
		path="",
		headers={},
		send_error=lambda code, msg=None: calls.append(("error", code, msg)),
		_get_review=lambda sid, tid: calls.append(("review", sid, tid)),
		_post_resume=lambda sid, tid: calls.append(("resume", sid, tid)),
		_post_abandon=lambda sid, tid: calls.append(("abandon", sid, tid)),
		_post_ask=lambda: calls.append(("ask",)),
		_post_answer=lambda: calls.append(("answer",)),
		_post_session=lambda: calls.append(("session",)),
		_post_delete=lambda sid: calls.append(("delete", sid)),
		_get_turns=lambda sid: calls.append(("turns", sid)),
		_get_sessions=lambda: calls.append(("sessions",)),
		_get_events=lambda sid, q: calls.append(("events", sid)),
		_page=lambda: calls.append(("page",)),
		_pet_image=lambda: calls.append(("pet",)),
	)

	fake.path = "/session/s1/turn/t1/review"
	server.Handler.do_GET(fake)
	assert calls == [("review", "s1", "t1")], calls

	calls.clear()
	fake.path = "/session/s1/turn/t1/resume"
	server.Handler.do_POST(fake)
	assert calls == [("resume", "s1", "t1")], calls

	calls.clear()
	fake.path = "/session/s1/turn/t1/abandon"
	server.Handler.do_POST(fake)
	assert calls == [("abandon", "s1", "t1")], calls

	# 少一段、多一段、动词写错,一律 404 —— 不猜
	for path in ("/session/s1/turn/t1", "/session/s1/turn/t1/review/x",
	             "/session/s1/turn/resume"):
		calls.clear()
		fake.path = path
		server.Handler.do_GET(fake)
		assert calls and calls[0][0] == "error" and calls[0][1] == 404, (path, calls)


# ---------------------------------------------------------------- 终端那一行

def _record(**over):
	"""一条账本记录,形状跟 usage.meter 写的对齐(这里只挑这一行要用的)。"""
	base = {name: 0 for name in usage.COUNTERS}
	base.update({"input_tokens": 10, "cache_read_input_tokens": 990,
	             "cache_creation_input_tokens": 0, "output_tokens": 131,
	             "total_input_tokens": 1000,
	             "agent": "main", "purpose": "main", "elapsed_ms": 712_800,
	             "cost": 1.1263, "cost_currency": "CNY"})
	base.update(over)
	return base


def test_每轮跑完_终端也打那一行(env, monkeypatch, capsys):
	"""这一行以前只在页面上有。跑服务的人想知道"这一轮花了多少",得切回
	浏览器、再找到那一轮 —— 而它本来就该跟这一轮的结果一起出现在终端上。

	**同一行,不是另写一遍。** 页面和终端拿的是同一个 usage.turn_line 的输出:
	钱、命中率、币种那几条规矩写两遍就会分家,而分家之后两个数长得都挺像。
	"""
	db, store, sid = env
	monkeypatch.setattr(server, "agent_loop", _scripted()[0])
	canned = [_record()]
	monkeypatch.setattr(server.usage, "read_turn", lambda s, t: canned)

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	assert handler.error is None, handler.error

	out = capsys.readouterr().out
	line = [l for l in out.splitlines() if l.startswith("[第 1 轮]")]
	assert len(line) == 1, out
	# 这一行里的每个数都在:调用次数、输入(命中 / 率)、上下文、输出、秒、钱
	assert "1 次调用" in line[0], line[0]
	assert "输入 1,000(命中 990 / 99.0%)" in line[0], line[0]
	assert "上下文 1,000" in line[0], line[0]
	assert "输出 131" in line[0], line[0]
	assert "712.8s" in line[0], line[0]
	assert "¥1.1263" in line[0], line[0]
	# 正常跑完不加状态词:页面上有轮次框,终端上没有
	assert line[0].endswith("¥1.1263"), line[0]


def test_中断那一轮_终端那行带个状态词(env, monkeypatch, capsys):
	"""终端上只有一行数字,看不出这一轮是跑完了还是被打断了。"""
	db, store, sid = env
	def step(messages, kw, seen):
		kw["record"]("assistant_response", "assistant",
		             [{"type": "text", "text": "写不进去的一句"}])

	monkeypatch.setattr(server, "agent_loop", _scripted(each=step)[0])
	monkeypatch.setattr(server.usage, "read_turn", lambda s, t: [_record()])
	# 让那条记录写不进去 —— 这一轮会停在半路
	monkeypatch.setattr(store, "append_turn_message",
	                    lambda *a, **kw: (_ for _ in ()).throw(
		                    sessions.PersistError("库写不进去了")))

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	out = capsys.readouterr().out
	line = [l for l in out.splitlines() if l.startswith("[第 1 轮]")][0]
	assert line.endswith("· 中断"), line


def test_没有账的时候_终端一行都不打(env, monkeypatch, capsys):
	"""账本里没有这一轮的记录时,那一行是 None —— **不许编一个 ¥0 出来**。
	0 的意思是"这一轮确定没花钱",而真相是"不知道"。"""
	db, store, sid = env
	monkeypatch.setattr(server, "agent_loop", _scripted()[0])
	monkeypatch.setattr(server.usage, "read_turn", lambda s, t: [])

	handler = _Handler({"session": sid, "query": "干活"})
	handler._post_ask()
	assert "[第 1 轮]" not in capsys.readouterr().out
