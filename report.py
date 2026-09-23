"""账本报表。把 .traces/usage.jsonl 变成几张能看的表。

用法:
    python report.py                  # 默认 config.USAGE_PATH
    python report.py 别的/usage.jsonl

八段,每一段回答一个具体问题:

    总览          花了多少 / 缓存命中没有 / 有没有算不出金额的
    按 purpose    **哪一块最贵** —— 这一张是"下一步优化什么"的直接输入
    按 agent      子 agent 值不值
    按会话        最大的几个会话
    按时段        空闲还是高峰 —— **差整整一倍**,挪时段是最直接的省钱
    重试的账      花掉了但没拿到结果的那些
    命中率曲线    逐轮看命中率,压缩那一轮标出来 —— 压缩有没有把缓存打掉
    账本自检      tier 和 ts 对不对得上(不一致 = 有人动过账本或窗口)

命中率曲线那一张是这份报表存在的主要理由。压缩省下 token,但缓存是**前缀匹配**,
而压缩器的摘要每次措辞都不可能逐字节相同 —— 于是存在一个反直觉的可能:
**压缩把缓存打掉了**,省下的 token 不如失去的折扣值钱。这件事只看总成本永远
看不出来,必须逐轮看。
"""

import codecs
import json
import sys
from collections import defaultdict
from pathlib import Path

from config import USAGE_PATH

# 账本的**定义和算法**都从写的那一头借过来,不在这儿再抄一份:
#   COUNTERS       哪些计数器
#   summarize      怎么加总
#   hit_rate       命中率的分母是什么
#   money          金额怎么渲染(含币种)
#   is_main_loop   哪些记录算主循环 —— 命中率曲线和页面那行小结(调用次数、
#                  输入、命中率、输出、耗时、金额)用的是同一条规矩,原来写在
#                  这儿,现在跟其它几条住一起
#
# 抄一份的代价:某天加了计数器、或者改了某个口径,usage.py 改了、这儿没改 ——
# 报表少算一栏、数字偏低。**而这不会报错**,只是数字变小了。同一个定义写两遍、
# 漂了不报错,是这个仓库反复在防的事。
#
# 页面上那行小结(usage.turn_line)用的是同一批函数,所以它和这份报表
# 必然一致 —— 不一致的可能性从"会不会漂"变成了"不可能"。
from pricing import tier_at
from usage import COUNTERS, hit_rate, is_main_loop, money, summarize


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
	rows = [(name, summarize(items)) for name, items in groups.items()]
	return sorted(rows, key=lambda pair: (-pair[1]["calls"], pair[0]))


def _readable_currency(exc):
	"""某个字符在**这个终端**的编码里没有时,换一个能打出来的写法。

	Windows 中文控制台的 codepage 是 936,`sys.stdout.encoding` 就是 gbk ——
	而它在打印 ¥ 的时候直接抛 UnicodeEncodeError。于是整份报表一个字都没
	出来,报错信息指向 report.py 里一行 `print`,看起来像是在说报表算错了,
	其实账本一个字没毛病。

	**为什么不 reconfigure 成 utf-8:** 那只是把字节换了个编码往同一个 cp936
	的控制台里倒 —— ¥ 不崩了,但整个中文也跟着变成乱码,比崩了更难查。终端
	认什么编码,就按什么编码写,认不出来的那几个字降级。

	降级成 "CNY " / "USD " 而不是 "?" 或 "\\xa5":跟 money() 里那条规矩同一个
	道理 —— 认不出的币种就把代码打出来,不猜一个符号。¥ 和 $ 差着七倍,而
	猜错的那个看起来一直是对的。

	注册成 codecs 的 error handler,是为了**一处生效**:money() 那边一个字
	不用改,也不用去包每一个 print。"""
	bad = exc.object[exc.start:exc.end]
	sub = {"¥": "CNY ", "$": "USD "}.get(bad)
	return (sub if sub is not None else "?", exc.end)


def main() -> None:
	# 装在 main 里,不装在模块顶上:装的那一下改了进程级的 stdout,而
	# **import 一个模块不该有那种副作用**(tests/test_report.py 就 import 它,
	# 装早了会连带改掉 pytest 的捕获流)。
	codecs.register_error("report", _readable_currency)
	try:
		sys.stdout.reconfigure(errors="report")
	except (AttributeError, ValueError, OSError):
		# reconfigure 是 3.7+ 的,而且 stdout 被换成不支持重配的对象时(管道、
		# 某些测试替身)会抛。这不是致命问题:拿不到降级就别降级,总比
		# 为了一行美化把整份报表拦下来强。
		pass

	path = Path(sys.argv[1]) if len(sys.argv) > 1 else USAGE_PATH
	if not path.exists():
		print(f"没有账本:{path}")
		print("跑一轮就有了 —— 跑一次 python server.py,在页面上聊几句。")
		return

	records, broken = load(path)
	print(f"账本 {path}")
	print(f"{len(records)} 条记录" + (f",{broken} 条坏行" if broken else ""))
	if not records:
		return

	# ---- 总览 ----
	overall = summarize(records)
	# 总输入的口径也只有一份(usage.total_input):input_tokens 是**未命中**
	# 那部分,把它当总量读会少一个数量级。
	total_in = overall["total_input"]
	print("\n## 总览")
	print(f"  调用次数    {overall['calls']}")
	print(f"  输入未命中  {overall['input_tokens']:,}")
	print(f"  输入命中    {overall['cache_read_input_tokens']:,}")
	print(f"  输入写入    {overall['cache_creation_input_tokens']:,}")
	print(f"  输出        {overall['output_tokens']:,}")
	print(f"  上下文总量  {total_in:,}")
	print(f"  缓存命中率  {hit_rate(overall)}")
	print(f"  总成本      {money(overall['cost'], overall['currency'])}"
	      + (f"   ({overall['unpriced']}/{overall['calls']} 条算不出金额)"
	         if overall["unpriced"] else ""))
	if overall["unpriced"]:
		print("              ↑ 价目表没填,见 pricing.py。它报 None 而不是 0,"
		      "所以这个数是**偏低**的,不是免费的。")
	if overall["cost"] is not None:
		print(f"  平均每次    "
		      f"{money(overall['cost'] / overall['calls'], overall['currency'])}")

	# ---- 按 purpose ----
	print("\n## 按 purpose —— 哪一块最贵")
	grouped = _group(records, lambda r: r.get("purpose") or "?")
	rows = [(name, row["calls"], f"{row['input_tokens']:,}",
	         f"{row['cache_read_input_tokens']:,}", f"{row['output_tokens']:,}",
	         hit_rate(row), money(row["cost"], row["currency"])) for name, row in grouped]
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
		             money(row["cost"], row["currency"])))
	print(_table(("agent", "calls", "miss", "hit", "hit%", "cost"), rows))

	# ---- 按会话 ----
	print("\n## 按会话(前 10,按调用次数)")
	rows = []
	for name, row in _group(records, lambda r: r.get("session") or "(无归属)"):
		rows.append((name, row["calls"], f"{row['input_tokens']:,}",
		             f"{row['cache_read_input_tokens']:,}", hit_rate(row),
		             money(row["cost"], row["currency"])))
	for row in rows[:10]:
		print(_table(("session", "calls", "miss", "hit", "hit%", "cost"), [row]))

	# ---- 按时段 ----
	print("\n## 按时段 —— 空闲是高峰的一半")
	by_tier = _group(records, lambda r: r.get("tier") or "(无)")
	rows = [(name, row["calls"], f"{row['input_tokens']:,}",
	         f"{row['cache_read_input_tokens']:,}", f"{row['output_tokens']:,}",
	         f"{row['elapsed_ms'] / 1000:.0f}s",
	         money(row["cost"], row["currency"])) for name, row in by_tier]
	print(_table(("tier", "calls", "miss", "hit", "out", "耗时", "cost"), rows))
	if any(name == "peak" for name, _ in by_tier):
		print("  高峰那段如果占了大头:批处理、长任务、批量重构挪到空闲时段 ——")
		print("  同样的量直接省一半,而且不用改一行代码。这是最省力的那个动作。")

	# ---- 重试的账 ----
	failed = [r for r in records if not r.get("ok", True)]
	if failed:
		row = summarize(failed)
		print("\n## 重试花掉的钱")
		print(f"  {row['calls']} 次调用没有拿到结果,"
		      f"其中 input 未命中 {row['input_tokens']:,} token,"
		      f"成本 {money(row['cost'], row['currency'])}")
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
			row = summarize(turns[key])
			marks = [name for name, seen in (("压缩", key in compacted),
			                                 ("重试", key in retried)) if seen]
			rows.append((key, row["calls"], f"{row['input_tokens']:,}",
			             f"{row['cache_read_input_tokens']:,}", hit_rate(row),
			             "← " + " + ".join(marks) if marks else ""))
		print(_table(("turn", "calls", "miss", "hit", "hit%", "发生了"), rows))
		print("  看什么:压缩那一轮的 hit% 如果掉一大截,而它省下的 token 又不多,"
		      "\n  那压缩的**净收益是负的** —— 省了 token,赔了缓存折扣。")

	# ---- 账本自检 ----
	#
	# tier 和 ts 在 usage.meter 里取自同一个时刻,所以拿 tier_at(ts) 复核每一条
	# 都该一致。不一致只有两种可能,两种都值得知道:
	#
	#   1. 账本被手改过;
	#   2. pricing 里的优惠时段窗口改过 —— 那会让**所有**历史记录一起对不上,
	#      而历史账目不该被新窗口重写(跟"改价必须一起改 PRICING_VERSION"是
	#      同一件事)。
	#
	# 这条检查的价值在于:上面两种都不会从别的任何地方露出来,而它们的后果是
	# 整个成本栏悄悄偏一倍。
	checked = [r for r in records
	           if r.get("ts") is not None and r.get("tier") is not None]
	drifted = [r for r in checked if tier_at(r["ts"]) != r["tier"]]
	print("\n## 账本自检")
	if drifted:
		sample = drifted[0]
		print(f"  ⚠️ {len(drifted)}/{len(checked)} 条的 tier 和它自己的 ts 对不上")
		print(f"     ts={sample['ts']} 记的是 {sample['tier']},"
		      f"按现在的窗口应是 {tier_at(sample['ts'])}")
		print("     要么账本被手改过,要么优惠时段窗口改过。后者会让全部历史")
		print("     记录一起对不上 —— 而历史账目不该被新窗口重写。")
	else:
		print(f"  {len(checked)} 条带 tier 的记录,都和自己的 ts 对得上。")
		# 没 tier 的那些是旧版记录(字段是这次改之前写的)。说出来,别让
		# "都对得上"这句话把没查的部分也算进去。
		skipped = len(records) - len(checked)
		if skipped:
			print(f"  (另外 {skipped} 条没有 tier 字段 —— 这次改动之前的旧记录)")


if __name__ == "__main__":
	main()
