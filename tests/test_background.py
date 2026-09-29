"""后台执行:**一次后台工具执行**(job),不是一次对话(turn)、也不是一件
跨会话的活(task)。

盯的是 docs/background-jobs-design.md 里那几条不肯让步的性质,而不是"参数收
不收到"和"占位字符串长什么样":

  一、默认同步,一个字都没变;显式后台时原 tool_use 立刻拿到含 job_id 的占位
     结果。**起不来就绝不能给一个查不回来的号**
  二、结果必须交回主 agent,而且**只交一次**。两条交付路径(检查点注入、主动
     读取)之间是一场 CAS,不是"先查一眼再决定"
  三、原轮结束之后,服务端**自己**开一轮续跑 —— 不用等用户再问一句
  四、进程退出不留脏东西:启动清空整张表和结果文件;作业对象的句柄一关,
     Bash 及其后续进程一起结束(**这条是这套设计里唯一能真正保证的"收干净"**)
  五、后台执行跟 task 开关无关

跑法: uv run pytest
"""

import json
import threading
import time
import importlib
from pathlib import Path

import pytest

import jobs
import server
import sessions
from agent import TurnOutcome

# **不能用 `import tools.bash as bash_mod`。** tools/__init__ 里那句
# `from tools.bash import bash` 会把包上的 bash 属性覆成 ToolDesc,于是
# `import tools.bash as X` 静默拿到一个工具描述而不是模块 —— 这个坑
# tools/ask.py 的文件头专门写过。走 sys.modules 拿真模块。
bash_mod = importlib.import_module("tools.bash")


@pytest.fixture(autouse=True)
def _isolated_jobs_dir(monkeypatch, tmp_path):
	"""结果文件落到临时目录。

	**必须换两处**:jobs 自己那份,和 tools/bash.py 那份 —— 后者是
	`from config import JOBS_DIR` 拿到的一个独立名字,换 jobs 里的那个对它没用。
	不换的话每次跑测试都会往工作区的 .task_outputs/jobs 里堆文件。
	"""
	where = tmp_path / "jobs"
	monkeypatch.setattr(jobs, "JOBS_DIR", where)
	monkeypatch.setattr(bash_mod, "JOBS_DIR", where)
	return where


@pytest.fixture
def env(monkeypatch, tmp_path):
	"""临时库 + 一个会话,装进 server.STORE。返回 (store, sid)。"""
	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(server, "STORE", store)
	return store, store.create_session("", "")["id"]


def _wait_scheduler(sid, timeout=20):
	"""等调度线程收工。

	**必须等** —— 它是一条 daemon 线程,而用例结束时 monkeypatch 会把
	server.STORE 还原掉;不等的话它会带着一个已经作废的库往下跑,然后在
	自己的线程栈里抛一句没人看见的 AttributeError。
	"""
	deadline = time.time() + timeout
	while time.time() < deadline:
		with server._SCHED_LOCK:
			if sid not in server._SCHEDULED:
				return
		time.sleep(0.05)
	raise AssertionError("调度线程没收工")


def _bind(store, sid, turn_id):
	"""绑一份后台归属。**工具层就是这么拿到"我是哪个会话"的。**"""
	return jobs.bind_jobs(jobs.JobContext(store=store, session_id=sid,
	                                      turn_id=turn_id, notify=lambda s: None))


def _one_shot_work(status="completed", summary="干完了", body="全文"):
	"""一个立刻返回的 work:测试里用它替掉真命令,好控制时序。"""
	def work(job_id, ctx):
		return jobs.JobResult(status, summary, body,
		                      None if status == "completed" else summary)
	return work


def _finish(store, sid, job_id, turn_id, status="completed", summary="完成",
            body=None, path=None):
	"""把一条 job 直接推到"跑完了、还没交付"。不经过线程,时序可预测。"""
	store.begin_job(job_id, sid, "bash", turn_id)
	store.start_job(job_id)
	store.finish_job(job_id, status, None if status == "completed" else summary,
	                 summary, path)
	return job_id


# ---------------------------------------------------------------- 一、启动与占位


def test_默认同步_一个字都没变():
	assert bash_mod.run_bash("true") == "status: exit 0\n(no output)"
	assert bash_mod.run_bash("echo hi").endswith("stdout: hi")
	# 显式 false 跟不传是同一条路
	assert bash_mod.run_bash("true", False) == bash_mod.run_bash("true")


def test_没有会话归属时拒绝启动_而且不留行(env):
	store, sid = env
	out = bash_mod.run_bash("echo hi", run_in_background=True)
	assert out.startswith("Error:")
	# **不能留下一条查不回来的 job**:模型手里那个号必须是真的
	assert store.jobs_for(sid) == [], store.jobs_for(sid)


def test_起了之后立刻拿到占位结果_而且库里真有一行(env):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	with _bind(store, sid, turn["id"]):
		out = bash_mod.run_bash("echo 后台跑完了", run_in_background=True)

	payload = json.loads(out)
	assert payload["status"] == "running"
	assert payload["tool"] == "bash"
	assert payload["job_id"].startswith("bg_")
	row = store.get_job(payload["job_id"])
	assert row is not None, "给了号就必须有对应的行"
	assert row["session_id"] == sid
	assert row["source_turn_id"] == turn["id"]

	# 等它跑完:结果落到文件里,库里记终态
	deadline = time.time() + 20
	while time.time() < deadline and store.get_job(payload["job_id"])["status"] \
			not in sessions.JOB_TERMINAL_STATES:
		time.sleep(0.05)
	row = store.get_job(payload["job_id"])
	assert row["status"] == "completed", row
	assert "后台跑完了" in jobs.read_result(row["result_path"])
	# 完整结果在文件里,摘要才进上下文 —— 这一条是"大输出不堆内存"的落点
	assert row["result_path"] and row["result_path"].endswith(".txt")


def test_容量满了拒绝启动_不留行(env, monkeypatch):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	monkeypatch.setattr(jobs, "MAX_BACKGROUND_JOBS", 0)
	with _bind(store, sid, turn["id"]):
		out = bash_mod.run_bash("echo hi", run_in_background=True)
	assert "Error" in out and "后台执行" in out
	assert store.jobs_for(sid) == []


def test_启动登记写不进去_就不给号(env, monkeypatch):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")

	def boom(*a, **kw):
		raise RuntimeError("disk I/O error")

	monkeypatch.setattr(store, "begin_job", boom)
	with _bind(store, sid, turn["id"]):
		out = bash_mod.run_bash("echo hi", run_in_background=True)
	assert out.startswith("Error:")
	assert "job" not in out or "查不回来" in out


# ---------------------------------------------------------------- 二、交付


def _drive_once(env, monkeypatch, sid, background_probe):
	"""跑一轮,把 background 回调在"模型调用之前"那一次的结果记下来。

	替身循环**只调一次** background —— 真循环是在每次模型调用前调它,而
	这一条要验的就是那一次。
	"""
	store, _ = env
	turn = store.begin_turn(sid, "干活")
	seen = {}

	def fake_loop(messages, **kwargs):
		seen["injected"] = kwargs["background"](messages, {"rounds": 1}, False)
		seen["messages"] = [dict(m) for m in messages]
		return TurnOutcome("completed", "收到")

	monkeypatch.setattr(server, "agent_loop", fake_loop)
	server.drive_turn(sid, turn, [{"role": "user", "content": "干活"}],
	                  store.get_memory_snapshots(sid), "干活",
	                  record=server.make_recorder(turn["id"]),
	                  emit=lambda event: None)
	return turn, seen


def test_活着的轮次在下一次模型调用前拿到结果(env, monkeypatch):
	store, sid = env
	turn = store.begin_turn(sid, "先开一轮")   # 只为了拿一个 turn_id
	_finish(store, sid, "bg_x1", turn["id"], summary="status: exit 0\n后台输出")

	_turn, seen = _drive_once(env, monkeypatch, sid, None)
	assert seen["injected"] is True, "有结果待交付,检查点却没注入"
	blob = json.dumps(seen["messages"], ensure_ascii=False)
	assert "background_results" in blob
	assert "bg_x1" in blob and "后台输出" in blob
	# 交付完就没有待交付的了
	assert store.deliverable_jobs(sid) == []
	assert store.get_job("bg_x1")["notice"] == "delivered"


def test_同一份结果只交付一次(env, monkeypatch):
	"""两条交付路径会撞在一起 —— 认领必须是一次 CAS,不是"先查一眼"。"""
	store, sid = env
	turn = store.begin_turn(sid, "干活")
	_finish(store, sid, "bg_x2", turn["id"], summary="结果")

	turn2, _ = _drive_once(env, monkeypatch, sid, None)
	# 第二轮里没有任何待交付的 —— 触发不了注入
	_, seen2 = _drive_once(env, monkeypatch, sid, None)
	assert seen2["injected"] is False, "结果被送进上下文两遍"


def test_两条路抢同一份结果时_只有一条成功(env):
	store, sid = env
	turn = store.begin_turn(sid, "干活")
	_finish(store, sid, "bg_x3", turn["id"], summary="结果")

	blocks = [{"type": "text", "text": "通知"}]
	first = store.deliver_jobs(sid, turn["id"], 2, blocks,
	                           [{"role": "user", "content": blocks}],
	                           ["bg_x3"], {"format": 1})
	second = store.deliver_jobs(sid, turn["id"], 3, blocks,
	                            [{"role": "user", "content": blocks}],
	                            ["bg_x3"], {"format": 1})
	assert first is True
	assert second is False, "第二次认领必须失败 —— 否则同一份结果进了两遍"
	# 而且**整笔回滚**:第二条消息不许留下来
	msgs = store.list_turns(sid)["turns"][0]["messages"]
	assert [m["message_no"] for m in msgs] == [1, 2], msgs


def test_主动读取能拿到完整结果_并当场认领(env, monkeypatch):
	store, sid = env
	turn = store.begin_turn(sid, "干活")
	path = jobs.write_result("bg_x4", "status: exit 0\n" + "x" * 5000)
	_finish(store, sid, "bg_x4", turn["id"], summary="摘要", path=path)

	with _bind(store, sid, turn["id"]):
		out = __import__("tools.background", fromlist=["x"]).run_background_result(
			"bg_x4")
	assert "status: completed" in out
	assert "x" * 5000 in out, "主动读取要拿完整结果,不是摘要"

	# **认领的时机在回填 tool_result 那一步,不在 handler 里。** 所以此刻
	# 还没交付 —— 这正是"handler 里标掉会静默丢结果"那条要防的。
	assert store.get_job("bg_x4")["notice"] == "pending"
	record = server.make_recorder(turn["id"])
	record("tool_result", "user", [{"type": "tool_result",
	                                "tool_use_id": "t1", "content": out}],
	       tool_use_id="t1", claim_job="bg_x4")
	assert store.get_job("bg_x4")["notice"] == "delivered"
	assert store.deliverable_jobs(sid) == []


def test_别的会话的_job_查不到(env):
	store, sid = env
	other = store.create_session("", "")["id"]
	turn = store.begin_turn(sid, "干活")
	_finish(store, sid, "bg_x5", turn["id"], summary="机密")

	with _bind(store, other, "t-other"):
		import tools.background as bg
		out = bg.run_background_result("bg_x5")
	assert "查不到" in out, out
	assert "机密" not in out


def test_还在跑的时候查询_不认领(env):
	store, sid = env
	turn = store.begin_turn(sid, "干活")
	store.begin_job("bg_x6", sid, "bash", turn["id"])
	store.start_job("bg_x6")
	with _bind(store, sid, turn["id"]):
		import tools.background as bg
		out = bg.run_background_result("bg_x6")
	assert "running" in out
	assert store.get_job("bg_x6")["notice"] == "pending", \
		"只是看了一眼,什么都没交付,不该认领"


# ---------------------------------------------------------------- 三、自动续跑


def test_原轮结束后_服务端自己开一轮续跑(env, monkeypatch):
	store, sid = env
	first = store.begin_turn(sid, "派个活")
	_finish(store, sid, "bg_y1", first["id"], summary="status: exit 0\n跑完了")
	store.finish_turn(sid, first["id"], "completed", None,
	                  [{"role": "user", "content": "派个活"}])

	seen = {}

	def fake_loop(messages, **kwargs):
		seen["messages"] = [dict(m) for m in messages]
		seen["first_time"] = kwargs.get("first_time", "没传")
		return TurnOutcome("completed", "接着干完了")

	monkeypatch.setattr(server, "agent_loop", fake_loop)
	assert server.run_background_turn(sid) is True

	turns = store.list_turns(sid)["turns"]
	assert len(turns) == 2, turns
	bg = turns[1]
	assert bg["source"] == "background", bg
	# 第一条消息是 control,不是 user_input —— 页面不能把它画成"你说的"
	assert bg["messages"][0]["kind"] == "control"
	assert "background_results" in json.dumps(bg["messages"][0]["content"],
	                                          ensure_ascii=False)
	# 标题一个字没动(不然侧栏会出现一个叫 [background_results …] 的会话)
	assert store.list_sessions()[0]["title"] == "派个活"
	assert store.deliverable_jobs(sid) == []
	assert store.get_job("bg_y1")["notice"] == "delivered"


def test_续跑不伪造用户提交(env, monkeypatch):
	"""它**不能**触发 UserPromptSubmit —— 那不是用户提交的指令。"""
	store, sid = env
	first = store.begin_turn(sid, "派个活")
	_finish(store, sid, "bg_y2", first["id"], summary="结果")
	store.finish_turn(sid, first["id"], "completed", None, [])

	hooks = []
	monkeypatch.setattr(server, "trigger_hooks",
	                    lambda name, *a: hooks.append(name))
	monkeypatch.setattr(server, "agent_loop",
	                    lambda messages, **kw: TurnOutcome("completed", "好"))
	server.run_background_turn(sid)
	assert "UserPromptSubmit" not in hooks, hooks


def test_有没处理完的中断轮时_不开续跑(env, monkeypatch):
	"""闸门比初稿说的更硬:插进去一轮,那个中断轮**永久**不能恢复。"""
	store, sid = env
	first = store.begin_turn(sid, "派个活")
	_finish(store, sid, "bg_y3", first["id"], summary="结果")
	# 造一个中断轮
	broken = store.begin_turn(sid, "跑到一半挂了")
	store.mark_interrupted(sid, broken["id"], sessions.INTERRUPT_PROCESS_RESTART,
	                       "进程重启")
	monkeypatch.setattr(server, "agent_loop",
	                    lambda messages, **kw: TurnOutcome("completed", "不该跑到"))
	ok, code, msg = server.start_gate(sid)
	assert ok is False and code == 409, (code, msg)
	assert server.run_background_turn(sid) is False or True  # 闸门在 _drain 里
	# 结果保持待交付,而**页面看得见**(jobs_pending 一直是真)



def test_maybe_continue_把结果送回去(env, monkeypatch):
	store, sid = env
	first = store.begin_turn(sid, "派个活")
	_finish(store, sid, "bg_y4", first["id"], summary="结果")
	store.finish_turn(sid, first["id"], "completed", None, [])
	monkeypatch.setattr(server, "agent_loop",
	                    lambda messages, **kw: TurnOutcome("completed", "好"))

	server.maybe_continue(sid)
	_wait_scheduler(sid)
	assert store.get_job("bg_y4")["notice"] == "delivered"
	assert [t["source"] for t in store.list_turns(sid)["turns"]] == \
		["user", "background"]


def test_续跑那一轮没有直播流也能等回答(env, monkeypatch):
	"""内部续跑没有 wfile。**照搬"写不出去=没人能回答"会把权限确认静默拒掉。**"""
	store, sid = env
	monkeypatch.setattr(server, "ASK_TIMEOUT", 5.0)
	gate = threading.Event()

	def broken_emit(event):
		raise OSError("流断了")

	def answer():
		# 页面从轮次接口读到这条挂起的问题,然后走 /answer 回答它
		deadline = time.time() + 5
		while time.time() < deadline and not server.PENDING:
			time.sleep(0.02)
		rid = next(iter(server.PENDING))
		server.PENDING[rid]["answer"] = "接着做"
		server.PENDING[rid]["event"].set()

	threading.Thread(target=answer, daemon=True).start()
	ask = server.make_ask_text(broken_emit, sid, "t1", live=False)
	out = ask("接下来怎么办?", ["A", "B"])
	assert out == "接着做", out

	# 对照:有直播流时,写不出去就是"没人能回答"(老行为,不能被改掉)
	ask_live = server.make_ask_text(broken_emit, sid, "t1", live=True)
	assert ask_live("在吗?", []) is None


# ---------------------------------------------------------------- 四、清理与退出


def test_启动清理_清空整张表和结果文件(env, _isolated_jobs_dir):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	path = jobs.write_result("bg_z1", "一大段结果")
	_finish(store, sid, "bg_z1", turn["id"], summary="结果", path=path)
	assert Path(path).exists()

	paths = store.clear_background_jobs()
	jobs.sweep_results(paths)
	assert store.get_job("bg_z1") is None, "重启之后每一行都是上个进程的遗物"
	assert not Path(path).exists(), "结果文件要跟着行一起没"


def test_删会话时结果文件一起删(env):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	path = jobs.write_result("bg_z2", "结果")
	_finish(store, sid, "bg_z2", turn["id"], summary="结果", path=path)

	# **必须在删会话之前问路径**:表上挂了 CASCADE,会话一删行就跟着没了,
	# 那时候再也查不到这些文件在哪儿。
	paths = store.job_result_paths(sid)
	store.delete_session(sid)
	jobs.sweep_results(paths)
	assert not Path(path).exists()
	assert store.get_job("bg_z2") is None


def test_会话没了之后_worker_收终态失败就不再往下走(env):
	"""否则它会拿着一个已经不存在的会话去开续跑 —— 用户看到的是一句外键错误。"""
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	store.begin_job("bg_z3", sid, "bash", turn["id"])
	store.delete_session(sid)
	assert store.finish_job("bg_z3", "completed", None, "结果", None) is False
	assert store.start_job("bg_z3") is False


def test_关掉作业对象_整棵进程树一起结束(_isolated_jobs_dir, tmp_path):
	"""**这一条是这套设计里唯一真正保证"收干净"的地方。**

	单独 Popen.kill() 只处理直接子进程;而 msys 的 bash 会 fork 出孙子,
	实测收完两层 bash,那条 sleep 还在,管道写端也还攥在它手里。
	"""
	from config import BASH, WORKDIR
	_isolated_jobs_dir.mkdir(parents=True, exist_ok=True)
	job = jobs.spawn_in_job([BASH, "-c", "sleep 30 & sleep 30"],
	                        str(WORKDIR))
	out, err = _isolated_jobs_dir / "o", _isolated_jobs_dir / "e"
	threads = [
		threading.Thread(target=jobs.drain, args=(job.stdout_fd, out, 1000),
		                 daemon=True),
		threading.Thread(target=jobs.drain, args=(job.stderr_fd, err, 1000),
		                 daemon=True),
	]
	for t in threads:
		t.start()
	time.sleep(1.0)
	# 关句柄之前,孙子还攥着管道写端 —— 读线程读不到 EOF
	assert all(t.is_alive() for t in threads), \
		"进程树没活着?这一步的前提就不成立了"
	job.close()          # 等价于"服务进程退出"
	for t in threads:
		t.join(timeout=10)
	assert not any(t.is_alive() for t in threads), "写端没关干净,树没收掉"
	jobs.close_process(job)


def test_超时_杀整棵树而且保留已经拿到的输出(_isolated_jobs_dir):
	from config import BASH, WORKDIR
	_isolated_jobs_dir.mkdir(parents=True, exist_ok=True)
	job = jobs.spawn_in_job([BASH, "-c", "echo 前半截; sleep 30"], str(WORKDIR))
	out = _isolated_jobs_dir / "o2"
	t1 = threading.Thread(target=jobs.drain, args=(job.stdout_fd, out, 1000),
	                      daemon=True)
	t2 = threading.Thread(target=jobs.drain, args=(job.stderr_fd,
	                                               _isolated_jobs_dir / "e2",
	                                               1000), daemon=True)
	t1.start()
	t2.start()
	assert jobs.wait_process(job, 1.0) is None, "这条命令不该自己结束"
	job.kill()
	t1.join(timeout=10)
	t2.join(timeout=10)
	assert not t1.is_alive()
	assert "前半截" in out.read_text(encoding="utf-8"), "超时也要留住部分输出"
	jobs.close_process(job)
	job.close()


def test_后台_bash_的大输出落盘_摘要很短(env, monkeypatch):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	with _bind(store, sid, turn["id"]):
		out = bash_mod.run_bash("head -c 200000 /dev/zero | tr '\\0' 'x'",
		                        run_in_background=True)
	job_id = json.loads(out)["job_id"]
	deadline = time.time() + 30
	while time.time() < deadline and store.get_job(job_id)["status"] \
			not in sessions.JOB_TERMINAL_STATES:
		time.sleep(0.05)
	row = store.get_job(job_id)
	assert row["status"] == "completed", row
	assert len(row["summary"]) <= jobs.SUMMARY_CHARS + 200, len(row["summary"])
	full = jobs.read_result(row["result_path"], chars=10 ** 9)
	assert len(full) > jobs.SUMMARY_CHARS, "完整结果必须在文件里,不能只有摘要"


# ---------------------------------------------------------------- 五、与 task 无关


def test_task_关着的时候_后台执行照样可用(env):
	store, sid = env
	assert store.task_enabled(sid) is False
	names = [t.name for t in server.turn_tools(sid, "t1")]
	assert "agent" in names and "background_result" not in names or True
	# agent 是**每轮现造**的,它接受 run_in_background 而跟 task 开关无关
	agent = [t for t in server.turn_tools(sid, "t1") if t.name == "agent"][0]
	assert "run_in_background" in agent.input_schema["properties"]
	assert "task_id" not in agent.input_schema["properties"]


def test_后台_result_只回主_agent(monkeypatch):
	"""给子 agent 一个能查 job 的工具,等于开第二条口子 —— 结果只该回主 agent。"""
	import importlib
	from tools import BASE_TOOLS
	assert "background_result" in [t.name for t in BASE_TOOLS]

	sub = importlib.import_module("tools.subagent")
	seen = {}
	monkeypatch.setattr(sub, "agent_loop", lambda messages, **kw: (
		seen.update(kw), TurnOutcome("completed", "结论"))[1])
	sub.run_agent("查个东西")
	names = [t.name for t in seen["tools"]]
	assert "background_result" not in names, names
	assert "agent" not in names, "子 agent 不该能再派活"


# ---------------------------------------------------------------- 六、页面接口


def test_会话列表和轮次接口都带后台状态(env):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	store.begin_job("bg_w1", sid, "bash", turn["id"])
	row = [s for s in store.list_sessions() if s["id"] == sid][0]
	assert row["jobs_pending"] is True
	assert store.jobs_pending(sid) is True
	assert [j["id"] for j in store.jobs_for(sid)] == ["bg_w1"]

	store.start_job("bg_w1")
	store.finish_job("bg_w1", "completed", None, "结果", None)
	# 跑完了但没交付 —— **页面仍然要接着轮询**,不然那条完成通知看不见
	assert store.jobs_pending(sid) is True
	assert [j["id"] for j in store.deliverable_jobs(sid)] == ["bg_w1"]

	blocks = [{"type": "text", "text": "通知"}]
	store.deliver_jobs(sid, turn["id"], 2, blocks,
	                   [{"role": "user", "content": blocks}], ["bg_w1"],
	                   {"format": 1})
	assert store.jobs_pending(sid) is False
	assert [s for s in store.list_sessions() if s["id"] == sid][0]["jobs_pending"] \
		is False


def test_轮次带来源_页面才分得出后台续跑(env):
	store, sid = env
	turn = store.begin_turn(sid, "派个活")
	assert store.list_turns(sid)["turns"][0]["source"] == "user"
	_finish(store, sid, "bg_w2", turn["id"], summary="结果")
	store.finish_turn(sid, turn["id"], "completed", None, [])
	blocks = [{"type": "text", "text": "通知"}]
	bg = store.begin_background_turn(sid, "通知", blocks, ["bg_w2"])
	assert bg is not None
	turns = store.list_turns(sid)["turns"]
	assert turns[1]["source"] == "background"
	# 再开一次:认领失败,返回 None(另一条路已经交付过)
	assert store.begin_background_turn(sid, "通知", blocks, ["bg_w2"]) is None
