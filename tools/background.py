"""后台执行在工具这一侧的公共部分:启动的入口、占位结果、以及取结果的工具。

三条产品路子(bash / agent / background_result)共用"起一个 job"和"认出这是
一个后台调用"这两件事,所以它们写在这儿一份,而不是在 bash.py 和 subagent.py
里各抄一遍 —— 抄两遍的代价是将来加一条规则(比如"没归属就拒绝")时漏掉一处,
而漏掉的那处不报错,只是那条路上多了一个不知道结果属于谁的 job。

跟 tools/compress.py 的 _RECALL 一个道理:后台归属是 contextvar,由 server.py
每轮 bind(见 jobs.bind_jobs),工具在 handler 里读 —— 因为 handler 只拿得到
**block.input,够不着"我是哪个会话"。
"""

import json
import time
from contextvars import ContextVar

import jobs
from tools.base import ToolDesc

# 一条 job 最多被"主动等待"多久。
#
# 必须有上限,而且必须短:等待期间模型那一步是停着的(整个 handler 是同步的),
# 会话锁也攥在手里。给它一个"等到跑完"的选项等于让模型用一次工具调用把整轮
# 挂死几分钟 —— 而它自己看不见代价,只会觉得"我等等就好了"。
#
# 30 秒够覆盖"我这边没别的活,就等这一件"的常见情形;真跑得久的东西本来就该
# 走自动注入那条路(结果完成时自己送回来),而不是在这儿等到天荒地老。
MAX_WAIT_SECONDS = 30.0

# 主动读取一次带回多少正文。比通知里那份摘要(2000)大得多 —— 这一条是
# "我就是要看它"的路径,而通知那条是"提一句有这件事"。再大的话,一次读取
# 就能把上下文吃掉一大块,而那正是后台执行要省下来的东西。
READ_CHARS = 20000


class Refused(RuntimeError):
	"""这次后台执行没能启动,理由要原样说给模型听。

	工具层接住它、把它当工具结果返回:模型据此能做的事(换同步跑、等一会儿、
	换一条路)取决于理由,所以不能吞成一句"失败了"。
	"""


def launch(tool: str, work) -> str:
	"""起一个后台 job,返回 job_id。起不来抛 Refused。

	**归属不确定就拒绝启动。** 拿不到 JobContext 意味着这个 handler 不在任何
	一轮里跑(裸调用、一次性脚本),或者跑在一个没有重建归属的线程里。猜一个
	会话出来是这里最坏的选择:结果会写进另一个会话的上下文,而那个会话的模型
	会拿一份不是自己的执行结果往下做决定,两边都不知道出过错。
	"""
	ctx = jobs.current()
	if ctx is None:
		raise Refused(
			f"现在没法确定这次后台执行属于哪个会话,所以没有启动它。"
			f"改成同步调用再试一次。")
	try:
		return jobs.start(ctx, tool, work)
	except jobs.JobError as e:
		raise Refused(str(e)) from e


def placeholder(job_id: str, tool: str) -> str:
	"""那条"正在执行"的工具结果 —— 原 tool_use 的**唯一**一条结果。

	机器可读(JSON)而不是一句话:页面要按字段画那个 job 格子,而"从一句话里
	抠出 job_id"这种解析迟早会在改文案的那天断掉,而且断得很安静 —— 页面上
	那个格子变成一片空白,没有人会把它跟一次文案改动联系起来。

	status 写 running 而不是 queued:走到这儿 job 已经登记并且线程已经起来了
	(见 jobs.start 的顺序),写 queued 是在描述一个不存在的阶段。
	"""
	return json.dumps({
		"job_id": job_id,
		"status": "running",
		"tool": tool,
		"message": "后台执行中;结果完成后会自动通知。",
	}, ensure_ascii=False)


# ------------------------------------------------------------ 主动读取的认领

# "这一条工具结果该把哪个 job 认领掉"。由 background_result 的 handler 设,
# 由 agent 循环在**回填 tool_result 那一步**取走。
#
# **位置是这件事的全部意义。** 在 handler 里顺手把标志标掉的话,模型拿到结果、
# 而这条 tool_result 还没进原始消息那一轮就崩了或被中断 —— 标志已经成了
# delivered,这份结果再也不会自动注入,而且没有任何地方看得出来丢了什么。
# 落在落库那一步就安全了:它和认领在同一个事务里(见 sessions.append_turn_message),
# 要么都成,要么都不成。
_CLAIM: ContextVar = ContextVar("job_claim", default=None)


def take_claim() -> str | None:
	"""取走"这一条工具结果该认领哪个 job",取走即清空。

	agent 循环每回填一条工具结果就调一次,**取不到也要调**:不清空的话,下一次
	工具调用的结果会替上一次认领 —— 而认领错了的表现是"一份结果永远不再自动
	注入",或者"另一份被重复注入",两种都不报错。
	"""
	job_id = _CLAIM.get()
	if job_id is not None:
		_CLAIM.set(None)
	return job_id


def _mark_claim(job_id: str) -> None:
	_CLAIM.set(job_id)


# ------------------------------------------------------------ background_result


def _describe(job: dict) -> str:
	"""一个 job 现在的样子,给模型看。不带正文 —— 正文由调用方拼。"""
	lines = [f"job_id: {job['id']}", f"tool: {job['tool']}",
	         f"status: {job['status']}"]
	if job.get("error"):
		lines.append(f"error: {job['error']}")
	return "\n".join(lines)


def run_background_result(job_id: str, wait_seconds: float | None = None) -> str:
	"""查一个后台 job。跑完了就把结果正文一起给,没跑完就给状态。

	**两条交付路径的互斥在这一条上认领**(见 take_claim):读到终态并成功返回
	之后,那份结果就不会再被自动注入。反过来,如果这里只是看了一眼还没跑完的
	job,什么都不认领 —— 那不是交付,只是查询。
	"""
	ctx = jobs.current()
	if ctx is None:
		return ("现在没有连到任何一个会话,查不了后台 job —— 这一轮不是在服务端"
		        "跑起来的。")

	job_id = str(job_id or "").strip()
	if not job_id:
		return "Error: job_id is empty. Pass the id from the placeholder result."
	job = ctx.store.get_job(job_id)
	# **归属必须自己查一遍。** 库那一层是按主键查的,它不知道"这个号属于谁"
	# —— 不查这一下的话,A 会话拿自己上下文里的一个号就能读到 B 会话的执行
	# 结果(包括它的命令和输出),而"两个会话互相看不见对方"是这里唯一还立着
	# 的边界,破了不报错。
	#
	# 拒绝时**不区分"没这个号"和"它是别人的"**:区分了就等于确认了那个号存在。
	if job is None or job["session_id"] != ctx.session_id:
		return (f"查不到 job {job_id}:它不在这个会话里(号写错了,或者它已经被"
		        f"清理掉了 —— 服务重启会清掉所有后台 job)。")

	try:
		deadline = time.monotonic() + min(float(wait_seconds or 0),
		                                  MAX_WAIT_SECONDS)
	except (TypeError, ValueError):
		return "Error: wait_seconds must be a number."

	while job["status"] not in ("completed", "failed"):
		if time.monotonic() >= deadline:
			return (f"{_describe(job)}\n还没跑完。**不要在这儿反复等** —— 它完成时"
			        f"结果会自动送进来,你接着做别的事就行。")
		time.sleep(0.25)
		job = ctx.store.get_job(job_id)
		if job is None or job["session_id"] != ctx.session_id:
			return f"查不到 job {job_id}:它已经被清理掉了。"

	# 走到这儿就是终态。认领挂上,回填 tool_result 那一步真的落库才算数
	# (见 take_claim)。**这一句不能挪进上面的循环** —— 认领了却还没交付,
	# 就是这条路径上唯一能把结果弄丢的方式。
	_mark_claim(job_id)
	body = jobs.read_result(job.get("result_path"), READ_CHARS)
	head = _describe(job)
	if job["status"] == "failed" and not body:
		return head
	return f"{head}\n\n{body}" if body else head


background_result = ToolDesc(
	name="background_result",
	description=(
		"查一个后台执行(run_in_background 起的)现在怎么样了。跑完了就把它的"
		"结果给你;还在跑就只给状态。\n"
		"什么时候用:你手上正好没有别的活、想等这一件;或者它已经通知过你完成、"
		"而你要看完整输出。**结果完成时会自动送到你面前,所以不必为了等它而反复"
		"调用这个工具** —— 那只是把时间花在原地转圈上。\n"
		"不传 wait_seconds 就是立刻返回当前状态;传了最多等 30 秒。"
	),
	input_schema={
		"type": "object",
		"properties": {
			"job_id": {
				"type": "string",
				"description": "占位结果里那个 job_id,形如 bg_1a2b3c4d5e6f。",
			},
			"wait_seconds": {
				"type": "number",
				"description": "最多等几秒(上限 30)。不传就立刻返回。",
			},
		},
		"required": ["job_id"],
	},
	handler=run_background_result,
	# 只读:重发一次无害,所以不用两阶段标记(见 tools/base.py 的 side_effect)。
	# 它确实会改一点库里的状态(认领通知),但那笔改动是"这份结果已经给过模型了"
	# —— 重发一次的结果完全相同,没有第二个副作用。
	side_effect=False,
)
