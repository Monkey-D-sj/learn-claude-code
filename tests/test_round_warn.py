"""轮数提醒:快用完的时候提前说一声。

不提醒的话,撞上限那一轮交付给用户的是 `Stopped: round limit of 50 reached,
task incomplete.` —— 前面几十轮的工作一个字都没出来。提醒要解决的就是这个:
让最后一次调用说出一句人读得懂的答复(做完了什么、剩了什么没做)。

守四件事,每一件坏掉都不报错:

  一、**只提醒最后几次。** 从第一轮就喊"预算紧张"的话,模型会一直处于收尾
     心态,该干的活也不干了。判据是"这一次之后还剩几次",而最后一次收到的
     必须是 `0 more allowed after this one` —— 差一的话,模型以为还能再调
     一个工具,而那一次之后循环直接停,这一轮又白跑。

  二、**提醒不进 history。** 这是它跟 todo 那条提醒的唯一区别。todo 那条留在
     上下文里是无害的唠叨;`你只剩 1 轮` 留到下一轮就是主动使坏 —— 用户换个
     问题再问,模型一上来就看见"预算要没了",于是草草答一句,而且会一直赖
     到被压缩归档。

  三、**并进尾巴那条消息,不另起一条。** 另起一条是合法的(连续两条 user
     没问题),但会把"提醒"从"你刚拿到的工具结果"里摘出去,变成一个独立的
     指令。这里只是想要它贴在结果旁边。

  四、**尾巴是纯字符串时不展开。** Stop hook 塞进来的控制消息 content 是个
     str,`[*content]` 会把它炸成一个个字符,页面上没事、上下文里就是一堆
     单字消息,而且不报错。

跑法: uv run pytest
"""

import json
from types import SimpleNamespace

import agent

WARN = 3


class _Pass:
	"""压缩器在这儿只是个占位:这一项验的是 payload 怎么拼,不是压缩。"""

	def prepare(self, messages, active_request, checkpoint):
		return messages


def _texts(payload) -> str:
	"""一条 payload 里所有 text 块拼起来 —— 提醒就混在这里面。

	工具结果的 content 是个字符串,不带 type,所以不会误收。"""
	return " ".join(
		block.get("text", "")
		for message in payload if isinstance(message.get("content"), list)
		for block in message["content"]
		if isinstance(block, dict) and block.get("type") == "text")


def _tool_use(n: int):
	return SimpleNamespace(
		content=[SimpleNamespace(type="tool_use", id=f"t{n}", name="nope", input={})],
		stop_reason="tool_use")


def run(monkeypatch, payloads: list, answer_at: int | None = None,
        max_rounds: int = 5, stop_once: bool = False, sizes: list | None = None):
	"""跑一轮,把每次调用收到的 payload 收进 payloads。

	answer_at 给定时,那一次调用回正文(不调工具),其余一律要工具 —— 于是
	循环一定会走到最后几次调用,把提醒都发出来。

	sizes 给定时,顺带记下"那一刻 history 有多长" —— 拿它跟 payload 比长度,
	就能分出提醒是并进了尾巴那条消息,还是另起了一条。

	用假 call_api 而不是假 client:要验的就是 agent_loop 拼出来的 payload,
	而 client 那一层只看得到 kwargs。"""
	calls = []
	history = [{"role": "user", "content": "重构这个模块"}]

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		payloads.append(kwargs["messages"])
		if sizes is not None:
			sizes.append(len(history))
		calls.append(1)
		if answer_at is not None and len(calls) == answer_at:
			return SimpleNamespace(
				content=[SimpleNamespace(type="text", text="做完了 A;B 没做完。")],
				stop_reason="end_turn")
		return _tool_use(len(calls))

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	if stop_once:
		# Stop hook 拦一次,逼循环带着一条**纯字符串**的控制消息继续走到
		# 下一次调用 —— 那一次尾巴就不是 list 了。见本文件第四条。
		fired = []

		def fake_hooks(event, *args):
			if event == "Stop" and not fired:
				fired.append(1)
				return "别停,接着干"
			return None

		monkeypatch.setattr(agent, "trigger_hooks", fake_hooks)

	outcome = agent.agent_loop(history, active_request="重构这个模块", system="s",
	                           tools=[], model="m", max_rounds=max_rounds,
	                           compactor=_Pass(), emit=lambda e: None,
	                           ask=lambda question: False, stream=False,
	                           record=lambda kind, role, content, tool_use_id=None: None)
	return history, outcome


def warned(payload) -> bool:
	return "Round budget" in _texts(payload)


def test_只有最后几次调用带提醒(monkeypatch):
	payloads = []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)
	_, outcome = run(monkeypatch, payloads, max_rounds=5)

	assert outcome.status == "failed", "假模型每次都要工具,一定撞上限"
	assert [warned(p) for p in payloads] == [False, False, True, True, True], \
		f"该是最后 {WARN} 次提醒,实际 {[warned(p) for p in payloads]}"


def test_最后一次说的是还剩零次(monkeypatch):
	"""差一就在这里咬人:最后一次要是写着"还剩 1 次",模型会再调一个工具。"""
	payloads = []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)
	run(monkeypatch, payloads, max_rounds=5)

	said = [_texts(p) for p in payloads if warned(p)]
	assert "call 3 of 5, with 2 more" in said[0], said[0]
	assert "call 5 of 5, with 0 more allowed after this one" in said[-1], said[-1]


def test_提醒不进_history(monkeypatch):
	"""上一轮的提醒不能活到下一次提问。"""
	payloads = []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)
	history, _ = run(monkeypatch, payloads, max_rounds=5)

	assert warned(payloads[-1]), "前提:最后一次确实提醒过"
	dump = json.dumps(history, ensure_ascii=False, default=str)
	assert "Round budget" not in dump, "提醒漏进了调用方那份 history"


def test_提醒并进尾巴那条消息而不是另起一条(monkeypatch):
	payloads, sizes = [], []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)
	run(monkeypatch, payloads, max_rounds=5, sizes=sizes)

	tail = payloads[-1][-1]
	assert len(payloads[-1]) == sizes[-1], "比那一刻的 history 长,就是另起了一条"
	assert tail["role"] == "user" and isinstance(tail["content"], list)
	assert tail["content"][-1]["text"].startswith("<reminder>Round budget")


def test_尾巴是纯字符串时另起一条_不展开(monkeypatch):
	"""Stop hook 那条控制消息的 content 是 str,[*content] 会炸成一个个字符。"""
	payloads = []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)
	run(monkeypatch, payloads, answer_at=2, max_rounds=5, stop_once=True)

	# 第 2 次调用回了正文,Stop hook 拦下并塞了一条 str,于是第 3 次调用的
	# 尾巴是那条 str(带提醒的那一次就是它)。
	tail = payloads[2][-1]
	assert isinstance(tail["content"], list), "纯字符串那条被就地展开了"
	assert all(isinstance(block, dict) for block in tail["content"]), tail["content"]
	assert warned(payloads[2])


def test_照提醒答复就交付得出去(monkeypatch):
	"""这一项是整件事的目的:最后一次调用给出答复,而不是 `round limit reached`。

	假模型只在看到 `0 more allowed`(也就是最后一次)时才回正文 —— 不提醒的
	话它每次都调工具,于是这一轮以 failed 收尾,用户什么都拿不到。"""
	payloads = []
	monkeypatch.setattr(agent, "ROUND_WARN", WARN)

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		payloads.append(kwargs["messages"])
		if "with 0 more allowed after this one" in _texts(kwargs["messages"]):
			return SimpleNamespace(
				content=[SimpleNamespace(type="text", text="做完了 A;B 没做完,原因是轮数用尽。")],
				stop_reason="end_turn")
		return _tool_use(len(payloads))

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	outcome = agent.agent_loop([{"role": "user", "content": "重构这个模块"}],
	                           active_request="重构这个模块", system="s", tools=[],
	                           model="m", max_rounds=5, compactor=_Pass(),
	                           emit=lambda e: None, ask=lambda question: False,
	                           stream=False)

	assert outcome.status == "completed", outcome.error
	assert "B 没做完" in outcome.text
