"""按内容搜文件。

跟 glob 是一对:glob 按**文件名**找,这个按**文件里写了什么**找。模型
第一件想做的事往往是后一种("这东西在哪"),而之前只有 glob。

**为什么是纯 Python 而不是 shell 出去给 grep:** 决定因素不是快慢,是
**结果集能不能用**。实测这个仓库上 `grep -rn "def "` 出来 15462 行、1.6 MB
—— 它钻进 .venv / .git,直接撞破压缩器 30000 字符的落盘线,而模型要的那
几行埋在几千行依赖代码里。

	用标准库的 glob 模块(也就是 tools/glob.py 用的那个)走文件:它的 `**`
	跳过隐藏目录,所以 .venv / .git 天然出局。**注意不是 pathlib 的
	Path.glob** —— 那个的 `**` 会钻进隐藏目录,跟 glob.py 行为不一致(写这
	个工具时先踩了一次)。用同一个模块,两个工具的行为才是一致的**由构造
	保证**,而不是靠我在两边各写一遍过滤。

	而且能在**读的时候**就掐住:命中到 200 条就停手,不再读剩下的文件 ——
	先攒出 1.6 MB 再切,内存和延迟都已经付过了。宽搜索实测 0.04 秒。

	subprocess 还有个编码坑:默认按 locale 解(text=True 不指定 encoding),
	中文 Windows 上是 GBK,grep 吐出来的 UTF-8 会炸成 UnicodeDecodeError。
	bash.py 拿 encoding="utf-8", errors="replace" 挡住了,但那道闸得每个
	shell 出去的调用各写一遍,纯 Python 根本没有这个问题。
"""

import glob as globlib
import re
from pathlib import Path

from config import WORKDIR
from tools.base import ToolDesc

# 命中上限。跟 glob 的 200 对齐 —— 两个工具在"结果太多"时给模型的信号
# 应该长成同一个样子,不然它得记两套。
MAX_MATCHES = 200

# 单行截断。一行可能是一整条压缩过的 JSON 或者 minified 的代码,原样吐出去
# 会让 200 条命中的额度全花在一条上。
MAX_LINE_CHARS = 200


def _hits(path: Path, rx: re.Pattern, limit: int) -> list[str]:
	"""这个文件里命中的行,最多 limit 条。

	读不了就返回空。这是个**正常**结果(仓库里总有图片、编译产物、别的编码
	的文本),不是错误 —— 报错的话模型会以为"搜索失败了",然后换个写法重试,
	而正确的结论是"这些文件里没有"。
	"""
	try:
		text = path.read_text(encoding="utf-8")
	except (UnicodeDecodeError, OSError):
		return []

	rel = path.relative_to(WORKDIR).as_posix()
	out = []
	for no, line in enumerate(text.splitlines(), 1):
		if rx.search(line):
			out.append(f"{rel}:{no}: {line.strip()[:MAX_LINE_CHARS]}")
			if len(out) >= limit:
				break
	return out


def run_grep(pattern: str, include: str = "**/*") -> str:
	try:
		rx = re.compile(pattern)
	except re.error as e:
		return f"Error: bad regex {pattern!r}: {e}"

	# 多收一条只为了知道"到顶了没有" —— 正好 200 条和"还有更多"是两回事,
	# 跟 glob.py 一样用 > 判,不用 >=。
	matches: list[str] = []
	for match in sorted(globlib.glob(include, root_dir=WORKDIR, recursive=True)):
		path = WORKDIR / match
		if not path.is_file():
			continue
		# 包含检查:glob 出来的按理都在 WORKDIR 里,但 include 是模型给的,
		# 里面可以写 ".."。跟 glob.py 同一道闸,理由也一样。
		if not path.resolve().is_relative_to(WORKDIR):
			continue
		matches += _hits(path, rx, MAX_MATCHES + 1 - len(matches))
		if len(matches) > MAX_MATCHES:
			break

	if not matches:
		return "(no matches)"
	if len(matches) > MAX_MATCHES:
		matches = matches[:MAX_MATCHES]
		matches.append(f"... (more than {MAX_MATCHES} matches; narrow the "
		               f"pattern or the include)")
	return "\n".join(matches)


grep = ToolDesc(
	name="grep",
	description=(
		"Search file contents by regular expression. Returns matching lines as "
		"'path:line: text'. Use this to find where something is defined, "
		"referenced or configured; use glob instead when you only know the file "
		"name. Hidden files and directories are not searched."
	),
	input_schema={
		"type": "object",
		"properties": {
			"pattern": {
				"type": "string",
				"description": "Python regular expression to search for.",
			},
			"include": {
				"type": "string",
				"description": ("Optional glob limiting which files to search, "
				                "e.g. '*.py' or 'tools/**/*.py'. Defaults to "
				                "every file."),
			},
		},
		"required": ["pattern"],
	},
	handler=run_grep,
	# 只读:重发一次无害,所以不用两阶段标记(见 tools/base.py 的 side_effect)。
	side_effect=False,
)

