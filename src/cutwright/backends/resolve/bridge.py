"""达芬奇 Resolve 脚本 API 的加载与薄封装。

这一层只做两件事：**把官方 Python 脚本模块加载起来**，以及**把散落在
系统各处的路径探测出来**。它不建时间线、不碰工程——那是
:mod:`cutwright.backends.resolve.timeline` 的事。

加载方式严格照官方文档（``Support/Developer/Scripting/README.txt``）::

    set RESOLVE_SCRIPT_API=...\\DaVinci Resolve\\Support\\Developer\\Scripting
    set RESOLVE_SCRIPT_LIB=...\\fusionscript.dll
    set PYTHONPATH=%RESOLVE_SCRIPT_API%\\Modules
    import DaVinciResolveScript as dvr
    resolve = dvr.scriptapp("Resolve")

本模块把后三步包进 :meth:`ResolveBridge.available` / :meth:`ResolveBridge.connect`，
并且**在 import 时不加载任何东西**——只有真的要用时才 ``import``，
这样 ``cutwright doctor`` 才敢在没装 Resolve 的机器上无痛跑一遍。

**为什么 ``available()`` 不 import、``connect()`` 先起子进程（本机实测，务必保留）**：
DaVinci Resolve 20.3 + Python 3.12，把 ``RESOLVE_SCRIPT_LIB`` 指向真实的
``fusionscript.dll`` 之后，在 **Resolve 没运行**时 ``import DaVinciResolveScript``
会让进程以 ``0xC0000005``（访问冲突）**直接退出**：没有 traceback、没有异常、
``finally`` 都不执行，宿主的 Python 进程连同它的输出一起消失。所以：

* 本模块**从不自动设置** ``RESOLVE_SCRIPT_LIB``（只探测、只提示）；
* :meth:`ResolveBridge.available` 改成**看部件齐不齐**，绝不 import；
* :meth:`ResolveBridge.connect` 在父进程 import 之前，先在**子进程**里试一遍
  ``import + scriptapp``。子进程崩掉只是一个非 0 退出码，可以翻译成
  「Resolve 没在运行，先启动它」，而不是让 ``cutwright`` 自己死掉。

未验证项（写代码时无从确认，标出来免得后人当成已验证事实）：

* ``Resolve.GetProductName()`` / ``GetProductVersion()`` 的确切返回串。
  免费版与 Studio 的产品名差异是按「包含 Studio 子串」猜的。
* macOS / Linux 上 ``fusionscript.dylib`` / ``.so`` 的确切位置——目录取自
  官方 ``DaVinciResolveScript.py`` 的默认查找逻辑，只在 Windows 上实测过。
* Resolve 21.1+ 自带 Python 解释器的目录布局（``install/ResolvePython``）
  只是兜底线索，本模块从不去启动它。
* 「Resolve 在运行 时 import 就一定安全」是按探测结果推断的，没有对每个
  大版本都做过压力验证；万一 ``connect()`` 之后的父进程 import 仍然崩，
  那说明这台机器上库与解释器不匹配，只能用 Resolve 自带的解释器跑。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from ...config import IS_MACOS, IS_WINDOWS, Paths, discover

__all__ = [
    "ResolveError",
    "ResolveBridge",
    "parse_version",
    "at_least",
    "reset_cache",
]


class ResolveError(RuntimeError):
    """达芬奇脚本 API 的任何失败。

    ``hint`` 是**给用户看的下一步动作**，会被拼进 ``str(exc)``——后端把
    异常原样塞进 ``BuildResult.error`` 时，用户就能直接照着做，而不是看到
    一句「连接失败」。
    """

    def __init__(self, message: str, hint: str = "") -> None:
        self.hint = hint
        super().__init__(f"{message}\n提示: {hint}" if hint else message)


# --------------------------------------------------------------------------- #
# 可操作的提示语。集中在一处，改一次全站受益。
# --------------------------------------------------------------------------- #
HINT_INSTALL = (
    "1) 确认装的是 DaVinci Resolve Studio（免费版只有应用内脚本，外部 Python 调不到）；"
    "2) Resolve 的 Preferences > System > General 里把 External scripting using 设为 "
    "Local（或 Network），免费版没有这一项；"
    "3) 设置环境变量 RESOLVE_SCRIPT_API 指向 ...\\DaVinci Resolve\\Support\\Developer\\Scripting，"
    "RESOLVE_SCRIPT_LIB 指向 ...\\fusionscript.dll；"
    "4) 确认 DaVinci Resolve 正在运行、并且已经打开一个工程。"
)

HINT_NOT_RUNNING = (
    "scriptapp('Resolve') 拿到空值，通常是这三种情况："
    "① Resolve 没在运行（先手动启动它，cutwright 不会替你启动）；"
    "② 装的是免费版，外部脚本被禁用；"
    "③ 是 Studio 但 Preferences > System > General 里 External scripting using 为 None。"
)

HINT_CRASH = (
    "cutwright 在子进程里试了一把脚本 API，子进程直接崩了——"
    "在 Resolve 没运行时 import fusionscript 会以 0xC0000005 访问冲突退出，"
    "父进程里根本抓不到异常，所以这是唯一安全的表达方式。"
    "① 先手动启动 DaVinci Resolve 并打开一个工程，再重跑；"
    "② 如果它明明开着还是崩，检查 RESOLVE_SCRIPT_LIB 是不是指到了不匹配的"
    "库（应该指向 Resolve 安装目录下的 fusionscript.dll）；"
    "③ 换 Resolve 自带的 ResolvePython.exe 跑一遍，能区分是环境问题还是"
    "解释器版本问题。"
)

# --------------------------------------------------------------------------- #
# 默认安装位置。config.discover() 里有一份同样的推断；这里独立一份是因为
# bridge 要在「config 没探测到」时自己兜底（例如只跑单元测试的场景）。
# --------------------------------------------------------------------------- #
_DEFAULT_SUPPORT: Dict[str, str] = {
    "win": r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support",
    "mac": "/Library/Application Support/Blackmagic Design/DaVinci Resolve",
    "linux": "/opt/resolve",
}

# 应用本体（fusionscript.dll 通常在应用目录，而不是 ProgramData）。
_WIN_APP_HINTS: List[str] = [
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve",
    r"C:\Program Files (x86)\Blackmagic Design\DaVinci Resolve",
]

# 应用目录不在默认位置时（本机就装在 D:\DaVinci Resolve），按盘符兜底探测。
# 只探这几个常见盘：遍历全部盘符在挂载了网络盘 / 断开映射盘的机器上会卡住。
_WIN_DRIVE_LETTERS: Tuple[str, ...] = ("C", "D", "E", "F")
_WIN_APP_SUBDIRS: Tuple[str, ...] = (
    r"Blackmagic Design\DaVinci Resolve",
    "DaVinci Resolve",
)

# Resolve 21.1+ 自带的 Python 解释器（未验证，仅作为兜底线索与提示）。
_BUNDLED_PYTHON_REL = ("install", "ResolvePython", "ResolvePython.exe")

# 库文件名与「库所在目录」的候选。三个平台的路径取自官方
# DaVinciResolveScript.py 里的默认查找逻辑（mac 上扩展名确实是 .so），
# 但只在本机 Windows 上实测过。
_LIB_NAMES = {"win": ("fusionscript.dll",),
              "mac": ("fusionscript.so", "fusionscript.dylib"),
              "linux": ("fusionscript.so",)}
_LIB_DIRS = {
    "mac": ["/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion"],
    "linux": ["/opt/resolve/libs/Fusion", "/opt/resolve/bin"],
}

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# 子进程预检脚本。刻意写得像 1990 年的 C：只用 print + SystemExit，不依赖
# f-string（万一用户用 Resolve 自带的旧解释器跑）、不做任何花哨的收尾，
# 因为这个进程有可能在 import 那一行就没了。
_PROBE_CODE = (
    "import sys\n"
    "try:\n"
    "    import DaVinciResolveScript as dvr\n"
    "except BaseException as exc:\n"
    "    print('IMPORT_FAILED %s: %s' % (type(exc).__name__, exc))\n"
    "    raise SystemExit(3)\n"
    "print('IMPORT_OK')\n"
    "try:\n"
    "    print('CONNECT_OK' if dvr.scriptapp('Resolve') else 'CONNECT_EMPTY')\n"
    "except BaseException as exc:\n"
    "    print('SCRIPTAPP_FAILED %s: %s' % (type(exc).__name__, exc))\n"
)

# 预检子进程的超时。第一次 import 要加载几十 MB 的 dll，给足 25 秒；
# 超时按「连不上」处理，不去猜。
_PROBE_TIMEOUT = 25.0

# import 结果缓存：成功才缓存，失败每次都重试（装完 Resolve 不用重开进程）。
_DVR: Any = None


def reset_cache() -> None:
    """清空 ``DaVinciResolveScript`` 的 import 缓存。

    给两个场景用：单元测试之间互相隔离；以及用户「刚装好/刚启动 Resolve，
    再试一次」而进程还活着的时候。
    """
    global _DVR
    _DVR = None


def _first_line_with(text: str, needle: str) -> str:
    """返回含 ``needle`` 的第一行（去掉标记本身）；没有就返回空串。"""
    for line in text.splitlines():
        if needle in line:
            return line.replace(needle, "").strip(" :：")[:300]
    return ""


def _platform_key() -> str:
    if IS_WINDOWS:
        return "win"
    if IS_MACOS:
        return "mac"
    return "linux"


def _default_support_dir() -> Optional[str]:
    """标准安装位置里确实存在的那个 Support 目录，没有则 ``None``。"""
    candidate = _DEFAULT_SUPPORT.get(_platform_key())
    if candidate and os.path.isdir(candidate):
        return candidate
    return None


def _first_file(candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def _first_dir(candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return None


def _registry_app_dirs() -> List[str]:
    """从注册表读 Resolve 的安装目录；读不到就安静地返回空列表。

    本机的 ``HKLM\\SOFTWARE\\Blackmagic Design\\DaVinci Resolve`` 只有
    ``Version`` 没有安装目录，所以这只是一条**额外**线索：有就用，没有就继续
    按盘符探测。非 Windows、沙箱禁读注册表、键不存在——全当没有，不让
    ``available()`` 因此抛异常。
    """
    dirs: List[str] = []
    try:
        import winreg  # noqa: PLC0415 —— 只有 Windows 有，延迟到函数内
    except ImportError:
        return dirs
    keys = (r"SOFTWARE\Blackmagic Design\DaVinci Resolve",
            r"SOFTWARE\WOW6432Node\Blackmagic Design\DaVinci Resolve")
    value_names = ("InstallDir", "InstallPath", "AppPath", "Path", "")
    for path in keys:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, path) as handle:
                    for name in value_names:
                        try:
                            raw, _ = winreg.QueryValueEx(handle, name)
                        except OSError:
                            continue
                        text = str(raw or "").strip().strip('"')
                        if not text:
                            continue
                        candidate = text if os.path.isdir(text) else os.path.dirname(text)
                        if candidate and candidate not in dirs:
                            dirs.append(candidate)
            except OSError:
                continue
    return dirs


# --------------------------------------------------------------------------- #
# 版本号工具
# --------------------------------------------------------------------------- #
def parse_version(text: Any) -> Optional[Tuple[int, ...]]:
    """把 ``"DaVinci Resolve Studio 20.3.1"`` 解析成 ``(20, 3, 1)``。

    取不到任何数字就返回 ``None``——调用方必须把 ``None`` 当作「版本未知」，
    而不是当作 0。这一点在 LUT 写法的版本分支里很关键：19.0.0 之后
    ``TimelineItem.SetLUT()`` 被移除，换成 ``Item.GetNodeGraph().SetLUT()``。
    """
    if text is None:
        return None
    s = str(text)
    match = _VERSION_RE.search(s)
    if match:
        return tuple(int(p) for p in match.group(0).split("."))
    single = re.search(r"\d+", s)
    if single:
        return (int(single.group(0)),)
    return None


def at_least(version: Optional[Tuple[int, ...]],
             target: Tuple[int, ...]) -> bool:
    """``version >= target``，版本未知一律返回 ``False``。"""
    if not version:
        return False
    padded = tuple(version) + (0,) * max(0, len(target) - len(version))
    return padded[:len(target)] >= target


# --------------------------------------------------------------------------- #
class ResolveBridge:
    """脚本 API 的加载器 + 路径探测。**不持有任何工程状态。**"""

    def __init__(self, paths: Optional[Paths] = None) -> None:
        self.paths: Paths = paths or discover()
        # 最近一次准备环境时记下的东西（diagnose() 会用到）。
        self.env_info: Dict[str, Any] = {}
        # available() 失败时的原因，why_unavailable() 直接拿去给用户看。
        self.last_error: str = ""
        self._studio: Optional[bool] = None
        self._resolve: Any = None

    # -- 路径探测 ---------------------------------------------------------- #
    def support_dir(self) -> Optional[str]:
        """``...\\DaVinci Resolve\\Support``（含 ``LUT/`` 的那个目录）。"""
        if self.paths.resolve_support and os.path.isdir(self.paths.resolve_support):
            return self.paths.resolve_support
        env = os.environ.get("RESOLVE_SCRIPT_API")
        if env and os.path.isdir(env):
            # ...\Support\Developer\Scripting -> ...\Support
            derived = os.path.normpath(os.path.join(env, "..", "..", ".."))
            if os.path.isdir(derived):
                return derived
        return _default_support_dir()

    def scripting_dir(self) -> Optional[str]:
        """``Support\\Developer\\Scripting``：``RESOLVE_SCRIPT_API`` 应指向这里。"""
        env = os.environ.get("RESOLVE_SCRIPT_API")
        if env and os.path.isdir(env):
            return env
        if self.paths.resolve_scripting and os.path.isdir(self.paths.resolve_scripting):
            return self.paths.resolve_scripting
        support = self.support_dir()
        if support:
            return _first_dir([os.path.join(support, "Developer", "Scripting")])
        return None

    def modules_dir(self) -> Optional[str]:
        """``Scripting\\Modules``：``DaVinciResolveScript.py`` 所在目录。"""
        scripting = self.scripting_dir()
        if not scripting:
            return None
        return _first_dir([os.path.join(scripting, "Modules")])

    def lut_dir(self) -> Optional[str]:
        """Resolve 的 LUT 目录。LUT 必须先出现在这里才可能被 ``SetLUT`` 认。

        目录不存在时返回 ``None``（不是「给你一个假路径」）——调用方据此
        走「跳过并 warning」而不是写到一个 Resolve 看不见的地方。
        """
        support = self.support_dir()
        if not support:
            return None
        return _first_dir([os.path.join(support, "LUT")])

    def fusionscript_lib(self) -> Optional[str]:
        """``fusionscript.dll`` 的位置（Windows 已验证；mac/Linux 未验证）。"""
        env = os.environ.get("RESOLVE_SCRIPT_LIB")
        if env and os.path.isfile(env):
            return env
        names = _LIB_NAMES.get(_platform_key(), ("fusionscript.dll",))
        candidates: List[str] = []
        for base in self._app_dirs():
            for name in names:
                candidates.append(os.path.join(base, name))
        support = self.support_dir()
        if support:
            for name in names:
                candidates.append(os.path.join(support, name))
        return _first_file(candidates)

    def bundled_python(self) -> Optional[str]:
        """Resolve 自带的 Python 解释器（21.1+，**未验证**）。

        只做探测与提示：``available()`` 在环境变量缺失时会把它写进诊断信息，
        告诉用户「也可以用 Resolve 自带的解释器跑」，但**绝不启动它**。
        """
        for base in self._app_dirs():
            candidate = os.path.join(base, *_BUNDLED_PYTHON_REL)
            if os.path.isfile(candidate):
                return candidate
        return None

    def _app_dirs(self) -> List[str]:
        """应用本体目录候选。

        顺序：``RESOLVE_SCRIPT_LIB`` 反推 → 安装注册表 → 默认安装位置 →
        各盘符下的非默认安装位置（本机就装在 ``D:\\DaVinci Resolve``）。
        官方加载器只认 ``RESOLVE_SCRIPT_LIB`` 和 ``C:\\Program Files\\...``，
        放在别的盘上时它会直接失败，所以这里替用户把它找出来。
        """
        dirs: List[str] = []
        env = os.environ.get("RESOLVE_SCRIPT_LIB")
        if env:
            dirs.append(os.path.dirname(env))
        if IS_WINDOWS:
            dirs.extend(_WIN_APP_HINTS)
            dirs.extend(_registry_app_dirs())
            for letter in _WIN_DRIVE_LETTERS:
                for sub in _WIN_APP_SUBDIRS:
                    dirs.append(os.path.join(f"{letter}:\\", sub))
        else:
            dirs.extend(_LIB_DIRS.get(_platform_key(), []))
        out: List[str] = []
        seen: set = set()
        for directory in dirs:
            key = os.path.normcase(directory)
            if key in seen or not os.path.isdir(directory):
                continue
            seen.add(key)
            out.append(directory)
        return out

    # -- 环境准备 ---------------------------------------------------------- #
    def prepare_env(self) -> Dict[str, Any]:
        """补齐 ``RESOLVE_SCRIPT_API`` 并把 Modules 塞进 ``sys.path``。

        幂等、不抛异常（缺什么记在返回值里）。真正的失败留给
        :meth:`available` / :meth:`connect` 去表达——那里能给出可操作的话。

        **刻意不自动设置 ``RESOLVE_SCRIPT_LIB``**：见模块 docstring 里那段
        ``0xC0000005`` 实测。库路径会探测出来放进 ``info["lib"]`` 供诊断，
        但要不要写进环境变量由用户决定——因为一旦写上、Resolve 又没在运行，
        下一次 ``import`` 就会把进程打崩。``RESOLVE_SCRIPT_API`` 没有这个
        副作用（它只是目录），所以照旧自动补。
        """
        info: Dict[str, Any] = {"scripting": None, "lib": None,
                                "modules": None, "added_sys_path": False,
                                "notes": []}
        scripting = self.scripting_dir()
        if scripting:
            info["scripting"] = scripting
            if not os.environ.get("RESOLVE_SCRIPT_API"):
                os.environ["RESOLVE_SCRIPT_API"] = scripting
                info["notes"].append(f"已自动设置 RESOLVE_SCRIPT_API={scripting}")
            modules = self.modules_dir()
            if modules:
                info["modules"] = modules
                if modules not in sys.path:
                    sys.path.insert(0, modules)
                    info["added_sys_path"] = True
            else:
                info["notes"].append(
                    f"{os.path.join(scripting, 'Modules')} 不存在，脚本模块可能没装全")
        lib = self.fusionscript_lib()
        if lib:
            info["lib"] = lib
            if os.environ.get("RESOLVE_SCRIPT_LIB"):
                info["notes"].append(f"RESOLVE_SCRIPT_LIB 已由环境变量给出：{lib}")
            else:
                info["notes"].append(
                    f"探测到 fusionscript 库：{lib}（**不自动注入**：Resolve 没在运行时"
                    "注入它会让 import 崩溃；需要时请自己设置 RESOLVE_SCRIPT_LIB）")
        self.env_info = info
        return info

    def diagnose(self) -> Dict[str, Any]:
        """一次性列出所有探测结果，给 ``why_unavailable()`` 与 ``doctor`` 用。"""
        bundled = self.bundled_python()
        info = self.prepare_env()
        return {
            "support": self.support_dir(),
            "scripting": info["scripting"],
            "modules": info["modules"],
            "lib": info["lib"],
            "lut_dir": self.lut_dir(),
            "bundled_python": bundled,
            "env_api": os.environ.get("RESOLVE_SCRIPT_API"),
            "env_lib": os.environ.get("RESOLVE_SCRIPT_LIB"),
            "notes": info["notes"],
            "last_error": self.last_error,
        }

    # -- 加载 -------------------------------------------------------------- #
    def _load_dvr(self) -> Any:
        """``import DaVinciResolveScript``（成功才缓存）。失败抛 :class:`ResolveError`。"""
        global _DVR
        if _DVR is not None:
            return _DVR

        info = self.prepare_env()
        if not info["scripting"]:
            raise ResolveError(
                "找不到 DaVinci Resolve 的脚本 API 目录"
                "（Support\\Developer\\Scripting）",
                hint=HINT_INSTALL,
            )
        if not info["modules"]:
            raise ResolveError(
                f"脚本 API 目录存在但缺少 Modules 子目录：{info['scripting']}",
                hint=HINT_INSTALL,
            )
        try:
            import DaVinciResolveScript as dvr  # noqa: WPS433 —— 必须在函数内延迟加载
        except Exception as exc:  # ImportError / OSError 都可能（底层要加载 dll）
            raise ResolveError(
                f"import DaVinciResolveScript 失败：{type(exc).__name__}: {exc}",
                hint=HINT_INSTALL,
            ) from exc
        _DVR = dvr
        self.last_error = ""
        return dvr

    def available(self) -> bool:
        """脚本 API 的**部件**是否齐备（这里刻意不 import）。

        检查顺序：已经被谁 import 过 → Scripting 目录 → ``Modules`` 子目录 →
        ``Modules\\DaVinciResolveScript.py`` → 能找到 ``fusionscript`` 库。
        缺哪一步就把可操作的原因写进 :attr:`last_error`。

        与「能 import 就 True」的差别是有意的：本机实测在 Resolve 没运行时
        import ``fusionscript`` 会让进程以 ``0xC0000005`` 消失，``cutwright
        doctor`` 只要敢 import 一次就可能整条命令崩掉。部件齐备已经足以区分
        「没装 / 装错」和「装了但没开」，剩下那半句留给 :meth:`connect`
        用子进程预检去回答。
        """
        if _DVR is not None or "DaVinciResolveScript" in sys.modules:
            self.last_error = ""
            return True
        info = self.prepare_env()
        if not info["scripting"]:
            self.last_error = (
                "找不到 DaVinci Resolve 的脚本 API 目录（Support\\Developer\\Scripting）")
            return False
        if not info["modules"]:
            self.last_error = (
                f"脚本 API 目录存在但缺少 Modules 子目录：{info['scripting']}")
            return False
        module_file = os.path.join(info["modules"], "DaVinciResolveScript.py")
        if not os.path.isfile(module_file):
            self.last_error = f"脚本模块缺失：{module_file}"
            return False
        if not info["lib"]:
            self.last_error = (
                "找不到 fusionscript 库（应用装在非默认位置时，请手动设置 "
                "RESOLVE_SCRIPT_LIB 指向安装目录下的 fusionscript.dll）")
            return False
        self.last_error = ""
        return True

    def probe_import(self) -> Tuple[bool, str]:
        """在**子进程**里试一次 ``import DaVinciResolveScript`` + ``scriptapp``。

        值得多起一个进程（只在首次 connect 时付一次）的原因写在模块
        docstring 里：Resolve 没在运行时，进程内 import ``fusionscript`` 会
        让 Python 以 ``0xC0000005`` 直接退出，父进程连异常都来不及抛。
        放进子进程之后，崩溃退化成一个非 0 退出码，于是能翻译成人话。

        返回 ``(True, "")``：子进程里 import 与 connect 都成功，可以在父进程
        里 import 了。返回 ``(False, 原因)``：不该在父进程里 import。
        起不了子进程（无 ``sys.executable``、``OSError``）时返回
        ``(True, "跳过预检")``——宁可退回旧行为，也不要因为探测本身不可用
        而把功能关掉。
        """
        exe = sys.executable or ""
        if not exe:
            return True, "跳过预检：拿不到 sys.executable"
        info = self.prepare_env()
        env = dict(os.environ)
        modules = info["modules"]
        if modules:
            old = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = modules + (os.pathsep + old if old else "")
        try:
            proc = subprocess.run(
                [exe, "-c", _PROBE_CODE],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, (f"子进程预检超过 {_PROBE_TIMEOUT:.0f} 秒没有返回"
                           "（脚本 API 卡住了，通常还是 Resolve 没起来）")
        except (OSError, ValueError) as exc:
            return True, f"跳过预检：起不了子进程（{type(exc).__name__}: {exc}）"
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        if proc.returncode != 0:
            return False, self._describe_exit(proc.returncode, out, err)
        if "CONNECT_OK" in out:
            return True, ""
        if "CONNECT_EMPTY" in out:
            return False, "子进程里 import 成功，但 scriptapp('Resolve') 返回空"
        if "IMPORT_FAILED" in out:
            line = _first_line_with(out, "IMPORT_FAILED")
            return False, f"子进程里 import 失败：{line}"
        return False, ("子进程预检没有给出可识别的结论"
                       f"（退出码 {proc.returncode}，输出 {out.strip()[:200]!r}）")

    @staticmethod
    def _describe_exit(code: int, out: str, err: str) -> str:
        """把子进程退出码翻译成一句人话（含 0xC0000005 的识别）。"""
        unsigned = code + 2 ** 32 if code < 0 else code
        hexed = f"0x{unsigned:08X}"
        if unsigned >= 0xC0000000:
            return (f"子进程在 import fusionscript 时崩溃退出（退出码 {hexed}，"
                    "访问冲突）——这是 Resolve 没在运行时的典型表现")
        lines = [ln for ln in err.strip().splitlines() if ln.strip()]
        tail = f"：{lines[-1].strip()[:200]}" if lines else ""
        return f"子进程预检失败（退出码 {hexed}）{tail}"

    def connect(self) -> Any:
        """取到 Resolve 对象（``dvr.scriptapp("Resolve")``）。连不上抛 :class:`ResolveError`。

        第一次调用会先跑 :meth:`probe_import`（子进程），确认脚本 API 在这台
        机器上真的能加载之后，才在父进程里 import。拿到对象后缓存在
        ``self._resolve``，后续调用不再预检。
        """
        if self._resolve is not None:
            return self._resolve
        ok, detail = self.probe_import()
        if not ok:
            self.last_error = detail
            hint = HINT_NOT_RUNNING if "scriptapp" in detail else HINT_CRASH
            raise ResolveError(f"连不上 DaVinci Resolve：{detail}", hint=hint)
        dvr = self._load_dvr()
        scriptapp = getattr(dvr, "scriptapp", None)
        if not callable(scriptapp):
            raise ResolveError(
                "DaVinciResolveScript 模块里没有 scriptapp()，脚本 API 不完整",
                hint=HINT_INSTALL,
            )
        try:
            resolve = scriptapp("Resolve")
        except Exception as exc:
            raise ResolveError(
                f"调用 scriptapp('Resolve') 抛异常：{type(exc).__name__}: {exc}",
                hint=HINT_NOT_RUNNING,
            ) from exc
        if not resolve:
            raise ResolveError("连不上正在运行的 DaVinci Resolve", hint=HINT_NOT_RUNNING)
        self._resolve = resolve
        return resolve

    # -- 元信息 ------------------------------------------------------------ #
    def version(self) -> str:
        """应用版本字符串，形如 ``"20.3"``；取不到返回 ``""``。

        依次试 ``GetVersionString()`` / ``GetVersion()``（两者都存在，但
        不同版本哪个给故事串不确定——**未验证**，所以两个都试）。
        """
        try:
            resolve = self._resolve or self.connect()
        except ResolveError:
            return ""
        for name in ("GetVersionString", "GetVersion"):
            fn = getattr(resolve, name, None)
            if not callable(fn):
                continue
            try:
                raw = fn()
            except Exception:  # 版本号拿不到不该让整条链路挂掉
                continue
            text = _version_text(raw)
            if text:
                return text
        return ""

    def is_studio(self) -> Optional[bool]:
        """是不是 Studio 版。判断不了返回 ``None``（**产品名串未验证**）。

        Studio 与免费版的分水岭是 ``External scripting`` 偏好项：免费版
        没有它，所以外部 Python 永远 ``scriptapp()`` 到空值。
        """
        if self._studio is not None:
            return self._studio
        try:
            resolve = self._resolve or self.connect()
        except ResolveError:
            return None
        for name in ("GetProductName", "GetProductVersion"):
            fn = getattr(resolve, name, None)
            if not callable(fn):
                continue
            try:
                raw = fn()
            except Exception:
                continue
            text = str(raw or "").strip()
            if not text:
                continue
            low = text.lower()
            if "studio" in low:
                self._studio = True
                return True
            if "resolve" in low:
                self._studio = False
                return False
        return None


def _version_text(raw: Any) -> str:
    """把 ``GetVersionString()`` 的返回值归一成 ``"20.3"`` 这样的串。"""
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
        candidate = ".".join(parts)
    else:
        candidate = str(raw).strip()
    match = _VERSION_RE.search(candidate)
    if match:
        return match.group(0)
    return candidate if candidate.replace(".", "").isdigit() else ""
