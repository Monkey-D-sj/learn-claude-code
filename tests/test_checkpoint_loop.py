"""真循环 + 真库:把 checkpoint 那条接线整条走一遍。

上面那一批用的是替身循环,验的是服务端这一侧的接线。这里换真的
`agent_loop`:只有它知道什么时候该落标记、什么时候该存快照 —— 而这几处
位置错了都不报错,只是恢复能力悄悄没了。

守四件事:

  一、**有副作用的工具动手之前落标记**(bash / write_file),只读的不落
     (read_file)—— 漏了前者,崩在中间的操作会看起来从没发生过;多了
     后者,每一条 grep 都要多写一行、还要进人工核对
  二、**每个完整回合存一次快照**,水位跟着已落库的记录走
  三、**严格写入**:关键记录写不进去时循环停下,而不是接着往下跑
  四、**记录先于事件**:库里那条消息一定先于页面上那条事件落下 —— 页面
     拿事件游标当分界,反过来的话刷新会凭空少一条

跑法: uv run pytest
"""

import json
from types import SimpleNamespace

import pytest

import agent
import server
import sessions
from agent import TurnOutcome


def _text(t: str):
	return SimpleNamespace(type="text", text=t)


def _tool_use(name: str, uid: str, **args):
	return SimpleNamespace(type="tool_use", id=uid, name=name, input=args)


def _response(content, stop_reason="end_turn"):
	return SimpleNamespace(content=content, stop_reason=stop_reason)


class _Pass:
	"""压缩器占位:这里验的是保存边界,不是压缩。"""

	def prepare(self, messages, active_request, checkpoint):
		return messages


def _tool(name: str, side_effect: bool, calls: list):
	"""一个假工具。side_effect 就是 ToolDesc 上那个字段。"""
	return SimpleNamespace(
		name=name,
		side_effect=side_effect,
		to_wire=lambda: {"name": name, "description": "d",
		                 "input_schema": {"type": "object", "properties": {}}},
		handler=lambda **kw: calls.append((name, kw)) or "结果正文")


@pytest.fixture
def wiring(monkeypatch, tmp_path):
	"""真库 + 真的 record / begin_exec / checkpoint 三个回调,拼成一组。"""
	db = tmp_path / "sessions.db"
	store = sessions.SessionStore(db)
	monkeypatch.setattr(server, "STORE", store)
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "干活")
	record = server.make_recorder(turn["id"])
	events = []
	emitted = []

	def emit(event):
		# 库里那条(record)和页面上这条(emit)的先后,靠 emitted 记顺序
		emitted.append(event)
		events.append(event)

	def checkpoint(messages, loop_state, compacted):
		server.Handler._checkpoint(
			SimpleNamespace(), sid, turn, record, messages,
			{**loop_state, "active_request": "干活", "signature": "sig"}, compacted)

	return {"db": db, "store": store, "sid": sid, "turn": turn, "record": record,
	        "emit": emit, "events": emitted, "checkpoint": checkpoint}


def _run(wiring, monkeypatch, responses, tools, *, max_rounds=5):
	"""跑一轮真循环,模型按 responses 依次回话。"""
	box = list(responses)

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		return box.pop(0)

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	history = [{"role": "user", "content": "干活"}]
	outcome = agent.agent_loop(
		history, active_request="干活", system="s", tools=tools, model="m",
		max_rounds=max_rounds, compactor=_Pass(), emit=wiring["emit"],
		ask=lambda question: False, stream=False,
		record=wiring["record"],
		checkpoint=wiring["checkpoint"],
		begin_exec=lambda uid, name, data: wiring["store"].begin_tool_exec(
			wiring["turn"]["id"], uid, name, data))
	return history, outcome


def _rows(wiring, sql, args=()):
	return wiring["store"]._conn.execute(sql, args).fetchall()


def test_真循环_只给有副作用的工具落标记(wiring, monkeypatch):
	calls = []
	tools = [_tool("bash", True, calls), _tool("read_file", False, calls)]
	responses = [
		_response([_tool_use("bash", "t1", command="ls"),
		           _tool_use("read_file", "t2", path="a.txt")], "tool_use"),
		_response([_text("干完了")]),
	]
	history, outcome = _run(wiring, monkeypatch, responses, tools)
	assert outcome.status == "completed", outcome

	rows = _rows(wiring, "SELECT tool_use_id, name, finished_at IS NOT NULL,"
	                     " message_id FROM tool_execs ORDER BY tool_use_id")
	assert [r[0] for r in rows] == ["t1"], rows
	assert rows[0][1] == "bash" and bool(rows[0][2]) is True, rows
	# 收口指到的那条结果确实在库里
	assert _rows(wiring, "SELECT kind FROM turn_messages WHERE id = ?",
	             (rows[0][3],)) == [("tool_result",)]
	# 两条工具都真跑了(标记不改变执行,只是留证据)
	assert len(calls) == 2, calls


def test_真循环_每个完整回合存一次快照(wiring, monkeypatch):
	calls = []
	tools = [_tool("bash", True, calls)]
	responses = [
		_response([_tool_use("bash", "t1", command="ls")], "tool_use"),
		_response([_tool_use("bash", "t2", command="pwd")], "tool_use"),
		_response([_text("干完了")]),
	]
	versions = []

	def watch(messages, loop_state, compacted):
		wiring["checkpoint"](messages, loop_state, compacted)
		versions.append((loop_state["rounds"],
		                 _rows(wiring, "SELECT covered_message_no, version"
		                               " FROM session_contexts")[0]))

	_run_with_watch(wiring, monkeypatch, responses, tools, watch)

	# 每个完整回合的边界上都存一次,包括最后那个只回了句话的回合 —— 它的
	# 存的是"上一次工具之后"的状态,不是为了那句回复。版本一个一个往上走。
	assert [v[0] for v in versions] == [1, 2, 3], versions
	assert [v[1][1] for v in versions] == sorted(v[1][1] for v in versions), versions
	# 水位是"存的那一刻已经落库到第几条":快照存在**回合的顶端**(发请求之前),
	# 所以第 1 回合存的时候库里只有用户那条(1),第 2 回合已经有上一回合的
	# assistant + 工具结果(3),第 3 回合是 5。差一个回合不是漏写 ——
	# 这正是"水位必须和正文一起存"的意思:它说的是**那一刻**的进度。
	assert [v[1][0] for v in versions] == [1, 3, 5], versions


def _run_with_watch(wiring, monkeypatch, responses, tools, watch):
	box = list(responses)

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		return box.pop(0)

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	history = [{"role": "user", "content": "干活"}]
	return agent.agent_loop(
		history, active_request="干活", system="s", tools=tools, model="m",
		max_rounds=5, compactor=_Pass(), emit=wiring["emit"],
		ask=lambda question: False, stream=False, record=wiring["record"],
		checkpoint=watch,
		begin_exec=lambda uid, name, data: wiring["store"].begin_tool_exec(
			wiring["turn"]["id"], uid, name, data))


def test_真循环_关键记录写不进去就停(wiring, monkeypatch):
	calls = []
	tools = [_tool("read_file", False, calls)]
	responses = [
		_response([_tool_use("read_file", "t1", path="a.txt")], "tool_use"),
		_response([_text("永远到不了这一句")]),
	]
	boom = {"on": False}
	real = wiring["store"].append_turn_message

	def flaky(*a, **kw):
		if boom["on"]:
			raise sessions.PersistError("库写不进去了")
		return real(*a, **kw)

	monkeypatch.setattr(wiring["store"], "append_turn_message", flaky)

	def arm(messages, loop_state, compacted):
		# assistant 那条落库之后开始使坏:工具结果写不进去
		boom["on"] = True
		wiring["checkpoint"](messages, loop_state, compacted)

	with pytest.raises(sessions.PersistError):
		_run_with_watch(wiring, monkeypatch, responses, tools, arm)
	# 抛出去是**对的**:谁来接、怎么标中断是服务端的事(见 server._drive),
	# 而循环这边绝不能把它变成一句工具输出然后接着跑
	assert calls == [], "记录没落库还是执行了工具"


def test_真循环_记录先于事件(wiring, monkeypatch):
	"""库里先有那条消息,页面才收到对应事件。

	反过来的话,页面拿事件游标当分界读轮次时,中间那次读会既没有这条消息、
	又已经跳过了它的事件 —— 页面上凭空少一条工具结果,刷新也补不回来。
	"""
	calls = []
	tools = [_tool("bash", True, calls)]
	responses = [
		_response([_tool_use("bash", "t1", command="ls")], "tool_use"),
		_response([_text("干完了")]),
	]
	orders = []
	real_record = wiring["record"]

	def record(kind, role, content, tool_use_id=None):
		rows = _rows(wiring, "SELECT COUNT(*) FROM turn_messages")[0][0]
		orders.append(("record", kind, rows))
		return real_record(kind, role, content, tool_use_id=tool_use_id)

	def emit(event):
		orders.append(("emit", event["kind"],
		               _rows(wiring, "SELECT COUNT(*) FROM turn_messages")[0][0]))
		wiring["emit"](event)

	box = list(responses)

	def fake_call_api(llm_client, emit_, stream=True, purpose="main", **kwargs):
		return box.pop(0)

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	history = [{"role": "user", "content": "干活"}]
	agent.agent_loop(history, active_request="干活", system="s", tools=tools,
	                 model="m", max_rounds=5, compactor=_Pass(), emit=emit,
	                 ask=lambda q: False, stream=False, record=record,
	                 checkpoint=wiring["checkpoint"],
	                 begin_exec=lambda uid, name, data:
		                 wiring["store"].begin_tool_exec(
			                 wiring["turn"]["id"], uid, name, data))

	tool_result_record = [i for i, o in enumerate(orders)
	                      if o[0] == "record" and o[1] == "tool_result"]
	tool_result_emit = [i for i, o in enumerate(orders)
	                    if o[0] == "emit" and o[1] == "tool_result"]
	assert tool_result_record and tool_result_emit, orders
	assert tool_result_record[0] < tool_result_emit[0], orders


def test_真循环_号拼在结果尾巴上_而且库里查得回来(wiring, monkeypatch):
	calls = []
	tools = [_tool("bash", True, calls)]
	responses = [
		_response([_tool_use("bash", "t1", command="ls")], "tool_use"),
		_response([_text("干完了")]),
	]
	history, outcome = _run(wiring, monkeypatch, responses, tools)

	result = history[2]["content"][0]
	assert "m000" in result["content"], result
	row_id = int(result["content"].split("m000")[-1].split("<")[0]) if "m000" in \
		result["content"] else 0
	# 号就是行号:按它查得到那条结果,而且查到的是**不带号**的那一份原文
	got = wiring["store"].find_message(wiring["sid"], row_id)
	assert got is not None, result["content"]
	# content_json 存的是那一个内容块(数组里只有它)
	assert "m000" not in got[0]["content"], got
