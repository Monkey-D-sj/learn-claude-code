from config import WORKDIR
from tools.base import ToolDesc


def run_write(path: str, content: str) -> str:
	try:
		file_path = (WORKDIR / path).resolve()
		file_path.parent.mkdir(parents=True, exist_ok=True)
		file_path.write_text(content, encoding="utf-8")
		return f"Wrote {len(content)} bytes to {path}"
	except Exception as e:
		return f"Error: {e}"


write_file = ToolDesc(
	name="write_file",
	description="Write text to a file, overwriting it if it already exists.",
	input_schema={
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Path to the file to write.",
			},
			"content": {
				"type": "string",
				"description": "Full content to write to the file.",
			},
		},
		"required": ["path", "content"],
	},
	handler=run_write,
)
