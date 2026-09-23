"""按号管上下文的那一对:compress 压掉一段,recall 把它取回来。

一个模块(tools/compress.py),所以一个测试文件。三段:

  拼号      context.stamp_tag —— 拼在正文尾巴上,存库读回还认得
  切号段    context.compress_range —— 切错了**什么都不动**并回一句话
  取回来    make_recall / run_recall —— 按号把被压掉的原文换回来

工具那一层只做"把当前的 messages / 库找出来、把结果转成一句话",所以真正要钉住
的判断都在 context 和 sessions 里。

**号从哪儿来不归前两段管。** 现在是库里那一行的行号(agent.py 拿 record 的返回
值拼上去,见下面"发号"那一节);号段那些用例里的 `stamp` 只是个凑号的替身,
给 setup 用。

**号 = 一次工具调用,拼在它那条结果的末尾。** assistant 那边不挂号:那种消息
经常一个字正文都没有,而且带 tool_use 时必须以 tool_use 收尾,号拼不上去
(拼上去端点直接 400)。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent
import app
import context
import server
import sessions
import tools
import tools.subagent as subagent
from agent import TurnOutcome
from tools.base import ToolDesc
from tools.compress import bind_recall, make_recall, run_recall
from tools.compress import compress as compress_tool
from tools.compress import recall as recall_tool
from tools.todo import TodoManager


def msg(role: str, text: str) -> dict:
	return {"role": role, "content": text}


def tool_round(index: int, result: str = "输出") -> list:
	"""一个完整回合:assistant 发 tool_use,user 回 tool_result。"""
	return [
		{"role": "assistant", "content": [
			{"type": "tool_use", "id": f"t{index}", "name": "bash",
			 "input": {"command": f"cmd {index}"}}]},
		{"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": f"t{index}", "content": result}]},
	]


def tags(messages: list) -> list[str]:
	"""把每条消息里结果身上的号揪出来,给断言用。"""
	return [context.message_tags(m) for m in messages]


def result_of(message) -> dict:
	return message["content"][0]


# ---------------------------------------------------------------- 拼号

def test_stamp_tag_拼在正文尾巴上_存库读回还认得():
	"""号拼在结果正文的尾巴上,不是挂在消息外头。

	它得活过三件事:存进 turn_messages 的 content_json、读回来、被压缩改写。
	只有正文活得过这三样 —— 挂在外面的字段过一遍 json.dumps 就没了。

	(号本身从哪儿来不归它管:现在是库里那一行的行号,见 sessions.find_message。
	这个函数只管"把事情办成正文尾巴上那一段"。)
	"""
	content = context.stamp_tag("hi", "m00027")
	assert content.startswith("hi"), "工具输出还是开头,一个字节没动"
	assert "<message-id token=" in content and content.endswith(">m00027</message-id>")

	reloaded = json.loads(json.dumps(
		[{"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": "t1", "content": content}]}]))
	assert context.result_tag(reloaded[0]["content"][0]) == "m00027", "存库读回还认得"


def test_stamp_tag_带上这条结果多大():
	"""号上带着这条结果多大 —— 模型拿它判断"值不值得压"。

	token= 只是它那次工具调用的结果正文,不是整条消息、也不是整段上下文。
	"""
	small = context.stamp_tag("x" * 40, "m00001")
	big = context.stamp_tag("x" * 4000, "m00002")
	small_n = int(context._MARKER_RE.search(small).group(1))
	big_n = int(context._MARKER_RE.search(big).group(1))
	assert big_n > small_n * 10, f"4 千字符该比 40 字符大一个量级:{big_n} vs {small_n}"


def stamp(msgs: list) -> None:
	"""给还没号的结果补一个号 —— 测试里代替 agent 循环那一步。

	**真号是库里那一行的行号**:agent.py 拿 record 的返回值拼上去,见下面
	"发号"那一节和 sessions.find_message。号段这一侧的测试不关心号从哪儿来,
	只关心"正文尾巴上有个不重样的号、两端的吸附按它算",所以这儿拿序号凑一个
	就够。

	**不复刻"水位只增不减"。** 那套逻辑原来住在 context.tag_ids 里,是为了
	在没有库 id 的时候模拟"号不许回退"—— 现在由 turn_messages.id 负责,
	测试落在 test_sessions.py(行号只增不减)和下面的发号那两条(每个结果各得
	一个号)。这儿从 1 数到底,只在这份测试数据里成立。
	"""
	n = 0
	for message in msgs:
		content = message.get("content")
		if not isinstance(content, list):
			continue
		for block in content:
			if isinstance(block, dict) and block.get("type") == "tool_result" \
			        and context.result_tag(block) is None:
				n += 1
				block["content"] = context.stamp_tag(
					str(block.get("content", "")), f"m{n:05d}")


# ---------------------------------------------------------------- 切号段

def test_compress_range_swaps_the_whole_round_for_one_summary():
	"""切对了:整段换成一条 user 消息,位置就在它原来待的地方,段外一个不动。"""
	msgs = [msg("user", "问题")]
	for i in range(1, 4):
		msgs += tool_round(i)
	stamp(msgs)
	# 1 条 user + 3 轮 × 2 条 = 7 条
	assert len(msgs) == 7

	out = context.compress_range(msgs, "m00001", "m00002", "前三步是查环境")
	assert "已压缩" in out
	# 7 条里端走 2 轮(4 条:调用+结果各两条),换成 1 条摘要 → 4 条
	assert len(msgs) == 4, f"实际 {len(msgs)}:{msgs}"
	assert msgs[1]["role"] == "user" and "前三步是查环境" in msgs[1]["content"]
	assert msgs[0]["content"].startswith("问题"), "用户那条指令一个字不许动"
	assert tags(msgs)[-1] == ["m00003"], "段外的号一个都不许动"


def test_the_report_carries_the_summary_so_a_human_can_read_it():
	"""回执里带上摘要正文 —— 不带的话,这次调用唯一有内容的产物就没人看得见。

	前端把工具结果收成一个折叠块:折叠时只露头两行非空行,展开才是全文。回执
	原来只有一句"已压缩…",于是页面上看不到摘要本身 —— 而摘要是这次调用唯一
	的产物。模型并不缺它(号段那条消息里已经写进去了),这一份是给**人**看的。

	所以两头都钉:开头还是那句"已压缩",结尾是摘要正文,中间隔一个空行 ——
	空行被前端滤掉,于是摘要的头一行正好露在折叠的那两行里。
	"""
	msgs = [msg("user", "问题")] + tool_round(1) + tool_round(2)
	stamp(msgs)

	out = context.compress_range(msgs, "m00001", "m00002", "两步查完了,结论是环境没问题")
	assert out.startswith("已压缩"), f"开头还是那句话:{out!r}"
	assert out.endswith("摘要:两步查完了,结论是环境没问题"), f"回执里没有摘要正文:{out!r}"
	assert "\n\n摘要:" in out, "摘要另起一段 —— 挤在同一行就露不进折叠那两行"
	# 正文只多了回执这一份:号段那条消息里的摘要还在,没被顶掉、也没被搬家
	assert msgs[1]["content"] == (
		"[m00001-m00002] Summary (reference only):两步查完了,结论是环境没问题")
	assert len(msgs) == 2


def test_摘要标记三处一致():
	"""那个标记名必须三处一致 —— 漂了不报错,只表现为模型开始跟着摘要里的字走。

	app.py 的 SYSTEM 只认标记、不认标签名(凡标了 (reference only) 的算资料),
	而写出这个标记的有两条路:第 4 档的 summary_message() 和号段压缩
	compress_range()。哪一处改了名字,模型手里那句话就落空 —— 它会把摘要
	(里面混着工具输出的原文)当成又要它干的活。没有任何东西会报错,所以只能
	在这儿把它们栓一起。
	"""
	marked = "(reference only)"

	assert marked in app._SYSTEM_FROZEN, "SYSTEM 得按标记说话,不然模型不知道哪个是资料"

	label, request, text = "Compacted", "问题", "干完了"
	fourth = context.ContextCompactor.summary_message(label, request, text, Path("t.md"))
	assert marked in fourth["content"], "第 4 档那条没标,标记就白写了"

	msgs = [msg("user", "问题")] + tool_round(1)
	stamp(msgs)
	context.compress_range(msgs, "m00001", "m00001", text)
	assert marked in msgs[1]["content"], f"号段那条没标:{msgs[1]['content']!r}"


def test_a_range_that_starts_mid_round_drags_its_call_in():
	"""号段从一轮中间开始:往前吸附到那次调用 —— 不吸附就是 400。

	模型点的是结果,而调用在它上一条消息里。只端走结果的话,那条 assistant 消息
	就剩一个没有结果的 tool_use,端点回的是 "tool_use ids were found without
	tool_result blocks immediately after" —— 而那个 400 会被收成"这一轮失败",
	模型连这句话都看不到。
	"""
	msgs = [msg("user", "问题")] + tool_round(1) + tool_round(2)
	stamp(msgs)

	out = context.compress_range(msgs, "m00001", "m00002", "两轮都压掉")
	assert "已压缩" in out
	assert len(msgs) == 2, f"两条调用连同它们的工具调用该一起走,实际剩 {len(msgs)}"
	# 剩下的:用户那条 + 摘要。配对检查在 compress_range 里跑过,这里再确认一次
	assert not any(b.get("type") == "tool_use" for b in msgs[1]["content"]
	               if isinstance(msgs[1]["content"], list))


def test_a_round_with_two_calls_is_taken_as_a_whole():
	"""一轮里两次调用,只点名其中一条结果 —— 整轮一起端走,并如实说一声。

	同一轮的两次调用分不开:它们共用一条 assistant 消息和一条结果消息。只圈一个
	的话,另一个的 tool_use 就悬空了。
	"""
	both = [
		{"role": "assistant", "content": [
			{"type": "tool_use", "id": "a", "name": "bash", "input": {}},
			{"type": "tool_use", "id": "b", "name": "bash", "input": {}}]},
		{"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": "a", "content": "第一条"},
			{"type": "tool_result", "tool_use_id": "b", "content": "第二条"}]},
	]
	msgs = [msg("user", "问题"), *both]
	stamp(msgs)
	assert tags(msgs) == [[], [], ["m00001", "m00002"]]

	out = context.compress_range(msgs, "m00001", "m00001", "只压第一条")
	assert "已压缩" in out
	assert "1 条结果跟着一起圈进来" in out, f"端走了不说一声,模型就不知道第二条哪去了:{out!r}"
	assert len(msgs) == 2


def test_compress_range_refuses_ids_that_are_not_there():
	"""号不在上下文里(压过了、或者压根没发过):回话,不动。"""
	msgs = [msg("user", "问题")] + tool_round(1)
	stamp(msgs)

	out = context.compress_range(msgs, "m00040", "m00042", "不存在的号")
	assert "切得不对" in out
	assert len(msgs) == 3


def test_compress_range_refuses_backwards_and_malformed_ranges():
	"""from 比 to 大、号写错了、摘要空着 —— 都是"切得不对",不是崩溃。

	模型偶尔会把两个号写反(它自己记的那份和上下文里的对不上时),这不是异常,
	是它需要听一句人话。每一次都该什么都不动。
	"""
	msgs = [msg("user", "问题")] + tool_round(1) + tool_round(2)
	stamp(msgs)
	before = len(msgs)

	for start, end, why in (("m00002", "m00001", "反的"),
	                        ("m1", "m00002", "号写错了"),
	                        ("m00001", "m00002", "摘要是空的")):
		out = context.compress_range(msgs, start, end, "" if "空" in why else "摘要")
		assert "切得不对" in out, f"{why} 该回一句拒绝:{out!r}"
		assert len(msgs) == before, f"{why} 的时候动了上下文"


def test_pairing_check_understands_sdk_blocks_not_just_dicts():
	"""配对检查必须认 SDK 的 pydantic 块,不能只认 dict。

	agent_loop 往 messages 里塞的 assistant 内容是 response.content —— 那是 SDK
	对象;而 tool_result 那条是 agent.py 自己拼的 dict。**同一条上下文里两副
	形状**(context._block_type 那段注释写的就是这件事)。

	真跑一轮才发现:只认 dict 的版本把 assistant 那边的 tool_use 全跳过了,于是
	每条结果都成了孤儿 —— 模型号段点得完全正确,工具却回它"你切的不对"。
	"""
	from anthropic.types import ToolUseBlock

	msgs = [
		msg("user", "问题"),
		{"role": "assistant", "content": [
			ToolUseBlock(type="tool_use", id="t1", name="bash", input={"command": "cmd"})]},
		{"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": "t1", "content": "输出"}]},
	]
	stamp(msgs)
	out = context.compress_range(msgs, "m00001", "m00001", "查了一下环境")
	assert "已压缩" in out, f"两副形状的配对该认出来,实际:{out!r}"
	assert len(msgs) == 2


# ================================================================ 取回来
#
# 守五件事,每一件坏掉都不报错:
#
#   一、**号来自落库,不是自己数的。** 写不进去就没有行号,上层也就不发号 ——
#      "有号 = 查得回来"这条不变量靠它撑着。子 agent 不接 record,所以它没有号。
#
#   二、**查回来只在同一个会话里。** 行号是全库一张表的,A 会话拿着自己上下文
#      里的一个号不该读到 B 会话的原文。
#
#   三、**取回的内容要过预览。** 被压掉的往往就是大的,原样回灌等于把压缩白做,
#      而且下一轮第 1 档又会把它落盘一遍。
#
#   四、**没接上库时要如实说。** 没绑取回器时这个工具查什么都是空 —— 说一句
#      "现在没接会话库"比回一句"查不到"强,后者会把人支去查号,而那个号没准
#      是对的(装配漏了,不是号错了)。
#
#   五、**子 agent 没有 compress。** 它发不出号,那两个工具在它手里永远是死的。


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

	def record(kind, role, content, tool_use_id=None):
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
	history = _run(monkeypatch, lambda kind, role, content, tool_use_id=None: None, calls=1)
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

	def record(kind, role, content, tool_use_id=None):
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
	assert any("(reference only)" in str(m.get("content")) for m in history), "摘要那条还在"
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
	"""跟 bind_messages 同一条规矩:出了 with,handler 就够不着了。

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
	_drive = server.Handler._drive
	_checkpoint = server.Handler._checkpoint

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
