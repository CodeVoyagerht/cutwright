"""媒体探测与 ffmpeg 调用的薄封装。

三件事：
  * ``ffprobe`` 结构化探测（时长 / 尺寸 / 帧率 / HDR / 色彩范围），带进程内缓存
  * 统一的 ``run()``：失败时把 stderr 尾部一起抛出，不留「静默失败」
  * 素材清单扫描、缩略图、场景切点检测

跨平台：ppath 一律用 ``os.path``，调用 ffmpeg 时参数是列表不是字符串，
所以带空格的 Windows 路径不需要额外引号。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import config

__all__ = [
    "MediaInfo",
    "FFmpegError",
    "probe",
    "probe_all",
    "clear_cache",
    "run",
    "ffmpeg_version",
    "mean_volume_db",
    "extract_frame",
    "scene_cuts",
    "scan_media",
    "VIDEO_EXT",
    "AUDIO_EXT",
]

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".insv", ".lrv", ".webm", ".mts", ".mxf"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}

_CACHE: Dict[str, "MediaInfo"] = {}
_CACHE_LOCK = threading.Lock()


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe 返回非零退出码。带 stderr 尾部便于定位。"""

    def __init__(self, cmd: Sequence[str], code: int, stderr: str):
        self.cmd = list(cmd)
        self.code = code
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-8:])
        super().__init__(f"退出码 {code}: {' '.join(self.cmd[:4])} ...\n{tail}")


@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    color_transfer: str = ""
    color_range: str = ""
    has_video: bool = False
    has_audio: bool = False
    size: int = 0

    @property
    def is_hdr(self) -> bool:
        """HLG 或 PQ 传输特性 —— 直接塞进 SDR 时间线会发灰。"""
        ct = (self.color_transfer or "").lower()
        return ct in ("arib-std-b67", "smpte2084") or "hdr" in ct

    @property
    def is_full_range(self) -> bool:
        return (self.color_range or "").lower() in ("pc", "jpeg", "full")

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path, "duration": round(self.duration, 3),
            "width": self.width, "height": self.height,
            "fps": round(self.fps, 3), "pix_fmt": self.pix_fmt,
            "hdr": self.is_hdr, "color_range": self.color_range,
            "has_video": self.has_video, "has_audio": self.has_audio,
            "size": self.size,
        }


# --------------------------------------------------------------------------- #
# 进程
# --------------------------------------------------------------------------- #
def run(cmd: Sequence[str], timeout: Optional[float] = None) -> str:
    """跑一个子进程，成功返回 stdout 文本，失败抛 :class:`FFmpegError`。"""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise FFmpegError(cmd, 127, f"找不到可执行文件: {cmd[0]}\n{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(cmd, 124, f"超时 {timeout}s") from exc
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, err)
    return out


def _require(paths: config.Paths, what: str) -> str:
    exe = getattr(paths, what, None)
    if not exe:
        raise FFmpegError([what], 127,
                          f"找不到 {what}。安装后重试，或设置环境变量 "
                          f"CUTWRIGHT_FFMPEG 指向 ffmpeg 可执行文件。")
    return exe


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def probe(path: str, paths: Optional[config.Paths] = None,
          use_cache: bool = True) -> MediaInfo:
    """探测单个媒体文件。结果按「路径 + mtime + 大小」缓存。"""
    apath = os.path.abspath(path)
    try:
        st = os.stat(apath)
    except OSError:
        return MediaInfo(path=path)
    key = f"{apath}|{int(st.st_mtime)}|{st.st_size}"
    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None:
                return hit

    paths = paths or config.discover()
    ffprobe = _require(paths, "ffprobe")
    info = MediaInfo(path=apath, size=st.st_size)
    out = run([ffprobe, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", apath])
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        data = {}

    fmt = data.get("format") or {}
    info.duration = _num(fmt.get("duration"))
    for stream in data.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.fps = _parse_rate(stream.get("r_frame_rate") or stream.get("avg_frame_rate"))
            info.pix_fmt = stream.get("pix_fmt") or ""
            info.color_transfer = stream.get("color_transfer") or ""
            info.color_range = stream.get("color_range") or ""
            if not info.duration:
                info.duration = _num(stream.get("duration"))
        elif kind == "audio":
            info.has_audio = True

    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = info
    return info


def probe_all(paths_list: Iterable[str], paths: Optional[config.Paths] = None,
              workers: int = 8) -> Dict[str, MediaInfo]:
    """并发探测一批文件（ffprobe 是纯 IO 等待，线程足够）。"""
    from concurrent.futures import ThreadPoolExecutor

    items = list(paths_list)
    paths = paths or config.discover()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda p: probe(p, paths), items))
    return {os.path.abspath(p): info for p, info in zip(items, results)}


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def ffmpeg_version(paths: Optional[config.Paths] = None) -> str:
    paths = paths or config.discover()
    ffmpeg = _require(paths, "ffmpeg")
    out = run([ffmpeg, "-version"])
    first = (out.splitlines() or [""])[0]
    m = re.search(r"version\s+(\S+)", first)
    return m.group(1) if m else first.strip()


# --------------------------------------------------------------------------- #
# 音频 / 画面小工具
# --------------------------------------------------------------------------- #
def mean_volume_db(path: str, paths: Optional[config.Paths] = None) -> Optional[float]:
    """``volumedetect`` 的平均音量（dBFS）。用来对齐不同素材的底噪。"""
    paths = paths or config.discover()
    ffmpeg = _require(paths, "ffmpeg")
    try:
        # 这个滤镜把报告写在 stderr，而 ffmpeg 退出码为 0 —— 所以不用 run()
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path, "-af", "volumedetect",
                               "-f", "null", "-"], capture_output=True,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return None
    text = (proc.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", text)
    return float(m.group(1)) if m else None


def extract_frame(path: str, out_png: str, at: float = 0.0, width: int = 480,
                  paths: Optional[config.Paths] = None) -> str:
    """抽一帧当缩略图 / 封面。已存在则不重做（幂等）。"""
    if os.path.exists(out_png) and os.path.getsize(out_png) > 0:
        return out_png
    paths = paths or config.discover()
    ffmpeg = _require(paths, "ffmpeg")
    os.makedirs(os.path.dirname(os.path.abspath(out_png)) or ".", exist_ok=True)
    run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{max(0.0, at):.3f}", "-i", path,
         "-frames:v", "1", "-vf", f"scale={width}:-2", out_png])
    return out_png


def scene_cuts(path: str, threshold: float = 0.35,
               paths: Optional[config.Paths] = None) -> List[float]:
    """场景切点检测（秒）。给「从一坨素材里自动挑镜头」用的。

    ``threshold`` 越小越敏感；0.3-0.4 适合口播/旅拍素材。
    """
    paths = paths or config.discover()
    ffmpeg = _require(paths, "ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-filter:v",
         f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    text = (proc.stderr or b"").decode("utf-8", "replace")
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", text)]
    return sorted(set(times))


def scan_media(root: str, recursive: bool = True,
               exts: Optional[Iterable[str]] = None) -> List[str]:
    """列出一个目录下的媒体文件（按文件名自然排序）。"""
    wanted = {e.lower() for e in (exts or (VIDEO_EXT | AUDIO_EXT))}
    out: List[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in wanted:
                    out.append(os.path.join(dirpath, fn))
    else:
        for fn in os.listdir(root) if os.path.isdir(root) else []:
            if os.path.splitext(fn)[1].lower() in wanted:
                out.append(os.path.join(root, fn))
    out.sort(key=_natural_key)
    return out


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_rate(rate) -> float:
    """``"30000/1001"`` → 29.97"""
    if not rate:
        return 0.0
    text = str(rate)
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    return _num(text)


def _natural_key(path: str) -> Tuple:
    name = os.path.basename(path)
    return tuple(int(p) if p.isdigit() else p.lower()
                 for p in re.split(r"(\d+)", name))
