"""账本。一次 API 调用一行,append-only JSONL。

为什么要有它:"这一轮花了多少 / 哪一块最贵 / 缓存命中没有 / 压缩到底省没省钱"
—— 这四个问题在记账之前一个都答不了,而它们是唯一能告诉你"下一步该优化哪里"
的东西。没有它,优化只能靠猜。

## 两层,跟 context.py 的压缩是同一个道理

  原始层  本模块写出的 JSONL。一次调用一行,一个字段都不丢。
  聚合层  之后进 sessions.db 给页面读的汇总。

**先做原始层。** 理由是你会需要现在想不到的维度(按 purpose 拆、按工具拆、
按尝试次数拆),而能不能重新聚合完全取决于原始那行留了多少字段 —— 聚合表不行。
压缩那边先落盘原文再提炼,是同一件事。

## 为什么归属用 contextvar,不用参数

要记的东西分两类:

  "这次调用是什么"   purpose / model / 那几个计数器 —— call_api 自己就知道
  "这次调用属于谁"   session / turn / 哪个 agent —— 只有调用方的调用方知道

第二类要沿 agent_loop → 工具 handler → call_api 往下传,穿四层,而中间那些
handler 根本不该关心记账(跟 README 里"工具 handler 够不着前端"是同一个约束:
agent.py 只传 **block.input)。所以让最外层开一个 span,里面所有调用自动带上。

**为什么不是模块级变量:** server.py 一个进程里同时跑着好几个会话,模块级那份
会被它们串成一份,而串了不报错 —— 你会看到"A 会话花了 B 会话的钱"。
contextvar 是按线程/任务分的,天然跟着会话走。
"""

import contextvars
import json
import sys
import threading
import time

from config import USAGE_PATH
from pricing import CURRENCY, PRICING_VERSION, estimate_cost, tier_at

# 计数器名单。顺序就是写进 JSONL 的顺序,方便肉眼比对。
COUNTERS = (
	"input_tokens",
	"cache_read_input_tokens",
	"cache_creation_input_tokens",
	"output_tokens",
)

_SPAN = contextvars.ContextVar("usage_span", default={})

# server.py 是多线程的,而 append 不是原子的:一行被撕成两半的话,JSONL 里就
# 躺着一只坏行,读的那头会静默跳过或者炸掉整个报表。锁很便宜,值这个钱。
_LOCK = threading.Lock()


def span(**fields):
	"""返回一个上下文管理器:这一段里所有的 API 调用都带上 fields。

	嵌套时**合并,不覆盖**。合并是必须的:子 agent 只改 agent 这一项,它不该
	把外层的 session / turn 抹掉 —— 抹掉的话那笔钱就变成一条没有归属的孤儿
	记录,而子 agent 恰恰是最该被看见的一笔(嵌套、没人看、没人问)。

	用法:
	    with usage.span(session=sid, turn=turn_id):
	        agent_loop(...)
	"""
	merged = {**_SPAN.get(), **fields}
	token = _SPAN.set(merged)

	class _Span:
		def __enter__(self_inner):
			return merged

		def __exit__(self_inner, *exc):
			_SPAN.reset(token)
			return False

	return _Span()


def current_span() -> dict:
	"""这一层 span 的内容。没有就是空 dict —— 子 agent 之外的地方不去读它。"""
	return dict(_SPAN.get())


def counts_of(usage_obj) -> dict:
	"""把 SDK 的 usage 对象拍平成计数器。缺的给 None,**不是 0**。

	**None 和 0 必须分开。** 实测这个端点:cache_creation_input_tokens 恒为 0
	(有这个口径,不收这笔钱),而 cache_creation(不带 _input_tokens 后缀那个)
	是 null(没有这个口径)。写成 `getattr(u, name, 0)` 会把两者压成同一个 0,
	于是"缓存没命中"和"这个端点不报这个字段"就永远分不出来了 ——
	换供应商的第一天就会撞上,而且不报错。
	"""
	if usage_obj is None:
		return {name: None for name in COUNTERS}
	return {name: getattr(usage_obj, name, None) for name in COUNTERS}


def total_input(counts: dict) -> int | None:
	"""**上下文有多大** —— 这才是总输入。

	input_tokens 只是未命中的那部分。实测冷热两次相加恒定(8057+0 = 249+7808),
	所以总输入 = input + cache_read + cache_creation。单独看 input_tokens 会
	低一个数量级。全都没有(端点没报)时返回 None,不返回 0。
	"""
	if all(counts.get(name) is None for name in COUNTERS[:3]):
		return None
	return sum(counts.get(name) or 0 for name in COUNTERS[:3])


def summarize(records: list[dict]) -> dict:
	"""把一批记录加成一行。报表的每一段和终端那句小结都走这儿。

	**同一个口径只能写一遍。** 两处各写一遍的话会漂,而漂了不报错 —— 只是
	报表上那个数和屏幕上那个数不一样,你还得先决定信哪个。
	"""
	counters = {name: 0 for name in COUNTERS}
	cost, priced, unpriced = 0.0, 0, 0
	currency = None
	for record in records:
		for name in COUNTERS:
			counters[name] += record.get(name) or 0
		if record.get("cost") is None:
			unpriced += 1
		else:
			cost += record["cost"]
			priced += 1
			currency = currency or record.get("cost_currency")
	return {
		"calls": len(records),
		# **一条都算不出来时给 None,不是 0.0。** 0 的意思是"确定不花钱",
		# 而真实情况是"价目表没填,不知道"。
		"cost": cost if priced else None,
		# 币种从记录里带上来,不在渲染处硬写 —— 换币种时旧记录仍然读得对。
		"currency": currency or CURRENCY,
		"priced": priced,
		"unpriced": unpriced,
		"total_input": total_input(counters),
		"elapsed_ms": sum(record.get("elapsed_ms") or 0 for record in records),
		**counters,
	}


def hit_rate(row: dict) -> str:
	"""命中率 = 命中 / **输入总量**。

	分母是三个输入计数器之和,不是 input_tokens。拿 input_tokens 当分母会得到
	一个恒等于 0% 的命中率 —— 而 0% 看起来像"缓存没生效",你会去查缓存,查的
	却是错的。
	"""
	total = sum(row.get(name) or 0 for name in COUNTERS[:3])
	return "—" if total == 0 else f"{100 * (row.get('cache_read_input_tokens') or 0) / total:.1f}%"


_SYMBOLS = {"CNY": "¥", "USD": "$"}


def money(value: float | None, currency: str = CURRENCY) -> str:
	"""渲染金额。三种"零"必须长得不一样:

	  None  不知道(模型/时段没填价)  → "—"
	  0.0   确定不花钱               → "¥0"
	  极小但非零                     → 科学计数,不能四舍五入成 "¥0"

	第三条不是洁癖:把"花了一点"渲染成 "¥0",这一栏就失去了存在理由 ——
	而它上面两行的区别正是同一个道理。

	**符号按记录自带的币种来。** 认不出的币种就把代码打出来(比如 "XXX 1.2"),
	不猜一个符号:¥ 和 $ 差着七倍,而猜错的那个看起来一直是对的。
	"""
	head = _SYMBOLS.get(currency) or f"{currency} "
	if value is None:
		return "—"
	if value == 0:
		return f"{head}0"
	digits = 6 if value < 0.01 else 4
	text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
	return f"{head}{text}" if text != "0" else f"{head}{value:.2e}"


def _read_records() -> list[dict]:
	"""把整份账本读回来。坏行跳过,不吭声。

	跳过是对的:一行写坏(进程正好在 append 中途被杀)不该让整个报表读不
	出来。数量由 report.py 那边统计,所以也不是丢了没人知道。
	"""
	if not USAGE_PATH.exists():
		return []
	out = []
	for line in USAGE_PATH.read_text(encoding="utf-8").splitlines():
		if not line.strip():
			continue
		try:
			out.append(json.loads(line))
		except ValueError:
			continue
	return out


def read_turn(session: str, turn) -> list[dict]:
	"""把某一轮已经落盘的记录捞回来。给终端做一行小结用。

	**从文件读,不在内存里另攒一份。** 账本是唯一真源;攒一份的话"屏幕上显示的"
	和"账上记的"就成了两份,而它们漂了不报错 —— 屏幕上少一行,账上一条不少。
	顺带,读回来也算验了一下写有没有落下去。

	代价是每轮 O(账本行数)。一行几百字节,几万次调用也才几 MB,毫秒级 ——
	换掉"两份可能不一致"这个问题,值。
	"""
	return [record for record in _read_records()
	        if record.get("session") == session and record.get("turn") == turn]


def read_session(session: str) -> dict:
	"""一个会话里每一轮的记录,按 turn 分好组。整份账本只读一遍。

	给轮次接口用 —— 那儿一次要的是**所有**轮的小结,按轮各调一次
	read_turn 等于把同一个文件读 N 遍:N 随会话长度涨,而读到的内容
	一模一样。

	没有归属的账(turn 是 None:spans 之外的调用)也会收进来,挂在
	None 这个键上。丢掉的话,那笔钱就永远不出现在任何地方。
	"""
	grouped: dict = {}
	for record in _read_records():
		if record.get("session") == session:
			grouped.setdefault(record.get("turn"), []).append(record)
	return grouped


def turn_line(records: list[dict], prefix: str = "[本轮] ") -> str | None:
	"""每跑完一轮打的那一行。没有记录时返回 None(什么都不打)。

	**钱只在算得出来的时候显示。** 价目表没填时打一个 "$0" 是最坏的选择 ——
	那句话的意思是"这一轮没花钱"。不显示比显示错的强。

	prefix 给浏览器留的:页面里那一行在轮次框内部,"[本轮]" 是终端才需要的
	指代。格式化本身只写一遍 —— money/hit_rate 那几条规矩(None 和 0 不同、
	币种跟着记录走、命中率的分母是输入总量)不可能在 JS 里再写对一次。
	"""
	row = summarize(records)
	if not row["calls"]:
		return None
	line = (f"{prefix}{row['calls']} 次调用 · "
	        f"输入 {row['total_input']:,}"
	        f"(命中 {row['cache_read_input_tokens']:,} / {hit_rate(row)}) · "
	        f"输出 {row['output_tokens']:,} · "
	        f"{row['elapsed_ms'] / 1000:.1f}s")
	if row["cost"] is not None:
		line += f" · {money(row['cost'], row['currency'])}"
	return line


def _append(record: dict) -> None:
	USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
	line = json.dumps(record, ensure_ascii=False) + "\n"
	with _LOCK:
		with USAGE_PATH.open("a", encoding="utf-8") as fh:
			fh.write(line)


def meter(*, purpose: str, model: str, usage_obj, attempt: int,
          ok: bool, elapsed_ms: int, kind: str) -> None:
	"""记一次 API 调用。

	**绝不抛异常** —— 记账失败不该掀翻 agent 循环,那是把一个会计问题升级成
	一次任务失败。但也**不静默**:出问题往 stderr 打一行带前缀的,而不是咽掉。
	咽掉的话,你会在"账目莫名少了一半"的时候才发现,而且无从查起 ——
	跟这个仓库里所有"不报错"的坑是同一个形状。
	"""
	try:
		counts = counts_of(usage_obj)
		# **时段在调用发生的这一刻定,不留给事后。** 空闲和高峰差整整一倍,
		# 而事后重算等于用今天的时段改写历史的账 —— 历史账目一旦能被重写,它
		# 就不再是账目了。ts 和 tier 取自同一个时刻,所以报表那边能拿
		# tier_at(ts) 复核这一条,两个值必然一致(不一致就是有人插了手)。
		now = time.time()
		tier = tier_at(now)
		cost, status = estimate_cost(model, counts, tier)
		record = {
			"ts": round(now, 3),
			"session": None,
			"turn": None,
			"agent": "main",
			**current_span(),
			"purpose": purpose,
			"model": model,
			"kind": kind,
			"attempt": attempt,
			"ok": ok,
			"elapsed_ms": elapsed_ms,
			**counts,
			"total_input_tokens": total_input(counts),
			"service_tier": getattr(usage_obj, "service_tier", None),
			"tier": tier,
			# 金额的字段叫 cost 不叫 cost_usd —— 它是人民币。币种跟着记录走,
			# 报表按它渲染,而不是各处硬写一个符号。
			"cost": cost,
			"cost_currency": CURRENCY,
			"cost_status": status,
			"pricing_version": PRICING_VERSION,
		}
		_append(record)
	except Exception as exc:  # noqa: BLE001 —— 见上面那段,有意的兜底
		print(f"[usage] 这一笔没记上:{type(exc).__name__}: {exc}", file=sys.stderr)
