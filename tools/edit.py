from config import WORKDIR
from tools.base import ToolDesc


def run_edit(path: str, old_string: str, new_string: str) -> str:
	try:
		file_path = (WORKDIR / path).resolve()
		text = file_path.read_text(encoding="utf-8")
		n = text.count(old_string)
		if n == 0:
			return f"Error: text not found in {path}"
		if n > 1:
			return f"Error: text appears {n} times in {path}; make it unique"
		file_path.write_text(text.replace(old_string, new_string), encoding="utf-8")
		return f"Edited {path}"
	except Exception as e:
		return f"Error: {e}"


edit_file = ToolDesc(
	name="edit_file",
	description=(
		"Replace old_string with new_string in a file. "
		"old_string must appear exactly once, otherwise nothing changes."
	),
	input_schema={
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Path to the file to edit.",
			},
			"old_string": {
				"type": "string",
				"description": "Exact text to replace. Must be unique in the file.",
			},
			"new_string": {
				"type": "string",
				"description": "Text to replace it with.",
			},
		},
		"required": ["path", "old_string", "new_string"],
	},
	handler=run_edit,
)
