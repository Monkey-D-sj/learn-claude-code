from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolDesc:
	name: str
	description: str
	input_schema: dict[str, Any]
	handler: Callable[..., str]
	# 这个工具会不会动工作区外面的世界(改文件、跑命令、派子 agent)。
	#
	# 它只在**恢复**那条路上被读,两个用途:
	#
	#   1. 有副作用的工具在动手之前要多写一条 "started" 标记(sessions.py 的
	#      begin_tool_exec)。进程崩在 handler 中间时,库里留下的那条标记就是
	#      "这条一定开始了、结果未知"的唯一证据 —— 没有它,"开始了"和"没开始"
	#      在库里长得一样,恢复时只能整批转人工核对。
	#   2. 无副作用的工具重发一次无害,核对范围里可以直接放行。
	#
	# **默认 True(有副作用),不是 False。** 漏声明时两种错的代价不对称:
	# 当成有害,代价是多写一条标记、恢复时多核对一条;当成无害,系统会在你
	# 不知道的情况下把一个刚写完文件的工具再跑一遍。往安全那侧倒。
	#
	# 与之成对的是 build_tools 那条"参数不给默认值":那条防的是"把决定藏起来",
	# 这条防的是"漏声明",方向不同,所以一个不给默认值、一个默认给最保守的值。
	side_effect: bool = True

	def to_wire(self) -> dict[str, Any]:
		return {
			"name": self.name,
			"description": self.description,
			"input_schema": self.input_schema,
		}
