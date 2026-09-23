"""dev.py 的"盯哪些文件"规则:改了要重启的、和不该管的。

只测纯函数 —— 这个文件要防的错就是"盯错了东西"(少盯一个 = 改了代码而没重启,
人还以为是缓存;多盯一个 = 每次保存都白重启一次),而那件事不用起服务就能验。
真正起进程、等空闲、重启那一段是手动的,见 dev.py 顶上的用法。

跑法: uv run pytest
"""

import sys
from pathlib import Path

import dev


def names(paths, root):
	"""相对路径的字符串形式,方便断言。"""
	return sorted(p.relative_to(root).as_posix() for p in paths)


def test_盯上服务真正会用到的东西():
	"""少盯一个的代价是"改了没生效而没人说话",所以这几个必须在里面。"""
	got = names(dev.iter_watched(), dev.ROOT)
	for want in ["server.py", "sessions.py", "agent.py", "context.py", "config.py",
	             "app.py", "tools/read.py", "tools/__init__.py",
	             "hooks/builtin.py", ".env"]:
		assert want in got, want


def test_不盯那几个没关系的():
	"""多盯一个的代价是每次保存都白重启 —— 尤其是一次性检查脚本,它们会被反复改。"""
	got = names(dev.iter_watched(), dev.ROOT)
	for unwanted in ["dev.py", "_ui_check.py", "_ui_scroll_check.py",
	                 "_ui_turns_check.py", "_session_check.py"]:
		assert unwanted not in got, unwanted
	assert not [p for p in got if p.startswith("tests/")], got
	assert not [p for p in got if "__pycache__" in p], got
	# 页面是每次请求现读的,改它刷一下浏览器就行,不该重启服务
	assert not [p for p in got if p.startswith("ui/")], got


def test_快照看得到新文件_改动只报被碰过的那一个(tmp_path):
	(tmp_path / "tools").mkdir()
	api = tmp_path / "api.py"
	api.write_text("x = 1", encoding="utf-8")
	helper = tmp_path / "tools" / "read.py"
	helper.write_text("y = 1", encoding="utf-8")
	# `_` 开头的不该被盯上:它改了也不进 changed
	throwaway = tmp_path / "_scratch.py"
	throwaway.write_text("z = 1", encoding="utf-8")

	before = dev.snapshot(tmp_path)
	assert names(before, tmp_path) == ["api.py", "tools/read.py"], names(before, tmp_path)
	assert dev.changed(before, before) == []

	# 改一个:只有它被报出来。长度也变了 —— 不靠 mtime 的分辨率赌
	api.write_text("x = 22222", encoding="utf-8")
	after = dev.snapshot(tmp_path)
	assert names(dev.changed(before, after), tmp_path) == ["api.py"]

	# 新加一个:也算改动(否则新文件要等到第二次改才生效)
	(tmp_path / "新模块.py").write_text("w = 1", encoding="utf-8")
	after2 = dev.snapshot(tmp_path)
	assert dev.changed(after, after2) == [tmp_path / "新模块.py"]

	# 删掉一个:也算(否则"删了"和"没动"分不出来)
	helper.unlink()
	after3 = dev.snapshot(tmp_path)
	assert dev.changed(after2, after3) == [helper]


def test_快照不看_下划线开头的脚本(tmp_path):
	(tmp_path / "real.py").write_text("a = 1", encoding="utf-8")
	scratch = tmp_path / "_ui_check.py"
	scratch.write_text("b = 1", encoding="utf-8")
	before = dev.snapshot(tmp_path)
	scratch.write_text("b = 22222", encoding="utf-8")
	assert dev.changed(before, dev.snapshot(tmp_path)) == []
