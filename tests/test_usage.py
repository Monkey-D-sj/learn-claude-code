"""账本。全部离线 —— 一次 API 都不打。

守的是四种"会静默出错"的东西,每一种在这个项目里都有先例:

1. **`input_tokens` 是未命中,不是总输入。** 实测冷 8057/user 0、热 249/7808,
   两次相加都是 8057。当总量读,账少算一个数量级,不报错。
2. **计数为 0 和字段不存在是两件事。** 这个端点 cache_creation_input_tokens
   恒为 0(有口径),而 cache_creation 是 null(没口径)。`getattr(u, x, 0)`
   把两者压成同一个 0。
3. **重试那一次的钱。** 第一次请求哪怕中途炸掉,input 也已经计费了,而
   message_start 里就带着 usage。
4. **记账失败不能掀翻 agent 循环**,但也不能静默。

前三条断言的是"数字对不对",第四条断言的是"坏了之后怎么样" —— 后者才是
这个仓库一贯在意的东西。
"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import agent
import pricing
import usage


# ---------------------------------------------------------------- 价目表

def test_price_is_in_cny_not_usd():
	"""币种是人民币。所以字段叫 `cost` 不叫 `cost_usd`,渲染用 ¥。

	一个叫 `_usd` 的字段装着人民币是最坏的那种错 —— 它看起来一直是对的,
	直到你拿它去对账单。
	"""
	assert pricing.CURRENCY == "CNY"
	assert usage.money(1.0) == "¥1"


def test_two_tiers_differ_by_exactly_two():
	"""高峰是空闲的两倍。这是那张表里唯一的规律,而整张表靠它自洽。

	钉住它:哪天有人只改了一档的价格,这里会红 —— 而只改一档的表现是账目
	"有点偏",从别的地方看不出来。
	"""
	table = pricing.USD_PER_MTOK["deepseek-flash"]
	for counter in ("input_tokens", "cache_read_input_tokens", "output_tokens"):
		assert table[pricing.PEAK][counter] == pytest.approx(
			table[pricing.OFF_PEAK][counter] * 2), counter


def test_real_prices_match_the_published_table():
	"""照抄一遍价目表 —— 填错数字的表现是账目整体偏一个倍数,不报错。"""
	table = pricing.USD_PER_MTOK["deepseek-flash"]
	assert table[pricing.OFF_PEAK]["cache_read_input_tokens"] == 0.02
	assert table[pricing.PEAK]["cache_read_input_tokens"] == 0.04
	assert table[pricing.OFF_PEAK]["input_tokens"] == 1.0
	assert table[pricing.PEAK]["input_tokens"] == 2.0
	assert table[pricing.OFF_PEAK]["output_tokens"] == 4.0
	assert table[pricing.PEAK]["output_tokens"] == 8.0


def test_cache_creation_is_free_not_unknown():
	"""缓存写入填的是 0.0,不是 None。

	实测这个端点的 cache_creation_input_tokens 恒为 0 —— 那是"没有这一笔",
	不是"不知道价格"。填 None 会让整笔都算不出金额。
	"""
	table = pricing.USD_PER_MTOK["deepseek-flash"]
	for tier in (pricing.PEAK, pricing.OFF_PEAK):
		assert table[tier]["cache_creation_input_tokens"] == 0.0


def test_cost_math_per_tier(monkeypatch):
	monkeypatch.setitem(pricing.USD_PER_MTOK, "m", {
		pricing.PEAK: {"input_tokens": 2.0, "cache_read_input_tokens": 0.2,
		               "cache_creation_input_tokens": 0.0, "output_tokens": 8.0},
		pricing.OFF_PEAK: {"input_tokens": 1.0, "cache_read_input_tokens": 0.1,
		                   "cache_creation_input_tokens": 0.0, "output_tokens": 4.0},
	})
	counts = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
	          "cache_creation_input_tokens": 0, "output_tokens": 1_000_000}
	peak, peak_status = pricing.estimate_cost("m", counts, pricing.PEAK)
	off, off_status = pricing.estimate_cost("m", counts, pricing.OFF_PEAK)
	assert peak_status == "priced" and off_status == "priced"
	assert peak == pytest.approx(2.0 + 0.2 + 8.0)
	assert off == pytest.approx(1.0 + 0.1 + 4.0)
	assert peak == pytest.approx(off * 2)


def test_unknown_model_or_tier_is_unknown_not_zero():
	cost, status = pricing.estimate_cost("gpt-nobody", {}, pricing.PEAK)
	assert cost is None and status == "unknown_model"
	# 模型认得、时段认不得 —— 也是 unknown。当成 0 就是把"不知道"说成"不花钱"。
	cost, status = pricing.estimate_cost("deepseek-flash", {}, "半夜")
	assert cost is None and status == "unknown_model"


def test_unfilled_price_is_none_not_zero(monkeypatch):
	"""某一档单价没填 → None,不是 0。

	写成 0 的话总账看起来是零,而真正的结论是"我不知道花了多少"。
	"""
	monkeypatch.setitem(pricing.USD_PER_MTOK, "half-filled", {
		pricing.PEAK: {"input_tokens": None, "cache_read_input_tokens": 0.04,
		               "cache_creation_input_tokens": 0.0, "output_tokens": 8.0},
	})
	cost, status = pricing.estimate_cost(
		"half-filled", {"input_tokens": 1_000_000}, pricing.PEAK)
	assert cost is None
	assert status == "unpriced"


def test_missing_counters_are_not_costed(monkeypatch):
	"""端点没报的口径按 0 计 —— 那是"没有这一笔",不是"价格不知道"。

	两者在 cost_status 上已经分开了,所以这儿可以按 0 算 —— 分开这件事只该
	发生一次。
	"""
	monkeypatch.setitem(pricing.USD_PER_MTOK, "m", {
		pricing.PEAK: {"input_tokens": 1.0, "cache_read_input_tokens": 0.1,
		               "cache_creation_input_tokens": 0.0, "output_tokens": 2.0},
	})
	cost, status = pricing.estimate_cost("m", {
		"input_tokens": 1_000_000,
		"cache_read_input_tokens": None,
		"cache_creation_input_tokens": None,
		"output_tokens": 0,
	}, pricing.PEAK)
	assert status == "priced"
	assert cost == pytest.approx(1.0)


# ---------------------------------------------------------------- 时段

def _beijing(hour: int, minute: int) -> float:
	"""北京时间某点某分对应的 epoch。中国固定 UTC+8,没有夏令时。"""
	return datetime(2026, 9, 21, hour, minute,
	                tzinfo=timezone(timedelta(hours=8))).timestamp()


def test_off_peak_window_is_left_closed_right_open():
	"""窗口边界。左闭右开。

	边界写错的表现:窗口两侧各错一次,而账目只是"有一点偏",看不出来。
	"""
	assert pricing.tier_at(_beijing(0, 29)) == pricing.PEAK
	assert pricing.tier_at(_beijing(0, 30)) == pricing.OFF_PEAK     # 左闭
	assert pricing.tier_at(_beijing(3, 0)) == pricing.OFF_PEAK
	assert pricing.tier_at(_beijing(8, 29)) == pricing.OFF_PEAK
	assert pricing.tier_at(_beijing(8, 30)) == pricing.PEAK         # 右开
	assert pricing.tier_at(_beijing(12, 0)) == pricing.PEAK


def test_tier_is_beijing_not_utc():
	"""窗口按北京时间 —— 不能跟着跑代码那台机器的时区、也不能按 UTC 判。

	按 UTC 判的表现:同一份账本换个时区读就变了样,而它不报错。
	"""
	# 北京 02:00(= UTC 前一天 18:00)→ 空闲。若误按 UTC 的 18:00 判就是高峰。
	moment = _beijing(2, 0)
	assert pricing.tier_at(moment) == pricing.OFF_PEAK
	utc_hour = datetime.fromtimestamp(moment, timezone.utc).hour
	assert utc_hour == 18          # 证明确实是"UTC 看是 18 点"的那个时刻


# ---------------------------------------------------------------- 计数器

def test_missing_counter_is_none_not_zero():
	"""端点没报的口径 → None。写成 0 就再也分不出"没命中"和"没这个字段"。"""
	counts = usage.counts_of(SimpleNamespace(input_tokens=1, output_tokens=2))
	assert counts["cache_read_input_tokens"] is None
	assert counts["cache_creation_input_tokens"] is None
	assert counts["input_tokens"] == 1


def test_measured_zero_stays_zero():
	"""实测这个端点 cache_creation_input_tokens 恒为 0 —— 那是 0,不是缺。"""
	counts = usage.counts_of(SimpleNamespace(
		input_tokens=1, output_tokens=2,
		cache_read_input_tokens=0, cache_creation_input_tokens=0))
	assert counts["cache_creation_input_tokens"] == 0
	assert counts["cache_read_input_tokens"] == 0
	assert counts["cache_creation_input_tokens"] is not None


def test_total_input_counts_cache_read():
	"""实测那条不变式:两次相加都是 8057。"""
	cold = {"input_tokens": 8057, "cache_read_input_tokens": 0,
	        "cache_creation_input_tokens": 0, "output_tokens": 14}
	warm = {"input_tokens": 249, "cache_read_input_tokens": 7808,
	        "cache_creation_input_tokens": 0, "output_tokens": 32}
	assert usage.total_input(cold) == 8057
	assert usage.total_input(warm) == 8057
	assert sum(cold[name] for name in usage.COUNTERS[:3]) == 8057


def test_total_input_is_none_when_nothing_reported():
	"""一个口径都没报 → None,不是 0。0 的意思是"上下文是空的"。"""
	assert usage.total_input(usage.counts_of(SimpleNamespace())) is None


# ---------------------------------------------------------------- span

def test_nested_span_merges_and_restores():
	"""内层只覆盖它说的那几项,出来了要还原 —— 不是留着。"""
	with usage.span(session="s1", turn="t1"):
		with usage.span(agent="subagent"):
			assert usage.current_span() == {"session": "s1", "turn": "t1",
			                                "agent": "subagent"}
		assert usage.current_span() == {"session": "s1", "turn": "t1"}
	assert usage.current_span() == {}


def test_sibling_spans_do_not_leak():
	"""同一个线程里先后开两个 span,后者不该继承前者。"""
	with usage.span(session="a"):
		pass
	with usage.span(session="b"):
		assert usage.current_span() == {"session": "b"}


# ---------------------------------------------------------------- meter

@pytest.fixture
def ledger(tmp_path, monkeypatch):
	path = tmp_path / "usage.jsonl"
	monkeypatch.setattr(usage, "USAGE_PATH", path)
	return path


def read_ledger(path):
	return [json.loads(line) for line in
	        path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_usage(**overrides):
	base = dict(input_tokens=100, cache_read_input_tokens=0,
	            cache_creation_input_tokens=0, output_tokens=10,
	            service_tier="standard")
	base.update(overrides)
	return SimpleNamespace(**base)


def test_meter_carries_the_span(ledger):
	with usage.span(session="s1", turn="t1"):
		with usage.span(agent="subagent"):
			usage.meter(purpose="main", model="deepseek-flash",
			            usage_obj=make_usage(), attempt=1, ok=True,
			            elapsed_ms=12, kind="stream")
	record = read_ledger(ledger)[0]
	assert record["session"] == "s1"
	assert record["turn"] == "t1"
	assert record["agent"] == "subagent"
	assert record["purpose"] == "main"
	assert record["total_input_tokens"] == 100
	assert record["cost_status"] == "priced"      # 这个模型现在有真价了
	assert record["pricing_version"] == pricing.PRICING_VERSION


def test_meter_freezes_the_tier_with_the_timestamp(ledger):
	"""tier 和 ts 必须同源 —— 报表最后那段"账本自检"就靠这条。

	不同源的表现:报表每次都说"有记录对不上";而没人去看的时候更糟 ——
	整个成本栏悄悄偏一倍,因为时段决定单价。
	"""
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="deepseek-flash",
		            usage_obj=make_usage(), attempt=1, ok=True,
		            elapsed_ms=5, kind="stream")
	record = read_ledger(ledger)[0]
	assert record["tier"] in (pricing.PEAK, pricing.OFF_PEAK)
	assert pricing.tier_at(record["ts"]) == record["tier"]
	assert record["cost_currency"] == pricing.CURRENCY
	assert record["cost"] > 0        # 有真价了,金额算得出来(而且不是 None)


def test_meter_appends_one_line_per_call(ledger):
	usage.meter(purpose="main", model="m", usage_obj=make_usage(),
	            attempt=1, ok=True, elapsed_ms=1, kind="stream")
	usage.meter(purpose="compaction", model="m", usage_obj=make_usage(),
	            attempt=1, ok=True, elapsed_ms=1, kind="nonstream")
	records = read_ledger(ledger)
	assert [r["purpose"] for r in records] == ["main", "compaction"]


def test_meter_survives_a_broken_ledger(ledger, monkeypatch, capsys):
	"""记账失败不能掀翻 agent 循环 —— 但也不能静默。"""
	def boom(record):
		raise OSError("disk full")

	monkeypatch.setattr(usage, "_append", boom)
	usage.meter(purpose="main", model="m", usage_obj=make_usage(),
	            attempt=1, ok=True, elapsed_ms=1, kind="stream")
	assert "usage" in capsys.readouterr().err


# ---------------------------------------------------------------- call_api

class OkStream:
	"""一次成功的流:message_start + 一段正文,最后拼好的 message。"""

	def __init__(self, start_usage, final_usage, text="hi"):
		self._start = start_usage
		self._final = final_usage
		self._text = text

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def __iter__(self):
		yield SimpleNamespace(type="message_start",
		                      message=SimpleNamespace(usage=self._start))
		yield SimpleNamespace(
			type="content_block_delta",
			delta=SimpleNamespace(type="text_delta", text=self._text))

	def get_final_message(self):
		return SimpleNamespace(usage=self._final, content=[], stop_reason="end_turn")


class BoomStream:
	"""一个中途炸掉的流:吐了 message_start(甚至 message_delta)之后断。"""

	def __init__(self, events, error):
		self._events = events
		self._error = error

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def __iter__(self):
		for event in self._events:
			yield event
		raise self._error

	def get_final_message(self):
		raise AssertionError("流已经断了,不该走到这儿")


def connection_error():
	"""一个真的 anthropic.APIConnectionError。

	**注意是 httpx2,不是 httpx** —— 这个 SDK 的 HTTP 层是那个包。agent.py 的
	error_chain 注释里那句「httpx2 和 httpcore2 会把同一个底层错误各包一遍」
	说的就是它。写 `import httpx` 会 ModuleNotFoundError,而测试挂掉的原因
	看起来像"依赖没装",跟真因(包名不同)差得很远。
	"""
	request = httpx2.Request("POST",
	                         "https://api.deepseek.com/anthropic/v1/messages")
	return anthropic.APIConnectionError(request=request)


def make_client(*streams):
	"""假的客户端:每调一次 messages.stream() 按顺序弹一个出来。

	弹出来的那个也可以是异常 —— 那就抛出来,模拟"还没建立流就失败"
	(那种情况下服务端一个 token 都没收到,账上不该多一笔)。
	"""
	queue = list(streams)

	def stream(**kwargs):
		item = queue.pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def test_successful_stream_records_final_usage(ledger, monkeypatch):
	monkeypatch.setattr(agent, "BASE_DELAY", 0)
	start = make_usage(output_tokens=0)
	final = make_usage(output_tokens=14)
	client = make_client(OkStream(start, final))

	response = agent.call_api(client, lambda event: None, model="m",
	                          messages=[], max_tokens=10)

	assert response.usage is final
	records = read_ledger(ledger)
	assert len(records) == 1
	assert records[0]["kind"] == "stream"
	assert records[0]["ok"] is True
	assert records[0]["attempt"] == 1
	# **final 那份,不是 start 那份** —— start 里 output_tokens 还是 0
	assert records[0]["output_tokens"] == 14


def test_retry_records_the_money_the_first_attempt_burned(ledger, monkeypatch):
	"""重试那一次的钱必须单独留下一笔。

	第一次请求哪怕中途炸掉,input 已经被计费了,而 message_start 里就带着
	usage。不接住它,每次重试都有一笔账凭空消失。
	"""
	monkeypatch.setattr(agent, "BASE_DELAY", 0)
	broken = BoomStream(
		[SimpleNamespace(type="message_start",
		                 message=SimpleNamespace(usage=make_usage(
			                 input_tokens=8057, output_tokens=0)))],
		connection_error(),
	)
	good = OkStream(make_usage(), make_usage(output_tokens=14))
	client = make_client(broken, good)

	agent.call_api(client, lambda event: None, model="m", purpose="compaction",
	               messages=[], max_tokens=10)

	records = read_ledger(ledger)
	assert len(records) == 2

	burned, succeeded = records
	assert burned["ok"] is False
	assert burned["kind"] == "partial"
	assert burned["attempt"] == 1
	assert burned["input_tokens"] == 8057
	assert burned["total_input_tokens"] == 8057
	# purpose 穿透到两笔上,包括重试那次
	assert burned["purpose"] == "compaction"

	assert succeeded["ok"] is True
	assert succeeded["attempt"] == 2
	assert succeeded["purpose"] == "compaction"


def test_late_delta_overrides_the_earlier_start_usage(ledger, monkeypatch):
	"""message_delta 那份更全(output_tokens 到那儿才齐),后到的覆盖先到的。

	不然中途失败记下的 output 永远是 0 —— 而模型可能已经吐了几百字。
	"""
	monkeypatch.setattr(agent, "BASE_DELAY", 0)
	broken = BoomStream(
		[SimpleNamespace(type="message_start",
		                 message=SimpleNamespace(usage=make_usage(output_tokens=0))),
		 SimpleNamespace(type="message_delta",
		                 usage=make_usage(output_tokens=137))],
		connection_error(),
	)
	client = make_client(broken, OkStream(make_usage(), make_usage()))

	agent.call_api(client, lambda event: None, model="m", messages=[], max_tokens=10)

	burned = read_ledger(ledger)[0]
	assert burned["ok"] is False
	assert burned["output_tokens"] == 137


def test_no_usage_no_record(ledger, monkeypatch):
	"""还没建立流就失败 —— 服务端一个 token 都没收到,账上不该多一笔。"""
	monkeypatch.setattr(agent, "BASE_DELAY", 0)
	client = make_client(connection_error(), OkStream(make_usage(), make_usage()))

	agent.call_api(client, lambda event: None, model="m", messages=[], max_tokens=10)

	records = read_ledger(ledger)
	assert len(records) == 1
	assert records[0]["attempt"] == 2
	assert records[0]["ok"] is True


def test_nonstream_is_labelled(ledger, monkeypatch):
	monkeypatch.setattr(agent, "BASE_DELAY", 0)
	response = SimpleNamespace(usage=make_usage(output_tokens=7))
	client = SimpleNamespace(messages=SimpleNamespace(
		create=lambda **kwargs: response))

	agent.call_api(client, lambda event: None, stream=False, model="m",
	               purpose="vision", messages=[], max_tokens=10)

	record = read_ledger(ledger)[0]
	assert record["kind"] == "nonstream"
	assert record["purpose"] == "vision"
	assert record["output_tokens"] == 7


# ---------------------------------------------------------------- 加总 / 命中率 / 金额

# 这三个的口径都在 usage.py 这一头,所以测试也在这儿。报表和终端那句小结都是
# 从这儿 import 的消费方 —— 只有一份定义,漂不了。


def rows(**over):
	base = dict(input_tokens=100, cache_read_input_tokens=900,
	            cache_creation_input_tokens=0, output_tokens=10,
	            elapsed_ms=100, cost=0.001, cost_currency="CNY")
	base.update(over)
	return base


def test_summarize_adds_up():
	row = usage.summarize([rows(), rows(cache_read_input_tokens=100)])
	assert row["calls"] == 2
	assert row["input_tokens"] == 200
	assert row["cache_read_input_tokens"] == 1000
	assert row["output_tokens"] == 20
	assert row["elapsed_ms"] == 200


def test_summarize_total_input_counts_cache_read():
	"""**踩过的坑。** total_input 是三个输入计数器之和。

	input_tokens 单独看会少一个数量级(实测冷 8057/命中 0、热 249/命中 7808,
	两次相加都是 8057)。
	"""
	row = usage.summarize([rows(input_tokens=249, cache_read_input_tokens=7808,
	                            cache_creation_input_tokens=0)])
	assert row["input_tokens"] == 249
	assert row["total_input"] == 8057


def test_summarize_cost_is_none_when_nothing_priced():
	"""**踩过的坑。** 全算不出来时是 None,不是 0.0。

	$0 的意思是"确定不花钱",而真实情况是"价目表没填,不知道"。第一版报表
	在这儿打了 "$0"。
	"""
	row = usage.summarize([rows(cost=None), rows(cost=None)])
	assert row["cost"] is None
	assert row["unpriced"] == 2
	assert row["priced"] == 0


def test_summarize_genuinely_free_is_zero():
	row = usage.summarize([rows(cost=0.0)])
	assert row["cost"] == 0.0
	assert row["priced"] == 1


def test_unpriced_rows_do_not_contaminate_the_sum():
	"""算不出来的那些不并进总和,单独数出来 —— 不然总账悄悄偏低。"""
	row = usage.summarize([rows(cost=0.5), rows(cost=None)])
	assert row["cost"] == pytest.approx(0.5)
	assert row["unpriced"] == 1


def test_hit_rate_denominator_is_all_three_input_counters():
	"""分母错写成 input_tokens 会得到一个恒等于 0% 的命中率 —— 而 0% 看起来像
	"缓存没生效",你会去查缓存,查的却是错的。"""
	assert usage.hit_rate({"input_tokens": 249, "cache_read_input_tokens": 7808,
	                       "cache_creation_input_tokens": 0}) == "96.9%"


def test_hit_rate_is_dash_when_nothing_reported():
	assert usage.hit_rate({"input_tokens": 0, "cache_read_input_tokens": 0,
	                       "cache_creation_input_tokens": 0}) == "—"


def test_money_distinguishes_three_kinds_of_zero():
	assert usage.money(None) == "—"            # 价目表没填
	assert usage.money(0.0) == "¥0"            # 确定不花钱
	assert usage.money(1e-9) == "¥1.00e-09"    # 不能四舍五入成 ¥0


def test_money_uses_the_records_currency():
	"""符号跟着记录自带的币种走,不硬写一个。

	认不出的币种把代码打出来,不猜符号 —— ¥ 和 $ 差着七倍,而猜错的那个
	看起来一直是对的。
	"""
	assert usage.money(1.0, "CNY") == "¥1"
	assert usage.money(1.0, "USD") == "$1"
	assert usage.money(1.0, "XYZ") == "XYZ 1"


def test_money_does_not_trail_zeros():
	"""单次调用的钱常常小于一分,所以小数位要够;但够了就别拖零。"""
	assert usage.money(0.006) == "¥0.006"      # 不是 ¥0.006000
	assert usage.money(0.0185) == "¥0.0185"
	assert usage.money(0.0009) == "¥0.0009"
	assert usage.money(1.2345) == "¥1.2345"


# ---------------------------------------------------------------- read_turn / turn_line

def test_read_turn_picks_only_that_turn(ledger):
	"""读回来的必须正好是 (session, turn) 那一组。

	终端那句小结靠它。多捞一条不报错,只是屏幕上那个数字悄悄变大 —— 而
	main.py 的 SESSION 带时间戳就是为了让这个匹配成立。
	"""
	for session, turn in (("s1", 1), ("s1", 2), ("s2", 1)):
		with usage.span(session=session, turn=turn):
			usage.meter(purpose="main", model="m", usage_obj=make_usage(),
			            attempt=1, ok=True, elapsed_ms=5, kind="stream")

	assert len(usage.read_turn("s1", 1)) == 1
	assert len(usage.read_turn("s1", 2)) == 1
	assert len(usage.read_turn("s2", 1)) == 1
	assert usage.read_turn("s1", 99) == []


def test_read_turn_is_empty_without_a_ledger(tmp_path, monkeypatch):
	monkeypatch.setattr(usage, "USAGE_PATH", tmp_path / "nope.jsonl")
	assert usage.read_turn("s1", 1) == []


def test_read_turn_skips_broken_lines(ledger):
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="m", usage_obj=make_usage(),
		            attempt=1, ok=True, elapsed_ms=5, kind="stream")
	with ledger.open("a", encoding="utf-8") as handle:
		handle.write("{ 半行 JSON\n")
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="m", usage_obj=make_usage(),
		            attempt=1, ok=True, elapsed_ms=5, kind="stream")
	assert len(usage.read_turn("s1", 1)) == 2


def test_read_session_groups_by_turn_in_one_pass(ledger, monkeypatch):
	"""一次读回整个会话,按轮分好组 —— 轮次接口要的是**所有**轮的小结。

	按轮各调一次 read_turn 结果一样,但把同一个文件读 N 遍。所以除了分组,
	还要钉住"只读一遍":多读几遍不报错,只是会话越长越慢。
	"""
	for session, turn in (("s1", 1), ("s1", 1), ("s1", 2), ("s2", 1)):
		with usage.span(session=session, turn=turn):
			usage.meter(purpose="main", model="m", usage_obj=make_usage(),
			            attempt=1, ok=True, elapsed_ms=5, kind="stream")

	content = ledger.read_text(encoding="utf-8")
	reads = []

	class CountingLedger:
		"""只数读了几遍。多读一遍不报错,所以只能这么钉。"""
		def exists(self):
			return True

		def read_text(self, **_):
			reads.append(1)
			return content

	monkeypatch.setattr(usage, "USAGE_PATH", CountingLedger())
	grouped = usage.read_session("s1")

	assert sorted(grouped) == [1, 2]
	assert len(grouped[1]) == 2          # 同一轮里的两次调用归到一组
	assert len(grouped[2]) == 1
	assert len(reads) == 1               # 整份账本只读一遍


def test_read_session_keeps_records_without_a_turn(ledger):
	"""有会话、没轮次的账也要收进来,挂在 None 这个键上。

	丢掉的话那笔钱永远不出现在任何地方 —— 而"少一笔"和"没花"在页面上
	长得一模一样。
	"""
	with usage.span(session="s1"):                      # 只开了会话,没开轮
		usage.meter(purpose="main", model="m", usage_obj=make_usage(),
		            attempt=1, ok=True, elapsed_ms=5, kind="stream")
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="m", usage_obj=make_usage(),
		            attempt=1, ok=True, elapsed_ms=5, kind="stream")

	grouped = usage.read_session("s1")
	assert set(grouped) == {None, 1}
	assert len(grouped[None]) == 1
	assert len(grouped[1]) == 1


def test_read_session_is_empty_without_a_ledger(tmp_path, monkeypatch):
	monkeypatch.setattr(usage, "USAGE_PATH", tmp_path / "nope.jsonl")
	assert usage.read_session("s1") == {}


def test_turn_line_prefix_is_dropped_for_the_page(ledger):
	"""页面上那一行在轮次框内部,"[本轮]" 是终端才需要的指代。

	只换前缀,不换格式 —— 金额和命中率的规矩在这儿重写一遍就会漂,而漂了
	不报错,只是页面上的数和屏幕上的数不一样。
	"""
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="m",
		            usage_obj=make_usage(input_tokens=100,
		                                 cache_read_input_tokens=900),
		            attempt=1, ok=True, elapsed_ms=5, kind="stream")
	records = usage.read_turn("s1", 1)
	default = usage.turn_line(records)
	assert default.startswith("[本轮] ")
	assert usage.turn_line(records, prefix="") == default[len("[本轮] "):]
	assert usage.turn_line([], prefix="") is None


def test_turn_line_shows_tokens_and_hides_unknown_money(ledger):
	"""价目表没填时**不显示金额**。

	显示 "$0" 是在说"这一轮没花钱" —— 不显示比显示错的强。
	"""
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="unknown-model",
		            usage_obj=make_usage(input_tokens=100,
		                                 cache_read_input_tokens=900,
		                                 output_tokens=10),
		            attempt=1, ok=True, elapsed_ms=1500, kind="stream")
	line = usage.turn_line(usage.read_turn("s1", 1))
	assert "1 次调用" in line
	assert "1,000" in line        # 总输入 = 100 + 900,不是 100
	assert "90.0%" in line
	assert "输出 10" in line
	assert "1.5s" in line
	assert "$" not in line and "¥" not in line


def test_turn_line_shows_money_when_priced(ledger, monkeypatch):
	monkeypatch.setitem(pricing.USD_PER_MTOK, "priced-model", {
		pricing.PEAK: {"input_tokens": 1.0, "cache_read_input_tokens": 0.1,
		               "cache_creation_input_tokens": 0.0, "output_tokens": 2.0},
		pricing.OFF_PEAK: {"input_tokens": 0.5, "cache_read_input_tokens": 0.05,
		                   "cache_creation_input_tokens": 0.0, "output_tokens": 1.0},
	})
	with usage.span(session="s1", turn=1):
		usage.meter(purpose="main", model="priced-model",
		            usage_obj=make_usage(input_tokens=1_000_000,
		                                 output_tokens=1_000_000),
		            attempt=1, ok=True, elapsed_ms=10, kind="stream")
	record = usage.read_turn("s1", 1)[0]
	line = usage.turn_line([record])
	# 不断言具体金额:测试跑在哪个时段是不确定的,而两个时段差一倍。
	# 断言的是"屏幕上那个数就是账本算出来的那个数"。
	# 注意记录里那个字段叫 cost_currency,汇总行里才叫 currency。
	assert usage.money(record["cost"], record["cost_currency"]) in line
	assert record["cost"] > 1.0


def test_turn_line_is_none_without_records():
	"""一条记录都没有时不打空行 —— 记账关掉/全失败的时候不该在终端留个残句。"""
	assert usage.turn_line([]) is None


# ---------------------------------------------------------------- 会话名

def test_terminal_session_name_is_per_run_not_a_constant():
	"""**踩过的坑。** main.py 的 SESSION 不能写死成那个常量 "terminal"。

	轮次序号每个进程都从 1 开始,而终端进程一个进程就是一个会话 —— 名字写死的
	话,**两次运行的第 1 轮会撞进同一组**。报表把它们当同一轮加总,数字凭空
	变大,不报错。

	实测见过:turn 1 显示 3 次调用,其实是两次运行各一次 + 另一次。逐轮数字
	从此就不可信了,而它看起来完全正常。

	所以这儿断言的是"这个名字是每次运行现造的",不是某个固定值。
	"""
	import main

	assert main.SESSION != "terminal"
	assert main.SESSION.startswith("terminal-")
	# 带时间戳:两个进程拿到的值不同。纯随机串也能满足唯一性,但读不出是哪次 ——
	# 报表的"按会话"那一栏就没法看。解析一遍顺便把格式也钉住。
	name = main.SESSION[len("terminal-"):]
	time.strptime(name, "%Y%m%d-%H%M%S")
