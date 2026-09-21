"""看一眼图片,拿回一段文字。

**为什么是"就地消化"而不是把图放进对话:** 后者要动 context.py,而它的尺子
量的是**字符数**(fingerprint -> estimate_chars),base64 也算。拿仓库根目录
那张 153 KB 的 jpg 实测过:

	estimate_chars = 204574,而 CONTEXT_CHAR_LIMIT = 50000

于是一张图就让压缩器以为超了 4 倍,第一轮就把 tool_result 换成一句指针 ——
**图没了**,而且"落盘"的那份是 str(list) 出来的 Python repr(单引号),
永远发不回 API。token 那头反倒没事:那张图才 667 输入 token,出问题的只有
那把尺子。

所以图在这儿就地消化:handler 自己调一次模型,只把回答交回去。主对话里从头
到尾只有文字,上面那一整套一点都碰不到。

**为什么用主模型而不是 deepseek-v4-flash-vision-exp:** 主模型自己就能看图
(同一次实测:冰晶瞳孔、精灵耳、额头菱形宝石、水晶耳坠全说对了)。多引一个
模型只是多一处要维护的东西,而那个还带 Exp。

**代价说清楚:** 图不进上下文,所以同一个问题再问一次就得再调一次(重复那
667 token)。工具的说明里对模型明说了这一点,它想一次问全就会一次问全。

handler 够不着前端(agent.py 只传 **block.input),所以这次调用没有屏幕可打。

变量就叫 vision(跟 glob / grep 一样),于是 `import tools.vision as V` 拿到的是
包上被覆掉的那个属性 —— 要模块得走 importlib。同一个坑在 tools/memory.py
末尾写着,那边靠改名绕开,这边靠测试绕开。
"""

import base64

import anthropic

from agent import call_api, client, error_chain
from config import WORKDIR
from tools.base import ToolDesc

# 跟主 agent 同一个模型。换掉这一行就能让看图走另一个模型 —— 但先想清楚
# 为什么,见文件头。
MODEL = "deepseek-flash"

# 输出预算。**给宽一点是有原因的:** 这个模型先吐 thinking 块,而 thinking
# 也算在 max_tokens 里。实测过 max_tokens=100 时思考会把预算吃光,text 块
# 根本不生成、stop_reason 变成 max_tokens、拿回来一个空字符串 —— 而"图里
# 什么都没有"和"预算不够"对模型是两件完全不同的事。
MAX_TOKENS = 2000

# 文件大小上限。防的是"把一张 50 MB 的原图丢过来":base64 涨三分之一,
# 传得慢、还可能被端点拒。5 MB 够放手机照片和截图了。
MAX_BYTES = 5 * 1024 * 1024

# 认魔数,不看扩展名 —— `.png` 里是什么都能是。
_MAGIC = (
	(b"\x89PNG\r\n\x1a\n", "image/png"),
	(b"\xff\xd8\xff", "image/jpeg"),
	(b"GIF87a", "image/gif"),
	(b"GIF89a", "image/gif"),
)


def _media_type(raw: bytes) -> str | None:
	"""按魔数认图片类型。认不出返回 None。"""
	for magic, media in _MAGIC:
		if raw.startswith(magic):
			return media
	# WebP 的魔数是分开的:RIFF....WEBP
	if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
		return "image/webp"
	return None


def _quiet(event: dict) -> None:
	"""没有屏幕可打。

	代价说清楚:call_api 重试时那条 note 会被吞掉 —— 悄悄重试了,页面上看
	不出来。要让它可见,得让工具的 handler 也能拿到 emit,那是另一件事
	(同一句话在 tools/subagent.py 里也写着)。
	"""


def run_vision(path: str, question: str) -> str:
	question = str(question or "").strip()
	if not question:
		return ("Error: question is empty. Say what you want to know about "
		        "the image — it is not kept, so ask for what you need now.")

	# 越界由 permission_hook 管(它拿 block.input["path"] 问人),这儿只管读。
	full = (WORKDIR / path).resolve()
	try:
		raw = full.read_bytes()
	except OSError as e:
		return f"Error: cannot read {path!r}: {type(e).__name__}: {e}"

	media = _media_type(raw)
	if media is None:
		# 把读到的头几个字节给出去:模型才知道手里这是什么(常见的是它拿
		# 一个 .txt 或 .pdf 来试),而不是只被告知"不行"。
		return (f"Error: {path!r} is not an image this tool can read. Supported: "
		        f"PNG, JPEG, GIF, WebP. It starts with {raw[:8]!r}.")
	if len(raw) > MAX_BYTES:
		return (f"Error: {path!r} is {len(raw)} bytes, over the {MAX_BYTES} "
		        f"cap. Shrink it first (bash has the tools) and try again.")

	content = [
		{"type": "image",
		 "source": {"type": "base64", "media_type": media,
		            "data": base64.b64encode(raw).decode()}},
		{"type": "text", "text": question},
	]
	try:
		response = call_api(client, _quiet, stream=False, model=MODEL,
		                    # 跟主循环分开记账:图不进上下文,所以这一次的信息量
		                    # 跟它花的钱完全不成比例。混进 main 里,你会以为主循环
		                    # 贵 —— 而真正贵的地方一次都看不见。
		                    purpose="vision",
		                    max_tokens=MAX_TOKENS,
		                    messages=[{"role": "user", "content": content}])
	except anthropic.APIError as e:
		# 跟 agent.py 一样把根因串上。只报外层的话,"连接超时"和"证书不对"
		# 长得一模一样,而这两件事的处置完全不同。
		detail = f"{type(e).__name__}: {e}"
		chain = error_chain(e)
		if chain:
			detail += f" <- {chain}"
		return f"Error: vision call failed: {detail}"

	# 只取 text,跳过 thinking 块 —— 那是它的草稿,不是答案。
	text = "".join(b.text for b in response.content if b.type == "text").strip()
	if not text:
		# 空字符串**绝不能**当答案交回去:模型会读成"图里什么都没有"。
		if response.stop_reason == "max_tokens":
			return (f"Error: the model spent its whole {MAX_TOKENS}-token output "
			        f"budget on thinking and never wrote an answer. Ask a "
			        f"narrower question.")
		return "Error: the model returned nothing for this image."
	return text


vision = ToolDesc(
	name="vision",
	description=(
		"Look at an image file and answer a question about it. The image "
		"never enters your context — this tool reads it, has it looked at, "
		"and returns only the answer. So ask for what you actually need: "
		"reading text off a screenshot, describing a chart, checking a "
		"layout, identifying what something is.\n"
		"Because the image is not kept, asking about the same file again "
		"means calling this tool again and paying for it again. When one "
		"question can cover what you need, ask that one.\n"
		"Works on PNG, JPEG, GIF and WebP. For any other file use read_file."
	),
	input_schema={
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Path to the image file.",
			},
			"question": {
				"type": "string",
				"description": ("What you want to know about the image. Be "
				                "specific — the answer is all you get back."),
			},
		},
		"required": ["path", "question"],
	},
	handler=run_vision,
)
