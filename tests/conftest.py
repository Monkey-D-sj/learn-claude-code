"""pytest 的公共前置。两个坑都在这儿填。

一、`import sessions` 得找得到。项目不是装成包的,tests/ 下也没有
    __init__.py,所以 pytest 只会把 tests/ 自己塞进 sys.path。仓库根得自己加。

二、**先换掉 SessionStore,再让 server 进来**。server.py 模块级有一句
    STORE = SessionStore(),而默认库路径在 sessions.py 里写死成
    Path(__file__).parent / "sessions.db",不读环境变量。不拦这一下,每次
    跑 pytest 都会开一次你正在用的那个库 —— WAL 不至于写坏,但库要是落后
    一个版本,测试进程会顺手把它迁移了。

    换的是 sessions 模块里 SessionStore 这个**名字**,而 server.py 写的是
    `from sessions import SessionStore` —— 导入时按名字取值,所以此刻换是
    生效的,生产代码一行不用动。

    不能改成 monkeypatch sessions.DB_PATH:默认参数在 def 那一刻就绑定了,
    之后再改那个常量对 path 的默认值没有任何影响。
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sessions  # noqa: E402

# 模块级执行 —— 必须赶在任何测试模块 import server 之前。
_SCRATCH = Path(tempfile.mkdtemp(prefix="agent-tests-"))
_RealStore = sessions.SessionStore


def _scratch_store(path=None):
	"""库路径换成临时的。给了 path 就照给(用例自己指定临时库)。"""
	return _RealStore(path or _SCRATCH / "server.db")


sessions.SessionStore = _scratch_store


@pytest.fixture(scope="session", autouse=True)
def _cleanup_scratch():
	yield
	shutil.rmtree(_SCRATCH, ignore_errors=True)
