"""按号取回:号是库里那一行的行号,模型拿它换回被压掉的原文。

守五件事,每一件坏掉都不报错:

  一、**号来自落库,不是自己数的。** 写不进去就没有行号,上层也就不发号 ——
     "有号 = 查得回来"这条不变量靠它撑着。子 agent 不接 record,所以它没有号。

  二、**查回来只在同一个会话里。** 行号是全库一张表的,A 会话拿着自己上下文
     里的一个号不该读到 B 会话的原文。

  三、**取回的内容要过预览。** 被压掉的往往就是大的,原样回灌等于把压缩白做,
     而且下一轮第 1 档又会把它落盘一遍。

  四、**没接上库时要如实说。** 没绑取回器时这个工具查什么都是空 —— 说一句
     "现在没接会话库"比回一句"查不到"强,后者会把人支去查号,而那个号没准
     是对的(装配漏了,不是号错了)。

  五、**子 agent 没有 compress。** 它发不出号,那个工具在它手里永远是死的。

跑法: uv run pytest
"""

from types import SimpleNamespace

import pytest

import agent
import context
import server
import sessions
import tools
import tools.subagent as subagent
from agent import TurnOutcome
from tools.base import ToolDesc
from tools.compress import compress as compress_tool
from tools.recall import bind_recall, make_recall, run_recall
from tools.recall import recall as recall_tool
from tools.todo import TodoManager


class _Pass:
	"""压缩器在这儿只是个占位:验的是号怎么发。"""

	def prepare(self, messages, active_request, checkpoint):
		return messages


@pytest.fixture
def store(tmp_path):
	"""每个用例一个全新的会话库。conftest 已经把 SessionStore 换成了临时路径版。"""
	return sessions.SessionStore(tmp_path / "sessions.db")


def _session_with_result(store, body) -> tuple[str, int]:
	"""(会话 id, 那条结果的号)。"""
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	row = store.append_turn_message(
		turn["id"], 2, "tool_result", "user",
		[{"type": "tool_result", "tool_use_id": "t1", "content": body}])
	return sid, row


def _compactor(tmp_path):
	"""一个真压缩器 —— 预览那套要它。client 只在摘要那次用得上,这儿不给。"""
	return context.ContextCompactor(None, "m", tmp_path, tmp_path, lambda e: None)


def _tool_use(n: int):
	"""一次回复要两个工具 —— 一轮里多个结果,号是各发各的。"""
	return SimpleNamespace(
		content=[SimpleNamespace(type="tool_use", id=f"t{n}a", name="nope", input={}),
		         SimpleNamespace(type="tool_use", id=f"t{n}b", name="nope", input={})],
		stop_reason="tool_use")


def _run(monkeypatch, record, calls: int = 1):
	"""跑一轮:前 calls 次都要工具,最后一次回正文。

	返回这一轮结束时的 history —— 那上面挂着的 tool_result 就是模型看得见的
	东西(号拼在它的 content 尾巴上)。
	"""
	history = [{"role": "user", "content": "问题"}]
	count = []

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		count.append(1)
		if len(count) > calls:
			return SimpleNamespace(
				content=[SimpleNamespace(type="text", text="完")],
				stop_reason="end_turn")
		return _tool_use(len(count))

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	agent.agent_loop(history, active_request="问题", system="s", tools=[], model="m",
	                 max_rounds=10, compactor=_Pass(), emit=lambda e: None,
	                 ask=lambda question: False, stream=False, record=record)
	return history


def _bodies(history) -> list[str]:
	"""history 里所有 tool_result 的正文,按顺序。"""
	return [block["content"] for message in history
	        if isinstance(message.get("content"), list)
	        for block in message["content"]
	        if isinstance(block, dict) and block.get("type") == "tool_result"]


# ---------------------------------------------------------------- 发号

def test_号是落库拿到的行号(monkeypatch):
	"""**号不是自己数的,是库里那一行的行号。**

	自己数一个计数器也能用,但那样"号"和"存没存进去"是两件事:发得出号不等于
	查得回来。用行号之后它们是同一件事 —— 写不进去就没有行号。
	"""
	ids = iter([41, 42])

	def record(kind, role, content):
		return next(ids) if kind == "tool_result" else None

	first, second = _bodies(_run(monkeypatch, record, calls=1))
	assert first.endswith(">m00041</message-id>"), first
	assert second.endswith(">m00042</message-id>"), second
	# 每段只挂一个号。拼两遍的话正文里留着上一个,而 _MARKER_RE 只认末尾那个
	# —— 模型看得见两个号,其中一个是野的。
	assert first.count("<message-id") == 1, first


def test_拿不到行号就不发号(monkeypatch):
	"""没接 record(子 agent)或者写库失败(返回 None)时,正文保持原样。

	不填一个编出来的号:那种号查不回来,而模型看到号就会去点它 —— 换来的是
	"这个号不在上下文里",看起来像它自己点错了。
	"""
	history = _run(monkeypatch, lambda kind, role, content: None, calls=1)
	assert all("<message-id" not in body for body in _bodies(history)), _bodies(history)


def test_没接record时照样不发号(monkeypatch):
	"""record 的默认值是 `_drop` —— 子 agent 走的就是这一条。"""
	history = _run(monkeypatch, agent._drop, calls=1)
	assert all("<message-id" not in body for body in _bodies(history)), _bodies(history)


# ---------------------------------------------------------------- 取回来

def test_查到的原文原样交回(store, tmp_path):
	"""号对得上就把那段正文交回去 —— 这就是整个功能。"""
	sid, row = _session_with_result(store, "命令输出:一切正常")
	recall = make_recall(store, sid, _compactor(tmp_path))
	assert recall(f"m{row:05d}") == "命令输出:一切正常"


def test_查不到就返回None(store, tmp_path):
	"""号不在这个会话里时不编内容 —— 由工具那一层去说人话。

	查不到是正常的:压过的段又被压了一次、号是上一轮的、或者那个前端根本
	没记库。返回 None 让上面分得清"没有"和"空"。
	"""
	sid, row = _session_with_result(store, "输出")
	other = store.create_session("项目记忆", "用户记忆")["id"]
	recall = make_recall(store, other, _compactor(tmp_path))
	assert recall(f"m{row:05d}") is None, "别的会话的号"
	assert recall("m99999") is None, "不存在的号"


def test_大结果取回来走预览不原样回灌(store, tmp_path):
	"""**取回的正文不能原样塞回上下文。**

	被压掉的往往就是大的(小的没必要压),原样回灌等于把压缩白做 —— 而且下一轮
	第 1 档压缩又会把它落一次盘。所以走跟第 1 档同一条路:落盘 + 头尾预览 +
	分片读命令。
	"""
	big = "行\n" * 40_000            # 远超 LARGE_RESULT_CHAR_LIMIT
	sid, row = _session_with_result(store, big)
	recall = make_recall(store, sid, _compactor(tmp_path))
	out = recall(f"m{row:05d}")

	assert "<persisted-output>" in out
	assert "head -c 8000" in out, "得告诉它怎么分片读,不然它会去 cat 整个文件"
	assert len(out) < 10_000, f"预览没生效:{len(out)} 字符"
	assert (tmp_path / f"m{row:05d}.txt").read_text(encoding="utf-8") == big


def test_小结果取回来就原样给(store, tmp_path):
	"""小结果套一层落盘标记反而更啰嗦 —— 它本来就没必要压。"""
	sid, row = _session_with_result(store, "输出" * 100)
	recall = make_recall(store, sid, _compactor(tmp_path))
	assert recall(f"m{row:05d}") == "输出" * 100
	assert not list(tmp_path.glob("m*.txt")), "没落盘"


def test_压掉之后原文照样查得回来(store, tmp_path):
	"""**整个功能的理由。** compress 把一段换成一句摘要,而那两头的号还在库里。

	工具描述里那句"压过的段那两头的号也查得到"就是它 —— 不成立的话,模型照着
	描述去查会得到一个"查无此号"。
	"""
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	bodies = ("第一段的原文", "第二段的原文")
	rows = [store.append_turn_message(
		turn["id"], i, "tool_result", "user",
		[{"type": "tool_result", "tool_use_id": f"t{i}", "content": body}])
		for i, body in enumerate(bodies, start=2)]

	msgs = [{"role": "user", "content": "问题"}]
	for i, row in enumerate(rows, start=1):
		msgs.append({"role": "assistant", "content": [
			{"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {}}]})
		msgs.append({"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": f"t{i}",
			 "content": context.stamp_tag("输出", f"m{row:05d}")}]})

	tags = [f"m{row:05d}" for row in rows]
	assert "已压缩" in context.compress_range(msgs, tags[0], tags[1], "两步都干完了")
	assert len(msgs) == 2, msgs          # 用户那条 + 摘要,原文全没了

	recall = make_recall(store, sid, _compactor(tmp_path))
	assert recall(tags[0]) == bodies[0]
	assert recall(tags[1]) == bodies[1]


def _one_tool_call(name: str, args: dict):
	return SimpleNamespace(
		content=[SimpleNamespace(type="tool_use", id=f"c{name}", name=name,
		                         input=args)],
		stop_reason="tool_use")


def test_整条路串起来(monkeypatch, tmp_path):
	"""一轮真跑:拿到号 → 调 compress 压掉那段 → 又调 recall 把原文取回来。

	各段各自的测试都绿、串起来不成立,是这种改动最典型的坏法:号在哪儿拼的、
	compress 改的是不是同一份 messages、recall 绑的是不是这个会话 —— 这三件
	只有一条真的调用链能同时验到。
	"""
	store = sessions.SessionStore(tmp_path / "server.db")
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	numbers, payloads = [], []
	history = [{"role": "user", "content": "问题"}]
	original = "那个长东西" * 20

	def record(kind, role, content):
		if kind != "tool_result":
			return None
		row = store.append_turn_message(turn["id"], len(numbers) + 2, kind, role,
		                                content)
		numbers.append(row)
		return row

	def fake_call_api(llm_client, emit, stream=True, purpose="main", **kwargs):
		payloads.append(kwargs["messages"])
		tag = f"m{numbers[0]:05d}" if numbers else ""
		if len(payloads) == 1:
			return _one_tool_call("echo", {})
		if len(payloads) == 2:
			return _one_tool_call("compress", {"from_id": tag, "to_id": tag,
			                                   "summary": "读过一段长东西"})
		if len(payloads) == 3:
			return _one_tool_call("recall", {"message_id": tag})
		return SimpleNamespace(content=[SimpleNamespace(type="text", text="完")],
		                       stop_reason="end_turn")

	monkeypatch.setattr(agent, "call_api", fake_call_api)
	echo = ToolDesc(name="echo", description="回一段字",
	                input_schema={"type": "object", "properties": {}},
	                handler=lambda: original)

	with bind_recall(make_recall(store, sid, _compactor(tmp_path))):
		agent.agent_loop(history, active_request="问题", system="s",
		                 tools=[echo, compress_tool, recall_tool],
		                 model="m", max_rounds=10, compactor=_Pass(),
		                 emit=lambda e: None, ask=lambda question: False,
		                 stream=False, record=record)

	tag = f"m{numbers[0]:05d}"
	assert tag in str(payloads[0]), "第一次请求里就该看得见号"

	bodies = _bodies(history)
	assert "已压缩" in bodies[0], bodies          # compress 的回话
	assert bodies[1].startswith(original), bodies  # recall 把原文还回来了(自己也被发了号)
	assert any("摘要" in str(m.get("content")) for m in history), "摘要那条还在"
	agent.agent_loop(history, active_request="问题", system="s",
	                 tools=[echo, compress_tool, recall_tool], model="m",
	                 max_rounds=10, compactor=_Pass(), emit=lambda e: None,
	                 ask=lambda question: False, stream=False, record=record)


# ---------------------------------------------------------------- 工具那一层

def test_没绑定取回器时如实说():
	"""没绑会话库时得说**没有库**,不是"查不到 m00007"。

	后者会把人支去查那个号;真相是这一轮压根没接上库(装配漏了,或者像子
	agent 那样没有库)。所以那句话里不该出现号。
	"""
	out = run_recall("m00007")
	assert "会话库" in out, out
	assert "m00007" not in out, out


def test_号写错了就说号写错了(store, tmp_path):
	"""格式不对在查库之前就拦下来 —— 不然它拿到的是一个查无此号的答复。"""
	with bind_recall(make_recall(store, "s", _compactor(tmp_path))):
		assert "m00007" in run_recall("7"), "得把正确写法给它"
		assert "m00007" in run_recall("")
		assert "m00007" in run_recall("m7")


def test_绑上之后按号取回(store, tmp_path):
	sid, row = _session_with_result(store, "那段原文")
	recall = make_recall(store, sid, _compactor(tmp_path))
	with bind_recall(recall):
		assert run_recall(f"m{row:05d}") == "那段原文"
		# 查不到时的话里要有那个号,不然模型不知道是哪一个没查着
		assert f"m{row + 1:05d}" in run_recall(f"m{row + 1:05d}")


def test_绑定只在这一段里有效(store, tmp_path):
	"""跟 bind_messages 同一条规矩:出了 with, handler 就够不着了。

	不恢复的话,下一次请求(可能是另一个会话的)会拿着上一个会话的取回器 ——
	按号查到别人的原文,而且不报错。
	"""
	sid, row = _session_with_result(store, "那段原文")
	with bind_recall(make_recall(store, sid, _compactor(tmp_path))):
		run_recall(f"m{row:05d}")
	assert "会话库" in run_recall(f"m{row:05d}")


# ---------------------------------------------------------------- 浏览器那一轮

class _Handler:
	"""只够把 _run_turn 跑起来:不建 socket、不走路由。"""

	_run_turn = server.Handler._run_turn

	def __init__(self):
		self.wrote = b""
		self.error = None

	def send_error(self, code, msg=None):
		self.error = (code, msg)

	def send_response(self, code): pass
	def send_header(self, name, value): pass
	def end_headers(self): pass
	def _cors(self): pass
	def write(self, data): self.wrote += data
	def flush(self): pass
	wfile = property(lambda self: self)


def test_浏览器那一轮_号是落库那一刻发出去的(monkeypatch, tmp_path):
	"""**整条路在浏览器里真的通** —— 这是唯一一条跨过那个接缝的测试。

	盯两件事,都只有在这条链上才碰得到:

	  一、循环拿到的 record **必须把行号返回出来**。装配层(server.make_recorder)
	     和 agent 循环之间那个接缝:吞掉返回值的话,号永远发不出去,而模型看不见
	     号就点不动任何一段 —— 这一整条功能是死的,而且不报错。
	  二、那一轮里 recall 手里绑着**本会话**的取回器。不绑的话它永远回"这个前端
	     没有会话库",而浏览器明明有。
	"""
	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(server, "STORE", store)
	sid = store.create_session("", "")["id"]
	seen = {}

	def fake_loop(messages, **kwargs):
		# 像 agent_loop 那样:落一条工具结果、拿返回值发号、再按号查回去
		row = kwargs["record"]("tool_result", "user",
		                       [{"type": "tool_result", "tool_use_id": "t1",
		                         "content": "那段原文"}])
		seen["row"] = row
		if isinstance(row, int):
			seen["back"] = run_recall(f"m{row:05d}")
			seen["stranger"] = run_recall("m99999")
		return TurnOutcome("completed", "完")

	monkeypatch.setattr(server, "agent_loop", fake_loop)
	handler = _Handler()
	handler._run_turn(sid, "干活")

	assert handler.error is None, handler.error
	assert isinstance(seen["row"], int), f"record 得把行号交出来:{seen}"
	assert seen["back"] == "那段原文", seen
	assert "查不到" in seen["stranger"], seen


# ---------------------------------------------------------------- 工具集

def test_子agent手里没有compress也没有recall(monkeypatch):
	"""子 agent 拿不到号(它的 record 是 `_drop`),这两个工具在它手里永远是死的 ——
	不如别给:一个点了没反应的工具有害无益。

	断言的是**真正递给它的那份工具集**(拦下 agent_loop 看它收到了什么),不是在
	这儿重抄一遍过滤名单 —— 重抄的话名单改了测试照样绿。
	"""
	given = {}

	def fake_agent_loop(messages, **kwargs):
		given.update(kwargs)
		return SimpleNamespace(text="", status="completed")

	monkeypatch.setattr(subagent, "agent_loop", fake_agent_loop)
	subagent.run_task("查一下")

	names = [tool.name for tool in given["tools"]]
	assert "compress" not in names, names
	assert "recall" not in names, names
