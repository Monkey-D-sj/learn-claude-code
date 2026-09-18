"""浏览器前端:把 agent 跑在一个 HTTP 服务后面。

本机自用 —— 没有登录、没有多会话、没有数据库。进程里就一份 history,
一把锁保证同一时刻只有一轮在跑。

只有两个端点:

    GET  /        页面
    POST /ask     请求体是 JSON {"query": "..."},响应是一条 NDJSON 流
                  (一行一个 JSON)
    OPTIONS /ask  预检。跨源那道门就架在这儿,见 do_OPTIONS

**为什么不用 SSE(EventSource):** 它只能发 GET,查询就得塞进 URL。
改成 fetch() 读响应流,格式走 NDJSON —— 解析是几行 JS,还省掉 `data:`
那层包装。反正两端都是自己的,不用迁就 EventSource 的约束。

**为什么 POST 处理里直接跑 agent_loop:** emit 就是"往响应写一行再
flush",所以不需要队列、不需要第二个线程。一个请求一个线程
(ThreadingHTTPServer),这条流开着直到本轮跑完。

**协议用 HTTP/1.0(默认),故意不发 Content-Length:** 这样响应体的结束
由连接关闭来标记,浏览器那边读到 EOF 就是本轮结束。发 Content-Length
就得先把整轮跑完才知道长度,那就没有流了。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import agent_loop
from app import MODEL, SYSTEM, make_compactor
from config import MAX_ROUNDS
from hooks import trigger_hooks
from tools import TOOLS

PORT = 8765
PAGE = Path(__file__).parent / "ui" / "index.html"

# 进程里就这一份对话。单会话,刷新页面不会丢。
HISTORY: list = []

# 一轮在跑时 HISTORY 正在被改。第二个请求挤进来的话不是"慢",是会跟它
# 抢同一个列表 —— assistant 和 tool_result 交错写进去,发出去直接 400。
BUSY = threading.Lock()


def is_local_origin(origin: str) -> bool:
	"""只放行同机来源。

	**不能回 "\*"**:回了的话,你浏览器里随便开着的哪个网页都能 POST 过来
	指挥这个 agent 跑 bash,而且还能把结果读走。这个 agent 手里是真 shell,
	不能对任意网页开门。

	顺带说清楚一件事:CORS 只管"能不能读响应"。跨源的简单请求(POST +
	text/plain)不管有没有这个头,请求本身都会发出去、命令都会跑。所以要
	真挡住,得让请求**必须过预检** —— 见 do_OPTIONS 和页面那边的
	Content-Type: application/json。
	"""
	return origin.startswith(("http://localhost:", "http://127.0.0.1:"))


def ndjson_emit(wfile):
	"""造一个把事件写进响应流的 emit。

	每条后面 flush:不 flush 的话全攒在缓冲区里,页面要等整轮结束才一次
	看到全部 —— 那就退化成了"不做流式"。
	"""
	def emit(event: dict) -> None:
		line = json.dumps(event, ensure_ascii=False) + "\n"
		wfile.write(line.encode("utf-8"))
		wfile.flush()
	return emit


class Handler(BaseHTTPRequestHandler):
	def _cors(self):
		"""同机来源就回一个 Allow-Origin,别的什么都不回。"""
		origin = self.headers.get("Origin")
		if origin and is_local_origin(origin):
			self.send_header("Access-Control-Allow-Origin", origin)

	def do_OPTIONS(self):
		"""预检。这是道真门,不是走过场。

		页面从 IDE 的预览服务(另一个源)发请求时,浏览器先发这个。坏页面
		发来的预检带着它自己的 Origin,而 _cors() 不会回 Allow-Origin ——
		预检不过,后面那个 POST 根本不会发出去。

		前提是页面必须**触发**预检,也就是不能被当成简单请求。所以页面那边
		发的是 application/json,不是 text/plain。
		"""
		self.send_response(204)
		self._cors()
		self.send_header("Access-Control-Allow-Methods", "POST")
		self.send_header("Access-Control-Allow-Headers", "content-type")
		self.send_header("Access-Control-Max-Age", "600")
		self.end_headers()

	def do_GET(self):
		if self.path != "/":
			self.send_error(404)
			return
		# 每次请求现读,改完 HTML 刷一下页面就生效,不用重启服务
		body = PAGE.read_bytes()
		self.send_response(200)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(body)

	def do_POST(self):
		if self.path != "/ask":
			self.send_error(404)
			return

		length = int(self.headers.get("Content-Length", 0))
		raw = self.rfile.read(length) if length else b""
		# 走 JSON 而不是裸文本,是为了强制预检 —— 见 do_OPTIONS。
		try:
			query = str(json.loads(raw or b"{}").get("query", ""))
		except (ValueError, AttributeError):
			self.send_error(400, "body must be JSON: {\"query\": \"...\"}")
			return

		# 跟 main.py 同样的洗法:stdin 也好、socket 也好,坏字节凑不成
		# 合法序列时会被 surrogateescape 兜成孤代理项,那东西编码不进
		# API 请求体,会在 SDK 内部炸成 UnicodeEncodeError(不是 APIError,
		# 捕不到)。
		query = query.encode("utf-8", "replace").decode("utf-8").strip()

		if not BUSY.acquire(blocking=False):
			self.send_error(409, "a turn is already running")
			return
		try:
			if not query:
				self.send_error(400, "empty query")
				return

			self.send_response(200)
			self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
			self.send_header("Cache-Control", "no-store")
			self._cors()
			self.end_headers()

			emit = ndjson_emit(self.wfile)
			trigger_hooks("UserPromptSubmit", query)
			HISTORY.append({"role": "user", "content": query})

			# 压缩器按请求建:它的 emit 指向这条响应流,而流是每请求一个。
			# 建它很便宜,就是存几个引用。
			try:
				reply = agent_loop(HISTORY,
				                   active_request=query,
				                   system=SYSTEM,
				                   tools=TOOLS,
				                   model=MODEL,
				                   max_rounds=MAX_ROUNDS,
				                   compactor=make_compactor(emit),
				                   emit=emit)
			except Exception as e:
				# 兜底:异常不该把 HISTORY 一起带走,也不该让流断在半截
				# 而没有下文 —— 前端会一直转圈。
				emit({"kind": "reply",
				      "text": f"Error: {type(e).__name__}: {e}"})
				return
			emit({"kind": "reply", "text": reply})
		finally:
			BUSY.release()

	def log_message(self, fmt, *args):
		# 默认实现往 stderr 打一行每个请求。前端本来就是终端,不需要它
		# 再复述一遍。
		pass


if __name__ == "__main__":
	print(f"http://localhost:{PORT}/")
	ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
