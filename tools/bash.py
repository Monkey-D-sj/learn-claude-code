import subprocess
import threading
from pathlib import Path

import jobs
from config import BASH, JOBS_DIR, WORKDIR
from tools.background import Refused, launch, placeholder
from tools.base import ToolDesc

# 输出上限。必须远高于落盘的阈值(LARGE_RESULT_CHAR_LIMIT = 30000),
# 否则那边存到的还是残的 —— 在这儿切一刀,落盘就永远拿不到完整内容。
#
# 它挡不住内存:capture_output=True 走 communicate(),把管道读到 EOF,
# 全部输出本来就已经在内存里了,切片只是切字符串。
#
# **后台那条路不走这儿,它边读边落盘**(见下面"第二套实现"),堆的是磁盘不是
# 内存 —— 但截断这道闸是同一个数,两条路共用这个常量。
#
# **TIMEOUT_SECONDS 在同步那条路上只管得住直接子进程。** bash 被杀掉之后,它留下
# 的孙子进程还攥着那个管道,run() 收尾时照样要等到 EOF —— 实测 timeout 设 0.5 秒
# 遇到 `sleep 5`,5.1 秒才返回(状态头说 timeout 0.5s,没错,但这句话是 4.6 秒后
# 才送到模型手里的)。**后台那条路没有这个坑**:整棵树从第一条指令起就在作业对象
# 里,超时是一个 TerminateJobObject,连孙子一起收。同步这条路照旧 —— 要它也变,
# 得把进程模型整个换成下面那一套,那是另一件事。
#
# 限的是 stdout + stderr 那两段,**不含状态头** —— 状态头是唯一必须活下来
# 的东西,不能跟着正文一起被切掉(见 _format)。
#
# 截断标注写在**尾部** —— 落盘的预览是头 + 尾(_preview),所以模型看得见。
#
# 已知的边界(不是这次修的东西,别以为已经处理了):stdout 单独就冲到 40 万字符
# 时,被切掉的是尾部,也就是**整个 stderr 段连同它里面的报错**。状态头活得下来
# (它在第一行),但"为什么失败"那句话没了。要真处理得改成边读边落盘,不跟退出码
# 这件事捆在一起。
MAX_OUTPUT_CHARS = 400000

# 超时给 120 秒。超时是"没跑完",不是"命令返回了非零" —— 副作用可能只做了
# 一半,所以它走独立的状态,不借一个退出码糊过去。
TIMEOUT_SECONDS = 120

_NO_OUTPUT = "(no output)"


def _text(raw) -> str:
	"""超时异常里带出来的那半截输出。

	文本模式下本该是 str,但异常对象上这个字段没有保证,裸 bytes 拼进
	f-string 会变成 `b'...'` —— 那比没有更误导。空值统一成空串。

	部分输出常常是唯一的线索(卡住之前它已经打了什么),所以不丢。
	"""
	if not raw:
		return ""
	if isinstance(raw, bytes):
		return raw.decode("utf-8", errors="replace")
	return raw


def _format(status: str, stdout: str = "", stderr: str = "") -> str:
	"""状态头 + 分开的 stdout / stderr。

	**状态头必须在第一行。** 落盘后的预览是头 2000 + 尾 300 字符,一个刷屏的
	构建日志足以把尾部冲出预览之外;放头部则怎么也冲不掉。这同时是原来
	"(no output)" 那个坑的一半:`exit 0` 和 `exit 1` 都无输出时,两条结果逐字节
	相同,模型只能按"没报错就是成了"来猜。

	**stdout 和 stderr 分开写,也别拿"有没有 stderr"当失败判据。** stderr 有
	内容是常态而非异常(git、编译器、pytest 的 warning 都走 stderr),按它判失败
	会在另一个方向上骗人。分开写还顺带把 stderr 留在最后,而预览留了尾巴 ——
	报错、traceback、汇总行本来就都落在那儿。

	段标跟在首行上,不占一整行。理由在页面那边:折叠着的工具结果拿 headline()
	当摘要,而那是**头两行**(ui/index.html)—— 标独占一行的话,每一格摘要都变成
	"status: exit 1 / stderr:",报错本身反而看不见了。
	另一条路是干脆不标,那 stdout 和 stderr 又混成一段(原来就是这样)。

	空的段不写:成功又没输出时,多两行空壳只是噪声。
	"""
	sections = []
	if stdout:
		sections.append(f"stdout: {stdout}")
	if stderr:
		sections.append(f"stderr: {stderr}")
	body = "\n".join(sections) if sections else _NO_OUTPUT
	if len(body) > MAX_OUTPUT_CHARS:
		body = (f"{body[:MAX_OUTPUT_CHARS]}\n"
		        f"... [truncated: {len(body)} chars total]")
	return f"status: {status}\n{body}"


def _sync(command: str) -> str:
	"""同步执行。**这一版之前 run_bash 的全部内容,一字未改。**"""
	try:
		r = subprocess.run([BASH, "-c", command], cwd=WORKDIR,
						   capture_output=True, text=True, encoding="utf-8",
						   errors="replace", timeout=TIMEOUT_SECONDS)
	except subprocess.TimeoutExpired as e:
		return _format(f"timeout after {TIMEOUT_SECONDS}s (killed)",
		               _text(e.stdout).strip(), _text(e.stderr).strip())
	except OSError as e:
		# 起不来(找不到 bash、权限、cwd 不在):命令**根本没执行过**,跟"执行了
		# 但失败"必须分开 —— 前者重试有意义,后者要改命令。
		return _format(f"failed to start ({e})")
	return _format(f"exit {r.returncode}",
	               r.stdout.strip(), r.stderr.strip())


# ---------------------------------------------------------------- 第二套实现

# **上面那条是同步的,下面这条是后台的 —— 两份代码,不是一个开关。**
#
# 差别不是"加个线程池":两条路要的进程模型根本不一样。同步那条只要"命令跑完、
# 输出拿回来",`subprocess.run` + 两段内存里的字符串正合适;后台这条要的是
#
#   1. 进程树**从第一条指令起**就被关住(见 jobs.py 那一大段),否则服务退出时
#      收不干净;
#   2. 输出**边读边落盘** —— 同步那条把整段攒在内存里(communicate 读到 EOF
#      才开始处理),而后台命令可以跑很久、吐很多,那些字符串会一直堆着;
#   3. 结果要先落成一个文件,因为进上下文的那一份只能是摘要。
#
# 共用的只有三个东西,而且必须共用:`TIMEOUT_SECONDS`、`MAX_OUTPUT_CHARS`,
# 以及 `_format`。前两个各写一份的话,两条路的超时和截断会慢慢分家;`_format`
# 各写一份的话,同一个退出码在两条路上会长成两种样子,而模型读的是同一个东西。
#
# 同步那条已知的坑照旧(见文件头):TIMEOUT_SECONDS 只管得住直接子进程,
# 收尾时还是要等管道 EOF。**后台这条没有那个坑** —— 整棵树在作业对象里,
# 超时是 TerminateJobObject,连孙子一起。

# 直接子进程结束之后,还给读线程多少秒去把管道里剩的读完。
#
# 为什么需要一个上限:命令可以留一个后台进程攥着管道写端(`server &` 就是
# 这种写法),那时候 EOF 永远不来,读线程会一直挂着。**不能因此去杀那棵树** ——
# "留下一个后台进程"是这条命令的正当意图,不是失控。所以到点就收工,把已经
# 读到的部分当结果,并且在摘要里说清楚尾巴可能不全。
READER_GRACE_SECONDS = 5.0

# 超时之后等进程树消失多久。TerminateJobObject 是异步的,不等一下就往下走的话,
# 读线程还攥着管道,后面读文件会读到正在被写的半截。
KILL_GRACE_SECONDS = 5.0


def _preview(body: str) -> str:
	"""摘要:正文的第一段。进上下文和数据库的是它,不是全文。"""
	if len(body) <= jobs.SUMMARY_CHARS:
		return body
	return (f"{body[:jobs.SUMMARY_CHARS]}\n"
	        f"... [完整输出在结果文件里]")


def _sink_read(path: Path) -> str:
	"""把落盘的那一段读回来。读不到给空串。

	读不到不算异常:文件可能压根没建(`true` 这种没有任何输出的命令,
	drain 那条线程仍然会建一个空文件,但建失败也不会让这一次执行失败)。
	"""
	try:
		return path.read_text(encoding="utf-8", errors="replace")
	except OSError:
		return ""


def _background_work(command: str):
	"""造一个 work 函数,交给 jobs.start 在新线程里跑。

	分两层是因为 jobs.py 只认机制、不认 bash(见它的文件头):那个模块要的是
	一个 work(job_id, ctx) -> JobResult,而"bash 的后台执行"是这一层的事。
	"""
	def work(job_id: str, ctx) -> jobs.JobResult:
		JOBS_DIR.mkdir(parents=True, exist_ok=True)
		# 中间文件按 job_id 命名,跟最终结果同一个命名空间。**不拿命令或会话 id
		# 拼路径** —— 那些是外面来的字符串,拼进路径就多一条把文件写到别处的路,
		# 而那条路上没有任何地方会报错。
		out_path = JOBS_DIR / f"{job_id}.out"
		err_path = JOBS_DIR / f"{job_id}.err"

		try:
			proc = jobs.spawn_in_job([BASH, "-c", command], str(WORKDIR))
		except OSError as e:
			# 起不来跟同步那条路一个口径:命令**根本没执行过**,所以它是 failed,
			# 而不是一条"输出为空"的完成结果。
			return jobs.JobResult("failed", f"failed to start ({e})", "",
			                      f"failed to start ({e})")

		threads = [
			threading.Thread(target=jobs.drain,
			                 args=(proc.stdout_fd, out_path, MAX_OUTPUT_CHARS),
			                 daemon=True),
			threading.Thread(target=jobs.drain,
			                 args=(proc.stderr_fd, err_path, MAX_OUTPUT_CHARS),
			                 daemon=True),
		]
		for thread in threads:
			thread.start()

		code = jobs.wait_process(proc, TIMEOUT_SECONDS)
		timed_out = code is None
		if timed_out:
			# **收的是整棵树,不是那个直接子进程。** TerminateProcess 只杀得到
			# 一层,而 msys 的 bash 会 fork 出孙子 —— 实测收完两层 bash,
			# 那条 sleep 还在,管道写端也还攥在它手里。
			proc.kill()
			code = jobs.wait_process(proc, KILL_GRACE_SECONDS)

		for thread in threads:
			thread.join(timeout=READER_GRACE_SECONDS)
		tail_lost = any(thread.is_alive() for thread in threads)

		stdout, stderr = _sink_read(out_path), _sink_read(err_path)
		# 中间文件用完就删:留着的话,一个跑得多一点的会话会在 JOBS_DIR 里
		# 堆下两倍的份数,而它们只是同一份结果的两半。
		for path in (out_path, err_path):
			try:
				path.unlink()
			except OSError:
				pass

		status = (f"timeout after {TIMEOUT_SECONDS}s (killed)" if timed_out
		          else f"exit {code}")
		body = _format(status, stdout.strip(), stderr.strip())
		if tail_lost:
			# 说清楚丢的是**尾巴**,以及为什么:命令留下了一个还活着的进程攥着
			# 输出管道。不说的话,模型会拿一份断在中间的日志当成完整日志读 ——
			# 而"日志到这里就没了"和"日志到这儿就结束了"是两回事。
			body += ("\n[输出没读到底:这条命令留下了还在运行的进程,"
			         "管道没关;上面是已经读到的部分]")

		error = (f"timeout after {TIMEOUT_SECONDS}s" if timed_out
		         else f"failed to start" if code is None else None)
		# 退出码非零**不算 job 失败**:那是命令自己的结果,模型要看的是它的输出
		# (跟同步那条路同一个判断,见 _format 上面那段)。只有"没能启动"和
		# "超时被杀"才是 failed。
		status = "failed" if (timed_out or code is None) else "completed"
		return jobs.JobResult(status, _preview(body), body, error)

	return work


def run_bash(command: str, run_in_background: bool = False) -> str:
	"""bash 工具的唯一入口。默认同步,语义跟以前一字不差。

	参数**不给"默认后台"那种口子**:后台执行不代表"可以安全并发改同一个文件",
	所以必须是模型显式要的。调用方(agent 循环)把 `run_in_background` 原样
	从 block.input 传下来,没有任何地方替它决定。

	后台那条路上的失败**不是异常**:容量满、没有会话归属、进程创建失败,
	都由 tools/background.py 收成一句给模型看的话。工具 handler 只有字符串
	一个出口,失败的形状也必须是它。
	"""
	if not run_in_background:
		return _sync(command)
	try:
		job_id = launch("bash", _background_work(command))
	except Refused as e:
		return f"Error: {e}"
	return placeholder(job_id, "bash")


bash = ToolDesc(
	name="bash",
	description=(
		"Run a bash command. It runs in the current working directory and "
		"returns the exit status plus stdout and stderr.\n"
		"Set run_in_background to true for a command that takes a while and "
		"that nothing else in this turn depends on: you get a job_id back "
		"immediately and can keep working. The result is delivered to you "
		"automatically when it finishes - do not poll for it. Only put work "
		"in the background when it does not touch the same files as what you "
		"are doing meanwhile."
	),
	input_schema={
		"type": "object",
		"properties": {
			"command": {
				"type": "string",
				"description": "The bash command to run.",
			},
			"run_in_background": {
				"type": "boolean",
				"description": "Run it in the background and return a job_id "
				               "right away. Default false (wait for it).",
			},
		},
		"required": ["command"],
	},
	handler=run_bash,
)
