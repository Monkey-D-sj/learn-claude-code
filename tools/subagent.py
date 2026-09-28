"""子 agent,以及那个把它派出去的工具。

工具叫 `agent`,不叫 `task` —— 名字让给任务了。这两个东西是不同的对象:
`agent` 是"开一个独立的上下文窗口去干活"这一次**调用**,task 是库里一条
跨会话的**工作项**。以前两者同名,读代码时得靠上下文猜是哪一层。

一次 `agent` 调用有两种用法:给 `prompt` 做临时委派,或者给 `task_id`
执行一条已有的任务。后者不是"另一种 prompt" —— 它把子 agent 的指令交给
库里的 name / description 决定,调用方不能再拿一段自由文本盖掉它。理由:
那条任务的要求是全项目共享的,能被一次调用改写的话,同一份要求在不同会话
里就不是同一件事了。
"""

import json

from agent import agent_loop, client
from config import MAX_ROUNDS, TOOL_RESULTS_DIR, TRANSCRIPT_DIR, WORKDIR
from context import ContextCompactor
from emit import terminal_emit
from sessions import TaskError
from tools.base import ToolDesc
from tools.task import make_task_read, occupy, release
import usage

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
# emit 走 emit.terminal_emit —— 也就是**打到服务进程的 stdout 上**,不走
# 调用它的那个页面:子 agent 的定位就是把过程藏起来,只回一句结论(这正是
# 它省上下文的方式)。要让它显在页面上,得让工具的 handler 也能拿到 emit,
# 那是另一件事。
COMPACTOR = ContextCompactor(client, MODEL, TRANSCRIPT_DIR, TOOL_RESULTS_DIR,
                             terminal_emit)


def _deny_all(question: str) -> bool:
	"""子 agent 的确认器:一律拒绝。

	不能把父 agent 的确认器传下来,有两个理由,任一都足够:

	  1. 子 agent 的定位就是"没人能回答问题"(见上面 SYSTEM),给它开一个
	     交互通道等于把自己那句设定推翻;
	  2. 它跑在调用它的那个前端的线程里。这儿要是走 input(),卡住的是
	     server.py 的 HTTP 线程 —— 而那个会话的那把锁还攥着,页面那边
	     只会看到一直转圈。

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


def run_agent(prompt: str, store=None, turn_id: str = "",
              task_enabled: bool = False) -> str:
	# 延迟导入:子 agent 要"所有工具",而本模块由 tools/__init__ 加载,
	# 模块级 from tools import build_tools 会拿到半初始化的包。
	from tools import build_tools

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
	# skill_manage 也写 WORKDIR 级的持久文件,同样只交给主 agent。
	#
	# ask 排除,理由跟 _deny_all 是同一个:上面 SYSTEM 头一句就是"nobody can
	# answer questions"。给它一个能问的工具,等于同时推翻那句设定和 _deny_all
	# 存在的理由。
	#
	# 压缩那对(compress / recall)排除:**它们都以"号"为抓手,而子 agent 发不
	# 出号** —— 它的 record 是 `_drop`,落库那一步根本不发生,也就没有行号可
	# 当号用。给它等于给一个点了没反应的工具:compress 会回"号不在上下文里",
	# 而 recall 会回"这个前端没有会话库",两句都是假话(真相是它自己没号)。
	#
	# 代价说清楚:子 agent 的上下文只能靠那几档自动压缩收。可以接受 —— 它的
	# 定位就是"干完报结论、上下文随用随弃",而它交回来的那句话才是主 agent
	# 要的东西。
	#
	# `agent` 不在这个名单里,因为它**根本不在 BASE_TOOLS 里**了:派活的工具
	# 由调用方挂上去(server.py),子 agent 那份工具集不挂,于是"套娃"这件事
	# 从"靠名单挡"变成了"压根没有"。名单在这儿留着是给别的工具的。
	_DENIED = ("memory", "user_memory", "skill_manage", "ask",
	           "compress", "recall")
	# 子 agent 读得到全局任务图,一个字都改不了(见 tools/task.py)。
	# 它想建的任务和想连的依赖,由它写进报告、主 agent 来建 —— 子 agent 是
	# 一次性上下文,它看不见别的会话正在动这张图。task 没开时不挂。
	extra = ([make_task_read(store, read_only=True)]
	         if (task_enabled and store is not None) else [])
	sub_tools = [
		t for t in build_tools(_nobody_to_ask, extra)
		if t.name not in _DENIED
	]

	print("\n\033[35m[Subagent started]\033[0m")
	# 嵌套 span:只把 agent 改成 "subagent",session / turn 从外层继承。
	#
	# **合并而不是覆盖**是必须的 —— 覆盖的话子 agent 花的钱会变成一条没有归属
	# 的孤儿记录。而它恰恰是最该被看见的一笔:嵌套、没人看、没人问,而且是整个
	# 系统里最容易失控的地方(它可以继续派活,只被 _DENIED 挡住)。
	with usage.span(agent="subagent"):
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
			# 对谁都没好处,代价却是**把重试禁掉**:call_api 里"吐过字就
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


def _task_prompt(task: dict) -> str:
	"""一条任务的 name + description 变成子 agent 的指令。

	**只有这两样,调用方插不进别的话**(见本模块开头那段)。name 是标题,
	description 是要求和完成标准 —— 设计 §4 就是这两列。
	"""
	parts = [task["name"]]
	if (task.get("description") or "").strip():
		parts.append(task["description"].strip())
	return "\n\n".join(parts)


def _run_task(store, turn_id: str, task_id: str) -> str:
	"""执行一条已有任务:认领 → 占用 → 派子 agent → 松手。

	**顺序是认领在前、派活在后**,这不是风格问题:验收第 2 条要的是"两个
	会话同时认领同一 ready 任务,恰好一个成功,失败的一方**在启动子 agent
	之前**得到结果"。认领就是那条原子 UPDATE(见 sessions.claim_task),
	而它失败时一句模型调用都不该发生 —— 那正是这条设计要保住的东西。

	占用(内存那份)在认领之前:它是"这个进程正攥着它"的记录,先占再认,
	认领失败就把占用退回去。反过来先认后占的话,中间那一瞬间库里已经
	in_progress、内存里却没人认领 —— 这时进程被杀,任务就卡在一个没有任何
	人在跑的 in_progress 上,而它看起来和"跑失败了等着核对"一模一样。
	"""
	try:
		occupy(task_id, turn_id)
	except TaskError as e:
		return _fail(e)
	try:
		task = store.claim_task(task_id)
	except TaskError as e:
		release(task_id, turn_id)
		return _fail(e)
	try:
		text = run_agent(_task_prompt(task), store=store, turn_id=turn_id,
		                 task_enabled=True)
	finally:
		# **子 agent 返回就松手**,不等这一轮结束:任务停不停留在
		# in_progress 是库里那列的事,而"谁在跑"到这儿就结束了。
		# 松了手,主 agent 才能接着调 complete 或 retry。
		release(task_id, turn_id)
	# 交回去的话必须说清楚"任务还没完成" —— 子 agent 回来只表示这次调用
	# 返回了,不表示任务达标(设计 §4)。不说的话,主 agent 会拿一段结论
	# 当结果往下做,而库里那条任务一直停在 in_progress。
	return (f"{text}\n\n[Task {task_id} is still in_progress. A subagent "
	        f"returning only means this run ended - check the result and "
	        f"call task_status complete, or retry if it fell short.]")


def _fail(e: TaskError) -> str:
	"""认领没成:原样说清楚,而且**一句模型调用都不发生**。"""
	return json.dumps({"error": e.reason, "detail": str(e)},
	                  ensure_ascii=False)


def make_agent_tool(store, turn_id: str, task_enabled: bool) -> ToolDesc:
	"""造这一轮的 `agent` 工具。

	两种用法一个工具,是因为它们对模型来说是同一件事的两种输入("派活
	给谁"),分成两个工具只会让它多猜一次该用哪个。二选一由 handler 判 ——
	schema 表达不了"恰好一个":JSON Schema 的 oneOf 在这套协议里能用,
	但报错信息是给模型看的,自己判能说清楚是哪一种不对。

	task 没开的会话,`task_id` 这个参数**根本不进 schema**:列在那儿的话,
	模型会去用它,然后拿到一句错误 —— 一个列出来却不能用的参数是陷阱。
	"""
	properties = {
		"prompt": {
			"type": "string",
			"description": "A self-contained task description, including "
			               "what to report back.",
		},
	}
	if task_enabled:
		properties["task_id"] = {
			"type": "string",
			"description": "Id of an existing task to execute. The subagent "
			               "gets that task's name and description as its "
			               "instructions, so do not also pass prompt.",
		}

	def run(prompt: str = "", task_id: str = "") -> str:
		prompt = (prompt or "").strip()
		task_id = (task_id or "").strip()
		if bool(prompt) == bool(task_id):
			return ("Error: pass exactly one of prompt (a one-off delegation) "
			        "or task_id (run an existing task) - not both, not neither.")
		if not task_id:
			# store / task_enabled 照样传下去:`agent(prompt)` 派出去的子 agent
			# 也该读得到那张任务表 —— 它读得、改不得(见上面 extra 那段)。
			# 漏传的话子 agent 在开着 task 的会话里也看不见任务,于是它会把
			# 已有的活重新做一遍。
			return run_agent(prompt, store=store, turn_id=turn_id,
			                 task_enabled=task_enabled)
		# schema 里没有 task_id 时走不到这儿;留着是因为 schema 和 handler
		# 是两处,而它们漂开的那天,静默地把 task_id 当成 prompt 用更糟。
		if not task_enabled or store is None:
			return "Error: this session does not have tasks enabled."
		return _run_task(store, turn_id, task_id)

	return ToolDesc(
		name="agent",
		description=(
			"Delegate work to a subagent with its own separate context "
			"window that reports back only a final summary. Two ways to use "
			"it. With prompt: a one-off investigation whose raw output you "
			"do not need to keep - searching a large codebase, reading many "
			"files. With task_id: execute an existing task from the shared "
			"list; its stored name and description become the subagent's "
			"instructions. The subagent cannot ask questions, so a prompt "
			"must be self-contained and say what to report back. Either way, "
			"the subagent returning does not complete the task: inspect the "
			"result and call task_status yourself."
		),
		input_schema={"type": "object", "properties": properties},
		handler=run,
	)
