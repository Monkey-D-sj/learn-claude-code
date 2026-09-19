from config import WORKDIR


def context_inject_hook(query: str) -> str | None:
	"""Inject current working directory info into every prompt."""
	print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
	return None   # return None = no modification, let prompt through


def log_hook(block, ask):
	# ask 收下不用:PreToolUse 的 hook 都拿同一组参数,而这里只需要 block。
	# 写成 (block, ask) 而不是加默认值,是为了让签名跟 permission_hook 一致
	# —— trigger_hooks 按事件分发,同事件的回调签名必须对齐。
	print(f"[HOOK] {block.name}(...)")


def large_output_hook(block, output):
	if len(str(output)) > 100000:
		print(f"[HOOK] ⚠ Large output from {block.name}")


def summary_hook(messages: list) -> str | None:
	"""Print a summary when the loop is about to stop."""
	tool_count = sum(
		1 for m in messages
		for b in (m.get("content") if isinstance(m.get("content"), list) else [])
		if isinstance(b, dict) and b.get("type") == "tool_result"
		)
	print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
	return None  # return None = allow stop, return string = force continuation
