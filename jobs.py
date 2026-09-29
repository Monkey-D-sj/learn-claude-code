"""后台执行的运行时:登记一个 job、在 daemon 线程里跑、把结果交回调度器。

这个模块**只干机制**,不知道跑的是 bash 还是子 agent —— 「后台跑什么」由调用方
给一个 work 函数。两条产品路子分别在 tools/bash.py(第二套 Bash 实现)和
tools/subagent.py 里,它们各自把结果摊成 JobResult。

分三层,别混:

	JobContext     "我是哪个会话、哪一轮、结果交给谁" —— contextvar,每轮 bind
	start()        登记 + 起线程,返回 job_id。**这是唯一对模型可见的入口**
	Windows Job Object   进程树的圈法与收法,给后台 Bash 用

**为什么 contextvar 而不是参数:** 工具的 handler 只拿得到 **block.input
(agent.py),够不着"我是哪个会话"。这跟 tools/compress.py 的 _RECALL 是同一个
问题、同一个解法 —— 那边绑取回器,这边绑归属。

**为什么不是模块级变量:** server.py 一个进程里同时跑着好几个会话,n 个后台
job 也同时在跑。模块级那份会被它们串成一份,而串了不报错,只是 A 会话的后台
Bash 把结果交到 B 会话的页面上。
"""

import ctypes
import json
import os
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path

import usage
from config import JOBS_DIR, MAX_BACKGROUND_JOBS

# 结果文件写多大。**这是第二个上限**,跟 tools/bash.py 的 MAX_OUTPUT_CHARS
# 不是一回事:那个限的是正文长度(截断后拼进结果),这个限的是"截断之后还能
# 落多少盘"。写进来的正文通常已经截过一刀,所以正常路径上永远碰不到它 ——
# 它在,是为了"哪天有人把那个上限调大,或者 work 返回了一段超长文本"时,
# 磁盘不会被一个后台任务写满。
MAX_RESULT_BYTES = 4_000_000

# 通知里带多少字。通知是要进模型上下文的,而正文可能 40 万字符 ——
# 全塞进去等于把"后台执行省下的上下文"又一次性还回去。完整结果在
# result_path 那个文件里,模型要就自己去读(它有 bash 和 read_file)。
SUMMARY_CHARS = 2000


@dataclass(frozen=True)
class JobResult:
	"""一次后台执行的产物。

	status  "completed" / "failed" —— 只这两个,跟 background_jobs 表的终态一致
	summary 给通知和页面看的那一小段(必要摘要),不是全文
	body    写进结果文件的完整正文
	error   失败原因;completed 时是 None

	**summary 和 body 是两个东西,不是一段文本的两种截法。** body 是"完整结果",
	它可能很大、可能只适合落盘;summary 是"进上下文的那一句",它必须是
	短到可以塞进每一条通知里的。合成一个的话,要么通知里驮着 40 万字符,
	要么完整结果根本没地方放。
	"""
	status: str
	summary: str
	body: str = ""
	error: str | None = None


@dataclass(frozen=True)
class JobContext:
	""""这个后台 job 是谁的"。每轮由 server.py bind 一份。

	store / session_id / turn_id 决定结果记到哪一行、写到哪个会话;
	notify 是**调度器**给的唤醒函数 —— 结果落库之后调它一次,job 那头就完事了。
	它不等待、不重试:结果已经在库里,叫不醒也只是"晚一拍交付",而下一道闸
	(轮末重扫、页面轮询)会补上(见 server.py 的 maybe_continue)。

	turn_id 是**发起这一次后台执行的轮**。它不参与任何恢复逻辑 —— 那一轮
	多半早收尾了。用途只有两个:用量归属(见 _worker 里的 usage.span),
	以及页面上"这个 job 是哪一轮派出去的"。
	"""
	store: object
	session_id: str
	turn_id: str
	notify: object


# 本轮的归属。bind_jobs 进、current() 出,见文件头。
_CTX: ContextVar = ContextVar("job_context", default=None)


class bind_jobs:
	"""`with bind_jobs(ctx):` —— 这一段里跑的 handler 都看得到这个后台归属。"""

	def __init__(self, ctx: JobContext):
		self._ctx = ctx
		self._token = None

	def __enter__(self):
		self._token = _CTX.set(self._ctx)
		return self._ctx

	def __exit__(self, *exc):
		_CTX.reset(self._token)
		return False


def current() -> JobContext | None:
	"""这一轮的后台归属。没有(子 agent 之外的裸调用、一次性脚本)返回 None。

	**返回 None 时调用方必须拒绝启动,不能猜一个会话出来。** 归属错的后果不是
	"结果送错地方"那么轻 —— 它会写进另一个会话的上下文,而那个会话的模型会
	拿一份不是自己的执行结果往下做决定。
	"""
	return _CTX.get()


class JobError(RuntimeError):
	"""这次后台执行没能启动,**原因是调用方能改的**。

	它不是异常情况:容量满了、没有会话归属、进程创建失败,都是正常请求撞上
	规则或环境。工具层接住它,把理由原样说给模型听 —— 模型据此能做的事
	(改小一点、同步跑、拆成两条)完全不同,只回一句"启动失败"它只能重试。
	"""


# 正在跑的 job -> 它的线程。**只增不删到线程结束**,用来数并发。
#
# 为什么不用库里的 status 去数:那个数是从 SQLite 读的,而"容量还够不够"
# 是个进程内的事实(线程能不能再起一个)。库里的行还要考虑"上个进程留下的"、
# "会话刚被删的",数出来会偏小或偏大 —— 而偏小的那一侧是没有上限。
_ACTIVE: dict[str, threading.Thread] = {}
_ACTIVE_LOCK = threading.Lock()


def active_count() -> int:
	with _ACTIVE_LOCK:
		return len(_ACTIVE)


def new_job_id() -> str:
	"""job_id 的形状。bg_ 前缀是为了让人一眼看出它不是 task_id、不是 turn_id。

	用 uuid 而不是自增:这个号会进模型上下文、进页面、进日志,而自增号会让人
	(和模型)忍不住去推测"bg_3 是不是 bg_2 后面那个" —— 那种推测在并发下
	没有意义,但会让人以为自己知道顺序。
	"""
	return f"bg_{uuid.uuid4().hex[:12]}"


def start(ctx: JobContext, tool: str, work) -> str:
	"""登记一个后台 job 并起线程。返回 job_id。

	**顺序是这个函数的全部意义:先登记,再起线程,最后才把 job_id 交出去。**

	  1. 登记(写库,queued)失败 → 抛,调用方回一句错误。模型拿到的是一个
	     明确的失败,而不是一个查不回来的 job_id。
	  2. 线程起不来 → 把那一行收成 failed 再抛。留着 queued 的话,页面上
	     会有一个永远"排队中"的 job,而它其实从来没跑过。
	  3. 只有前两步都成了,才返回号。

	work 的签名是 work(job_id, ctx) -> JobResult。它在**新线程**里跑,所以不能
	假设调用线程的 contextvars 跟过去了(见 _worker)—— ctx 是显式递进去的,
	不是让 work 自己去 current() 里捞(那儿是 None)。

	容量满时抛 JobError 而不是排队:排队要一个队列、一个调度器和"轮到时
	会话可能已经不在了"这一整套状态,而这一版明确不做跨重启的保全 ——
	排队排到一半进程没了,那个 job 既没跑也说不出为什么。直接拒绝更诚实。
	"""
	with _ACTIVE_LOCK:
		if len(_ACTIVE) >= MAX_BACKGROUND_JOBS:
			raise JobError(
				f"后台执行已经开满 {MAX_BACKGROUND_JOBS} 个了,等其中一个结束"
				f"再试,或者把这条改成同步执行")
		# 先占位再写库:写库可能慢(锁竞争),而这中间另一个线程可能在数
		# 同一个 len(_ACTIVE)。占位是"这个名额归我了",写失败再还回去。
		_ACTIVE[""] = None  # type: ignore[assignment]

	job_id = new_job_id()
	try:
		ctx.store.begin_job(job_id, ctx.session_id, tool, ctx.turn_id)
	except Exception as e:
		_release(None)
		raise JobError(
			f"这次后台执行没登记上({type(e).__name__}: {e}),所以没有启动它:"
			f"启动登记写不进库的话,你会拿到一个查不回来的 job_id") from e

	thread = threading.Thread(
		target=_worker, args=(ctx, job_id, work),
		name=f"job-{job_id}", daemon=True)
	try:
		thread.start()
	except Exception as e:
		ctx.store.finish_job(job_id, "failed", f"线程没能启动:{e}",
		                     "后台执行没能启动", None)
		_release(None)
		raise JobError(f"后台线程没能启动({type(e).__name__}: {e})") from e

	with _ACTIVE_LOCK:
		del _ACTIVE[""]
		_ACTIVE[job_id] = thread
	return job_id


def _release(job_id: str | None) -> None:
	"""把占位(或真号)从并发计数里去掉。"""
	with _ACTIVE_LOCK:
		if job_id is None:
			_ACTIVE.pop("", None)
		else:
			_ACTIVE.pop(job_id, None)


def _worker(ctx: JobContext, job_id: str, work) -> None:
	"""job 的线程体:起跑 → 干活 → 落结果 → 收终态 → 唤醒调度器。

	**不能依赖这个函数的 finally 写终态。** 它是 daemon 线程,进程退出会直接
	把它截断在任意一行上,而这一版明确接受那个截断(结果既不保全也不补交,
	见 docs 的 §4)。所以这里没有任何"崩溃后重试"的逻辑 —— 有的话它会写回
	一个已经被启动清理删掉的 session。
	"""
	try:
		# **usage.span 必须在这儿重建。** 这个线程是新起的,而 span 是
		# contextvar(ctx / agent / turn 都在 _drive 那一层绑着)——
		# 不重建的话,这笔钱会变成一条**没有 session / turn 的孤儿记录**。
		# 后台 job 是子 agent 那件事的加强版:嵌套、没人看、没人问。
		with usage.span(session=ctx.session_id, turn=ctx.turn_id,
		                agent="background"):
			_run(ctx, job_id, work)
	finally:
		_release(job_id)


def _run(ctx: JobContext, job_id: str, work) -> None:
	store = ctx.store
	# 从 queued 翻到 running。**翻不动就不跑。** 匹配 0 行只有两种可能:
	# 会话已经被删(行顺着 CASCADE 没了),或者这一行压根没建上。两种情况下
	# 这个 job 都没有归宿 —— 跑完也没有地方交结果,那就不该跑(它可能有副作用)。
	if not store.start_job(job_id):
		return

	try:
		result = work(job_id, ctx)
	except Exception as e:
		# 工具层的异常不外抛:它跑在一条没有调用方的线程上,抛出去只有
		# threading 的默认钩子接得住(打一行 stderr,而那一行跟这个 job
		# 的对应关系要靠线程名去猜)。收成 failed 是唯一能让模型看见的路。
		detail = f"{type(e).__name__}: {e}"
		result = JobResult("failed", detail, "", detail)

	path = None
	try:
		path = write_result(job_id, result.body)
	except OSError as e:
		# 落盘失败不该把这次执行判成失败 —— 活干完了,只是完整结果取不回来。
		# 摘要照给(它本来就是为了进上下文而准备的),但要**说出来**完整结果
		# 没了,否则模型会以为 summary 就是全部。
		result = replace(result, summary=(
			f"{result.summary}\n[完整结果没能落盘:{type(e).__name__}: {e}]"))

	# **收终态返回 False 就不再往下走。** 那意味着这一行已经不在了(会话被删),
	# 而下一步(唤醒调度器)会去开一轮续跑 —— 开在一个不存在的会话上就是
	# 一句外键错误,用户看到的是一个莫名其妙的报错。
	if store.finish_job(job_id, result.status, result.error, result.summary, path):
		ctx.notify(ctx.session_id)


def write_result(job_id: str, body: str) -> str | None:
	"""把结果正文写进受控目录,返回路径。空正文返回 None(不建空文件)。

	文件名只用 job_id:它是我们自己发的,而且**不拿会话 id 或工具名拼路径** ——
	那些是外面来的字符串,拼进路径就多了一条"目录穿越"的路(而这条路上
	没有任何地方会报错,只会把一个文件写到别处)。

	截到 MAX_RESULT_BYTES:正文通常已经截过一刀,所以这个上限正常碰不到,
	它在是为了"哪天上游的上限被调大"时磁盘不会被写满。
	"""
	if not body:
		return None
	JOBS_DIR.mkdir(parents=True, exist_ok=True)
	path = JOBS_DIR / f"{job_id}.txt"
	data = body.encode("utf-8")[:MAX_RESULT_BYTES]
	path.write_bytes(data)
	return str(path)


def read_result(path: str | None, chars: int = SUMMARY_CHARS) -> str:
	"""按路径取回一段结果正文,最多 chars 个字符。取不到给空串。

	给两处用:调度器拼通知(要摘要)、background_result 工具(要完整结果)。
	读不到不算异常 —— 文件可能被启动清理删了、或者根本就没落盘(写失败的
	那一刻已经在 summary 里说过了),两种都该由调用方说一句人话,而不是抛。
	"""
	if not path:
		return ""
	try:
		text = Path(path).read_text(encoding="utf-8", errors="replace")
	except OSError:
		return ""
	if len(text) <= chars:
		return text
	return f"{text[:chars]}\n... [结果更长,共 {len(text)} 字符,按上面的路径读全文]"


def sweep_results(paths) -> None:
	"""删掉这些结果文件。删不掉就留着,不抛。

	调用方是两处清理(启动时清空整张表、删会话),它们的主业是把库里的行
	弄干净;一个删不掉的文件不该让那件事失败 —— 用户会看到一个"删不掉会话"
	的报错,而真正的问题只是某个文件被别的进程占着。
	"""
	for path in paths:
		try:
			os.unlink(path)
		except OSError:
			pass


# ---------------------------------------------------------------- Windows 进程圈

# 下面这一截是后台 Bash 的**进程树**那件事,不是"再包一层 subprocess"。
#
# **为什么不能直接用 Popen:** 它给不了"创建之后、派生之前"这个时机。要收干净
# 整棵树,必须让进程**从第一个指令起就在作业对象里** —— 而 Popen 返回时进程
# 已经在跑了,那中间那段窗口里它 fork 出来的孩子不在作业对象里,关掉句柄时
# 它们照样活着(实测:收完两层 bash,那条 sleep 还在,管道写端也还攥在它手里)。
#
# 所以走 CreateProcessW,而且要 CREATE_SUSPENDED:挂着建出来 → 塞进作业对象
# → 才 ResumeThread。这三步之间进程一条指令都没执行过,也就不可能派生任何
# 不受控的东西。
#
# 作业对象设 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:句柄一关,其中的进程全结束。
# 服务进程退出时这件事由内核替我们做 —— 包括被 Ctrl+C、被任务管理器杀掉,
# 那些路径上我们自己的清理代码一行都不会跑。这是这一版"进程要收干净"的
# 唯一保证(见 docs 的 §1)。

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
	_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

	JobObjectExtendedLimitInformation = 9
	JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
	CREATE_SUSPENDED = 0x00000004
	STARTF_USESTDHANDLES = 0x00000100
	INFINITE = 0xFFFFFFFF
	WAIT_TIMEOUT = 0x00000102

	class _STARTUPINFOW(ctypes.Structure):
		_fields_ = [
			("cb", ctypes.c_ulong),
			("lpReserved", ctypes.c_wchar_p),
			("lpDesktop", ctypes.c_wchar_p),
			("lpTitle", ctypes.c_wchar_p),
			("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
			("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong),
			("dwXCountChars", ctypes.c_ulong),
			("dwYCountChars", ctypes.c_ulong),
			("dwFillAttribute", ctypes.c_ulong),
			("dwFlags", ctypes.c_ulong),
			("wShowWindow", ctypes.c_ushort),
			("cbReserved2", ctypes.c_ushort),
			("lpReserved2", ctypes.c_void_p),
			("hStdInput", ctypes.c_void_p),
			("hStdOutput", ctypes.c_void_p),
			("hStdError", ctypes.c_void_p),
		]

	class _PROCESS_INFORMATION(ctypes.Structure):
		_fields_ = [
			("hProcess", ctypes.c_void_p),
			("hThread", ctypes.c_void_p),
			("dwProcessId", ctypes.c_ulong),
			("dwThreadId", ctypes.c_ulong),
		]

	class _IO_COUNTERS(ctypes.Structure):
		_fields_ = [(name, ctypes.c_ulonglong) for name in (
			"ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
			"ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

	class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
		_fields_ = [
			("PerProcessUserTimeLimit", ctypes.c_longlong),
			("PerJobUserTimeLimit", ctypes.c_longlong),
			("LimitFlags", ctypes.c_ulong),
			("MinimumWorkingSetSize", ctypes.c_size_t),
			("MaximumWorkingSetSize", ctypes.c_size_t),
			("ActiveProcessLimit", ctypes.c_ulong),
			("Affinity", ctypes.c_size_t),
			("PriorityClass", ctypes.c_ulong),
			("SchedulingClass", ctypes.c_ulong),
		]

	class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
		_fields_ = [
			("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
			("IoInfo", _IO_COUNTERS),
			("ProcessMemoryLimit", ctypes.c_size_t),
			("JobMemoryLimit", ctypes.c_size_t),
			("PeakProcessMemoryUsed", ctypes.c_size_t),
			("PeakJobMemoryUsed", ctypes.c_size_t),
		]


class Job:
	"""一个受控进程 + 它所属的作业对象。

	handle 是作业对象的句柄,而**它就是"收干净"这件事本身**:进程退出时
	(任何一条路径,包括被强杀)句柄跟着关,KILL_ON_JOB_CLOSE 让内核把里面的
	所有进程结束。所以没有一个单独的"取消"方法需要被记得调用。
	"""

	__slots__ = ("handle", "process", "stdout_fd", "stderr_fd", "_killed")

	def __init__(self, handle, process, stdout_fd, stderr_fd):
		self.handle = handle
		self.process = process
		self.stdout_fd = stdout_fd
		self.stderr_fd = stderr_fd
		self._killed = False

	def kill(self) -> None:
		"""立刻结束整棵树。重复调用无害。

		路径是 TerminateJobObject,不是 TerminateProcess:后者只杀直接子进程,
		而它 fork 出来的孩子会活下来并继续攥着管道写端 —— 表现是"命令被杀掉了,
		可读输出的线程一直等不到 EOF"。
		"""
		if self._killed:
			return
		self._killed = True
		_kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1)

	def close(self) -> None:
		"""关掉作业对象句柄。其中的进程到此结束(见类注释)。"""
		if self.handle:
			_kernel32.CloseHandle(ctypes.c_void_p(self.handle))
			self.handle = None


def spawn_in_job(argv: list[str], cwd: str) -> Job:
	"""把 argv 建成一个进程,从第一条指令起就关在作业对象里。返回 Job。

	调用方拿到的 Job 里已经有 stdout / stderr 两个文件描述符:
	  job.stdout_fd / job.stderr_fd  —— 见下面 _PIPE 那段

	起不来(找不到可执行文件、权限)抛 OSError,跟 subprocess 一个口径 ——
	"命令根本没执行过"和"执行了但失败"必须分得开,前者重试有意义。
	"""
	if not _IS_WINDOWS:
		raise OSError(
			"后台 Bash 的进程树清理只实现了 Windows 那一套(作业对象);"
			"这个平台上没有等价物,所以拒绝启动而不是假装收得干净")

	handle = _kernel32.CreateJobObjectW(None, None)
	if not handle:
		raise OSError(f"CreateJobObject 失败(错误码 {ctypes.get_last_error()})")
	try:
		info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
		info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if not _kernel32.SetInformationJobObject(
				ctypes.c_void_p(handle), JobObjectExtendedLimitInformation,
				ctypes.byref(info), ctypes.sizeof(info)):
			raise OSError(
				f"SetInformationJobObject 失败(错误码 {ctypes.get_last_error()})")
		process = _create_suspended(argv, cwd, handle)
	except BaseException:
		_kernel32.CloseHandle(ctypes.c_void_p(handle))
		raise
	return process


def _create_suspended(argv: list[str], cwd: str, job_handle) -> Job:
	"""CREATE_SUSPENDED 建进程 → 塞进作业对象 → 才开始跑。

	三步之间的顺序不能动:先 Resume 的话,进程在"还没进作业对象"的那段时间里
	就能 fork 出不受控的孩子,而那种孩子既杀不掉也等不到 EOF(见文件上面那段)。
	"""
	import msvcrt
	import subprocess as _sp

	out_r, out_w = os.pipe()
	err_r, err_w = os.pipe()
	# 子进程要继承的是**写端**,而 os.pipe 给的 fd 默认不可继承(PEP 446)。
	# 不设的话 CreateProcessW 拿到的是一对无效的 std 句柄,而它的表现是
	# 子进程的输出直接消失 —— 不报错,只是什么都没有。
	for fd in (out_w, err_w):
		os.set_handle_inheritable(msvcrt.get_osfhandle(fd), True)

	# stdin 接 NUL:后台命令不该能在没人应答的线程上读标准输入。不接的话
	# 子进程继承的是服务进程的 stdin,一条 `read x` 会把整棵树挂在那儿等,
	# 直到超过 TIMEOUT_SECONDS 才被杀。
	null_fd = os.open(os.devnull, os.O_RDONLY)
	os.set_handle_inheritable(msvcrt.get_osfhandle(null_fd), True)

	startup = _STARTUPINFOW()
	startup.cb = ctypes.sizeof(_STARTUPINFOW)
	startup.dwFlags = STARTF_USESTDHANDLES
	startup.hStdInput = msvcrt.get_osfhandle(null_fd)
	startup.hStdOutput = msvcrt.get_osfhandle(out_w)
	startup.hStdError = msvcrt.get_osfhandle(err_w)

	info = _PROCESS_INFORMATION()
	# list2cmdline 是 subprocess 自己那套引号规则 —— 手写一份的话,带空格和
	# 引号的参数会在某一天开始被拆错,而那种错只在这条命令上出现。
	command_line = ctypes.create_unicode_buffer(_sp.list2cmdline(argv))
	ok = _kernel32.CreateProcessW(
		None, command_line, None, None, True, CREATE_SUSPENDED, None,
		str(cwd), ctypes.byref(startup), ctypes.byref(info))

	# 父进程手里这三份句柄必须关掉:写端留着的话读端永远等不到 EOF(写端还有
	# 人攥着),表现是后台命令跑完了、输出也读到了,可读线程就是不结束。
	os.close(out_w)
	os.close(err_w)
	os.close(null_fd)

	if not ok:
		os.close(out_r)
		os.close(err_r)
		raise OSError(
			f"CreateProcess 失败(错误码 {ctypes.get_last_error()}):{argv[0]}")

	try:
		if not _kernel32.AssignProcessToJobObject(
				ctypes.c_void_p(job_handle), ctypes.c_void_p(info.hProcess)):
			# 塞不进作业对象 = 这棵树将来收不干净。**这时候必须放弃**,
			# 而不是"照跑不误,大不了收不干净" —— 后者会让一条 sleep 链
			# 活过服务进程,而用户没有任何地方能看到它。
			#
			# 放弃要放弃彻底:进程还挂在 CREATE_SUSPENDED 上,不杀它的话
			# 它会以"挂起"的姿态永远留在任务管理器里,谁也清理不掉。
			_kernel32.TerminateProcess(ctypes.c_void_p(info.hProcess), 1)
			raise OSError(
				f"AssignProcessToJobObject 失败"
				f"(错误码 {ctypes.get_last_error()})")
		_kernel32.ResumeThread(ctypes.c_void_p(info.hThread))
	except BaseException:
		os.close(out_r)
		os.close(err_r)
		_kernel32.CloseHandle(ctypes.c_void_p(info.hProcess))
		raise
	finally:
		_kernel32.CloseHandle(ctypes.c_void_p(info.hThread))

	return Job(job_handle, info, out_r, err_r)


def wait_process(job: Job, timeout: float) -> int | None:
	"""等进程结束。超时返回 None(**不是**返回一个退出码)。

	超时给 None 而不是某个哨兵整数:调用方要据此去 TerminateJobObject,而
	"我以为它跑完了"这条路上任何整数都会被当成退出码写进结果。
	"""
	millis = INFINITE if timeout is None else int(timeout * 1000)
	rc = _kernel32.WaitForSingleObject(
		ctypes.c_void_p(job.process.hProcess), millis)
	if rc == WAIT_TIMEOUT:
		return None
	code = ctypes.c_ulong()
	_kernel32.GetExitCodeProcess(ctypes.c_void_p(job.process.hProcess),
	                             ctypes.byref(code))
	return code.value


def close_process(job: Job) -> None:
	"""关掉进程句柄。**收完进程树才算收干净**,这里只管句柄本身。"""
	if job.process is not None and job.process.hProcess:
		_kernel32.CloseHandle(ctypes.c_void_p(job.process.hProcess))
		job.process.hProcess = None


def drain(fd: int, sink: Path, limit: int) -> None:
	"""把 fd 上的输出**边读边写进 sink**,最多 limit 个字符。给一条线程跑。

	这是"不把整段输出攒在内存里"那件事的落点:sink 列表(旧实现)换成受控
	文件之后,一个刷屏的后台任务占的是磁盘而不是进程的堆,而且服务进程退出
	时它已经落了一半 —— 不是全丢。

	超限之后**继续读、只是不写**:停读的话写端会被管道缓冲塞满,子进程就卡在
	write 上,于是"输出太多"变成了"命令永远跑不完"。读干净、丢掉,是这里
	唯一正确的做法。

	任何读错误(管道被强关、句柄先一步失效)都直接收工:这条线程的产物是
	文件,而文件里已经有的部分就是结论。
	"""
	written = 0
	try:
		with open(sink, "wb") as fh:
			while True:
				try:
					chunk = os.read(fd, 65536)
				except OSError:
					return
				if not chunk:
					return
				if written < limit:
					take = chunk[:limit - written]
					fh.write(take)
					written += len(take)
	finally:
		try:
			os.close(fd)
		except OSError:
			pass
