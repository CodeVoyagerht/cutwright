"""命令行入口。

设计原则只有一条：**每条命令都能被 agent 无障碍消费**。

* 人看的时候是中文分栏、带数字、带结论；
* 机器看的时候加 ``--json``，stdout 只有一份合法 JSON，日志走 stderr；
* 退出码说真话：0 = 可以继续，1 = 有 error 级问题，2 = 用法错误。

子命令：``doctor`` / ``backends`` / ``validate`` / ``plan`` / ``build`` /
``qc`` / ``init``。``build`` 是唯一有副作用的命令，其余全是只读。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional

from . import __version__, config, media, planner, qc
from .backends import available_backends, get_backend
from .schema import Shotlist, load_shotlist, validate
from .timeline import Timeline, TimelineError

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _print_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _err(msg: str) -> None:
    """日志一律走 stderr，保证 --json 时 stdout 干净。"""
    sys.stderr.write(msg + "\n")


def _hms(seconds: float) -> str:
    total = max(0.0, float(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m)}:{s:05.2f}"


def _parse_opts(pairs: Optional[List[str]]) -> Dict[str, Any]:
    """``--opt key=value`` → dict。值尽量按 JSON 解析，解析不了就当字符串。"""
    out: Dict[str, Any] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--opt 需要 key=value 形式，收到 {item!r}")
        key, _, raw = item.partition("=")
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            out[key.strip()] = raw
    return out


def _load(path: str) -> Shotlist:
    if not os.path.exists(path):
        raise SystemExit(f"分镜表不存在: {path}")
    try:
        return load_shotlist(path)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"分镜表不是合法 JSON: {path}\n  {exc}") from exc


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    paths = config.discover()
    data = {
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
        "paths": paths.as_dict(),
        "backends": available_backends(paths),
    }
    if paths.ffmpeg:
        try:
            data["ffmpeg_version"] = media.ffmpeg_version(paths)
        except Exception as exc:       # 找得到但跑不起来也要如实说
            data["ffmpeg_version"] = f"调用失败: {exc}"

    if args.json:
        _print_json(data)
        return 0

    print(f"cutwright {__version__}  ·  Python {data['python']}  ·  {sys.platform}")
    print("\n外部程序")
    for key, label in (("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"),
                       ("jianying_install", "剪映安装目录"),
                       ("jianying_drafts", "剪映草稿库"),
                       ("resolve_support", "达芬奇 Support")):
        value = paths.as_dict().get(key)
        src = paths.sources.get(key, "")
        mark = "OK  " if value else "MISS"
        print(f"  [{mark}] {label:12s} {value or '未找到'}"
              + (f"  ({src})" if src else ""))
    if data.get("ffmpeg_version"):
        print(f"         ffmpeg 版本  {data['ffmpeg_version']}")

    print("\n后端")
    for item in data["backends"]:
        mark = "OK  " if item.get("available") else "MISS"
        reason = item.get("reason") or item.get("description", "")
        print(f"  [{mark}] {item['name']:10s} {reason}")
    usable = [b for b in data["backends"] if b.get("available")]
    print(f"\n{len(usable)}/{len(data['backends'])} 个后端可用。")
    if not usable:
        _err("提示: 装 ffmpeg 后可先用 ffmpeg 后端出片；"
             "剪映/达芬奇后端需要对应软件已安装。")
        return 1
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    data = available_backends()
    if args.json:
        _print_json(data)
        return 0
    for item in data:
        print(f"{item['name']:10s} {'可用' if item.get('available') else '不可用'}"
              f"  {item.get('reason') or ''}")
    return 0


# --------------------------------------------------------------------------- #
# validate / plan
# --------------------------------------------------------------------------- #
def cmd_validate(args: argparse.Namespace) -> int:
    sl = _load(args.shotlist)
    issues = validate(sl, check_files=args.check_files)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warn"]
    if args.json:
        _print_json({"ok": not errors, "errors": len(errors), "warnings": len(warnings),
                     "issues": [i.to_dict() for i in issues],
                     "summary": {"shots": len(sl.video_segments),
                                 "duration": round(sl.video_duration(), 3)}})
        return 1 if errors else 0
    for issue in issues:
        print(issue)
    print(f"\n{len(errors)} 个错误，{len(warnings)} 个警告。"
          f" 镜头 {len(sl.video_segments)} 个，时长 {_hms(sl.video_duration())}。")
    return 1 if errors else 0


def cmd_plan(args: argparse.Namespace) -> int:
    sl = _load(args.shotlist)
    p = planner.Planner()
    sl, report = p.normalize(sl)
    if args.target:
        sl, fit = p.fit(sl, args.target, strategy=args.strategy)
        report.actions.extend(fit.actions)
        report.messages.extend(fit.messages)
        report.ok = report.ok and fit.ok
        report.target = fit.target
        report.achieved = fit.achieved
        report.drift = fit.drift
    else:
        report.achieved = sl.video_duration()
    report.shots = len(sl.video_segments)

    if args.out:
        _write_shotlist(sl, args.out)

    data = {"report": report.to_dict(),
            "summary": Timeline.from_shotlist(sl, strict=False).summary(),
            "out": args.out}
    if args.json:
        _print_json(data)
        return 0 if report.ok else 1

    summary = data["summary"]
    print(f"分镜表 {sl.name}：{summary['shots']} 镜 / {summary['duration_hms']}"
          f" / {summary['canvas']} @ {summary['fps']}fps")
    print(f"  平均镜长 {summary['avg_shot']}s（{summary['min_shot']}–"
          f"{summary['max_shot']}s）  字幕 {summary['texts']} 条  "
          f"旁白 {summary['vo_segments']} 段  叠加 {summary['overlays']} 层")
    if summary["per_act"]:
        print("  分幕时长: " + ", ".join(f"{k} {v}s" for k, v in summary["per_act"].items()))
    if report.target:
        pct = report.drift / report.target * 100 if report.target else 0
        print(f"  目标 {report.target:.2f}s → 实际 {report.achieved:.3f}s"
              f"（偏差 {report.drift:+.3f}s / {pct:+.2f}%）")
    for action in report.actions[:20]:
        print(f"  · {action}")
    if len(report.actions) > 20:
        print(f"  · ……另有 {len(report.actions) - 20} 条改动")
    for message in report.messages:
        _err(f"注意: {message}")
    if args.out:
        print(f"  已写出 {args.out}")
    return 0 if report.ok else 1


def _write_shotlist(sl: Shotlist, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sl.to_dict(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def cmd_build(args: argparse.Namespace) -> int:
    sl = _load(args.shotlist)
    issues = validate(sl, check_files=bool(args.check_files))
    errors = [i for i in issues if i.level == "error"]
    for issue in issues:
        if issue.level == "error" or args.verbose:
            _err(str(issue))
    if errors and not args.force:
        _err(f"有 {len(errors)} 个错误，已中止。确认要继续请加 --force。")
        return 1

    try:
        tl = Timeline.from_shotlist(sl, strict=False, check_files=bool(args.check_files))
    except TimelineError as exc:
        _err(f"分镜表无法构成时间线: {exc}")
        return 1

    # 构建前先过一遍时间线体检：宁可现在就报错，也别渲到一半才发现有空档
    health = qc.check_timeline(tl)
    blocking = [c for c in health.failed if c.severity == "error"]
    if blocking and not args.force:
        for check in blocking:
            _err(f"时间线问题 [{check.name}] {check.detail}")
        _err("已中止。确认要渲请加 --force。")
        return 1

    options = _parse_opts(args.opt)
    if args.dry_run:
        options["dry_run"] = True
    backend = get_backend(args.backend, options=options)

    if not backend.available() and not args.dry_run:
        _err(f"后端 {args.backend} 不可用：{backend.why_unavailable()}")
        _err("跑 `cutwright doctor` 看缺什么。")
        return 1

    out_dir = os.path.abspath(args.out or "out")
    os.makedirs(out_dir, exist_ok=True)
    _err(f"[{args.backend}] 开始构建 {tl.name} → {out_dir}")
    result = backend.build(tl, out_dir)

    data = {"backend": args.backend, "out_dir": out_dir,
            "result": result.to_dict(),
            "qc": health.to_dict()}

    # ffmpeg 后端出片后顺手体检成片
    mp4 = next((a for a in result.artifacts if a.lower().endswith((".mp4", ".mov"))), None)
    if mp4 and result.ok and not args.no_verify:
        rendered = qc.check_render(mp4, expect_duration=tl.duration,
                                   expect_canvas=(tl.canvas.width, tl.canvas.height))
        data["render_qc"] = rendered.to_dict()

    if args.json:
        _print_json(data)
    else:
        if result.ok:
            print(f"构建成功（{args.backend}）")
            for artifact in result.artifacts:
                print(f"  → {artifact}")
            for key, value in (result.data or {}).items():
                if isinstance(value, (str, int, float)):
                    print(f"  {key}: {value}")
        else:
            _err(f"构建失败（{args.backend}）: {result.error}")
        for warning in result.warnings:
            _err(f"警告: {warning}")
        if "render_qc" in data:
            report = data["render_qc"]
            print(f"\n成片体检: {'通过' if report['ok'] else '有问题'}")
            for check in report["checks"]:
                mark = "OK  " if check["ok"] else "FAIL"
                print(f"  [{mark}] {check['name']:16s} {check['detail']}")

    if not result.ok:
        return 1
    if "render_qc" in data and not data["render_qc"]["ok"]:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# qc
# --------------------------------------------------------------------------- #
def cmd_qc(args: argparse.Namespace) -> int:
    sl = _load(args.shotlist)
    tl = Timeline.from_shotlist(sl, strict=False, check_files=False)
    report = qc.run_all(tl, args.mp4)
    if args.json:
        _print_json(report.to_dict())
        return 0 if report.ok else 1
    print(f"时间线 {tl.name}  {len(tl.clips)} 镜 / {_hms(tl.duration)}")
    for check in report.checks:
        mark = "OK  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name:16s} {check.detail}")
    if not report.ok:
        _err(f"\n{len(report.failed)} 项未通过。")
        return 1
    print("\n全部通过。")
    return 0


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #
_EXAMPLE_FILES = ("shotlist.json", "README.md")


def cmd_init(args: argparse.Namespace) -> int:
    target = os.path.abspath(args.dir)
    os.makedirs(target, exist_ok=True)
    material_dir = os.path.abspath(args.materials or os.path.join(target, "materials"))
    os.makedirs(material_dir, exist_ok=True)

    sl = Shotlist.from_dict({
        "name": args.name,
        "material_dir": material_dir,
        "canvas": {"width": 1080, "height": 1920},
        "fps": 30,
        "styles": {k: v.to_dict() for k, v in
                   planner.default_styles().items()},
        "video_segments": [
            {"file": "clip01.mp4", "src_start": 0, "src_dur": 3.0, "desc": "开场"},
            {"file": "clip02.mp4", "src_start": 1.5, "src_dur": 2.5,
             "zoom": "in", "zoom_amount": 0.08, "desc": "推进"},
            {"file": "clip03.mp4", "src_start": 0, "src_dur": 3.0,
             "cx": 0.4, "desc": "偏左构图"},
        ],
        "text_segments": [
            {"text": "这样拍|竖屏才好看", "tl_start": 0.4, "tl_dur": 2.2,
             "style": "caption"},
            {"text": "第一步", "tl_start": 3.0, "tl_dur": 1.6, "style": "huazi"},
        ],
        "vo_segments": [
            {"text": "先站稳，再取景。", "tl_start": 0.5, "tl_dur": 3.0,
             "style": "vo_hidden"},
        ],
        "image_overlays": [
            {"file": "frame.png", "tl_start": 5.2, "tl_dur": 2.0, "fade": 0.2},
        ],
        "audio_segments": [
            {"file": "bgm.mp3", "tl_start": 0, "tl_dur": 8.5, "kind": "music"},
        ],
    })
    sl, _ = planner.normalize_shotlist(sl, trust_existing=False)
    shotlist_path = os.path.join(target, "shotlist.json")
    _write_shotlist(sl, shotlist_path)

    readme = os.path.join(target, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(_SCAFFOLD_README.format(name=args.name, materials=material_dir))

    if args.json:
        _print_json({"dir": target, "shotlist": shotlist_path,
                     "material_dir": material_dir,
                     "files": list(_EXAMPLE_FILES)})
        return 0
    print(f"已在 {target} 生成：")
    print(f"  shotlist.json   分镜表（3 镜 / 2 字幕 / 1 花字 / 1 旁白 / 1 叠加 / 1 配乐）")
    print(f"  README.md       接下来怎么走")
    print(f"  materials/      把素材放这里（{material_dir}）")
    print("\n下一步：")
    print(f"  cutwright plan     {shotlist_path}")
    print(f"  cutwright build    {shotlist_path} --backend ffmpeg -o out")
    return 0


_SCAFFOLD_README = """# {name}

这个目录由 `cutwright init` 生成。

## 放素材
把视频拷进 `{materials}`，文件名改成 `shotlist.json` 里 `video_segments[].file`
引用的名字（默认 `clip01.mp4` / `clip02.mp4` / `clip03.mp4`）。
配乐放 `bgm.mp3`，叠加用的透明 PNG 放 `frame.png`——缺哪个就先把 `shotlist.json`
里对应的段落删掉，validate 会告诉你还缺什么。

## 三步走
```bash
cutwright validate shotlist.json       # 先看有没有错误
cutwright plan     shotlist.json       # 看镜头/时长/分幕统计
cutwright build    shotlist.json --backend ffmpeg -o out
```

后端换成 `jianying` 会生成剪映草稿，换成 `resolve` 会建达芬奇时间线。
`cutwright doctor` 告诉你本机哪几个后端可用。

## 改分镜表的三条经验
1. `tl_start` 可以不写——不写就按顺序自动首尾相接。
2. 字幕里 `|` 后面是关键词，会用 `keyword_color` 换色。
3. 旁白只出声不出字：样式必须 `invisible`，否则会糊在画面上（qc 会报错）。
"""


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cutwright", description="剪匠：分镜表进，成片出。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  cutwright doctor
  cutwright validate shotlist.json --check-files
  cutwright plan shotlist.json --target 45
  cutwright build shotlist.json --backend ffmpeg -o out
  cutwright build shotlist.json --backend jianying --opt install=true
  cutwright qc shotlist.json --mp4 out/demo.mp4
""")
    parser.add_argument("--version", action="version", version=f"cutwright {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
        sp.set_defaults(func=func)
        return sp

    add("doctor", "检查 ffmpeg / 剪映 / 达芬奇 是否就绪", cmd_doctor)
    add("backends", "列出后端及可用性", cmd_backends)

    sp = add("validate", "静态检查分镜表", cmd_validate)
    sp.add_argument("shotlist", help="分镜表 JSON 路径")
    sp.add_argument("--check-files", action="store_true", help="同时检查素材文件是否存在")

    sp = add("plan", "统计与时长拟合", cmd_plan)
    sp.add_argument("shotlist")
    sp.add_argument("--target", type=float, help="目标总时长（秒）")
    sp.add_argument("--strategy", choices=["trim", "speed"], default="trim",
                    help="拟合策略：trim=改剪点，speed=改变速（默认 trim）")
    sp.add_argument("--out", help="把规整后的分镜表写到这个路径")

    sp = add("build", "构建目标产物（草稿/时间线/成片）", cmd_build)
    sp.add_argument("shotlist")
    sp.add_argument("--backend", "-b", default="ffmpeg",
                    help="ffmpeg | jianying | resolve（默认 ffmpeg）")
    sp.add_argument("--out", "-o", default="out", help="输出目录（默认 out/）")
    sp.add_argument("--opt", action="append", metavar="KEY=VALUE",
                    help="传给后端的高级选项，可重复")
    sp.add_argument("--dry-run", action="store_true", help="只出计划不落盘")
    sp.add_argument("--force", action="store_true", help="有 error 也继续")
    sp.add_argument("--check-files", action="store_true", help="构建前检查素材存在")
    sp.add_argument("--no-verify", action="store_true", help="出片后跳过成片体检")
    sp.add_argument("--verbose", "-v", action="store_true", help="打印全部校验信息")

    sp = add("qc", "时间线 / 成片体检", cmd_qc)
    sp.add_argument("shotlist")
    sp.add_argument("--mp4", help="要体检的成片路径（给了才查第二层）")

    sp = add("init", "生成一份可用的示例分镜表", cmd_init)
    sp.add_argument("dir", help="生成到哪个目录")
    sp.add_argument("--name", default="demo", help="分镜表名（默认 demo）")
    sp.add_argument("--materials", help="素材目录（默认 <dir>/materials）")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:              # argparse 用法错误/--help → 原样返回
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _err("已中断。")
        return 130
    except SystemExit as exc:
        # 命令内部用 ``raise SystemExit("原因")`` 表示"停在这里"。它不是错误
        # 回溯，所以只把原因写到 stderr，退出码 1。整数码原样透传。
        code = exc.code
        if isinstance(code, int):
            return code
        if code:
            _err(str(code))
        return 1
    except Exception as exc:                # 最后兜底：给 traceback 式的可读信息
        if getattr(args, "json", False):
            _print_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            _err(f"出错: {type(exc).__name__}: {exc}")
        if os.environ.get("CUTWRIGHT_DEBUG"):
            raise
        return 1


if __name__ == "__main__":          # pragma: no cover
    sys.exit(main())
