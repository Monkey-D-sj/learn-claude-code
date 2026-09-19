from config import WORKDIR

# 减速带,不是边界。
#
# shell 图灵完备,任何静态子串匹配都能被重新编码绕过:rm -fr /、s'u'do、
# sh -c '...'、base64 -d | sh、python -c "shutil.rmtree(...)"。实测 15 条
# 危险命令只拦得住 3 条,其中 2 条还是巧合(字面上凑出了 "rm -rf /");
# 同时 4 条正常命令误伤 3 条(grep -rn 'reboot' 之类)。
#
# 所以:别指望它,也别再加词条 —— 加词条误伤涨得比拦截快。它挡的是手滑
# (模型原样吐出 rm -rf /),不是决心。真边界只能下沉到 OS(单独账户 /
# 容器 / VM)。现有这几条要不要精简,是另一个决定,没动。
DENY_LIST = [
	"rm -rf /", "sudo", "shutdown", "reboot",
	"mkfs", "dd if=", "> /dev/sda",
]

FILE_TOOLS = ("read_file", "write_file", "edit_file")


def permission_hook(block, ask):
	"""PreToolUse:决定这次调用放不放行,返回字符串 = 拦下,None = 放行。

	ask 是注入进来的确认器,签名 ask(question: str) -> bool。这里绝不能直接
	调 input():hooks/__init__.py 是全局注册,server.py 的 _run_turn 走的是
	同一个 agent_loop,而浏览器那边没有 stdin —— 要么把 HTTP 线程卡死在
	input() 上(那一轮还攥着它那个会话的锁,于是这个会话的后续请求全是
	409),要么抛 EOFError 被当成工具错误回给前端。两个都不该发生。

	(这里本来写的是"BUSY 还被占着"。BUSY 是当初那把全局锁,现在已经是
	每个会话一把了 —— 卡住的后果从"整个服务 409"缩小到"这个会话 409"。)
	"""
	if block.name == "bash":
		command = block.input.get("command", "")
		for pattern in DENY_LIST:
			if pattern in command:
				# 理由是说给模型听的,不是给你听的:它会照着自己改道,而不是
				# 换个写法反复撞同一堵墙。所以带上命中的是哪条。
				return (f"blocked: matches never-allowed pattern {pattern!r}. "
				        f"That construct has no safe use here — pick a "
				        f"different approach rather than rephrasing this one.")
	if block.name in FILE_TOOLS:
		path = block.input.get("path", "")
		if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
			# 越界不硬拒,交给人判 —— 通用 agent 本来就要能够到外面,
			# 问题只在"要不要现在这个动作",而这个问题只有人答得了。
			if not ask(f"{block.name} {path!r} is outside {WORKDIR}"):
				return "denied by user"
	return None
