"""账本报表。把 .traces/usage.jsonl 变成几张能看的表。

用法:
    python report.py                  # 默认 config.USAGE_PATH
    python report.py 别的/usage.jsonl

六段,每一段回答一个具体问题:

    总览          花了多少 / 缓存命中没有 / 有没有算不出金额的
    按 purpose    **哪一块最贵** —— 这一张是"下一步优化什么"的直接输入
    按 agent      子 agent 值不值
    按会话        最大的几个会话
    重试的账      花掉了但没拿到结果的那些
    命中率曲线    逐轮看命中率,压缩那一轮标出来 —— 压缩有没有把缓存打掉

最后一段是这份报表存在的主要理由。压缩省下 token,但缓存是**前缀匹配**,
而压缩器的摘要每次措辞都不可能逐字节相同 —— 于是存在一个反直觉的可能:
**压缩把缓存打掉了**,省下的 token 不如失去的折扣值钱。这件事只看总成本
永远看不出来,必须逐轮看。
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from config import USAGE_PATH

# 计数器名单**从写的那一头借过来**,不在这儿再抄一份。
#
# 抄一份的代价:某天给账本加一个计数器(reasoning_tokens 之类),usage.py 改了、
# 这儿没改 —— 报表就少算一栏、总输入偏低。**而这不会报错**,只是数字变小了,
# 你看不出来。同一个定义写两遍、漂了不报错,是这个仓库反复在防的事。
from usage import COUNTERS


def load(path: Path) -> tuple[list[dict], int]:
	"""读账本。返回 (记录, 坏行数)。

	坏行不抛异常,只计数 —— 一行坏了不该让整份报表作废,但也不能咽掉:
	坏行的数量本身就是信息(多线程追加被撕开了?写的时候断电了?)。
	"""
	records, broken = [], 0
	for line in Path(path).read_text(encoding="utf-8").splitlines():
		if not line.strip():
			continue
		try:
			records.append(json.loads(line))
		except ValueError:
			broken += 1
	return records, broken


def _sum(records: list[dict]) -> dict:
	"""把一批记录加成一行。金额算不出的单独数出来,不混进总和。"""
	total = {name: 0 for name in COUNTERS}
	cost, priced, unpriced = 0.0, 0, 0
	for record in records:
		for name in COUNTERS:
			total[name] += record.get(name) or 0
		if record.get("cost_usd") is None:
			unpriced += 1
		else:
			cost += record["cost_usd"]
			priced += 1
	# **一条都算不出来的时候给 None,不是 0.0。** 给 0 的话报表会打印 "$0",
	# 而 $0 的意思是"确定不花钱",真实情况是"价目表没填,不知道"。
	# 这正是 usage.py 一路在防的那件事 —— 不能让它从报表这头漏回来。
	return {"calls": len(records), "cost": cost if priced else None,
	        "unpriced": unpriced, **total}


def hit_rate(row: dict) -> str:
	"""命中率 = 命中 / 输入总量。分母是三个输入计数器之和,不是 input_tokens。

	这是这份报表里最容易写错的一行:input_tokens 只是**未命中**的那部分,
	拿它当分母会算出一个恒等于 0% 的命中率 —— 而 0% 看起来像"缓存没生效",
	你会去查缓存,查的却是错的。
	"""
	miss = row["input_tokens"]
	hit = row["cache_read_input_tokens"]
	written = row["cache_creation_input_tokens"]
	denom = miss + hit + written
	return "—" if denom == 0 else f"{100 * hit / denom:.1f}%"


def is_main_loop(record: dict) -> bool:
	"""这一条算不算**主 agent 的主循环**那次调用。曲线只认这个。

	两个条件缺一不可,而且第二个是踩过的坑:

	  purpose == "main"
	      压缩那次是 "compaction",本来就分开。它拿的是完整上下文、另一个
	      system,混进曲线毫无意义。

	  agent == "main"
	      **子 agent 的 purpose 也是 "main"** —— 它复用 agent_loop,拿的是
	      默认值。但它有另一个上下文窗口、另一个 system,它的命中率跟主循环
	      的缓存行为没有任何关系。混进来会把主循环的数字往上拉,而这条曲线
	      的全部意义就是看主循环那个数字。

	漏掉第二个条件的表现:t3 那行显示 33.6%,而主循环自己那一次是 5.7%
	—— 差的正好是子 agent 的 4,000 个命中。不报错,只是这条曲线从此答不了
	它唯一要回答的问题。
	"""
	return record.get("purpose") == "main" and record.get("agent") == "main"


def _usd(value: float | None) -> str:
	"""渲染金额。三种"零"必须长得不一样:

	  None  不知道(价目表没填)   → "—"
	  0.0   确定不花钱            → "$0"
	  极小但非零                  → 科学计数,不能四舍五入成 "$0"

	第三条不是洁癖:把"花了一点"渲染成"$0",这一栏就失去了存在理由 ——
	而它上面两行的区别正是同一个道理。
	"""
	if value is None:
		return "—"
	if value == 0:
		return "$0"
	digits = 6 if value < 0.01 else 4
	text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
	return f"${text}" if text != "0" else f"${value:.2e}"


def _table(headers: tuple, rows: list[tuple]) -> str:
	if not rows:
		return "  (空)"
	cells = [[str(c) for c in row] for row in rows]
	widths = [max(len(str(headers[i])), *(len(r[i]) for r in cells))
	          for i in range(len(headers))]
	out = ["  " + "  ".join(str(headers[i]).ljust(widths[i])
	                        for i in range(len(headers)))]
	out.append("  " + "  ".join("-" * w for w in widths))
	for row in cells:
		out.append("  " + "  ".join(row[i].ljust(widths[i])
		                            for i in range(len(headers))))
	return "\n".join(out)


def _group(records: list[dict], key) -> list[tuple[str, dict]]:
	groups = defaultdict(list)
	for record in records:
		groups[key(record)].append(record)
	rows = [(name, _sum(items)) for name, items in groups.items()]
	return sorted(rows, key=lambda pair: (-pair[1]["calls"], pair[0]))


def main() -> None:
	path = Path(sys.argv[1]) if len(sys.argv) > 1 else USAGE_PATH
	if not path.exists():
		print(f"没有账本:{path}")
		print("跑一轮就有了 —— 终端 python main.py,或者浏览器 python server.py。")
		return

	records, broken = load(path)
	print(f"账本 {path}")
	print(f"{len(records)} 条记录" + (f",{broken} 条坏行" if broken else ""))
	if not records:
		return

	# ---- 总览 ----
	overall = _sum(records)
	total_in = (overall["input_tokens"] + overall["cache_read_input_tokens"]
	            + overall["cache_creation_input_tokens"])
	print("\n## 总览")
	print(f"  调用次数    {overall['calls']}")
	print(f"  输入未命中  {overall['input_tokens']:,}")
	print(f"  输入命中    {overall['cache_read_input_tokens']:,}")
	print(f"  输入写入    {overall['cache_creation_input_tokens']:,}")
	print(f"  输出        {overall['output_tokens']:,}")
	print(f"  上下文总量  {total_in:,}")
	print(f"  缓存命中率  {hit_rate(overall)}")
	print(f"  总成本      {_usd(overall['cost'])}"
	      + (f"   ({overall['unpriced']}/{overall['calls']} 条算不出金额)"
	         if overall["unpriced"] else ""))
	if overall["unpriced"]:
		print("              ↑ 价目表没填,见 pricing.py。它报 None 而不是 0,"
		      "所以这个数是**偏低**的,不是免费的。")
	if overall["cost"] is not None:
		print(f"  平均每次    {_usd(overall['cost'] / overall['calls'])}")

	# ---- 按 purpose ----
	print("\n## 按 purpose —— 哪一块最贵")
	grouped = _group(records, lambda r: r.get("purpose") or "?")
	rows = [(name, row["calls"], f"{row['input_tokens']:,}",
	         f"{row['cache_read_input_tokens']:,}", f"{row['output_tokens']:,}",
	         hit_rate(row), _usd(row["cost"])) for name, row in grouped]
	print(_table(("purpose", "calls", "miss", "hit", "out", "hit%", "cost"), rows))
	if any(name == "compaction" for name, _ in grouped):
		print("  compaction 那一行拿的是**完整上下文**,它常常是最大的一笔 ——")
		print("  拿它的成本跟它省下来的缓存折扣比,才知道压缩是赚还是赔。")
	else:
		print("  **没有 compaction 这一行** = 这段账里压缩一次都没触发。")
		print("  那本身是个结论:阈值太高,或者会话太短。")

	# ---- 按 agent ----
	print("\n## 按 agent")
	rows = []
	for name, row in _group(records, lambda r: r.get("agent") or "?"):
		rows.append((name, row["calls"], f"{row['input_tokens']:,}",
		             f"{row['cache_read_input_tokens']:,}", hit_rate(row),
		             _usd(row["cost"])))
	print(_table(("agent", "calls", "miss", "hit", "hit%", "cost"), rows))

	# ---- 按会话 ----
	print("\n## 按会话(前 10,按调用次数)")
	rows = []
	for name, row in _group(records, lambda r: r.get("session") or "(无归属)"):
		rows.append((name, row["calls"], f"{row['input_tokens']:,}",
		             f"{row['cache_read_input_tokens']:,}", hit_rate(row),
		             _usd(row["cost"])))
	for row in rows[:10]:
		print(_table(("session", "calls", "miss", "hit", "hit%", "cost"), [row]))

	# ---- 重试的账 ----
	failed = [r for r in records if not r.get("ok", True)]
	if failed:
		row = _sum(failed)
		print("\n## 重试花掉的钱")
		print(f"  {row['calls']} 次调用没有拿到结果,"
		      f"其中 input 未命中 {row['input_tokens']:,} token,"
		      f"成本 {_usd(row['cost'])}")
		print("  这些请求发出去了、被计费了,然后失败了。不单独记账的话,"
		      "它们和'没花过'在总账里长得一样。")
	else:
		print('\n## 重试花掉的钱\n  没有 —— 没有一次调用是"花掉但没拿到结果"的。')

	# ---- 命中率曲线 ----
	by_session = defaultdict(list)
	for record in records:
		# 只认主循环那一次。为什么子 agent 也必须滤掉,见 is_main_loop。
		if is_main_loop(record):
			by_session[record.get("session") or "(无归属)"].append(record)
	if by_session:
		biggest = max(by_session, key=lambda k: len(by_session[k]))
		print(f"\n## 命中率曲线 —— 会话 {biggest}"
		      f"(只算主循环:main purpose + main agent)")
		turns = defaultdict(list)
		order = []
		for record in by_session[biggest]:
			key = record.get("turn")
			if key not in turns:
				order.append(key)
			turns[key].append(record)
		# 这一轮里发生过什么。
		#
		# 命中率难看的时候必须分得清是哪种原因:压缩改了前缀(缓存真废了),
		# 还是只是重试多打了几次(那一轮账难看,但缓存没坏)。两种症状在
		# hit% 那一栏长得一模一样,处置却完全相反 —— 一个要改压缩,一个
		# 什么都不用改。不标出来,这张表就只能告诉你"有事发生",不能告诉你
		# 是什么事。
		session_records = [r for r in records
		                   if (r.get("session") or "(无归属)") == biggest]
		compacted = {r.get("turn") for r in session_records
		             if r.get("purpose") == "compaction"}
		retried = {r.get("turn") for r in session_records
		           if not r.get("ok", True)}
		rows = []
		for key in order:
			row = _sum(turns[key])
			marks = [name for name, seen in (("压缩", key in compacted),
			                                 ("重试", key in retried)) if seen]
			rows.append((key, row["calls"], f"{row['input_tokens']:,}",
			             f"{row['cache_read_input_tokens']:,}", hit_rate(row),
			             "← " + " + ".join(marks) if marks else ""))
		print(_table(("turn", "calls", "miss", "hit", "hit%", "发生了"), rows))
		print("  看什么:压缩那一轮的 hit% 如果掉一大截,而它省下的 token 又不多,"
		      "\n  那压缩的**净收益是负的** —— 省了 token,赔了缓存折扣。")


if __name__ == "__main__":
	main()
