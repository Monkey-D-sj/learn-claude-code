pricing.py 的时段窗口由用户确认为:高峰 09:00–12:00、14:00–18:00(北京时间,左闭右开),其余全是空闲 —— 这是用户定的,不是从文档查来的。
测试用 `uv run pytest` 跑;tests/conftest.py 把 SessionStore 和 usage.USAGE_PATH 都重定向到临时目录,所以整套跑不会碰真实的 sessions.db 和 .traces/usage.jsonl。
frontend-custom/ 是用 Vite 手搭的 Vue 3 + TS + Element Plus todo 应用（npm install / npm run build 均通过），状态在 src/composables/useTodos.ts，持久化用 localStorage key「frontend-custom:todos」。
聊天界面是单文件前端 ui/index.html(无构建步骤);frontend-custom/ 是另一个独立的 Vue todo 应用,与聊天无关。滚动跟随逻辑在 ui/index.html 的 follow() 函数里。
agent.py 输出预算分两个常量：MAX_OUTPUT_TOKENS=80_000（流式，主循环）和 MAX_OUTPUT_TOKENS_NONSTREAM=20_000（子 agent 走的 stream=False 那条路）；SDK 对非流式有本地闸门，max_tokens 超 21,333（=600*128000/3600）就抛 ValueError 而不是发请求。主循环认 stop_reason=="max_tokens" 并返回 failed；截断那条 assistant 只进 record 不进 messages，避免留下没有 tool_result 的 tool_use 让下次提问 400。
