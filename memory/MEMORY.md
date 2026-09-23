测试用 `uv run pytest` 跑;tests/conftest.py 把 SessionStore 和 usage.USAGE_PATH 都重定向到临时目录,所以整套跑不会碰真实的 sessions.db 和 .traces/usage.jsonl。
