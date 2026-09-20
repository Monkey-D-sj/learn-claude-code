"""grep 的特征化测试:一棵临时的小树当 WORKDIR,不碰真仓库。

盯四件事:

  一、命中长什么样(`路径:行号: 内容`)、`include` 怎么过滤、正则真的在用
  二、四道闸:封顶 200 条(到顶就停手)、单行截断、坏正则报错、到顶给一句
     怎么收窄
  三、不该看见的东西看不见:隐藏目录、二进制、读不了的编码。最后那两种是
     **正常结果**,不能变成报错 —— 报错的话模型会以为搜索失败、换个写法
     重试,而正确的结论是"这些文件里没有"
  四、`include` 里写 `..` 出不去 WORKDIR(跟 glob.py 同一道闸)

隐藏目录那条特别值得钉:工具走的是标准库 glob 模块,**不是** pathlib 的
Path.glob —— 后者的 `**` 会钻进隐藏目录。写的时候第一版就是照抄了 glob.py
的包含检查、却没照抄它的遍历机制,`.venv` 会悄悄回到结果里。这个仓库上一次
shell grep 就是 15462 行 / 1.6 MB,而模型要的那几行埋在几千行依赖代码里。

跑法: uv run pytest
"""

import importlib
import inspect

import pytest

from tools import build_tools
from tools.todo import TodoManager

grep_mod = importlib.import_module("tools.grep")


@pytest.fixture
def tree(tmp_path, monkeypatch):
	"""一棵固定的小树,当这次测试的 WORKDIR。

	必须 resolve:代码里那道路径包含检查是
	`path.resolve().is_relative_to(WORKDIR)`,假定 WORKDIR 本身已经解析过
	(真仓库里就是 config.py 的 `Path.cwd().resolve()`)。
	"""
	root = tmp_path.resolve()
	(root / "a.py").write_text("needle one\ndef foo():\n", encoding="utf-8")
	(root / "b.txt").write_text("needle two\n", encoding="utf-8")
	(root / "sub").mkdir()
	(root / "sub" / "c.py").write_text("needle three\n", encoding="utf-8")
	(root / ".hidden").mkdir()
	(root / ".hidden" / "d.py").write_text("needle hidden\n", encoding="utf-8")
	(root / "bin.dat").write_bytes(b"\xff\xfe needle \x00\x94")
	(root / "gbk.py").write_bytes("needle 中文".encode("gbk"))
	(root / "long.py").write_text("needle " + "x" * 500 + "\n", encoding="utf-8")
	(root / "many.py").write_text("\n".join(f"needle {i}" for i in range(300))
	                             + "\n", encoding="utf-8")
	# 树**外面**的一个文件,给".."那条用
	(tmp_path.parent / "outside.py").write_text("needle outside\n", encoding="utf-8")

	monkeypatch.setattr(grep_mod, "WORKDIR", root)
	return root


def test_命中格式_路径_行号_内容(tree):
	assert grep_mod.run_grep("needle", include="a.py") == "a.py:1: needle one"
	assert grep_mod.run_grep("def foo", include="a.py") == "a.py:2: def foo():"
	# 子目录里的路径用 / 分隔、且是相对 WORKDIR 的
	assert grep_mod.run_grep("needle three") == "sub/c.py:1: needle three"


def test_正则真的在用(tree):
	# \d 是正则不是字面量:命中 100 和 200,不该命中 0 或 42
	assert grep_mod.run_grep(r"needle \d00", include="many.py").splitlines() == [
		"many.py:101: needle 100", "many.py:201: needle 200"]
	# 转义和两端锚点都是正则语义,不是字面量
	assert grep_mod.run_grep(r"^def", include="a.py") == "a.py:2: def foo():"
	assert grep_mod.run_grep(r"one$", include="a.py") == "a.py:1: needle one"
	assert grep_mod.run_grep(r"foo\(\):$", include="a.py") == "a.py:2: def foo():"
	# 而字面量的括号不该当成分组:`foo()$` 是"foo 后面跟一个空分组再收尾",
	# 而这一行以 ":" 收尾 —— 一个都不该命中
	assert grep_mod.run_grep("foo()$", include="a.py") == "(no matches)"


def test_include_过滤(tree):
	def files(**kw):
		# 掐掉到顶那一行提示,这里看的是"搜了哪些文件"
		return {line.split(":")[0]
		        for line in grep_mod.run_grep("needle", **kw).splitlines()
		        if not line.startswith("...")}

	assert files(include="*.py") == {"a.py", "long.py", "many.py"}
	assert files(include="sub/*.py") == {"sub/c.py"}
	assert files(include="*.txt") == {"b.txt"}


def test_隐藏目录不搜(tree):
	""".hidden/d.py 就在树里,但结果里不许有它。"""
	assert (tree / ".hidden" / "d.py").read_text(encoding="utf-8") == "needle hidden\n"
	assert grep_mod.run_grep("needle hidden") == "(no matches)"


def test_二进制和读不了的编码当没命中_不报错(tree):
	# 两者都是"这些文件里没有",不是"搜索失败了"
	assert grep_mod.run_grep("needle", include="bin.dat") == "(no matches)"
	assert grep_mod.run_grep("needle", include="gbk.py") == "(no matches)"
	# 而它们确实没被整棵树的搜索漏掉之外的对待:换个能读的照样命中
	assert grep_mod.run_grep("needle", include="b.txt") == "b.txt:1: needle two"


def test_没命中(tree):
	assert grep_mod.run_grep("这儿没有的东西") == "(no matches)"


def test_坏正则返回Error不抛(tree):
	out = grep_mod.run_grep("(")
	assert out.startswith("Error: bad regex"), out


def test_单行截断(tree):
	line = grep_mod.run_grep("needle", include="long.py")
	assert len(line) == len("long.py:1: ") + grep_mod.MAX_LINE_CHARS, len(line)
	assert line.startswith("long.py:1: needle xxx"), line


def test_命中封顶200条_到顶就停手(tree):
	lines = grep_mod.run_grep("needle", include="many.py").splitlines()
	assert len(lines) == grep_mod.MAX_MATCHES + 1, len(lines)
	assert lines[0] == "many.py:1: needle 0"
	assert lines[-2] == "many.py:200: needle 199", lines[-2]
	assert lines[-1].startswith("... (more than 200 matches"), lines[-1]


def test_正好200条时不多一句提示(tree, monkeypatch):
	# 200 条是一回事,"还有更多"是另一回事 —— 判据用 >,不用 >=
	exactly = tree / "exactly.py"
	exactly.write_text("\n".join(f"needle {i}" for i in range(200)) + "\n",
	                   encoding="utf-8")
	lines = grep_mod.run_grep("needle", include="exactly.py").splitlines()
	assert len(lines) == 200, len(lines)
	assert not any(line.startswith("...") for line in lines), lines[-1]


def test_include里写点点出不去WORKDIR(tree):
	assert (tree.parent / "outside.py").exists(), "外面那个文件得先在"
	assert grep_mod.run_grep("needle outside", include="../*.py") == "(no matches)"


def _nobody(question, options):
	"""build_tools 那个 ask_user 是必填的(理由见 tools/__init__.py:没有哪个
	默认值在三个前端里都对)。这几项不碰 ask 工具,给一个够用的就行。"""
	return None


def test_进工具集_挨着glob():
	tools = build_tools(TodoManager(), _nobody)
	names = [t.name for t in tools]
	assert names[names.index("glob") + 1] == "grep", names
	# 描述里得说清楚什么时候用它、什么时候该用 glob 和它的已知边界
	desc = [t.description for t in tools if t.name == "grep"][0]
	assert "glob" in desc and "Hidden" in desc, desc


def test_子agent的工具集_给grep_两个记忆不给(monkeypatch):
	""""给什么"没有常量可查 —— 它是 run_task 里现算的,所以直接跑一次
	run_task,把它交给循环的那份工具集截下来看。grep 是通用能力,不像
	task / memory 有理由排除。"""
	import tools.subagent as subagent
	from agent import TurnOutcome

	seen = {}

	def fake_loop(messages, **kwargs):
		seen.update(kwargs)
		return TurnOutcome("completed", "结论")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	assert subagent.run_task("去看看") == "结论"

	names = [t.name for t in seen["tools"]]
	assert "grep" in names, names
	for excluded in ("task", "memory", "user_memory"):
		assert excluded not in names, (excluded, names)


def test_handler的签名是agent_loop要的那种():
	"""agent.py 按 handler(**block.input) 调,所以参数名必须跟 schema 对齐。"""
	params = inspect.signature(grep_mod.grep.handler).parameters
	assert list(params) == ["pattern", "include"], list(params)
	assert params["include"].default == "**/*", params["include"].default
	assert set(grep_mod.grep.input_schema["properties"]) == {"pattern", "include"}
	assert grep_mod.grep.input_schema["required"] == ["pattern"]
