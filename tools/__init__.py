from tools.ask import make_ask_tool
from tools.base import ToolDesc
from tools.bash import bash
from tools.compress import compress, recall
from tools.edit import edit_file
from tools.glob import glob
from tools.grep import grep
from tools.memory import memory_tool, user_memory_tool
from tools.read import read_file
from tools.skill import skill
from tools.skill_manage import skill_manage_tool
from tools.subagent import task
from tools.todo import TodoManager, make_todo_write
from tools.vision import vision
from tools.write import write_file

# 除了 todo_write 和 ask,其余工具都是无状态的,可以全进程共用一份。
#
# 那两个不是,而且**都是因为要绑一个每轮/每会话才存在的东西**:
#   todo_write  背后是一个任务清单,清单是每个 agent 一份的
#   ask         背后是"这一轮的问题往哪条流上问",那条流是每请求一条的
#
# 所以它俩现造,由 build_tools 挂在后面。
#
# compress 和 recall 这对也是"要绑一个东西"的,但绑的东西没有"造工具的那一刻"
# 可以挂上去 —— 所以它俩都走 contextvar,见 tools/compress.py:
#   compress 绑当前那份 messages,由 agent_loop 每轮 bind
#   recall   绑这个会话的取回器(库 + 会话 id + 压缩器),由 server.py 每轮 bind
BASE_TOOLS = [
	bash, read_file, write_file, edit_file, glob, grep, skill, skill_manage_tool, vision,
	memory_tool, user_memory_tool, task, compress, recall,
]


def build_tools(todo: TodoManager, ask_user) -> list[ToolDesc]:
	"""组一份完整的工具集。todo 是**谁的**清单、ask_user 是**往哪儿问**,
	两个都由调用方说了算。

	故意都不给默认值。默认值等于把这两个决定藏起来,而它们正是这个函数存在
	的理由:浏览器传当前会话那一份清单 + 绑在这一轮那条流上的提问器
	(server.py),子 agent 传一个新造的 + 一个一律拒答的(tools/subagent.py)。

	ask_user 更是一份默认值都编不出来 —— 卡在 input() 上等,浏览器那边会
	把 HTTP 线程连同会话锁一起挂死;直接返回"没人答",那个前端里 ask 就
	静默地永远不能用。不给默认值,忘了传就是调用处当场 TypeError。
	"""
	return [*BASE_TOOLS, make_ask_tool(ask_user), make_todo_write(todo)]
