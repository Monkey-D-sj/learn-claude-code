"""价目表。数据,不是代码。

**为什么单独一个文件:** 它跟代码的寿命不一样。代码改一次是一次提交;价格是
**外部**给定的东西,随时会变,而变了之后历史记录还得能被解释 —— 见
PRICING_VERSION。

**为什么算出金额就要写进那条记录、不能事后重算:** 事后重算等于用今天的价格
改写历史账本。三个月后你看着一堆金额,分不出哪些是当时的价。所以
pricing_version 跟着每一条记录走。

**⚠️ 下面这几项是结构占位,不是当前价目。** 写这份代码的时候没有联网核对过
deepseek-flash 的真实单价,所以留的是 None。**你必须自己从 DeepSeek 的价目页
把数字填进来** —— 填之前,记录里的 cost_status 是 "unpriced",金额是 None,
报表的金额那一栏会显示成 "—"。这是有意的:一个没填的价目表不应该被误读成
"不花钱"(见下面 unpriced 那段)。

字段名**故意**跟 SDK 的 usage 计数器一模一样(input_tokens / cache_read_input_tokens
/ cache_creation_input_tokens / output_tokens),不做任何改名。中间加一层
"业务名 ↔ 字段名"的映射表,就是多一个会漂的地方,而它漂了不报错 —— 这是这个
仓库里最贵的一类 bug。名字一样,下面那个循环可以直接走 pricing 表的键去取计数,
没有中间层。

四个字段各自的理由:

  input_tokens
      **未命中**缓存的输入。注意它是"未命中",不是"总输入"。
      实测:同一个前缀打两次,冷那次 input_tokens=8057 / cache_read=0,
      热那次 input_tokens=249 / cache_read_input_tokens=7808 ——
      **两次相加都是 8057**。
      把 input_tokens 当总量读,账会少算一大截,而它不报错。

  cache_read_input_tokens
      命中的输入。与未命中通常差着几倍,这是整套缓存设计(记忆冻结、system
      拼在末尾)的全部收益所在。这一栏填对,你才量得出那套设计值多少钱。

  cache_creation_input_tokens
      显式缓存写入。这个端点上**实测恒为 0**,所以那 0.0 不是编的:它乘出来
      就是 0。留着这个键,是为了换供应商时不用改结构 —— Anthropic 原生那边
      这一项是真收钱的。
      **哪天它不再是 0,这一行必须重新填。**

  output_tokens
      输出。含 thinking —— 实测 max_tokens 不够时思考会把预算吃光、text 块
      根本不生成,所以这一栏不只是"正文"的成本。
"""

# 价格变了必须改它。改之前写出去的那些记录带着旧版本号,报表按版本分组,
# 所以历史账目不会被新价悄悄改写。
PRICING_VERSION = "ds-2026-09-unverified"

# 单位:美元 / 每 100 万 token。
#
# 三个给 None 的键 = "还没填"。None 和 0.0 在这里是**两种完全不同的意思**:
#   None  不知道价格 → 金额算不出来,记 cost_status="unpriced"
#   0.0   确实不收这笔钱 → 金额是 0
# 把不知道写成 0,总账看起来就是零,而你会以为很便宜。
USD_PER_MTOK = {
	"deepseek-flash": {
		"input_tokens": None,
		"cache_read_input_tokens": None,
		"cache_creation_input_tokens": 0.0,
		"output_tokens": None,
	},
}

# 算金额之前必须都填上的那几项。cache_creation 不在里面:它是 0,乘不乘都一样。
_REQUIRED = ("input_tokens", "cache_read_input_tokens", "output_tokens")


def rates(model: str):
	"""这个模型的单价表。没有返回 None。"""
	return USD_PER_MTOK.get(model)


def estimate_cost(model: str, counts: dict) -> tuple[float | None, str]:
	"""按计数算这一笔的美元数。返回 (金额, 状态)。

	状态三种,读报表的时候要分开:
	  "priced"         算出来了
	  "unpriced"       模型认得,但单价没填全 —— 金额是 None
	  "unknown_model"  这张表里根本没有这个模型

	后两种都返回 None 而不是 0.0。理由见文件头:0 的意思是"确定不花钱"。
	"""
	table = USD_PER_MTOK.get(model)
	if table is None:
		return None, "unknown_model"
	if any(table.get(key) is None for key in _REQUIRED):
		return None, "unpriced"

	usd = 0.0
	for key, per_mtok in table.items():
		# counts 里缺这个键、或者值是 None(端点没报这个口径)都当 0 处理:
		# 没报的口径等于没有这一笔,不是"不知道价格"。两者在 cost_status
		# 上已经分开了,不用在这儿再分一次。
		usd += (counts.get(key) or 0) * per_mtok / 1_000_000
	return usd, "priced"
