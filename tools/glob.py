import glob as globlib

from config import WORKDIR
from tools.base import ToolDesc


def run_glob(pattern: str) -> str:
	try:
		matches = sorted({
			match for match in globlib.glob(pattern, root_dir=WORKDIR, recursive=True)
			if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
		})
		shown = matches[:200]
		if len(matches) > 200:
			shown.append("... (more matches omitted; narrow the pattern)")
		return "\n".join(shown) if shown else "(no matches)"
	except Exception as e:
		return f"Error: {e}"


glob = ToolDesc(
	name="glob",
	description="List files matching a glob pattern. Use ** to match across directories.",
	input_schema={
		"type": "object",
		"properties": {
			"pattern": {
				"type": "string",
				"description": "Glob pattern, e.g. '*.py' or '**/*.py'.",
			},
		},
		"required": ["pattern"],
	},
	handler=run_glob,
	# 只读:重发一次无害,所以不用两阶段标记(见 tools/base.py 的 side_effect)。
	side_effect=False,
)

