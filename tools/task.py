"""task 那五个工具,外加"现在谁正攥着哪条任务"那份内存记录。

跟 tools/todo.py 那一版的根本区别:那份清单是**每个 agent 一份**的内存
变量,这一版的任务在库里,是**跨会话**的。所以这五个工具都得拿到
SessionStore —— 它们不是无状态的,也就不能像 bash/grep 那样做成模块级
单例。造它们的人(server.py)必须说清楚"用哪个库、这是哪一轮"。

读写成对分开,是因为 ToolDesc.side_effect 是按**整个工具**标记的:
task_read 只查库,漏声明成有副作用会让恢复时多一轮人工核对;反过来把
四个写工具声明成无副作用,恢复时会把一次刚改过任务图的调用再跑一遍。
后者更糟,所以四个写工具一律走默认的 True。
"""

import json
import threading
from typing import Callable

from sessions import TASK_STATES, TaskError
from tools.base import ToolDesc


# 正在执行的 task -> 攥着它的那个 turn。
#
# **这不是持久化的任务身份**,所以不进 task 表:它回答的是"这个进程里
# 谁正在跑它",而进程一死这个问题的答案就没有意义了 —— 重启后任务停在
# in_progress(见 sessions.set_task_status 那段),要人核对过再显式重试。
#
# 存 turn_id 而不是一个布尔,是因为要区分"我自己攥着"和"别人攥着":
# 同一个 turn 在干完活之后当然可以提交 complete,而另一个会话不行。
ACTIVE: dict[str, str] = {}

# 护 ACTIVE 的增删。它只护这一个 dict —— 数据库那把锁在 SessionStore 里,
# 两把锁谁都不套谁(工具层先查 ACTIVE 再进事务,或者反过来,都不构成
# 锁序问题,因为 ACTIVE 这个锁从来不跨越数据库调用)。
_ACTIVE_LOCK = threading.Lock()


def held_by(task_id: str) -> str | None:
	"""这条任务现在被哪个 turn 攥着。没人攥着返回 None。"""
	with _ACTIVE_LOCK:
		return ACTIVE.get(task_id)


def occupy(task_id: str, turn_id: str) -> None:
	"""把一条任务记在本轮名下。别人正攥着就抛。"""
	with _ACTIVE_LOCK:
		current = ACTIVE.get(task_id)
		if current is not None and current != turn_id:
			raise TaskError(
				"running",
				f"任务 {task_id} 正在另一个会话里执行(第 {current} 轮),"
				f"等它结束、或者先核对它的结果")
		ACTIVE[task_id] = turn_id


def release(task_id: str, turn_id: str) -> None:
	"""松手,**只松自己攥的那一条**。

	不加这个判断的话:自己超时松手之后,那条记录可能已经被下一轮重新
	攥上了,而这里一删就把别人的占有抹掉 —— 于是"另一个会话正在跑"这
	件事在内存里消失,下一次 retry 会放行。
	"""
	with _ACTIVE_LOCK:
		if ACTIVE.get(task_id) == turn_id:
			del ACTIVE[task_id]


def release_turn(turn_id: str) -> int:
	"""一轮结束时把它名下的任务全松掉,返回松了几条。

	由 server.py 在轮末调用(含失败和中断那两条路)。**必须在轮末松**:
	主 agent 用 task_status(start) 开始的任务要一直占用到这一轮结束
	(见设计 §5),而那一轮之后任务还留在 in_progress —— 状态在库里,
	占用在这儿,两者寿命不同正是这一版的安排。
	"""
	with _ACTIVE_LOCK:
		gone = [tid for tid, owner in ACTIVE.items() if owner == turn_id]
		for tid in gone:
			del ACTIVE[tid]
		return len(gone)


def _dump(payload: dict) -> str:
	"""工具结果统一走 JSON。

	设计里那句"返回机器可读的任务 ID、实际状态和错误原因,不能只打印
	人类可读的清单"就是这一句:模型要能拿到 id 去调下一个工具,而不是从
	一段中文里把 id 抠出来。ensure_ascii=False 是为了库里中文的任务名
	原样过去 —— 转义成 \\uXXXX 也读得懂,但白占 token。
	"""
	return json.dumps(payload, ensure_ascii=False)


def _fails(fn):
	"""把 TaskError 摊成一条 JSON 结果,而不是让异常飞出去。

	不抛的原因:这几种失败都是**正常请求撞上规则**(前置没完成、任务在跑、
	ID 打错),不是 bug。抛出去的话 agent.py 会把它包成
	`Error: TaskError: ...` —— 模型看到的是"工具炸了",而它该看到的是
	"这条路现在走不通,以及为什么"。reason 短码原样带着,好让模型能按
	类别决定下一步(等、去查、停手),而不是照着一段中文猜。
	"""
	def run(**kwargs) -> str:
		try:
			return _dump(fn(**kwargs))
		except TaskError as e:
			return _dump({"error": e.reason, "detail": str(e)})
	return run


def _read_handler(store) -> Callable[..., dict]:
	"""task_read 的实现。"""
	def do_read(task_id: str = "", owner: str = "", status: str = "") -> dict:
		"""有 task_id 给详情,否则给列表。

		两个一起给时 task_id 说了算:详情里有它的依赖和被依赖关系,那些
		恰恰是"这条能不能开始"要看的,不该被一个列表过滤条件挡掉。
		"""
		if task_id:
			task = store.get_task(task_id)
			if task is None:
				raise TaskError("not_found", f"没有这个任务:{task_id}")
			return {"task": task}
		if status and status not in (*TASK_STATES, "ready", "blocked", "all"):
			raise TaskError(
				"invalid_input",
				f"status 只能是 {'/'.join((*TASK_STATES, 'ready', 'blocked', 'all'))},"
				f"收到的是 {status!r}")
		tasks = store.list_tasks(owner=owner or None, status=status or None)
		return {"tasks": tasks, "count": len(tasks),
		        "filter": {"owner": owner or None, "status": status or None}}
	return do_read


def make_task_read(store) -> ToolDesc:
	"""只读的 task 工具，仅供启用 task 的主 agent 使用。"""
	description = (
		"Read the shared task list. With task_id: that task's full "
		"detail, including what it waits on and what waits on it. "
		"Without it: a list, by default the unfinished tasks. "
		"Tasks live in the server's database and are shared across "
		"sessions, so this is how you find out what already exists "
		"before creating a duplicate. Each task's 'state' is either "
		"ready (can be started) or blocked (a prerequisite is not "
		"done yet); 'blocking' names the prerequisites responsible."
	)
	return ToolDesc(
		name="task_read",
		description=description,
		input_schema={
			"type": "object",
			"properties": {
				"task_id": {"type": "string",
				            "description": "One task's id. Omit to list."},
				"owner": {"type": "string",
				          "description": "Only tasks with this owner label."},
				"status": {
					"type": "string",
					"enum": ["pending", "in_progress", "completed",
					         "cancelled", "ready", "blocked", "all"],
					"description": "Filter. Default: not yet finished.",
				},
			},
		},
		handler=_fails(_read_handler(store)),
		side_effect=False,
	)


def make_task_tools(store, turn_id: str) -> list[ToolDesc]:
	"""造这一轮的五个 task 工具。

	turn_id 是**这一轮**的 id,不是会话 id:占用记录按轮算,而"同一轮里
	可以提交 complete、别的会话不行"这条判断要的正是它。
	"""

	def do_create(name: str, description: str = "", owner: str = "main",
	              depends_on_ids: list | None = None) -> dict:
		if not isinstance(depends_on_ids, (list, type(None))):
			raise TaskError("invalid_input", "depends_on_ids 必须是 id 的数组")
		return {"task": store.create_task(name, description, owner,
		                                  depends_on_ids)}

	def do_edit(task_id: str, name: str | None = None,
	            description: str | None = None,
	            owner: str | None = None) -> dict:
		return {"task": store.edit_task(task_id, name, description, owner)}

	def do_dependency(task_id: str, depends_on_id: str, action: str) -> dict:
		if action == "add":
			task = store.add_dependency(task_id, depends_on_id)
		elif action == "remove":
			task = store.remove_dependency(task_id, depends_on_id)
		else:
			raise TaskError("invalid_input",
			                f"action 只能是 add 或 remove,收到的是 {action!r}")
		return {"task": task}

	def do_status(task_id: str, action: str) -> dict:
		return {"task": _transition(store, turn_id, task_id, action)}

	return [
		make_task_read(store),
		ToolDesc(
			name="task_create",
			description=(
				"Create a task in the shared task list. A task is a unit of "
				"work that outlives this conversation, so write the "
				"description as a requirement someone else could pick up: "
				"what has to be true when it is done. Use depends_on_ids to "
				"record that this task cannot start before others finish."
			),
			input_schema={
				"type": "object",
				"properties": {
					"name": {"type": "string", "minLength": 1,
					         "description": "Short title."},
					"description": {"type": "string",
					                "description": "What is required, and the "
					                               "bar for calling it done."},
					"owner": {"type": "string",
					          "description": "Owner label. Default: main."},
					"depends_on_ids": {
						"type": "array", "items": {"type": "string"},
						"description": "Ids of tasks that must complete first.",
					},
				},
				"required": ["name"],
			},
			handler=_fails(do_create),
		),
		ToolDesc(
			name="task_edit",
			description=(
				"Change a task's name, description or owner. Only the fields "
				"you pass are written. The task must still be pending: a "
				"running task is being worked against its current "
				"description, and a finished one is what other tasks already "
				"depended on."
			),
			input_schema={
				"type": "object",
				"properties": {
					"task_id": {"type": "string"},
					"name": {"type": "string"},
					"description": {"type": "string"},
					"owner": {"type": "string"},
				},
				"required": ["task_id"],
			},
			handler=_fails(do_edit),
		),
		ToolDesc(
			name="task_dependency",
			description=(
				"Add or remove a prerequisite edge between two tasks. "
				"action=add makes task_id wait for depends_on_id; action="
				"remove drops that wait. Cycles, self-dependencies and "
				"duplicate edges are refused, and only a pending task's "
				"edges can change."
			),
			input_schema={
				"type": "object",
				"properties": {
					"task_id": {"type": "string",
					            "description": "The task that waits."},
					"depends_on_id": {"type": "string",
					                  "description": "The task waited for."},
					"action": {"type": "string", "enum": ["add", "remove"]},
				},
				"required": ["task_id", "depends_on_id", "action"],
			},
			handler=_fails(do_dependency),
		),
		ToolDesc(
			name="task_status",
			description=(
				"Move a task between states. start: claim a ready task when "
				"YOU are going to do the work - it fails if the task is "
				"blocked or already taken, and costs nothing when it fails. "
				"complete: the work is done and meets the description. "
				"cancel: drop a pending task without doing it. retry: put a "
				"task that is stuck in in_progress back to pending, only "
				"once its previous run has stopped. Completing a task is "
				"always your call; an agent(prompt) result never changes task status."
			),
			input_schema={
				"type": "object",
				"properties": {
					"task_id": {"type": "string"},
					"action": {"type": "string",
					           "enum": ["start", "complete", "cancel", "retry"]},
				},
				"required": ["task_id", "action"],
			},
			handler=_fails(do_status),
		),
	]


def _transition(store, turn_id: str, task_id: str, action: str) -> dict:
	"""task_status 的那几条迁移,连占用记录一起。

	**占用记录和状态迁移是两件事,各管各的。**
	库里那条 status 决定"这条任务还能不能被认领",内存那份 ACTIVE 决定
	"这个进程里谁正攥着它"。两者寿命不同(见 release_turn),所以不能
	合并成一个判断。

	四种 action 的占用规矩不一样,分开写:

	  start    没人攥着才行。库里那条条件 UPDATE 是真闸门,这儿先查一遍
	           只是为了一句更准的话 —— 库里只会说"它已经是 in_progress",
	           而模型更该知道的是"另一个会话正在跑它"。
	  complete 自己和别人**都不**拦:主 agent 可以在本轮提交,而
	           进程重启后 ACTIVE 是空的 —— 这些情况下任务都停在
	           in_progress,等着主 agent 核对完显式提交。只拦别人。
	           提交成功之后**当场松手**:任务已经是终态,再攥着它谁也动不了,
	           而"攥着"这件事只该用来挡住"另一个会话正在跑它"。
	  retry    **谁也不许攥着**。"原执行已停止"是这一条的前提,而攥着就
	           说明没停 —— 包括自己这一轮(start 占着没放)。
	  cancel   只从 pending 走,而 pending 的任务不可能被占用。
	"""
	holder = held_by(task_id)
	if action == "start":
		if holder is not None:
			raise TaskError("running", f"任务 {task_id} 正被第 {holder} 轮攥着")
		task = store.claim_task(task_id)
		occupy(task_id, turn_id)
		return task
	if action == "complete":
		if holder is not None and holder != turn_id:
			raise TaskError(
				"running", f"任务 {task_id} 正被第 {holder} 轮执行,"
				f"该由那一轮来提交结果")
		task = store.set_task_status(task_id, "complete")
		release(task_id, turn_id)
		return task
	if action == "retry":
		if holder is not None:
			raise TaskError(
				"running",
				f"任务 {task_id} 的执行还没停(第 {holder} 轮),"
				f"先等它结束再重试")
		return store.set_task_status(task_id, "retry")
	return store.set_task_status(task_id, action)
