"""Manage the project's SKILL.md files; loading a skill stays in tools.skill."""

import os
import re
import tempfile
from pathlib import Path

from config import WORKDIR
from tools.base import ToolDesc
from tools.filelock import path_lock
from tools.skill import SKILLS_DIR, _parse

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


def _path(root: Path, name: str) -> Path | None:
	if not isinstance(name, str) or not _NAME.fullmatch(name):
		return None
	if name.split(".")[0] in _WINDOWS_RESERVED:
		return None
	path = root / name / "SKILL.md"
	# A linked directory/file can alias another skill or escape the skill tree.
	if path.parent.is_symlink() or path.is_symlink():
		return None
	if not path.resolve().is_relative_to(root.resolve()):
		return None
	return path


def _valid_description(description: str | None) -> bool:
	return (isinstance(description, str) and bool(description.strip())
	        and len(description.splitlines()) == 1
	        and not any(ord(c) < 32 or ord(c) == 127 for c in description))


def _valid_content(content: str | None) -> bool:
	return isinstance(content, str) and bool(content.strip())


def _render(description: str, content: str) -> str:
	return f"---\ndescription: {description.strip()}\n---\n\n{content.strip()}\n"


def _updated_text(original: str, description: str | None,
				  content: str | None) -> str:
	"""Change only requested fields, preserving other frontmatter and body text."""
	if not original.startswith("---\n") or "\n---" not in original[4:]:
		return _render(description or "", content or original)
	_, _, rest = original.partition("\n")
	meta, _, tail = rest.partition("\n---")
	if description is not None:
		lines = [line for line in meta.splitlines()
		         if line.partition(":")[0].strip() != "description"]
		lines.insert(0, f"description: {description.strip()}")
		meta = "\n".join(lines)
	if content is not None:
		tail = "\n\n" + content.strip() + "\n"
	return "---\n" + meta + "\n---" + tail


def _write_atomic(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = None
	try:
		with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
		                                 dir=path.parent, prefix=".SKILL-", suffix=".tmp",
		                                 delete=False) as stream:
			tmp = Path(stream.name)
			stream.write(text)
		os.replace(tmp, path)
	finally:
		if tmp is not None:
			tmp.unlink(missing_ok=True)


def _manage(root: Path, action: str, name: str = "",
			 description: str | None = None, content: str | None = None) -> str:
	action = (action or "").strip().lower()
	if action == "list":
		if not root.resolve().is_relative_to(WORKDIR):
			return "Error: skills path points outside the workspace."
		found = []
		for path in sorted(root.glob("*/SKILL.md")):
			if _path(root, path.parent.name) != path or not path.is_file():
				continue
			fields, _ = _parse(path)
			found.append(f"- {path.parent.name}: {fields.get('description', '')}")
		return "\n".join(found) or "(no skills)"
	if action not in {"create", "update", "delete"}:
		return "Error: action must be list, create, update, or delete."
	path = _path(root, name)
	if path is None or not root.resolve().is_relative_to(WORKDIR):
		return "Error: invalid skill name or path. Use lowercase letters, digits, _ or -."
	with path_lock(path):
		# Recheck under the lock in case an existing directory changed meanwhile.
		if _path(root, name) != path:
			return "Error: skill path changed or points outside skills/."
		if action == "create":
			if path.exists() or path.is_symlink() or path.parent.exists():
				return f"Error: skill {name!r} already exists. Use update."
			if not _valid_description(description) or not _valid_content(content):
				return "Error: create requires a one-line description and nonempty content."
			_write_atomic(path, _render(description, content))
			return (f"Created skill {name!r}. It can be loaded with skill now; "
			        "restart the server to refresh the system-prompt skill list.")
		if not path.is_file() or path.is_symlink():
			return f"Error: unknown skill {name!r}."
		if action == "delete":
			if any(item.name != "SKILL.md" for item in path.parent.iterdir()):
				return "Error: skill has supporting files; remove them explicitly before deleting it."
			path.unlink()
			path.parent.rmdir()
			return (f"Deleted skill {name!r}. Restart the server to refresh "
			        "the system-prompt skill list.")
		if description is None and content is None:
			return "Error: update requires description or content."
		if description is not None and not _valid_description(description):
			return "Error: description must be a nonempty single line."
		if content is not None and not _valid_content(content):
			return "Error: content must be nonempty."
		original = path.read_text(encoding="utf-8")
		fields, old_content = _parse(path)
		new_description = description if description is not None else fields.get("description", "")
		new_content = content if content is not None else old_content
		if not _valid_description(new_description) or not _valid_content(new_content):
			return "Error: existing skill needs a description and nonempty content."
		_write_atomic(path, _updated_text(original, description, content))
		return (f"Updated skill {name!r}. Its body can be loaded with skill now; "
		        "restart the server to refresh its system-prompt description.")


def run_skill_manage(action: str, name: str = "", description: str | None = None,
					 content: str | None = None) -> str:
	return _manage(SKILLS_DIR, action, name, description, content)


skill_manage_tool = ToolDesc(
	name="skill_manage",
	description=(
		"Manage reusable procedures in skills/<name>/SKILL.md. "
		"list shows the live name/description catalog; create adds a skill; "
		"update changes its description and/or full body; delete removes a skill "
		"only when it has no supporting files. Use skill to load a body's text. "
		"Create for a user request or a verified reusable workflow with no "
		"matching skill; update an existing skill when verified work finds it "
		"wrong or incomplete. Delete only at the user's request. "
		"The system-prompt catalog is fixed at server startup; list is live."
	),
	input_schema={
		"type": "object",
		"properties": {
			"action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
			"name": {"type": "string", "description": "Skill directory name; required except for list."},
			"description": {"type": "string", "description": "One-line purpose; required for create."},
			"content": {"type": "string", "description": "Full SKILL.md body without frontmatter; required for create."},
		},
		"required": ["action"],
	},
	handler=run_skill_manage,
)
