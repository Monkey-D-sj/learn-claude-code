"""压缩器的尺子。

`estimate_tokens` 是整条压缩阶梯的闸门(第 1 层的批次门槛由它派生,第 3、4 档
拿它做判断),它量出来的数直接决定"要不要把工具结果换成指针"。所以它量错了
不是慢一点的问题,是**该留的被砍掉**。

这里只守"没量错"和"没改过头"两件事。口径和实测的数记在
`notes/context.md` 的「尺子」一节 —— 那是笔记该干的活,不在这儿再抄一份。

**`fingerprint` 不跟着改,这是有意的。** 它还被 `prepare` 用来比"压前压后
是不是同一份"(决定要不要存检查点),那里的语义是"这坨东西变了没有",
跟"要发多大一坨"是两回事。顺手一起改掉的话,那个判断会静默地变味。
"""

import pytest

import context

Compactor = context.ContextCompactor


def assistant(thinking_text: str, answer: str = "好的") -> dict:
	"""一条带 thinking 块的 assistant 消息。"""
	return {"role": "assistant", "content": [
		{"type": "thinking", "thinking": thinking_text, "signature": "sig-abc"},
		{"type": "text", "text": answer},
	]}


def test_estimate_tokens_still_counts_everything_else():
	"""别的东西一个都不能少算 —— 正文、工具入参、工具结果都是要进 prompt 的。

	这条是防"改过头"的:如果哪天为了少算某类块,把整条 assistant 消息跳过,
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


# ------------------------------------------- 第 2 档的总闸门:按 token,不按条数

def test_the_gate_sits_inside_the_budget():
	"""闸门必须落在预算**以内**。

	等于 1.0 的话,等它响的时候这一轮已经按满价发出去了 —— 而提前一档
	动手正是它存在的理由。
	"""
	assert 0 < Compactor.SNIP_TRIGGER_RATIO < 1
	assert Compactor.SNIP_TRIGGER_TOKENS == int(
		Compactor.CONTEXT_TOKEN_BUDGET * Compactor.SNIP_TRIGGER_RATIO)


def test_snip_stays_out_when_only_the_count_is_high(tmp_path):
	"""**这条是这道闸门的全部理由。** 条数早就过线、token 没到 —— 不许切。

	150 条小结果可能只有几万 token,离预算差一个数量级,却照样把中段切掉;
	而切中段就是改前缀,改了前缀这一轮全部按未命中重算(实测冷 ¥0.005546
	对热 ¥0.000528)。拿一个跟成本无关的量决定动不动缓存,注定错。
	"""
	msgs = conversation(100, result="x" * 2000)      # 202 条,远过 150
	c = make_compactor(tmp_path)
	assert len(msgs) > c.SNIP_MAX_MESSAGES, "前提:条数这道闸门早就过了"
	assert c.estimate_tokens(msgs) < c.SNIP_TRIGGER_TOKENS, "前提:token 没到"

	out = c.prepare(msgs, "任务")

	assert out is msgs, "原样返回 = 一档都没走"
	assert len(out) == 202, "一条都不许切"


@pytest.mark.skip(reason=(
	"第 2 档当前是关着的(见 context.prepare 里那一行「先关掉(2026-09-22,临时的)」),"
	"而这条断言的是它开着时的行为 —— 它和上面那条 "
	"test_snip_stays_out_when_only_the_count_is_high 是一对,合起来编码的才是"
	"「闸门按 token 说话」这个意思。等 compress 那条路的用法定下来、四档按那时"
	"的注释补好 _split_marker 接回去,就该把这个 skip 去掉。"))
def test_snip_runs_once_the_context_is_heavy_enough(tmp_path):
	"""同一个条数,token 上去了就该切 —— 闸门量的是 token。

	刻意停在这条带里:过了 75%、还没到 100%。再往上第 3、4 档要接手,
	而第 4 档调模型 —— 这儿的压缩器 client 是 None,走上去就炸。
	"""
	msgs = conversation(100, result="x" * 8500)      # 239,614 token
	c = make_compactor(tmp_path)
	tokens = c.estimate_tokens(msgs)
	assert c.SNIP_TRIGGER_TOKENS < tokens <= c.CONTEXT_TOKEN_BUDGET, (
		f"{tokens:,} 该落在 ({c.SNIP_TRIGGER_TOKENS:,}, "
		f"{c.CONTEXT_TOKEN_BUDGET:,}] 里 —— 常量改了就把这个夹具跟着改")

	out = c.prepare(msgs, "任务")

	assert out is not msgs, "构造了新列表 = snip 动过了"
	assert len(out) < len(msgs)
	assert any(c.is_archive_marker(m) for m in out), "该留下一条归档标记"


def test_the_first_layer_can_never_fire_below_the_gate():
	"""第 1 层的门槛是**整份预算**,闸门是预算的 75% —— 它响的时候闸门必然
	早就开了。

	所以"第 1 层不并进闸门"是个空操作,不是漏掉:一批要 30 万 token 才算
	大,而那意味着整段上下文至少也是 30 万,早过了 22.5 万。两层各自独立,
	写在一起只是因为它们本来就是这个关系。

	钉住它:哪天有人把 TOOL_RESULT_BATCH_TOKEN_BUDGET 调到闸门以下,第 1
	层就会开始出现在短上下文里 —— 那是新行为,该有人知道。
	"""
	assert (Compactor.TOOL_RESULT_BATCH_TOKEN_BUDGET
	        >= Compactor.SNIP_TRIGGER_TOKENS)


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
