"""剪映 ``videoeditor.dll`` 的加解密封装（ctypes）。

剪映 11.6 的草稿里有几个文件是**密文**（磁盘上是 ASCII base64 文本），
由 ``videoeditor.dll`` 导出的 ``EncryptUtils`` 类负责。本模块用 ctypes 直接
调用那两个导出函数，不依赖任何第三方库。

调用约定（MSVC x64，成员函数首参是 ``this``）：

* ``?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@...AEBV34@0AEA_N@Z``
  参数 ``(this, out, src, param, ok_flag)``，``param`` 固定传 ``b"{}"``；
* ``?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@...AEBV34@@Z``
  参数 ``(this, out, src)``。

字符串用 MSVC 的 ``std::string`` 布局（32 字节：16 字节 SSO 缓冲区 +
``size`` + ``res``，``res == 15`` 表示小字符串优化）。

设计要点
--------
* **导入无副作用**：模块被 import 时不会去找 DLL、不会崩；一切推迟到第一次
  真正用到加解密时（:func:`available` / :func:`get_crypto`）。
* **优雅降级**：找不到 ``videoeditor.dll`` 时 :func:`available` 返回 ``False``，
  :func:`encrypt_bytes` / :func:`decrypt_bytes` 抛带可执行建议的
  :class:`CryptoError`（提示可以用 ``--no-encrypt`` 输出明文草稿）。
* 只在 Windows 上可用；其他平台 :func:`available` 恒为 ``False``。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CryptoError",
    "JianyingCrypto",
    "available",
    "crypt_tree",
    "decrypt_bytes",
    "encrypt_bytes",
    "find_install_dir",
    "get_crypto",
    "is_content_file_path",
    "is_plain_json",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: MSVC 修饰名。名字里带 ``$`` 与 ``@``，不能改一个字符。
NAME_DECRYPT_WITH_FLAG = (
    "?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z"
)
NAME_DECRYPT_PLAIN = (
    "?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@0@Z"
)
NAME_ENCRYPT = (
    "?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@@Z"
)
NAME_ENABLE = "?enable@EncryptUtils@lvve@@QEAAX_N@Z"
NAME_IS_ENABLE = "?isEnable@EncryptUtils@lvve@@QEAA_NXZ"

#: 解密参数固定为 ``{}``。
_DEFAULT_PARAM = b"{}"

#: DLL 文件名。
DLL_NAME = "videoeditor.dll"

#: 允许是密文的相对路径（其余文件一律明文）。
_CONTENT_NAMES = frozenset(
    ("draft_content.json", "draft_content.json.bak", "template-2.tmp",
     "draft_info.json", "draft_info.json.bak")
)

#: 剪映安装目录的环境变量覆盖，按优先级排列。
ENV_INSTALL_DIR = "CUTWRIGHT_JY_INSTALL"
_ENV_COMPAT = ("JY_INSTALL_DIR", "JY_DLL")

#: ``SetDefaultDllDirectories`` / ``LoadLibraryExW`` 用到的标志位。
_LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x00000100
_LOAD_LIBRARY_SEARCH_USER_DIRS = 0x00000400
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

_IS_WINDOWS = sys.platform.startswith("win")

#: 找不到 DLL 时给用户看的一句话（API 与 CLI 共用）。
NO_DLL_HINT = (
    "未找到剪映 videoeditor.dll，可用 --no-encrypt 输出明文草稿"
    "（也可以设置环境变量 {} 指向剪映安装目录）".format(ENV_INSTALL_DIR)
)


class CryptoError(RuntimeError):
    """加解密失败。消息里一定带「怎么办」，调用方可以直接打印给用户看。"""


# --------------------------------------------------------------------------- #
# MSVC std::string 布局
# --------------------------------------------------------------------------- #

class _Bxty(ctypes.Union):
    """``std::string`` 的 16 字节缓冲区（SSO 内联 / 堆指针二选一）。"""

    _fields_ = [("buf", ctypes.c_char * 16), ("ptr", ctypes.c_void_p)]


class _MSVCString(ctypes.Structure):
    """MSVC x64 下的 ``std::string``：16 字节 buf/bxty + size + res。"""

    _fields_ = [
        ("bxty", _Bxty),
        ("size", ctypes.c_size_t),
        ("res", ctypes.c_size_t),
    ]


assert ctypes.sizeof(_MSVCString) == 32, "MSVCString 必须是 32 字节"


def _make_out() -> _MSVCString:
    """造一个空的输出字符串（``res = 15`` 表示 SSO）。"""
    out = _MSVCString()
    ctypes.memset(ctypes.addressof(out), 0, ctypes.sizeof(out))
    out.size = 0
    out.res = 15
    return out


def _make_in(data: bytes, keep: List[Any]) -> _MSVCString:
    """把 bytes 塞进 ``std::string``。堆缓冲区登记进 ``keep`` 防 GC。"""
    s = _MSVCString()
    ctypes.memset(ctypes.addressof(s), 0, ctypes.sizeof(s))
    n = len(data)
    if n <= 15:
        if n:
            ctypes.memmove(ctypes.addressof(s.bxty), data, n)
        s.size = n
        s.res = 15
    else:
        buf = ctypes.create_string_buffer(n + 1)
        ctypes.memmove(buf, data, n)
        keep.append(buf)
        s.bxty.ptr = ctypes.cast(buf, ctypes.c_void_p)
        s.size = n
        s.res = n
    return s


def _read_out(s: _MSVCString) -> bytes:
    """把输出 ``std::string`` 读回 bytes。"""
    n = int(s.size)
    if n <= 0:
        return b""
    if n <= 15:
        return ctypes.string_at(ctypes.addressof(s.bxty), n)
    return ctypes.string_at(s.bxty.ptr, n)


# --------------------------------------------------------------------------- #
# 明文判定 / 文件表
# --------------------------------------------------------------------------- #

def is_plain_json(data: bytes) -> bool:
    """判断一段 bytes 是不是**明文 JSON**（而不是剪映密文）。

    剪映密文是纯 ASCII 的 base64 文本，头一个字符不可能是 ``{`` ``[`` ``"``；
    真正的明文则一定能被 ``json.loads`` 解析。空数据视为「非明文」。
    """
    if not data:
        return False
    head = data.lstrip()[:1]
    if head not in (b"{", b"[", b'"'):
        return False
    if data[:4] == b"\xef\xbb\xbf":  # UTF-8 BOM
        data = data[4:]
    try:
        json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def is_content_file_path(rel_path: str) -> bool:
    """判断草稿目录内的相对路径是否**应该是密文**。

    规则（与剪映 11.6 实际落盘一致）：

    * ``draft_meta_info.json``：只在根目录，必须加密；
    * ``draft_content.json`` / ``draft_content.json.bak`` / ``template-2.tmp`` /
      ``draft_info.json``：根目录与 ``Timelines/<id>/`` 下都要加密；
    * 其余（``subdraft/``、``common_attachment/``、``draft_settings`` 等）一律明文。
    """
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    if name == "draft_meta_info.json":
        return len(parts) == 1
    if name not in _CONTENT_NAMES:
        return False
    if len(parts) == 1:
        return True
    return parts[0] == "Timelines" and len(parts) == 3


# --------------------------------------------------------------------------- #
# 找安装目录
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)")


def _version_key(path: str) -> Tuple[int, int, int, int]:
    """把目录名里的版本号（如 ``11.6.0.14552``）转成可排序的 4 元组。"""
    m = _VERSION_RE.search(os.path.basename(os.path.normpath(path)))
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def _candidate_dirs() -> List[str]:
    """列出可能藏有 ``videoeditor.dll`` 的目录（去重、保持顺序）。"""
    cands: List[str] = []

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        path = os.path.expanduser(os.path.expandvars(str(path)))
        if not path:
            return
        if os.path.isfile(path):
            path = os.path.dirname(path)
        path = os.path.abspath(path)
        if path not in cands:
            cands.append(path)

    for env in (ENV_INSTALL_DIR,) + _ENV_COMPAT:
        _add(os.environ.get(env))

    local = os.environ.get("LOCALAPPDATA")
    if local:
        apps = os.path.join(local, "JianyingPro", "Apps")
        _add(apps)
        _add(os.path.join(local, "JianyingPro"))
        # Apps 下每个版本目录，以及版本目录的一层子目录
        for base in (apps, os.path.join(local, "JianyingPro")):
            try:
                children = sorted(os.listdir(base))
            except OSError:
                continue
            for child in children:
                _add(os.path.join(base, child))
                try:
                    for grand in sorted(os.listdir(os.path.join(base, child))):
                        _add(os.path.join(base, child, grand))
                except OSError:
                    continue

    for pf in ("PROGRAMFILES", "PROGRAMFILES(X86)", "ProgramFiles", "ProgramW6432"):
        root = os.environ.get(pf)
        if root:
            _add(os.path.join(root, "JianyingPro"))
    for fixed in ("C:\\Program Files\\JianyingPro",
                  "C:\\Program Files (x86)\\JianyingPro"):
        _add(fixed)
    return cands


def find_install_dir(install_dir: Optional[str] = None) -> Optional[str]:
    """找剪映安装目录（含 ``videoeditor.dll`` 的那个）。

    优先级：显式参数 > ``CUTWRIGHT_JY_INSTALL`` / ``JY_INSTALL_DIR`` / ``JY_DLL``
    > ``%LOCALAPPDATA%\\JianyingPro\\Apps`` 下的版本目录（按版本号从高到低）
    > ``Program Files`` 里的常见路径。

    返回 ``None`` 表示没找到——调用方据此走明文降级，**不要**抛异常。
    """
    if install_dir:
        path = os.path.abspath(os.path.expanduser(install_dir))
        if os.path.isfile(path):
            path = os.path.dirname(path)
        return path if os.path.isfile(os.path.join(path, DLL_NAME)) else None

    found: List[str] = []
    for cand in _candidate_dirs():
        try:
            if os.path.isfile(os.path.join(cand, DLL_NAME)):
                found.append(cand)
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=_version_key, reverse=True)
    return found[0]


# --------------------------------------------------------------------------- #
# DLL 封装
# --------------------------------------------------------------------------- #

class JianyingCrypto:
    """一个已经 ``load()`` 的 ``EncryptUtils`` 句柄。

    用法::

        crypto = JianyingCrypto().load()
        plain = crypto.decrypt_bytes(cipher)
        cipher2 = crypto.encrypt_bytes(plain)   # 自带回环校验
        crypto.close()
    """

    def __init__(self, install_dir: Optional[str] = None) -> None:
        self.install_dir = find_install_dir(install_dir)
        if not self.install_dir:
            raise CryptoError(NO_DLL_HINT)
        self._dll: Any = None
        self._kernel32: Any = None
        self._cookie: Optional[int] = None
        self._decrypt: Any = None
        self._encrypt: Any = None
        self._enable: Any = None
        self._is_enable: Any = None
        self._keep: List[Any] = []
        self._loaded = False
        self._has_flag = True

    # -- 生命周期 ---------------------------------------------------------- #
    def load(self) -> "JianyingCrypto":
        """加载 DLL 并解析导出符号。失败抛 :class:`CryptoError`。"""
        if self._loaded:
            return self
        if not _IS_WINDOWS:
            raise CryptoError(
                "剪映加解密只能在 Windows 上使用；其他平台请用 --no-encrypt 输出明文草稿"
            )
        dll_path = os.path.join(self.install_dir or "", DLL_NAME)
        if not os.path.isfile(dll_path):
            raise CryptoError(NO_DLL_HINT)

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.SetDefaultDllDirectories.argtypes = [ctypes.c_uint32]
        k32.SetDefaultDllDirectories.restype = ctypes.c_int
        k32.AddDllDirectory.argtypes = [ctypes.c_wchar_p]
        k32.AddDllDirectory.restype = ctypes.c_void_p
        k32.RemoveDllDirectory.argtypes = [ctypes.c_void_p]
        k32.RemoveDllDirectory.restype = ctypes.c_int
        k32.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
        k32.LoadLibraryExW.restype = ctypes.c_void_p
        k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        k32.GetProcAddress.restype = ctypes.c_void_p
        k32.FreeLibrary.argtypes = [ctypes.c_void_p]
        k32.FreeLibrary.restype = ctypes.c_int

        flags = _LOAD_LIBRARY_SEARCH_SYSTEM32 | _LOAD_LIBRARY_SEARCH_USER_DIRS
        if not k32.SetDefaultDllDirectories(flags):
            raise CryptoError(
                "SetDefaultDllDirectories 失败: %d" % ctypes.get_last_error()
            )
        self._kernel32 = k32
        self._cookie = k32.AddDllDirectory(self.install_dir)

        # DLL 内部会用相对路径加载同目录的依赖，必须先切到安装目录。
        old_cwd = os.getcwd()
        try:
            os.chdir(self.install_dir)
            handle = k32.LoadLibraryExW(dll_path, None, 0x100 | flags)
        finally:
            os.chdir(old_cwd)
        if not handle:
            raise CryptoError(
                "加载 %s 失败（错误码 %d）；请确认剪映已正确安装、"
                "且本进程位数与 DLL 一致（64 位）" % (DLL_NAME, ctypes.get_last_error())
            )
        self._dll = handle

        decrypt_addr = k32.GetProcAddress(handle, NAME_DECRYPT_WITH_FLAG.encode("ascii"))
        if decrypt_addr:
            proto = ctypes.CFUNCTYPE(
                ctypes.c_void_p,                       # 返回值其实是 std::string（隐藏返回值）
                ctypes.c_void_p,                       # this
                ctypes.POINTER(_MSVCString),           # out
                ctypes.POINTER(_MSVCString),           # src
                ctypes.POINTER(_MSVCString),           # param
                ctypes.POINTER(ctypes.c_bool),         # ok flag
            )
            self._decrypt = proto(decrypt_addr)
            self._has_flag = True
        else:
            decrypt_addr = k32.GetProcAddress(handle, NAME_DECRYPT_PLAIN.encode("ascii"))
            if not decrypt_addr:
                raise CryptoError("缺少 EncryptUtils::decrypt 导出；剪映版本不兼容")
            proto = ctypes.CFUNCTYPE(
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_MSVCString),
                ctypes.POINTER(_MSVCString),
                ctypes.POINTER(_MSVCString),
            )
            self._decrypt = proto(decrypt_addr)
            self._has_flag = False

        enc_addr = k32.GetProcAddress(handle, NAME_ENCRYPT.encode("ascii"))
        if not enc_addr:
            raise CryptoError("缺少 EncryptUtils::encrypt 导出；剪映版本不兼容")
        enc_proto = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_MSVCString),
            ctypes.POINTER(_MSVCString),
        )
        self._encrypt = enc_proto(enc_addr)

        en_addr = k32.GetProcAddress(handle, NAME_ENABLE.encode("ascii"))
        if en_addr:
            self._enable = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_bool)(en_addr)
        en_q_addr = k32.GetProcAddress(handle, NAME_IS_ENABLE.encode("ascii"))
        if en_q_addr:
            self._is_enable = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)(en_q_addr)

        self._loaded = True
        return self

    def close(self) -> None:
        """释放 DLL。已关闭时再调是安全的。"""
        if self._dll and self._kernel32:
            self._kernel32.FreeLibrary(self._dll)
        if self._cookie and self._kernel32:
            self._kernel32.RemoveDllDirectory(self._cookie)
        self._dll = None
        self._cookie = None
        self._decrypt = self._encrypt = None
        self._loaded = False

    def __enter__(self) -> "JianyingCrypto":
        return self.load()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 能力 -------------------------------------------------------------- #
    @property
    def is_enabled(self) -> bool:
        """DLL 内部的加密开关当前是否打开。"""
        if not self._loaded or self._is_enable is None:
            return False
        return bool(self._is_enable(None))

    def enable(self, on: bool = True) -> None:
        """打开/关闭 DLL 的加密开关（加密前必须打开）。"""
        if not self._loaded:
            self.load()
        if self._enable is not None:
            self._enable(None, bool(on))

    # -- 加解密 ------------------------------------------------------------ #
    def decrypt_bytes(self, data: bytes) -> bytes:
        """解密一段剪映密文。失败抛 :class:`CryptoError`。"""
        if not self._loaded:
            self.load()
        keep: List[Any] = []

        def _once(payload: bytes) -> Tuple[bytes, bool]:
            src = _make_in(payload, keep)
            out = _make_out()
            param = _make_in(_DEFAULT_PARAM, keep)
            if self._has_flag:
                ok = ctypes.c_bool(False)
                self._decrypt(None, ctypes.byref(out), ctypes.byref(src),
                              ctypes.byref(param), ctypes.byref(ok))
                return _read_out(out), bool(ok.value)
            self._decrypt(None, ctypes.byref(out), ctypes.byref(src),
                          ctypes.byref(param))
            return _read_out(out), True

        result, ok = _once(data)
        if not ok:
            stripped = data.strip()
            if stripped != data:
                result, ok = _once(stripped)
        if not ok:
            raise CryptoError(
                "解密失败（ok_flag=false，输入 %d 字节）；请确认这是剪映 11.6 写出的密文"
                % len(data)
            )
        if not result:
            raise CryptoError("解密输出为空（输入可能不是合法密文）")
        return result

    def encrypt_bytes(self, plain: bytes) -> bytes:
        """加密一段明文 JSON，并做一次解密回环校验。"""
        if not self._loaded:
            self.load()
        self.enable(True)
        keep: List[Any] = []
        src = _make_in(plain, keep)
        out = _make_out()
        self._encrypt(None, ctypes.byref(out), ctypes.byref(src))
        enc = _read_out(out)
        if not enc:
            raise CryptoError(
                "加密失败：输出为空（明文须为合法草稿 JSON，且含 id / new_version 字段）"
            )
        back = self.decrypt_bytes(enc)
        if back != plain:
            raise CryptoError(
                "加密回环校验失败（解密结果与明文不一致，%d vs %d 字节）"
                % (len(back), len(plain))
            )
        return enc


# --------------------------------------------------------------------------- #
# 单例
# --------------------------------------------------------------------------- #

_LOCK = threading.Lock()
_SINGLETON: Optional[JianyingCrypto] = None
_LOAD_ERROR: Optional[str] = None


def get_crypto(install_dir: Optional[str] = None) -> JianyingCrypto:
    """取进程内单例（首次调用时加载 DLL）。失败抛 :class:`CryptoError`。"""
    global _SINGLETON, _LOAD_ERROR
    with _LOCK:
        if _SINGLETON is not None:
            return _SINGLETON
        if _LOAD_ERROR is not None:
            raise CryptoError(_LOAD_ERROR)
        try:
            inst = JianyingCrypto(install_dir).load()
        except CryptoError as exc:
            _LOAD_ERROR = str(exc)
            raise
        _SINGLETON = inst
        return inst


def reset() -> None:
    """丢掉单例缓存（测试与「插上/拔掉剪映」后重新探测用）。"""
    global _SINGLETON, _LOAD_ERROR
    with _LOCK:
        if _SINGLETON is not None:
            try:
                _SINGLETON.close()
            except Exception:  # pragma: no cover - 关闭失败不该影响调用方
                pass
        _SINGLETON = None
        _LOAD_ERROR = None


def available() -> bool:
    """本机能不能加解密（DLL 在不在、加载得起来吗）。**不会抛异常**。"""
    try:
        get_crypto()
    except CryptoError:
        return False
    except Exception:  # pragma: no cover - 防御性：任何意外都当不可用
        return False
    return True


def decrypt_bytes(data: bytes) -> bytes:
    """解密字节串。已经是明文则原样返回（幂等，便于反复调用）。"""
    if is_plain_json(data):
        return data
    return get_crypto().decrypt_bytes(data.strip() or data)


def encrypt_bytes(plain: bytes) -> bytes:
    """加密字节串。明文保持明文（调用方明确要求「这份文件加密」时才该调用）。"""
    return get_crypto().encrypt_bytes(plain)


# --------------------------------------------------------------------------- #
# 整树拷贝 + 按文件表加解密
# --------------------------------------------------------------------------- #

def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def plain_json_bytes(obj: Any) -> bytes:
    """把 dict 序列化成剪映习惯的单行紧凑 JSON bytes。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def crypt_tree(src: str, out: str, *, encrypt: bool = False,
               crypto: Optional[JianyingCrypto] = None) -> Dict[str, Any]:
    """把明文草稿目录 ``src`` 复制到 ``out``，顺带按文件表加/解密。

    ``encrypt=True`` → 内容文件写成剪映密文（真实草稿用）；
    ``encrypt=False`` → 内容文件保持明文（便于 diff / review）。

    返回统计 ``{"files": [...], "encrypted": [...], "failed": [{path, error}]}``，
    单个文件失败不会中断整棵树。
    """
    stats: Dict[str, Any] = {"files": [], "encrypted": [], "failed": []}
    inst = None
    if encrypt:
        inst = crypto or get_crypto()

    for root, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".recycle_bin"))
        for fname in sorted(filenames):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, src)
            dst = os.path.join(out, rel)
            stats["files"].append(rel.replace("\\", "/"))
            try:
                if not is_content_file_path(rel):
                    parent = os.path.dirname(dst)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    shutil.copy2(full, dst)
                    continue
                data = _read_bytes(full)
                if not is_plain_json(data):
                    data = decrypt_bytes(data)
                if encrypt:
                    assert inst is not None
                    data = inst.encrypt_bytes(data)
                    stats["encrypted"].append(rel.replace("\\", "/"))
                _write_bytes(dst, data)
            except Exception as exc:  # noqa: BLE001 - 汇总报告而不是中断
                stats["failed"].append({"path": rel.replace("\\", "/"),
                                        "error": "%s: %s" % (type(exc).__name__, exc)})
    return stats
