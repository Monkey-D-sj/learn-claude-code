from pathlib import Path

from config import WORKDIR
from tools.base import ToolDesc

# 技能是"该怎么做"的纯文本流程,不是"能做什么"的函数。
# 所以这里只有读文件的代码,没有 handler 注册。
SKILLS_DIR = WORKDIR / "skills"


def _parse(path: Path) -> tuple[dict[str, str], str]:
	"""拆开 SKILL.md 的 frontmatter 和正文。

	只认 `key: value` 这种单行标量,不引 yaml —— 多一个依赖不值当。
	没有 frontmatter 或没写闭合的 `---` 时,整个文件都当正文。
	"""
	text = path.read_text(encoding="utf-8")
	if not text.startswith("---"):
		return {}, text

	_, _, rest = text.partition("\n")
	meta, sep, body = rest.partition("\n---")
	if not sep:
		return {}, text

	fields = {}
	for line in meta.splitlines():
		key, colon, value = line.partition(":")
		if colon:
			fields[key.strip()] = value.strip()
	return fields, body.lstrip("\n")


def _find(name: str) -> Path | None:
	"""按名字找 SKILL.md。

	名字是模型给的,所以拿扫描出来的目录名当白名单,不接受拼路径 ——
	否则 name="../../.." 就能读到 WORKDIR 外面,跟 read_file 一个道理。
	"""
	for path in SKILLS_DIR.glob("*/SKILL.md"):
		if path.parent.name == name:
			return path
	return None


def discover() -> list[tuple[str, str]]:
	"""列出所有技能,返回 [(名字, 一句话描述)]。

	名字就是目录名,单一来源 —— frontmatter 里再写一遍 name 只会带来不一致。
	"""
	found = []
	for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
		fields, _ = _parse(path)
		found.append((path.parent.name, fields.get("description", "")))
	return found


def run_skill(name: str) -> str:
	path = _find(name)
	if path is None:
		available = ", ".join(n for n, _ in discover()) or "(none)"
		return f"Error: unknown skill {name!r}. Available: {available}"

	# 每次都重读:改完 SKILL.md 立刻生效,不用重启。
	# 清单是启动时拼进 system prompt 的,新增技能才需要重启。
	_, body = _parse(path)
	print(f"\n\033[35m[Skill loaded: {name}]\033[0m")
	return body or f"(skill {name!r} is empty)"


skill = ToolDesc(
	name="skill",
	description=(
		"Load a reusable procedure by name. The available skills and what each "
		"one is for are listed in your system prompt. Call this when the task "
		"at hand matches one of them, before you start improvising - the "
		"procedure encodes steps and pitfalls that are easy to get wrong. "
		"Returns the full text of the procedure; follow it."
	),
	input_schema={
		"type": "object",
		"properties": {
			"name": {
				"type": "string",
				"description": "Skill name exactly as listed in the system prompt.",
			},
		},
		"required": ["name"],
	},
	handler=run_skill,
	# 只读:重发一次无害,所以不用两阶段标记(见 tools/base.py 的 side_effect)。
	side_effect=False,
)

