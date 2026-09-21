"""压缩器的尺子。

`estimate_tokens` 是整条压缩阶梯的闸门(第 1 层的批次门槛由它派生,第 3、4 档
拿它做判断),它量出来的数直接决定"要不要把工具结果换成指针"。所以它量错了
不是慢一点的问题,是**该留的被砍掉**。

这里只守一件事:**thinking 块不计入**。理由是实测出来的,两条独立证据:

1. **输入侧不计费。** 同一个 messages,带 thinking 块和不带,端点报的
   prompt token 一模一样(带 vs 不带:6,193/6,193、109/109、90/90、86/86、
   174/174,纯问答和真 ReAct 轨迹都试过)。端点把回灌的 thinking 整个丢掉。
2. **模型也读不到。** 只在 thinking 里出现过的事实,下一轮它答不出来;同样
   的事实放进正文块或工具结果,立刻就答得出(正对照每次都过)。

所以它是**双重不存在**的:不进账、不进 prompt。拿它去撑字符预算,等于用
空气顶额度 —— 而第 5 轮那份上下文里它占了 52%(50,369 字符中 26,339 是
thinking),把尺子撑到线上,逼着压缩器去砍真正会被读到的工具结果。

**`fingerprint` 不跟着改,这是有意的。** 它还被 `prepare` 用来比"压前压后
是不是同一份"(决定要不要存检查点),那里的语义是"这坨东西变了没有",
跟"要发多大一坨"是两回事。顺手一起改掉的话,那个判断会静默地变味。
"""

import context

Compactor = context.ContextCompactor


def assistant(thinking_text: str, answer: str = "好的") -> dict:
	"""一条带 thinking 块的 assistant 消息。"""
	return {"role": "assistant", "content": [
		{"type": "thinking", "thinking": thinking_text, "signature": "sig-abc"},
		{"type": "text", "text": answer},
	]}


def test_estimate_tokens_ignores_thinking_text():
	"""thinking 写多长,尺子都不动 —— 它不进 prompt。

	改坏的样子:把 thinking 算进去,于是模型"想得越久",压缩器越以为上下文
	快满了,越早把工具结果换成指针。想得久反而看得少。
	"""
	short = [assistant("想一下。"), {"role": "user", "content": "继续"}]
	long = [assistant("想一下。" * 2000), {"role": "user", "content": "继续"}]
	assert Compactor.estimate_tokens(short) == Compactor.estimate_tokens(long)


def test_estimate_tokens_drops_the_whole_thinking_block():
	"""整块都不算,连结构也不算 —— 端点丢的是整块,不是只丢正文。

	只把 thinking 正文挖空、留下 {"type":"thinking","signature":...} 那点
	结构的话,25 个块仍然白占约 1,500 字符。数字不大,但它是同一个错误的
	小号版本:为一份读不到的东西留位置。
	"""
	with_block = [assistant("想了很久。" * 500)]
	without_block = [{"role": "assistant", "content": [{"type": "text", "text": "好的"}]}]
	assert Compactor.estimate_tokens(with_block) == Compactor.estimate_tokens(without_block)


def test_estimate_tokens_still_counts_everything_else():
	"""别的东西一个都不能少算 —— 正文、工具入参、工具结果还是会进 prompt 的。

	这条是防"改过头"的:如果为了实现上面两条而把整条 assistant 消息都跳过,
	尺子就瞎了,该压的时候不压,输入侧的钱直接翻倍。
	"""
	base = [{"role": "user", "content": "问题"}]
	bigger_text = [assistant("", answer="答" * 500), {"role": "user", "content": "问题"}]
	assert Compactor.estimate_tokens(bigger_text) > Compactor.estimate_tokens(base)

	bigger_tool = [{"role": "assistant", "content": [
		{"type": "tool_use", "id": "t1", "name": "bash",
		 "input": {"command": "x" * 500}}]}]
	assert Compactor.estimate_tokens(bigger_tool) > Compactor.estimate_tokens(base)

	bigger_result = [{"role": "user", "content": [
		{"type": "tool_result", "tool_use_id": "t1", "content": "y" * 500}]}]
	assert Compactor.estimate_tokens(bigger_result) > Compactor.estimate_tokens(base)


def test_fingerprint_still_sees_thinking():
	"""`fingerprint` 必须**照旧**对 thinking 敏感。

	它的另一个用户是 prepare 里那个"真压过了才存检查点"的判断 —— 比的是
	"这一坨变了没有"。跟着 estimate_tokens 一起把 thinking 剔掉的话,将来
	任何只动了 thinking 的改写都不会被认出来,检查点静默地少存,不报错。
	"""
	a = [assistant("原来的想法")]
	b = [assistant("换了个想法")]
	assert Compactor.fingerprint(a) != Compactor.fingerprint(b)


# ------------------------------------------------- 尺子的单位:按类算 token

def test_token_estimate_costs_cjk_four_times_ascii():
	"""同一字数下,CJK 的估算约是 ASCII 的 4 倍。

	CJK 1 字符/token、ASCII 4 字符/token,这是端点实测出来的:单个汉字
	1.000 token/字符,ASCII 散文 5.0 字符/token、代码 2.7、高熵串 1.07。

	按类分开算比一个统一的"字符数"准得多 —— 第 5 轮那份上下文里一半以上
	是中文注释,统一按字符量会把它的 token 估错一倍以上。
	"""
	def est(text):
		return Compactor.estimate_tokens([{"role": "user", "content": text}])

	cjk, ascii_ = est("中" * 1000), est("a" * 1000)
	assert 3.5 < cjk / ascii_ < 4.5, f"CJK/ASCII = {cjk / ascii_:.2f},该接近 4"


# ------------------------------------------------- 保留策略:一件活装得下

def make_compactor(tmp_path):
	"""一个不联网的压缩器。下面几条只调不碰模型的那几档,所以 client 给 None。"""
	return context.ContextCompactor(None, "m", tmp_path, tmp_path,
	                                lambda event: None)


def tool_exchange(index: int, content: str) -> list:
	"""一个完整回合:assistant 发 tool_use,user 回 tool_result。"""
	return [
		{"role": "assistant", "content": [
			{"type": "tool_use", "id": f"t{index}", "name": "bash",
			 "input": {"command": f"cmd {index}"}}]},
		{"role": "user", "content": [
			{"type": "tool_result", "tool_use_id": f"t{index}", "content": content}]},
	]


def conversation(rounds: int, result: str = "x" * 300) -> list:
	"""一条长轨迹。结尾故意停在 assistant 上 —— 这样所有 tool_result 都是
	"模型已经见过的",micro 才会去动它们(它不碰还没发出去的那批)。"""
	msgs = [{"role": "user", "content": "任务"}]
	for i in range(rounds):
		msgs += tool_exchange(i, result)
	msgs.append({"role": "assistant", "content": [
		{"type": "tool_use", "id": "last", "name": "bash", "input": {"command": "c"}}]})
	return msgs


def result_contents(msgs: list) -> list[str]:
	return [str(b.get("content", "")) for m in msgs
	        if isinstance(m.get("content"), list)
	        for b in m["content"] if b.get("type") == "tool_result"]


def test_a_whole_task_is_not_archived(tmp_path):
	"""一件 118 条消息的活(跟第 5 轮同量级)不该被 snip 动。

	第 5 轮就是被它搞死的:snip 每轮都触发、每轮归档 3 条,48 次下来模型
	只剩一个往前滑的窗口 —— 于是 index.html 被读了 20 遍。
	"""
	msgs = conversation(58)
	assert len(msgs) == 118
	c = make_compactor(tmp_path)
	assert c.snip_compact(msgs) is msgs, "原样返回 = 没动"


def test_recent_results_survive_micro_compact(tmp_path):
	"""最近 30 个工具结果的内容不许动。

	KEEP_RECENT_RESULTS=3 时模型永远只看得到最近 3 个结果的内容 —— 那正是
	它把 index.html 读了 20 遍的直接原因。实测第 5 轮 24 个结果里 20 个
	成了 134 字的指针。
	"""
	msgs = conversation(31, result="y" * 300)
	make_compactor(tmp_path).micro_compact(msgs)
	got = result_contents(msgs)
	assert len(got) == 31
	assert "saved at" in got[0], "第 1 个该换成指针"
	assert got[1] == "y" * 300, "第 2 个离尾巴还差 30 个位子,不该动"
	assert got[-1] == "y" * 300


def test_batch_budget_measures_the_same_unit_as_its_limit(tmp_path):
	"""第 1 层的量法必须跟它的限额同单位。

	换单位时最容易漏的一处:limit 变成 token 了,里面还在
	sum(len(str(block["content"]))) 数**字符**。那第 1 层要单批超几万 token
	才落盘 —— 静默失效,正是这几轮一路在抓的那类 bug。

	取证法:ASCII 是 4 字符/token,4 万字符 ≈ 1.1 万 token。给 2 万的限额:
	按 token 量 -> 不落盘;按字符量 -> 落盘。
	"""
	c = make_compactor(tmp_path)
	ascii_msgs = [{"role": "user", "content": [
		{"type": "tool_result", "tool_use_id": "t1", "content": "a" * 40000}]}]
	c.tool_result_budget(ascii_msgs, max_tokens=20000)
	assert "<persisted-output>" not in result_contents(ascii_msgs)[0], \
		"4 万 ASCII 字符只有约 1.1 万 token,不该落盘 —— 还在按字符量"

	# 反方向:CJK 是 1 字符/token,同样 4 万字符约 4.4 万 token,该落盘
	cjk_msgs = [{"role": "user", "content": [
		{"type": "tool_result", "tool_use_id": "t2", "content": "中" * 40000}]}]
	c.tool_result_budget(cjk_msgs, max_tokens=20000)
	assert "<persisted-output>" in result_contents(cjk_msgs)[0]


def test_fewer_results_than_the_keep_window_are_all_kept(tmp_path):
	"""结果数比 KEEP_RECENT_RESULTS 还少时,一条都不该换成指针。

	切片写成 consumed[: len(consumed) - KEEP] 时,这个差是**负数** ——
	Python 把 consumed[:-5] 读成"去掉最后 5 个",于是它换掉的是**最新**的
	那几个、留下最旧的,跟这个常量想干的事正好相反。不报错,只是模型眼前
	只剩一堆指向旧文件的指针。

	KEEP_RECENT_RESULTS 从 3 提到 30 之后这条从"几乎撞不上"变成"很容易撞上":
	一件用 25 个结果的活(第 4 轮就是)正好落在这个区间里。
	"""
	msgs = conversation(25, result="z" * 300)
	make_compactor(tmp_path).micro_compact(msgs)
	got = result_contents(msgs)
	assert len(got) == 25
	assert all(c == "z" * 300 for c in got), \
		f"{sum(c != 'z' * 300 for c in got)} 个被换成了指针"
