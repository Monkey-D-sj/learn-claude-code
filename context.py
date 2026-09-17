from pathlib import Path

from config import WORKDIR


def _display(path: Path) -> str:
	"""给模型看的路径,一律正斜杠。

	Windows 上 str(Path) 是反斜杠,模型拿它去 bash 里用会被当成转义。
	posix 形式 git bash 和 Python 的 open 都认。
	"""
	try:
		return path.relative_to(WORKDIR).as_posix()
	except ValueError:
		return path.as_posix()


def _safe_name(tool_id: str) -> str:
	"""tool_id 来自 API,不洗干净就拼进文件名等于让外部决定写哪儿。"""
	cleaned = "".join(c for c in tool_id if c.isalnum() or c in "-_")
	return cleaned[:80] or "result"


class ContextCompactor:
	CONTEXT_CHAR_LIMIT = 50000
	TOOL_RESULT_BATCH_CHAR_LIMIT = 200000
	LARGE_RESULT_CHAT_LIMIT = 30000
	SUMMARY_INPUT_CHAR_LIMIT = 80000
	KEEP_RECENT_RESULTS = 3
	KEEP_RECENT_MESSAGES = 5

	# 落盘后留在上下文里的预览长度(头尾各一半)。
	# 只留头是错的:bash 的报错、traceback、汇总都在尾部,
	# 而且模型不知道后面还有东西。
	PREVIEW_CHARS = 4000

	def __init__(self,llm_client, model: str, transcript_dir: Path, tool_result_dir: Path):
		self.client = llm_client
		self.model = model
		self.transcript_dir = transcript_dir
		self.tool_result_dir = tool_result_dir
		
	def tool_result_budget(self, messages: list, max_chars: int | None = None) -> list:
		last_message = messages[-1]
		content = last_message.get("content")
		if last_message.get("role") != "user" or not isinstance(content, list):
			return messages
		
		blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]
		limit = max_chars or self.TOOL_RESULT_BATCH_CHAR_LIMIT
		total = sum(len(str(block.get("content", ""))) for block in blocks)
		
		for block in sorted(blocks, key = lambda item: len(str(item.get("content", ""))), reverse=True):
			if total < limit:
				break
			output = str(block.get("content", ""))
			if len(output) <= self.LARGE_RESULT_CHAT_LIMIT:
				continue
			block["content"] = self.persist_large_output(block.get("tool_use_id", "unknown"), output)
			total = sum(len(str(item.get("content", ""))) for item in blocks)
			
		return messages
	
	
	def persist_large_output(self, tool_id: str, output: str) -> str:
		"""完整结果落盘,返回"路径 + 头尾预览"。

		返回值永远非空。落盘失败也必须退回截断 —— 返回空字符串等于
		告诉模型"这个工具没有输出",那是静默的错误信息,比截断更糟:
		截断至少让它知道有东西被砍了。
		"""
		if len(output) <= self.PREVIEW_CHARS:
			return output

		half = self.PREVIEW_CHARS // 2
		head, tail = output[:half], output[-half:]
		omitted = len(output) - len(head) - len(tail)
		preview = f"{head}\n... [omitted {omitted} chars] ...\n{tail}"

		try:
			self.tool_result_dir.mkdir(parents=True, exist_ok=True)
			path = self.tool_result_dir / f"{_safe_name(tool_id)}.txt"
			path.write_text(output, encoding="utf-8", errors="replace")
		except OSError as e:
			# 权限、磁盘满、路径不存在……落盘失败不该把输出一起带走
			return (f"[output too large: {len(output)} chars, "
			        f"could not save ({e})]\n\n{preview}")

		return (f"[output too large: {len(output)} chars, saved to "
		        f"{_display(path)} — read it with read_file, "
		        f"or grep it with bash]\n\n{preview}")
	
	# 每轮预压缩
	def prepare(self, messages: list) -> list:
		# 工具压缩
		messages = self.tool_result_budget(messages)
		
		
		return messages
	