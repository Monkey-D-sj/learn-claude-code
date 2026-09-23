pricing.py 的时段窗口由用户确认为:高峰 09:00–12:00、14:00–18:00(北京时间,左闭右开),其余全是空闲 —— 这是用户定的,不是从文档查来的。
测试用 `uv run pytest` 跑;tests/conftest.py 把 SessionStore 和 usage.USAGE_PATH 都重定向到临时目录,所以整套跑不会碰真实的 sessions.db 和 .traces/usage.jsonl。
frontend-custom/ 是用 Vite 手搭的 Vue 3 + TS + Element Plus todo 应用（npm install / npm run build 均通过），状态在 src/composables/useTodos.ts，持久化用 localStorage key「frontend-custom:todos」。
聊天界面是单文件前端 ui/index.html(无构建步骤);frontend-custom/ 是另一个独立的 Vue todo 应用,与聊天无关。滚动跟随逻辑在 ui/index.html 的 follow() 函数里。
agent.py 输出预算分两个常量：MAX_OUTPUT_TOKENS=80_000（流式，主循环）和 MAX_OUTPUT_TOKENS_NONSTREAM=20_000（子 agent 走的 stream=False 那条路）；SDK 对非流式有本地闸门，max_tokens 超 21,333（=600*128000/3600）就抛 ValueError 而不是发请求。主循环认 stop_reason=="max_tokens" 并返回 failed；截断那条 assistant 只进 record 不进 messages，避免留下没有 tool_result 的 tool_use 让下次提问 400。
context.compress_range 的回执末尾会再带一份摘要正文(context.py 里 `return f"{report}\n\n摘要:{summary}"`)——看着像重复,其实是给前端看的:工具结果在 ui/index.html 里是折叠的 details,折叠时只露 headline() 的头两行非空行,不带上就看不到压掉了什么。
sessions.db schema 已到 v4:tool_execs 表(两阶段标记)+ session_contexts.checkpoint_turn_id/covered_message_no/runtime_json + turns.status 收到 'interrupted'(带 interrupt_reason、model_rounds_started);v4 那条迁移要关外键跑(NEEDS_FK_OFF),见 skills/sqlite-check-rebuild-migration。
checkpoint 入口:POST /session/{sid}/turn/{tid}/resume|abandon(带 session_contexts.version),GET 同路径 /review 看能否续跑;有未处理的中断轮次时 POST /ask 回 409 挡住新提问。session_contexts.version 就是"快照版本"。
server.send_error 的第二段是 HTTP 状态行的 reason phrase,按 latin-1 编码 —— 消息里带中文会让响应根本发不出去(浏览器看到"连接被断开")。给页面的人话要走页面自己那侧。
agent_loop 的 record 回调签名是 record(kind, role, content, tool_use_id=None);tests 里的 record 替身都要收这个 kwarg,否则一跑到工具结果就 TypeError(interface 改过,替身没跟)。
