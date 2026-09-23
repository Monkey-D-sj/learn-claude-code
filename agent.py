import os
import time
from dataclasses import dataclass

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

import usage
from config import ROUND_WARN
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


def delta_of(event) -> tuple[str, str]:
	"""从一条流事件里取出能转发的碎片 (去哪儿, 什么字)。取不到返回空。

	只转发**正文**和**推理** —— 它们每时每刻都是能显示的东西。

	工具参数的碎片不转发:它是半个 JSON(拼到一半长这样 {"path": "/c),
	拼好之前解析不了、执行不了,拿去问用户也问不明白。完整的那块由 SDK
	拼好交回来。
	"""
	if getattr(event, "type", None) != "content_block_delta":
		return "", ""
	delta = getattr(event, "delta", None)
	kind = getattr(delta, "type", None)
	if kind == "text_delta":
		return "text", delta.text
	if kind == "thinking_delta":
		return "thinking", delta.thinking
	return "", ""


def _meter(purpose: str, model: str, usage_obj, attempt: int, ok: bool,
           started: float, kind: str) -> None:
	"""把一次调用的用量交给账本。薄壳,不做判断。

	判断放在 usage.meter 里,是因为"记什么"和"什么时候记"是两件事:这里只
	知道时机(拿到响应了 / 这次要重试了),那边知道该留下哪些字段。
	"""
	usage.meter(purpose=purpose, model=model, usage_obj=usage_obj,
	            attempt=attempt, ok=ok,
	            elapsed_ms=int((time.monotonic() - started) * 1000), kind=kind)


def call_api(llm_client, emit, stream: bool = True, purpose: str = "main",
             **kwargs):
	"""调一次 Messages API,可重试的失败按指数退避重试。

	重试:连接错误、超时、429、5xx
	不重试:其他 4xx —— 401/400 这类重试一百遍还是同样结果,只是浪费时间。

	client 是参数不是全局:压缩器(context.py)也要走这条路,而它拿的是
	构造函数注进来的那个 client。写死用模块级那个的话,压缩器注进来的
	就静默失效了 —— 改了不生效、不报错。

	这是全项目唯一的重试出口。谁要直连 client.messages.create,就绕过了
	这里所有的退避策略,包括摘要那次调用。

	except 的顺序要紧:RateLimitError 是 APIStatusError 的子类,
	写在它后面就永远轮不到,429 会被误当成 5xx。

	另外用 exc / err 两个名字:Python 3 里 `except X as exc` 的 exc
	在 except 块结束时就解绑了,块外再引用会 UnboundLocalError。

	**流式是默认的**,它带来一条新规矩:已经吐出去的字收不回来,所以吐过
	之后**不再重试** —— 再试一次,页面上就会凭空重复一段。一个字都没吐的
	时候照旧重试。碎片走传进来的那个 emit(落不落库由 emit 那头决定);
	返回的是 SDK 拼好的 message,形状跟以前一样,下游不用改。

	不想流就传 stream=False —— 压缩器那次摘要就是这么调的:内部工序,流
	出去会像模型在说话。

	purpose 是"这笔钱算谁的"。全项目四个调用点都把用量送进同一个账本,但账上
	必须分得开:主循环、压缩摘要、vision。分不开的话最贵的那笔永远是看不见的
	—— 摘要调用拿的是**完整上下文**,它可能是整个会话里最大的一笔,而以前它
	在账上是零。

	跟 emit 一样,必须写在 **kwargs 前面。写成 **{"purpose": ...} 会被当成
	messages.create 的参数发出去,SDK 那边报一个 TypeError —— 比 emit 那个
	被彻底静默吞掉好一点,但一样不该发生。
	"""
	model = kwargs.get("model", "")
	for attempt in range(1, MAX_ATTEMPTS + 1):
		streamed = False
		# 这次尝试的计时,以及"服务端已经告诉我们的用量"。
		#
		# partial 存在的唯一理由是重试:第一次请求哪怕在中途炸掉,它的 input
		# **已经被计费了** —— 而服务端把 usage 塞在 message_start 里,也就是流
		# 一开始就送到了。不接住它,每一次重试都有一笔账凭空消失。
		# 这跟下面"吐过字就不再重试"是同一类判断,只是换成了钱。
		#
		# message_delta 那份更全(output_tokens 到那儿才齐),所以后到的覆盖
		# 先到的。代价说清楚:**中途失败记下的 output_tokens 偏低** —— 最后
		# 一个 delta 没到,而模型可能已经吐了几百字。偏低是没办法的,但不能编:
		# 编出来的那一笔看起来是完整的,你就永远不会去查它。
		started = time.monotonic()
		partial = None
		try:
			if not stream:
				response = llm_client.messages.create(**kwargs)
				_meter(purpose, model, getattr(response, "usage", None),
				       attempt, True, started, "nonstream")
				return response
			with llm_client.messages.stream(**kwargs) as live:
				for event in live:
					event_type = getattr(event, "type", None)
					if event_type == "message_start":
						partial = getattr(getattr(event, "message", None),
						                  "usage", None)
					elif event_type == "message_delta":
						delta_usage = getattr(event, "usage", None)
						if delta_usage is not None:
							partial = delta_usage
					target, text = delta_of(event)
					if text:
						streamed = True
						emit({"kind": "delta", "target": target, "text": text})
				final = live.get_final_message()
			_meter(purpose, model, getattr(final, "usage", None),
			       attempt, True, started, "stream")
			partial = None
			return final
		except anthropic.RateLimitError as exc:
			err, retryable = exc, True
		except anthropic.APIStatusError as exc:
			err, retryable = exc, exc.status_code >= 500
		except anthropic.APIConnectionError as exc:    # 含 APITimeoutError
			err, retryable = exc, True
		finally:
			# 走到这儿 partial 还留着,只有一种可能:上面三条 except 一条都没
			# 接住 —— 一个非 APIError 跳了出去(emit 撞上断掉的连接
			# 之类)。不补这一下,那笔钱就从账上消失了,而"消失了"和"没花"
			# 在报表里长得一模一样。
			#
			# 放 finally 而不是放重试判断前面:重试会再走一遍上面这段,而
			# **失败的那一次不会再回来**。
			if partial is not None:
				_meter(purpose, model, partial, attempt, False, started, "partial")
				partial = None

		# 吐过字就不再重试:重试的代价从"再等一会儿"变成"屏幕上多一段"。
		# 这一条比退避策略重要,所以放在同一个判断里,别挪到下面去。
		if not retryable or attempt == MAX_ATTEMPTS or streamed:
			raise err
		wait = BASE_DELAY * 2 ** (attempt - 1)
		# emit 是命名参数,不会被 **kwargs 带走(它写在 **kwargs 前面,
		# 所以能截住)。写成 call_api(client, **{"emit": ...}) 就会被当成
		# messages.create 的参数发出去。
		emit({"kind": "note", "source": f"retry {attempt}/{MAX_ATTEMPTS - 1}",
		      "text": f"{type(err).__name__}, {wait:.0f}s 后重试"})
		time.sleep(wait)


def error_chain(err, limit: int = 3) -> str:
	"""把异常链上有信息量的几层串成一行,供报错用。

	SDK 的外层永远是固定措辞 —— APIConnectionError 只会说 "Connection
	error.",一个字都不多。真正的原因挂在链的下一层:

	    ConnectTimeout   网络慢,或 TLS 握手超时
	    ConnectError     连不上,或 TLS 被中间人截断
	    SSLError         证书不对
	    gaierror         DNS 解析不了

	这几种的处置方式完全不同,外层消息却长得一模一样。不挖出来,这个报错
	就没法自助 —— 只能靠猜,或者手工再挖一遍。

	跳过跟上一层一字不差的:httpx2 和 httpcore2 会把同一个底层错误各包
	一遍,原文完全相同,连打两遍只是让人多读一行。

	limit 是上限:链可以很深,而报错不该长到看不清。
	"""
	parts, seen, cause = [], str(err), err.__cause__
	while cause is not None and len(parts) < limit:
		text = str(cause) or type(cause).__name__
		if text != seen:
			parts.append(f"{type(cause).__name__}: {text}")
			seen = text
		cause = cause.__cause__
	return " <- ".join(parts)


def final_text(response) -> str:
	"""最后一条回复里的文本部分(跳过 thinking 块)。"""
	return "".join(b.text for b in response.content if b.type == "text")


@dataclass(frozen=True)
class TurnOutcome:
	"""一轮的结构化结果。

	为什么不是直接返回一个字符串:调用方要拿它决定这一轮在库里记成
	completed 还是 failed,而"失败"以前是靠**看返回的文本是不是以 Error
	开头**判断的 —— 模型自己完全可能回一句以 Error 开头的话,那时正常
	结束的一轮会被记成失败。这是"用字符串兼职状态",迟早要还。

	status   "completed" / "failed",跟 turns.status 的取值一一对应
	text     最后的文本回复,照旧是要显示给用户的那一句
	error    失败原因;completed 时是 None(不是空字符串)
	"""
	status: str
	text: str
	error: str | None = None


def _drop(kind: str, role: str, content) -> None:
	"""没给 record 时的占位。子 agent 没有会话库可记。"""


# 工具结果往事件里塞多少。bash 的门槛是 400000 字符,原样发出去一条命令
# 就能把页面冲垮。
#
# 截在发事件这一侧,不留给前端:让前端各自截的话,那 40 万字符已经先过
# 了一遍网络,而且截的规矩还得在每个前端里各写一份。
#
# 4000 落在"够看清在干什么"和"不淹没屏幕"之间 —— 报错、汇总、开头几行
# 都在里面了。要看全文本来也不该从这儿看:模型自己拿到的也是落盘预览,
# 路径就在那儿。
EVENT_RESULT_CHARS = 4000


def clip_for_event(output: str) -> str:
	if len(output) <= EVENT_RESULT_CHARS:
		return output
	return (f"{output[:EVENT_RESULT_CHARS]}\n"
	        f"... [display truncated: {len(output)} chars total]")

def round_warn(rounds: int, max_rounds: int) -> str:
	"""轮数快用完时塞给模型的那一句。

	报的是**这一次调用是第几次**、以及**它后面还允许几次** —— 不报"还剩几次"
	这种含糊说法:循环顶部先自增再算,`max_rounds - rounds` 是"这一次之后
	剩的",而模型读"还剩 3 次"时多半把当前这次也算进去,两边差一。差一在这
	里不是小事:最后一次调用收到的提醒要是写着"还剩 1 次",它会以为还能再调
	一个工具,而那一次之后循环直接停 —— 前面几十轮的工作一个字都交付不出去。

	三件事缺一不可:第几次、后面还有几次、**没做完的要说出来**。

	最后那句不是客套。子 agent 拿不到别的信号 —— tools/subagent.py 只把
	outcome 摊平成一段文本交回主 agent,主 agent 看不到它的中间过程。提醒
	如果只说"赶紧收尾",模型的自然反应是编一句"已完成",而那两个结论长得
	一模一样,主 agent 会拿半成品当结果往下做。
	"""
	after = max_rounds - rounds
	return (f"<reminder>Round budget: this is API call {rounds} of {max_rounds}, "
	        f"with {after} more allowed after this one. Stop starting new work. "
	        "Finish or answer now; if the task is not done, say plainly what is "
	        "done and what is missing - do not claim completion.</reminder>")

def agent_loop(messages: list, active_request: str, system: str, tools: list,
               model: str, max_rounds: int, compactor, emit, ask,
               record=_drop, checkpoint=None, stream: bool = True) -> TurnOutcome:
	"""跑一轮完整的 agent 循环,返回这一轮的结果(TurnOutcome)。

	只负责机制。提示词、工具集、模型、轮数上限、压缩器都从外面传进来 ——
	它不知道调用它的是主 agent 还是子 agent。

	max_rounds 数的是 API 调用次数:一轮 = 一次请求 + 它要的那些工具。
	这是唯一的兜底 —— 模型陷入循环、或者子 agent 不返回时,主 agent
	会一直卡着,所以上限不是可选项。

	compactor 也必须注入,不能在这儿建:它带着一个 model,而主 agent 和
	子 agent 用的不是同一个。模块级单例给不了两个对的。

	active_request 是当前这条指令的原文,原样交给压缩器。压缩到最后一档
	会把整段 messages 换成一条摘要,那条摘要里只有它 —— 不从外面传进来,
	当前任务就跟着一起被总结掉了。

	emit 是"往哪块屏幕说话"。它只发这个循环自己的事:哪个工具在跑、
	跑出什么、重试、上限。工具的日志、hook 的日志不归它管 —— 那些是
	诊断信息,不是"agent 干了什么"。

	它不发 reply:最后的文本仍然是返回值里的一项。这样"返回什么"和
	"显示什么"不会分家 —— 调用方拿 outcome.text 去显示,拿 outcome.status
	去记终态,两者出自同一次判断。

	ask 是"拿不准的时候问谁",签名 ask(question: str) -> bool。跟 emit 一样
	必须注入:它得绑在"这一轮那条响应流"上(server.py 的 make_ask),写死一个
	全局的话,几个会话会互相把问题推到别人的页面上。它只被 permission_hook
	用,子 agent 传的是"一律拒绝",理由见 tools/subagent.py。

	record 是"这一轮产生了什么",签名 record(kind, role, content)。
	只发给库的那一份,跟 emit 是两回事:emit 是**给屏幕看的**,会截断、
	会漏掉没有 seq 的;record 是**存档**,一字不改。所以别想从 events
	反推原始消息 —— 截断过的东西推不回去。默认是个空函数,子 agent 不用记。

	每一次 record 都安排在对应的 emit **前面**。这不是顺手:页面读轮次时
	拿事件游标当分界(turns 接口返回的那个 cursor),反过来的话,卡在
	两者中间的那次读会既没有这条消息、又已经跳过了它的事件 —— 页面上
	凭空少一条工具结果,而且刷新也补不回来。

	stream 是"这一轮要不要流式",默认要 —— 页面靠它把字尽早显示出来。
	子 agent 传 False(见 tools/subagent.py):它的 emit 是 emit.terminal_emit,
	而那个没有 delta 分支,碎片打进去等于丢掉,所以流式对它唯一的实际影响是
	**把重试禁掉** —— call_api 里"吐过字就不再重试"那条跟 emit 收到什么无关,
	模型吐第一个字的那一刻起,后面一个 500 或连接超时就没得重试了。
	"""
	# 放在函数里,不放模块顶上:context 反过来要 `from agent import call_api`
	# (压缩器第 4 档要调模型),模块级导入就成了环 —— 而 tools 那个包又拽着
	# tools.subagent,那边还要 `from agent import agent_loop`。函数里导入没这个
	# 问题,代价只是一次 sys.modules 查表。
	import context

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
			emit({"kind": "note", "source": f"round limit {max_rounds} reached",
			      "text": ""})
			return TurnOutcome(
				"failed",
				f"Stopped: round limit of {max_rounds} reached, task incomplete.",
				f"round limit of {max_rounds} reached")
		rounds += 1

		# 发送前压缩。必须赶在 call_api 之前:上一轮的工具结果已经追加
		# 进来但还没发出去,这时压掉才省得下钱;发完之后再压,钱已经花过。
		# 位置也只能在这儿 —— 此处 messages 停在完整回合上,切在 tool_use
		# 和它的 tool_result 之间下次请求直接 400。
		#
		# 切片赋值,不能用 messages = ...。
		#
		# messages 是调用方传进来的那个 list 对象(server.py 手里的那份),
		# 而 prepare 内部会构造新列表返回 —— snip_compact / compact_history
		# 都是。写成 = 的话本地名指向了新列表,调用方那份还停在旧的上面:
		# 这一回合的 assistant 回复和工具结果全写进了新列表,调用方看不见,
		# 下轮提问时整段工作凭空消失,而且不报错(连续两条 user 是合法的)。
		#
		# 切片赋值改的是原对象的内容,prepare 返回同一个还是新的都对。
		messages[:] = compactor.prepare(messages, active_request, checkpoint)

		# 轮数快用完了,提前说一声。判据是"这一次之后还剩几次"(rounds 已自增
		# 过):0 就是最后一次,那一次的提醒最要紧 —— 它必须让模型给出答复,
		# 而不是再开一个工具调用,否则这一轮什么都不会交付。
		#
		# 只进这一次请求的 payload,不进 messages。跟 todo 那个提醒不同:
		# 那条留在 history 里是无害的唠叨,这条留到下一轮就成了主动使坏 ——
		# 用户换个问题再问,模型一上来就看见"预算要没了",于是草草答一句。
		# 它会一直赖在那儿,直到被压缩归档。
		#
		# 也不能写成"先 append 进 messages、调用完再 pop":中间隔着上面那句
		# prepare(),它会 messages[:] = 重建整个列表(snip_compact 归档中段、
		# compact_history 整段换摘要)。等回来再按位置删,删掉的很可能已经
		# 是另一条消息,而且不报错 —— 正是"改错了静默毁历史"那一类。
		#
		# 位置在 prepare 之后:提醒是这一次请求的临时装饰,不是历史,不该被
		# 压缩器看见。此刻 messages 停在完整回合上,尾巴是那条带 tool_result
		# 的 user 消息,并进它而不是另起一条,形状跟 todo 提醒一致。
		payload = messages
		if 0 <= max_rounds - rounds < ROUND_WARN:
			warn = {"type": "text", "text": round_warn(rounds, max_rounds)}
			tail = messages[-1]
			if isinstance(tail.get("content"), list):
				payload = [*messages[:-1],
				           {**tail, "content": [*tail["content"], warn]}]
			else:
				# Stop hook 塞进来的那条控制消息是纯字符串,顺序展开会炸成
				# 一个个字符。退成另起一条 user —— 连续两条 user 是合法的。
				payload = [*messages, {"role": "user", "content": [warn]}]
			# 记进库里那条是"模型当时看到了什么"。不进 history 是另一回事:
			# context 那份存的是可继续的对话,这条只属于这一次请求。
			record("control", "user", [warn])

		try:
			response = call_api(
				client,
				emit,
				model=model,
				messages=payload,
				system=system,
				tools=wire,
				max_tokens=8000,
				stream=stream,
			)
		except anthropic.APIError as e:
			# 重试耗尽或不可重试:作为结果交回去,不让它掀翻整个会话。
			# 调用方(REPL / 子 agent)拿到的是一个 TurnOutcome,不是异常。
			#
			# 根因必须带上 —— 光看外层那句话是分不出诊的,见 error_chain。
			detail = f"{type(e).__name__}: {e}"
			chain = error_chain(e)
			if chain:
				detail += f" <- {chain}"
			return TurnOutcome("failed", f"Error: API call failed: {detail}",
			                   detail)
		messages.append({
			"role": "assistant", "content": response.content
		})
		# 完整响应一到就记。包括最后那条纯文本的回复 —— 它是"这一轮模型
		# 说过的话"里最该留下的那一句,漏了它这一轮在库里就只剩工具。
		# 只记这一次:轮末收尾不再补一条,否则同一句话会出现两遍。
		record("assistant_response", "assistant", response.content)

		# 推理内容往外发一份。
		#
		# 它本来就存在 messages 里(thinking 块),但那是给模型自己下一轮看的
		# —— 事件流里没有任何一种携带它,所以页面上看不见、重放里也没有。
		#
		# 位置在这儿:推理发生在这一轮的工具调用和回复**之前**。放这个位置,
		# 事件流的顺序才跟真实发生的顺序一致,页面重放出来也就是那个顺序。
		#
		# 发的是截断版,跟工具输出同一笔账(clip_for_event):推理可以几千字,
		# 而这条要落库、要重放、要塞进 DOM。完整的那份在 messages 里,
		# 一个字不少 —— 库里存的两份东西本来就各有各的完整度。
		#
		# signature 不发:那是签名,只在发回 API 时有用(在 messages 里),
		# 给页面看没有意义。
		#
		# 空的不发:模型有时会返回空的 thinking 块(实测最后一轮就会有),
		# 发出去页面上就多一个点开什么都没有的折叠块。
		for block in response.content:
			if block.type == "thinking" and block.thinking.strip():
				emit({"kind": "thinking", "text": clip_for_event(block.thinking)})

		tool_calls = [
			block for block in response.content if block.type == "tool_use"
		]

		if not tool_calls:
			force = trigger_hooks("Stop", messages)
			if force:
				# hook 要它接着干:这不是"这一轮结束了",而是又塞了一条
				# 用户消息进去。所以记的是 control,而且不返回 —— 返回了
				# 这一轮就会以 completed 收尾,而它其实还没干完。
				messages.append({"role": "user", "content": force})
				record("control", "user", force)
				continue
			return TurnOutcome("completed", final_text(response))

		results = []
		used_todo = False
		for block in tool_calls:
			# 调用和结果分两次发,前端才能知道"这条结果属于哪次调用"。
			# 被 hook 拦下来的那次也有结果(拦截理由),所以 tool_result
			# 在每条路径上都要发,不然页面上会留一个没有下文的调用。
			emit({"kind": "tool_call", "name": block.name, "input": block.input})
			blocked = trigger_hooks("PreToolUse", block, ask)
			if blocked:
				output = str(blocked)
			else:
				handler = handlers.get(block.name)
				try:
					# bind 把当前这份 messages 递给 compress 那个工具 ——
					# handler 只拿得到 **block.input,够不着它,而压缩改的正是
					# 这个活列表。跟 usage.span 同一种做法、同一个理由。
					with context.bind_messages(messages):
						output = handler(**block.input) if handler else f"error: unknown tool {block.name!r}"
				except Exception as e:
					output = f"Error: {type(e).__name__}: {e}"
				trigger_hooks("PostToolUse", block, output)
				used_todo = used_todo or block.name == "todo_write"

			# 两条路(被拦 / 跑完)在这儿合流,是为了让"记一条工具结果"
			# 只写一处。写三处的话,将来加第四条路(比如超时)时漏掉一处
			# 是不报错的:页面上少一条结果,而上下文里那条还在。
			result = {
				"type": "tool_result",
				"tool_use_id": block.id,
				"content": output,
			}
			# **号 = 库里那一行的行号**,由 record 的返回值给(见 sessions.
			# append_turn_message)。所以"有号"和"查得回来"是同一件事:写不
			# 进去就没有行号,也就没有号,模型看不见它自然点不动 —— 不会出现
			# "点了一个查不回来的号"。
			#
			# 没接 record 的(子 agent)走 `_drop`,恒返回 None,结果就
			# 保持原样 —— 它们本来也没有地方能查回来。
			#
			# **库里那一行不带号,这是对的。** record 是 json.dumps 的当下快照,
			# 而号要等它返回行号才知道,所以存下去的那份结尾没有号 —— 无所谓:
			# 查回来是按行号查,不靠在正文里搜号。上下文里这份带着号就够了,而
			# 它会被轮末的 save_context 一起存下来,所以接着聊时号还在。
			#
			# 号一旦拼上就**不该再被改写**:正文尾巴是它唯一的落脚点,而第
			# 1~4 档压缩改写正文。第 1 档已经用 _split_marker 摘下来再拼回去,
			# 2~4 档现在是注释掉的 —— 重开之前每一处都得补同样的处理。
			row_id = record("tool_result", "user", [result])
			if row_id is not None:
				result["content"] = context.stamp_tag(output, f"m{row_id:05d}")
			results.append(result)
			emit({"kind": "tool_result", "name": block.name,
			      "output": clip_for_event(output)})

		rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
		if rounds_since_todo >= 3:
			reminder = {
				"type": "text",
				"text": "<reminder>Update your todos.</reminder>"
			}
			results.append(reminder)
			record("control", "user", [reminder])
			rounds_since_todo = 0

		# Feed tool results back, loop continues
		messages.append({"role": "user", "content": results})
