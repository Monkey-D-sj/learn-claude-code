import shutil
from pathlib import Path

WORKDIR = Path.cwd().resolve()

BASH = shutil.which("bash") or "bash"
