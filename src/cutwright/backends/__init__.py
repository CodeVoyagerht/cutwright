"""后端包。

导入本包不会导入任何具体后端——各后端按需引入，避免「只想用剪映却
被迫加载达芬奇依赖」的情况。用 :func:`get_backend` 拿实例。
"""
from .base import (  # noqa: F401
    BACKENDS,
    Backend,
    BuildResult,
    available_backends,
    get_backend,
    register,
)

__all__ = [
    "Backend", "BuildResult", "BACKENDS", "register",
    "get_backend", "available_backends",
]
