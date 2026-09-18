import shutil
from pathlib import Path

WORKDIR = Path.cwd().resolve()

BASH = shutil.which("bash") or "bash"

# 一轮 = 一次 API 调用 + 它要的那些工具。主 agent 和子 agent 共用,
# 免得跟 MODEL 一样在两个地方各写一份。
MAX_ROUNDS = 50

# 压缩用的两个目录。都放在 WORKDIR 里 —— 模型得能拿 bash/read_file 去读
# 落盘的工具结果,出了 WORKDIR 就够不着(还会被 permission_hook 拦)。
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
