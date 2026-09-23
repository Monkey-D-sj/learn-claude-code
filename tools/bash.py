import subprocess

from config import BASH, WORKDIR
from tools.base import ToolDesc

# 输出上限。必须远高于落盘的阈值(LARGE_RESULT_CHAR_LIMIT = 30000),
# 否则那边存到的还是残的 —— 在这儿切一刀,落盘就永远拿不到完整内容。
#
# 它挡不住内存:capture_output=True 走 communicate(),把管道读到 EOF,
# 全部输出本来就已经在内存里了,切片只是切字符串。
#
# **TIMEOUT_SECONDS 只管得住直接子进程。** bash 被杀掉之后,它留下的孙子进程
# 还攥着那个管道,run() 收尾时照样要等到 EOF —— 实测 timeout 设 0.5 秒遇到
# `sleep 5`,5.1 秒才返回(状态头说 timeout 0.5s,没错,但这句话是 4.6 秒后
# 才送到模型手里的)。要连孙子一起收掉,得换成能控制整个进程组的执行方式,
# 那是"取消"那件事的活,不在这儿做。
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


def run_bash(command: str) -> str:
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


bash = ToolDesc(
	name="bash",
	description="Run a bash command.",
	input_schema={
		"type": "object",
		"properties": {
			"command": {
				"type": "string",
				"description": "The bash command to run.",
			},
		},
		"required": ["command"],
	},
	handler=run_bash,
)
