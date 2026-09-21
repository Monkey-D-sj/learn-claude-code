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
from pricing import PRICING_VERSION, estimate_cost

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
		cost, status = estimate_cost(model, counts)
		record = {
			"ts": round(time.time(), 3),
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
			"cost_usd": cost,
			"cost_status": status,
			"pricing_version": PRICING_VERSION,
		}
		_append(record)
	except Exception as exc:  # noqa: BLE001 —— 见上面那段,有意的兜底
		print(f"[usage] 这一笔没记上:{type(exc).__name__}: {exc}", file=sys.stderr)
