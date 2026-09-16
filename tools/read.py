from config import WORKDIR
from tools.base import ToolDesc


def run_read(path: str, limit: int | None = None) -> str:
	try:
		lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
		if limit and limit < len(lines):
			lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
		return "\n".join(lines)
	except Exception as e:
		return f"Error: {e}"


read_file = ToolDesc(
	name="read_file",
	description="Read a file and return its text.",
	input_schema={
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Path to the file to read.",
			},
			"limit": {
				"type": "integer",
				"description": "Optional maximum number of lines to return.",
			},
		},
		"required": ["path"],
	},
	handler=run_read,
)
