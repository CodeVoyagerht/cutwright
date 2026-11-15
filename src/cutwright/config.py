"""外部程序与目录的发现逻辑。

cutwright 本身零第三方依赖，但它要调用别人的东西：``ffmpeg`` 做渲染、
剪映的 ``videoeditor.dll`` 做草稿加解密、达芬奇的脚本 API 建时间线。

这个模块的唯一职责是「把散落在系统各处的这些路径找出来」，规则统一为：

    **环境变量 > PATH/标准安装位置 > 已知的随包位置**

显式传参永远是最高优先级（CLI 的 ``--ffmpeg``）。所有发现函数都返回
``Optional``，找不到就是 ``None``，绝不抛异常——真正的报错应该发生在
「用户确实要用它」的那一刻，由后端给出可操作的提示。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = ["Paths", "discover", "which", "IS_WINDOWS", "IS_MACOS"]

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# 剪映 / CapCut 的草稿根目录候选（相对用户目录）
_JY_DRAFT_HINTS = [
    r"JianyingPro\User Data\Projects\com.lveditor.draft",
    r"JianyingPro\User Data\Projects",
    r"CapCut\User Data\Projects\com.lveditor.draft",
    r"CapCut\User Data\Projects",
]

# 剪映安装目录候选（含自带的 ffmpeg.exe，能省一次下载）
_JY_INSTALL_HINTS = [
    r"JianyingPro\Apps",
    r"CapCut\Apps",
]

# ffmpeg 的已知随包位置：剪辑软件、以及其他工具自带的副本
_FFMPEG_HINTS_WIN = [
    r"D:\AutoClip Desktop\resources\ffmpeg\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
]
_FFMPEG_HINTS_POSIX = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]


@dataclass
class Paths:
    """一次发现的结果。所有字段都可能为 None。"""

    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None
    # 剪映
    jianying_install: Optional[str] = None      # 含 videoeditor.dll 的版本目录
    jianying_drafts: Optional[str] = None       # 草稿根目录（含 root_meta_info.json）
    # 达芬奇
    resolve_support: Optional[str] = None       # ...\DaVinci Resolve\Support
    resolve_scripting: Optional[str] = None     # ...\Support\Developer\Scripting
    # 来源说明，便于 doctor 解释「这个路径是怎么来的」
    sources: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "jianying_install": self.jianying_install,
            "jianying_drafts": self.jianying_drafts,
            "resolve_support": self.resolve_support,
            "resolve_scripting": self.resolve_scripting,
            "sources": dict(self.sources),
        }

    def missing_for(self, backend: str) -> List[str]:
        """某个后端还缺什么，用于给出可操作的错误信息。"""
        need: Dict[str, List[str]] = {
            "ffmpeg": ["ffmpeg", "ffprobe"],
            "jianying": ["jianying_drafts"],
            "resolve": ["resolve_support"],
        }
        return [k for k in need.get(backend, []) if not getattr(self, k, None)]


def which(name: str) -> Optional[str]:
    """``shutil.which`` 的薄封装，额外处理 Windows 上 ``ffmpeg.exe`` 的写法。"""
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS and not name.lower().endswith(".exe"):
        return shutil.which(name + ".exe")
    return None


def _first_existing(candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _discover_ffmpeg(paths: Paths) -> None:
    env = os.environ.get("CUTWRIGHT_FFMPEG")
    if env and os.path.exists(env):
        paths.ffmpeg = env
        paths.sources["ffmpeg"] = "环境变量 CUTWRIGHT_FFMPEG"
    else:
        found = which("ffmpeg")
        if found:
            paths.ffmpeg = found
            paths.sources["ffmpeg"] = "PATH"
        else:
            hints = _FFMPEG_HINTS_WIN if IS_WINDOWS else _FFMPEG_HINTS_POSIX
            hints = list(hints)
            # 剪映自带的 ffmpeg 也能用，但**排在最后**：它是给剪映自己打包的，
            # 滤镜集不保证齐全（实测 11.7 自带的那份 `-version` 报
            # `1.0.0-401a53202a`，且不保证带 zscale/zoompan）。先试已知完整构建。
            jy = _find_jianying_install()
            if jy:
                hints.append(os.path.join(jy, "ffmpeg.exe"))
            found = _first_existing(hints)
            if found:
                paths.ffmpeg = found
                paths.sources["ffmpeg"] = "已知位置"

    if paths.ffmpeg:
        # ffprobe 通常和 ffmpeg 同目录
        sibling = os.path.join(os.path.dirname(paths.ffmpeg),
                               "ffprobe.exe" if IS_WINDOWS else "ffprobe")
        if os.path.exists(sibling):
            paths.ffprobe = sibling
            paths.sources["ffprobe"] = "与 ffmpeg 同目录"
    if not paths.ffprobe:
        found = which("ffprobe")
        if found:
            paths.ffprobe = found
            paths.sources["ffprobe"] = "PATH"


def _find_jianying_install() -> Optional[str]:
    """找到含 ``videoeditor.dll`` 的剪映版本目录（多版本时取版本号最大的）。"""
    roots: List[str] = []
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    for hint in _JY_INSTALL_HINTS:
        roots.append(os.path.join(local, hint))
    for root in roots:
        if not os.path.isdir(root):
            continue
        if os.path.exists(os.path.join(root, "videoeditor.dll")):
            return root
        versions = [d for d in _listdir(root)
                    if os.path.exists(os.path.join(root, d, "videoeditor.dll"))]
        if versions:
            versions.sort(key=_version_key)
            return os.path.join(root, versions[-1])
    return None


def _discover_jianying(paths: Paths) -> None:
    paths.jianying_install = _find_jianying_install()
    if paths.jianying_install:
        paths.sources["jianying_install"] = "剪映安装目录"

    env = os.environ.get("CUTWRIGHT_JY_DRAFTS")
    if env and os.path.isdir(env):
        paths.jianying_drafts = env
        paths.sources["jianying_drafts"] = "环境变量 CUTWRIGHT_JY_DRAFTS"
        return

    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    candidates = [os.path.join(local, h) for h in _JY_DRAFT_HINTS]
    # 自定义草稿目录：谁有 root_meta_info.json 谁是草稿根
    for drive in ("C:", "D:", "E:"):
        candidates.append(os.path.join(drive + os.sep, "Jianying", "JianyingPro Drafts"))
    found = None
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "root_meta_info.json")):
            found = c
            break
    if found:
        paths.jianying_drafts = found
        paths.sources["jianying_drafts"] = "发现 root_meta_info.json"


def _discover_resolve(paths: Paths) -> None:
    env = os.environ.get("RESOLVE_SCRIPT_API")
    support = None
    if env and os.path.isdir(env):
        # RESOLVE_SCRIPT_API 指向 ...\Support\Developer\Scripting
        support = os.path.normpath(os.path.join(env, "..", "..", ".."))
    if not support:
        candidates: List[str] = []
        if IS_WINDOWS:
            progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            candidates.append(os.path.join(
                progdata, "Blackmagic Design", "DaVinci Resolve", "Support"))
        elif IS_MACOS:
            candidates.append("/Library/Application Support/Blackmagic Design/"
                              "DaVinci Resolve")
        else:
            candidates.append("/opt/resolve")
        support = _first_existing(candidates)
    if support:
        paths.resolve_support = support
        paths.sources["resolve_support"] = "标准安装位置"
        scripting = os.path.join(support, "Developer", "Scripting")
        if os.path.isdir(scripting):
            paths.resolve_scripting = scripting
            paths.sources["resolve_scripting"] = "Support/Developer/Scripting"


def discover() -> Paths:
    """跑一次完整发现。纯只读，不会创建任何文件。"""
    paths = Paths()
    _discover_ffmpeg(paths)
    _discover_jianying(paths)
    _discover_resolve(paths)
    return paths


def _listdir(path: str) -> List[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _version_key(name: str):
    """把 ``11.6.0.14552`` 这类目录名变成可比较的元组。"""
    parts = []
    for chunk in name.replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])
