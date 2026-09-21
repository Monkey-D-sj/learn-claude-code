"""报表。全部离线。

报表是账本的唯一出口 —— 它错了,前面记得再对也没人看得见。这里只守报表**自己**
的那两件事:

1. **命中率曲线只认主循环。** 见 is_main_loop —— 这是踩过的坑。
2. 端到端跑一遍:压缩和重试在曲线上分得开,子 agent 不混进来。

加总、命中率的分母、金额怎么渲染 —— 那些连同实现都在 usage.py,测试也在
test_usage.py。report.py 从 usage 那头 import,不抄第二份。
"""

import json
import sys

import report


def record(**over):
	base = dict(session="s1", turn="t1", agent="main", purpose="main",
	            ok=True, input_tokens=100, cache_read_input_tokens=900,
	            cache_creation_input_tokens=0, output_tokens=10,
	            elapsed_ms=100, cost_usd=0.001)
	base.update(over)
	return base


# ------------------------------------------------------- 曲线只认主循环

def test_curve_excludes_subagent():
	"""**踩过的坑。** 子 agent 的 purpose 也是 "main"。

	不滤 agent 的话,t3 那行会显示 33.6%,而主循环自己那一次是 5.7% ——
	差的正好是子 agent 的 4,000 个命中。不报错,只是这条曲线从此答不了它
	唯一要回答的问题。
	"""
	assert report.is_main_loop(record()) is True
	assert report.is_main_loop(record(agent="subagent")) is False


def test_curve_excludes_compaction():
	"""压缩那次拿的是完整上下文、另一个 system,混进曲线毫无意义。"""
	assert report.is_main_loop(record(purpose="compaction")) is False
	assert report.is_main_loop(record(purpose="vision")) is False


def test_failed_attempts_still_count_as_main_loop():
	"""重试那次算主循环,子 agent 的不算。

	重试那笔钱是真花了、那次请求的缓存行为也是真的,所以它该进曲线(标成
	"← 重试");子 agent 是另一个上下文窗口,不该进。
	"""
	assert report.is_main_loop(record(ok=False, kind="partial")) is True
	assert report.is_main_loop(record(ok=False, agent="subagent")) is False


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

	curve = out.split("## 命中率曲线")[1]
	# 两种症状都是 hit% 掉一截,但处置相反,所以必须分得开
	assert "← 压缩" in curve
	assert "← 重试" in curve
	# t3 = 主循环(5,000+300)+ 重试(3,000+0)→ miss 8,000 / hit 300 = 3.6%
	t3 = next(line for line in curve.splitlines()
	          if line.strip().startswith("t3"))
	assert "8,000" in t3 and "3.6%" in t3
	# 子 agent 的 4,000 不在曲线里 —— 这是那个 bug 的指纹
	assert "4,000" not in curve
	# 压缩那 40,000 也不在(它走"按 purpose"那一段)
	assert "40,000" not in curve


def test_main_reports_unpriced_instead_of_zero(tmp_path, monkeypatch, capsys):
	"""价目表没填时总成本是 "—",不是 "$0"。

	"$0" 的意思是"确定不花钱"。这条从 usage.py 一路守到报表,别从报表这头漏。
	"""
	path = _write(tmp_path, [record(cost_usd=None)])
	monkeypatch.setattr(sys, "argv", ["report.py", str(path)])
	monkeypatch.setattr(report, "USAGE_PATH", path)

	report.main()
	out = capsys.readouterr().out
	assert "总成本      —" in out
	assert "价目表没填" in out
