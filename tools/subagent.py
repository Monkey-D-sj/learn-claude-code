from agent import agent_loop
from config import MAX_ROUNDS, WORKDIR
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


def run_task(prompt: str) -> str:
	# 延迟导入:子 agent 要"所有工具",而本模块由 tools/__init__ 加载,
	# 模块级 from tools import TOOLS 会拿到半初始化的包。
	from tools import TOOLS

	# 排除自己,否则子 agent 可以无限套娃。
	sub_tools = [t for t in TOOLS if t.name != "task"]

	print("\n\033[35m[Subagent started]\033[0m")
	return agent_loop(
		[{"role": "user", "content": prompt}],
		system=SYSTEM,
		tools=sub_tools,
		model=MODEL,
		max_rounds=MAX_ROUNDS,
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
