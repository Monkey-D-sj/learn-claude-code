"""问用户一个问题,等他的回答。

跟 permission_hook 那个 ask 是**两回事**,别混:

	permission_hook   由 harness 触发 —— 它拦下一次工具调用,问"放不放行"
	                  签名 ask(question) -> bool,答案是是/否
	本模块             由**模型**触发 —— 它拿不准,问"该怎么办"
	                  答案是任意一段文字

方向相反,答案的类型也不同,所以是两个通道。

**为什么 handler 不能自己弹个框:** agent_loop 调的是 handler(**block.input)
(见 agent.py),没有 emit、没有 ask —— 工具 handler 够不着前端。所以"往哪儿
问"由调用方 partial 进来,见文件末尾。这也正是 build_tools 那个 ask_user
参数不给默认值的原因:**没有哪个默认值在三个前端里都是对的**(浏览器没有
stdin,子 agent 没人能回答)。

本模块里**没有**模块级变量叫 ask:ToolDesc 由一个工厂现造(make_ask_tool),
不落成一个模块级名字。这是刻意的 —— `import tools.ask as A` 取的是**包上的
属性**,谁要是写一句 `ask = ToolDesc(...)`,那个属性就被覆掉,于是这句
import 静默地拿到一个 ToolDesc,而报出来的错("ToolDesc object has no
attribute ...")完全不指向病因。tools/memory.py 那边当初就是这么栽的,详见
它的文件末尾。
"""

from functools import partial

from tools.base import ToolDesc

# 选项个数上限。
#
# 没有闸的话,模型可以往页面上灌一屏按钮 —— 而页面是给人用的,不是用来
# 展示模型列了多少种可能的。超了直接拒,让它自己收窄:跟记忆那两条上限
# 一个道理,拒的时候说清楚,它下一步就能自己改。
MAX_OPTIONS = 6


def _one_line(text: str) -> str:
	"""选项压成一行。留着换行的话,一个按钮里能塞进一整段。"""
	return " ".join(str(text).split())


def run_ask(ask_user, question: str, options=None) -> str:
	"""问一个问题,把回答原样交回模型。

	ask_user 的签名是 ask_user(question, options) -> str | None,None 表示
	**没人答上**(超时、页面关了、回答通道断了)。那几种情况对模型是同一件事:
	手里没答案,得自己拿主意 —— 所以合成一条报错,不细分。

	返回的**永远**是字符串:工具 handler 只有这一个出口,失败的形状也必须是
	它(见 agent.py 那段"异常变成一段交回模型的字符串")。
	"""
	question = str(question or "").strip()
	if not question:
		return "Error: question is empty. Pass what you want to ask."

	# 去空、压成一行。选项是要变成按钮的,空白和换行在这儿没有意义。
	opts = [line for line in (_one_line(o) for o in (options or [])) if line]
	if len(opts) > MAX_OPTIONS:
		return (f"Error: {len(opts)} options, over the {MAX_OPTIONS} cap. Keep "
		        f"the question, or narrow it to at most {MAX_OPTIONS} choices.")

	answer = ask_user(question, opts)
	if answer is None:
		# 措辞里给了下一步:不说的话模型会以为是自己问的方式不对,换个说法
		# 再问一遍 —— 而真正的原因是人不在,再问一遍还是没人答。
		return ("Error: no answer came back — the question was shown to the "
		        "user, but nobody replied in time (or the page was closed). "
		        "Use your own judgement, or put the question in your final "
		        "message instead of asking again.")
	return str(answer)


_SCHEMA = {
	"type": "object",
	"properties": {
		"question": {
			"type": "string",
			"description": ("What to ask. Be specific and self-contained — the "
			                "user has not seen your reasoning."),
		},
		"options": {
			"type": "array",
			"items": {"type": "string"},
			"description": (f"Optional, at most {MAX_OPTIONS}. A few short "
			                f"choices, when the answer is one of them. They "
			                f"are shown as buttons next to the question, and "
			                f"the user can still type something else."),
		},
	},
	"required": ["question"],
}

_DESCRIPTION = (
	"Ask the user a question and wait for their answer. Use it when you are "
	"blocked on something only they can settle: a preference, a fact you "
	"cannot find with your tools, or a choice between approaches that look "
	"equally good to you.\n"
	"Do NOT use it to ask permission to run something. Tool permissions are "
	"handled automatically — if a call needs the user's approval they are "
	"asked directly, and a question like 'shall I run this?' just costs a "
	"round trip.\n"
	f"Pass short `options` (at most {MAX_OPTIONS}) when the answer is a choice "
	"between a few known things: they are drawn as buttons beside the "
	"question. The answer comes back verbatim — pick it up as given, whether "
	"it was a button or something typed."
)


def make_ask_tool(ask_user) -> ToolDesc:
	"""按前端那份提问器造一个 ask 工具。

	造而不是模块级常量,因为 handler 得把 ask_user 绑进去 —— 而 ask_user
	带着"这一轮的问题往哪条流上问"(服务端那份闭包住了 emit / sid / turn_id)。
	所以工具集是**每轮现造**的,见 tools/__init__.py 的 build_tools。
	"""
	return ToolDesc(
		name="ask",
		description=_DESCRIPTION,
		input_schema=_SCHEMA,
		# partial 之后签名正好剩 (question, options) 两个,跟 _SCHEMA 对齐。
		handler=partial(run_ask, ask_user),
		# 问一句不动机器。但「卡在一条没答完的提问上」被进程打断是另一回事,
		# 那种尾部由恢复判定单独处理,别混进这个标志。
		side_effect=False,
	)
