"""报表。全部离线。

这里守的是三件"数字算错但不报错"的事 —— 报表是账本的唯一出口,它错了,
前面记得再对也没人看得见:

1. **曲线必须只认主循环。** 子 agent 的 purpose 也是 "main"(复用 agent_loop
   的默认值),但它是另一个上下文窗口,命中率跟主循环无关。混进来会把数字
   往上拉 —— 而这条曲线唯一要回答的就是"压缩有没有把主循环的缓存打掉"。
2. **`hit_rate` 的分母是三个输入计数器之和,不是 input_tokens。**
   input_tokens 只是未命中那部分,拿它当分母会得到一个恒等于 0% 的命中率,
   而 0% 看起来像"缓存没生效" —— 你会去查缓存,查的却是错的。
3. **一条都算不出金额时是 None,不是 0.0。** `$0` 的意思是"确定不花钱"。
"""

import json
import sys

import pytest

import report


def record(**over):
	base = dict(session="s1", turn="t1", agent="main", purpose="main",
	            ok=True, input_tokens=100, cache_read_input_tokens=900,
	            cache_creation_input_tokens=0, output_tokens=10, cost_usd=0.001)
	base.update(over)
	return base


# ------------------------------------------------------- 曲线只认主循环

def test_curve_excludes_subagent():
	"""**踩过的坑。** 子 agent 的 purpose 也是 "main"。

	不滤 agent 的话,t3 那行会显示 33.6%,而主循环自己那一次是 5.7% ——
	差的正好是子 agent 的 4,000 个命中。
	"""
	assert report.is_main_loop(record()) is True
	assert report.is_main_loop(record(agent="subagent")) is False


def test_curve_excludes_compaction():
	"""压缩那次拿的是完整上下文、另一个 system,混进曲线毫无意义。"""
	assert report.is_main_loop(record(purpose="compaction")) is False
	assert report.is_main_loop(record(purpose="vision")) is False


def test_curve_excludes_failed_calls_that_arent_main_loop():
	"""重试那次算主循环(钱是真花了、缓存行为是真的),但子 agent 的不算。"""
	assert report.is_main_loop(record(ok=False, kind="partial")) is True
	assert report.is_main_loop(record(ok=False, agent="subagent")) is False


# ------------------------------------------------------- 命中率的分母

def test_hit_rate_counts_all_three_input_counters():
	"""实测那条:命中率的分母必须是 input + cache_read + cache_creation。

	拿 input_tokens 当分母会算出 0% —— 看起来像"缓存没生效",而真相是
	input_tokens 只是未命中那部分。
	"""
	row = {"input_tokens": 249, "cache_read_input_tokens": 7808,
	       "cache_creation_input_tokens": 0}
	assert report.hit_rate(row) == "96.9%"

	# 若分母错写成 input_tokens,会得到 "3135.3%";错写成分子/input 亦然
	assert report.hit_rate(row) != "0.0%"


def test_hit_rate_is_dash_when_nothing_reported():
	row = {"input_tokens": 0, "cache_read_input_tokens": 0,
	       "cache_creation_input_tokens": 0}
	assert report.hit_rate(row) == "—"


# ------------------------------------------------------- 金额的 None

def test_cost_is_none_when_nothing_priced():
	"""**踩过的坑。** 第一版报表在这儿打了 "$0"。

	一条都算不出金额的时候,总成本是 None(渲染成 "—"),不是 0.0。
	$0 的意思是"确定不花钱",而真实情况是"价目表没填,不知道"。
	"""
	row = report._sum([record(cost_usd=None), record(cost_usd=None)])
	assert row["cost"] is None
	assert row["unpriced"] == 2
	assert report._usd(row["cost"]) == "—"


def test_cost_is_zero_only_when_genuinely_free():
	priced = report._sum([record(cost_usd=0.0)])
	assert priced["cost"] == 0.0
	assert priced["unpriced"] == 0
	assert report._usd(priced["cost"]) == "$0"


def test_unpriced_rows_do_not_contaminate_the_sum():
	"""算不出来的那些不并进总和,单独数出来 —— 不然总账悄悄偏低。"""
	row = report._sum([record(cost_usd=0.5), record(cost_usd=None)])
	assert row["cost"] == pytest.approx(0.5)
	assert row["unpriced"] == 1


def test_usd_distinguishes_three_kinds_of_zero():
	"""不知道 / 确定不花钱 / 极小但非零 —— 三者必须长得不一样。

	把"花了一点"渲染成 "$0",这一栏就失去了存在理由;而它上面那两行的区别
	正是同一个道理。
	"""
	assert report._usd(None) == "—"            # 价目表没填
	assert report._usd(0.0) == "$0"            # 确定不花钱
	assert report._usd(1e-9) == "$1.00e-09"    # 不能四舍五入成 $0


def test_usd_does_not_trail_zeros():
	"""单次调用的钱常常小于一分,所以小数位要够;但够了就别拖零。

	`f"{0.006:.6f}"` 是 "$0.006000"。4 位小数又反过来不够用:0.0009 会变成
	"$0.0009"(勉强),而 0.00009 就成了 "$0.0001" —— 一个比真实值大十倍、
	看起来还很合理的数。
	"""
	assert report._usd(0.006) == "$0.006"      # 不是 $0.006000
	assert report._usd(0.0185) == "$0.0185"
	assert report._usd(0.0009) == "$0.0009"
	assert report._usd(1.2345) == "$1.2345"


# ------------------------------------------------------- 端到端

def _write(tmp_path, records):
	path = tmp_path / "usage.jsonl"
	path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
	                          for r in records) + "\n", encoding="utf-8")
	return path


def test_main_end_to_end_marks_compaction_and_retry(tmp_path, monkeypatch, capsys):
	path = _write(tmp_path, [
		record(turn="t1"),
		record(turn="t2", input_tokens=900, cache_read_input_tokens=200),
		record(turn="t2", purpose="compaction", kind="nonstream",
		       input_tokens=40000, cache_read_input_tokens=0),
		# 主循环自己:5,000 未命中
		record(turn="t3", input_tokens=5000, cache_read_input_tokens=300),
		# 重试烧掉的:也是主循环
		record(turn="t3", kind="partial", ok=False, input_tokens=3000,
		       cache_read_input_tokens=0),
		# 子 agent:命中 4,000,必须被曲线剔掉
		record(turn="t3", agent="subagent", input_tokens=500,
		       cache_read_input_tokens=4000),
	])
	monkeypatch.setattr(sys, "argv", ["report.py", str(path)])
	monkeypatch.setattr(report, "USAGE_PATH", path)

	report.main()
	out = capsys.readouterr().out

	check = out.split("## 命中率曲线")[1]
	assert "← 压缩" in check
	assert "← 重试" in check
	# t3 = 主循环 5,000+300 加 重试 3,000+0 → miss 8,000 hit 300 = 3.6%
	t3 = next(line for line in check.splitlines()
	          if line.strip().startswith("t3"))
	assert "8,000" in t3 and "3.6%" in t3
	# 子 agent 的 4,000 不在曲线里 —— 这是那个 bug 的指纹
	assert "4,000" not in check
	assert "40,000" not in check
