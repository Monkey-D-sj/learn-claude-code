from tools.base import ToolDesc
from tools.bash import bash
from tools.edit import edit_file
from tools.glob import glob
from tools.read import read_file
from tools.skill import skill
from tools.subagent import task
from tools.todo import TodoManager, make_todo_write
from tools.write import write_file

# 除了 todo_write,其余工具都是无状态的,可以全进程共用一份。
# todo_write 不是 —— 它背后是一个任务清单,而清单是每个 agent 一份的,
# 所以它得现造。
BASE_TOOLS = [bash, read_file, write_file, edit_file, glob, skill, task]


def build_tools(todo: TodoManager) -> list[ToolDesc]:
	"""组一份完整的工具集。todo 是**谁的**清单,由调用方说了算。

	故意不给默认值。默认值等于把"这是谁的清单"这个决定藏起来,而它正是
	这个函数存在的理由:终端传进程一份(main.py),浏览器传当前会话那一份
	(server.py),子 agent 传一个新造的(tools/subagent.py)。
	"""
	return [*BASE_TOOLS, make_todo_write(todo)]
