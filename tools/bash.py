import subprocess

from config import BASH, WORKDIR
from tools.base import ToolDesc

# 输出上限。必须远高于落盘的阈值(LARGE_RESULT_CHAR_LIMIT = 30000),
# 否则那边存到的还是残的 —— 在这儿切一刀,落盘就永远拿不到完整内容。
#
# 它挡不住内存:capture_output=True 走 communicate(),把管道读到 EOF,
# 全部输出本来就已经在内存里了,切片只是切字符串。真正兜住内存的是
# timeout=120。
#
# 截断标注写在**尾部** —— 落盘的预览是头 + 尾(_preview),所以模型看得见。
MAX_OUTPUT_CHARS = 400000


def run_bash(command: str) -> str:
	try:
		r = subprocess.run([BASH, "-c", command], cwd=WORKDIR,
						   capture_output=True, text=True, encoding="utf-8",
						   errors="replace", timeout=120)
		out = (r.stdout + r.stderr).strip()
		if not out:
			return "(no output)"
		if len(out) > MAX_OUTPUT_CHARS:
			return (f"{out[:MAX_OUTPUT_CHARS]}\n"
			        f"... [truncated: {len(out)} chars total]")
		return out
	except subprocess.TimeoutExpired:
		return "Error: Timeout (120s)"
	except OSError as e:
		return f"Error: {e}"


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
