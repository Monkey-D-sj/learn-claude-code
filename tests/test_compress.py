"""模型自己压:号怎么拼、号段怎么切、切错了怎么办。

两条路分开测:
  拼号      context.stamp_tag —— 拼在正文尾巴上,存库读回还认得
  切号段    context.compress_range —— 切错了**什么都不动**并回一句话

工具那一层(tools/compress.py)只做"把当前的 messages 找出来、把结果转成一句话",
所以真正要钉住的判断都在这儿。

**号从哪儿来不归这儿管。** 现在是库里那一行的行号(agent.py 拿 record 的返回
值拼上去,见 tests/test_recall.py);这里的 `stamp` 只是个凑号的替身,给号段
那些用例当 setup 用。

**号 = 一次工具调用,拼在它那条结果的末尾。** assistant 那边不挂号:那种消息
经常一个字正文都没有,而且带 tool_use 时必须以 tool_use 收尾,号拼不上去
(拼上去端点直接 400)。
"""

import json

import context


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


# ---------------------------------------------------------------- 发号

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

	**真号是库里那一行的行号**:agent.py 拿 record 的返回值拼上去,见
	tests/test_recall.py 和 sessions.find_message。号段这一侧的测试不关心
	号从哪儿来,只关心"正文尾巴上有个不重样的号、两端的吸附按它算",所以
	这儿拿序号凑一个就够。

	**不复刻"水位只增不减"。** 那套逻辑原来住在 context.tag_ids 里,是为了
	在没有库 id 的时候模拟"号不许回退"—— 现在由 turn_messages.id 负责,
	测试落在 test_sessions.py(行号只增不减)和 test_recall.py(每个结果各得
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
