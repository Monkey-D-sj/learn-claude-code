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
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

import agent
import pricing
import usage


# ---------------------------------------------------------------- 价目表

def test_unfilled_price_is_none_not_zero():
	"""单价没填 → None,不是 0。

	写成 0 的话总账看起来是零,而你会以为很便宜 —— 而真正的结论是
	"我不知道花了多少"。
	"""
	counts = {"input_tokens": 1_000_000, "cache_read_input_tokens": 0,
	          "cache_creation_input_tokens": 0, "output_tokens": 1_000_000}
	cost, status = pricing.estimate_cost("deepseek-flash", counts)
	assert cost is None
	assert status == "unpriced"


def test_unknown_model_is_a_different_status():
	cost, status = pricing.estimate_cost("gpt-nobody", {})
	assert cost is None
	assert status == "unknown_model"


def test_cost_math(monkeypatch):
	monkeypatch.setitem(pricing.USD_PER_MTOK, "m", {
		"input_tokens": 1.0,
		"cache_read_input_tokens": 0.1,
		"cache_creation_input_tokens": 0.0,
		"output_tokens": 2.0,
	})
	cost, status = pricing.estimate_cost("m", {
		"input_tokens": 1_000_000,
		"cache_read_input_tokens": 1_000_000,
		"cache_creation_input_tokens": 5_000_000,
		"output_tokens": 1_000_000,
	})
	assert status == "priced"
	assert cost == pytest.approx(1.0 + 0.1 + 0.0 + 2.0)


def test_missing_counters_are_not_costed(monkeypatch):
	"""端点没报的口径按 0 计,但那是"没有这一笔",不是"价格不知道"。

	cost_status 已经在更外层把这两件事分开了,所以这儿可以按 0 算 ——
	分开这件事只该发生一次。
	"""
	monkeypatch.setitem(pricing.USD_PER_MTOK, "m", {
		"input_tokens": 1.0,
		"cache_read_input_tokens": 0.1,
		"cache_creation_input_tokens": 0.0,
		"output_tokens": 2.0,
	})
	cost, status = pricing.estimate_cost("m", {
		"input_tokens": 1_000_000,
		"cache_read_input_tokens": None,
		"cache_creation_input_tokens": None,
		"output_tokens": 0,
	})
	assert status == "priced"
	assert cost == pytest.approx(1.0)


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
	assert record["cost_status"] == "unpriced"
	assert record["pricing_version"] == pricing.PRICING_VERSION


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
