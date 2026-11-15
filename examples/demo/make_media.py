#!/usr/bin/env python3
"""生成一套能直接跑通的演示素材（不需要你有任何视频）。

    python examples/demo/make_media.py --out examples/demo/materials
    cutwright build examples/demo/shotlist.json -o examples/demo/out

素材合成逻辑放在 ``cutwright.demo`` 里，``cutwright init --with-media`` 用的是
同一份代码——两条路不能各造一套，否则 demo 能跑而 init 造出来的跑不了（或者
反过来），这种分叉最难查。
"""
from __future__ import annotations

import argparse
import os
import sys

# 配乐要比成片(13.8s)长一点，否则音乐会在中途停住。这个数字跟着 shotlist
# 的总时长走，改分镜表时一起改。
BGM_SECONDS = 14.0


def _bootstrap_src() -> None:
    """让脚本在没 pip install 的仓库里也能 import 到 cutwright。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(os.path.dirname(os.path.dirname(here)), "src")
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成演示素材")
    ap.add_argument("--out", default="examples/demo/materials")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    _bootstrap_src()
    from cutwright import demo

    try:
        report = demo.make_demo_media(
            args.out, clips=demo.PREVIEW_CLIPS, ffmpeg=args.ffmpeg,
            force=args.force, bgm_duration=BGM_SECONDS,
            log=lambda line: print(line))
    except demo.DemoMediaError as exc:
        raise SystemExit(str(exc))

    out = os.path.abspath(args.out)
    print(f"\n完成：{len(report['created'])} 个新造、"
          f"{len(report['skipped'])} 个已存在。")
    root = os.path.dirname(out)
    print("下一步：\n"
          f"  cutwright validate {os.path.join(root, 'shotlist.json')} --check-files\n"
          f"  cutwright build    {os.path.join(root, 'shotlist.json')} -o "
          f"{os.path.join(root, 'out')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
