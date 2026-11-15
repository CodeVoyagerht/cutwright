"""后端基类与注册表。

一个后端就是「把 :class:`~cutwright.timeline.Timeline` 翻译成某样东西」：

    ffmpeg   → 一条 MP4
    jianying → 一份剪映草稿目录（可选用剪映自己的 DLL 加密）
    resolve  → 达芬奇里的一个时间线

写后端只需要继承 :class:`Backend`、实现 ``available()`` 与 ``build()``，然后
用 ``@register`` 登记。不要在后端里读分镜表——分镜表已经在 IR 里了。

**后端模块允许缺失**。``resolve`` 依赖达芬奇的脚本 API、``jianying`` 依赖剪映
的 DLL，用户可能只装了其中一个，甚至源码被裁剪过。所以这里的加载逻辑是
「尽力而为」：某个模块 import 失败就记进 :data:`IMPORT_ERRORS`，其余后端照常
可用，``doctor`` 会把它列成「不可用 + 原始错误」。这比整个包起不来好得多。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import config
from ..timeline import Timeline

__all__ = ["Backend", "BuildResult", "get_backend", "available_backends",
           "BACKENDS", "IMPORT_ERRORS", "register", "load_backends"]

# 各后端模块（相对本包）。顺序即 doctor 里的展示顺序。
_BACKEND_MODULES = ("ffmpeg", "jianying", "resolve")


@dataclass
class BuildResult:
    """一次后端构建的产物描述。``ok=False`` 时 ``error`` 说明原因。"""

    backend: str
    ok: bool = True
    artifacts: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend, "ok": self.ok,
            "artifacts": self.artifacts, "data": self.data,
            "error": self.error, "warnings": self.warnings,
        }


class Backend:
    """后端协议。子类覆写 ``name`` / ``available`` / ``build``。"""

    name = "base"
    description = ""

    def __init__(self, paths: Optional[config.Paths] = None,
                 options: Optional[Dict[str, Any]] = None):
        self.paths = paths or config.discover()
        self.options = options or {}

    # -- 能力 -------------------------------------------------------------- #
    def available(self) -> bool:
        """当前机器能不能用这个后端。纯只读检查。"""
        return True

    def why_unavailable(self) -> str:
        """``available()`` 为 False 时给用户看的原因，必须可操作。"""
        missing = self.paths.missing_for(self.name)
        if missing:
            return f"缺少 {', '.join(missing)}"
        return ""

    # -- 构建 -------------------------------------------------------------- #
    def build(self, timeline: Timeline, out_dir: str) -> BuildResult:
        raise NotImplementedError

    # -- 便利 -------------------------------------------------------------- #
    def ensure_out_dir(self, out_dir: str) -> str:
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def report(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "available": self.available(), "reason": self.why_unavailable()}


BACKENDS: Dict[str, type] = {}
IMPORT_ERRORS: Dict[str, str] = {}
_LOADED = False


def register(cls: type) -> type:
    """把后端类登记进注册表（也用作装饰器）。"""
    BACKENDS[cls.name] = cls
    return cls


def load_backends(force: bool = False) -> None:
    """导入所有后端模块，缺失的记进 :data:`IMPORT_ERRORS` 而不抛异常。"""
    global _LOADED
    if _LOADED and not force:
        return
    import importlib

    # 注意用 ``__package__``（= ``cutwright.backends``）而不是 ``__name__``
    # —— 本模块自己的名字是 ``cutwright.backends.base``，拿它拼相对名会变成
    # ``cutwright.backends.base.ffmpeg``，永远 import 不到。
    package = __package__ or "cutwright.backends"
    for module in _BACKEND_MODULES:
        before = set(BACKENDS)
        try:
            importlib.import_module(f"{package}.{module}")
        except Exception as exc:       # 依赖缺失、语法错误都要能看见
            IMPORT_ERRORS[module] = f"{type(exc).__name__}: {exc}"
            continue
        # import 成功却一个后端都没登记，等于「目录在、但没接线」：
        # 可能是缺 __init__.py（Python 会把目录当命名空间包静默导入），
        # 也可能是忘了 @register。这种情况必须报出来，否则 doctor 里
        # 它连一行都不出现，用户会以为后端不存在。
        if set(BACKENDS) == before:
            IMPORT_ERRORS[module] = (
                "模块可导入但没有登记任何后端"
                "（检查 __init__.py 是否缺失、或是否忘了 @register）")
    _LOADED = True


def get_backend(name: str, paths: Optional[config.Paths] = None,
                options: Optional[Dict[str, Any]] = None) -> Backend:
    """按名字取后端实例。

    模块 import 失败时给出**原始错误**，而不是一句「未知后端」——用户得知道
    是缺依赖还是版本不对。
    """
    load_backends()
    if name not in BACKENDS:
        hint = ""
        if name in IMPORT_ERRORS:
            hint = f"\n  后端模块 {name!r} 加载失败: {IMPORT_ERRORS[name]}"
        raise KeyError(f"未知后端 {name!r}，可用: {sorted(BACKENDS)}{hint}")
    return BACKENDS[name](paths=paths, options=options)


def available_backends(paths: Optional[config.Paths] = None) -> List[Dict[str, Any]]:
    """列出所有后端及其可用性（``cutwright doctor`` 用）。"""
    paths = paths or config.discover()
    load_backends()

    out: List[Dict[str, Any]] = []
    for name, cls in sorted(BACKENDS.items()):
        try:
            out.append(cls(paths=paths).report())
        except Exception as exc:       # 后端自己初始化就炸，也要能列出来
            out.append({"name": name, "description": "",
                        "available": False,
                        "reason": f"初始化失败: {type(exc).__name__}: {exc}"})
    for module, err in sorted(IMPORT_ERRORS.items()):
        if module not in BACKENDS:
            out.append({"name": module, "description": "",
                        "available": False, "reason": f"模块加载失败: {err}"})
    return out
