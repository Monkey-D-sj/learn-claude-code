from config import WORKDIR
from tools.base import ToolDesc
from tools.filelock import path_lock

# 版本指纹:写之前再看一眼"文件还是我读到的那份吗"。
#
# 为什么需要,既然上面已经有一把锁了:那把锁只管得住**本进程**(见
# tools/filelock.py 的边界那一段)。bash 里的 sed、你的编辑器、另一个 server
# 进程都可能在这个窗口里写进来,而我们的读改写是"读到内存里再整份写回" ——
# 拿旧内容盖上去,对方的改动就没了,而且我们返回的是 "Edited"。
#
# 指纹用 (大小 + mtime_ns) 而不是内容哈希:窗口只有读和写之间的几微秒,这两个
# 数任一个变了就足以说明"有人动过",而它不用把文件再读一遍。代价说清楚:它
# **收窄**窗口,关不上(真要关上得用 OS 级文件锁,那是另一件事)。
def _fingerprint(path) -> tuple[int, int]:
	st = path.stat()
	return (st.st_size, st.st_mtime_ns)


def _edit_inside_lock(file_path, name: str, old_string: str, new_string: str) -> str:
	"""file_path 是**解析过的**、真正要动的那个文件;name 是回给模型的那个写法。

	两个参数分开,是因为锁的键必须规范化(同一个文件的不同写法要落到同一把锁,
	见 tools/filelock.py),而报文里该出现的是**模型自己给的那个相对路径** ——
	它手里的 read_file / bash 都从 WORKDIR 出发,回一句它没见过的绝对路径只是
	噪声,还长得像另一个文件。
	"""
	text = file_path.read_text(encoding="utf-8")
	before = _fingerprint(file_path)
	n = text.count(old_string)
	if n == 0:
		return f"Error: text not found in {name}"
	if n > 1:
		return f"Error: text appears {n} times in {name}; make it unique"
	if _fingerprint(file_path) != before:
		return (f"Error: {name} changed while this edit was being prepared "
		        f"(someone else wrote it) — nothing was written. Read it again "
		        f"and retry.")
	file_path.write_text(text.replace(old_string, new_string), encoding="utf-8")
	return f"Edited {name}"


def run_edit(path: str, old_string: str, new_string: str) -> str:
	try:
		# 读—判断—写整段在一把**按路径**的锁里(见 tools/filelock.py):两个会话
		# 同时改一个文件时,不加锁的话两边都读到同一份旧内容、各自整份写回,后写
		# 的那个把先写的改动盖掉 —— 而两次调用都返回 "Edited"。
		#
		# resolve() 只用在锁和读写上,path 这个原始写法一路带到报文里。
		target = WORKDIR / path
		with path_lock(target):
			return _edit_inside_lock(target.resolve(), path, old_string, new_string)
	except Exception as e:
		return f"Error: {e}"


edit_file = ToolDesc(
	name="edit_file",
	description=(
		"Replace old_string with new_string in a file. "
		"old_string must appear exactly once, otherwise nothing changes."
	),
	input_schema={
		"type": "object",
		"properties": {
			"path": {
				"type": "string",
				"description": "Path to the file to edit.",
			},
			"old_string": {
				"type": "string",
				"description": "Exact text to replace. Must be unique in the file.",
			},
			"new_string": {
				"type": "string",
				"description": "Text to replace it with.",
			},
		},
		"required": ["path", "old_string", "new_string"],
	},
	handler=run_edit,
)
