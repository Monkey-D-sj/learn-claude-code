"""会话库:SQLite 里几张表。

本机自用,一个进程一个文件。这个模块只干"存取",不懂 agent —— 谁在
什么时候调它,是 server.py 的事。

现在有五个活着的对象:

	sessions          会话本身
	turns             一轮执行,状态机只有 running -> completed / failed
	turn_messages     这一轮的原始消息,只追加、不修改
	session_contexts  交给模型的那份工作上下文,整体重写
	events            页面重放的流水账,只追加

另有一张 messages:它的上下文职责已经交给 session_contexts,现在只是
迁移前的备份,没人读也没人写(见 _MIGRATION_2)。

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
	摆在文件里,看得到。它**只圈事务(毫秒级)**,序列化一律在锁外做完;
	它绝不圈住 agent 循环,那是每会话一把锁的事,见 server.py。

**二、turn_messages 是日志,session_contexts 是镜像。**
	turn_messages 只追加、永不修改:它是"这一轮到底发生过什么"的原始
	记录,一轮跑完再回头改它就等于篡改事实。

	session_contexts 每轮**整体重写**。压缩器(context.py)随时可能把整个
	列表换成别的形状 —— 中段归档、整体换摘要 —— 所以"往尾巴追加"这个
	模型在这里根本不成立。真去按轮开始时的下标切片追加的话,压缩之后那个
	下标就失效了,而且失效方式是静默的:切片可能切成空的,整轮凭空消失,
	不报错。整体重写的代价有界:压缩器把消息压在 SNIP_MAX_MESSAGES 条以内。

	两者是**分开**的,这正是这一版新增的东西:压缩只改镜像,日志一个字
	不动。以前 messages 一张表兼职两件事,压一次原始记录就没了。

	events 同样只追加、永不修改。它是页面重放的来源,重放出来必须跟当时
	屏幕上发生过的一致 —— 所以库里存的就是发出去的那一份(包括工具输出
	那 4000 字符的截断,见 agent.py 的 clip_for_event)。库里存一份比页面
	更完整的版本,只会让重放跟你记忆里那次对话对不上。

**三、方法分两档:读路径和轮末写路径会抛,热路径从不起异常。**
	append_turn_message / append_event 的调用方是 record 和 emit 包装,而
	它是 agent 循环调的 —— 那里没有 try,一条消息写不进去不该掀翻一整轮。
	写不进去就返回 None 或打一行日志,页面上少一条而已。

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

SCHEMA_VERSION = 2

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


def _backfill_contexts(conn) -> None:
	"""把旧 messages 快照搬进 session_contexts。

	旧 messages 存的本来就是"这一段对话的上下文"—— 每轮整体重写,id 顺序
	就是当时的消息顺序。所以按 id 读出来原样就是一串合法 messages_json,
	不用重建、也不能重建:它可能已经被压过,原文找不回来了。

	version 固定 1:没有历史版本可继承。last_compacted_at 留空 —— 旧快照
	压没压过无从判断,编不出一个时间。留空的意思是"不知道",不是"没压过",
	将来别拿它当"这个会话没压过"用。
	"""
	grouped: dict[str, list] = {}
	latest: dict[str, float] = {}
	for session_id, message, created_at in conn.execute(
			"SELECT session_id, message, created_at FROM messages"
			" ORDER BY session_id, id"):
		grouped.setdefault(session_id, []).append(json.loads(message))
		latest[session_id] = max(latest.get(session_id, 0.0), created_at)

	# 遍历 sessions 而不是上面那个分组:一条消息都没有的会话**也要**有一行。
	# 少了的话 load_context 读不到行,会当成"这个会话没有上下文"—— 而
	# "空上下文"和"迁移漏了"在那边长得一模一样。
	for session_id, updated_at in conn.execute("SELECT id, updated_at FROM sessions"):
		conn.execute(
			"INSERT INTO session_contexts (session_id, messages_json, version,"
			" updated_at, last_compacted_at) VALUES (?, ?, 1, ?, NULL)",
			(session_id, json.dumps(grouped.get(session_id, []), ensure_ascii=False),
			 latest.get(session_id, updated_at)))


_MIGRATION_2 = (
	# 一轮执行。状态机这一版只有三条边,CHECK 里就写这三种。
	#
	# created_at 兼作开始时间:创建即执行,没有排队阶段。以后有了 queued
	# 再加 started_at 区分"收到"和"开始",那时这两个时间才不是一回事。
	#
	# turn_no 由本模块在一个事务里发号(见 begin_turn),UNIQUE 是兜底 ——
	# 发号逻辑将来被改坏,至少不会静默地出现两个第 3 轮。
	"""
	CREATE TABLE turns (
		id            TEXT PRIMARY KEY,
		session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
		turn_no       INTEGER NOT NULL CHECK (turn_no > 0),
		status        TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
		created_at    REAL NOT NULL,
		updated_at    REAL NOT NULL,
		finished_at   REAL,
		error_message TEXT,
		UNIQUE (session_id, turn_no),
		CHECK ((status =  'running' AND finished_at IS NULL)
		    OR (status <> 'running' AND finished_at IS NOT NULL))
	)
	""",

	# 这一轮的原始消息。kind 是业务语义,role 是模型协议语义 —— Anthropic
	# 协议里工具结果的 role 也是 user,所以**不能**只看 role 判断那条是不是
	# 用户说的话。tool_use_id 留在 content_json 的内容块里,这一版不另建
	# 工具执行表。
	#
	# content_json 里放的是该条消息在协议里的 content 原样:用户输入和
	# control 是字符串,其余是内容块数组。
	"""
	CREATE TABLE turn_messages (
		id           INTEGER PRIMARY KEY,
		turn_id      TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
		message_no   INTEGER NOT NULL CHECK (message_no > 0),
		kind         TEXT NOT NULL CHECK (kind IN (
			'user_input', 'assistant_response', 'tool_result', 'control')),
		role         TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
		content_json TEXT NOT NULL,
		created_at   REAL NOT NULL,
		UNIQUE (turn_id, message_no)
	)
	""",

	# 一个会话一份,不是每次压缩一条历史。它保存的是"当前有效的模型上下文",
	# 包括还没压过的那种 —— 想知道压没压过看 last_compacted_at,不是数行数。
	#
	# version 是快照版本(每存一次加一),不是压缩次数。
	"""
	CREATE TABLE session_contexts (
		session_id        TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
		messages_json     TEXT NOT NULL,
		version           INTEGER NOT NULL CHECK (version > 0),
		updated_at        REAL NOT NULL,
		last_compacted_at REAL
	)
	""",

	_backfill_contexts,
)

# 下标 = 目标版本 - 1。加一次改动就往后接一个,并把 SCHEMA_VERSION 加一。
# 每一项里既可以是 SQL 字符串,也可以是拿 conn 的函数(要搬数据的那种)。
MIGRATIONS = (_MIGRATION_1, _MIGRATION_2)


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
		# 而且不报错。自动提交下这个状态根本不存在;需要原子性的几处显式
		# BEGIN IMMEDIATE。
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
				for step in MIGRATIONS[target - 1]:
					# 迁移里除了 DDL 还有要搬数据的,那种写成一个拿 conn 的
					# 函数。仍然逐条执行、仍然不用 executescript,理由见上面。
					if callable(step):
						step(conn)
					else:
						conn.execute(step)
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
		"""建会话,连它的空上下文一起。

		两件事一个事务:只有会话行、没有上下文行的话,load_context 读到
		空,而"新会话还没聊过"和"上下文那一行丢了"就分不出来 —— 后者会
		让旧历史静默消失,所以宁可在建的时候就把它钉死。
		"""
		now = time.time()
		sid = uuid.uuid4().hex
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE")
			try:
				conn.execute(
					"INSERT INTO sessions (id, title, created_at, updated_at)"
					" VALUES (?, ?, ?, ?)", (sid, "", now, now))
				conn.execute(
					"INSERT INTO session_contexts (session_id, messages_json,"
					" version, updated_at, last_compacted_at)"
					" VALUES (?, '[]', 1, ?, NULL)", (sid, now))
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise
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

	def load_context(self, sid: str) -> list:
		"""这一轮的起点:交给模型的那份工作上下文。

		读回来的是纯 dict —— assistant 的 content 也一样,它原来是 SDK 的
		pydantic 对象,落库时被 _block_json 转成了 dict。
		"""
		with self._lock:
			row = self._conn.execute(
				"SELECT messages_json FROM session_contexts WHERE session_id = ?",
				(sid,)).fetchone()
		return json.loads(row[0]) if row else []

	def list_turns(self, sid: str) -> dict:
		"""这一页要的东西:会话里每一轮,连同它自己的原始消息。

		三条 SELECT 放在一个读事务里,为的是那个 cursor。cursor 是"截到哪条
		事件为止"的分界:页面拿它当起点,只画之后的新事件,而 turn_messages
		里能画的东西恰好覆盖到它 —— 前提是三条读的是**同一个快照**。分开读
		的话,中间落进来的那条消息会既不在 turns 里(读早了)、又因为 seq 太
		小被跳过(读晚了),页面上凭空少一条工具结果。

		另外约定:消息是先落库、再发事件(agent.py 里 record 在 emit 前面)。
		所以 cursor 划到的事件,它对应的消息一定已经在 turn_messages 里了。
		"""
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN")
			try:
				turns = conn.execute(
					"SELECT id, turn_no, status, created_at, updated_at,"
					" finished_at, error_message FROM turns"
					" WHERE session_id = ? ORDER BY turn_no", (sid,)).fetchall()
				messages = conn.execute(
					"SELECT m.turn_id, m.message_no, m.kind, m.role,"
					" m.content_json, m.created_at"
					" FROM turn_messages m JOIN turns t ON t.id = m.turn_id"
					" WHERE t.session_id = ? ORDER BY t.turn_no, m.message_no",
					(sid,)).fetchall()
				cursor = conn.execute(
					"SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ?",
					(sid,)).fetchone()[0]
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise

		by_turn: dict[str, list] = {}
		for turn_id, no, kind, role, content, created_at in messages:
			by_turn.setdefault(turn_id, []).append({
				"message_no": no, "kind": kind, "role": role,
				"content": json.loads(content), "created_at": created_at,
			})

		return {
			"turns": [{
				"id": row[0], "turn_no": row[1], "status": row[2],
				"created_at": row[3], "updated_at": row[4], "finished_at": row[5],
				"error_message": row[6], "messages": by_turn.get(row[0], []),
			} for row in turns],
			"cursor": cursor,
		}

	def events_since(self, sid: str, since: int) -> list[tuple[int, dict]]:
		"""取游标之后的事件,按 id 升序。返回 (id, event),id 就是新游标。"""
		with self._lock:
			rows = self._conn.execute(
				"SELECT id, event FROM events WHERE session_id = ? AND id > ?"
				" ORDER BY id", (sid, since)).fetchall()
		return [(row[0], json.loads(row[1])) for row in rows]

	def delete_session(self, sid: str) -> None:
		"""连带 turns / turn_messages / session_contexts / events 一起删 ——
		靠 schema 里的 ON DELETE CASCADE,而它要求连接上开着 PRAGMA
		foreign_keys(在 _migrate 里设了)。

		这里要小心的是 turn_messages:它只引用 turns,不直接引用 sessions,
		所以删会话能不能删掉它是**间接**的(sessions -> turns -> turn_messages
		两级)。少一级 CASCADE 就会留下一堆孤儿消息,而且不报错。
		"""
		with self._lock:
			self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

	def begin_turn(self, sid: str, query: str) -> dict:
		"""开一轮:发一个 turn_no、建 running 的 Turn、存用户那条 Message、
		把会话排到列表最前。四件事一个短事务。

		发号必须在这个事务里:先查后插分成两个事务的话,两个请求可能拿到
		同一个号,而 UNIQUE 只会让其中一个报错 —— 报错的那一刻用户的东西
		已经丢了一半。BEGIN IMMEDIATE 一上来就拿写锁,查号到插入之间没有
		别人能插进来。

		不另存"最近一条 title 是不是空的"状态:CASE 由数据库保证原子。
		写成"先读 title、判空、再写"就有竞态(两个请求同时判空),而这里
		没有值得为它加锁的理由。

		title 不调模型:那是一次 API 调用、卡在关键路径上,为的是一个**已经
		摆在眼前**的字符串。存 40 字,侧栏用 CSS 自己截。

		这里**会抛**,而且调用点在发响应头之前 —— 写不进库就不该回 200,
		否则页面上那一轮看着开跑了,库里一条记录都没有。
		"""
		now = time.time()
		turn_id = uuid.uuid4().hex
		title = " ".join(query.split())[:40]
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE")
			try:
				turn_no = conn.execute(
					"SELECT COALESCE(MAX(turn_no), 0) + 1 FROM turns"
					" WHERE session_id = ?", (sid,)).fetchone()[0]
				conn.execute(
					"INSERT INTO turns (id, session_id, turn_no, status,"
					" created_at, updated_at, finished_at, error_message)"
					" VALUES (?, ?, ?, 'running', ?, ?, NULL, NULL)",
					(turn_id, sid, turn_no, now, now))
				# message_no=1 是用户那条。后面由 record 回调从 2 接着发。
				conn.execute(
					"INSERT INTO turn_messages (turn_id, message_no, kind, role,"
					" content_json, created_at) VALUES (?, 1, 'user_input',"
					" 'user', ?, ?)",
					(turn_id, json.dumps(query, ensure_ascii=False), now))
				conn.execute(
					"UPDATE sessions SET updated_at = ?,"
					" title = CASE WHEN title = '' THEN ? ELSE title END"
					" WHERE id = ?", (now, title, sid))
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise
		return {"id": turn_id, "turn_no": turn_no, "status": "running"}

	def save_context(self, sid: str, messages: list, compacted: bool = False) -> None:
		"""中途存一个上下文检查点。压缩之后存,是给"这一轮跑到一半进程没了"
		留的:那时下次读到的至少是压过的那份,不是压之前那份发不出去的。

		它**不是**本轮的唯一一次保存 —— 轮末还有一次(finish_turn),那次
		才是权威的。所以调用方在这上面栽了不必掀翻整轮,见 server.py。
		"""
		now = time.time()
		text = json.dumps(messages, ensure_ascii=False, default=_block_json)
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE")
			try:
				self._put_context(conn, sid, text, now, compacted)
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise

	def finish_turn(self, sid: str, turn_id: str, status: str,
	                error_message: str | None, messages: list) -> None:
		"""一轮收尾:最终 Context 和 Turn 终态**同一个事务**。

		分两次写就有一个真实的窗口:本轮已经 completed,而库里那份上下文
		还停在开轮时读到的样子 —— 用户接着问下一轮,模型拿到的历史里少了
		刚跑完的这一整轮,而且不报错。

		error_message 只在失败时有值:正常跑完那条路径传 None,别传空字符串
		—— "没有错误原因"和"错误原因是空"在页面上是两回事。
		"""
		now = time.time()
		text = json.dumps(messages, ensure_ascii=False, default=_block_json)
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE")
			try:
				# 轮末这一次不带 compacted:本轮压过的话,检查点那一次已经
				# 把 last_compacted_at 写上了,这里再写一遍只会把它推后。
				self._put_context(conn, sid, text, now, compacted=False)
				# 条件更新:只有还在 running 的才收尾。这一版只有一个写者
				# (攥着会话锁的那条线程),正常跑不到 0 行;真跑到了说明
				# 有人已经把它写成终态 —— 那声张一声,别静默盖掉。
				changed = conn.execute(
					"UPDATE turns SET status = ?, finished_at = ?, updated_at = ?,"
					" error_message = ? WHERE id = ? AND status = 'running'",
					(status, now, now, error_message, turn_id)).rowcount
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise
		if not changed:
			print(f"[sessions] turn {turn_id} 收尾时已经不是 running,状态没改")

	@staticmethod
	def _put_context(conn, sid: str, text: str, now: float, compacted: bool) -> None:
		"""写工作上下文,version 加一。

		用 upsert 而不是 UPDATE:UPDATE 打空行不报错,而这个文件里最怕的
		就是"静默什么都没发生"。建会话时已经插过一行(version=1),这儿
		正常走 conflict 那一支;真走 insert 那一支说明那一行没了,补上比
		丢掉强。

		last_compacted_at 走 COALESCE:没压过就保留上一次压的时间。直接写
		NULL 的话,轮末这次保存会把"三分钟前压过"这个事实抹掉。
		"""
		conn.execute(
			"INSERT INTO session_contexts (session_id, messages_json, version,"
			" updated_at, last_compacted_at) VALUES (?, ?, 1, ?, ?)"
			" ON CONFLICT(session_id) DO UPDATE SET"
			"   messages_json = excluded.messages_json,"
			"   version = session_contexts.version + 1,"
			"   updated_at = excluded.updated_at,"
			"   last_compacted_at = COALESCE(excluded.last_compacted_at,"
			"                                session_contexts.last_compacted_at)",
			(sid, text, now, now if compacted else None))

	# ---- 热路径:从不起异常 ----

	def append_turn_message(self, turn_id: str, message_no: int, kind: str,
	                        role: str, content) -> None:
		"""记一条原始消息。调用方是 record 回调,而它是 agent 循环调的 ——
		那里没有 try,一条消息写不进去不该掀翻一整轮。

		message_no 由调用方发(它是内存里数的),所以失败会留下一个空号。
		允许空号:UNIQUE 只管不重复,而且这一版明确不重编号 —— 补号意味着
		去改已经落库的邻居,那是另一回事。
		"""
		try:
			text = json.dumps(content, ensure_ascii=False, default=_block_json)
			with self._lock:
				self._conn.execute(
					"INSERT INTO turn_messages (turn_id, message_no, kind, role,"
					" content_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
					(turn_id, message_no, kind, role, text, time.time()))
		except Exception as e:
			print(f"[sessions] 轮次消息没落库: {type(e).__name__}: {e}")

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
