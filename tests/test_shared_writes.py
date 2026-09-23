"""共享目录上的读—校验—写(R5)。

会话是并发的(同进程,几根线程),而它们共享一个 WORKDIR。两个调用都"先读、
判断、再整份写回"时,后写的那个用的是它读到的旧内容 —— 先写的那次修改凭空
消失,而**两边都收到成功**。这个仓库里最难查的就是这种:没有报错。

盯四件事:

  一、两份记忆各自按**规范化路径**上锁,读—校验—写整段在锁里。并发添加两
     条,两条都得在(不加锁的话活下来一条)
  二、容量检查也在锁里。29 条时两个 add 一起来:一个成,另一个必须报"满了"
     —— 不许两个都看到"还没满",一起写进去变成 31 条
  三、编辑撞车时**明确报错**,不许两边都说成功。锁让两个读改写串起来,后到的
     那个会看见前一次的结果,于是"文本不在了" —— 这正是想要的失败方式
  四、文件在读取之后、写入之前被别人(另一个进程、你的编辑器、bash)改过,
     要报冲突而不是照着自己的那份覆盖上去

测试全程临时目录 / 临时文件:两份真记忆(项目那份、用户那份)一个字都不许动。
"""

import threading

import pytest

import tools.filelock as filelock
from tools import edit as edit_mod
from tools import memory as M


# ---------- 一、二:记忆 ----------

def test_两个并发添加都留下(tmp_path):
	"""没有锁的话:两边都读到空文件,各自写回自己那一条,后写的把先写的盖掉。"""
	path = tmp_path / "MEMORY.md"
	results = {}

	def add(name, text):
		results[name] = M.run_memory(path, "add", content=text)

	threads = [threading.Thread(target=add, args=(n, f"fact {n}"))
	           for n in ("甲", "乙")]
	for t in threads:
		t.start()
	for t in threads:
		t.join(10)

	assert all(r.startswith("Added.") for r in results.values()), results
	entries = M._entries(path.read_text(encoding="utf-8"))
	# 两边都排:进来的先后本来就由线程调度决定,不该参与断言。而字面量按
	# **码点**排 —— 乙 是 U+4E59,甲 是 U+7532,所以"甲的在前"只是读着顺,
	# sorted 出来的顺序正好相反。
	assert sorted(entries) == sorted(["fact 甲", "fact 乙"]), entries


def test_容量检查在锁里(tmp_path):
	"""29 条时两根线同时加:只能有一个到 30,另一个必须看见"满了"。

	检查在锁外的话两边都看到 29,都通过,写出来 31 条 —— 上限形同虚设,
	而且没人报错。
	"""
	path = tmp_path / "MEMORY.md"
	path.write_text("\n".join(f"第 {i} 条" for i in range(29)) + "\n",
	                encoding="utf-8")
	results = {}

	def add(name):
		results[name] = M.run_memory(path, "add", content=f"新 {name}")

	threads = [threading.Thread(target=add, args=(n,)) for n in ("甲", "乙")]
	for t in threads:
		t.start()
	for t in threads:
		t.join(10)

	ok = [r for r in results.values() if r.startswith("Added.")]
	full = [r for r in results.values() if "memory is full" in r]
	assert len(ok) == 1 and len(full) == 1, results
	assert M.count_entries(path.read_text(encoding="utf-8")) == 30


def test_先删后加不丢(tmp_path):
	"""remove 和 add 混着来:各自是完整的读改写,不互相盖。"""
	path = tmp_path / "MEMORY.md"
	path.write_text("要删的那条\n留着的那条\n", encoding="utf-8")
	results = {}

	def work(name, fn):
		results[name] = fn()

	threads = [
		threading.Thread(target=work, args=("删", lambda: M.run_memory(
			path, "remove", match="要删的那条"))),
		threading.Thread(target=work, args=("加", lambda: M.run_memory(
			path, "add", content="新加的"))),
	]
	for t in threads:
		t.start()
	for t in threads:
		t.join(10)

	assert M._entries(path.read_text(encoding="utf-8")) == ["留着的那条", "新加的"], results


def test_记忆锁按规范化路径(tmp_path):
	"""同一个文件的两个写法必须是同一把锁 —— 不然两把锁护一个文件。"""
	(tmp_path / "sub").mkdir()
	one = filelock.path_lock(tmp_path / "sub" / ".." / "MEMORY.md")
	two = filelock.path_lock(tmp_path / "MEMORY.md")
	assert one is two


# ---------- 三:两个编辑撞车 ----------

def test_两个冲突编辑只有一个成功(tmp_path, monkeypatch):
	"""两边都想把同一段 X 换成不一样的东西:一个成,另一个必须**明确报错**。

	没有锁的话两边都可能返回 "Edited",而其中一次修改在磁盘上根本不存在。
	"""
	monkeypatch.setattr(edit_mod, "WORKDIR", tmp_path)
	path = tmp_path / "note.txt"
	path.write_text("开头 X 结尾\n", encoding="utf-8")
	results = {}

	def edit(name, repl):
		results[name] = edit_mod.run_edit("note.txt", "X", repl)

	threads = [threading.Thread(target=edit, args=(n, f"Y{n}"))
	           for n in ("甲", "乙")]
	for t in threads:
		t.start()
	for t in threads:
		t.join(10)

	ok = [r for r in results.values() if r == "Edited note.txt"]
	bad = [r for r in results.values()
	       if r.startswith("Error: text not found")]
	assert len(ok) == 1 and len(bad) == 1, results
	body = path.read_text(encoding="utf-8")
	# 赢家的那次改动在盘上,而且是完整的那一份(不是拼出来的两半)
	assert body in ("开头 Y甲 结尾\n", "开头 Y乙 结尾\n"), body
	assert body.count("Y") == 1, body


def test_编辑卡在别人手里的锁上(tmp_path, monkeypatch):
	"""锁真的在起作用:锁在我手里时,那根线只能等着,不能改完返回。"""
	monkeypatch.setattr(edit_mod, "WORKDIR", tmp_path)
	path = tmp_path / "note.txt"
	path.write_text("X\n", encoding="utf-8")
	lock = filelock.path_lock(path.resolve())
	done = threading.Event()
	box = {}

	def edit():
		box["out"] = edit_mod.run_edit("note.txt", "X", "Y")
		done.set()

	lock.acquire()
	try:
		t = threading.Thread(target=edit, daemon=True)
		t.start()
		# 它在锁上等着:这段时间里文件不许被动
		assert not done.wait(0.3), "没等锁就把文件改了"
		assert path.read_text(encoding="utf-8") == "X\n"
	finally:
		lock.release()
	assert done.wait(5), "放了锁它还是没跑完"
	assert box["out"] == "Edited note.txt"
	assert path.read_text(encoding="utf-8") == "Y\n"


# ---------- 四:读进来之后文件被别人改过 ----------

def test_读进来之后文件被改过_报冲突而不是覆盖(tmp_path, monkeypatch):
	"""我们的锁管不住别的进程。所以写之前再看一眼版本:变了就报,别拿手里
	那份旧内容盖上去。"""
	monkeypatch.setattr(edit_mod, "WORKDIR", tmp_path)
	path = tmp_path / "note.txt"
	path.write_text("X\n", encoding="utf-8")

	real = edit_mod._fingerprint
	calls = {"n": 0}

	def twice(*a, **kw):
		calls["n"] += 1
		if calls["n"] == 1:
			return real(*a, **kw)
		return ("被换过的版本", 1)      # 第二次问:文件变了

	monkeypatch.setattr(edit_mod, "_fingerprint", twice)
	out = edit_mod.run_edit("note.txt", "X", "Y")
	assert out.startswith("Error:") and "changed" in out, out
	# 一个字都没写进去
	assert path.read_text(encoding="utf-8") == "X\n"


def test_没人动过就正常写(tmp_path, monkeypatch):
	monkeypatch.setattr(edit_mod, "WORKDIR", tmp_path)
	(tmp_path / "note.txt").write_text("X\n", encoding="utf-8")
	assert edit_mod.run_edit("note.txt", "X", "Y") == "Edited note.txt"
	assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "Y\n"


def test_指纹看得到变化(tmp_path):
	path = tmp_path / "f.txt"
	path.write_text("a", encoding="utf-8")
	first = edit_mod._fingerprint(path)
	path.write_text("abcdef", encoding="utf-8")
	assert edit_mod._fingerprint(path) != first
