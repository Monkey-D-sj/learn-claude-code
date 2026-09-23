"""启动独占(R2):一个库、一个服务,靠库旁边的文件锁,不靠端口。

盯四件事:

  一、锁是真锁。同一进程里两把拿不到、跨进程也拿不到 —— 前者保证测试有效,
     后者才是产品行为。特别钉住 flock/LockFile 而不是 fcntl.lockf:后者
     **同进程内不互斥**,用它的话下面那条"第二把拿不到"会假通过
  二、**拿不到锁的实例一个字都不许写库**。这是整件事的目的:第二个实例一
     启动就会 reap_running(),而它分不出"死进程留下的"和"别人正在跑的"
  三、进程死了锁自己回来(强杀和正常退出两条路都验)。PID 文件在这儿会留下
     一个说谎的残骸,内核放的锁不会
  四、锁文件**不删**。删掉一个别人正持着的锁文件 = 让下一个来的人在一把
     新锁上成功 = 两把锁护同一个库 = 又回到两个服务

第二、三条要起真进程,所以走 AGENT_DB_PATH / AGENT_PORT 两个环境变量 + 独立
临时库:**不能碰用户正在用的 sessions.db,也不能占着 8765**(§7.1 的进程测试
那条)。这里不跑任何真实模型请求 —— 库里那条 running 轮是直接写进去的,它
代表"A 正在跑的那一轮"。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

import dblock
import sessions

ROOT = Path(__file__).resolve().parent.parent


# ---------- 一、锁本身 ----------

def test_同进程第二把拿不到(tmp_path):
	a = dblock.acquire(tmp_path / "x.db")
	try:
		# 换一个 fd 去拿同一把:必须失败。这一条同时是后面那些进程级用例的
		# 事实基础 —— 本地都互斥不了的话,跨进程那条测的是别的东西
		with pytest.raises(dblock.AlreadyRunning):
			dblock.acquire(tmp_path / "x.db")
	finally:
		a.release()


def test_释放之后能再拿(tmp_path):
	a = dblock.acquire(tmp_path / "x.db")
	a.release()
	# 重复 release 无害(收尾路径可能走到两次)
	a.release()
	b = dblock.acquire(tmp_path / "x.db")
	b.release()


def test_锁文件不删_删了就等于没锁(tmp_path):
	"""删掉锁文件之后,原来那位还持着它那把锁,而新人创建了一把**新的**。"""
	db = tmp_path / "x.db"
	a = dblock.acquire(db)
	lock_file = dblock.lock_path(db)
	assert lock_file.exists()
	a.release()
	assert lock_file.exists(), "释放时把锁文件删了 —— 下一个来的人会拿到一把新锁"


def test_不同库互不影响(tmp_path):
	a = dblock.acquire(tmp_path / "a.db")
	b = dblock.acquire(tmp_path / "b.db")
	try:
		assert a.path != b.path
	finally:
		a.release()
		b.release()


def test_锁文件就在库旁边(tmp_path):
	assert dblock.lock_path(tmp_path / "sessions.db") == tmp_path / "sessions.db.lock"


# ---------- 二、三、进程级 ----------

def _can_hold(db: Path) -> bool:
	"""这个库现在还拿得到锁吗?拿到就立刻还回去。"""
	try:
		dblock.acquire(db).release()
	except dblock.AlreadyRunning:
		return False
	return True


def _free_within(db: Path, seconds: float = 5.0) -> bool:
	"""等锁回到手上 —— **只在杀掉一个进程之后**用,别拿它当"挡住"的判据。

	进程刚被杀时锁会晚一点点才真的放开:TerminateProcess 是异步的,内核
	清理句柄在进程对象被信号之后(实测第一次跑就撞上过,单跑又好了 ——
	典型的窗口)。所以在"它死了锁该回来"这一条上等一下是诚实的做法,
	而"它活着时拿不到"那一条仍然当场判。
	"""
	deadline = time.time() + seconds
	while time.time() < deadline:
		if _can_hold(db):
			return True
		time.sleep(0.05)
	return False


def _env(**extra) -> dict:
	"""子进程的环境:继承 + 两个测试专用口。

	PYTHONUTF8=1 是**为了读它的输出**:子进程的 stdout 接的是管道,Python
	于是按 locale 编码写(cp936),而这边按 utf-8 解 —— 中文全成乱码,断言
	就永远对不上。写死编码比按平台猜好。
	"""
	return {**os.environ, "PYTHONUTF8": "1", **extra}


def _child(code: str, *args) -> subprocess.Popen:
	"""起一个继承环境的子进程 python(所以 import 得到本仓库的模块)。"""
	return subprocess.Popen([sys.executable, "-c", code, *map(str, args)],
	                        cwd=ROOT, env=_env(), stdout=subprocess.PIPE,
	                        text=True, encoding="utf-8", errors="replace")


def test_子进程持着的时候拿不到_它死了锁自己回来(tmp_path):
	"""强杀 = A 异常退出。靠的是内核在进程消失时放锁,不是谁记得去清理。"""
	db = tmp_path / "x.db"
	p = _child("import sys, time, dblock\n"
	           "dblock.acquire(sys.argv[1])\n"
	           "print('HELD', flush=True)\n"
	           "time.sleep(60)", db)
	try:
		assert p.stdout.readline().strip() == "HELD"
		assert not _can_hold(db), "子进程持着锁,这边却拿到了"
		p.terminate()
		p.wait(10)
	finally:
		if p.poll() is None:
			p.kill()
	assert _free_within(db), "子进程没了,锁没还回来"


def test_正常退出也放锁(tmp_path):
	db = tmp_path / "x.db"
	p = _child("import sys, dblock\ndblock.acquire(sys.argv[1])", db)
	assert p.wait(10) == 0
	assert _can_hold(db)


# ---------- 真服务:第二个实例拒绝启动 ----------

def _free_port() -> int:
	"""要一个当前没人占的端口。测试服务用,不占用户的 8765。"""
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


def _start_server(db: Path, port: int) -> subprocess.Popen:
	return subprocess.Popen([sys.executable, "server.py"], cwd=ROOT,
	                        env=_env(AGENT_DB_PATH=str(db), AGENT_PORT=str(port)),
	                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
	                        text=True, encoding="utf-8", errors="replace")


def _kill(p: subprocess.Popen) -> str:
	"""收掉子进程再读它的输出。先收后读 —— 反过来的话读会挂在那儿等 EOF。"""
	if p.poll() is None:
		p.terminate()
		try:
			p.wait(10)
		except subprocess.TimeoutExpired:
			p.kill()
			p.wait(10)
	return p.stdout.read() or ""


def _ready(port: int, seconds: float = 30.0) -> bool:
	"""等服务真的能应答。

	不信它那句 print(f"http://localhost:...") —— 那句在 bind 之前就打了,后面
	紧跟的可能就是"端口被占"。
	"""
	deadline = time.time() + seconds
	while time.time() < deadline:
		try:
			with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions",
			                            timeout=1) as r:
				if r.status == 200:
					return True
		except (urllib.error.URLError, OSError):
			time.sleep(0.2)
	return False


def _get(port: int, path: str):
	with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
		return json.loads(r.read())


@contextmanager
def _server(db: Path, port: int):
	p = _start_server(db, port)
	try:
		assert _ready(port), f"服务没起来:\n{_kill(p)}"
		yield p
	finally:
		if p.poll() is None:
			_kill(p)


def test_第二个实例拒绝启动_而且没动第一个的库(tmp_path):
	"""R2 的验收:临时库起 A,A 手里有一条 running 轮;起 B —— B 必须拒绝启动,
	而且**状态不变**(那条轮还是 running)。

	库里那条 running 轮是直接写进去的,代表"A 正在跑的那一轮"(真跑一轮要
	真模型,§7.1 明确不依赖网络)。它是不是 A 自己开的对这一条不重要:要紧的
	是 B 不该碰它。
	"""
	db = tmp_path / "sessions.db"
	port_a = _free_port()
	with _server(db, port_a):
		# 从测试进程直接写进这个库:A 是**另一个进程**,看不到这里的内存锁
		store = sessions.SessionStore(db)
		sid = store.create_session("", "")["id"]
		turn = store.begin_turn(sid, "干一件长活")

		# A 自己看得见这条(顺带钉住 AGENT_DB_PATH 真生效:不生效的话 A
		# 读的是你的真库,这一步是 404)
		seen = _get(port_a, f"/session/{sid}/turns")
		assert [t["status"] for t in seen["turns"]] == ["running"], seen

		port_b = _free_port()
		b = _start_server(db, port_b)
		try:
			assert b.wait(30) == 1, "第二个实例没有拒绝启动"
			out = _kill(b)
		finally:
			if b.poll() is None:
				b.kill()
				b.wait(10)

		assert "已经被另一个进程占用" in out, out
		assert str(db) in out, out
		# 拒绝发生在 serve_forever 之前:B 从来没绑过端口
		assert not _ready(port_b, seconds=1.0), "B 居然在应答"

		# 而 A 那条 running 轮还在 running —— 这就是"状态不变"
		after = store.list_turns(sid)["turns"][0]
		assert after["status"] == "running", after
		assert after["error_message"] is None, after
		assert turn["status"] == "running"
		assert _get(port_a, f"/session/{sid}/turns")["running"] is False


def test_第一个没了新的实例接手并清理遗留任务(tmp_path):
	"""A 异常退出之后:锁要放得下,而库里那条永远 running 的轮要被收掉
	(不然页面那个轮次框一直显示"运行中",刷新也刷不掉)。"""
	db = tmp_path / "sessions.db"
	store = sessions.SessionStore(db)
	sid = store.create_session("", "")["id"]
	store.begin_turn(sid, "没跑完就走了")

	with _server(db, _free_port()):
		status = store.list_turns(sid)["turns"][0]
		assert status["status"] == "failed", status
		assert "重启" in (status["error_message"] or ""), status
		assert status["finished_at"] is not None, status
