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

from . import __version__, config, demo, media, planner, qc
from . import screenplay as screenplay_mod
from . import story as story_mod
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


def _mark(check: Any) -> str:
    """一条体检结论的标记。

    **标记必须和定级一致**：error 级的失败才叫 FAIL，warn 级的失败叫 WARN。
    以前一律打 FAIL、却按 warn 定级，于是出现「打印 FAIL、结论通过」的自相矛盾。

    吃两种喂法：``build`` 的成片体检拿到的是 ``report["checks"]``（dict），
    ``qc`` 拿到的是 ``QcCheck`` 对象——所以不能假定是 dict。
    """
    def field(name: str, default: Any = None) -> Any:
        if isinstance(check, dict):
            return check.get(name, default)
        return getattr(check, name, default)

    if field("ok"):
        return "OK  "
    return "FAIL" if field("severity", "error") == "error" else "WARN"


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
def _print_plan(plan: Dict[str, Any]) -> None:
    """把 ``--dry-run`` 的计划打成人能读的样子。

    JSON 里本来就有完整字段，但没人愿意对着字典数「第几段用哪一秒」；
    ``--dry-run`` 存在的意义就是先看一眼，所以这里把它摊成一张表。
    """
    shots = plan.get("shots") or []
    if not shots:
        return
    speed = "（--fast 预览档）" if plan.get("preview") else ""
    print(f"\n计划: {len(shots)} 段切片 → {plan.get('output', '')}")
    print(f"  画布 {plan.get('canvas', '?')} @ {plan.get('fps', '?')}fps，"
          f"成片 {plan.get('duration', '?')}s{speed}")
    for shot in shots:
        print(f"  [{shot['index']}] {shot['name']:<16s}"
              f" 入点 {shot['src_in']:>7.3f}s +{shot['src_dur']:.2f}s"
              f"  → {shot['segment']}")
    print("  （切片名带内容摘要：名字一样就是能直接复用的缓存）")


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
    if args.fast:
        # 快速预览走后端已有的 preview 档（切片 veryfast/crf28、成片 veryfast/crf26），
        # 比交付档快一个量级；切片摘要里带着这面旗，所以预览切片不会被当成成品复用。
        options["preview"] = True
    if args.dry_run:
        options["dry_run"] = True
    backend = get_backend(args.backend, options=options)

    if not backend.available() and not args.dry_run:
        _err(f"后端 {args.backend} 不可用：{backend.why_unavailable()}")
        _err("跑 `cutwright doctor` 看缺什么。")
        return 1

    out_dir = os.path.abspath(args.out or "out")
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
    if args.dry_run:
        _err(f"[{args.backend}] 预演 {tl.name}（不渲染）→ {out_dir}")
    else:
        _err(f"[{args.backend}] 开始构建 {tl.name} → {out_dir}")
    result = backend.build(tl, out_dir)

    data = {"backend": args.backend, "out_dir": out_dir,
            "preview": bool(options.get("preview")),
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
        if options.get("dry_run"):
            # 预演到此为止：它不是「失败」，也不该掉进下面那条失败分支
            _print_plan(data["result"]["data"])
        elif result.ok:
            print(f"构建成功（{args.backend}）")
            for artifact in result.artifacts:
                print(f"  → {artifact}")
            for key, value in (result.data or {}).items():
                if isinstance(value, (str, int, float)):
                    print(f"  {key}: {value}")
        else:
            _err(f"构建失败（{args.backend}）: {result.error}")
        if options.get("preview"):
            _err("这是 --fast 快速预览（编码更快、画质略降）。要交付就用同一条命令去掉 --fast 再渲一次。")
        for warning in result.warnings:
            _err(f"警告: {warning}")
        if "render_qc" in data:
            report = data["render_qc"]
            warns = sum(1 for c in report["checks"]
                        if not c["ok"] and c["severity"] != "error")
            print("\n成片体检: " + ("通过" if report["ok"] else "有问题")
                  + (f"（{warns} 条警告）" if warns and report["ok"] else ""))
            for check in report["checks"]:
                print(f"  [{_mark(check)}] {check['name']:16s} {check['detail']}")

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
        print(f"  [{_mark(check)}] {check.name:16s} {check.detail}")
    warns = [c for c in report.checks if not c.ok and c.severity != "error"]
    if not report.ok:
        _err(f"\n{len(report.failed)} 项未通过。")
        return 1
    if warns:
        print(f"\n通过，但有 {len(warns)} 条警告（不影响退出码）："
              + "、".join(c.name for c in warns))
        return 0
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
        # 画框 PNG 与配乐跟视频放同一个目录，否则 validate --check-files 会
        # 报 E041/E051——素材目录分不分家是使用者的选择，默认不分。
        "asset_dir": material_dir,
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

    made: List[str] = []
    if args.with_media:
        try:
            total = Timeline.from_shotlist(sl).duration
            report = demo.make_demo_media(
                material_dir, clips=demo.SCAFFOLD_CLIPS,
                bgm_duration=total + 0.5, log=lambda m: _err(m))
            made = report["created"]
        except demo.DemoMediaError as exc:
            _err(f"演示素材没造出来：{exc}")
            return 1

    if args.json:
        _print_json({"dir": target, "shotlist": shotlist_path,
                     "material_dir": material_dir,
                     "files": list(_EXAMPLE_FILES) + [os.path.basename(p) for p in made]})
        return 0
    print(f"已在 {target} 生成：")
    print(f"  shotlist.json   分镜表（3 镜 / 2 字幕 / 1 花字 / 1 旁白 / 1 叠加 / 1 配乐）")
    print(f"  README.md       接下来怎么走")
    if made:
        print(f"  materials/      {len(made)} 个合成素材（可以立刻 build）")
    else:
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

不想自己找素材？生成时加 `--with-media`，它会用 ffmpeg 现场合成这三段片子、
那张透明画框和一条配乐，直接就能 build 出一条真片子看效果。

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
2. 字幕里用 `|`（半角或全角 `｜`）隔开的是关键词，**成对交替**换色：
   `"这样拍|竖屏才好看"` → 白 / 黄；`"A|B|C"` → 白 / 黄 / 白。分隔符本身不印出来。
3. 旁白只出声不出字：样式必须 `invisible`，否则会糊在画面上（qc 会报错）。
"""


# --------------------------------------------------------------------------- #
# 剧本 → 分镜表
# --------------------------------------------------------------------------- #
def cmd_index(args: argparse.Namespace) -> int:
    if not os.path.isdir(args.materials):
        raise SystemExit(f"素材目录不存在: {args.materials}")
    index = story_mod.MaterialIndex.from_dir(
        args.materials, scene_threshold=args.scene_threshold,
        min_shot=args.min_shot, max_shot=args.max_shot, paths=config.discover())
    if not index.pieces:
        if args.json:
            _print_json({"ok": False, "pieces": 0, "error": "切不出可用镜头"})
        else:
            _err(f"切不出可用镜头：检查 {args.materials} 里有没有视频，"
                 f"或调小 --min-shot / 降低 --scene-threshold")
        return 1
    index.save(args.out)
    tags: Dict[str, int] = {}
    for piece in index.pieces:
        for tag in piece.tags:
            tags[tag] = tags.get(tag, 0) + 1
    if args.json:
        _print_json({"ok": True, "pieces": len(index.pieces), "out": args.out,
                     "tags": tags})
        return 0
    print(f"素材索引 {args.out}：{len(index.pieces)} 个镜头")
    if tags:
        top = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)[:12]
        print("  标签: " + ", ".join(f"{k}×{v}" for k, v in top))
    else:
        print("  没有标签——文件名里没带语义（如 `S01_subway_night.mp4` 会带出 subway/night）")
    return 0


def cmd_story(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.screenplay):
        raise SystemExit(f"剧本不存在: {args.screenplay}")
    screenplay = screenplay_mod.load_screenplay(args.screenplay)
    issues = screenplay_mod.validate_screenplay(screenplay, check_files=args.check_files)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warn"]
    if errors:
        if args.json:
            _print_json({"ok": False, "issues": [i.to_dict() for i in issues]})
        else:
            for issue in issues:
                print(issue)
            _err(f"\n剧本有 {len(errors)} 个错误，先修掉再排片。")
        return 1

    options = story_mod.StoryOptions(
        target_duration=args.target,
        vo_captions=args.vo_captions,
        captions=not args.no_captions,
        paths=config.discover(),
    )
    if args.cps:
        options.cps_dialogue = args.cps
        options.cps_voiceover = args.cps * 0.92
    if args.min_shot:
        options.min_shot = args.min_shot
    if args.max_shot:
        options.max_shot = args.max_shot
    if args.pause is not None:
        options.pause_default = args.pause

    index = story_mod.read_index(screenplay, args.index or "", args.material or "", options)
    if not index.pieces:
        detail = ("素材索引是空的" if args.index else
                  "既没给素材目录，剧本里也没写 material_dir")
        if args.json:
            _print_json({"ok": False, "error": detail})
        else:
            _err(f"排不了：{detail}。用 --material <目录> 或 --index materials.json 指定。")
        return 1

    shotlist, report = story_mod.build_shotlist(screenplay, index, options)
    if args.target:
        shotlist, fit = planner.fit_duration(shotlist, args.target,
                                             paths=options.paths)
        report.warnings.extend(fit.messages)
        report.duration = round(shotlist.video_duration(), 3)
        report.shots = len(shotlist.video_segments)

    shotlist, _ = planner.normalize_shotlist(shotlist, trust_existing=True)
    issues_sl = validate(shotlist)
    sl_errors = [i for i in issues_sl if i.level == "error"]
    _write_shotlist(shotlist, args.out)

    if args.json:
        _print_json({"ok": report.ok and not sl_errors, "report": report.to_dict(),
                     "out": args.out,
                     "issues": [i.to_dict() for i in issues_sl]})
        return 0 if report.ok and not sl_errors else 1

    print(f"剧本 {screenplay.name} → {args.out}")
    print(f"  {report.scenes} 场 / {report.lines} 句 → {report.shots} 镜 / "
          f"{_hms(report.duration)}")
    for item in report.per_scene[:12]:
        print(f"    {item['id']:<6} {item['dur']:>7.2f}s  {item['shots']} 镜  {item['slug']}")
    if len(report.per_scene) > 12:
        print(f"    ……另有 {len(report.per_scene) - 12} 场")
    print(f"  字幕 {len(shotlist.text_segments)} 条  旁白 {len(shotlist.vo_segments)} 段  "
          f"音频 {len(shotlist.audio_segments)} 条")
    for note in report.rhythm:
        print(f"  节奏建议: {note}")
    if report.reuse:
        worst = next(iter(report.reuse.items()))
        top = ", ".join(f"{name}×{n}" for name, n in report.reuse.items())
        print(f"  素材复用 {len(index.pieces)} 段撑 {report.shots} 镜（最多 {worst[1]} 次）: {top}")
        if worst[1] >= 6:
            print("    同一段素材用 6 次以上，观众会认出来；补拍几段本场素材再排一次更耐看。")
    for warning in report.warnings[:8]:
        _err(f"注意: {warning}")
    if len(report.warnings) > 8:
        _err(f"注意: ……另有 {len(report.warnings) - 8} 条")
    for issue in sl_errors[:5]:
        print(issue)
    if sl_errors:
        _err(f"排出来的分镜表有 {len(sl_errors)} 个错误（已写出，修完再 build）")
        return 1
    print(f"  下一步: cutwright build {args.out} -o out")
    return 0


# --------------------------------------------------------------------------- #
# 剪映草稿 → 分镜表
# --------------------------------------------------------------------------- #
def cmd_import(args: argparse.Namespace) -> int:
    from .backends.jianying import reader as jy_reader

    if not os.path.isdir(args.draft):
        raise SystemExit(f"草稿目录不存在: {args.draft}")
    try:
        draft = jy_reader.read_draft(
            args.draft, crypto=False if args.plain_only else None)
    except jy_reader.DraftError as error:
        if args.json:
            _print_json({"ok": False, "error": str(error)})
        else:
            _err(f"读不了这份草稿：{error}")
        return 1

    shotlist = draft.to_shotlist()
    shotlist.name = args.name or shotlist.name
    shotlist, _ = planner.normalize_shotlist(shotlist, trust_existing=True)
    issues = validate(shotlist, check_files=False)
    errors = [i for i in issues if i.level == "error"]
    _write_shotlist(shotlist, args.out)
    missing = draft.missing_files()

    if args.json:
        _print_json({"ok": not errors, "summary": draft.summary(), "out": args.out,
                     "shots": len(draft.videos), "texts": len(draft.texts),
                     "audios": len(draft.audios), "encrypted": draft.encrypted,
                     "missing": missing, "lost": draft.lost,
                     "warnings": draft.warnings + [str(i) for i in errors]})
        return 0 if not errors else 1

    print(draft.summary())
    print(f"  → {args.out}：{len(shotlist.video_segments)} 镜 / "
          f"{len(shotlist.text_segments)} 条字幕 / {len(shotlist.audio_segments)} 条音频 / "
          f"{_hms(shotlist.video_duration())}")
    if draft.lost:
        print("  不还原: " + "、".join("%s %d 个" % (k, v)
                                       for k, v in draft.lost.items()))
    for warning in draft.warnings[:6]:
        _err(f"注意: {warning}")
    if missing:
        _err(f"注意: {len(missing)} 个素材文件已经不在原路径了（换机器就会这样），"
             f"例如 {missing[0]}")
        _err("      把这些素材放回去，或改分镜表里的 file 再 build。")
    for issue in errors[:5]:
        print(issue)
    if errors:
        _err(f"读出来的分镜表有 {len(errors)} 个错误（已写出，修完再 build）")
        return 1
    if not shotlist.video_segments:
        _err("这份草稿没有视频段，分镜表是空的。")
        return 1
    print(f"  下一步: cutwright validate {args.out} --check-files")
    return 0


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
    sp.add_argument("--fast", action="store_true",
                    help="快速预览：切片/成片都用更快的编码参数（画质略降，别拿去交付）")
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
    sp.add_argument("--with-media", action="store_true",
                    help="连素材一起合成（需要 ffmpeg；生成后可以直接 build）")

    sp = add("index", "扫素材目录，建一份带标签的素材索引", cmd_index)
    sp.add_argument("materials", help="素材目录")
    sp.add_argument("--out", "-o", default="materials.json", help="索引写到哪（默认 materials.json）")
    sp.add_argument("--scene-threshold", type=float, default=0.4,
                    help="场景切点灵敏度，越小越敏感（默认 0.4）")
    sp.add_argument("--min-shot", type=float, default=0.8, help="最短镜头（默认 0.8s）")
    sp.add_argument("--max-shot", type=float, default=6.0, help="最长镜头（超出会被均分，默认 6s）")

    sp = add("story", "剧本 → 分镜表（短剧/剧情片用这条）", cmd_story)
    sp.add_argument("screenplay", help="剧本 JSON 路径")
    sp.add_argument("--out", "-o", default="shotlist.json", help="分镜表写到哪")
    sp.add_argument("--material", help="素材目录（默认取剧本里的 material_dir）")
    sp.add_argument("--index", help="现成的素材索引 JSON（不给就扫目录）")
    sp.add_argument("--cps", type=float, help="台词字数/秒，覆盖默认 4.6")
    sp.add_argument("--target", type=float, help="目标总时长（秒），会做时长拟合")
    sp.add_argument("--min-shot", type=float, help="最短镜头（默认 1.0s）")
    sp.add_argument("--max-shot", type=float, help="最长镜头（默认 3.6s）")
    sp.add_argument("--pause", type=float, help="每句后面的气口（默认 0.12s）")
    sp.add_argument("--no-captions", action="store_true", help="不出字幕")
    sp.add_argument("--vo-captions", action="store_true", help="旁白也出字幕（默认不出）")
    sp.add_argument("--check-files", action="store_true", help="同时检查旁白/配乐文件是否存在")

    sp = add("import", "剪映草稿 → 分镜表（读回来改成数据，再换个后端出片）", cmd_import)
    sp.add_argument("draft", help="剪映草稿目录（里面有 draft_content.json 的那层）")
    sp.add_argument("--out", "-o", default="shotlist.json", help="分镜表写到哪")
    sp.add_argument("--name", help="分镜表名字（默认用草稿目录名）")
    sp.add_argument("--plain-only", action="store_true",
                    help="只读明文草稿，绝不加载 videoeditor.dll（解不开就报错退出）")

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
