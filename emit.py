"""终端渲染器:把一个事件打成终端能看的样子。

它是"终端这个前端"的展示层,跟 server.py 里那个"推给浏览器"的 emit
是一对 —— 两边拿到的是同一批事件,各自决定怎么显示。

展示层有三件事:把事件画出来(terminal_emit),和拿不准的时候问人
(terminal_ask / terminal_ask_text)。后者也是前端的活 —— 浏览器那边问的是
网页,不是终端,所以 agent_loop / build_tools 把它当参数收,而不是写死。

问人有**两个**函数,不是一个带 mode 参数的:回答的类型不同,一个是 bool
(放不放行),一个是 str(说了什么)。合成一个的话,调用方得先看 mode 才知道
手里那个值是哪种,而漏判的时候不报错 —— `False` 和 `""` 都是 falsy,
`if answer:` 会把"人答了个空"和"没人答"读成同一件事。

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
	"""把事件打到终端。五种 kind,跟 agent_loop / ContextCompactor 发的一致。"""
	kind = event.get("kind")

	if kind == "tool_call":
		print(f"{YELLOW}$ {event['name']} {event['input']}{RESET}")

	elif kind == "thinking":
		# 推理:斜体 + 灰。跟工具输出(也是灰)靠斜体分开,跟正文靠灰分开 ——
		# 它是过程,不是结论。终端不认斜体的话就退化成灰,也能接受。
		print(f"\033[3m{GRAY}{event['text']}{RESET}")

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


def terminal_ask_text(question: str, options: list[str]) -> str | None:
	"""终端这个前端的提问器(模型主动问的那种)。返回用户说了什么,None = 没答上。

	选项用序号点,不用整行抄一遍 —— 这是终端里"按钮"的等价物。但序号**只
	活在本地**:换回选项原文再交出去,模型看到的永远是文字。浏览器那边同理
	(按钮直接把原文回上来)。两边都传下标的话,那个下标到文字的对应关系就是
	一份两边各存一半的约定 —— 而它会漂,漂的时候不报错。

	EOFError / KeyboardInterrupt 算**没答上**,不算空回答:管道里跑或
	stdin 被关掉时 input() 直接 EOF,那条路径上给一个空字符串的话,模型的
	tool_result 里就是一段空白,而它分不出"人说了句空的"和"根本没人在"。

	跟 terminal_ask 分开而不是合并成一个,理由见文件头。
	"""
	print(f"{YELLOW}{question}{RESET}")
	for i, option in enumerate(options, 1):
		print(f"  {i}) {option}")
	try:
		reply = input("  回答: ").strip()
	except (EOFError, KeyboardInterrupt):
		print()
		return None
	if options and reply.isdigit() and 1 <= int(reply) <= len(options):
		return options[int(reply) - 1]
	return reply or None
