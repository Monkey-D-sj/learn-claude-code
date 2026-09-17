import os
import time

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

from hooks import trigger_hooks

load_dotenv()

# 共享资源:主 agent 和子 agent 打同一个端点。
# 将来要让子 agent 走别的端点,再把它也变成参数。
#
# max_retries=0:SDK 自带一层重试,再叠我们的会变成"3 次尝试 × 每次 3 个请求",
# 实际发出去 9 个。重试策略只由 call_api 一处掌握。
client = Anthropic(
	base_url="https://api.deepseek.com/anthropic",
	api_key=os.getenv("DEEPSEEK_API_KEY"),
	max_retries=0,
)

MAX_ATTEMPTS = 3
BASE_DELAY = 1.0


def call_api(**kwargs):
	"""调一次 Messages API,可重试的失败按指数退避重试。

	重试:连接错误、超时、429、5xx
	不重试:其他 4xx —— 401/400 这类重试一百遍还是同样结果,只是浪费时间。

	except 的顺序要紧:RateLimitError 是 APIStatusError 的子类,
	写在它后面就永远轮不到,429 会被误当成 5xx。

	另外用 exc / err 两个名字:Python 3 里 `except X as exc` 的 exc
	在 except 块结束时就解绑了,块外再引用会 UnboundLocalError。
	"""
	for attempt in range(1, MAX_ATTEMPTS + 1):
		try:
			return client.messages.create(**kwargs)
		except anthropic.RateLimitError as exc:
			err, retryable = exc, True
		except anthropic.APIStatusError as exc:
			err, retryable = exc, exc.status_code >= 500
		except anthropic.APIConnectionError as exc:    # 含 APITimeoutError
			err, retryable = exc, True

		if not retryable or attempt == MAX_ATTEMPTS:
			raise err
		wait = BASE_DELAY * 2 ** (attempt - 1)
		print(f"\033[90m[retry {attempt}/{MAX_ATTEMPTS - 1}] "
		      f"{type(err).__name__}, {wait:.0f}s 后重试\033[0m")
		time.sleep(wait)


def final_text(response) -> str:
	"""最后一条回复里的文本部分(跳过 thinking 块)。"""
	return "".join(b.text for b in response.content if b.type == "text")


def agent_loop(messages: list, system: str, tools: list, model: str,
               max_rounds: int) -> str:
	"""跑一轮完整的 agent 循环,返回最后的文本回复。

	只负责机制。提示词、工具集、模型、轮数上限都从外面传进来 ——
	它不知道调用它的是主 agent 还是子 agent。

	max_rounds 数的是 API 调用次数:一轮 = 一次请求 + 它要的那些工具。
	这是唯一的兜底 —— 模型陷入循环、或者子 agent 不返回时,主 agent
	会一直卡着,所以上限不是可选项。
	"""
	handlers = {t.name: t.handler for t in tools}
	wire = [t.to_wire() for t in tools]
	rounds_since_todo = 0
	rounds = 0

	while True:
		# 在循环顶部查,不在底部。此处 messages 必定停在一个完整回合上
		# (首轮,或上一条是带 tool_result 的 user 消息)。
		# 若在工具执行完、tool_result 还没回填的位置退出,messages 里会
		# 留下没有结果的 tool_use,用户下次提问直接 400。
		if rounds >= max_rounds:
			print(f"\033[31m[round limit {max_rounds} reached]\033[0m")
			return f"Stopped: round limit of {max_rounds} reached, task incomplete."
		rounds += 1

		try:
			response = call_api(
				model=model,
				messages=messages,
				system=system,
				tools=wire,
				max_tokens=8000,
			)
		except anthropic.APIError as e:
			# 重试耗尽或不可重试:作为文本交回去,不让它掀翻整个会话。
			# 调用方(REPL / 子 agent)拿到的是一个字符串,不是异常。
			return f"Error: API call failed: {type(e).__name__}: {e}"
		messages.append({
			"role": "assistant", "content": response.content
		})

		tool_calls = [
			block for block in response.content if block.type == "tool_use"
		]

		if not tool_calls:
			force = trigger_hooks("Stop", messages)
			if force:
				# hook returned a message → inject it and continue
				messages.append({"role": "user", "content": force})
				continue
			return final_text(response)

		results = []
		used_todo = False
		for block in tool_calls:
			print(f"\033[33m$ {block.name} {block.input}\033[0m")
			blocked = trigger_hooks("PreToolUse", block)
			if blocked:
				results.append({
					"type": "tool_result",
					"tool_use_id": block.id,
					"content": str(blocked),
				})
				continue

			handler = handlers.get(block.name)
			try:
				output = handler(**block.input) if handler else f"error: unknown tool {block.name!r}"
			except Exception as e:
				output = f"Error: {type(e).__name__}: {e}"
			trigger_hooks("PostToolUse", block, output)

			if block.name == "todo_write":
				used_todo = True

			results.append({
				"type": "tool_result",
				"tool_use_id": block.id,
				"content": output,
			})

		rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
		if rounds_since_todo >= 3:
			results.append({
				"type": "text",
				"text": "<reminder>Update your todos.</reminder>"
			})
			rounds_since_todo = 0

		# Feed tool results back, loop continues
		messages.append({"role": "user", "content": results})
