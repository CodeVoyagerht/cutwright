#!/usr/bin/env python3
"""生成一套能直接跑通的演示素材（不需要你有任何视频）。

为什么要这个脚本：一个剪辑工具最尴尬的时刻，是别人 clone 下来发现
「示例需要 7 个 mp4」然后关掉页面。这个脚本用 ffmpeg 的合成源现造素材——
纯色渐变、移动的色块、一段简单的合成音——30 秒渲完，之后 `cutwright build`
就能出一条真片子。

    python examples/demo/make_media.py --out examples/demo/materials
    cd examples/demo && cutwright build shotlist.json -o out

素材刻意做得"像一个真实项目"：不同分辨率、一段横屏、一段带 alpha 的 PNG、
一段比别的暗的片子（用来验证自动补增益），以及一条合成 BGM。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 这些素材的时长与 shotlist.json 里的剪点对应，改一个就要改另一个。
CLIPS = [
    # (文件名, 宽, 高, 秒, 描述)
    ("clip01.mp4", 1080, 1920, 6.0, "竖屏主镜头"),
    ("clip02.mp4", 1920, 1080, 5.0, "横屏素材（验证裁切与 cx）"),
    ("clip03.mp4", 1080, 1920, 6.0, "竖屏特写"),
    ("clip04.mp4", 1080, 1920, 5.0, "快节奏段"),
    ("clip05.mp4", 1080, 1920, 6.0, "收束段"),
    ("clip06.mp4", 1080, 1920, 5.0, "细节补充"),
    ("clip07.mp4", 1080, 1920, 4.0, "收尾"),
]


def _ffmpeg(paths_ffmpeg=None):
    if paths_ffmpeg:
        return paths_ffmpeg
    try:                                    # 优先复用项目的路径发现
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "src"))
        from cutwright import config
        found = config.discover().ffmpeg
        if found:
            return found
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def run(cmd, quiet=True):
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise SystemExit("ffmpeg 失败:\n  " + "\n  ".join(tail))
    return proc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成演示素材")
    ap.add_argument("--out", default="examples/demo/materials")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    ffmpeg = _ffmpeg(args.ffmpeg)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    print(f"ffmpeg: {ffmpeg}\n输出:   {out}\n")

    for i, (name, w, h, dur, desc) in enumerate(CLIPS):
        path = os.path.join(out, name)
        if os.path.exists(path) and not args.force:
            print(f"  [skip] {name} 已存在")
            continue
        # 每段用不同的色相与运动方向，肉眼能分清是哪一镜。
        # 别用 "i * 常数" 直接当颜色：i=0 时是纯黑，第一镜就成了黑场，
        # 既难看又会让 qc 的黑场检查报警。这里固定给一个中调起点。
        r = 40 + (i * 37) % 130
        g = 40 + (i * 61) % 110
        b = 40 + (i * 89) % 140
        hue = (i * 47) % 360
        src = (f"color=c=0x{r:02x}{g:02x}{b:02x}:s={w}x{h}:d={dur}:r=30,"
               f"hue=H={hue},"
               f"drawbox=x='if(eq(mod(t,4),0),0,(t*{w}/max({dur},1))-{w})'"
               f":y=ih/3:w=iw/4:h=ih/3:color=white@0.55:t=fill,"
               f"drawtext=text='SHOT {i + 1}':fontcolor=white@0.9:fontsize={max(48, h // 12)}"
               f":x=(w-text_w)/2:y=(h-text_h)/2")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", src,
               "-f", "lavfi", "-i", f"sine=frequency={220 + i * 55}:duration={dur}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
               "-shortest", path]
        run(cmd)
        print(f"  [ok]   {name}  {w}x{h} {dur}s  {desc}")

    # 叠加层用的透明 PNG：两层描边的画框，中间必须是**真透明**——
    # 不透明的话它会把画面挡成一个黑窟窿（这是实测踩过的）。
    # 用 Pillow 而不是 ffmpeg 的 lavfi：`color=c=black@0.0` 出来的图
    # 实测是 RGBA 但 alpha 全 255，透明区被写成不透明黑。
    png = os.path.join(out, "frame.png")
    if not os.path.exists(png) or args.force:
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover - 环境缺依赖时给出可执行的指引
            raise SystemExit(
                "生成 frame.png 需要 Pillow：\n"
                "  python -m pip install pillow\n"
                "（或 python -m pip install -e \".[render]\"）")
        frame = Image.new("RGBA", (900, 1500), (0, 0, 0, 0))
        draw = ImageDraw.Draw(frame)
        draw.rectangle([8, 8, 891, 1491], outline=(255, 255, 255, 216), width=10)
        draw.rectangle([24, 24, 875, 1475], outline=(212, 175, 55, 153), width=4)
        frame.save(png)
        print("  [ok]   frame.png 900x1500 透明画框")

    bgm = os.path.join(out, "bgm.mp3")
    if not os.path.exists(bgm) or args.force:
        # 三段和弦叠一点节奏，听起来像"配乐"而不是"提示音"
        expr = ("aevalsrc=0.12*sin(2*PI*220*t)*(1+0.3*sin(2*PI*2*t))"
                "+0.09*sin(2*PI*330*t)+0.07*sin(2*PI*440*t)"
                f":s=48000:d={max(c[3] for c in CLIPS)}")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", expr,
               "-af", "tremolo=f=3.2:d=0.25,afade=t=in:d=0.4,"
                      "afade=t=out:st=13.5:d=1.0",
               "-ar", "48000", "-ac", "2", bgm]
        run(cmd)
        print("  [ok]   bgm.mp3 合成配乐")

    print(f"\n完成。下一步：\n  cutwright build "
          f"{os.path.join(os.path.dirname(out), 'shotlist.json')} -o "
          f"{os.path.join(os.path.dirname(out), 'out')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
