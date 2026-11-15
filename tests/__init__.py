"""测试包入口。

两件事：

1. 把 ``src/`` 放进 ``sys.path``，让测试在**不安装**的情况下也能跑；
2. 提供 :func:`tmpdir`，一个把临时目录建在仓库内 ``.tmp/`` 下的上下文管理器。

第 2 点不是洁癖：Windows 沙箱里 ``tempfile.TemporaryDirectory`` 清理阶段会对目录
做 ``chmod``，在受限令牌下会抛 ``PermissionError: [WinError 5]``，让一堆本来
通过的用例变成 error。建在自己的目录里、清理时 ``ignore_errors`` 就绕开了。

测试刻意不用 pytest —— 这个项目的卖点之一是「核心零第三方依赖」，
测试套件也应该 ``git clone`` 完就能跑：

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from contextlib import contextmanager

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

TMP_ROOT = os.path.join(_ROOT, ".tmp")


@contextmanager
def tmpdir(prefix: str = "t"):
    """建一个仓库内的临时目录，退出时尽力删除。"""
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, f"{prefix}-{uuid.uuid4().hex[:8]}")
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
