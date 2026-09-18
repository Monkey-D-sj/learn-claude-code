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


def run_task(prompt: str) -> str:
	# 延迟导入:子 agent 要"所有工具",而本模块由 tools/__init__ 加载,
	# 模块级 from tools import TOOLS 会拿到半初始化的包。
	from tools import TOOLS

	# 排除自己,否则子 agent 可以无限套娃。
	sub_tools = [t for t in TOOLS if t.name != "task"]

	print("\n\033[35m[Subagent started]\033[0m")
	return agent_loop(
		[{"role": "user", "content": prompt}],
		active_request=prompt,
		system=SYSTEM,
		tools=sub_tools,
		model=MODEL,
		max_rounds=MAX_ROUNDS,
		compactor=COMPACTOR,
		emit=terminal_emit,
	)


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
