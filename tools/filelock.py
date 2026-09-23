"""按路径的一把锁:读 — 校验 — 写这一整段,不许别人插进来。

**它解决的是什么。** 会话之间是并发的(见 server.py 顶上那两把锁),而它们
共享同一个 WORKDIR。两个会话各自"读文件 → 判断 → 把整个文件写回"时,后写的
那个用的是**它读到的那份旧内容**,于是先写的那次修改凭空消失,而两次调用都
返回成功 —— 这是这个仓库里最难查的一类错:没有报错,只有"我明明改了"。

加锁之后每个读改写是原子的,后到的那次会看见前一次的结果,于是它明确地报
"没找到那段文本"或"不唯一",而不是默默盖掉。

**键是规范化后的路径。** 同一个文件的不同写法(`a/../b.md`、大小写不同、符号
链接)必须落到**同一把**锁上,不然两把锁护同一个文件,等于没护。Windows 上还
要 `normcase`(大小写不敏感,而且反斜杠统一)。

**只增不删**,跟 server.py 的 LOCKS 同一个理由:删掉的瞬间,等在它上面的线程
手里还攥着**旧那把**,而下一个进来的人拿到的是新的 —— 两把锁护同一份状态。
本机工具,路径数是几百个,让它涨。

**边界,直说:** 这是**线程锁**,只管得住本进程。bash 里的 sed、你自己的编辑器、
另一个 server 进程(换了 AGENT_DB_PATH 但用同一个 WORKDIR)都不受影响。管住
那些得用 OS 级文件锁或独立工作目录(worktree),那是另一件事,`edit.py` 里那道
版本校验只是把跨进程的窗口收窄,关不上。
"""

import os
import threading
from pathlib import Path

# key 是规范化路径,value 是那把锁。只增不删,见文件头。
_LOCKS: dict[str, threading.Lock] = {}

# 护上面那个 dict 的增查。它不护文件,任何一毫秒都不圈住 I/O。
_GUARD = threading.Lock()


def canonical(path) -> str:
	"""锁键:解析过的绝对路径 + 这个平台的大小写规则。

	resolve() 对**还不存在**的文件也管用(不抛),这一点要紧:记忆文件第一次
	写之前是不存在的,而"不存在"和"存在"必须落在同一把锁上。
	"""
	return os.path.normcase(str(Path(path).resolve()))


def path_lock(path) -> threading.Lock:
	"""这个路径的那把锁。同一个文件无论怎么写法,拿到的都是同一把。"""
	key = canonical(path)
	with _GUARD:
		lock = _LOCKS.get(key)
		if lock is None:
			lock = _LOCKS[key] = threading.Lock()
		return lock
