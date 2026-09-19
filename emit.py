"""终端渲染器:把一个事件打成终端能看的样子。

它是"终端这个前端"的展示层,跟 server.py 里那个"推给浏览器"的 emit
是一对 —— 两边拿到的是同一批事件,各自决定怎么显示。

展示层有两件事:把事件画出来(terminal_emit),和拿不准的时候问人
(terminal_ask)。后者也是前端的活 —— 浏览器那边问的是网页,不是终端,
所以 agent_loop 把它当参数收,而不是在 hook 里写死。

放在单独一个模块里,是因为有两个地方要用它:main.py 的 REPL,和子
agent。子 agent 的循环不往上抛事件(它的定位就是把过程藏起来,只回
一句结论),所以它自己那个前端就是终端。

本模块不 import 项目里任何东西 —— 它是叶子。tools/subagent.py 要用它,
而 tools/ 被 app.py 引用,往这儿引项目模块会绕成环。
"""

GRAY = "\033[90m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def terminal_emit(event: dict) -> None:
	"""把事件打到终端。四种 kind,跟 agent_loop / ContextCompactor 发的一致。"""
	kind = event.get("kind")

	if kind == "tool_call":
		print(f"{YELLOW}$ {event['name']} {event['input']}{RESET}")

	elif kind == "tool_result":
		# 也上色,不然它跟模型最后的回复在终端里分不出来 —— 一堆输出
		# 之后突然出现一段正文,看不出来哪些是工具吐的、哪些是模型说的。
		print(f"{GRAY}{event['output']}{RESET}")

	elif kind == "note":
		# rstrip 要在拼色之前。写成 f"... {text}{RESET}".rstrip() 是白写:
		# 空格后面跟着的是转义码,不是行尾,rstrip 够不着它。
		# 有些 note 只有 source 没有正文(比如轮数上限),不削就留个尾空格。
		line = f"[{event['source']}] {event['text']}".rstrip()
		print(f"{GRAY}{line}{RESET}")

	elif kind == "reply":
		print(event["text"])


def terminal_ask(question: str) -> bool:
	"""终端这个前端的确认器。返回值 = 放不放行。

	EOFError 必须当成拒绝,不能当成同意,也不能让它抛出去:stdin 被重定向
	或关掉时(管道、后台跑、某些 IDE)input() 直接 EOF —— 那条路径上如果
	默认放行,"把 stdin 关掉"就成了提权手段。
	"""
	try:
		reply = input(f"\033[33m{question}\033[0m\n   Allow? [y/N] ")
	except (EOFError, KeyboardInterrupt):
		print()
		return False
	return reply.strip().lower() in ("y", "yes")
