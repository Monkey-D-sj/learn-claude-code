"""开发用:server.py 一改就自动重启,不用手动停/起。

为什么不干脆在进程里重载:这个进程是长跑的服务,手里攥着 sqlite 连接、会话锁和
一堆内存状态(见 server.py 顶上那段)。进程内换掉代码等于把那些状态丢在半路,
重启是唯一干净的做法 —— 这里只是把重启这一步自动化。

**改 .py 要重启,改页面不用**:页面是每次请求现读的(server.py 的 _page),刷一下
浏览器就生效,所以这个脚本不看 ui/。

**有轮在跑的时候不重启 —— 等它跑完。** 这条不是礼貌,是它存在的理由:让"让 agent
改自己的服务端代码"变成安全的事。那一轮用旧代码跑完,下一轮才是新代码;直接重启
的话你会把正在跑的那一轮从中间掐死,而它可能正在改的就是自己。

跑法:uv run dev.py     Ctrl+C 退出(会先收掉子进程)。
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 跟 server.PORT 是同一个值,改要一起改(ui/index.html 的 BACKEND 里还有一份 ——
# 这个项目里端口就是这么写的)。
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

# 看一眼文件的间隔,以及"还在等"那句话最多多久喊一次。
POLL = 0.5
NAG = 15.0

# 除了根目录的 *.py,还要盯的:import 的包、启动时拼的技能清单(README 里提过,
# 加一个技能要重启,所以 skills/ 也算)、以及 .env(config/app 在 import 时读它,
# 改了不重启就是"我明明换了 key 啊")。
WATCH_DIRS = ("tools", "hooks", "skills")
WATCH_FILES = (".env",)


def iter_watched(root: Path = ROOT) -> list[Path]:
	"""要盯着的文件。规则只有一条:改了它而不重启,就会不生效。

	根目录的 *.py 里要跳过两种:``_`` 开头的是"验完就删"的一次性检查脚本,重启服务
	对它们毫无意义;``dev.py`` 是自己,没有东西会重载它。

	tests/ 不看(那是 pytest 的),ui/ 不看(页面每次请求现读)。
	"""
	out = []
	for p in sorted(root.glob("*.py")):
		if not p.name.startswith("_") and p.name != Path(__file__).name:
			out.append(p)
	for name in WATCH_DIRS:
		d = root / name
		if not d.is_dir():
			continue
		out += [p for p in sorted(d.rglob("*"))
		        if p.is_file() and "__pycache__" not in p.parts]
	for name in WATCH_FILES:
		p = root / name
		if p.exists():
			out.append(p)
	return out


def snapshot(root: Path = ROOT) -> dict[Path, tuple[int, int]]:
	"""每个被盯着文件的 (mtime_ns, size)。

	两个都要:mtime 的分辨率在某些文件系统上不够(一分钟内连改两次可能一模一样),
	而 size 不变的改动又在所多有。合起来够用了,不必上哈希 —— 几十个 stat 而已。
	"""
	out = {}
	for p in iter_watched(root):
		try:
			st = p.stat()
		except OSError:
			continue                  # 正好被删掉或改名了,下一眼再说
		out[p] = (st.st_mtime_ns, st.st_size)
	return out


def changed(before: dict, after: dict) -> list[Path]:
	"""跟上一次比,哪些文件不一样了。改了的、新加进来的、没了的都算。"""
	touched = [p for p, sig in after.items() if before.get(p) != sig]
	touched += [p for p in before if p not in after]
	return sorted(touched)


def backend_state() -> bool | None:
	"""后端的轮在跑吗?None = 问不到(子进程可能已经死了)。

	看的是 /sessions 里那个 running,它是服务端内存锁的映射(server.py 的
	is_running)—— 也就是"此刻真的有一轮在跑",不是库里的状态。库里的那个可能是
	上一次留下的(启动时会被 reap 收掉),拿它当判据会让重启永远等下去。
	"""
	try:
		body = urllib.request.urlopen(BASE + "/sessions", timeout=2).read()
	except Exception:
		return None
	return any(s.get("running") for s in json.loads(body)["sessions"])


def port_owner() -> str | None:
	"""这个端口上已经有人在应答吗?有就返回它的 pid(尽力而为了)。

	探端口而不是探进程:能应答就说明**已经有东西绑在上面了**,那是唯一要紧的事实。
	"""
	try:
		urllib.request.urlopen(BASE + "/sessions", timeout=1)
	except Exception:
		return None
	try:
		out = subprocess.run(["netstat", "-ano"], capture_output=True,
		                     text=True).stdout
	except OSError:
		return "?"
	pids = {line.split()[-1] for line in out.splitlines()
	        if f":{PORT} " in line and "LISTENING" in line}
	return "/".join(sorted(pids)) or "?"


def start_child() -> subprocess.Popen:
	"""起一个 server.py。

	继承 stdout/stderr,**不要 PIPE**:PIPE 不排空的话,服务端输出一多就会把它自
	己堵死在写上面 —— 而它那些 print(包括启动时那句 reap)恰恰是出问题时唯一能
	看的东西。
	"""
	return subprocess.Popen([sys.executable, "server.py"], cwd=ROOT)


def wait_ready(child: subprocess.Popen, seconds: float = 25.0) -> bool:
	"""等它真的能应答。

	不信 server.py 那句 print(f"http://localhost:...") —— 那句在 bind **之前**就打
	了,可能紧随其后的就是"端口被占"。
	"""
	deadline = time.time() + seconds
	while time.time() < deadline:
		if child.poll() is not None:
			return False
		if backend_state() is not None:
			return True
		time.sleep(0.2)
	return False


def stop_child(child: subprocess.Popen) -> None:
	"""弄死子进程,而且**确认它真的死了**。

	Windows 上这一步不能省:SO_REUSEADDR 在这儿不是 Unix 语义 —— 它允许新的
	socket 绑一个**还被别人持着**的端口,于是旧进程没死透就起新的,会出现两个
	server 打同一个库、同一个会话跑两轮,而请求由内核随手派发,谁都不知道自己连
	的是哪个。terminate() 是 TerminateProcess,异步的,所以必须 wait 到它真没了。

	边界也直说:terminate 收不到孙子进程 —— bash 工具起的那些会活下来。自用工具
	先这样,别假装它管得住。
	"""
	if child.poll() is not None:
		return
	child.terminate()
	try:
		child.wait(timeout=5)
		return
	except subprocess.TimeoutExpired:
		print("  5 秒还没退,强杀")
	child.kill()
	child.wait(timeout=5)


def main() -> int:
	# 一行输出就吐一行。重定向/管道下 Python 默认攒够 8KB 才吐,而这个脚本的全部
	# 价值就是"让你看见它在干什么" —— 攒着的时候它看起来像卡死了(而且子进程的
	# 输出是直接继承过去的、立刻就出来了,两份输出还会错位)。
	sys.stdout.reconfigure(line_buffering=True)

	owner = port_owner()
	if owner:
		# 拒绝启动,而不是替你把那个进程杀掉。跟那堆一次性检查脚本现在是同一条
		# 原则(它们以前会把 8765 上的东西一律 taskkill —— 于是把用户正看着的
		# 那个服务也杀了,页面死在"流断了")。
		print(f"{PORT} 上已经有个服务在应答(pid {owner})。")
		print("先把它关掉:一个库、一个端口上只能有一个 server,两个一起跑会互相踩。")
		return 1

	child = start_child()
	if not wait_ready(child):
		print("第一次就没起来 —— 看上面的输出。")
		stop_child(child)
		return 1
	print(f"服务在跑(pid {child.pid}): http://localhost:{PORT}/")
	print(f"盯着 {len(iter_watched())} 个文件,改了会自动重启(Ctrl+C 退出)")

	before = snapshot()
	pending = False
	waiting_since = None
	last_nag = 0.0
	dead_reported = False

	try:
		while True:
			time.sleep(POLL)
			now = snapshot()
			touched = changed(before, now)
			before = now
			if touched:
				# 新的改动只是把同一件事推后,不排队:反正重启一次就把所有改都带上了
				pending = True
				waiting_since = None
				last_nag = 0.0
				print("改了:", ", ".join(str(p.relative_to(ROOT)) for p in touched))

			if child.poll() is not None and not dead_reported:
				print(f"  子进程(pid {child.pid})自己退了。改一下文件会重新起一个。")
				dead_reported = True

			if not pending:
				continue

			if backend_state():
				# 有轮在跑。**不设放弃上限**:放弃 = 改动永远不生效而没人说话,那是
				# 这个仓库最讨厌的失败形态。等多久是这一轮说了算的 —— 卡在提问上的
				# 一轮能等满 ASK_TIMEOUT(5 分钟),这是它的正常代价,Ctrl+C 是逃生口。
				if waiting_since is None:
					waiting_since = time.time()
				if time.time() - last_nag >= NAG:
					last_nag = time.time()
					print(f"  有轮在跑,跑完就重启(已经等了 "
					      f"{int(time.time() - waiting_since)} 秒)")
				continue

			print("重启服务…")
			stop_child(child)
			# 确认端口真的空了**再**起新的。这一步不是多余:uv 起的 venv
			# python.exe 是个壳,它会再 fork 一个真解释器 —— 也就是子进程底下还有
			# 一层,而那层才是真正 listen 的那个。现在这层壳会带着孩子一起死
			# (uv 的 launcher 用了 job object),但那是它的实现细节,不是这里能假定
			# 的事。万一没死透,两个 server 会同时绑上(SO_REUSEADDR),请求由内核
			# 随手派发,同一个会话可能跑两轮 —— 静默,而且比"这次没重启"糟得多。
			still = port_owner()
			if still:
				print(f"  老的还占着 {PORT}(pid {still}),这次不起新的了。"
				      "把它关掉,然后随便改一下文件再试。")
				pending = False
				waiting_since = None
				continue
			child = start_child()
			dead_reported = False
			pending = False
			waiting_since = None
			if wait_ready(child):
				print(f"  起来了(pid {child.pid})")
			else:
				# 多半是新代码 import 就炸了。不重试:重试就是拿同一份坏代码原地
				# 打转;把 pending 放掉,等下一次改动。
				print("  **没起来** —— 看上面的输出。改一下文件会再试。")
				dead_reported = True    # 上面那句已经说过了,别再补一句"它自己退了"
	except KeyboardInterrupt:
		print("\n收工")
	finally:
		stop_child(child)
	return 0


if __name__ == "__main__":
	sys.exit(main())
