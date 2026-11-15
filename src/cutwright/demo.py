"""合成演示素材：让第一次 `clone` 下来的人立刻能出片。

为什么要有这一层：一个剪辑工具最尴尬的时刻，是别人照着 README 敲了第一条命令，
得到「素材不存在」四个字，然后关掉页面。所以演示素材必须是**能现造**的——
ffmpeg 的 lavfi 合成源负责视频与音频，一个三十行的 PNG 编码器负责那张透明画框。

刻意不依赖 Pillow：核心包的一个卖点是「零依赖」，而演示素材是所有人第一条会
走的路，它不该成为例外。PNG 是唯一需要手写的格式，zlib 是标准库。

素材刻意做得"像一个真实项目"：不同分辨率、一段横屏、一张真透明的 PNG、
一段比别的暗的片子（用来验证自动补增益），以及一条合成 BGM。
"""
from __future__ import annotations

import os
import struct
import subprocess
import zlib
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# (文件名, 宽, 高, 秒, 描述)
ClipSpec = Tuple[str, int, int, float, str]

# `cutwright init --with-media` 用的三镜：时长要够分镜表里最长的"起点 + 长度"
# （clip02 是 1.5 + 2.5 = 4.0s），否则切片会拿到空尾巴。
SCAFFOLD_CLIPS: List[ClipSpec] = [
    ("clip01.mp4", 1080, 1920, 4.5, "竖屏主镜头"),
    ("clip02.mp4", 1920, 1080, 4.5, "横屏素材（验证裁切）"),
    ("clip03.mp4", 1080, 1920, 4.5, "偏左构图"),
]

# `examples/demo` 用的七镜，时长与那份 shotlist.json 的剪点对应。
PREVIEW_CLIPS: List[ClipSpec] = [
    ("clip01.mp4", 1080, 1920, 6.0, "竖屏主镜头"),
    ("clip02.mp4", 1920, 1080, 6.0, "横屏素材（验证裁切与 cx）"),
    ("clip03.mp4", 1080, 1920, 6.0, "竖屏特写"),
    ("clip04.mp4", 1080, 1920, 6.0, "快节奏段"),
    ("clip05.mp4", 1080, 1920, 6.0, "收束段"),
    ("clip06.mp4", 1080, 1920, 6.0, "细节补充"),
    ("clip07.mp4", 1080, 1920, 5.0, "收尾"),
]

FRAME_SIZE = (900, 1500)


class DemoMediaError(RuntimeError):
    """演示素材造不出来（缺 ffmpeg、命令失败）。消息里要给出下一步。"""


# --------------------------------------------------------------------------- #
# 最小 PNG 编码器
# --------------------------------------------------------------------------- #
def write_rgba_png(path: str, width: int, height: int,
                   pixel: Callable[[int, int], Tuple[int, int, int, int]]) -> None:
    """写一张 8bit RGBA 的 PNG。``pixel(x, y)`` 返回 (r, g, b, a)。

    只用标准库：zlib 压缩 + 手拼 chunk。够小、够确定、可被单元测试逐像素验证。
    """
    raw = bytearray()
    for y in range(height):
        raw.append(0)                      # 每行的 filter type：0 = None
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload +
                struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", ihdr))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        fh.write(chunk(b"IEND", b""))


def _frame_pixel(x: int, y: int) -> Tuple[int, int, int, int]:
    """两层描边的画框，中间**真透明**。

    不透明的话它会把画面挡成一个黑窟窿——这是实测踩过的坑，所以中间的
    alpha 必须是 0，而不是"看起来是黑的"。
    """
    d = min(x, FRAME_SIZE[0] - 1 - x, y, FRAME_SIZE[1] - 1 - y)
    if 8 <= d < 18:                        # 外框：白，85% 不透明
        return (255, 255, 255, 216)
    if 24 <= d < 28:                       # 内框：金，60% 不透明
        return (212, 175, 55, 153)
    return (0, 0, 0, 0)


def make_frame_png(path: str, force: bool = False) -> bool:
    """写透明画框。返回 True 表示真写了。"""
    if os.path.exists(path) and not force:
        return False
    write_rgba_png(path, FRAME_SIZE[0], FRAME_SIZE[1], _frame_pixel)
    return True


# --------------------------------------------------------------------------- #
# ffmpeg 合成
# --------------------------------------------------------------------------- #
def find_ffmpeg(explicit: Optional[str] = None) -> str:
    """找 ffmpeg：显式参数 → 系统 PATH → 项目已知位置。"""
    if explicit:
        return explicit
    try:
        from . import config
        found = config.discover().ffmpeg
        if found:
            return found
    except Exception:                      # 独立运行时可导入失败，不致命
        pass
    import shutil
    return shutil.which("ffmpeg") or ""


def _run(cmd: Sequence[str]) -> None:
    proc = subprocess.run(list(cmd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise DemoMediaError("ffmpeg 失败：\n  " + "\n  ".join(tail))


def make_clip(ffmpeg: str, path: str, index: int, width: int, height: int,
              duration: float, force: bool = False) -> bool:
    """合成一段"看得出是哪一镜"的测试视频（带一条正弦环境声）。"""
    if os.path.exists(path) and not force:
        return False
    # 别用 "索引 * 常数" 直接当颜色：索引 0 会算出纯黑，第一镜变黑场，
    # 既难看又会踩 qc 的黑场阈值。这里固定给中调起点，再按索引铺开。
    r = 40 + (index * 37) % 130
    g = 40 + (index * 61) % 110
    b = 40 + (index * 89) % 140
    hue = (index * 47) % 360
    src = (f"color=c=0x{r:02x}{g:02x}{b:02x}:s={width}x{height}:d={duration}:r=30,"
           f"hue=H={hue},"
           f"drawbox=x='if(eq(mod(t,4),0),0,(t*{width}/max({duration},1))-{width})'"
           f":y=ih/3:w=iw/4:h=ih/3:color=white@0.55:t=fill,"
           f"drawtext=text='SHOT {index + 1}':fontcolor=white@0.9"
           f":fontsize={max(48, height // 12)}"
           f":x=(w-text_w)/2:y=(h-text_h)/2")
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", src,
          "-f", "lavfi", "-i", f"sine=frequency={220 + index * 55}:duration={duration}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
          "-shortest", path])
    return True


def make_bgm(ffmpeg: str, path: str, duration: float, force: bool = False) -> bool:
    """合成一段"像配乐"的音轨：三段和弦 + 慢速颤音 + 首尾淡入淡出。"""
    if os.path.exists(path) and not force:
        return False
    duration = max(1.0, float(duration))
    expr = ("aevalsrc=0.12*sin(2*PI*220*t)*(1+0.3*sin(2*PI*2*t))"
            "+0.09*sin(2*PI*330*t)+0.07*sin(2*PI*440*t)"
            f":s=48000:d={duration}")
    fade_out = max(0.0, duration - 1.0)
    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", expr,
          "-af", f"tremolo=f=3.2:d=0.25,afade=t=in:d=0.4,"
                 f"afade=t=out:st={fade_out:.3f}:d=1.0",
          "-ar", "48000", "-ac", "2", path])
    return True


def make_demo_media(out_dir: str,
                    clips: Optional[Sequence[ClipSpec]] = None,
                    ffmpeg: Optional[str] = None,
                    force: bool = False,
                    with_bgm: bool = True,
                    bgm_duration: Optional[float] = None,
                    log: Optional[Callable[[str], None]] = None) -> Dict[str, List[str]]:
    """把整套演示素材造到 ``out_dir`` 里。

    Returns:
        ``{"created": [...], "skipped": [...]}``，值都是绝对路径。
        缺 ffmpeg 时抛 :class:`DemoMediaError`，消息里写清楚怎么装。
    """
    say = log or (lambda _msg: None)
    specs = list(clips if clips is not None else SCAFFOLD_CLIPS)
    target = os.path.abspath(out_dir)
    os.makedirs(target, exist_ok=True)

    exe = find_ffmpeg(ffmpeg)
    if not exe:
        raise DemoMediaError(
            "找不到 ffmpeg，无法合成演示素材。\n"
            "  装一个再回来：winget install Gyan.FFmpeg（Windows）"
            " / brew install ffmpeg（macOS）\n"
            "  或者自己把素材放进 "
            f"{target}，文件名与分镜表里的一致就行。")

    created: List[str] = []
    skipped: List[str] = []

    for index, (name, width, height, duration, desc) in enumerate(specs):
        path = os.path.join(target, name)
        if make_clip(exe, path, index, width, height, duration, force=force):
            created.append(path)
            say(f"  [ok]   {name}  {width}x{height} {duration}s  {desc}")
        else:
            skipped.append(path)
            say(f"  [skip] {name} 已存在")

    frame = os.path.join(target, "frame.png")
    if make_frame_png(frame, force=force):
        created.append(frame)
        say(f"  [ok]   frame.png {FRAME_SIZE[0]}x{FRAME_SIZE[1]} 透明画框")
    else:
        skipped.append(frame)
        say("  [skip] frame.png 已存在")

    if with_bgm:
        total = bgm_duration if bgm_duration is not None else max(c[3] for c in specs)
        bgm = os.path.join(target, "bgm.mp3")
        if make_bgm(exe, bgm, total, force=force):
            created.append(bgm)
            say(f"  [ok]   bgm.mp3 {total:.1f}s 合成配乐")
        else:
            skipped.append(bgm)
            say("  [skip] bgm.mp3 已存在")

    return {"created": created, "skipped": skipped}
