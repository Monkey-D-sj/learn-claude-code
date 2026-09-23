"""把事件打成一行行终端文字。**现在只有子 agent 用它。**

终端那个前端(原来的 main.py)已经删了,所以这不再是"某个前端的展示层"——
它现在是**唯一能看见子 agent 在干什么的窗口**:子 agent 的循环不往上抛事件
(它的定位就是把过程藏起来,只回一句结论),所以它自己那个 emit 就往
server.py 这个进程的 stdout 上打。

叫 terminal_emit 是历史遗留的名字:输出的确是终端的样子(ANSI 上色),只是
已经没有"终端前端"这回事了。改名要动几处而没有任何行为变化,先留着。

本模块不 import 项目里任何东西 —— 它是叶子。tools/subagent.py 要用它,而
tools/ 被 app.py 引用,往这儿引项目模块会绕成环。
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
