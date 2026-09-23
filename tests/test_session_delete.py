"""删除与执行之间的互斥(R4)。

修之前:删除先问 `is_running()`(它只是看一眼锁),再删库 —— 问和删之间没有
互斥区间。真跑起来能出现这样的交错:

    删除线程看到空闲 → 执行线程取得锁、开轮 → 删除线程把会话删掉

于是那一轮开始写回时命中的是一个不存在的会话,而用户看到的是"聊到一半的东西
没了"。修法只有一条:**两条路都得先攥住同一个 sid 的那把锁**,检查和操作在
同一个互斥区间里。

盯四件事:

  一、执行攥着锁的时候删除返回冲突(409),会话和那一轮都完好 —— 而且那一轮
     照常收尾,没被打断
  二、删除先赢的时候,后面的执行必须**在拿到锁之后**才发现会话没了,回 404
     —— 不是一路走到 begin_turn 撞外键、回一句 500 "cannot start turn"
  三、两次都没开轮。会话没了就不该有轮次记录,哪怕是一闪而过的
  四、顺序是**先拿锁再查存在性**:反过来的话,"查到有、删的时候没了"这条路
     依然是开的

第二、三条里的顺序用 Event 和"脚本化的存在性回答"卡死,**不靠 sleep**:
顺序不确定的并发用例只能验不变量,验不了这两条具体的路径。
"""

import threading

import pytest

# import server 现在**不会**打开会话库(见它的 open_store):STORE 是 None,
# 要等 main() 拿到那把排他锁才装。所以测试模块可以放心在顶上 import 它。
import server
import sessions
from agent import TurnOutcome


class _FakeHandler:
	"""让 _post_ask / _post_delete 按非绑定方法调起来。

	不建 socket、不发请求 —— 验的是那两把锁的进出场顺序,不是路由。
	send_error 只记不抛,所以每个用例自己 assert 它报了哪个码。
	"""

	def __init__(self, body=None):
		self.body = body
		self.json = None
		self.error = None
		self.wrote = b""
		self.headers = {}
		self.wfile = self         # ndjson_emit(self.wfile)

	# _post_ask 里那两步都是按 handler 调的,得接到真的实现上去。_flush_unsaved
	# 是 R3 加的:上一轮没存进库的话,它要在开新轮之前先补写。
	_run_turn = server.Handler._run_turn
	_drive = server.Handler._drive
	_checkpoint = server.Handler._checkpoint
	_flush_unsaved = server.Handler._flush_unsaved

	def _json_body(self, expect):
		return self.body

	def _send_json(self, obj):
		self.json = obj

	def send_error(self, code, msg=None):
		self.error = (code, msg)

	def _cors(self):
		pass

	def send_response(self, code):
		pass

	def send_header(self, name, value):
		pass

	def end_headers(self):
		pass

	def write(self, data):
		self.wrote += data

	def flush(self):
		pass


@pytest.fixture
def env(monkeypatch, tmp_path):
	"""一个临时库 + 一个会话,装进 server.STORE。返回 (server, store, sid)。"""
	store = sessions.SessionStore(tmp_path / "server.db")
	monkeypatch.setattr(server, "STORE", store)
	# 会话锁按 sid 记(随机 hex),所以这份进程级状态不会串到别的用例
	return server, store, store.create_session("", "")["id"]


def test_执行期间删除被拒_那一轮照跑(env, monkeypatch):
	server, store, sid = env
	started, release = threading.Event(), threading.Event()

	def fake_loop(messages, **kwargs):
		started.set()
		assert release.wait(10), "测试没放行"
		return TurnOutcome("completed", "答")

	monkeypatch.setattr(server, "agent_loop", fake_loop)

	turn = _FakeHandler({"session": sid, "query": "问"})
	turn_thread = threading.Thread(
		target=server.Handler._post_ask, args=(turn,), daemon=True)
	turn_thread.start()
	assert started.wait(10), "那一轮没开起来"

	# 此刻锁真在这根线程手里(不是"我以为它在跑")
	delete = _FakeHandler()
	server.Handler._post_delete(delete, sid)
	assert delete.error == (409, "this session has a turn running"), delete.error
	assert store.session_exists(sid), "会话被删掉了"
	assert delete.json is None

	# 而且那一轮没被打断,收尾正常 —— 这就是"不许出现已创建的任务因删除而写回失败"
	release.set()
	turn_thread.join(10)
	assert not turn_thread.is_alive()
	last = store.list_turns(sid)["turns"][-1]
	assert last["status"] == "completed", last
	# 终态和回复都发出去了:那一轮是**正常**跑完的,不是被打断之后凑了个终态
	assert b'"kind": "reply"' in turn.wrote, turn.wrote[-200:]


def test_删除先赢_后续执行在锁内发现会话没了(env, monkeypatch):
	"""锁外那次说"在",锁内那次说"没了" —— 模拟删除线程正好插在中间。

	少了锁内这一下,控制流会走到 begin_turn 才撞外键,用户拿到的是一句
	500 "cannot start turn",而事实是**会话已经没了** —— 报错说的不是真事,
	而且白开了一轮。
	"""
	server, store, sid = env
	answers = [True, False]
	monkeypatch.setattr(store, "session_exists", lambda s: answers.pop(0))

	handler = _FakeHandler({"session": sid, "query": "问"})
	server.Handler._post_ask(handler)
	assert handler.error == (404, "no such session"), handler.error
	assert handler.json is None
	# 一行轮次都不该有:会话没了就不该开轮
	assert store.list_turns(sid)["turns"] == []


def test_删除之后再执行是404_而且没开轮(env):
	"""删除整条路走完之后(锁已经还了),同一个请求再进来。"""
	server, store, sid = env
	delete = _FakeHandler()
	server.Handler._post_delete(delete, sid)
	assert delete.json == {"ok": True}, delete.error
	assert store.session_exists(sid) is False

	handler = _FakeHandler({"session": sid, "query": "问"})
	server.Handler._post_ask(handler)
	assert handler.error == (404, "no such session"), handler.error
	assert store.list_turns(sid)["turns"] == []


def test_删除不存在的会话是404(env):
	"""锁拿得到,检查在锁内 —— "删一个不在的东西"不该报成功。"""
	server, _, _ = env
	handler = _FakeHandler()
	server.Handler._post_delete(handler, "根本没有这个会话")
	assert handler.error == (404, "no such session"), handler.error
	assert handler.json is None


def test_正常删除连轮次和事件一起带走(env):
	"""删除本身还是照旧:级联删。锁只是加在它前面的那一步。"""
	server, store, sid = env
	store.begin_turn(sid, "跑过一轮")
	handler = _FakeHandler()
	server.Handler._post_delete(handler, sid)
	assert handler.error is None, handler.error
	assert store.session_exists(sid) is False
	assert store.list_turns(sid)["turns"] == [], "轮次没跟会话一起删掉"
	assert store.events_since(sid, 0) == []
