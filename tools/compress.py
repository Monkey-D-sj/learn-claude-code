"""按"号"管上下文的那一对:compress 压掉一段,recall 把它取回来。

号是 sessions.db 里那一行的行号(agent.py 落库时拿到它、拼进结果正文的尾巴,
见 sessions.find_message)。所以**"有号"就等于"这段查得回来"** —— 写不进去的
结果没有行号,也就没有号,模型看不见它,自然也不会去点它。

一个模块,因为是一件事:两个工具都只读写号,不产生号。判断切得对不对、以及
真正动上下文的那件事在 context.py(compress_range)。

    compress                          按号段把当前 messages 压成一句摘要
    make_recall(store, sid, compactor) 按号查回原文,并按预览那套渲染好
    run_recall(message_id)            工具那一层:递号进去、把结果转成一句话

**两个工具各要绑一样东西**,而且都是 contextvar,不是参数也不是模块级变量:

    compress 绑的是**当前那份 messages** —— 每轮都换(甚至在同一轮里被压缩
             改过),由 agent_loop 跑 handler 之前 bind,见 context.bind_messages
    recall   绑的是**本会话的取回器**(库 + 会话 id + 压缩器),由 server.py
             跑一轮之前 bind,见下面 _RECALL

为什么都不走参数:handler 只拿得到 **block.input。为什么都不走模块级变量:
server.py 一个进程里同时跑着好几个会话,模块级那份会被它们串成一份 —— 而串了
**不报错**,只是 A 会话的号在 B 会话里查到了别人的原文。
"""

import json
from contextvars import ContextVar

import context
from context import tag_number
from tools.base import ToolDesc

# ---------------------------------------------------------------- compress


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
		"带着它的号,形如 <message-id token=1240>m00007</message-id> —— 号就是 "
		"m00007 那一段,token= 后面是这次结果的大致花费。from_id / to_id "
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

# ---------------------------------------------------------------- recall

# 本会话的取回器。server.py 跑一轮之前 bind,工具在 handler 里读。
_RECALL: ContextVar = ContextVar("recall", default=None)


class bind_recall:
	"""`with bind_recall(fn):` —— 这一段里跑的 handler 都拿得到它。"""

	def __init__(self, recall):
		self._recall = recall
		self._token = None

	def __enter__(self):
		self._token = _RECALL.set(self._recall)
		return self._recall

	def __exit__(self, *exc):
		_RECALL.reset(self._token)
		return False


def _body_of(content) -> str:
	"""从落库那份 content 里把工具结果的正文抽出来。

	号只挂在 tool_result 上,所以库里那条的形状是 `[{type: tool_result, ...}]`。
	其它形状是给"将来号挂到别处去了"留的活口:退回 JSON 总比交回一句"取不到"
	强 —— 至少模型看得到内容,只是样子丑。
	"""
	if isinstance(content, list):
		for block in content:
			if isinstance(block, dict) and block.get("type") == "tool_result":
				content = block.get("content")
				break
	if isinstance(content, str):
		return content
	return json.dumps(content, ensure_ascii=False)


def make_recall(store, sid: str, compactor):
	"""造一个取回器:查库 + 按预览那套渲染好。绑给 recall 那个工具用。

	**渲染那一步不能省。** 被压掉的往往就是大的(小的没必要压),原样回灌等于
	把压缩白做 —— 而且下一轮第 1 档压缩还会把它再落一次盘。所以走跟第 1 档
	同一条路:落盘 + 头尾预览 + 分片读命令。小结果不必包那一层,原样给就行。

	**按会话查**(store.find_message 里那个 join):号是全库一张表上的行号,
	不圈回本会话的话,A 会话能读到 B 会话的原文。

	查不到给 None,不抛 —— 号不在了是正常情况(压过的段又被压了一次、号是
	上一轮的),由工具那一层去说人话。
	"""
	def recall(message_id: str) -> str | None:
		number = tag_number(message_id)
		if number is None:
			return None
		content = store.find_message(sid, number)
		if content is None:
			return None
		body = _body_of(content)
		if len(body) <= compactor.LARGE_RESULT_CHAR_LIMIT:
			return body
		return compactor.persisted_preview(message_id, body)
	return recall


def run_recall(message_id: str) -> str:
	"""按号取回一段原文。取不到就回一句人话,不抛。"""
	recall = _RECALL.get()
	if recall is None:
		return ("现在没接会话库,这段原文取不回来 —— 要它就读一次文件、"
		        "或者重跑一次那条命令。")
	if tag_number(message_id) is None:
		return "号不对:得写成 m00007 这样(字母 m + 五位数字),照着结果末尾那个填。"
	text = recall(message_id)
	if text is None:
		return (f"查不到 {message_id}:这个号不在本会话里 —— 可能它是上一轮的,"
		        f"或者那一段又被压过一次。现在的上下文里还有哪些号,照着那些点。")
	return text


recall = ToolDesc(
	name="recall",
	description=(
		"按号取回一段被压掉的原文。什么时候用:你早先压掉过一段,后来发现还用得上"
		"它的细节,而摘要里没写全。\n"
		"号就是结果末尾那个 <message-id token=N>m00007</message-id> 里的 m00007;"
		"压过的段在上下文里长这样 —— [m00003-m00012] 摘要:… —— 那两头的号也查得到"
		"(它们各自代表的那一条原文还在库里)。\n"
		"取回来的是原文本身:大块的会落盘,给你一段头尾预览和分片读的命令。它不会"
		"把它放回上下文里 —— 要用就再查一次。"
	),
	input_schema={
		"type": "object",
		"properties": {
			"message_id": {
				"type": "string",
				"description": "要取回的那个号,比如 m00007。",
			},
		},
		"required": ["message_id"],
	},
	handler=run_recall,
)
