#!/usr/bin/env python3
"""一步验收：validate → plan → qc（可选成片体检），退出码即结论。

这是给 agent 和 CI 用的**硬门禁**。它不产出任何建议，只回答一个问题：
"这份分镜表现在能不能交付？" 能就 ``0``，不能就 ``1``，并把第一条致命错
原样回显。

    python scripts/verify.py shotlist.json
    python scripts/verify.py shotlist.json --target 45 --mp4 out/demo.mp4 --json

刻意写得比 `cutwright qc` 更啰嗦：它会**逐层**跑、任何一层不过就停，
因为 agent 最需要的不是"综合评分"，而是"下一个该修哪一行"。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _cli_base():
    """找到能跑 cutwright 的方式。

    优先用装好的 ``cutwright`` 命令（技能被单独分发时就是这种情形）；
    退回到仓库里的模块（开发态、CI）；都不行就明确报错而不是假装成功。
    """
    import shutil
    exe = shutil.which("cutwright")
    if exe:
        return [exe]
    src = os.path.join(REPO, "src")
    if os.path.isdir(os.path.join(src, "cutwright")):
        return [sys.executable, "-m", "cutwright.cli"]
    return None


def _run(base, args, want_json=True):
    cmd = list(base) + list(args)
    if want_json and "--json" not in cmd:
        cmd.append("--json")
    env = dict(os.environ)
    src = os.path.join(REPO, "src")
    if os.path.isdir(os.path.join(src, "cutwright")):
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"]
                                   if env.get("PYTHONPATH") else "")
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    payload = None
    if want_json:
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
    return proc.returncode, payload, (proc.stdout or ""), (proc.stderr or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="分镜表交付门禁")
    ap.add_argument("shotlist")
    ap.add_argument("--target", type=float, default=None,
                    help="顺带做时长拟合检查（秒）")
    ap.add_argument("--mp4", default=None, help="顺带体检这个成片")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args(argv)

    base = _cli_base()
    if not base:
        print("找不到 cutwright：先 `pip install -e .`，或从仓库根跑。",
              file=sys.stderr)
        return 1
    if not os.path.exists(args.shotlist):
        print(f"分镜表不存在: {args.shotlist}", file=sys.stderr)
        return 1

    steps = []

    # ① 静态检查（连素材一起查——少了文件的片子渲出来也是废的）
    code, data, out, err = _run(base, ["validate", args.shotlist, "--check-files"])
    issues = (data or {}).get("issues") or []
    fatal = [i for i in issues if i.get("level") == "error"]
    steps.append({"step": "validate", "ok": code == 0, "code": code,
                  "errors": len(fatal), "issues": issues[:12]})
    if code != 0:
        return _finish(steps, args, first=fatal[0] if fatal else None)

    # ② 统计（这一步几乎不会失败，但没有它就拿不到时长基准）
    plan_args = ["plan", args.shotlist]
    if args.target:
        plan_args += ["--target", str(args.target)]
    code, data, out, err = _run(base, plan_args)
    steps.append({"step": "plan", "ok": code == 0, "code": code,
                  "summary": (data or {}).get("summary"),
                  "report": (data or {}).get("report")})
    if code != 0:
        return _finish(steps, args, first=None)

    # ③ 时间线体检
    qc_args = ["qc", args.shotlist]
    code, data, out, err = _run(base, qc_args)
    failed = [c for c in ((data or {}).get("checks") or []) if not c.get("ok")]
    steps.append({"step": "qc", "ok": code == 0, "code": code,
                  "failed": failed})
    if code != 0:
        return _finish(steps, args, first=failed[0] if failed else None)

    # ④ 成片体检（可选，只有给了 --mp4 才跑）
    if args.mp4:
        code, data, out, err = _run(base, ["qc", args.shotlist, "--mp4", args.mp4])
        failed = [c for c in ((data or {}).get("checks") or []) if not c.get("ok")]
        steps.append({"step": "render_qc", "ok": code == 0, "code": code,
                      "failed": failed})
        if code != 0:
            return _finish(steps, args, first=failed[0] if failed else None)

    return _finish(steps, args, first=None)


def _finish(steps, args, first) -> int:
    ok = all(s["ok"] for s in steps)
    if args.json:
        print(json.dumps({"ok": ok, "steps": steps,
                          "first_failure": first}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    if not args.quiet:
        for step in steps:
            mark = "OK  " if step["ok"] else "FAIL"
            print(f"[{mark}] {step['step']:<10} exit={step['code']}")
        if not ok and first:
            print(f"\n第一个要修的问题:\n  {first.get('code', '')} "
                  f"{first.get('message') or first.get('detail', '')}",
                  file=sys.stderr)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
