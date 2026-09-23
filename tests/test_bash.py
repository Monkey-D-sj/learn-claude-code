"""bash 的特征化测试:六种结局必须两两可分,而且超长也分得开。

盯三件事:

  一、**成败不再靠猜**。原来成功和失败都无输出时,两条结果逐字节相同
     (`(no output)`),模型只能按"没报错就是成了"来读 —— 安装/构建/检查命令
     失败之后,它会接着基于错误的前提往下干。现在状态头在第一行,六个用例
     就是六种结局:成功无输出、失败无输出、成功但有 stderr、非零退出但有
     stdout、超时、起不来。
  二、**stderr 有字 ≠ 失败**。git、编译器、pytest 的 warning 都走 stderr,
     所以只能分开写,不能拿它当失败信号。哪一段有内容是内容的事,成没成看状态头。
  三、**超长时状态头还在**。落盘预览是头 2000 + 尾 300,一个刷屏的日志足以
     把尾部冲出预览 —— 状态在尾部的话,模型看到的这半截里没有成败。

WORKDIR 全程指到 tmp_path:这些命令真会执行(只读的 echo / sleep),但那
也包括写文件的用例,不能落在真仓库里。
"""

import importlib
import subprocess

import pytest

import config
from tools import build_tools
from tools.todo import TodoManager

bash_mod = importlib.import_module("tools.bash")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
	"""临时目录当 WORKDIR。必须 resolve —— config 里那份就是 resolve 过的。"""
	root = tmp_path.resolve()
	monkeypatch.setattr(bash_mod, "WORKDIR", root)
	return root


def _status(out: str) -> str:
	"""状态头那一行。"""
	return out.splitlines()[0]


def test_成功无输出(workdir):
	assert bash_mod.run_bash("true") == "status: exit 0\n(no output)"


def test_失败无输出(workdir):
	out = bash_mod.run_bash("exit 1")
	assert out == "status: exit 1\n(no output)", out
	# 跟成功那条逐字节不同 —— 这正是原来丢掉的信号
	assert out != bash_mod.run_bash("true")


def test_成功但有stderr(workdir):
	"""status 是 exit 0:有 stderr 不等于失败,别把 warning 读成报错。"""
	out = bash_mod.run_bash("echo warn >&2")
	assert _status(out) == "status: exit 0", out
	assert "stderr: warn" in out, out
	assert "stdout:" not in out, out


def test_非零退出但有stdout(workdir):
	"""有 stdout 也不等于成功。"""
	out = bash_mod.run_bash("echo done; exit 3")
	assert _status(out) == "status: exit 3", out
	assert "stdout: done" in out, out
	assert "stderr:" not in out, out


def test_两段都有时分开写(workdir):
	out = bash_mod.run_bash("echo o; echo e >&2; exit 2")
	assert out == "status: exit 2\nstdout: o\nstderr: e", out


def test_命令真的在WORKDIR里跑(workdir):
	bash_mod.run_bash("echo hi > out.txt")
	assert (workdir / "out.txt").read_text(encoding="utf-8").strip() == "hi"


def test_超时是独立状态不是退出码(workdir, monkeypatch):
	"""超时 = 没跑完,副作用可能只做了一半,不能伪装成"命令返回了非零"。

	只睡 2 秒:bash 被杀掉之后,它那个 sleep 孙子还攥着管道,subprocess.run
	收尾要等到 EOF(实测见 tools/bash.py 顶上那段)—— 超时设 0.2 秒,这个
	调用仍然要 2 秒才回来,所以别在这儿睡 5 秒。
	"""
	monkeypatch.setattr(bash_mod, "TIMEOUT_SECONDS", 0.2)
	out = bash_mod.run_bash("echo started; sleep 2")
	assert _status(out) == "status: timeout after 0.2s (killed)", out
	# 卡住之前已经打出来的那半截是唯一线索,要留下
	assert "stderr:" not in out, out
	assert "stdout: started" in out, out


def test_起不来跟失败分开(workdir, monkeypatch):
	"""命令根本没执行过:重试有意义,而"执行了但失败"要改命令。"""
	monkeypatch.setattr(bash_mod, "BASH", str(workdir / "nope" / "bash.exe"))
	out = bash_mod.run_bash("echo hi")
	# 就两行:状态 + 空正文。没有退出码,也没有 stdout / stderr 段 ——
	# 三样都不能编出来。异常文本按平台不同,所以只认前缀。
	lines = out.splitlines()
	assert len(lines) == 2, out
	assert lines[0].startswith("status: failed to start ("), out
	assert lines[1] == "(no output)", out


def test_超长时状态仍可见_截断标在尾部(workdir):
	out = bash_mod.run_bash("head -c 500000 /dev/zero | tr '\\0' 'x'")
	# 状态头在第一行,且在 400000 那一刀之外
	assert _status(out) == "status: exit 0", _status(out)
	# 截断标注在尾部:落盘预览是头 + 尾,两侧都看得见
	assert out.endswith("chars total]"), out[-80:]
	assert len(out) < 500000, len(out)
	# 报的总数得是真数(正文 = "stdout: " + 500000 个 x)
	assert f"[truncated: {len('stdout: ') + 500000} chars total]" in out


def test_六种结局两两不同(workdir, monkeypatch):
	"""逐条比对 —— 撞在一起的两个状态就是模型分不出的那一对。"""
	outs = [bash_mod.run_bash("true"),
	        bash_mod.run_bash("exit 1"),
	        bash_mod.run_bash("echo warn >&2"),
	        bash_mod.run_bash("echo done; exit 3")]
	monkeypatch.setattr(bash_mod, "TIMEOUT_SECONDS", 0.2)
	outs.append(bash_mod.run_bash("sleep 2"))
	monkeypatch.setattr(bash_mod, "BASH", str(workdir / "nope" / "bash.exe"))
	outs.append(bash_mod.run_bash("echo hi"))
	assert len(set(outs)) == 6, outs


def test_还是返回字符串_接口没换(workdir):
	"""落盘、压缩、事件那几层都按字符串收结果,别在这儿引入新类型。"""
	assert isinstance(bash_mod.run_bash("true"), str)
	assert bash_mod.bash.handler is bash_mod.run_bash


def test_进工具集():
	names = [t.name for t in build_tools(TodoManager(), lambda q, o: None)]
	assert "bash" in names, names


def test_用的是config里那两样(workdir):
	"""WORKDIR / BASH 认的是 config 里那两个名字,不是自己写死的字面量。

	WORKDIR 这条还顺带钉住"cwd 是调用时现取模块属性":写成
	`def run_bash(command, cwd=WORKDIR)` 之后 monkeypatch 就不再生效,而
	测试会落在真仓库里跑。
	"""
	assert bash_mod.WORKDIR == workdir
	assert bash_mod.BASH == config.BASH
	# 而且那份 config.BASH 真能跑、退出码能透出来
	assert subprocess.run([bash_mod.BASH, "-c", "exit 7"]).returncode == 7
