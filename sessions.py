"""会话库:SQLite 里三张表。

本机自用,一个进程一个文件。这个模块只干"存取",不懂 agent —— 谁在
什么时候调它,是 server.py 的事。

三条贯穿全文件的规矩,每条都有理由:

**一、单连接 + 一把锁,不是每个线程一个连接。**
	sqlite3 连接默认 check_same_thread=True,而服务端是每请求一个线程
	(ThreadingHTTPServer)。三条路(per-thread 连接 / 单连接加锁 / 专用
	写线程)在这里性能都是噪声,决定因素是别的:

	per-thread 连接的问题在 PRAGMA 的作用域。journal_mode 写进文件、设一次
	就够,但 busy_timeout 和 foreign_keys 是**每连接**的 —— 每线程一个连接
	意味着"记得在每个连接上都设一遍",而漏掉一次不报错,只是行为慢慢不
	一样。收在一个 _setup() 里就没有这条岔路。
	专用写线程则是把"已经发给页面了但还没进库"变成一个真实状态,读回来
	会缺一段;进程被杀时队列里那截正好丢掉,丢的恰好是要保的东西。

	代价是并发的正确性从"靠 SQLite 内部"变成"靠我们这把锁" —— 那把锁就
	摆在文件里,看得到。它**只圈单条语句(毫秒级)**,序列化一律在锁外做完;
	它绝不圈住 agent 循环,那是每会话一把锁的事,见 server.py。

**二、messages 是镜像,events 是日志。**
	messages 每轮**整体重写**。压缩器(context.py)随时可能把整个列表换成
	别的形状 —— 中段归档、整体换摘要 —— 所以"往尾巴追加"这个模型在这里
	根本不成立。真去按轮开始时的下标切片追加的话,压缩之后那个下标就失效
	了,而且失效方式是静默的:切片可能切成空的,整轮凭空消失,不报错。
	整体重写的代价有界:压缩器把消息压在 SNIP_MAX_MESSAGES 条以内。

	events 只追加、永不修改。它是页面重放的来源,重放出来必须跟当时屏幕上
	发生过的一致 —— 所以库里存的就是发出去的那一份(包括工具输出那 4000
	字符的截断,见 agent.py 的 clip_for_event)。库里存一份比页面更完整的
	版本,只会让重放跟你记忆里那次对话对不上。

**三、热路径上的方法从不起异常。**
	append_event / touch / append_message 的调用方是 emit 包装,而 emit 是
	agent 循环调的 —— 那里没有 try,一条事件写不进去不该掀翻一整轮。
	写不进去就返回 None,页面那边只是少一个用来去重的 seq。

	读历史和轮末重写相反:那两个**要抛**。拿一份错误的上下文去跑一轮,
	比停下来糟得多 —— 前者会拿着空历史去改用户的仓库。分档的理由跟
	server.py 的 emit_quietly 是同一个:坏在哪一层,就在那一层处置。
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

# 库跟 server.py 同级,不跟 WORKDIR(cwd)。
#
# 会话是**这个服务**的东西,不是"当前目录"的东西。而 WORKDIR 是
# Path.cwd() —— 换个目录起服务就会在那边凭空多出一个空库,聊天记录
# "不见了",而且不报错。PAGE(server.py)也是这么定的。
#
# 附带好处:它在 WORKDIR 外面,read_file/write_file/edit_file 够不着它。
# bash 仍然删得掉(permission_hook 也拦不住,不该指望它拦),这个接受。
DB_PATH = Path(__file__).resolve().parent / "sessions.db"

SCHEMA_VERSION = 1

# 每条一个语句,不写成一个大字符串走 executescript。
# 理由:executescript 在遇到已挂起的事务时会先隐式 COMMIT —— 那会把
# 迁移外面那个 BEGIN IMMEDIATE 拆掉,原子性就没了。逐条 execute 不会。
_MIGRATION_1 = (
	"""
	CREATE TABLE sessions (
		id         TEXT PRIMARY KEY,
		title      TEXT NOT NULL DEFAULT '',
		created_at REAL NOT NULL,
		updated_at REAL NOT NULL
	)
	""",
	# 列会话的排序键。会话数到不了需要索引的量级,但 ORDER BY 的意图写在
	# 索引里,将来要加 LIMIT 翻页不用回头找。
	"CREATE INDEX sessions_updated ON sessions(updated_at DESC)",

	"""
	CREATE TABLE messages (
		id         INTEGER PRIMARY KEY,
		session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
		message    TEXT NOT NULL,
		created_at REAL NOT NULL
	)
	""",
	"CREATE INDEX messages_session ON messages(session_id, id)",

	# events.id 是发给页面的游标(?since=),所以它必须是 AUTOINCREMENT:
	# 普通 rowid 在删掉最大行之后会被复用,而复用一次就等于让客户端跳过
	# 或重放一段。messages.id 从不外传,普通 rowid 就够。
	"""
	CREATE TABLE events (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
		event      TEXT NOT NULL,
		created_at REAL NOT NULL
	)
	""",
	"CREATE INDEX events_session ON events(session_id, id)",
)

# 下标 = 目标版本 - 1。加一次改动就往后接一个,并把 SCHEMA_VERSION 加一。
MIGRATIONS = (_MIGRATION_1,)


def _block_json(obj):
	"""json.dumps 的 default:非内置类型怎么落库。

	参数跟 SDK 自己发请求时用的那份对齐(exclude_unset=True、mode="json"、
	by_alias=True —— 见 anthropic/_utils/_transform.py 里 _transform_recursive
	对 BaseModel 的那一支)。这不是洁癖:

	从库里读回来的 assistant content block 是 dict,再发出去时走的是
	_transform_typeddict,而它用 is_given(None) 判断字段在不在。裸
	model_dump() 会把没设过的可选字段写成 "citations": null,那是 "given",
	会被原样发给 API —— 跟原来那次请求就不是一个形状了。
	exclude_unset 让这些字段根本不进字典。

	不走 model_dump 的(未知对象)退化成 str,跟 context.py 的 _json_default
	一样:只可读、不可还原,但至少落得下。
	"""
	if hasattr(obj, "model_dump"):
		return obj.model_dump(exclude_unset=True, mode="json", by_alias=True)
	return str(obj)


class SessionStore:
	def __init__(self, path: Path = DB_PATH):
		self._lock = threading.Lock()
		# isolation_level=None 是必须的,不是顺手写的。
		#
		# 默认(deferred)模式下,sqlite3 会在第一条 DML 时**隐式开一个事务**
		# 并一直挂着,直到有人 commit()。而这里要的是"每条事件立刻落盘" ——
		# 那个隐式事务会把整轮的写入全圈在里面,此刻 Ctrl+C 就丢光一整轮,
		# 而且不报错。自动提交下这个状态根本不存在;需要原子性的两处
		# (建库、轮末重写)显式 BEGIN IMMEDIATE。
		#
		# timeout=5.0 就是 busy_timeout:单连接只挡得住本进程,你用 sqlite3
		# 命令行翻库、或者手滑起了第二个 server,都会撞上写锁。不加的话
		# 那一下直接是 database is locked 异常。
		self._conn = sqlite3.connect(path, check_same_thread=False,
		                             timeout=5.0, isolation_level=None)
		self._migrate()

	def _migrate(self):
		"""PRAGMA user_version 当版本号,逐级往上迁。

		没用一张 meta 表存版本号:那张表本身要先 CREATE 出来才能读,是个
		先有鸡还是先有蛋。user_version 是 SQLite 给的那个整数,读它不需要
		任何表存在。
		"""
		conn = self._conn
		# journal_mode 写进文件,设一次就够。WAL 让读者不被写者挡 ——
		# 重放一条上千事件的会话时,正在跑的那一轮不用停下来。
		# synchronous 和 foreign_keys 是每连接的,所以必须在 _setup 里设。
		conn.execute("PRAGMA journal_mode = WAL")
		# NORMAL:WAL 下 FULL 是每次 commit 都 fsync,一轮上百条事件就是
		# 上百次 fsync,Windows 上能拖出半秒。代价是"断电可能丢最后几条",
		# 而这里的故障模型是进程崩 / Ctrl+C —— 进程崩不会损坏 WAL。
		conn.execute("PRAGMA synchronous = NORMAL")
		# ON DELETE CASCADE 要靠它才生效。不开的话删会话会留下孤儿行,
		# 而且不报错。
		conn.execute("PRAGMA foreign_keys = ON")

		version = conn.execute("PRAGMA user_version").fetchone()[0]
		if version > SCHEMA_VERSION:
			# 比这份代码新的库直接拒绝启动。带着不认识的 schema 继续跑,
			# 最可能的结局是**不报错地**少写一列。
			raise RuntimeError(
				f"会话库是 v{version},这份代码只认到 v{SCHEMA_VERSION} —— "
				f"换新版代码再打开它")

		for target in range(version + 1, SCHEMA_VERSION + 1):
			conn.execute("BEGIN IMMEDIATE")
			try:
				for statement in MIGRATIONS[target - 1]:
					conn.execute(statement)
				# PRAGMA 不能带参数占位符;target 是我们自己 range 出来的整数。
				conn.execute(f"PRAGMA user_version = {target}")
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise

	# ---- 读路径 / 轮末写路径:会抛,调用方接得住 ----

	def session_exists(self, sid: str) -> bool:
		with self._lock:
			row = self._conn.execute(
				"SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()
		return row is not None

	def create_session(self) -> dict:
		now = time.time()
		sid = uuid.uuid4().hex
		with self._lock:
			self._conn.execute(
				"INSERT INTO sessions (id, title, created_at, updated_at)"
				" VALUES (?, ?, ?, ?)", (sid, "", now, now))
		return {"id": sid, "title": "", "updated_at": now}

	def list_sessions(self) -> list[dict]:
		"""按最近动过的排前面。

		rowid DESC 只用来打破 updated_at 相同时的平局(同一毫秒里建的两个
		会话)。不加的话顺序由 SQLite 自己定,两次请求可能给出不同的顺序,
		侧栏会自己跳。
		"""
		with self._lock:
			rows = self._conn.execute(
				"SELECT id, title, updated_at FROM sessions"
				" ORDER BY updated_at DESC, rowid DESC LIMIT 50").fetchall()
		return [{"id": row[0], "title": row[1], "updated_at": row[2]}
		        for row in rows]

	def load_messages(self, sid: str) -> list:
		"""这一轮的起点。读回来的是纯 dict —— assistant 的 content 也一样,
		它原来是 SDK 的 pydantic 对象,落库时被 _block_json 转成了 dict。
		"""
		with self._lock:
			rows = self._conn.execute(
				"SELECT message FROM messages WHERE session_id = ? ORDER BY id",
				(sid,)).fetchall()
		return [json.loads(row[0]) for row in rows]

	def replace_messages(self, sid: str, messages: list) -> None:
		"""整体重写,不追加。理由见模块开头第二条。"""
		now = time.time()
		# 序列化(可能上百条、几毫秒)在锁外做完,锁里只放语句。
		rows = [(sid, json.dumps(message, ensure_ascii=False, default=_block_json), now)
		        for message in messages]
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE")
			try:
				conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
				conn.executemany(
					"INSERT INTO messages (session_id, message, created_at)"
					" VALUES (?, ?, ?)", rows)
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise

	def events_since(self, sid: str, since: int) -> list[tuple[int, dict]]:
		"""取游标之后的事件,按 id 升序。返回 (id, event),id 就是新游标。"""
		with self._lock:
			rows = self._conn.execute(
				"SELECT id, event FROM events WHERE session_id = ? AND id > ?"
				" ORDER BY id", (sid, since)).fetchall()
		return [(row[0], json.loads(row[1])) for row in rows]

	def delete_session(self, sid: str) -> None:
		"""连带 messages / events 一起删 —— 靠 schema 里的 ON DELETE CASCADE,
		而它要求连接上开着 PRAGMA foreign_keys(在 _migrate 里设了)。"""
		with self._lock:
			self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

	# ---- 热路径:从不起异常 ----

	def touch(self, sid: str, query: str) -> None:
		"""把会话排到列表最前,首轮顺手补上 title。

		一条 UPDATE 干两件事:CASE 由数据库保证原子。写成"先读 title、
		判空、再写"就有竞态(两个请求同时判空),而这里没有值得为它加锁
		的理由。

		title 不调模型:那是一次 API 调用、卡在关键路径上,为的是一个**已经
		摆在眼前**的字符串。存 40 字,侧栏用 CSS 自己截。
		"""
		title = " ".join(query.split())[:40]
		try:
			with self._lock:
				self._conn.execute(
					"UPDATE sessions SET updated_at = ?,"
					" title = CASE WHEN title = '' THEN ? ELSE title END"
					" WHERE id = ?", (time.time(), title, sid))
		except sqlite3.Error as e:
			print(f"[sessions] touch 没写成: {type(e).__name__}: {e}")

	def append_message(self, sid: str, message: dict) -> None:
		"""轮开始时把用户那条补上。

		它到轮末会被 replace_messages 一起重写掉,所以留着它只有一个目的:
		进程在这一轮中途死掉时,至少还看得见用户问的是什么。
		"""
		try:
			text = json.dumps(message, ensure_ascii=False, default=_block_json)
			with self._lock:
				self._conn.execute(
					"INSERT INTO messages (session_id, message, created_at)"
					" VALUES (?, ?, ?)", (sid, text, time.time()))
		except Exception as e:
			print(f"[sessions] 用户消息没落库: {type(e).__name__}: {e}")

	def append_event(self, sid: str, event: dict) -> int | None:
		"""落库一条事件,返回它的游标。写不进去返回 None,**不抛**。

		调用方是 emit 包装,而 emit 被 agent 循环直接调 —— 那里没有 try。
		丢一条事件不该毁掉一整轮:流照旧往下发,只是那条不带 seq,页面
		那边去重失效(可能重复画一条),但不会报错。
		"""
		try:
			text = json.dumps(event, ensure_ascii=False, default=_block_json)
			with self._lock:
				cursor = self._conn.execute(
					"INSERT INTO events (session_id, event, created_at)"
					" VALUES (?, ?, ?)", (sid, text, time.time()))
				return cursor.lastrowid
		except Exception as e:
			print(f"[sessions] 事件没落库: {type(e).__name__}: {e}")
			return None
