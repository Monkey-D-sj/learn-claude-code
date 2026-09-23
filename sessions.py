"""会话库:SQLite 里几张表。

本机自用,一个进程一个文件。这个模块只干"存取",不懂 agent —— 谁在
什么时候调它,是 server.py 的事。

现在有五个活着的对象:

	sessions          会话本身,外加会话开始那一刻的记忆快照
	turns             一轮执行,状态机只有 running -> completed / failed
	turn_messages     这一轮的原始消息,只追加、不修改
	session_contexts  交给模型的那份工作上下文,整体重写
	events            页面重放的流水账,只追加

三条贯穿全文件的规矩,每条都有理由:

**一、单连接 + 一把锁,不是每个线程一个连接。**
	服务端是每请求一个线程(ThreadingHTTPServer),而 sqlite3 连接默认
	check_same_thread=True。选单连接的理由是 PRAGMA 的作用域:busy_timeout
	和 foreign_keys 是**每连接**的,每线程一个连接就得记得每个都设一遍,
	漏掉一次不报错,只是行为慢慢不一样。专用写线程也不行 —— 它把"已经
	发给页面但还没进库"变成一个真实状态,进程被杀时丢的恰好是要保的东西。

	代价是并发的正确性从"靠 SQLite 内部"变成"靠我们这把锁"。它**只圈事务
	(毫秒级)**,序列化一律在锁外做完;绝不圈住 agent 循环 —— 那是每会话
	一把锁的事,见 server.py。

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
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# 库跟 server.py 同级,不跟 WORKDIR(cwd)。
#
# 会话是**这个服务**的东西,不是"当前目录"的东西。而 WORKDIR 是
# Path.cwd() —— 换个目录起服务就会在那边凭空多出一个空库,聊天记录
# "不见了",而且不报错。PAGE(server.py)也是这么定的。
#
# 附带好处:它在 WORKDIR 外面,read_file/write_file/edit_file 够不着它。
# bash 仍然删得掉(permission_hook 也拦不住,不该指望它拦),这个接受。
#
# **AGENT_DB_PATH 这个口子是为了进程级测试**:启动独占(R2)要验的是"第二个
# 实例拒绝启动、第一个的状态不变,第一个死了第三个能接手并清理",那必须起
# 真进程 —— 而 pytest 换掉 SessionStore 这个**名字**(见 tests/conftest.py)
# 对子进程没用,子进程是自己 import 的。server.py 的 AGENT_PORT 同理。
DB_PATH = Path(os.environ.get("AGENT_DB_PATH")
               or (Path(__file__).resolve().parent / "sessions.db"))

SCHEMA_VERSION = 4

# 每条一个语句,不写成一个大字符串走 executescript。
# 理由:executescript 在遇到已挂起的事务时会先隐式 COMMIT —— 那会把
# 迁移外面那个 BEGIN IMMEDIATE 拆掉,原子性就没了。逐条 execute 不会。
#
# v1 是基线:五张表一次建齐。后面两条只往上加列 —— 那两条分着写,是因为
# v2 已经落在一个跑着的库上了,改它的正文对那个库没用。
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

	# events.id 是发给页面的游标(?since=),所以它必须是 AUTOINCREMENT:
	# 普通 rowid 在删掉最大行之后会被复用,而复用一次就等于让客户端跳过
	# 或重放一段。
	"""
	CREATE TABLE events (
		id         INTEGER PRIMARY KEY AUTOINCREMENT,
		session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
		event      TEXT NOT NULL,
		created_at REAL NOT NULL
	)
	""",
	"CREATE INDEX events_session ON events(session_id, id)",

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
)

# v2 和 v3 是同一次改动的两半,分两条只是因为 v2 先落到了一个已经跑着的库上
# —— 改 v2 的正文对它没用,那条迁移已经被记进那个库的 user_version 里了。
#
# 两列名字不对称(memory_snapshot / user_snapshot)也是这个原因:改列名在
# SQLite 里要重建表,不值得,所以在 _put 注释里把"memory_snapshot 就是项目
# 那份"写清楚。
#
# **为什么要存下来,而不是每轮现读记忆文件:** 记忆是拼进 system prompt 的,
# 而 tools + system 是 DeepSeek 自动前缀缓存的锚点,从 byte 0 逐字节比。一个
# 会话内它变一个字,后面整段历史都要按未命中重算 —— 差的不是一点点。冻住
# 之后本会话零重算,代价只是"写进去的记忆下个会话才生效"。
#
# **为什么不塞进 session_contexts:** 那张表每轮整体重写(见文件头第二条),
# 把永不改变的值放进去等于每轮白写一遍;而且它存的是"消息",system 那截
# 不是消息。
_MIGRATION_2 = (
	"ALTER TABLE sessions ADD COLUMN memory_snapshot TEXT NOT NULL DEFAULT ''",
)

# v3:用户级那份记忆的快照,跟 v2 那个项目级的并排。见上面。
_MIGRATION_3 = (
	"ALTER TABLE sessions ADD COLUMN user_snapshot TEXT NOT NULL DEFAULT ''",
)

# v4 是 checkpoint 那一版:恢复要的三样东西 —— 两阶段标记、快照元数据、
# 一个能表达"进程被打断"的 turn 状态。
_MIGRATION_4 = (
	# ---- 工具执行的两阶段标记 ----
	#
	# 一条工具记录的**开始**和**结果**分成两次写,中间夹着 handler 的执行。
	# 这么分是因为副作用没法回滚:崩在 handler 中间时,唯一能救命的证据是
	# "它开始了"这一条。只有结果那一条的话,"开始过、结果未知"和"压根没开始"
	# 在库里长得一模一样,恢复时只能保守地把整批工具转成人工核对。
	#
	# 只给有副作用的工具写(见 tools/base.py 的 side_effect):只读的重发一次
	# 无害,不必多一条写,也不必进核对范围。
	#
	# tool_use_id 当主键是白拿的幂等:sessions 那套"允许有限次数重试同一份
	# 写入"要求重试不产生第二行,而协议里的 tool_use_id 本来就只有一份。
	#
	# message_id 指向 tool_messages 里那条结果(行号),finished_at 为空 =
	# 开始过但结果没落库 —— 恢复判定读的就是这个组合。
	"""
	CREATE TABLE tool_execs (
		tool_use_id TEXT PRIMARY KEY,
		turn_id     TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
		name        TEXT NOT NULL,
		input_json  TEXT NOT NULL,
		started_at  REAL NOT NULL,
		finished_at REAL,
		message_id  INTEGER
	)
	""",
	"CREATE INDEX tool_execs_turn ON tool_execs(turn_id)",

	# ---- 快照的恢复元数据 ----
	#
	# 放在 session_contexts 上,因为这三样都是"这份快照的属性",不是会话的:
	# 哪个 turn 存的、存到这一轮的第几条消息、当时的运行状态。
	#
	# covered_message_no 是**水位**:这份正文已经涵盖到哪个 message_no。
	# 恢复时判"有没有尾部"就是拿它跟 turn_messages 比。它跟全库行号无关,
	# 是这一轮里的序号。
	#
	# checkpoint_turn_id 可以为空(旧数据、或者建会话那一行),空 = 这份快照
	# 不属于任何一轮,不能当恢复基础。
	"ALTER TABLE session_contexts ADD COLUMN checkpoint_turn_id TEXT",
	"ALTER TABLE session_contexts ADD COLUMN covered_message_no INTEGER"
	" NOT NULL DEFAULT 0",
	"ALTER TABLE session_contexts ADD COLUMN runtime_json TEXT NOT NULL DEFAULT ''",

	# ---- turns 重建:多一个状态,多两列 ----
	#
	# 加 interrupted 是**必须**的,不是好看:重启后收尾和"关键记录存不上"
	# 这两种中断,跟模型自己报错、工具报错收成 failed 是两码事 —— 前者可以
	# 续跑,后者不该续。混在一个值里,页面和恢复接口就只能猜。
	#
	# SQLite 改不了 CHECK,只能重建。重建的坑全在 DROP 那一步:开
	# foreign_keys 时 DROP TABLE 会先隐式 DELETE,而 turn_messages 是
	# ON DELETE CASCADE —— 那一下会把**所有原始消息删光**,而且不报错。
	# 所以这条迁移在**关掉外键的另一个连接**里跑,见 _migrate 里的
	# NEEDS_FK_OFF 分支;跑完在同一个事务里 foreign_key_check,不干净就回滚。
	#
	# 表的形状照抄 v1(列序、UNIQUE、CHECK 一个不少),只多 interrupt_reason
	# 和 model_rounds_started。少了 UNIQUE 就等于把发号的兜底拆了。
	"""
	CREATE TABLE turns_rebuild (
		id            TEXT PRIMARY KEY,
		session_id    TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
		turn_no       INTEGER NOT NULL CHECK (turn_no > 0),
		status        TEXT NOT NULL CHECK (status IN (
			'running', 'completed', 'failed', 'interrupted')),
		created_at    REAL NOT NULL,
		updated_at    REAL NOT NULL,
		finished_at   REAL,
		error_message TEXT,
		interrupt_reason TEXT,
		model_rounds_started INTEGER NOT NULL DEFAULT 0,
		UNIQUE (session_id, turn_no),
		CHECK ((status =  'running' AND finished_at IS NULL)
		    OR (status <> 'running' AND finished_at IS NOT NULL))
	)
	""",
	# 旧行的两列取默认:中断原因只有中断过的那一轮才有,已完成的轮次
	# 编一个原因出来就是伪造事实。
	"INSERT INTO turns_rebuild (id, session_id, turn_no, status, created_at,"
	" updated_at, finished_at, error_message, interrupt_reason,"
	" model_rounds_started)"
	" SELECT id, session_id, turn_no, status, created_at, updated_at,"
	" finished_at, error_message, NULL, 0 FROM turns",
	"DROP TABLE turns",
	"ALTER TABLE turns_rebuild RENAME TO turns",
)

# 下标 = 目标版本 - 1。加一次改动就往后接一个,并把 SCHEMA_VERSION 加一。
# 每一项是一串 SQL 字符串,逐条执行。
MIGRATIONS = (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4)

# 必须在**关掉外键的连接**里跑的那几条迁移。目前只有 v4,理由见它上面
# 那段(turns 重建:开着外键 DROP TABLE 会顺着 CASCADE 删光原始消息)。
#
# 写成一张表而不是散在 _migrate 里判版本号:哪条迁移需要特殊的开法,是
# 那条迁移自己的性质,新增一条时加在这儿,而不是回去改 _migrate 的分支。
NEEDS_FK_OFF = (4,)


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


class TurnStateConflict(RuntimeError):
	"""收尾时那一轮已经不是 running 了。

	正常跑不到:一个会话只有攥着会话锁的那条线程在写它的轮次。真跑到只说明
	有人已经把它写成了终态(比如进程重启时的 reap_running 收掉过它),这时候
	**不能**当没看见 —— 见 finish_turn。
	"""


def _tool_use_id_of(content_json: str) -> str | None:
	"""从一条 tool_result 的正文里取出 tool_use_id。取不到给 None。

	给 checkpoints 的"尾部里有什么"用:页面要能说清尾巴上那条结果是对
	哪一次调用的。只认协议形状(数组 + 第一块 tool_use_id),别的形状一律
	当取不到 —— 这里不是解析器,猜错比说不知道更糟。
	"""
	try:
		blocks = json.loads(content_json)
	except ValueError:
		return None
	if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
		value = blocks[0].get("tool_use_id")
		return value if isinstance(value, str) else None
	return None

class PersistError(RuntimeError):
	"""一条**恢复关键**的记录没写进库。

	它跟"页面少画一条"是两回事,所以不能混在返回 None 那条路里(那条是
	热路径:写不进去拉倒,页面上少一条而已)。这里失败的含义是:模型接下来
	要看到的东西缺了一块 —— 再往下跑,模型会基于一份缺了东西的历史做决定,
	或者工具已经动了机器而库里没有痕迹。

	**必须一路传出去,不许被 except Exception 吃掉。** agent_loop 里那个
	包的 try 是给工具 handler 用的(把工具的异常变成一条工具结果),它不能
	把这条也变成一句工具输出然后接着跑 —— 那正是"改错了静默毁历史"的
	最坏版本。所以调用点要放在那个 try 外面。
	"""


class CheckpointConflict(RuntimeError):
	"""这份快照不能作为恢复基础。

	三种来源:版本对不上(页面看到的是旧的)、快照不属于这个 turn、水位
	之后还有没纳进来的尾部记录。三个都要拒绝,而且要说得出是哪一个 ——
	"恢复失败"这四个字对用户没有用。
	"""


# 中断原因是**机器读**的,所以是短码不是句子;给人看的那句在
# turns.error_message 里。分开存是因为页面要根据它决定画什么按钮,而
# 拿中文句子去 fitz 匹配字符串,迟早会以"改了一次文案,按钮就没了"收场。
#
# 三类分开有实际后果:进程重启(可以续跑)和关键记录存不上(要核对)在
# 恢复判定里走的是两条不同的路,而"模型自己报错"根本不进这个字段 ——
# 那是 failed,不是 interrupted。
INTERRUPT_PROCESS_RESTART = "process_restart"
INTERRUPT_PERSIST_FAILED = "persist_failed"
INTERRUPT_CHECKPOINT_FAILED = "checkpoint_failed"


class SessionStore:
	def __init__(self, path: Path = DB_PATH):
		self._lock = threading.Lock()
		# 路径留着:v4 那条迁移要用**另一个连接**跑(见 NEEDS_FK_OFF),
		# 而那个连接得自己知道库在哪儿。
		self._path = path
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

	@contextmanager
	def _tx(self, immediate: bool = True):
		"""一个事务的进出场:拿锁、BEGIN、COMMIT,出错 ROLLBACK 再抛。

		COMMIT 留在 try 里面是有意的:它自己失败(比如磁盘满)时也该试着
		回滚一次,而不是把半开的事务留在连接上,让下一个 BEGIN 撞上
		"cannot start a transaction within a transaction"。

		immediate=False 给 list_turns:它要的是三条 SELECT 落在同一个快照
		上,而不是一上来就抢写锁。
		"""
		with self._lock:
			conn = self._conn
			conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
			try:
				yield conn
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise

	def _migrate(self):
		"""PRAGMA user_version 当版本号,逐级往上迁。

		没用一张 meta 表存版本号:那张表本身要先 CREATE 出来才能读,是个
		先有鸡还是先有蛋。user_version 是 SQLite 给的那个整数,读它不需要
		任何表存在。
		"""
		conn = self._conn
		# journal_mode 写进文件,设一次就够。WAL 让读者不被写者挡 ——
		# 重放一条上千事件的会话时,正在跑的那一轮不用停下来。
		# synchronous 和 foreign_keys 是每连接的 —— 单连接只要在这儿设一次。
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
			if target in NEEDS_FK_OFF:
				self._migrate_no_fk(target)
				continue
			with self._tx() as conn:
				for step in MIGRATIONS[target - 1]:
					conn.execute(step)
				# PRAGMA 不能带参数占位符;target 是我们自己 range 出来的整数。
				conn.execute(f"PRAGMA user_version = {target}")

	def _migrate_no_fk(self, target: int) -> None:
		"""跑一条必须关掉外键的迁移:另开一个连接,foreign_keys 保持默认的 OFF。

		**为什么不能就地 PRAGMA foreign_keys = OFF:** 它是 no-op —— 整个
		迁移外面套着 BEGIN IMMEDIATE,而 SQLite 明确说事务里改这个开关不生效。
		真跑起来的话,v4 里那句 DROP TABLE turns 会先隐式删一遍行,顺着
		turn_messages 的 ON DELETE CASCADE 把**所有原始消息**删光,而且
		一句错都不报。这是这一条分支存在的全部理由。

		外键关掉之后没人替我们盯着完整性了,所以**在 COMMIT 之前**显式
		foreign_key_check:有问题就抛,让事务回滚 —— 提交之后再发现就只能
		人工修库了。PRAGMA user_version 也在同一个事务里写。

		主连接此刻没有开着的事务(每条迁移各自一个),所以这个连接拿得到
		写锁。timeout 跟主连接一样,撞上别人的写锁时行为也一致。
		"""
		conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
		try:
			conn.execute("BEGIN IMMEDIATE")
			try:
				for step in MIGRATIONS[target - 1]:
					conn.execute(step)
				bad = conn.execute("PRAGMA foreign_key_check").fetchall()
				if bad:
					raise RuntimeError(
						f"迁移 v{target} 之后外键对不上(前几条:{bad[:3]})—— 已回滚")
				conn.execute(f"PRAGMA user_version = {target}")
				conn.execute("COMMIT")
			except BaseException:
				conn.execute("ROLLBACK")
				raise
		finally:
			conn.close()

	# ---- 读路径 / 轮末写路径:会抛,调用方接得住 ----

	def session_exists(self, sid: str) -> bool:
		with self._lock:
			row = self._conn.execute(
				"SELECT 1 FROM sessions WHERE id = ?", (sid,)).fetchone()
		return row is not None

	def create_session(self, memory_snapshot: str, user_snapshot: str) -> dict:
		"""建会话,连它的空上下文和两份记忆快照一起。

		四件事一个事务:只有会话行、没有上下文行的话,load_context 读到
		空,而"新会话还没聊过"和"上下文那一行丢了"就分不出来 —— 后者会
		让旧历史静默消失,所以宁可在建的时候就把它钉死。

		memory_snapshot 是**项目级**那份(列名是 v2 留下的,那时还只有一份),
		user_snapshot 是用户级那份。

		两个 **故意都不给默认值**:默认值等于把"这份记忆是谁读的、什么时候
		读的"这个决定藏起来,而它正是这两个参数存在的理由 —— 本模块只干
		存取,读文件是调用方(server.py)的事,理由见文件头。
		"""
		now = time.time()
		sid = uuid.uuid4().hex
		with self._tx() as conn:
			conn.execute(
				"INSERT INTO sessions (id, title, created_at, updated_at,"
				" memory_snapshot, user_snapshot) VALUES (?, ?, ?, ?, ?, ?)",
				(sid, "", now, now, memory_snapshot, user_snapshot))
			conn.execute(
				"INSERT INTO session_contexts (session_id, messages_json,"
				" version, updated_at, last_compacted_at)"
				" VALUES (?, '[]', 1, ?, NULL)", (sid, now))
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

	def get_memory_snapshots(self, sid: str) -> tuple[str, str]:
		"""这一会话开始时冻下的两份记忆,拼进 system prompt 用。返回
		(项目级, 用户级)。

		会话中途写进记忆文件的东西**不会**出现在这儿 —— 那正是它存在的
		理由,见 _MIGRATION_2 上面那段。要拿到最新的记忆文件,得等下一个
		会话(那时 create_session 会读到新的那两份)。

		跟 load_context 一档,会抛。静默拿一份空记忆去跑一轮,模型会以为
		用户的规矩是另一套 —— 它不会报错,只会照着自己以为的来。
		"""
		with self._lock:
			row = self._conn.execute(
				"SELECT memory_snapshot, user_snapshot FROM sessions WHERE id = ?",
				(sid,)).fetchone()
		return (row[0], row[1]) if row else ("", "")

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
		with self._tx(immediate=False) as conn:
			turns = conn.execute(
				"SELECT id, turn_no, status, created_at, updated_at,"
				" finished_at, error_message, interrupt_reason,"
				" model_rounds_started FROM turns"
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
				"error_message": row[6], "interrupt_reason": row[7],
				"model_rounds_started": row[8],
				"messages": by_turn.get(row[0], []),
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
		with self._tx() as conn:
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
		return {"id": turn_id, "turn_no": turn_no, "status": "running"}

	def save_context(self, sid: str, messages: list, compacted: bool = False) -> None:
		"""中途存一个上下文检查点。压缩之后存,是给"这一轮跑到一半进程没了"
		留的:那时下次读到的至少是压过的那份,不是压之前那份发不出去的。

		**不带归属和水位** —— 那两样是 save_checkpoint 的事,这条老路
		(压缩器回调、测试)就照旧只写正文,已有的元数据原样留着:每次
		压缩都把水位清零的话,一份好端端的快照会被自己变成"没有水位、
		不可恢复"。
		"""
		now = time.time()
		text = json.dumps(messages, ensure_ascii=False, default=_block_json)
		with self._tx() as conn:
			self._put_context(conn, sid, text, now, compacted)

	def save_checkpoint(self, sid: str, turn_id: str, covered_no: int,
	                    messages: list, runtime: dict,
	                    compacted: bool = False) -> int:
		"""存一份完整回合快照:正文 + 归属 + 覆盖水位 + 运行状态,一个事务。

		四个东西必须同一笔提交:只写了正文没写水位,那份快照就说不清自己
		涵盖到哪儿,恢复时只能整份不信;只写了水位没写正文,水位就指向一份
		还没落地的历史。分开写的话中间那个窗口里,库里的状态是**自相矛盾**
		的,而恢复判定恰好就靠这几个字段互相印证。

		返回新的 version —— 页面拿它当"我看到的是第几版",恢复请求要带回来
		比对(见 resume_turn 的 expected_version)。

		不在这儿判"内容变没变":去重是调用方的事(它才知道上一次存的是
		什么时候那份),而这里错一次就是漏存一个水位。
		"""
		now = time.time()
		text = json.dumps(messages, ensure_ascii=False, default=_block_json)
		payload = json.dumps(runtime, ensure_ascii=False)
		with self._tx() as conn:
			self._put_context(conn, sid, text, now, compacted, turn_id=turn_id,
			                  covered=covered_no, runtime=payload)
			return conn.execute(
				"SELECT version FROM session_contexts WHERE session_id = ?",
				(sid,)).fetchone()[0]

	def begin_tool_exec(self, turn_id: str, tool_use_id: str, name: str,
	                    tool_input) -> None:
		"""两阶段标记的前一半:有副作用的工具在动手**之前**先落一条。

		**严格写,写不进去抛 PersistError。** 顺序在这儿就是全部的意义:
		标记落地了才动手,于是"库里有标记"⇒"那一刻它真的开始过了"。
		反过来(先动手再记)等于事后补账,而崩溃恰好发生在补账之前时,库里
		那句"没做过任何事"就是假的 —— 恢复会照着一个假事实自动重跑。

		ON CONFLICT 那支是幂等:调用方允许重试同一份写入(docs 里那条
		"允许有限次数重试同一份数据库写入"),重试不该插出第二行。把
		finished_at 和 message_id 一并清空,是因为同一份写入重试意味着
		这一次尝试还没有结果。
		"""
		text = json.dumps(tool_input, ensure_ascii=False, default=_block_json)
		now = time.time()
		try:
			with self._lock:
				self._conn.execute(
					"INSERT INTO tool_execs (tool_use_id, turn_id, name,"
					" input_json, started_at, finished_at, message_id)"
					" VALUES (?, ?, ?, ?, ?, NULL, NULL)"
					" ON CONFLICT(tool_use_id) DO UPDATE SET"
					"   started_at = excluded.started_at,"
					"   finished_at = NULL, message_id = NULL",
					(tool_use_id, turn_id, name, text, now))
		except Exception as e:
			raise PersistError(
				f"工具 {name} 的执行标记没落库,所以没有执行它:"
				f"{type(e).__name__}: {e}") from e

	def reserve_round(self, turn_id: str, max_rounds: int) -> bool:
		"""发出一次模型请求之前,先把"这一轮用掉了一次"记到库里。

		为什么要落库,而不是接着用内存里的计数:恢复时**不能靠快照**。快照
		是回合边界上存的,而崩溃完全可能发生在"请求已经发出去、快照还没存"
		之间 —— 那时光看快照,那一次调用像没发生过,恢复就等于白送一笔额度。

		所以这个数独立于快照,只增不减,预留了就计上:哪怕请求还没真正发出去
		进程就没了,也算用掉(保守,但省下的是一次重复扣费都算不出的账)。

		返回 False 表示额度已经用完(或者这一轮已经不是 running 了)。
		条件写在 UPDATE 的 WHERE 里而不是"先查后写":查和写之间会挤进另一个
		请求,而两个请求同时看到"还剩 1 次"时,两边都会发出去。
		"""
		with self._tx() as conn:
			changed = conn.execute(
				"UPDATE turns SET model_rounds_started ="
				" model_rounds_started + 1, updated_at = ?"
				" WHERE id = ? AND status = 'running'"
				" AND model_rounds_started < ?",
				(time.time(), turn_id, max_rounds)).rowcount
			return bool(changed)

	def mark_interrupted(self, sid: str, turn_id: str, reason: str,
	                     message: str) -> bool:
		"""把一轮标成 interrupted。**只动 turns,一个字都不写上下文。**

		不碰 session_contexts 是这一条的要点:走到这儿说明那份快照是我们
		能信的最后一份,而手上这份内存里的历史恰恰是**没验证过**的(它可能
		缺了一条没写进库的工具结果)。顺手存下去,等于拿一份没有证据支持
		的历史盖掉最后一个可信点。

		**不抛。** 调用这条路径的原因通常就是库写不进去了,那时该给页面一句
		说得清的话,而不是再掀翻一层 —— 所以返回 False,由调用方决定怎么
		告诉用户(库里会留着 running,下次启动的 reap 会接手)。
		"""
		now = time.time()
		try:
			with self._tx() as conn:
				changed = conn.execute(
					"UPDATE turns SET status = 'interrupted', finished_at = ?,"
					" updated_at = ?, error_message = ?, interrupt_reason = ?"
					" WHERE id = ? AND session_id = ? AND status = 'running'",
					(now, now, message, reason, turn_id, sid)).rowcount
			return bool(changed)
		except Exception as e:
			print(f"[sessions] 中断状态没写进库: {type(e).__name__}: {e}")
			return False

	def unresolved_interrupt(self, sid: str) -> dict | None:
		"""这个会话里有没有还没处理的中断任务(有的话给最新的那一轮)。

		拿它挡新提问:一个会话同时只有一个"最后一个完整回合",而中断的那
		一轮正指着它。这时候开新的一轮,新任务第一次 checkpoint 就会把那份
		唯一能恢复的状态覆盖掉 —— 而用户还没决定是继续还是放弃。

		所以:先处理它。这不算麻烦,因为处理就两个按钮。
		"""
		with self._lock:
			row = self._conn.execute(
				"SELECT id, turn_no, interrupt_reason, error_message FROM turns"
				" WHERE session_id = ? AND status = 'interrupted'"
				" ORDER BY turn_no DESC LIMIT 1", (sid,)).fetchone()
		if row is None:
			return None
		return {"turn_id": row[0], "turn_no": row[1],
		        "interrupt_reason": row[2], "error_message": row[3]}

	# ---- 恢复 ----

	def checkpoint_info(self, sid: str, turn_id: str, max_rounds: int,
	                    signature: str) -> dict:
		"""这一轮能不能续跑,以及不能的话是卡在哪儿。**只读,不改任何东西。**

		页面把它画的"继续"按钮,是拿这个函数的结果决定的 —— 所以每条拒绝
		都要带得走的理由,而不是一个 False。

		判据全部照 docs/checkpoint-implementation.md 第 8.2 节,一条不落。
		其中最能省事、也最不能省的是**尾部规则**:只要这一轮还有 message_no
		大于水位的记录,就不给直接续跑 —— 因为那些记录意味着"水位之后还发生
		过事",而"见过的事"和"做过的事"在库里是两回事(有 tool_use 不等于
		执行过,没结果也不等于没执行)。v1 不猜,交给人工核对。

		signature 由调用方算(模型名、system、工具、工作目录、相关源码指纹的
		合体):恢复一个用**另一套提示词、另一个模型**跑了一半的任务,比不恢复
		更糟 —— 模型会拿着一份不是自己的历史继续做决定。发现不一致就拒绝,
		而不是静默换成新的接着跑。
		"""
		with self._lock:
			turn = self._conn.execute(
				"SELECT status, turn_no, model_rounds_started,"
				" interrupt_reason, error_message FROM turns"
				" WHERE id = ? AND session_id = ?", (turn_id, sid)).fetchone()
			if turn is None:
				return {"resumable": False, "reason": "no_such_turn",
				        "detail": "这一轮不在这个会话里", "tail": []}
			ctx = self._conn.execute(
				"SELECT messages_json, version, updated_at, checkpoint_turn_id,"
				" covered_message_no, runtime_json, last_compacted_at"
				" FROM session_contexts WHERE session_id = ?", (sid,)).fetchone()
			last_no = self._conn.execute(
				"SELECT COALESCE(MAX(turn_no), 0) FROM turns WHERE session_id = ?",
				(sid,)).fetchone()[0]
			tail = self._conn.execute(
				"SELECT m.message_no, m.kind, m.role, m.content_json"
				" FROM turn_messages m WHERE m.turn_id = ? AND m.message_no > ?"
				" ORDER BY m.message_no",
				(turn_id, ctx[4] if ctx else 0)).fetchall()
			unresolved = self._conn.execute(
				"SELECT name, input_json, started_at FROM tool_execs"
				" WHERE turn_id = ? AND finished_at IS NULL"
				" ORDER BY started_at", (turn_id,)).fetchall()

		status, turn_no, rounds_used, reason, error = turn
		info = {
			"turn_id": turn_id, "turn_no": turn_no, "status": status,
			"interrupt_reason": reason, "error_message": error,
			"rounds_used": rounds_used, "max_rounds": max_rounds,
			"checkpoint": None if ctx is None else {
				"version": ctx[1], "updated_at": ctx[2],
				"turn_id": ctx[3], "covered_message_no": ctx[4],
				"last_compacted_at": ctx[6],
			},
			"tail": [{"message_no": r[0], "kind": r[1],
			          "tool_use_id": _tool_use_id_of(r[3])} for r in tail],
			"unknown_tools": [
				{"name": r[0], "input": json.loads(r[1]),
				 "started_at": r[2]} for r in unresolved
			],
		}

		def no(reason_code: str, detail: str) -> dict:
			info.update(resumable=False, reason=reason_code, detail=detail)
			return info

		if status != "interrupted":
			# 包括 running:那条要么是本进程正在跑(锁在别人手里),要么是
			# 上一个进程留下的、还没被 reap 收过 —— 两种都不许从恢复入口进。
			return no("not_interrupted", f"这一轮现在是 {status}")
		if turn_no != last_no:
			return no("has_later_turn", "这一轮之后会话里又开过新的轮次")
		if ctx is None:
			return no("no_snapshot", "这个会话没有快照")
		if ctx[3] != turn_id:
			return no("not_owner",
			          "库里那份快照属于另一次执行,不能当这一轮的恢复基础")
		if not ctx[4]:
			return no("no_watermark", "这份快照没有覆盖水位")
		if tail:
			return no("unresolved_tail",
			          f"水位之后还有 {len(tail)} 条没有纳入快照的记录,"
			          f"其中可能有已经执行过、结果未知的操作")
		if unresolved:
			# 两阶段标记里"开始了、没有结果"的那些。它们可能没有留下任何
			# 原始消息(崩在 handler 里、结果还没写),所以上一条查不到它们 ——
			# 少了这一条,一条已经跑了一半的 bash 会看起来完全没发生过,
			# 而恢复会把它连同别的工具一起重跑。
			return no("unknown_tool_result",
			          f"有 {len(unresolved)} 个操作已经开始、结果未知:" +
			          "、".join(f"{r[0]}({r[1]})" for r in unresolved[:3]))
		if rounds_used >= max_rounds:
			return no("rounds_exhausted",
			          f"原来的回合上限已经用掉({rounds_used}/{max_rounds})")
		try:
			runtime = json.loads(ctx[5]) if ctx[5] else None
		except ValueError:
			runtime = None
		if not isinstance(runtime, dict) or "signature" not in runtime:
			return no("no_runtime", "这份快照没有可用的运行状态")
		if signature and runtime["signature"] != signature:
			return no("incompatible",
			          "模型、提示词、工具或代码已经跟当时不一样了")
		info.update(resumable=True, reason="ok", detail="可以继续",
		            runtime=runtime, version=ctx[1])
		return info

	def resume_turn(self, sid: str, turn_id: str, expected_version: int,
	                    messages: list, control_text: str, runtime: dict) -> dict:
		"""把一个中断的轮次重新变成 running,连带把"我回来了"写进历史。

		五个动作一个事务:版本比对、把控制记录追加进原始消息、更新快照
		(正文 + 水位 + 运行状态)、清掉终态、状态回到 running。

		**为什么控制记录必须在这个事务里**:它自己也是一条原始消息,单独晚一步
		写的话,它会立刻变成"水位之后的尾部" —— 于是刚刚恢复好的任务,下一眼
		看起来又不可恢复了。

		版本比对用 expected_version,不信页面上先前显示的那份:页面看到的
		快照可能已经过期(用户在另一个标签页里放弃了、或者它已经被恢复过一次),
		而这里的每一个判断都要落在**当前**这份数据上。
		"""
		now = time.time()
		block = [{"type": "text", "text": control_text}]
		with self._tx() as conn:
			ctx = conn.execute(
				"SELECT version, checkpoint_turn_id, covered_message_no"
				" FROM session_contexts WHERE session_id = ?", (sid,)).fetchone()
			if ctx is None:
				raise CheckpointConflict("这个会话没有快照")
			if ctx[0] != expected_version:
				raise CheckpointConflict(
					f"快照版本对不上(库里是 v{ctx[0]},请求带的是"
					f" v{expected_version})—— 刷新页面看看最新状态")
			if ctx[1] != turn_id:
				raise CheckpointConflict("库里那份快照不属于这一轮")
			last = conn.execute(
				"SELECT COALESCE(MAX(message_no), 0) FROM turn_messages"
				" WHERE turn_id = ?", (turn_id,)).fetchone()[0]
			if last > ctx[2]:
				raise CheckpointConflict(
					f"水位之后还有 {last - ctx[2]} 条记录没有核对,不能直接续跑")
			no = last + 1
			conn.execute(
				"INSERT INTO turn_messages (turn_id, message_no, kind, role,"
				" content_json, created_at) VALUES (?, ?, 'control', 'user',"
				" ?, ?)",
				(turn_id, no, json.dumps(block, ensure_ascii=False), now))
			text = json.dumps([*messages, {"role": "user", "content": block}],
			                  ensure_ascii=False, default=_block_json)
			self._put_context(conn, sid, text, now, False, turn_id=turn_id,
			                  covered=no,
			                  runtime=json.dumps(runtime, ensure_ascii=False))
			changed = conn.execute(
				"UPDATE turns SET status = 'running', finished_at = NULL,"
				" updated_at = ?, error_message = NULL, interrupt_reason = NULL"
				" WHERE id = ? AND session_id = ? AND status = 'interrupted'",
				(now, turn_id, sid)).rowcount
			if not changed:
				raise TurnStateConflict(
					"这一轮已经不是中断状态了(可能已经被别人恢复或者放弃过)")
			version = conn.execute(
				"SELECT version FROM session_contexts WHERE session_id = ?",
				(sid,)).fetchone()[0]
		return {"message_no": no, "version": version,
		        "messages": [*messages, {"role": "user", "content": block}]}

	def abandon_turn(self, sid: str, turn_id: str, expected_version: int,
	                 note: str, messages: list) -> dict:
		"""放弃一次中断的任务:收成 failed,并把"哪些结果不确定"写进历史。

		写进历史这一步不是客套。不写的话,模型下一轮会拿到一份**看起来干净**
		的上下文,以为自己知道世界现在长什么样 —— 而实际上有几个操作做没做
		成谁也不知道。那句话是给模型看的,内容由调用方组织,里面要点出操作
		的原文和"结果未知"。

		同样带版本、同样在事务里:放弃和恢复是两条互斥的路,两条都落在
		session_contexts.version 上,所以谁先谁后写得清清楚楚。
		"""
		now = time.time()
		block = [{"type": "text", "text": note}]
		with self._tx() as conn:
			ctx = conn.execute(
				"SELECT version, checkpoint_turn_id, covered_message_no"
				" FROM session_contexts WHERE session_id = ?", (sid,)).fetchone()
			if ctx is None or ctx[0] != expected_version:
				raise CheckpointConflict("快照版本对不上 —— 刷新页面看看最新状态")
			if ctx[1] is not None and ctx[1] != turn_id:
				raise CheckpointConflict("库里那份快照不属于这一轮")
			last = conn.execute(
				"SELECT COALESCE(MAX(message_no), 0) FROM turn_messages"
				" WHERE turn_id = ?", (turn_id,)).fetchone()[0]
			# **有尾部也允许放弃**,跟恢复那条路正相反:尾巴上挂着“进行到一半、
			# 结果未知”的记录时,放弃是唯一出路,把它也拦掉等于让这个会话永远
			# 卡住。水位跳过那几行是有意的 —— 它们没有被装进快照,而“它们是什么”
			# 由调用方写进那段说明里(见 server._interrupt_note):不是丢下不管,
			# 是换一种方式交代。
			no = last + 1
			conn.execute(
				"INSERT INTO turn_messages (turn_id, message_no, kind, role,"
				" content_json, created_at) VALUES (?, ?, 'control', 'user',"
				" ?, ?)",
				(turn_id, no, json.dumps(block, ensure_ascii=False), now))
			text = json.dumps([*messages, {"role": "user", "content": block}],
			                  ensure_ascii=False, default=_block_json)
			self._put_context(conn, sid, text, now, False, turn_id=turn_id,
			                  covered=no)
			changed = conn.execute(
				"UPDATE turns SET status = 'failed', finished_at = ?,"
				" updated_at = ?, error_message = ? WHERE id = ?"
				" AND session_id = ? AND status = 'interrupted'",
				(now, now, note, turn_id, sid)).rowcount
			if not changed:
				raise TurnStateConflict("这一轮已经不是中断状态了")
			version = conn.execute(
				"SELECT version FROM session_contexts WHERE session_id = ?",
				(sid,)).fetchone()[0]
		return {"message_no": no, "version": version,
		        "messages": [*messages, {"role": "user", "content": block}]}

	def finish_turn(self, sid: str, turn_id: str, status: str,
	                error_message: str | None, messages: list) -> None:
		"""一轮收尾:最终 Context 和 Turn 终态**同一个事务**。

		分两次写就有一个真实的窗口:本轮已经 completed,而库里那份上下文
		还停在开轮时读到的样子 —— 用户接着问下一轮,模型拿到的历史里少了
		刚跑完的这一整轮,而且不报错。

		error_message 只在失败时有值:正常跑完那条路径传 None,别传空字符串
		—— "没有错误原因"和"错误原因是空"在页面上是两回事。

		**条件更新命中 0 行 = 抛,不是打一行日志。** 那说明这一轮在库里已经是
		终态了(别人收过它),而此刻事务里那份上下文是**按"它还在跑"算出来的**
		—— 提交上去就是拿一份旧账盖掉终态任务的工作上下文,而且没有任何地方
		看得出来。抛出会让 _tx 回滚,上下文一个字不动;调用方拿到的是一句
		明确的失败,而不是"存成功了但状态没改"。
		"""
		now = time.time()
		text = json.dumps(messages, ensure_ascii=False, default=_block_json)
		with self._tx() as conn:
			# 轮末这一次不带 compacted:本轮压过的话,检查点那一次已经
			# 把 last_compacted_at 写上了,这里再写一遍只会把它推后。
			#
			# **带上水位**,而且水位取这一轮最大那条 —— 收尾这份正文是
			# 权威的最终历史,把水位留在最后一次回合检查点那个位置的话,
			# 快照正文里就会出现几条"水位之后"的记录。终态的轮次反正不给
			# 恢复(status 检查挡着),但留着这种自相矛盾的元数据,下一个人
			# 读它的时候要重新推一遍才敢用。
			covered = conn.execute(
				"SELECT COALESCE(MAX(message_no), 0) FROM turn_messages"
				" WHERE turn_id = ?", (turn_id,)).fetchone()[0]
			self._put_context(conn, sid, text, now, compacted=False,
			                  turn_id=turn_id, covered=covered)
			changed = conn.execute(
				"UPDATE turns SET status = ?, finished_at = ?, updated_at = ?,"
				" error_message = ? WHERE id = ? AND status = 'running'",
				(status, now, now, error_message, turn_id)).rowcount
			if not changed:
				raise TurnStateConflict(
					f"turn {turn_id} 收尾时已经不是 running —— 这一轮在库里"
					f"已经是终态,这份上下文没有写进去")

	def reap_running(self) -> int:
		"""把上次进程死掉时留下的 running 轮收成终态。启动时调一次。

		为什么需要:一轮的状态是它自己那条线程在最后写上的,进程被杀就没有
		那一次。库里于是留着一条永远 running 的行 —— 而页面画的正是库里这个
		status(见 ui/index.html 的 renderTurn),那个轮次框会一直显示"运行中",
		刷新也刷不掉。

		**为什么在 server 的 __main__ 里调,而不是在这儿(__init__)**:库是
		import 时就打开的(server.py 的 STORE)。pytest、那堆一次性检查脚本、
		REPL 都会 import 这个模块,写在 __init__ 里等于"任何一次 import 都可能
		改你的库"。启动时恢复是一个明确的动作,就该待在一个明确的地方。

		**边界,直说**:同一个库上开第二个实例,它会把第一个实例正在跑的那一轮
		收掉(第一个随后收尾时条件更新匹配 0 行,打上面那行 "收尾时已经不是
		running")。没有心跳列就分不出"死进程留下的"和"别人正在跑的";这一版
		靠"一个库一个 server"这条前提兜住 —— dev.py 的"端口有人在应答就拒绝
		启动"把最常见的那条第二条路也堵上了。真要更硬,下一步是启动时拿一个
		排他文件锁。

		**不碰 session_contexts**:那一份还是开轮之前的样子,下一轮正该从那儿
		接着跑。顺手在这儿"补一笔"等于悄悄丢掉一整轮历史 —— 这个方法只改轮
		自己的状态。

		finished_at 必须和 status 写在同一条 UPDATE 里,这是表上那个 CHECK
		要求的(status 不是 running 时它必须非空)。那条 CHECK 在这儿是朋友:
		谁以后漏掉它,SQLite 直接抛,而不是写进一条半合法的行。

		状态用 interrupted,不再是 failed。**这一版才敢改**:v1 里 SQLite
		改不了 CHECK,只能就地收成 failed 加一句原因,于是"进程被杀"和
		"模型自己报错"在库里长得一样 —— 而恢复入口正需要区分这两者:前者
		可以续,后者不该续。现在 turns 表在 v4 里重建过,CHECK 收得下
		interrupted 了。

		interrupt_reason 存机器读的短码(见文件上面那几个常量),error_message
		存给人看的那句。两个都要:页面拿前者决定画不画"继续",拿后者显示原因。
		"""
		now = time.time()
		with self._tx() as conn:
			return conn.execute(
				"UPDATE turns SET status = 'interrupted', finished_at = ?,"
				" updated_at = ?, error_message = ?, interrupt_reason = ?"
				" WHERE status = 'running'",
				(now, now, "进程重启,这一轮没有跑完",
				 INTERRUPT_PROCESS_RESTART)).rowcount

	@staticmethod
	def _put_context(conn, sid: str, text: str, now: float, compacted: bool,
	                 turn_id: str | None = None, covered: int = 0,
	                 runtime: str | None = None) -> None:
		"""写工作上下文,version 加一。

		用 upsert 而不是 UPDATE:UPDATE 打空行不报错,而这个文件里最怕的
		就是"静默什么都没发生"。建会话时已经插过一行(version=1),这儿
		正常走 conflict 那一支;真走 insert 那一支说明那一行没了,补上比
		丢掉强。

		last_compacted_at 走 COALESCE:没压过就保留上一次压的时间。直接写
		NULL 的话,轮末这次保存会把"三分钟前压过"这个事实抹掉。

		turn_id / runtime 给 None 时原样留着(save_context 那条老路、轮末收尾、
		放弃都走这一支):"不知道"必须写成"别动",不能写成空。水位清零等于把
		一份好快照自己变成不可恢复的;运行状态清空则会让"这一轮当时跑在第几
		回合、在干哪件事"这些信息,在收尾那一下凭空消失。

		所以只有真正知道这几样的调用方(save_checkpoint / resume_turn)才传值。
		"""
		if turn_id is None or runtime is None:
			row = conn.execute(
				"SELECT checkpoint_turn_id, covered_message_no, runtime_json"
				" FROM session_contexts WHERE session_id = ?", (sid,)).fetchone()
			if row is not None:
				if turn_id is None:
					turn_id, covered = row[0], row[1]
				if runtime is None:
					runtime = row[2]
		conn.execute(
			"INSERT INTO session_contexts (session_id, messages_json, version,"
			" updated_at, last_compacted_at, checkpoint_turn_id,"
			" covered_message_no, runtime_json) VALUES (?, ?, 1, ?, ?, ?, ?, ?)"
			" ON CONFLICT(session_id) DO UPDATE SET"
			"   messages_json = excluded.messages_json,"
			"   version = session_contexts.version + 1,"
			"   updated_at = excluded.updated_at,"
			"   last_compacted_at = COALESCE(excluded.last_compacted_at,"
			"                                session_contexts.last_compacted_at),"
			"   checkpoint_turn_id = excluded.checkpoint_turn_id,"
			"   covered_message_no = excluded.covered_message_no,"
			"   runtime_json = excluded.runtime_json",
			(sid, text, now, now if compacted else None, turn_id, covered,
			 runtime))



	# ---- 热路径:从不起异常 ----

	def append_turn_message(self, turn_id: str, message_no: int, kind: str,
	                        role: str, content, strict: bool = False,
	                        close_exec: str | None = None) -> int | None:
		"""记一条原始消息,返回它的行号(写不进去给 None)。

		message_no 由调用方发(它是内存里数的),所以失败会留下一个空号。
		允许空号:UNIQUE 只管不重复,而且这一版明确不重编号 —— 补号意味着
		去改已经落库的邻居,那是另一回事。

		**行号就是发给模型的那个"号"**(见 tools/compress.py 的 make_recall)。用它
		而不是自己数一个计数器,买的是三件事:

		  1. 只增不减由 SQLite 保证 —— 不用再维护"接着最大号发、被压掉的号
		     也算数"那套水位逻辑(那是为了在没有这个 id 的时候模拟它);
		  2. **有号 = 查得回来。** 写不进去就没有行号,上层就不发号,模型
		     看不到号也就点不动它 —— 不会出现"点了一个查不回来的号";
		  3. 查回来是主键命中,不用拿 LIKE 去扫 content_json 那种大字段。

		失败返回 None 而不是 0:0 是个合法行号吗?不是(INTEGER PRIMARY KEY
		 ︎从 1 起),但 None 的意思更明确 —— "没有这一行"。

		strict=True 走另一档:写不进去抛 PersistError,而不是打印一行。
		它给的是**恢复关键**的那三种记录(assistant 响应、工具结果、控制
		消息)—— 缺了它们,模型接下来看到的历史就是缺的,再往下跑是拿
		一份残史做决定。热路径那条(页面上少一条)照旧返回 None。

		close_exec 给的是 tool_use_id:同一个事务里顺手把 tool_execs 上那条
		两阶段标记收口。**必须同一个事务** —— 分两次写的话,中间那个窗口里
		库里的状态是"开始了、没结果",而结果其实已经落库了;恢复判定会为此
		把这轮判进人工核对(结果未知),明明它有结果。宁可一起写,让状态只有
		两种:没开始,或者开始了并且有结果。
		"""
		try:
			text = json.dumps(content, ensure_ascii=False, default=_block_json)
			with self._tx() as conn:
				cursor = conn.execute(
					"INSERT INTO turn_messages (turn_id, message_no, kind, role,"
					" content_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
					(turn_id, message_no, kind, role, text, time.time()))
				if close_exec is not None:
					conn.execute(
						"UPDATE tool_execs SET finished_at = ?, message_id = ?"
						" WHERE tool_use_id = ? AND turn_id = ?",
						(time.time(), cursor.lastrowid, close_exec, turn_id))
				return cursor.lastrowid
		except Exception as e:
			if strict:
				raise PersistError(
					f"这条 {kind} 记录没落库({type(e).__name__}: {e}),"
					f"停在这儿:再往下跑,模型手里的历史就缺了这一块") from e
			print(f"[sessions] 轮次消息没落库: {type(e).__name__}: {e}")
			return None

	def find_message(self, sid: str, message_id: int) -> object | None:
		"""按号(行号)捞回那条消息的正文;不在这个会话里就给 None。

		号就是 `turn_messages.id`,而那张表是**全库一张** —— 所以必须用
		`turns.session_id` 把它圈回这个会话。少了那一句,A 会话拿自己上下文里
		的一个号就能读到 B 会话的原文;而"两个会话互相看不见对方"是这张表
		唯一还立着的边界,破了不报错。

		读路径**要抛**(跟 load_context 同一条规矩:拿一份错的原文接着跑,
		比停下来糟得多)。查不到不算异常 —— 那是"这个号不在",给 None。
		"""
		with self._lock:
			row = self._conn.execute(
				"SELECT m.content_json FROM turn_messages AS m"
				" JOIN turns AS t ON t.id = m.turn_id"
				" WHERE m.id = ? AND t.session_id = ?",
				(message_id, sid)).fetchone()
		return json.loads(row[0]) if row else None

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
