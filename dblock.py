"""一个库、一个服务:按数据库路径取进程级排他锁。

**为什么需要它,而不是靠端口。** 端口只挡得住"绑同一个端口的第二个实例"。
`dev.py` 会拒绝在有人在应答时启动,但直接 `uv run server.py` 绕得过去;而
Windows 上 SO_REUSEADDR 又不是 Unix 语义(见 dev.py 的 stop_child),换个端口
更简单。两个进程写同一个库的后果不是库坏了,是**互相改状态**:第二个实例一
启动就 `reap_running()`,而它分不出"死进程留下的"和"别人正在跑的",于是把
第一个实例正在跑的那一轮收成 failed;第一个实例随后收尾时条件更新匹配 0 行
(那行日志现在打得出来,就是这件事的痕迹)。

**为什么是文件锁,不是 PID 文件或一张 lock 表。** 进程被杀(任务管理器、
Ctrl+C 之后强杀、断电)时,PID 文件会留下一个**说谎的**残骸 —— 它说"有人",
而那个人早没了,还没人负责擦。文件锁由内核在进程消失时释放,"忘了删"这个
状态根本不存在。也因为这个:锁文件**永远不删**(见 lock_path)。

**跨平台两支,别换:**

    Windows  msvcrt.locking(LockFile)锁 1 个字节。同进程里两个 fd 也会互斥,
             所以测试能在一个进程里验它;进程退出即释放(强杀也一样)。
    POSIX    fcntl.flock —— 同样"同进程两个 fd 互斥"。换成 fcntl.lockf
             (POSIX 记录锁)就**不行**:那种锁同进程内不冲突,于是
             "第二把拿不到"的测试会假通过,而真起两个进程时才发现。

**它管不到什么,直说:** 只管住"用这个模块拿锁的进程"。你在另一个终端
`sqlite3 sessions.db`、或者某个脚本直接开 SessionStore,都不受影响 —— 文件锁
不是数据库层的隔离。库自己的并发由 SQLite 的写锁兜着(WAL + busy_timeout)。
"""

import os
from pathlib import Path

if os.name == "nt":
	import msvcrt
else:
	import fcntl


class AlreadyRunning(RuntimeError):
	"""这个库已经有别的进程持着锁了。调用方该拒绝启动,而不是接着往下走。"""


def lock_path(db_path) -> Path:
	"""锁文件就在库旁边,叫 `<库名>.lock`。

	**不删它**(release 里也只解锁、不删):删掉一个别人正持着的锁文件,等于
	让下一个来的人在一把新锁上成功 —— 两把锁护同一个库,又回到"两个服务"
	那个问题。留一个 1 字节的空壳文件,比留一个竞态便宜得多。
	"""
	return Path(str(db_path) + ".lock")


def _lock(fd: int) -> None:
	if os.name == "nt":
		# 0 字节的文件也能锁,但先写一个字节:锁一个"不存在的区间"在某些
		# 文件系统/驱动上会失败,而那种失败长得和"别人持着锁"一模一样。
		if os.fstat(fd).st_size == 0:
			os.write(fd, b"\0")
			os.lseek(fd, 0, os.SEEK_SET)
		msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
	else:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
	if os.name == "nt":
		msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
	else:
		fcntl.flock(fd, fcntl.LOCK_UN)


class DbLock:
	"""持着那把锁的凭证。**别让这个对象被回收** —— fd 一关,锁就没了。

	调用方(server.py)把它挂在模块级变量上,整个服务生命周期持有;进程退出
	时由操作系统释放,正常关闭那条路只是提前一点。
	"""

	def __init__(self, path: Path, fd: int):
		self.path = path
		self.fd = fd

	def release(self) -> None:
		"""解锁并关 fd。重复调用无害。"""
		fd, self.fd = self.fd, None
		if fd is None:
			return
		try:
			_unlock(fd)
		finally:
			os.close(fd)


def acquire(db_path) -> DbLock:
	"""拿 `db_path` 那把锁。拿不到抛 AlreadyRunning。

	**先拿锁,再建 SessionStore** —— 后者会跑迁移,那是往库里写。没拿到锁的
	实例一个字都不该写,包括 schema 版本号。这一点由调用顺序保证,见
	server.py 的 open_store()。
	"""
	path = lock_path(db_path)
	path.parent.mkdir(parents=True, exist_ok=True)
	fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
	try:
		_lock(fd)
	except OSError as e:
		os.close(fd)
		raise AlreadyRunning(
			f"{db_path} 已经被另一个进程占用({path})") from e
	return DbLock(path, fd)
