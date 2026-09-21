"""价目表。数据,不是代码。

数字来自你给的那张表 —— 单位是**人民币**,按百万 tokens 计:

    输入(缓存命中)    空闲 0.02 元    高峰 0.04 元
    输入(缓存未命中)  空闲 1    元    高峰 2    元
    输出              空闲 4    元    高峰 8    元

三件事会各自让账目差出一个倍数,而它们都不会报错:

一、**是人民币,不是美元。** 所以记录的字段叫 `cost` 不叫 `cost_usd`,渲染用 ¥。
    一个叫 `_usd` 的字段装着人民币是最坏的那种错 —— 它看起来一直是对的,直到
    你拿它去对账单。

二、**两个时段差整整一倍。** 所以单价不是一个数,是一张二维表;而"这次调用算
    哪个时段"必须由**调用发生的时刻**决定,不能事后猜 —— 事后重算等于用今天的
    时段改写历史的账。见 tier_at。

三、缓存写入**不在这张表里**。实测这个端点的 cache_creation_input_tokens 恒为
    0,所以它不是"未知",是"没有这一笔"。填 0.0 而不是留 None:留 None 会让
    整笔都算不出金额。

**为什么一个字段不填 None 就整笔算不出来:** 宁可整笔是"不知道",也不要给一个
少算了某一项的假数。少算的那部分不会报错,只会让你的账比真实的小。
"""

import time
from datetime import datetime, timedelta, timezone

# 价格变了必须改它。改之前写出去的那些记录带着旧版本号,所以历史账目不会被
# 新价悄悄改写。
PRICING_VERSION = "ds-2026-09-21-cny"

# 计价的货币。记录里带着它走(见 usage.meter),报表按它渲染 —— 而不是所有地方
# 都硬写一个 ¥。换供应商换币种时,旧记录仍然读得对。
CURRENCY = "CNY"

PEAK = "peak"
OFF_PEAK = "off_peak"

# 单价:人民币 / 每 100 万 token。
#
# 字段名**故意**跟 SDK 的 usage 计数器一模一样(input_tokens /
# cache_read_input_tokens / cache_creation_input_tokens / output_tokens),不做
# 改名。中间加一层"业务名 ↔ 字段名"的映射就是多一个会漂的地方,而它漂了不报错。
USD_PER_MTOK = {
	"deepseek-flash": {
		PEAK: {
			"input_tokens": 2.0,
			"cache_read_input_tokens": 0.04,
			"cache_creation_input_tokens": 0.0,
			"output_tokens": 8.0,
		},
		OFF_PEAK: {
			"input_tokens": 1.0,
			"cache_read_input_tokens": 0.02,
			"cache_creation_input_tokens": 0.0,
			"output_tokens": 4.0,
		},
	},
}

# 算金额之前必须都填上的那几项。cache_creation 不在里面:它是 0,乘不乘都一样。
_REQUIRED = ("input_tokens", "cache_read_input_tokens", "output_tokens")

# 优惠时段的窗口,**北京时间**,按分钟表示。左闭右开。
#
# ⚠️ **这个窗口我核不了** —— 我手上没有联网工具,查不到 DeepSeek 当前公布的
#    优惠时段。下面填的是"北京时间 00:30–08:30"(也就是 UTC 16:30–00:30),
#    这是 DeepSeek 一贯的优惠时段。**你必须自己确认一次。**
#
# 猜错的表现:落在窗口里的调用被按高峰计价,账目整体偏高,而它不报错。所以每次
# 调用都把 tier 记进账本(见 usage.meter),报表还专门有一段"按时段"——
# 拿它跟你的账单对一下就知道这里填得对不对。
#
# 改这里只需要改这两个数,别处不用动。
_OFF_PEAK_START_MIN = 0 * 60 + 30      # 00:30
_OFF_PEAK_END_MIN = 8 * 60 + 30        # 08:30

# 中国没有夏令时,固定 UTC+8 —— 所以直接加 8 小时就够,不用 zoneinfo。
# (Windows 上 zoneinfo 还得额外装 tzdata,而"只在某些机器上崩"是另一类坑。)
_BEIJING = timezone(timedelta(hours=8))


def tier_at(ts: float) -> str:
	"""这次调用(发生在 ts 这个时刻)该按哪个时段计价。

	**必须在调用发生时算,不能事后猜。** 两个时段差一倍,而事后重算会用今天的
	时刻改写历史的账 —— 历史账目一旦能被重写,它就不再是账目了。
	"""
	local = datetime.fromtimestamp(ts, _BEIJING)
	minutes = local.hour * 60 + local.minute
	return OFF_PEAK if _OFF_PEAK_START_MIN <= minutes < _OFF_PEAK_END_MIN else PEAK


def rates(model: str, tier: str):
	"""这个模型这个时段的单价表。没有返回 None。"""
	return USD_PER_MTOK.get(model, {}).get(tier)


def estimate_cost(model: str, counts: dict, tier: str) -> tuple[float | None, str]:
	"""按计数和时段算这一笔的金额。返回 (金额, 状态)。

	状态三种,读报表的时候要分开:
	  "priced"         算出来了
	  "unpriced"       模型/时段认得,但单价没填全 —— 金额是 None
	  "unknown_model"  这张表里根本没有这个模型或这个时段

	后两种都返回 None 而不是 0.0。0 的意思是"确定不花钱" —— 把"不知道"写成 0,
	总账看起来是零,而你会以为很便宜。
	"""
	table = USD_PER_MTOK.get(model)
	if table is None:
		return None, "unknown_model"
	prices = table.get(tier)
	if prices is None:
		return None, "unknown_model"
	if any(prices.get(key) is None for key in _REQUIRED):
		return None, "unpriced"

	amount = 0.0
	for key, per_mtok in prices.items():
		# counts 里缺这个键、或者值是 None(端点没报这个口径)都当 0 处理:
		# 没报的口径等于没有这一笔,不是"不知道价格"。两者在 cost_status 上
		# 已经分开了,不用在这儿再分一次。
		amount += (counts.get(key) or 0) * per_mtok / 1_000_000
	return amount, "priced"
