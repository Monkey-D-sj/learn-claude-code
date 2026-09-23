"""会话库的特征化测试:行为变了就得响。

断言的是**外部可观察**的东西:落库的行、返回值、异常,以及"失败之后连接
还干不干净"。最后一条是要害:事务样板收成一个 helper 之后,回滚路径最容易
悄悄坏掉 —— 而它坏掉时不会报错,只会让下一个请求莫名其妙地失败。

跑法: uv run pytest
"""

import contextlib
import io
import json
import sqlite3
import threading
import time

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


def test_落一条消息_返回它在库里的行号(db, store):
	"""返回值就是那条消息的行号 —— 上层拿它当号发给模型。

	**号 = 行号**,于是"有号"等于"真的存进去了",而且水位不用自己数:
	id 是 SQLite 的 INTEGER PRIMARY KEY,只增不减。上层那套"接着最大号发、
	被压掉的号也算数"的补丁就是为了在没有它的时候模拟这件事。
	"""
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	turn = store.begin_turn(sid, "问题")

	first = store.append_turn_message(turn["id"], 2, "assistant_response",
	                                  "assistant", "回答")
	second = store.append_turn_message(turn["id"], 3, "tool_result", "user",
	                                   [{"type": "tool_result", "content": "输出"}])

	assert first == raw(db, "SELECT id FROM turn_messages WHERE message_no = 2")[0][0]
	assert second == raw(db, "SELECT id FROM turn_messages WHERE message_no = 3")[0][0]
	assert second > first


def test_没落进去的消息_返回None(store):
	"""写不进去就没有行号。

	**这一条是有用的**,不是兜底:上层拿 None 就不发号,模型看不到号也就
	点不动这段 —— 比发一个查不回来的号强。"有号 = 查得回来"这条不变量
	就靠它撑着。
	"""
	noisy = io.StringIO()
	with contextlib.redirect_stdout(noisy):
		assert store.append_turn_message("查无此轮", 2, "tool_result", "user",
		                                 "x") is None
	assert "没落库" in noisy.getvalue(), noisy.getvalue()


def test_按号取回_只认本会话(db, store):
	"""按行号捞回那条消息的正文 —— 模型点一个号,要拿回被压掉的原文。

	**必须带会话过滤。** 行号是 `turn_messages` 这一张表上的,不带 session_id
	的话,A 会话拿着自己上下文里的一个号,能读到 B 会话的原文 —— 而"两个会话
	互相看不见对方"是这张表唯一的边界。查不到就返回 None,不抛。
	"""
	sid = store.create_session("项目记忆", "用户记忆")["id"]
	other = store.create_session("项目记忆", "用户记忆")["id"]
	mine_turn = store.begin_turn(sid, "问题")
	other_turn = store.begin_turn(other, "别的会话")
	body = [{"type": "tool_result", "content": "本会话的输出"}]
	other_body = [{"type": "tool_result", "content": "别的会话的输出"}]
	store.append_turn_message(mine_turn["id"], 2, "tool_result", "user", body)
	store.append_turn_message(other_turn["id"], 2, "tool_result", "user", other_body)

	ids = [row[0] for row in raw(db, "SELECT id FROM turn_messages"
	                                 " WHERE kind = 'tool_result' ORDER BY id")]
	mine, theirs = ids

	assert store.find_message(sid, mine) == body
	assert store.find_message(other, theirs) == other_body
	assert store.find_message(other, mine) is None, "别人的号在本会话里必须查不到"
	assert store.find_message(sid, 99999) is None


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
	dead_row = raw(db, "SELECT status, finished_at, error_message,"
	                   " interrupt_reason FROM turns"
	                   " WHERE session_id = ?", (dead,))[0]
	# v4 起收成 interrupted:它不是"跑错了",是"进程没了、而这份状态还能接着用"。
	# 页面在这一格画的是"继续 / 放弃"两个按钮,而 failed 那格没有。
	assert dead_row[0] == "interrupted", dead_row
	assert dead_row[3] == "process_restart", dead_row
	# 非 running 就必须有 finished_at,这是表上 CHECK 要的
	assert dead_row[1] is not None, dead_row
	assert dead_row[2] == "进程重启,这一轮没有跑完", dead_row

	# 上下文一个字都不许动
	assert raw(db, "SELECT messages_json, version, updated_at"
	               " FROM session_contexts WHERE session_id = ?",
	           (dead,)) == ctx_before
	assert store.load_context(dead) == [{"role": "user", "content": "开轮时的历史"}]

	# 页面看的就是这个:list_turns 里那一轮得报 interrupted,而且两样都带上 ——
	# 机器读的原因决定画哪些按钮,那句人话决定显示什么。
	one = store.list_turns(dead)["turns"]
	assert len(one) == 1, one
	assert (one[0]["status"], one[0]["error_message"]) \
		== ("interrupted", "进程重启,这一轮没有跑完"), one[0]
	assert one[0]["interrupt_reason"] == "process_restart", one[0]
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


def test_空库一次建到当前版本_表齐了(db, store):
	# 跟着代码走,不写死版本号 —— 写死了,每加一条迁移都得回来改一次
	assert raw(db, "PRAGMA user_version") == [(sessions.SCHEMA_VERSION,)]
	# sqlite_sequence 是 events 那个自增主键自带的内部表
	assert tables(db) == ["events", "session_contexts", "sessions",
	                      "sqlite_sequence", "tool_execs", "turn_messages", "turns"]
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


# ---------------------------------------------------------------- v4 迁移

def _build_v3(db):
	"""造一个真正的 v3 老库:用当时那三条迁移的正文建表,再塞数据。

	**不借新版代码的任何路径** —— 省事的写法(把 SCHEMA_VERSION 改小再建一个
	SessionStore)其实建出来的是"新版代码眼里的老库",而那正是被测的东西:
	迁移要能处理**别人(旧版代码)写出来的库**。
	"""
	conn = sqlite3.connect(db)
	conn.isolation_level = None
	for steps in sessions.MIGRATIONS[:3]:
		for step in steps:
			conn.execute(step)
	now = time.time()
	conn.execute("BEGIN")
	conn.execute("PRAGMA user_version = 3")
	sid = "老会话"
	tid = "老轮次"
	conn.execute("INSERT INTO sessions (id,title,created_at,updated_at,"
	             " memory_snapshot,user_snapshot) VALUES (?,?,?,?,?,?)",
	             (sid, "老标题", now, now, "项目记忆", "用户记忆"))
	conn.execute("INSERT INTO turns (id,session_id,turn_no,status,created_at,"
	             " updated_at,finished_at,error_message)"
	             " VALUES (?,?,?,'running',?,?,NULL,NULL)", (tid, sid, 1, now, now))
	for no, kind, role, body in (
			(1, "user_input", "user", "老问题"),
			(2, "assistant_response", "assistant", "老回答")):
		conn.execute("INSERT INTO turn_messages (turn_id,message_no,kind,role,"
		             " content_json,created_at) VALUES (?,?,?,?,?,?)",
		             (tid, no, kind, role, json.dumps(body, ensure_ascii=False), now))
	conn.execute("INSERT INTO session_contexts (session_id,messages_json,version,"
	             " updated_at,last_compacted_at) VALUES (?,?,?,?,NULL)",
	             (sid, json.dumps([{"role": "user", "content": "老历史"}],
	                              ensure_ascii=False), 7, now))
	conn.execute("COMMIT")
	conn.close()
	return sid, tid


def test_从v3迁到v4_数据一条不少(db, monkeypatch):
	"""turns 重建是这条迁移里唯一危险的动作:开着外键 DROP TABLE 会顺着
	CASCADE 把原始消息删光,而且一句错都不报。所以这里查的是"消息还在不在"。
	"""
	sid, tid = _build_v3(db)
	store = sessions.SessionStore(db)

	assert raw(db, "PRAGMA user_version") == [(sessions.SCHEMA_VERSION,)]
	# turn_messages 一条不少 —— 这是"DROP 没把 CASCADE 带上"的证据
	assert raw(db, "SELECT message_no, kind FROM turn_messages ORDER BY message_no") \
		== [(1, "user_input"), (2, "assistant_response")]
	assert raw(db, "SELECT title, memory_snapshot, user_snapshot FROM sessions") \
		== [("老标题", "项目记忆", "用户记忆")]
	# 新列取默认,不伪造历史:没中断过的轮次不该有一个原因
	assert raw(db, "SELECT turn_no, status, interrupt_reason, model_rounds_started"
	               " FROM turns") == [(1, "running", None, 0)]
	# 老快照没有归属和水位:它是"不能当恢复基础"的那一类,而不是"水位是 0"
	assert raw(db, "SELECT messages_json, version, checkpoint_turn_id,"
	               " covered_message_no FROM session_contexts") \
		== [(json.dumps([{"role": "user", "content": "老历史"}],
		                ensure_ascii=False), 7, None, 0)]
	# 外键和索引都还在
	assert raw(db, "PRAGMA foreign_key_check") == []
	assert tables(db) == ["events", "session_contexts", "sessions",
	                      "sqlite_sequence", "tool_execs", "turn_messages", "turns"]
	assert "tool_execs_turn" in [r[0] for r in raw(
		db, "SELECT name FROM sqlite_master WHERE type='index'")]
	# 老库照常能聊:读上下文、开新轮
	assert store.load_context(sid) == [{"role": "user", "content": "老历史"}]
	assert store.begin_turn(sid, "新问题")["turn_no"] == 2


def test_迁移失败_老库原样不动(db, monkeypatch):
	"""迁移要么整条上去,要么一个字不写。

	中途挂掉而只迁了一半的库是最难查的一种:表在、列在、数据缺,而且
	user_version 已经当成新版本了。
	"""
	sid, tid = _build_v3(db)
	real = sessions.MIGRATIONS[3]
	monkeypatch.setattr(sessions, "MIGRATIONS",
	                    (*sessions.MIGRATIONS[:3], (*real, "DROP TABLE 没有这张表")))
	with pytest.raises(sqlite3.OperationalError):
		sessions.SessionStore(db)

	assert raw(db, "PRAGMA user_version") == [(3,)]
	assert "tool_execs" not in tables(db)
	assert raw(db, "SELECT COUNT(*) FROM turn_messages") == [(2,)]
	assert raw(db, "SELECT COUNT(*) FROM turns") == [(1,)]
	# 表还是老样子:新列一个字都没加进去(查询会抛"没有这一列")
	with pytest.raises(sqlite3.OperationalError):
		raw(db, "SELECT interrupt_reason FROM turns")
	# 而老库还能照常打开(用旧代码的路径读它)
	assert raw(db, "SELECT status FROM turns") == [("running",)]
