from hooks.base import HOOKS, register_hook, trigger_hooks
from hooks.builtin import (
	context_inject_hook,
	large_output_hook,
	log_hook,
	summary_hook,
)
from hooks.permission import permission_hook

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
