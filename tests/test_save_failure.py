"""保存失败的语义(R3):**"这一轮跑出什么"和"这一轮存没存上"是两件事。**

修之前:`finish_turn` 抛异常只发一条灰色旁注,**reply 事件里的 status 照旧是
模型的心气**。于是模型跑完、库没写进去的那一轮,页面显示成普通的"完成";
用户接着问,模型手里是上一版上下文,而它看不出少了什么。

盯五件事:

  一、保存失败时 reply 发的是 `status: "unsaved"`,不是 `completed` —— 模型
     那段文字留着(它在库里没有备份,页面上是唯一一份),另外单发一份
     `model_status`:"干活的结果"和"存下来的结果"用户两个都要知道
  二、库里那一轮**还停在 running**(终态那一次写就没成功)。不许声称 failed
     已经落库 —— 那是另一件没发生的事
  三、下一轮**不许**拿旧上下文偷偷开跑:先把那份补写进去,补不进去就 503。
     这就是"旧上下文不能被悄悄用于下一轮"
  四、补写**不重跑模型、不重跑工具**:手上就有最终上下文,补的是写。验收里
     "恢复保存时工具执行次数不增加"说的就是这条
  五、状态冲突(收尾时那一轮在库里已经是终态)不进那道闸 —— 它重试也没用,
     进了的话这个会话就永远问不下去了。它的回滚本身在 test_sessions.py 里验

跑法: uv run pytest
"""

import json
import threading

import pytest

import server
import sessions
from agent import TurnOutcome


class _FakeHandler:
	"""让 _post_ask 按非绑定方法调起来(不建 socket、不走路由)。"""

	# 这几步是按 handler 调的,接到真实现上去
	_run_turn = server.Handler._run_turn
	_drive = server.Handler._drive
	_checkpoint = server.Handler._checkpoint
	_flush_unsaved = server.Handler._flush_unsaved

	def __init__(self, body=None):
		self.body = body
		self.json = None
		self.error = None
		self.wrote = b""
		self.headers = {}
		self.wfile = self

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
		self.wrote += data

	def flush(self):
		pass

	def events(self) -> list[dict]:
		"""这一轮往响应流里写过的 NDJSON 事件。"""
		return [json.loads(line) for line in self.wrote.decode("utf-8").splitlines()
		        if line.strip()]


def _replies(handler) -> list[dict]:
	return [e for e in handler.events() if e["kind"] == "reply"]


@pytest.fixture(autouse=True)
def _clean_unsaved():
	"""UNSAVED 是进程级的,别让上一项用例的残留串到下一项。"""
	server.UNSAVED.clear()
	yield
	server.UNSAVED.clear()


@pytest.fixture
def env(monkeypatch, tmp_path):
	"""临时库 + 一个会话,装进 server.STORE。返回 (store, sid)。"""
	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(server, "STORE", store)
	return store, store.create_session("", "")["id"]


def _scripted_loop(seen: dict):
	"""替身循环:记下每一轮拿到的历史,并像真循环那样往 messages 尾巴上追加
	一条 assistant —— 不追加的话"补写的那份上下文里有没有上一轮"就验不了。"""
	def fake_loop(messages, **kwargs):
		seen["calls"] = seen.get("calls", 0) + 1
		seen.setdefault("histories", []).append(list(messages))
		text = f"第{seen['calls']}轮的回答"
		messages.append({"role": "assistant",
		                 "content": [{"type": "text", "text": text}]})
		return TurnOutcome("completed", text)
	return fake_loop


def _break_saves(monkeypatch, store, exc: Exception):
	def boom(*a, **kw):
		raise exc
	monkeypatch.setattr(store, "finish_turn", boom)


def test_保存失败_不显示成普通完成(env, monkeypatch):
	store, sid = env
	seen = {}
	monkeypatch.setattr(server, "agent_loop", _scripted_loop(seen))
	_break_saves(monkeypatch, store, RuntimeError("disk I/O error"))

	handler = _FakeHandler({"session": sid, "query": "干活"})
	server.Handler._post_ask(handler)
	assert handler.error is None, handler.error

	reply = _replies(handler)[0]
	assert reply["status"] == "unsaved", reply
	assert reply["saved"] is False, reply
	assert reply["model_status"] == "completed", reply
	assert "disk I/O error" in reply["save_error"], reply
	# 模型那段文字必须留着:库里没有它,页面上是唯一一份
	assert reply["text"] == "第1轮的回答", reply

	# 而库里那一轮还是 running —— 不声称 failed 已经落库(那是另一件没发生的事)
	turn = store.list_turns(sid)["turns"][-1]
	assert turn["status"] == "running", turn
	assert turn["error_message"] is None, turn

	# 人也得在流里看到一句解释,不能只有一个状态词
	notes = [e for e in handler.events()
	         if e["kind"] == "note" and e.get("source") == "store"]
	assert notes and "没存进库" in notes[0]["text"], notes


def test_保存失败之后_下一轮不许拿旧上下文偷偷开跑(env, monkeypatch):
	"""闸在会话锁里、开轮之前:补不进去就 503,而不是拿旧上下文接着聊。"""
	store, sid = env
	seen = {}
	monkeypatch.setattr(server, "agent_loop", _scripted_loop(seen))
	_break_saves(monkeypatch, store, RuntimeError("disk I/O error"))

	first = _FakeHandler({"session": sid, "query": "干活"})
	server.Handler._post_ask(first)
	assert seen["calls"] == 1

	second = _FakeHandler({"session": sid, "query": "接着干"})
	server.Handler._post_ask(second)
	assert second.error == (503, "previous turn result is not saved yet"), second.error
	# 模型一次都没被再叫(那一轮根本没开)
	assert seen["calls"] == 1, seen["calls"]
	assert len(store.list_turns(sid)["turns"]) == 1, "开了一轮不该开的"
	assert sid in server.UNSAVED, "那份没存上的记录被丢了"


def test_库恢复之后补写_不重跑模型(env, monkeypatch):
	store, sid = env
	seen = {}
	monkeypatch.setattr(server, "agent_loop", _scripted_loop(seen))

	broken = {"on": True}
	real = store.finish_turn

	def flaky(*a, **kw):
		if broken["on"]:
			raise RuntimeError("disk I/O error")
		return real(*a, **kw)

	monkeypatch.setattr(store, "finish_turn", flaky)

	first = _FakeHandler({"session": sid, "query": "干活"})
	server.Handler._post_ask(first)
	assert _replies(first)[0]["status"] == "unsaved"
	assert store.list_turns(sid)["turns"][-1]["status"] == "running"

	# 库能写了。下一次提问:先补写上一轮,再开新的一轮
	broken["on"] = False
	second = _FakeHandler({"session": sid, "query": "接着干"})
	server.Handler._post_ask(second)
	assert second.error is None, second.error
	assert _replies(second)[0]["status"] == "completed", _replies(second)[0]

	# 补写用的是**手上那份最终上下文**:上一轮写成 completed,而且它进了库
	turns = store.list_turns(sid)["turns"]
	assert [t["status"] for t in turns] == ["completed", "completed"], turns
	assert sid not in server.UNSAVED, "补上了还留着记录,下一轮会被再挡一次"

	# 工具执行次数不增加 = 模型只被叫了两次(两轮各一次),没有为补写重跑
	assert seen["calls"] == 2, seen["calls"]
	# 而且第二轮的历史里真的有第一轮:这就是"补写生效了"
	second_history = seen["histories"][1]
	texts = [b.get("text") for m in second_history
	         if isinstance(m.get("content"), list) for b in m["content"]]
	assert "第1轮的回答" in texts, second_history


def test_刷新之后页面还知道那一轮没存上(env, monkeypatch):
	"""/turns 里补一条 unsaved:保存失败的话库里那一轮还是 running,光看库里
	的状态,刷新之后页面只会显示"运行中" —— 而真相是它已经跑完了。"""
	store, sid = env
	monkeypatch.setattr(server, "agent_loop", _scripted_loop({}))
	_break_saves(monkeypatch, store, RuntimeError("disk I/O error"))

	server.Handler._post_ask(_FakeHandler({"session": sid, "query": "干活"}))

	handler = _FakeHandler()
	server.Handler._get_turns(handler, sid)
	turn = handler.json["turns"][-1]
	assert turn["status"] == "running", turn
	assert turn["unsaved"] == {"model_status": "completed", "error": None}, turn["unsaved"]


def test_状态冲突不进那道闸(env, monkeypatch):
	"""冲突是"这一轮在库里已经是终态",不是"库坏了"。它重试也没用 ——
	进闸的话这个会话就永远问不下去了(每次都被 503 挡回来)。"""
	store, sid = env
	seen = {}
	monkeypatch.setattr(server, "agent_loop", _scripted_loop(seen))
	_break_saves(monkeypatch, store, sessions.TurnStateConflict("已经不是 running"))

	first = _FakeHandler({"session": sid, "query": "干活"})
	server.Handler._post_ask(first)
	reply = _replies(first)[0]
	assert reply["status"] == "unsaved", reply
	# save_error 是原始异常(类型 + 消息);给人看的那句在 note 里
	assert reply["save_error"].startswith("TurnStateConflict"), reply
	notes = [e for e in first.events()
	         if e["kind"] == "note" and e.get("source") == "store"]
	assert notes and "状态冲突" in notes[0]["text"], notes
	assert sid not in server.UNSAVED, "冲突那条不该进补写闸"

	# 下一轮照常开得起来(不是 503)
	second = _FakeHandler({"session": sid, "query": "接着干"})
	server.Handler._post_ask(second)
	assert second.error is None, second.error
	assert seen["calls"] == 2, seen["calls"]
