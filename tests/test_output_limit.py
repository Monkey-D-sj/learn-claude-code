"""输出撞上 max_tokens 上限:这一轮是**半截**的,不能当正常收尾。

为什么值得单独一个文件:这条路上坏掉的症状是"**什么都没发生**"。

模型拿 8,000 的输出预算,thinking 和正文算在同一笔里,于是 thinking 把预算吃光
时 text 块一个都不生成 —— 而主循环原来的收尾判断是"没有工具调用 = 干完了",
于是 `final_text` 返回的空字符串被当成这一轮的答复交出去,status 还是 completed,
账上一切正常。子 agent 就是这么栽的:三次派活三次撞上 8,000,三次都朝主 agent
回了一句空话,而主 agent 唯一的信息源就是那句话 —— 它拿着空白往下做。

守四件事:

  一、**status 是 failed,error 说得清是为什么。** 交不出结果就是交不出,
     记成 completed 等于把一次失败伪装成一次成功。

  二、**text 里不能只剩空白。** 调用方(尤其是子 agent 那条 `[subagent failed]`
     前缀)靠这段文字决定下一步;一个空字符串和"干完了"长得一模一样。

  三、**半截的 tool_use 不能被执行。** 截断的 tool_use 是半个 JSON,参数缺一半,
     拿它去写文件就是拿一把没齿的钥匙开门。

  四、**那条 assistant 消息不进 messages,但要进 record。** 它可能带着一个没有
     tool_result 的 tool_use,留在上下文里用户下次提问直接 400(整个循环的前提
     就是退出的位置停在完整回合上);而存档那头照记不误,页面上刷新之后还得
     看得到模型当时说了什么。

跑法: uv run pytest
"""

from types import SimpleNamespace

import agent


class _Pass:
	"""压缩器在这儿只是个占位:这一项验的是收尾怎么判,不是压缩。"""

	def prepare(self, messages, active_request, checkpoint):
		return messages


def _text(text: str):
	return SimpleNamespace(type="text", text=text)


def _tool_use(name: str = "dangerous"):
	"""一个"拼到一半"的工具调用:注意 input 是空的 —— 真实截断长这样。"""
	return SimpleNamespace(type="tool_use", id="t1", name=name, input={})


def _response(content, stop_reason: str):
	return SimpleNamespace(content=content, stop_reason=stop_reason)


class _Recorder:
	def __init__(self):
		self.seen = []

	def __call__(self, kind, role, content):
		self.seen.append((kind, role, content))


def run(monkeypatch, response, *, tools=None, calls=None, history=None):
	"""跑一轮,模型第一次就返回 response。返回 (history, outcome, record)。

	calls 给定时记下工具 handler 被调了几次 —— 第三、四条靠它证。"""
	record = _Recorder()
	history = history if history is not None else [
		{"role": "user", "content": "干这件活"}
	]

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		return response

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	outcome = agent.agent_loop(
		history, active_request="干这件活", system="s", tools=tools or [],
		model="m", max_rounds=5, compactor=_Pass(), emit=lambda e: None,
		ask=lambda question: False, stream=False, record=record)
	return history, outcome, record


def _tool(monkeypatch, calls):
	"""一个假工具:handler 被调用就往 calls 里记一笔。

	PreToolUse 那边还有一道权限 hook,可能先把它拦下 —— 无所谓,这一项要证的
	是"**根本没走到执行那一步**",所以拦下来也算通过。
	"""
	return SimpleNamespace(
		name="dangerous",
		to_wire=lambda: {"name": "dangerous", "description": "d",
		                 "input_schema": {"type": "object", "properties": {}}},
		handler=lambda **kw: calls.append(kw))


def test_撞上限是failed不是completed(monkeypatch):
	history, outcome, _ = run(monkeypatch, _response([_text("做了一半")], "max_tokens"))

	assert outcome.status == "failed", "半截的答复被记成了完成"
	assert "truncated" in outcome.error and "incomplete" in outcome.error, outcome.error


def test_一个text块都没有时交出去的不是空字符串(monkeypatch):
	"""thinking 吃光预算时 text 不生成 —— 原来的代码在这儿交出一个空字符串。"""
	history, outcome, _ = run(monkeypatch, _response([], "max_tokens"))

	assert outcome.status == "failed", outcome.error
	assert outcome.text.strip(), "交出去的是空的,调用方分不出它和'干完了'"
	assert "truncated" in outcome.text, outcome.text


def test_半截的工具调用不执行(monkeypatch):
	calls = []
	history, outcome, _ = run(
		monkeypatch, _response([_text("调个工具"), _tool_use()], "max_tokens"),
		tools=[_tool(monkeypatch, calls)])

	assert calls == [], "拼了一半的工具调用被执行了"
	assert outcome.status == "failed", outcome.error


def test_截断的那条不进messages但进record(monkeypatch):
	"""留一个没有 tool_result 的 tool_use 在上下文里,下次提问就是 400。"""
	history, outcome, record = run(
		monkeypatch, _response([_text("半句"), _tool_use()], "max_tokens"))

	assert len(history) == 1, f"截断的消息漏进了上下文:{history}"
	assert history[-1]["role"] == "user", history[-1]

	kinds = [kind for kind, _, _ in record.seen]
	assert "assistant_response" in kinds, "存档里也没留下,页面上就是一片空白"


def test_没撞上限时行为不变(monkeypatch):
	"""闸门只该管截断那一格:正常收尾照旧 completed。"""
	history, outcome, record = run(monkeypatch, _response([_text("干完了。")], "end_turn"))

	assert outcome.status == "completed", outcome.error
	assert outcome.text == "干完了。"
	assert len(history) == 2 and history[-1]["role"] == "assistant", history


def test_非流式那条路顶在SDK闸门之下():
	"""SDK 对非流式有一道**本地**闸门:估出来超过 10 分钟就抛 ValueError,
	请求根本不发。边界 = 600 * 128_000 / 3_600 = 21,333.3。

	所以非流式那条路的预算一旦超过 21,333,子 agent **每一次调用**都会当场
	炸 —— 而且是个 ValueError,不是 APIError,连重试那条路都进不去。

	这条不碰网络:它钉的是"两个常量满足那个不等式"。哪天有人为了省事把它俩
	改成同一个数,SDK 的报错会出现在运行时的子 agent 里,而不是这儿。
	"""
	# SDK 的算法照抄一遍(_base_client._calculate_nonstreaming_timeout):
	# 抄错了这条测试自己会先炸,这正是它能当边界用的原因。
	expected_seconds = 3600 * agent.MAX_OUTPUT_TOKENS_NONSTREAM / 128_000
	assert expected_seconds <= 600, (
		f"非流式预算 {agent.MAX_OUTPUT_TOKENS_NONSTREAM:,} 会被 SDK 当场拒掉"
		f"(它估 {expected_seconds:.0f}s > 600s)")
	assert agent.MAX_OUTPUT_TOKENS > agent.MAX_OUTPUT_TOKENS_NONSTREAM, \
		"流式那条路才该是大的那个"


def test_两条路各拿各的预算(monkeypatch):
	"""常量分开了,但用错一个还是炸 —— 而且炸在子 agent 里面。

	它那一层只有 handler 的 `except Exception`,所以症状是主 agent 收到一句
	`Error: ValueError: Streaming is required...`,而子 agent 一次都没跑成。
	"""
	seen = {}

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		seen["max_tokens"] = kwargs["max_tokens"]
		return _response([_text("干完了。")], "end_turn")

	monkeypatch.setattr(agent, "call_api", fake_call_api)

	def go(stream):
		agent.agent_loop([{"role": "user", "content": "干这件活"}],
		                 active_request="干这件活", system="s", tools=[], model="m",
		                 max_rounds=5, compactor=_Pass(), emit=lambda e: None,
		                 ask=lambda q: False, stream=stream, record=lambda *a: None)
		return seen["max_tokens"]

	assert go(False) == agent.MAX_OUTPUT_TOKENS_NONSTREAM, "子 agent 那条路拿了流式的预算"
	assert go(True) == agent.MAX_OUTPUT_TOKENS


def test_有工具调用且正常结束时照旧往下走(monkeypatch):
	"""stop_reason=tool_use 不能被新加的判断误伤 —— 那是循环继续的正路。"""
	calls = []

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		if len(calls) == 0:
			calls.append("tool")
			return _response([_tool_use()], "tool_use")
		return _response([_text("干完了。")], "end_turn")

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	tool = SimpleNamespace(
		name="dangerous",
		to_wire=lambda: {"name": "dangerous", "description": "d",
		                 "input_schema": {"type": "object", "properties": {}}},
		handler=lambda **kw: "ok")
	outcome = agent.agent_loop(
		[{"role": "user", "content": "干这件活"}], active_request="干这件活",
		system="s", tools=[tool], model="m", max_rounds=5, compactor=_Pass(),
		emit=lambda e: None, ask=lambda question: False, stream=False,
		record=lambda kind, role, content: None)

	assert outcome.status == "completed", outcome.error
	assert outcome.text == "干完了。"
