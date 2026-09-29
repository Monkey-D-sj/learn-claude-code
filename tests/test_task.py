"""Task:全局任务表、依赖图、认领与状态迁移。

这一版把"每会话一份内存待办清单"换成了**库里一张跨会话的任务表**,所以
每条用例都对着一个具体的错法:

  一、任务不绑会话:两个会话看到同一张表,一个关掉 task 不影响另一个 ——
     反过来做(任务跟着会话走)在演示里看不出来,只会在"换个会话就找不到
     自己的活了"那天暴露
  二、认领必须原子:两个会话同时抢一条 ready 任务,恰好一个成功 —— 先读
     后写的话两个都会成功,任务归属就不再确定
  三、依赖是图不是树:多前置全完成才 ready,环、自依赖、重复边都要拒
  四、运行中的任务不可改:执行期间改变要求会让完成判据漂移
  五、任何情况下都不自动重试:失败、中断、进程重启之后任务停在
     in_progress,等人核对过再显式 retry
  六、关着 task 的会话**根本看不到**那些工具,而不是看得见调不动
  七、任务写入和依赖边同事务,而且**事务不跨越模型调用和 agent 执行**

跑法: uv run pytest
"""

import json
import sqlite3
import threading

import pytest

import server
import sessions
import tools.subagent as subagent
from agent import TurnOutcome


def raw(db, sql, args=()):
	conn = sqlite3.connect(db)
	try:
		return conn.execute(sql, args).fetchall()
	finally:
		conn.close()


@pytest.fixture
def env(monkeypatch, tmp_path):
	"""临时库 + 一个会话,装进 server.STORE。返回 (db_path, store, sid)。"""
	db = tmp_path / "sessions.db"
	store = sessions.SessionStore(db)
	monkeypatch.setattr(server, "STORE", store)
	return db, store, store.create_session("项目记忆", "用户记忆")["id"]


def tools_for(sid, turn_id="turn-1"):
	"""这一轮的工具集,按名字索引。走的是真的 turn_tools。"""
	return {t.name: t for t in server.turn_tools(sid, turn_id)}


def call(tools, tool, **kwargs):
	"""调一个工具,把它的 JSON 结果解回来。

	**所有 task 工具都返回 JSON**,失败也是 —— 失败那条是
	{"error": <reason>, "detail": ...},所以断言既能看到"没做成",也能看到
	"为什么没做成",而不是从一句中文里猜。

	第二个参数叫 tool 而不是 name:工具自己的入参里就有个 name(任务标题),
	占了这个位置的话 `ok(tools, "task_create", name="x")` 会撞成
	TypeError,而那是这条辅助函数自己的问题,不该让每条用例都绕着写。
	"""
	return json.loads(tools[tool].handler(**kwargs))


def ok(tools, tool, **kwargs):
	result = call(tools, tool, **kwargs)
	assert "error" not in result, result
	return result


def fails(tools, tool, **kwargs) -> str:
	result = call(tools, tool, **kwargs)
	assert "error" in result, result
	return result["error"]


def enable(store, *sids):
	for sid in sids:
		store.set_task_enabled(sid, True)


@pytest.fixture(autouse=True)
def _clean_active():
	"""占用记录是模块级的内存状态,用例之间不能互相串。"""
	from tools.task import ACTIVE
	ACTIVE.clear()
	yield
	ACTIVE.clear()


# ---------------------------------------------------------------- 一、跨会话

def test_两个会话看到同一张任务表(env):
	db, store, sid = env
	other = store.create_session("项目记忆", "用户记忆")["id"]
	enable(store, sid, other)

	ok(tools_for(sid), "task_create", name="共享的活", description="做完它")
	seen = ok(tools_for(other), "task_read")
	assert [t["name"] for t in seen["tasks"]] == ["共享的活"]
	assert seen["tasks"][0]["description"] == "做完它"


def test_一个会话关掉task_不影响另一个会话和任务数据(env):
	db, store, sid = env
	other = store.create_session("项目记忆", "用户记忆")["id"]
	enable(store, sid, other)

	created = ok(tools_for(sid), "task_create", name="别删我")["task"]
	store.set_task_enabled(other, False)

	# 另一个会话照样看得见、改得动
	assert ok(tools_for(sid), "task_read", task_id=created["id"])["task"]["name"] \
		== "别删我"
	assert ok(tools_for(sid), "task_edit", task_id=created["id"], name="改了") \
		["task"]["name"] == "改了"
	# 而关掉的那个:工具集里连 task_read 都没有
	assert "task_read" not in tools_for(other)
	# **关掉不等于删掉**:再打开还是同一张表、同一条任务
	store.set_task_enabled(other, True)
	assert ok(tools_for(other), "task_read")["count"] == 1
	assert raw(db, "SELECT name FROM tasks") == [("改了",)]


def test_新会话默认关着(env):
	db, store, sid = env
	assert store.task_enabled(sid) is False
	assert [t.name for t in server.turn_tools(sid, "t1")] == ["agent"]


# ------------------------------------------------------ 二、认领必须原子

def test_两个会话同时认领_恰好一个成功(env):
	"""真开两个线程抢,不是模拟先后。

	认领那条 UPDATE 的 WHERE 里带着"status = pending 且前置都完成",所以
	谁先到谁拿到;先读后写的写法在这里会两个都成功,而且不会报错。
	"""
	db, store, sid = env
	other = store.create_session("项目记忆", "用户记忆")["id"]
	enable(store, sid, other)

	task_id = ok(tools_for(sid), "task_create", name="只能一个人干")["task"]["id"]
	barrier = threading.Barrier(2)
	results = []

	def grab(s, turn):
		tools = tools_for(s, turn)
		barrier.wait()          # 尽量让两条 UPDATE 撞在一起
		results.append(call(tools, "task_status", task_id=task_id, action="start"))

	threads = [threading.Thread(target=grab, args=(s, f"turn-{i}"))
	           for i, s in enumerate((sid, other))]
	for t in threads:
		t.start()
	for t in threads:
		t.join()

	assert len(results) == 2
	won = [r for r in results if "error" not in r]
	lost = [r for r in results if "error" in r]
	assert len(won) == 1, results
	assert len(lost) == 1, results
	# 输的那个说得清是为什么 —— 三种原因要模型做的事完全不同
	assert lost[0]["error"] in ("invalid_state", "running"), lost
	assert raw(db, "SELECT COUNT(*) FROM tasks WHERE status = 'in_progress'") == [(1,)]


def test_认领失败不启动独立agent(env, monkeypatch):
	"""task_status 的认领结果不应自动派出子 agent。"""
	db, store, sid = env
	enable(store, sid)
	started = []

	def fake_loop(messages, **kw):
		started.append(messages)
		return TurnOutcome("completed", "干完了")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)

	blocked = ok(tools_for(sid), "task_create", name="先做别的")["task"]
	second = ok(tools_for(sid), "task_create", name="等它")["task"]
	ok(tools_for(sid), "task_dependency", task_id=second["id"],
	   depends_on_id=blocked["id"], action="add")

	got = call(tools_for(sid), "task_status", task_id=second["id"],
	           action="start")
	assert got["error"] == "blocked", got
	assert started == []
	# ready 任务的状态可以独立认领,子 agent 只接受 prompt。
	ok(tools_for(sid), "task_status", task_id=blocked["id"], action="start")
	assert tools_for(sid)["agent"].handler(prompt="查一件事") == "干完了"
	assert len(started) == 1


# ------------------------------------------------------------ 三、依赖图

def test_多前置全完成才ready(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	first = ok(tools, "task_create", name="一")["task"]
	second = ok(tools, "task_create", name="二")["task"]
	last = ok(tools, "task_create", name="三",
	          depends_on_ids=[first["id"], second["id"]])["task"]

	assert last["state"] == "blocked"
	assert {d["name"] for d in last["blocking"]} == {"一", "二"}
	ok(tools, "task_status", task_id=first["id"], action="start")
	ok(tools, "task_status", task_id=first["id"], action="complete")
	assert ok(tools, "task_read", task_id=last["id"])["task"]["state"] == "blocked"
	ok(tools, "task_status", task_id=second["id"], action="start")
	ok(tools, "task_status", task_id=second["id"], action="complete")
	assert ok(tools, "task_read", task_id=last["id"])["task"]["state"] == "ready"


def test_自依赖_重复边_环_都拒绝(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	b = ok(tools, "task_create", name="b", depends_on_ids=[a["id"]])["task"]

	# 自依赖:新建时不可能(还没有 id),有了 id 之后加边就可能了
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id=a["id"], action="add") == "cycle"
	# 重复边
	assert fails(tools, "task_dependency", task_id=b["id"],
	             depends_on_id=a["id"], action="add") == "duplicate_edge"
	# 环:b 已经依赖 a,再让 a 依赖 b 就绕回去了
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id=b["id"], action="add") == "cycle"
	# 不存在的任务:id 打错了要说 not_found,不能和"成环"混成一句话
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id="查无此任务", action="add") == "not_found"


def test_三条边的环也拒绝(env):
	"""两个点的环好查,顺着走三跳的才是真正要判的那个。"""
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	b = ok(tools, "task_create", name="b", depends_on_ids=[a["id"]])["task"]
	c = ok(tools, "task_create", name="c", depends_on_ids=[b["id"]])["task"]
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id=c["id"], action="add") == "cycle"
	# 合法方向仍然加得上
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id=a["id"], action="add") == "cycle"


def test_取消前置_后续仍然blocked(env):
	"""依赖被取消时后续**继续 blocked**,直到人调整依赖或另建替代任务。

	自动把"前置没了"当成"前置完成了"是在替用户做决定:那条活的产物根本
	不存在,后面这条会踩空,而且不报错。
	"""
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	b = ok(tools, "task_create", name="b", depends_on_ids=[a["id"]])["task"]
	ok(tools, "task_status", task_id=a["id"], action="cancel")
	after = ok(tools, "task_read", task_id=b["id"])["task"]
	assert after["state"] == "blocked"
	assert [d["status"] for d in after["blocking"]] == ["cancelled"]
	# 另建替代任务再改依赖,这条路走得通
	alt = ok(tools, "task_create", name="a2")["task"]
	ok(tools, "task_dependency", task_id=b["id"], depends_on_id=a["id"],
	   action="remove")
	assert ok(tools, "task_read", task_id=b["id"])["task"]["state"] == "ready"


# ------------------------------------------------------ 四、运行中不可改

def test_运行中不能改要求也不能改依赖(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	b = ok(tools, "task_create", name="b")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")

	assert fails(tools, "task_edit", task_id=a["id"],
	             description="换一套要求") == "invalid_state"
	assert fails(tools, "task_edit", task_id=a["id"], name="改名") == "invalid_state"
	assert fails(tools, "task_dependency", task_id=a["id"],
	             depends_on_id=b["id"], action="add") == "invalid_state"
	# 也不能再次认领
	assert fails(tools, "task_status", task_id=a["id"], action="start") \
		in ("invalid_state", "running")
	# 原文一个字没动
	assert ok(tools, "task_read", task_id=a["id"])["task"]["description"] == ""


def test_已完成是终态_不能改也不能取消(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")
	ok(tools, "task_status", task_id=a["id"], action="complete")

	assert fails(tools, "task_edit", task_id=a["id"], name="改") == "invalid_state"
	assert fails(tools, "task_status", task_id=a["id"], action="cancel") \
		== "invalid_state"
	assert fails(tools, "task_status", task_id=a["id"], action="retry") \
		== "invalid_state"


def test_独立agent返回不改变任务状态(env, monkeypatch):
	db, store, sid = env
	enable(store, sid)
	said = {}

	def fake_loop(messages, **kw):
		said["messages"] = messages
		return TurnOutcome("completed", "查完了")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="查一件事", description="要求:查清楚")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")

	text = tools["agent"].handler(prompt="独立调查")
	sent = said["messages"][0]["content"]
	assert sent == "独立调查"
	assert text == "查完了"
	assert ok(tools, "task_read", task_id=a["id"])["task"]["status"] == "in_progress"
	ok(tools, "task_status", task_id=a["id"], action="complete")
	assert ok(tools, "task_read", task_id=a["id"])["task"]["status"] == "completed"


def test_agent只接受prompt(env):
	db, store, sid = env
	enable(store, sid)
	agent = tools_for(sid)["agent"]
	# 入参是 prompt(必填)+ run_in_background(可选,后台执行那一版加的)。
	# **没有 task_id** —— "agent 跟 task 开关无关"这条就落在这一句上。
	assert set(agent.input_schema["properties"]) == {"prompt", "run_in_background"}
	assert agent.input_schema["required"] == ["prompt"]
	assert agent.handler(prompt=" ").startswith("Error:")
	with pytest.raises(TypeError):
		agent.handler(task_id="不再支持")


# ------------------------------------------------------ 五、绝不自动重试

def test_原执行还在跑时不许重试(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")
	# start 占着不放,直到这一轮结束(设计 §5)
	assert fails(tools, "task_status", task_id=a["id"], action="retry") == "running"
	# 轮末松手之后可以重试
	from tools.task import release_turn
	release_turn("turn-1")
	ok(tools, "task_status", task_id=a["id"], action="retry")
	assert ok(tools, "task_read", task_id=a["id"])["task"]["status"] == "pending"


def test_别的会话不能替它完成或重试(env):
	db, store, sid = env
	other = store.create_session("项目记忆", "用户记忆")["id"]
	enable(store, sid, other)
	tools, theirs = tools_for(sid), tools_for(other, "turn-2")
	a = ok(tools, "task_create", name="a")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")

	assert fails(theirs, "task_status", task_id=a["id"], action="complete") == "running"
	assert fails(theirs, "task_status", task_id=a["id"], action="retry") == "running"
	# 自己这一轮可以提交
	ok(tools, "task_status", task_id=a["id"], action="complete")


def test_进程重启后不自动重试(env):
	"""占用记录是内存里的,重启就没了;而**任务还停在 in_progress**。

	不自动改回 pending 是有意的:那一轮可能已经改了文件、发了请求,自动
	重来等于把那些副作用再做一遍,而且没人知道。
	"""
	db, store, sid = env
	enable(store, sid)
	a = ok(tools_for(sid), "task_create", name="a")["task"]
	ok(tools_for(sid), "task_status", task_id=a["id"], action="start")

	from tools.task import ACTIVE, release_turn
	release_turn("turn-1")                       # 模拟进程退出:内存那份没了
	assert ACTIVE == {}
	restarted = sessions.SessionStore(db)        # 同一个库,新进程
	assert restarted.get_task(a["id"])["status"] == "in_progress"
	# 而它没被任何东西攥着:核对过之后显式 retry 这条路是通的(但要人显式走)
	ok(tools_for(sid), "task_status", task_id=a["id"], action="retry")


def test_独立agent失败不改变任务状态(env, monkeypatch):
	db, store, sid = env
	enable(store, sid)
	monkeypatch.setattr(subagent, "agent_loop",
	                    lambda messages, **kw: TurnOutcome("failed", "", "炸了"))
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	ok(tools, "task_status", task_id=a["id"], action="start")
	assert "subagent failed" in tools["agent"].handler(prompt="调查")
	assert ok(tools, "task_read", task_id=a["id"])["task"]["status"] == "in_progress"


# ------------------------------------------------------ 六、关着就看不见

def test_关着的时候模型看不到任务工具_但agent还在(env, monkeypatch):
	db, store, sid = env
	monkeypatch.setattr(subagent, "agent_loop",
	                    lambda messages, **kw: TurnOutcome("completed", "结论"))
	names = [t.name for t in server.turn_tools(sid, "t1")]
	assert names == ["agent"], names
	agent_tool = server.turn_tools(sid, "t1")[0]
	assert set(agent_tool.input_schema["properties"]) == {
		"prompt", "run_in_background"}
	assert agent_tool.handler(prompt="查个东西") == "结论"
	with pytest.raises(TypeError):
		agent_tool.handler(task_id="随便")


def test_开着的时候task工具不改变agent接口(env, monkeypatch):
	db, store, sid = env
	enable(store, sid)
	names = [t.name for t in server.turn_tools(sid, "t1")]
	assert set(names) == {"agent", "task_read", "task_create", "task_edit",
	                      "task_dependency", "task_status"}, names
	agent_tool = [t for t in server.turn_tools(sid, "t1") if t.name == "agent"][0]
	assert set(agent_tool.input_schema["properties"]) == {
		"prompt", "run_in_background"}

	seen = {}
	monkeypatch.setattr(subagent, "agent_loop",
	                    lambda messages, **kw: seen.update(kw) or
	                    TurnOutcome("completed", "结论"))
	agent_tool.handler(prompt="查个东西")
	sub_names = {t.name for t in seen["tools"]}
	assert not (sub_names & {"task_read", "task_create", "task_edit",
	                         "task_dependency", "task_status", "agent"}), sub_names


def test_开关进恢复签名(env):
	"""开和关是**两套工具集**,签名必须跟着变。

	不变的话,一个关着 task 的会话能恢复一轮当时开着 task 的检查点 ——
	模型会拿着一条它现在根本没有的工具调用往下跑。
	"""
	db, store, sid = env
	off = server.signature_tools(sid)
	enable(store, sid)
	on = server.signature_tools(sid)
	assert [t.name for t in off] != [t.name for t in on]
	assert "task_read" not in [t.name for t in off]
	assert "task_read" in [t.name for t in on]


# ------------------------------------------------- 七、原子性 / 不持事务

def test_建任务和依赖边同事务(env):
	"""依赖 id 不存在时**任务也不该留下** —— 分开写的话它会留下一条 ready
	的任务,而它正好可以被另一个会话认领开跑。"""
	db, store, sid = env
	enable(store, sid)
	result = call(tools_for(sid), "task_create", name="先做别的",
	              depends_on_ids=["查无此任务", "另一个也不存在"])
	assert result["error"] == "not_found", result
	assert raw(db, "SELECT COUNT(*) FROM tasks") == [(0,)]
	assert raw(db, "SELECT COUNT(*) FROM task_dependencies") == [(0,)]


def test_模型和子agent执行期间不持SQLite事务(env, monkeypatch):
	"""事务只圈毫秒级的 SQL,不圈一次可能跑几分钟的模型调用。

	持有的话,整个会话库在这期间对别的会话是关着的 —— 页面刷新、另一个
	会话存消息,全都堵在那儿等,而它们看不出为什么。
	"""
	db, store, sid = env
	enable(store, sid)
	inside = {}

	def fake_loop(messages, **kw):
		# 子 agent 执行中:连接上不许挂着事务
		inside["subagent"] = store._conn.in_transaction
		return TurnOutcome("completed", "结论")

	monkeypatch.setattr(subagent, "agent_loop", fake_loop)
	tools = tools_for(sid)
	tools["agent"].handler(prompt="独立调查")
	assert inside["subagent"] is False
	assert store._conn.in_transaction is False
	# 认领那条也一样:认领完了不该留着事务
	b = ok(tools, "task_create", name="b")["task"]
	ok(tools, "task_status", task_id=b["id"], action="start")
	assert store._conn.in_transaction is False


def test_工具只改明确传入的字段(env):
	"""两个会话同时编辑同一条任务时,整行覆盖会把对方刚改的字段抹掉,而且
	不报错。所以 SET 子句只带调用方给的那几列。"""
	db, store, sid = env
	other = store.create_session("项目记忆", "用户记忆")["id"]
	enable(store, sid, other)
	ok(tools_for(sid), "task_create", name="原名", description="原要求",
	   owner="alice")
	# 另一个会话只改名字
	ok(tools_for(other, "turn-2"), "task_edit", task_id=_only_task(db), name="新名")
	task = ok(tools_for(sid), "task_read", task_id=_only_task(db))["task"]
	assert task["name"] == "新名"
	assert task["description"] == "原要求", "整行覆盖把别人改的字段抹了"
	assert task["owner"] == "alice"


def _only_task(db) -> str:
	return raw(db, "SELECT id FROM tasks")[0][0]


# ------------------------------------------------------------ 读的过滤

def test_默认只列未完成的(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	ok(tools, "task_create", name="b")
	ok(tools, "task_status", task_id=a["id"], action="start")
	ok(tools, "task_status", task_id=a["id"], action="complete")

	assert [t["name"] for t in ok(tools, "task_read")["tasks"]] == ["b"]
	assert {t["name"] for t in ok(tools, "task_read", status="all")["tasks"]} \
		== {"a", "b"}
	assert [t["name"] for t in ok(tools, "task_read", status="completed")["tasks"]] \
		== ["a"]
	assert fails(tools, "task_read", status="查无此状态") == "invalid_input"


def test_详情给两个方向的依赖(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	a = ok(tools, "task_create", name="a")["task"]
	b = ok(tools, "task_create", name="b", depends_on_ids=[a["id"]])["task"]
	detail = ok(tools, "task_read", task_id=a["id"])["task"]
	# 只给 depends_on 的话,"我把它取消了会挡住谁"这个问题没人答得上
	assert [d["name"] for d in detail["dependents"]] == ["b"]
	assert [d["name"] for d in detail["depends_on"]] == []


def test_查不到的id说not_found(env):
	db, store, sid = env
	enable(store, sid)
	tools = tools_for(sid)
	assert fails(tools, "task_read", task_id="查无此任务") == "not_found"
	assert fails(tools, "task_edit", task_id="查无此任务", name="x") == "not_found"
	assert fails(tools, "task_status", task_id="查无此任务", action="start") \
		== "not_found"
	assert fails(tools, "task_status", task_id="查无此任务", action="飞升") \
		== "invalid_input"
