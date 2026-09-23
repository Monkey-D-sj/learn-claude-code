import importlib

import pytest

import tools.skill_manage as manage
from tools import BASE_TOOLS
from tools.skill import run_skill

skill_module = importlib.import_module("tools.skill")


@pytest.fixture
def skill_root(tmp_path, monkeypatch):
	root = tmp_path / "skills"
	monkeypatch.setattr(manage, "WORKDIR", tmp_path)
	return root


def test_registered_and_create_load_update_delete(skill_root, monkeypatch):
	assert "skill_manage" in {tool.name for tool in BASE_TOOLS}
	assert manage._manage(skill_root, "list") == "(no skills)"
	assert "Created" in manage._manage(
		skill_root, "create", "sample", "A sample procedure", "# Steps\nDo the thing.")
	assert "sample: A sample procedure" in manage._manage(skill_root, "list")
	monkeypatch.setattr(skill_module, "SKILLS_DIR", skill_root)
	assert run_skill("sample") == "# Steps\nDo the thing.\n"
	assert "Updated" in manage._manage(
		skill_root, "update", "sample", content="# Revised")
	assert "description: A sample procedure" in (skill_root / "sample" / "SKILL.md").read_text()
	assert run_skill("sample") == "# Revised\n"
	assert "Deleted" in manage._manage(skill_root, "delete", "sample")
	assert not (skill_root / "sample").exists()


def test_update_preserves_unrequested_fields(skill_root):
	path = skill_root / "sample" / "SKILL.md"
	path.parent.mkdir(parents=True)
	path.write_text("---\ndescription: Old\nauthor: Person\n---\n\nBody with space  \n",
	                encoding="utf-8")
	assert "Updated" in manage._manage(
		skill_root, "update", "sample", description="New")
	assert path.read_text(encoding="utf-8") == (
		"---\ndescription: New\nauthor: Person\n---\n\nBody with space  \n")
	assert "Updated" in manage._manage(
		skill_root, "update", "sample", content="New body")
	assert "author: Person" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["../other", "x/y", "x\\y", "CON", "", "Bad Name"])
def test_invalid_name_cannot_create(skill_root, name):
	assert manage._manage(
		skill_root, "create", name, "Description", "Body").startswith("Error:")
	assert not skill_root.exists()


def test_rejects_symlink_and_keeps_supporting_files(skill_root, tmp_path):
	outside = tmp_path / "outside"
	outside.mkdir()
	skill_root.mkdir()
	try:
		(skill_root / "linked").symlink_to(outside, target_is_directory=True)
	except (OSError, NotImplementedError):
		pytest.skip("directory symlinks unavailable")
	assert manage._manage(
		skill_root, "create", "linked", "Description", "Body").startswith("Error:")
	assert not (outside / "SKILL.md").exists()
	assert "Created" in manage._manage(
		skill_root, "create", "sample", "Description", "Body")
	(skill_root / "sample" / "asset.txt").write_text("keep", encoding="utf-8")
	assert manage._manage(skill_root, "delete", "sample").startswith("Error:")
	assert (skill_root / "sample" / "SKILL.md").exists()
