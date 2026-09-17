import shutil
from pathlib import Path

WORKDIR = Path.cwd().resolve()

BASH = shutil.which("bash") or "bash"

# 一轮 = 一次 API 调用 + 它要的那些工具。主 agent 和子 agent 共用,
# 免得跟 MODEL 一样在两个地方各写一份。
MAX_ROUNDS = 50
