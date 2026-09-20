import os
import shutil
from pathlib import Path

WORKDIR = Path.cwd().resolve()

# Git Bash 优先 —— 不能直接信 shutil.which("bash")。
# 从 PowerShell 启动时 PATH 里 System32 排在 Git 前面,which 拿到的是
# C:\Windows\System32\bash.exe,那是 WSL 的启动器。它两处不合用:
#
#   1. 路径变成 /mnt/c/...,跟 WORKDIR 以及 read_file/write_file/edit_file
#      那套 Windows 路径对不上,模型得在两套写法之间自己换算;
#   2. 启动器自己的消息走 UTF-16LE,而 Linux 侧命令的输出是 UTF-8。两者混在
#      同一个 stderr 里,按哪种解都烂一半 —— 按 utf-8 解就是满屏 \ufffd。
#      bash.py 那边用的是 errors="replace",所以不报错,只静静地出乱码。
#
# Git Bash 没有中间那一层,stderr 是纯 UTF-8。
_GIT_BASH = [
	Path(r"C:\Program Files\Git\bin\bash.exe"),
	Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
	Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
	Path(r"C:\Program Files (x86)\Git\usr\bin\bash.exe"),
]

# 这两处的 bash 是 WSL / Microsoft Store 的入口,理由同上,绕开。
_SKIP_BASH = ("system32", "windowsapps")


def _find_bash() -> str:
	for path in _GIT_BASH:
		if path.exists():
			return str(path)
	# Git 装在非默认位置的话(换盘符之类),PATH 里再搜一遍。
	for entry in os.environ.get("PATH", "").split(os.pathsep):
		if not entry:
			continue
		cand = Path(entry) / "bash.exe"
		if cand.is_file() and not any(s in str(cand).lower() for s in _SKIP_BASH):
			return str(cand)
	# 只剩 WSL 了。能用,但上面那两个问题都还在 —— 留给没装 Git for Windows 的机器。
	return shutil.which("bash") or "bash"


BASH = _find_bash()

# 一轮 = 一次 API 调用 + 它要的那些工具。主 agent 和子 agent 共用,
# 免得跟 MODEL 一样在两个地方各写一份。
MAX_ROUNDS = 50

# 压缩用的两个目录。都放在 WORKDIR 里 —— 模型得能拿 bash/read_file 去读
# 落盘的工具结果,出了 WORKDIR 就够不着(还会被 permission_hook 拦)。
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"

# 两份记忆,两个作用域,两个文件。
#
#   memory/MEMORY.md   项目级 —— 这个仓库的约定和坑
#   user/USER.md       用户级 —— 你这个人的习惯和喜好
#
# **为什么分两份而不是一份里加标记:** 两者的寿命和归属不一样。项目那份跟着
# 仓库走(换个目录起服务就该是另一套),用户那份写的是"这个人怎么干活"。
# 混在一个文件里,换个仓库就把用户的喜好一起丢了,而丢的时候没有任何提示。
#
# **为什么都在 WORKDIR 里:** 跟 skills/ 一个理由 —— 它在 WORKDIR 里,
# permission_hook 现成管着,read_file/write_file 也能直接读写。搬到 WORKDIR
# 外面(比如家目录)就得为它开一条权限上的口子,而且用户想手改一条还得去别处找。
MEMORY_PATH = WORKDIR / "memory" / "MEMORY.md"
USER_MEMORY_PATH = WORKDIR / "user" / "USER.md"

# 记忆的两条线。放这儿跟 MAX_ROUNDS 并列,而不是散在 tools/memory.py 里:
# 它们是"这个 agent 的脾气",调的时候该跟轮数上限在同一个地方看到。
#
# 条数限的是**粒度** —— 一条一行,模型指认的时候才认得准(remove/update
# 按子串匹配,条目越短越不容易撞车)。
# 字符限的是**总量** —— 没有它,30 条可以写成一本书。
#
# 两条谁先到谁说了算。system prompt 里那行水位条的百分比取更满的那个,
# 见 tools/memory.py 的 memory_meter()。
MEMORY_MAX_ENTRIES = 30
MEMORY_MAX_CHARS = 4000
