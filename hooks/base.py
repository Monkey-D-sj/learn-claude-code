HOOKS = {
	"UserPromptSubmit": [],
	"PreToolUse": [],
	"PostToolUse": [],
	"Stop": [],
}


def register_hook(event: str, callback):
	HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
	for callback in HOOKS[event]:
		result = callback(*args)
		if result is not None:   # 返回值 ≠ None → hook 说"停"
			return result
	return None
