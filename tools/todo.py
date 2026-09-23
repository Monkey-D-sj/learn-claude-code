import ast
import json

from tools.base import ToolDesc


class TodoManager:
	def __init__(self):
		self.items: list[dict[str, str]] = []

	def update(self, todos: list | str) -> str:
		if isinstance(todos, str):
			try:
				todos = json.loads(todos)
			except json.JSONDecodeError:
				try:
					todos = ast.literal_eval(todos)
				except (SyntaxError, ValueError) as e:
					raise ValueError("todos must be a list or JSON array string") from e

		if not isinstance(todos, list):
			raise ValueError("todos must be a list")
		if len(todos) > 20:
			raise ValueError("Max 20 todos allowed")

		validated = []
		in_progress_count = 0
		for index, todo in enumerate(todos):
			if not isinstance(todo, dict):
				raise ValueError(f"todos[{index}] must be an object")

			content = str(todo.get("content", "")).strip()
			status = str(todo.get("status", "pending")).lower()
			if not content:
				raise ValueError(f"todos[{index}] requires content")
			if status not in ("pending", "in_progress", "completed"):
				raise ValueError(f"todos[{index}] has invalid status '{status}'")
			if status == "in_progress":
				in_progress_count += 1
			validated.append({"content": content, "status": status})

		if in_progress_count > 1:
			raise ValueError("Only one todo can be in_progress at a time")

		self.items = validated
		return self.render()

	def render(self) -> str:
		if not self.items:
			return "No todos."

		lines = []
		for todo in self.items:
			marker = {
				"pending": "[ ]",
				"in_progress": "[>]",
				"completed": "[x]",
			}[todo["status"]]
			lines.append(f"{marker} {todo['content']}")

		done = sum(todo["status"] == "completed" for todo in self.items)
		lines.append(f"\n({done}/{len(self.items)} completed)")
		return "\n".join(lines)


def make_todo_write(todo: TodoManager) -> ToolDesc:
	"""造一个 todo 工具,它只往传进来的这个清单上写。

	为什么是工厂而不是模块级单例:"当前任务清单"是**每个 agent 一份**的
	东西 —— 主 agent、它派的子 agent、浏览器里另一个会话,各有各的任务。
	以前只有一条会话,共用一份看不出来(模型看不到别人的清单,见下面),
	但语义已经错了:工具描述里写的是 "for your current session"。

	为什么清单是参数、不在这儿自己 new 一个:那样清单的寿命就等于这个
	工具对象的寿命,而浏览器那边工具是**每轮现造**的(server.py),
	清单一轮就没了。谁持有它由调用方决定 —— 浏览器是每会话一份,
	子 agent 是每次派活一份。上一级的调用方
	(tools/__init__.py 的 build_tools)故意不给默认值:谁造工具,
	谁就得说清楚这是谁的清单。

	顺带说清楚"共用一份"到底会怎样,免得下次有人重新推导一遍:
	TodoManager.items 只被 render() 读,而 render() 全项目只在 update()
	里被调一次 —— 所以工具返回的永远是本次调用刚提交的那份清单。
	模型只有这一个通道能看见 todo,它看不见别人的。真会串的只有下面那个
	print(两个会话的输出在服务进程的 stdout 上交错),那是纯显示问题。
	"""
	def run_todo_write(todos: list | str) -> str:
		try:
			output = todo.update(todos)
		except ValueError as e:
			return f"Error: {e}"
		print(f"\n\033[33m## Current Tasks\033[0m\n{output}")
		return output

	return ToolDesc(
		name="todo_write",
		description=(
			"Create and manage a task list for your current session. "
			"Overwrites the entire list: always send every task, not just the changed ones."
		),
		input_schema={
			"type": "object",
			"properties": {
				"todos": {
					"type": "array",
					"maxItems": 20,
					"items": {
						"type": "object",
						"properties": {
							"content": {
								"type": "string",
								"minLength": 1
							},
							"status": {
								"type": "string",
								"enum": ["pending", "in_progress", "completed"]
							}
						},
						"required": ["content", "status"]
					}
				}
			},
			"required": ["todos"],
		},
		handler=run_todo_write,
		# 只改这个会话内存里那份清单,不动工作区。
		side_effect=False,
	)
