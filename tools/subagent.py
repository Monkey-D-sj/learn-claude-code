from agent import agent_loop, client
from config import MAX_ROUNDS, TOOL_RESULTS_DIR, TRANSCRIPT_DIR, WORKDIR
from context import ContextCompactor
from emit import terminal_emit
from tools.base import ToolDesc

SYSTEM = (
	f"You are a subagent working in {WORKDIR}. "
	"You have your own context window and a full set of tools. "
	"Work autonomously until the task is done - nobody can answer questions, "
	"so never ask for clarification. "
	"When you finish, report back concisely: what you found, and anything "
	"the main agent needs in order to act. Report conclusions, not a "
	"transcript of everything you read."
)

# 子 agent 不必用跟主 agent 一样的模型。探索类任务换成更便宜的,
# 主 agent 留着做决策 —— 改这一行即可。
MODEL = "deepseek-flash"

# 子 agent 建自己那个压缩器 —— 就是为了它的 model 能跟主 agent 不一样。
# 这也是它不能是模块级单例的原因。
#
# emit 走终端,不走调用它的那个前端:子 agent 的定位就是把过程藏起来,
# 只回一句结论(这正是它省上下文的方式)。它的中间步骤在浏览器里也看不见,
# 跟终端一致 —— 要让它可见,得让工具的 handler 也能拿到 emit,那是另一件事。
COMPACTOR = ContextCompactor(client, MODEL, TRANSCRIPT_DIR, TOOL_RESULTS_DIR,
                             terminal_emit)


def _deny_all(question: str) -> bool:
	"""子 agent 的确认器:一律拒绝。

	不能把父 agent 的确认器传下来,有两个理由,任一都足够:

	  1. 子 agent 的定位就是"没人能回答问题"(见上面 SYSTEM),给它开一个
	     交互通道等于把自己那句设定推翻;
	  2. 它跑在调用它的那个前端的线程里。父 agent 在浏览器里时,这儿要是
	     走终端的 input(),卡住的是 server.py 的 HTTP 线程 —— 而那个会话
	     的那把锁还攥着,页面那边只会看到一直转圈。

	所以越界的调用直接拒掉,理由(字符串)交回模型,让它自己绕路。
	"""
	return False


def _nobody_to_ask(question: str, options) -> None:
	"""子 agent 的提问器:没人能回答,返回 None(= 没答上)。

	正常路径上轮不到它 —— ask 已经躺在 _DENIED 里,压根没进工具集。留着是因为
	build_tools 那个参数是必填的,而**签名必须是对的**:随手把 _deny_all 传过去
	能过,但它是按一个参数调的,哪天 deny 名单动了一下,炸出来的是一个
	TypeError,而不是"没人答得上"。
	"""
	return None


def run_task(prompt: str) -> str:
	# 延迟导入:子 agent 要"所有工具",而本模块由 tools/__init__ 加载,
	# 模块级 from tools import build_tools 会拿到半初始化的包。
	from tools import build_tools
	from tools.todo import TodoManager

	# 每次派活现造一个任务清单:子 agent 就是一个全新的上下文窗口
	# (下面那句 agent_loop 也只喂一条 prompt),它的任务不该跟主 agent 的
	# 混在一个列表里 —— 这正是 todo 那个工厂存在的理由。
	#
	# 排除自己,否则子 agent 可以无限套娃。
	#
	# 两个记忆工具(memory / user_memory)也排除。两个理由,任一都够:
	#
	#   1. 子 agent 的定位是"独立上下文、只回结论"(见上面 SYSTEM)。它翻到
	#      的东西该写进报告交回主 agent,由主 agent 决定记不记 —— 它自己
	#      记,主 agent 就看不见记了什么。
	#   2. 记忆是**一份 WORKDIR 级的文件**,谁都能写就谁都能覆盖。子 agent
	#      拿的是自己那份上下文,看不到主 agent 刚写了什么。
	#
	# ask 排除,理由跟 _deny_all 是同一个:上面 SYSTEM 头一句就是"nobody can
	# answer questions"。给它一个能问的工具,等于同时推翻那句设定和 _deny_all
	# 存在的理由。
	_DENIED = ("task", "memory", "user_memory", "ask")
	sub_tools = [
		t for t in build_tools(TodoManager(), _nobody_to_ask)
		if t.name not in _DENIED
	]

	print("\n\033[35m[Subagent started]\033[0m")
	outcome = agent_loop(
		[{"role": "user", "content": prompt}],
		active_request=prompt,
		system=SYSTEM,
		tools=sub_tools,
		model=MODEL,
		max_rounds=MAX_ROUNDS,
		compactor=COMPACTOR,
		ask=_deny_all,
		emit=terminal_emit,
		# 不走流式。terminal_emit 没有 delta 分支,碎片打进去等于丢掉 ——
		# 对终端一点好处没有,代价却是**把重试禁掉**:call_api 里"吐过字就
		# 不再重试"那条与 emit 收到什么无关,吐出第一个字之后再来个 500 或
		# 连接超时,这一轮就只能整个失败交回主 agent。
		stream=False,
	)
	# 工具 handler 只能回字符串,所以 TurnOutcome 到这儿要摊平。失败必须
	# 说出来:主 agent 看不到子 agent 的中间过程,它唯一的信息源就是这段
	# 返回文本。"跑了一半就停下"和"干完了"给出的结论长得一样的话,主 agent
	# 会拿着一个半成品当结果往下做。
	if outcome.status == "failed":
		return f"[subagent failed: {outcome.error}]\n{outcome.text}"
	return outcome.text


task = ToolDesc(
	name="task",
	description=(
		"Delegate a task to a subagent that works in its own separate context "
		"window and reports back only a final summary. Use it for work that "
		"would otherwise flood your context with material you do not need to "
		"keep: searching a large codebase, reading many files, or any "
		"multi-step investigation whose raw output you only need the "
		"conclusion of. The subagent cannot ask you questions, so the prompt "
		"must be self-contained and say what to report back."
	),
	input_schema={
		"type": "object",
		"properties": {
			"prompt": {
				"type": "string",
				"description": "A self-contained task description, including what to report back.",
			},
		},
		"required": ["prompt"],
	},
	handler=run_task,
)
