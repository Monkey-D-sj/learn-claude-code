"""流式的特征化测试:假客户端喂脚本化的 SSE,不碰网络。

盯四件事:

  一、碎片按序、原样转发(工具参数的碎片不转发 —— 拼好了才有意义)
  二、返回的还是那个"拼好的 message",下游一行不用改
  三、**吐过字就不许重试**(重试会让页面上凭空重复一段)
  四、碎片不落库、不带位置标记

跑法: uv run pytest
"""

from types import SimpleNamespace

import pytest

import anthropic

import agent
import sessions

# 这版 SDK 把 httpx 改名成了 httpx2(见 agent.error_chain 的注释),
# 造一个带状态码的错要用它。
import httpx2


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
	"""别在测试里真睡 —— 退避的秒数不是这几项要验的东西。"""
	monkeypatch.setattr(agent, "BASE_DELAY", 0)


def status_error(code: int) -> Exception:
	req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
	return anthropic.APIStatusError("炸了", response=httpx2.Response(code, request=req),
	                                body=None)


def delta(kind: str, text: str):
	"""一条碎片事件。形状跟 SDK 发出来的一样:外层 type + 内层 delta。

	字段名两种不统一:text_delta 用 text,thinking_delta 用 thinking —— 照抄。"""
	inner = {"type": kind, "thinking" if kind == "thinking_delta" else "text": text}
	return SimpleNamespace(type="content_block_delta", index=0,
	                       delta=SimpleNamespace(**inner))


def tool_json_delta(fragment: str):
	"""工具参数的碎片。它**不该**被转发出去。"""
	return SimpleNamespace(type="content_block_delta", index=1,
	                       delta=SimpleNamespace(type="input_json_delta",
	                                             partial_json=fragment))


FINAL = SimpleNamespace(content=[SimpleNamespace(type="text", text="你好，世界")],
                        stop_reason="end_turn")


class FakeStream:
	def __init__(self, events, boom_after=None, boom=None, final=FINAL):
		self.events, self.boom_after, self.boom, self.final = events, boom_after, boom, final

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def __iter__(self):
		for i, event in enumerate(self.events):
			if self.boom_after == i:
				raise self.boom
			yield event
		if self.boom_after is not None and self.boom_after >= len(self.events):
			raise self.boom

	def get_final_message(self):
		return self.final


class FakeClient:
	"""记下每次调用,好数"重试了几次"。"""

	def __init__(self, scripts):
		self.scripts = list(scripts)          # 每次调用消费一个:要么 FakeStream,要么异常
		self.streams = 0
		self.creates = 0
		self.kwargs_seen = []
		self.messages = SimpleNamespace(stream=self._stream, create=self._create)

	def _next(self, kwargs):
		self.kwargs_seen.append(kwargs)
		item = self.scripts.pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	def _stream(self, **kwargs):
		self.streams += 1
		return self._next(kwargs)

	def _create(self, **kwargs):
		self.creates += 1
		return self._next(kwargs)


def run_call(client, **over):
	"""跑一次 call_api,把转发出去的碎片收集起来。"""
	got = []
	kwargs = {"model": "m", "messages": [{"role": "user", "content": "嗨"}],
	          "system": "s", "tools": [], "max_tokens": 8}
	kwargs.update(over)
	result = agent.call_api(client, got.append, **kwargs)
	return result, got


def test_碎片按序转发_工具参数的碎片不转发():
	events = [delta("thinking_delta", "想"), delta("thinking_delta", "了想"),
	          tool_json_delta('{"path"'), tool_json_delta(': "/c"}'),
	          delta("text_delta", "你好"), delta("text_delta", "，世界")]
	client = FakeClient([FakeStream(events)])
	result, got = run_call(client)
	assert result is FINAL, result
	assert got == [
		{"kind": "delta", "target": "thinking", "text": "想"},
		{"kind": "delta", "target": "thinking", "text": "了想"},
		{"kind": "delta", "target": "text", "text": "你好"},
		{"kind": "delta", "target": "text", "text": "，世界"},
	], got


def test_返回的是拼好的那个message_参数原样传下去():
	client = FakeClient([FakeStream([delta("text_delta", "嗨")])])
	result, _ = run_call(client, metadata={"user_id": "u1"})
	assert result is FINAL
	seen = client.kwargs_seen[0]
	assert seen["model"] == "m" and seen["max_tokens"] == 8, seen
	assert seen["metadata"] == {"user_id": "u1"}, seen
	assert client.streams == 1 and client.creates == 0


def test_吐过字之后不重试_一次调用直接抛():
	err = status_error(500)
	client = FakeClient([FakeStream([delta("text_delta", "吐了")], boom_after=1, boom=err),
	                     FakeStream([delta("text_delta", "重来的")])])
	with pytest.raises(anthropic.APIStatusError) as exc:
		run_call(client)
	assert exc.value is err, exc.value
	assert client.streams == 1, f"重试了 {client.streams} 次,页面上会重复一段"


def test_一个字都没吐时照旧重试():
	client = FakeClient([status_error(500), FakeStream([delta("text_delta", "第二次")])])
	result, got = run_call(client)
	assert result is FINAL
	assert client.streams == 2, client.streams
	# 先一条 retry 的旁注(重试了就该让页面知道),再是第二次的碎片
	assert [e["kind"] for e in got] == ["note", "delta"], got
	assert got[-1] == {"kind": "delta", "target": "text", "text": "第二次"}, got


def test_不可重试的错一次都不重试():
	client = FakeClient([status_error(400), FakeStream([])])
	with pytest.raises(anthropic.APIStatusError) as exc:
		run_call(client)
	assert exc.value.status_code == 400
	assert client.streams == 1, client.streams


def test_关掉流式_走原来那个入口_一个碎片都不发():
	client = FakeClient([FINAL])
	result, got = run_call(client, stream=False)
	assert result is FINAL
	assert got == [], got
	assert client.streams == 0 and client.creates == 1


class _Passthrough:
	"""压缩器在这几项里只是个占位:验的是 stream 有没有透传下去。"""

	def prepare(self, messages, active_request, checkpoint):
		return messages


def run_loop(**over):
	"""跑一次 agent_loop,把转发出去的事件收集起来。

	客户端不给参数 —— agent_loop 用的是 agent 模块级那个 client,所以调用方
	得自己 monkeypatch 掉它。"""
	got = []
	kwargs = {"active_request": "问题", "system": "s", "tools": [], "model": "m",
	          "max_rounds": 3, "compactor": _Passthrough(), "emit": got.append,
	          "ask": lambda question: False}
	kwargs.update(over)
	outcome = agent.agent_loop([{"role": "user", "content": "问题"}], **kwargs)
	return outcome, got


def test_agent_loop_把stream透传给call_api(monkeypatch):
	# 不传:照旧流式,碎片发出来
	streaming = FakeClient([FakeStream([delta("text_delta", "你好")])])
	monkeypatch.setattr(agent, "client", streaming)
	outcome, got = run_loop()
	assert outcome.status == "completed" and outcome.text == "你好，世界", outcome
	assert streaming.streams == 1 and streaming.creates == 0, \
		f"streams={streaming.streams} creates={streaming.creates}"
	assert [e["kind"] for e in got] == ["delta"], got

	# stream=False:走原来那个入口,一个碎片都不发。子 agent 靠这个关掉流式
	# —— 关掉之后"吐过字就不许重试"那条也就不再对它生效。
	plain = FakeClient([FINAL])
	monkeypatch.setattr(agent, "client", plain)
	outcome, got = run_loop(stream=False)
	assert outcome.status == "completed" and outcome.text == "你好，世界", outcome
	assert plain.streams == 0 and plain.creates == 1, \
		f"streams={plain.streams} creates={plain.creates}"
	assert got == [], got


def test_子agent_关掉流式(monkeypatch):
	"""上面那条钉的是"机制在",这条钉的是"子 agent 确实用了它"。

	两件事分开,是因为它们会各自坏:参数被谁删掉,或者 subagent 那一行被
	谁顺手改成默认值 —— 两种坏法都不会报错,只会让子 agent 重新变成吐过
	字就不能重试。"""
	import tools.subagent as subagent

	seen = {}

	def fake_loop(messages, **kwargs):
		seen.update(kwargs)
		return agent.TurnOutcome("completed", "结论")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	assert subagent.run_task("去看看") == "结论"
	assert seen.get("stream") is False, seen.get("stream")


def test_碎片不落库_不带位置标记(tmp_path, monkeypatch):
	import server

	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(server, "STORE", store)

	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	pushed = []
	emit = server.recording_emit(sid, pushed.append, turn)

	emit({"kind": "delta", "target": "text", "text": "你"})
	emit({"kind": "delta", "target": "text", "text": "好"})
	assert store.events_since(sid, 0) == [], "碎片落库了"
	assert pushed == [
		{"kind": "delta", "target": "text", "text": "你",
		 "turn_id": turn["id"], "turn_no": 1},
		{"kind": "delta", "target": "text", "text": "好",
		 "turn_id": turn["id"], "turn_no": 1},
	], pushed
	assert all("seq" not in ev for ev in pushed), pushed

	# 完整事件照旧:落库、带 seq
	emit({"kind": "reply", "text": "你好", "status": "completed"})
	stored = store.events_since(sid, 0)
	assert [e[0] for e in stored] == [1], stored
	assert stored[0][1]["kind"] == "reply" and "seq" not in stored[0][1]
	assert pushed[-1]["seq"] == 1, pushed[-1]
