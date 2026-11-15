"""给 ``examples/story`` 造一批「有语义文件名」的素材。

剧本层的素材匹配靠**文件名里的标签**（``S01_subway_night_wide.mp4`` →
``subway`` / ``night`` / ``wide``），所以这个脚本的重点不是画面好看，而是名字
起对：每条素材一个机位、一个情绪，标签写在名字里，``cutwright index`` 才切得准。

用法::

    python make_media.py                # 缺什么补什么（已存在的跳过）
    python make_media.py --force        # 全部重做
    python make_media.py --out materials

画面是 ffmpeg lavfi 现画的纯色 + 机位文字，每段 4 秒；另生成一条 16 秒配乐。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from cutwright import config, demo  # noqa: E402

#: (文件名, 时长, 画面基色索引) —— 名字里的标签就是剧本用来匹配的标签
CLIPS = [
    ("S01_subway_night_wide", 4.0, 0),      # 站台全景
    ("S01_subway_night_close", 3.5, 1),     # 站台近景
    ("S02_train_close_lin", 4.0, 2),        # 车厢 · 林队
    ("S02_train_close_zhou", 3.5, 3),       # 车厢 · 周小满
    ("S03_exit_night_wide", 4.0, 4),        # 出口全景
    ("S03_exit_night_detail", 3.0, 5),      # 出口细节
]

BGM_SECONDS = 16.0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="造 examples/story 的演示素材")
    parser.add_argument("--out", default=os.path.join(_HERE, "materials"))
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    parser.add_argument("--no-bgm", action="store_true", help="不生成配乐")
    args = parser.parse_args(argv)

    ffmpeg = demo.find_ffmpeg("")
    if not ffmpeg:
        print("找不到 ffmpeg：装一个并放进 PATH，或用 CUTWRIGHT_FFMPEG 指定路径。",
              file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)

    made, skipped = [], []
    for index, (stem, seconds, color_index) in enumerate(CLIPS):
        path = os.path.join(args.out, f"{stem}.mp4")
        if os.path.exists(path) and not args.force:
            skipped.append(os.path.basename(path))
            continue
        demo.make_clip(ffmpeg, path, color_index, 1080, 1920, seconds,
                       force=args.force)
        made.append(os.path.basename(path))

    if not args.no_bgm:
        bgm = os.path.join(args.out, "bgm.mp3")
        if args.force or not os.path.exists(bgm):
            demo.make_bgm(ffmpeg, bgm, BGM_SECONDS, force=args.force)
            made.append("bgm.mp3")
        else:
            skipped.append("bgm.mp3")

    print(f"生成 {len(made)} 个文件 → {args.out}")
    for name in made:
        print(f"  + {name}")
    if skipped:
        print(f"跳过 {len(skipped)} 个已存在的（--force 可覆盖）")
    print("\n下一步:")
    print(f"  cutwright index {os.path.relpath(args.out)} -o materials.json")
    print("  cutwright story screenplay.json --index materials.json -o shotlist.json")
    print("  cutwright build shotlist.json -o out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
