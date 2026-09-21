import time

from agent import agent_loop
from app import MODEL, SYSTEM, make_compactor
from config import MAX_ROUNDS
from emit import terminal_ask, terminal_ask_text, terminal_emit
from hooks import trigger_hooks
from tools import build_tools
from tools.todo import TodoManager
import usage

# 终端这个前端:事件打在这儿,压缩器的日志也打在这儿。
# 浏览器那个前端在 server.py,它建自己那个压缩器。
COMPACTOR = make_compactor(terminal_emit)

# 终端一个进程只有一个 agent,所以任务清单也是进程一份。
# 浏览器那个前端不是 —— 那边是每个会话一份,见 server.py。
TODO = TodoManager()
# ask 那个工具问出来的问题也打在这个终端上。终端这一个进程从头到尾就是一个
# 会话,所以提问器可以是模块级的那一个;浏览器那边不是 —— 那边得绑在"这一轮
# 那条响应流"上,每轮现造,见 server.py 的 make_ask_text。
TOOLS = build_tools(TODO, terminal_ask_text)

# 这次运行的名字。**不能写死成 "terminal"**:轮次序号每个进程都从 1 开始,
# 写死的话两次运行的第 1 轮会撞在一起 —— 报表把它们当同一轮加总,数字凭空
# 变大,而不报错。一个终端进程从头到尾就是一个会话,但**每跑一次是一个新会话**。
# 带时间戳是为了在报表里还认得出是哪一次(纯随机串读起来没有信息)。
SESSION = f"terminal-{time.strftime('%Y%m%d-%H%M%S')}"

if __name__ == "__main__":
	print("s01: Agent Loop")
	print("Enter a question, press Enter to send. Type q to quit.\n")

	history = []
	# 轮次序号,只给账本用。终端一个进程从头到尾就是一个会话,所以"第几轮"
	# 拿一个计数器就够了 —— 浏览器那边不是,它有真正的 turn_id,见 server.py。
	turn = 0
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
		turn += 1
		try:
			# span 里发生的每一次 API 调用都自动带上这些归属 —— 包括压缩器
			# 那次摘要,和工具里那些(vision)。传参穿不过工具 handler:
			# agent_loop 只给 handler 传 **block.input,所以只能用这一层环境。
			with usage.span(session=SESSION, turn=turn):
				outcome = agent_loop(history,
				                     active_request=query,
				                     system=SYSTEM,
				                     tools=TOOLS,
				                     model=MODEL,
				                     max_rounds=MAX_ROUNDS,
				                     compactor=COMPACTOR,
				                     ask=terminal_ask,
				                     emit=terminal_emit)
			print(outcome.text)
			# 终端这边不记库,"失败了"就没有第二个人知道 —— 得自己说。
			# 不说的话,轮数耗尽和一次正常回复在屏幕上长得一模一样,
			# 下一句提问还会接着一个其实没干完的上下文往下走。
			if outcome.status == "failed":
				print(f"\033[31m[这一轮没跑完: {outcome.error}]\033[0m")
			# 这一轮花了多少,当场就能看见 —— 不用等事后去跑 report.py。
			# 从账本读回来,不另攒一份:见 usage.read_turn 那段。
			# 灰色,跟工具输出一个色阶:它是诊断信息,不是模型说的话。
			line = usage.turn_line(usage.read_turn(SESSION, turn))
			if line:
				print(f"\033[90m{line}\033[0m")
		except Exception as e:
			# 兜底:任何异常都不该把 history 一起带走
			print(f"\033[31mError: {type(e).__name__}: {e}\033[0m")
		print()
