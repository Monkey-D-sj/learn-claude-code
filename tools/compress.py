"""compress —— 模型自己决定压哪一段。

号段由模型点,摘要由模型写(工具输入就是摘要)。判断切得对不对、以及真正动
上下文的那两件事都在 context 里(tag_ids / compress_range / bind_messages),
这个模块只管把工具接上去、把结果这句话转出来。

当前那份 messages 怎么到 handler 手上,见 context.py 里 _CURRENT_MESSAGES
那段 —— 总之是 contextvar,不是参数也不是模块级变量。
"""

import context
from tools.base import ToolDesc


def run_compress(from_id: str, to_id: str, summary: str) -> str:
	"""把 from_id 到 to_id 这一段压成 summary。切错了回一句话,不动上下文。"""
	messages = context.current_messages()
	if messages is None:
		return "compress 只能在 agent 循环里用:现在没有拿到上下文。"
	return context.compress_range(messages, from_id, to_id, summary)


compress = ToolDesc(
	name="compress",
	description=(
		"把一段已经干完、后面不会再回头看的活压成一句话。每条工具结果的末尾都"
		"带着它的号(<message-id token=N>m00007</message-id>),from_id / to_id "
		"就填那个号。\n"
		"号段两端会自己吸附到完整的回合:你点的是结果,连同产生它的那次调用"
		"(以及同一次回复里别的结果)一起端走 —— 它们分不开。\n"
		"摘要由你写,它就是那一段将来唯一留下的东西 —— 写它当时在干什么、结论"
		"是什么、后面还用得上的事实(文件路径、数字、决定)。原文还在库里,按号"
		"能查回来。"
	),
	input_schema={
		"type": "object",
		"properties": {
			"from_id": {
				"type": "string",
				"description": "号段的第一个号,比如 m00003。",
			},
			"to_id": {
				"type": "string",
				"description": "号段的最后一个号,比如 m00012。包含它本身。",
			},
			"summary": {
				"type": "string",
				"description": "这一段换成的那句摘要。将来只看得到它,写全一点。",
			},
		},
		"required": ["from_id", "to_id", "summary"],
	},
	handler=run_compress,
)
