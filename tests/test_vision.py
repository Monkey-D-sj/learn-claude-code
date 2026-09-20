"""vision 工具的特征化测试:四道闸、调用长什么样、以及"没答上"的三种分法。

盯五件事:

  一、**认魔数,不认扩展名**。`.png` 里是什么都能是;认不出的时候把读到的
     头几个字节交出去,模型才知道手里是个什么(常见的是拿 .pdf 来试)
  二、四道闸:读不到 / 不是图 / 超大 / 问题为空。都返回 Error 字符串,
     不抛 —— 工具 handler 只有这一个出口
  三、**空回答绝不能当答案交回去**。这个坑是实测踩出来的:模型先吐 thinking
     块,预算小的时候思考会把 max_tokens 吃光、text 块根本不生成。交回一个
     空字符串,模型会读成"图里什么都没有" —— 那是最坏的一种错
  四、发出去的东西:图是 base64 内联的、media_type 按魔数定的、问题原样、
     **stream=False**(这是内部工序,跟压缩器那次摘要一个道理)
  五、越界要问人:vision 读文件,所以跟 read_file 同一道闸。漏了它就是个
     洞,而且不报错

跑法: uv run pytest
"""

import base64
import importlib
from types import SimpleNamespace

import pytest

from tools import build_tools
from tools.todo import TodoManager

# 变量就叫 vision,tools/__init__.py 那句 import 会把包上的 tools.vision 属性
# 覆成 ToolDesc —— `as` 形式取的是属性,所以得走 importlib。详见 vision.py 末尾。
V = importlib.import_module("tools.vision")

# 四种格式的头几个字节 + 后面跟一点垃圾,够魔数认出来就行
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 40


@pytest.fixture
def looked(monkeypatch, tmp_path):
	"""换掉 call_api:记下这次调用长什么样,按脚本回答。WORKDIR 指到 tmp_path。"""
	root = tmp_path.resolve()
	monkeypatch.setattr(V, "WORKDIR", root)
	calls = []

	def install(reply="看过了。", stop_reason="end_turn", boom=None):
		def fake_call_api(api_client, emit, **kwargs):
			calls.append(kwargs)
			if boom is not None:
				raise boom
			blocks = [SimpleNamespace(type="thinking", thinking="先看看……")]
			if reply:
				blocks.append(SimpleNamespace(type="text", text=reply))
			return SimpleNamespace(content=blocks, stop_reason=stop_reason)
		monkeypatch.setattr(V, "call_api", fake_call_api)

	install()

	def write(name: str, raw: bytes):
		(root / name).write_bytes(raw)
		return name

	return SimpleNamespace(root=root, calls=calls, install=install, write=write)


# ---------- 一、认格式 ----------

@pytest.mark.parametrize("raw,media", [
	(PNG, "image/png"),
	(JPEG, "image/jpeg"),
	(GIF, "image/gif"),
	(WEBP, "image/webp"),
])
def test_四种格式都认(looked, raw, media):
	name = looked.write("x.bin", raw)
	assert V.run_vision(name, "这是什么?") == "看过了。"
	sent = looked.calls[0]["messages"][0]["content"][0]
	assert sent["type"] == "image"
	assert sent["source"]["media_type"] == media, sent["source"]["media_type"]
	# 发出去的必须**是那个文件的字节**,不是路径、不是缩略图
	assert base64.b64decode(sent["source"]["data"]) == raw


def test_不看扩展名(looked):
	"""名字叫 .png 但里面是文本 —— 得当场拒掉,不能把一段文字当图发出去。"""
	name = looked.write("假的.png", "这不是图,只是一段文字。".encode("utf-8"))
	out = V.run_vision(name, "里面是什么?")
	assert out.startswith("Error:"), out
	assert "not an image" in out, out
	assert looked.calls == [], "不是图还发出去了"


def test_不是图时报错里带读到的头几个字节(looked):
	"""模型才知道手里是个什么(常见的是拿 .pdf 或 .txt 来试),而不是只被告知不行。"""
	name = looked.write("a.pdf", b"%PDF-1.7\n")
	out = V.run_vision(name, "里面写了什么?")
	assert "'a.pdf'" in out and "%PDF" in out, out


# ---------- 二、另外三道闸 ----------

def test_读不到就报错(looked):
	out = V.run_vision("没有这个文件.png", "这是什么?")
	assert out.startswith("Error: cannot read"), out
	assert looked.calls == []


def test_超大就拒(looked):
	name = looked.write("大图.png", PNG + b"\x00" * V.MAX_BYTES)
	out = V.run_vision(name, "这是什么?")
	assert f"over the {V.MAX_BYTES} cap" in out, out
	assert looked.calls == [], "超了还发出去"


def test_问题空的就报错(looked):
	name = looked.write("a.png", PNG)
	for blank in ("", "   ", None):
		out = V.run_vision(name, blank)
		assert out.startswith("Error: question is empty"), out
	assert looked.calls == [], "问题都没成形,不该去调模型"


def test_问题两端的空白削掉(looked):
	name = looked.write("a.png", PNG)
	V.run_vision(name, "  里面是什么?  ")
	assert looked.calls[0]["messages"][0]["content"][1]["text"] == "里面是什么?"


# ---------- 三、空回答 ----------

def test_有回答就原样交回(looked):
	name = looked.write("a.png", PNG)
	assert V.run_vision(name, "这是什么?") == "看过了。"


def test_只取正文_跳过thinking(looked):
	"""thinking 是它的草稿,不是答案;两张都交回去等于把草稿当结论。"""
	name = looked.write("a.png", PNG)
	assert "先看看" not in V.run_vision(name, "这是什么?")


def test_没答上就报错_不能说图里什么都没有(looked):
	"""交回空字符串的话,模型会读成"图里什么都没有" —— 那是最坏的一种错:
	它会据此下结论,而真实原因是这次调用没成。"""
	name = looked.write("a.png", PNG)
	looked.install(reply="")
	out = V.run_vision(name, "这是什么?")
	assert out.startswith("Error:"), out
	assert "nothing" in out or "budget" in out, out


def test_预算不够和模型没答是两条不同的报错(looked):
	"""stop_reason=max_tokens 是实测踩到的那个:思考把预算吃光,text 块根本
	没生成。它的下一步(把问题问窄一点)跟"模型莫名其妙没答"不一样。"""
	name = looked.write("a.png", PNG)

	looked.install(reply="", stop_reason="max_tokens")
	starved = V.run_vision(name, "这是什么?")
	assert "budget" in starved and "narrower" in starved, starved

	looked.install(reply="", stop_reason="end_turn")
	silent = V.run_vision(name, "这是什么?")
	assert silent != starved
	assert "nothing" in silent, silent


def test_API错误带上根因(looked):
	"""只报外层的话,"连接超时"和"证书不对"长得一模一样,而处置完全不同 ——
	跟 agent.py 里 error_chain 是同一条理由。"""
	name = looked.write("a.png", PNG)
	import anthropic, httpx2

	req = httpx2.Request("POST", "https://api.deepseek.com/anthropic/v1/messages")
	inner = httpx2.ConnectError("连不上")
	boom = anthropic.APIConnectionError(request=req)
	boom.__cause__ = inner
	looked.install(boom=boom)

	out = V.run_vision(name, "这是什么?")
	assert out.startswith("Error: vision call failed"), out
	assert "ConnectError" in out and "连不上" in out, out


# ---------- 四、调用长什么样 ----------

def test_不走流式_用的是主模型(looked):
	"""内部工序,流出去会像模型在说话 —— 跟压缩器那次摘要同一个理由。"""
	name = looked.write("a.png", PNG)
	V.run_vision(name, "这是什么?")
	seen = looked.calls[0]
	assert seen["stream"] is False, seen["stream"]
	assert seen["model"] == V.MODEL, seen["model"]
	assert seen["max_tokens"] == V.MAX_TOKENS
	assert len(seen["messages"]) == 1 and seen["messages"][0]["role"] == "user"


def test_先图后问题(looked):
	name = looked.write("a.png", PNG)
	V.run_vision(name, "左半边什么颜色?")
	content = looked.calls[0]["messages"][0]["content"]
	assert [b["type"] for b in content] == ["image", "text"], content


# ---------- 五、接线 ----------

def test_handler的签名是agent_loop要的那种():
	import inspect
	params = inspect.signature(V.vision.handler).parameters
	assert list(params) == ["path", "question"], list(params)
	assert set(V.vision.input_schema["properties"]) == {"path", "question"}
	assert V.vision.input_schema["required"] == ["path", "question"]


def test_进工具集():
	names = [t.name for t in build_tools(TodoManager(), lambda q, o: "")]
	assert "vision" in names, names


def test_在FILE_TOOLS里():
	"""漏了它,`vision(path="../../x.png")` 就能绕开"越界要问人"那道门,
	而且不报错 —— 这是这次改动里最要紧的一行。"""
	from hooks.permission import FILE_TOOLS
	assert "vision" in FILE_TOOLS, FILE_TOOLS


def test_越界的图会拦下来问人():
	from hooks.permission import permission_hook

	asked = []
	block = SimpleNamespace(name="vision", input={"path": "../../外面.png"})
	assert permission_hook(block, lambda q: asked.append(q) or False) == "denied by user"
	assert asked and "vision" in asked[0], asked

	# 放行的那条路:问了,人同意了,就过
	allowed = SimpleNamespace(name="vision", input={"path": "../../外面.png"})
	assert permission_hook(allowed, lambda q: True) is None


def test_工作区里的图不问人():
	from hooks.permission import permission_hook
	block = SimpleNamespace(name="vision", input={"path": "shots/a.png"})
	assert permission_hook(block, lambda q: pytest.fail("不该问")) is None


def test_子agent拿得到(monkeypatch):
	"""通用读能力,像 grep —— 不像 task/memory/ask 有理由排除。"""
	import tools.subagent as subagent
	from agent import TurnOutcome

	seen = {}

	def fake_loop(messages, **kwargs):
		seen.update(kwargs)
		return TurnOutcome("completed", "结论")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	assert subagent.run_task("去看看") == "结论"
	assert "vision" in [t.name for t in seen["tools"]]
