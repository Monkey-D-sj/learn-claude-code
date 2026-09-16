import subprocess

from config import BASH, WORKDIR
from tools.base import ToolDesc


def run_bash(command: str) -> str:
	try:
		r = subprocess.run([BASH, "-c", command], cwd=WORKDIR,
						   capture_output=True, text=True, encoding="utf-8",
						   errors="replace", timeout=120)
		out = (r.stdout + r.stderr).strip()
		return out[:50000] if out else "(no output)"
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
