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
from tools.vision import vision
from tools.write import write_file

# 除了 ask,其余工具都是无状态的,可以全进程共用一份。
#
# ask 不是,因为**它绑一个每轮才存在的东西**:"这一轮的问题往哪条流上问",
# 那条流是每请求一条的。所以它现造,由 build_tools 挂在后面。
#
# compress 和 recall 这对也是"要绑一个东西"的,但绑的东西没有"造工具的那一刻"
# 可以挂上去 —— 所以它俩都走 contextvar,见 tools/compress.py:
#   compress 绑当前那份 messages,由 agent_loop 每轮 bind
#   recall   绑这个会话的取回器(库 + 会话 id + 压缩器),由 server.py 每轮 bind
#
# **这里没有 todo_write,也没有 agent。** 前者随任务清单迁入数据库而删除；
# 后者由 server.py 挂到主 agent 的工具集，子 agent 不获得委派工具。
BASE_TOOLS = [
	bash, read_file, write_file, edit_file, glob, grep, skill, skill_manage_tool, vision,
	memory_tool, user_memory_tool, compress, recall,
]


def build_tools(ask_user, per_turn: list[ToolDesc]) -> list[ToolDesc]:
	"""组一份完整的工具集。

	两个参数都**不给默认值**,理由跟上一版一样,只是换了内容:
	  ask_user  是"往哪儿问"。卡在 input() 上等的话,浏览器那边会把 HTTP
	            线程连同会话锁一起挂死;直接返回"没人答"又会让那个前端里
	            ask 静默地永远不能用。没有一份默认值能同时避开这两种,所以
	            忘了传就是调用处当场 TypeError。
	  per_turn  是"这一轮多了哪些工具":主 agent 的委派工具，以及 task
	            开启时绑定库和本轮 id 的五个 task 工具。给默认值等于把"这个会话到底能不能用 task"
	            这个决定藏起来 —— 而它正是这个参数存在的理由。

	子 agent 传空列表，不获得 agent 和 task 工具(见 tools/subagent.py)。
	"""
	return [*BASE_TOOLS, *per_turn, make_ask_tool(ask_user)]
