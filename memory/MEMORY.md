pricing.py 的时段窗口由用户确认为:高峰 09:00–12:00、14:00–18:00(北京时间,左闭右开),其余全是空闲 —— 这是用户定的,不是从文档查来的。
测试用 `uv run pytest` 跑;tests/conftest.py 把 SessionStore 和 usage.USAGE_PATH 都重定向到临时目录,所以整套跑不会碰真实的 sessions.db 和 .traces/usage.jsonl。
