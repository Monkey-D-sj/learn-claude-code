"""会话库的特征化测试:行为变了就得响。

断言的是**外部可观察**的东西:落库的行、返回值、异常,以及"失败之后连接
还干不干净"。最后一条是要害:事务样板收成一个 helper 之后,回滚路径最容易
悄悄坏掉 —— 而它坏掉时不会报错,只会让下一个请求莫名其妙地失败。

跑法: uv run pytest
"""

import contextlib
import io
import sqlite3
import threading

import pytest

import sessions


@pytest.fixture
def db(tmp_path):
	"""每个用例一个全新的库文件。"""
	return tmp_path / "sessions.db"


@pytest.fixture
def store(db):
	return sessions.SessionStore(db)


def raw(db, sql, args=()):
	"""另开一个连接直接翻库 —— 绕开被测对象,看真正落下去的东西。"""
	conn = sqlite3.connect(db)
	try:
		return conn.execute(sql, args).fetchall()
	finally:
		conn.close()


def tables(db):
	"""库里的表名,排序后。用来钉"哪张表在、哪张表不在"。"""
	return [row[0] for row in raw(
		db, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]


def test_建会话_连空上下文一起(db, store):
	out = store.create_session("项目记忆", "用户记忆")
	assert set(out) == {"id", "title", "updated_at"}, out
	assert out["title"] == ""
	assert store.session_exists(out["id"]) is True
	assert store.session_exists("查无此会话") is False
	assert store.load_context(out["id"]) == []
	assert raw(db, "SELECT messages_json, version, last_compacted_at"
	              " FROM session_contexts") == [("[]", 1, None)]
	assert raw(db, "SELECT memory_snapshot, user_snapshot FROM sessions") \
		== [("项目记忆", "用户记忆")]
	assert store.list_sessions() == [out], store.list_sessions()


def test_开轮_发号_标题只认第一次(db, store):
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	t1 = store.begin_turn(sid, "  第一条   问题  ")
	assert t1["turn_no"] == 1 and t1["status"] == "running", t1
	t2 = store.begin_turn(sid, "第二条")
	assert t2["turn_no"] == 2, t2

	assert raw(db, "SELECT turn_no, status, finished_at, error_message"
	              " FROM turns ORDER BY turn_no") == [
		(1, "running", None, None), (2, "running", None, None)]
	assert raw(db, "SELECT title FROM sessions") == [("第一条 问题",)]
	assert raw(db, "SELECT turn_id, message_no, kind, role, content_json"
	              " FROM turn_messages ORDER BY id") == [
		(t1["id"], 1, "user_input", "user", '"  第一条   问题  "'),
		(t2["id"], 1, "user_input", "user", '"第二条"')]


def test_开轮失败要回滚_一行都不留_连接还能接着用(db, store):
	with pytest.raises(sqlite3.IntegrityError) as exc:
		store.begin_turn("查无此会话", "x")
	assert "FOREIGN KEY" in str(exc.value), exc.value

	assert raw(db, "SELECT COUNT(*) FROM turns") == [(0,)]
	# 回滚没做干净的话,连接上还挂着一个写事务,这一句会报
	# "cannot start a transaction within a transaction"。
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	assert store.begin_turn(sid, "回滚之后还能开轮")["turn_no"] == 1


def test_读路径_轮次_事件游标_热路径不抛(store):
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	store.begin_turn(sid, "问题")
	tid = store.list_turns(sid)["turns"][0]["id"]

	assert store.append_event(sid, {"kind": "you", "text": "嗨"}) == 1
	assert store.append_event(sid, {"kind": "note", "text": "笔记"}) == 2

	noisy = io.StringIO()
	with contextlib.redirect_stdout(noisy):
		assert store.append_event("查无此会话", {"kind": "note"}) is None
		store.append_turn_message("查无此轮", 2, "assistant_response",
		                          "assistant", "x")
	assert noisy.getvalue().count("没落库") == 2, noisy.getvalue()

	store.append_turn_message(tid, 2, "assistant_response", "assistant",
	                          [{"type": "text", "text": "回答"}])

	page = store.list_turns(sid)
	assert page["cursor"] == 2, page["cursor"]
	assert [t["turn_no"] for t in page["turns"]] == [1]
	turn = page["turns"][0]
	assert (turn["status"], turn["finished_at"], turn["error_message"]) \
		== ("running", None, None), turn
	assert [(m["message_no"], m["kind"], m["role"], m["content"])
	        for m in turn["messages"]] == [
		(1, "user_input", "user", "问题"),
		(2, "assistant_response", "assistant",
		 [{"type": "text", "text": "回答"}])]

	assert store.events_since(sid, 0) == [
		(1, {"kind": "you", "text": "嗨"}),
		(2, {"kind": "note", "text": "笔记"})]
	assert store.events_since(sid, 1) == [(2, {"kind": "note", "text": "笔记"})]
	assert store.events_since(sid, 99) == []


def test_中途检查点与轮末收尾(db, store):
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")

	store.save_context(sid, [{"role": "user", "content": "压过的历史"}],
	                   compacted=True)
	assert store.load_context(sid) == [{"role": "user", "content": "压过的历史"}]
	checkpoint = raw(db, "SELECT version, last_compacted_at"
	                     " FROM session_contexts")[0]
	assert checkpoint[0] == 2 and checkpoint[1] is not None, checkpoint

	store.finish_turn(sid, turn["id"], "completed", None,
	                  [{"role": "user", "content": "最终历史"}])
	assert store.load_context(sid) == [{"role": "user", "content": "最终历史"}]
	assert raw(db, "SELECT status, finished_at, error_message FROM turns")[0][:1] \
		== ("completed",)
	assert raw(db, "SELECT finished_at FROM turns")[0][0] is not None
	assert raw(db, "SELECT error_message FROM turns") == [(None,)]

	final = raw(db, "SELECT version, last_compacted_at FROM session_contexts")[0]
	assert final[0] == 3, final
	# 轮末那次不带 compacted,不能把"压过"这个时间抹掉
	assert final[1] == checkpoint[1], (final, checkpoint)

	# 重复收尾:条件更新打 0 行 —— 那是"这一轮在库里已经是终态"。它现在
	# **抛**,不再只是打一行日志:不抛的话整个事务会照常提交,而那一半是按
	# "它还在跑"算出来的上下文,提交上去就是拿旧账盖掉终态任务的工作上下文。
	with pytest.raises(sessions.TurnStateConflict):
		store.finish_turn(sid, turn["id"], "failed", "晚了", [])
	# 终态一个字没动(不是被打成 failed)
	assert raw(db, "SELECT status, error_message FROM turns") == [("completed", None)]
	# 上下文也一个字没动:还是那份最终历史,连版本号和"压过"的时间都留着 ——
	# 事务回滚了,不是"写了一半"
	assert store.load_context(sid) == [{"role": "user", "content": "最终历史"}]
	again = raw(db, "SELECT version, last_compacted_at FROM session_contexts")[0]
	assert (again[0], again[1]) == (final[0], final[1]), (again, final)


def test_失败收尾_错误原因落库(db, store):
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	store.finish_turn(sid, turn["id"], "failed", "模型超时", [{"role": "user"}])
	assert raw(db, "SELECT status, error_message FROM turns") == [("failed", "模型超时")]
	assert store.load_context(sid) == [{"role": "user"}]


def test_重启收尾_只动还在跑的轮_不碰上下文(db, store):
	"""进程被杀之后,启动时那一下:running 的行收成失败,已完成的原样不动。

	这条盯的是三个具体的错法:漏写 finished_at(表上那个 CHECK 会抛)、
	WHERE 写宽了把已完成的轮一起吃掉,以及"顺手"连 session_contexts 也改一笔
	—— 那份是开轮之前的历史,下一轮正该从那儿接着跑,改了等于悄悄丢一轮。
	"""
	done = store.create_session("项目记忆", "用户记忆")["id"]
	dead = store.create_session("项目记忆", "用户记忆")["id"]
	finished = store.begin_turn(done, "跑完的")
	store.finish_turn(done, finished["id"], "completed", None,
	                  [{"role": "user", "content": "跑完的历史"}])
	store.begin_turn(dead, "被打断的")
	store.save_context(dead, [{"role": "user", "content": "开轮时的历史"}])

	done_before = raw(db, "SELECT status, updated_at, finished_at, error_message"
	                      " FROM turns WHERE session_id = ?", (done,))
	ctx_before = raw(db, "SELECT messages_json, version, updated_at"
	                     " FROM session_contexts WHERE session_id = ?", (dead,))

	assert store.reap_running() == 1
	assert store.reap_running() == 0          # 幂等:第二遍没有 running 可收了

	# 已完成那条整行没被动过(updated_at 也没动)
	assert raw(db, "SELECT status, updated_at, finished_at, error_message"
	               " FROM turns WHERE session_id = ?", (done,)) == done_before
	dead_row = raw(db, "SELECT status, finished_at, error_message FROM turns"
	                   " WHERE session_id = ?", (dead,))[0]
	assert dead_row[0] == "failed", dead_row
	# 非 running 就必须有 finished_at,这是表上 CHECK 要的
	assert dead_row[1] is not None, dead_row
	assert dead_row[2] == "进程重启,这一轮没有跑完", dead_row

	# 上下文一个字都不许动
	assert raw(db, "SELECT messages_json, version, updated_at"
	               " FROM session_contexts WHERE session_id = ?",
	           (dead,)) == ctx_before
	assert store.load_context(dead) == [{"role": "user", "content": "开轮时的历史"}]

	# 页面看的就是这个:list_turns 里那一轮得报 failed 带原因
	one = store.list_turns(dead)["turns"]
	assert len(one) == 1, one
	assert (one[0]["status"], one[0]["error_message"]) \
		== ("failed", "进程重启,这一轮没有跑完"), one[0]
	assert one[0]["finished_at"] is not None, one[0]


def test_删会话_级联清干净_不碰邻居(db, store):
	keep = store.create_session("项目记忆", "用户记忆")["id"]
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")
	store.append_turn_message(turn["id"], 2, "assistant_response", "assistant", "回答")
	store.append_event(sid, {"kind": "note"})
	store.save_context(sid, [{"role": "user"}])
	store.append_event(keep, {"kind": "note"})

	store.delete_session(sid)
	for table in ("turns", "session_contexts", "events"):
		assert raw(db, f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)) \
			== [(0,)], table
	# turn_messages 只引用 turns,级联是**间接**的,所以这里查全表
	assert raw(db, "SELECT COUNT(*) FROM turn_messages") == [(0,)]
	assert raw(db, "SELECT COUNT(*) FROM events WHERE session_id = ?", (keep,)) == [(1,)]


def test_并发开轮_turn_no_不重号(db, store):
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	got, errs = [], []

	def worker(i):
		try:
			got.append(store.begin_turn(sid, f"q{i}")["turn_no"])
		except Exception as e:  # noqa: BLE001
			errs.append(f"{type(e).__name__}: {e}")

	threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()
	assert not errs, errs
	assert sorted(got) == list(range(1, 9)), sorted(got)
	assert raw(db, "SELECT COUNT(*) FROM turns") == [(8,)]


def test_空库一次建到当前版本_就五张表(db, store):
	# 跟着代码走,不写死版本号 —— 写死了,每加一条迁移都得回来改一次
	assert raw(db, "PRAGMA user_version") == [(sessions.SCHEMA_VERSION,)]
	# sqlite_sequence 是 events 那个自增主键自带的内部表
	assert tables(db) == ["events", "session_contexts", "sessions",
	                      "sqlite_sequence", "turn_messages", "turns"]
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	store.begin_turn(sid, "问题")
	assert len(store.list_turns(sid)["turns"]) == 1


def test_库比代码新就拒绝启动(db):
	conn = sqlite3.connect(db)
	conn.execute("PRAGMA user_version = 99")
	conn.commit()
	conn.close()
	with pytest.raises(RuntimeError) as exc:
		sessions.SessionStore(db)
	assert "v99" in str(exc.value), exc.value
