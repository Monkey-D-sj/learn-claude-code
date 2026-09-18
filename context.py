from pathlib import Path


def _display(path: Path) -> str:
	"""给模型看的路径,一律正斜杠。

	Windows 上 str(Path) 是反斜杠,模型拿它去 bash 里用会被当成转义。
	posix 形式 git bash 和 Python 的 open 都认。

	用绝对路径而不是相对 WORKDIR:这个路径要被解析回来的,而相对路径
	能不能解析取决于进程 cwd 有没有变过。
	"""
	return path.resolve().as_posix()


def _safe_name(tool_id: str) -> str:
	"""tool_id 来自 API,不洗干净就拼进文件名等于让外部决定写哪儿。"""
	cleaned = "".join(c for c in tool_id if c.isalnum() or c in "-_")
	return cleaned[:80] or "result"


def _preview(text: str, head: int, tail: int) -> str:
	"""头 + 尾,中间标出省略了多少。

	text[-0:] 会取到整个串,不是空 —— 所以 tail 为 0 时必须单独判,
	否则"不要尾巴"会变成"把全文再贴一遍"。
	"""
	if len(text) <= head + tail:
		return text
	omitted = len(text) - head - tail
	start = text[:head] if head > 0 else ""
	end = text[-tail:] if tail > 0 else ""
	return "\n".join(
		part for part in (start, f"... [omitted {omitted} chars] ...", end) if part
	)


class ContextCompactor:
	"""发请求之前压缩 messages。

	分三层,代价从低到高:
	  1. 大结果落盘,上下文里只留路径 + 头尾预览   —— 不调模型,可逆
	  2. 丢掉旧的 tool_result,保留 assistant 的文本 —— 不调模型
	  3. 摘要中段                                  —— 要调模型,不可逆,而且毁 cache

	现在只做了第 1 层。压缩和 prompt cache 是冲突的:改到前缀就等于
	让后面全部缓存失效,所以宁可少压、压得狠,不要频繁地浅压。
	"""

	# 整段上下文的触发线。第 2、3 层用,现在还没接上。
	CONTEXT_CHAR_LIMIT = 50000

	# 单轮一批 tool_result 的字符总和门槛。
	# 注意它比 run_bash 的 50000 截断还大,一轮里凑不出这么多,所以
	# 现在基本够不到 —— 真正该用的是下面那个单结果门槛。
	TOOL_RESULT_BATCH_CHAR_LIMIT = 200000

	# 单个结果超过这个才值得落盘。太小的话"落盘 + 模型再读一次"比
	# 直接放进上下文更贵:落盘不是免费的,它只是把上下文成本换成了
	# 磁盘写 + 大概率一次额外读取。
	LARGE_RESULT_CHAR_LIMIT = 30000

	# 喂给摘要器的输入上限(第 3 层用)。摘要器必须能看到第一条 user
	# 消息,否则它不知道原始任务是什么,摘要出来会丢掉目标。
	SUMMARY_INPUT_CHAR_LIMIT = 80000

	# 压缩时保留多少。按消息条数保有个陷阱:5 条可能全是 tool_result,
	# 一条用户的话都没有 —— 按回合数保,并且强制至少留一条纯文本。
	KEEP_RECENT_RESULTS = 3
	KEEP_RECENT_MESSAGES = 5

	# 落盘后留在上下文里的预览长度。
	#
	# 只留头是系统性偏的:run_bash 是 stdout + stderr,报错、traceback、汇总
	# 都落在最后;而且我们自己写的截断标注也在尾部 —— 只留头的话,模型看不出
	# 盘上那份本身也是残的,读回来照样以为拿到了全部。
	#
	# 尾比头小一个量级,是因为尾部的价值集中在最后几行(汇总行、最后一个
	# 异常),再往上翻收益掉得很快。
	PREVIEW_HEAD = 2000
	PREVIEW_TAIL = 300

	def __init__(self,llm_client, model: str, transcript_dir: Path,
	             tool_results_dir: Path):
		# client/model 是给第 3 层(摘要)准备的,现在还没用上。
		# model 必须在每次调用时才对 —— 主 agent 和子 agent 用的不是
		# 同一个,所以这个对象不能建成模块级单例。
		self.client = llm_client
		self.model = model
		self.transcript_dir = transcript_dir
		self.tool_results_dir = tool_results_dir

	def tool_result_budget(self, messages: list, max_chars: int | None = None) -> list:
		"""第 1 层:一批 tool_result 太大,就把最大的几个落盘。

		只看 messages[-1],所以它管的是"最新这一批",不是整段上下文 ——
		老的 tool_result 永远不回收,上下文仍然会一批一批地涨。
		真正兜住增长要靠 CONTEXT_CHAR_LIMIT,那条线还没接。

		原地改 blocks 再返回同一个 list,不构造新列表。
		"""
		last_message = messages[-1]
		content = last_message.get("content")
		if last_message.get("role") != "user" or not isinstance(content, list):
			return messages

		blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
		limit = max_chars or self.TOOL_RESULT_BATCH_CHAR_LIMIT
		total = sum(len(str(block.get("content", ""))) for block in blocks)

		# 从大到小:先砍大的,少砍几个就够,小结果留着还能读。
		#
		# sorted 返回的是新列表,但里面装的是同一批 dict 的引用;循环体改的是
		# block["content"],原地改那个共享的 dict。两者叠加的结果是:
		# 迭代顺序按大小走,blocks 自己的顺序一点没动。
		#
		# 顺序必须保住 —— tool_result 要靠 tool_use_id 跟前面 assistant 的
		# tool_use 配对,没有理由去动它。排序只用来决定"先砍谁"。
		#
		# 而这个性质依赖"改的是同一个 dict"。谁要是改成
		#     block = {**block, "content": ...}
		# 或者把 blocks 换成构造出来的新列表,落盘照样跑、日志照样打,
		# 只有 messages 没变 —— 一种不报错的失效。
		for block in sorted(blocks, key = lambda item: len(str(item.get("content", ""))), reverse=True):
			if total < limit:
				break
			output = str(block.get("content", ""))
			if len(output) <= self.LARGE_RESULT_CHAR_LIMIT:
				continue
			block["content"] = self.persist_large_output(block.get("tool_use_id", "unknown"), output)
			# 落盘后这个 block 变小了,总和得重算,否则会多砍几个。
			# 重算用的是 blocks 而不是上面排序过的那个:它们是同一批 dict,
			# 用哪个结果都一样,但 blocks 才是最后要返回的那份,不容易误会。
			total = sum(len(str(item.get("content", ""))) for item in blocks)

		return messages
	
	
	def save_output(self, tool_use_id: str, output: str) -> Path:
		"""完整输出写盘,返回路径。"""
		self.tool_results_dir.mkdir(parents=True, exist_ok=True)
		path = self.tool_results_dir / f"{_safe_name(tool_use_id)}.txt"
		path.write_text(output, encoding="utf-8", errors="replace")
		return path

	def persisted_output_path(self, output: str) -> Path | None:
		"""这段内容如果已经是"落过盘的标记",把原文件路径捞回来。

		没有它,同一段内容被压第二次就会套娃:第二次存进去的是第一次
		的预览,预览里再套预览。

		标记里的路径是不可信输入 —— 它进了上下文,模型可以改它,别的
		内容也能伪造它。所以要验:必须真在 tool_results_dir 里,而且是
		个存在的文件。
		"""
		if not output.startswith("<persisted-output>\n"):
			return None
		line = next((line for line in output.splitlines()
		             if line.startswith("Full output: ")), None)
		if line is None:
			return None
		candidate = Path(line.removeprefix("Full output: ").strip())
		try:
			candidate = candidate.resolve()
		except OSError:
			return None
		if (not candidate.is_relative_to(self.tool_results_dir.resolve())
		        or not candidate.is_file()):
			return None
		return candidate

	def persisted_preview(self, tool_use_id: str, output: str,
	                      head_chars: int = PREVIEW_HEAD,
	                      tail_chars: int = PREVIEW_TAIL) -> str:
		"""落盘,返回带预览的标记。

		返回值永远非空。落盘失败也必须退回预览 —— 返回空字符串等于
		告诉模型"这个工具没有输出",那是静默的错误信息,比截断更糟:
		截断至少让它知道有东西被砍了。
		"""
		saved_path = self.persisted_output_path(output)
		if saved_path is not None:
			# 已经落过盘:预览要从原文件读,别拿标记本身当内容
			try:
				content = saved_path.read_text(encoding="utf-8", errors="replace")
			except OSError:
				content = output
		else:
			try:
				saved_path = self.save_output(tool_use_id, output)
			except OSError as e:
				# 权限、磁盘满……落盘失败不该把输出一起带走
				return (f"[output too large: {len(output)} chars, "
				        f"could not save ({e})]\n\n"
				        f"{_preview(output, head_chars, tail_chars)}")
			content = output

		return (f"<persisted-output>\nFull output: {_display(saved_path)}\n"
		        f"Preview:\n{_preview(content, head_chars, tail_chars)}\n"
		        f"</persisted-output>")

	def persist_large_output(self, tool_use_id: str, output: str) -> str:
		if len(output) <= self.LARGE_RESULT_CHAR_LIMIT:
			return output
		return self.persisted_preview(tool_use_id, output)
	
	# 每轮预压缩
	def prepare(self, messages: list) -> list:
		"""agent_loop 每轮发送前调一次,返回就地改过的同一个 list。

		调用点在循环顶部、call_api 之前 —— 那里上一轮的工具结果已经
		追加进 messages 但还没发出去,压掉才省得下钱。发送之后再压,
		钱已经花过了。

		也只能在那儿压:此处 messages 必定停在一个完整回合上。切在
		assistant 带 tool_use、它的 tool_result 还没回填的位置,下次
		请求直接 400 —— 跟 MAX_ROUNDS 的检查点是同一个约束。
		"""
		# 工具压缩
		messages = self.tool_result_budget(messages)


		return messages
	