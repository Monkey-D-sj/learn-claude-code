from agent import agent_loop
from app import MODEL, SYSTEM, make_compactor
from config import MAX_ROUNDS
from emit import terminal_emit
from hooks import trigger_hooks
from tools import TOOLS

# 终端这个前端:事件打在这儿,压缩器的日志也打在这儿。
# 浏览器那个前端在 server.py,它建自己那个压缩器。
COMPACTOR = make_compactor(terminal_emit)

if __name__ == "__main__":
	print("s01: Agent Loop")
	print("Enter a question, press Enter to send. Type q to quit.\n")

	history = []
	while True:
		try:
			# \001/\002 tell Readline the ANSI escapes have zero display width.
			query = input("\001\033[36m\002s01 >> \001\033[0m\002")
		except (EOFError, KeyboardInterrupt):
			break
		# stdin 重定向时,Python 按 locale 解码;凑不成合法序列的字节会被
		# surrogateescape 兜成孤代理项。那东西编码不进 API 请求体,
		# 会在 SDK 内部炸成 UnicodeEncodeError(不是 APIError,捕不到)。
		# 在这里洗掉,任何来源的坏字符都活不到发请求。
		query = query.encode("utf-8", "replace").decode("utf-8")
		if query.strip().lower() in ("q", "exit", ""):
			break
		trigger_hooks("UserPromptSubmit", query)
		history.append({"role": "user", "content": query})
		try:
			print(agent_loop(history,
			                 active_request=query,
			                 system=SYSTEM,
			                 tools=TOOLS,
			                 model=MODEL,
			                 max_rounds=MAX_ROUNDS,
			                 compactor=COMPACTOR,
			                 emit=terminal_emit))
		except Exception as e:
			# 兜底:任何异常都不该把 history 一起带走
			print(f"\033[31mError: {type(e).__name__}: {e}\033[0m")
		print()
