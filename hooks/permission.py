from config import WORKDIR

DENY_LIST = [
	"rm -rf /", "sudo", "shutdown", "reboot",
	"mkfs", "dd if=", "> /dev/sda",
]


def permission_hook(block):
	if block.name == "bash":
		for pattern in DENY_LIST:
			if pattern in block.input.get("command", ""):
				return "Permission denied by deny list"
	if block.name in ("read_file", "write_file", "edit_file"):
		path = block.input.get("path", "")
		if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
			choice = input("   Allow? [y/N] ").strip().lower()
			if choice not in ("y", "yes"):
				return "Permission denied by user"
	return None
