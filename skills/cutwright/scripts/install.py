#!/usr/bin/env python3
"""把 cutwright 技能装到各家的 skill 目录。

各家读的位置不一样（而且 Codex 与 Gemini 更认 ``.agents/skills``），所以做法是
**一份真源、多处落地**：本目录永远是唯一真源，安装只是一次复制（默认）或软链。

    python scripts/install.py                 # 装到本机所有已知位置
    python scripts/install.py --target claude # 只装 Claude Code
    python scripts/install.py --link          # 用软链（仅 POSIX / 开了开发者模式的 Windows）
    python scripts/install.py --dir D:/skills # 装到自定义目录的父级
    python scripts/install.py --dry-run       # 只看会写哪

设计取舍：Windows 上默认**复制**而不是软链。软链要管理员或开发者模式，
一个"装不上"的技能比"装了两份、要记得重新同步"的技能更糟。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)          # .../skills/cutwright
SKILL_NAME = os.path.basename(SKILL_DIR)
HOME = os.path.expanduser("~")

# (键, 说明, 目标父目录)。同一份技能可能被多处读，重复装不冲突。
TARGETS = [
    ("dsh", "DeepSeek Harness（配合 preset/ 里的「剪匠」预设）",
     os.path.join(HOME, ".dsh", "skills")),
    ("agents", "通用 .agents（Cursor / Codex / Gemini 原生识别）",
     os.path.join(HOME, ".agents", "skills")),
    ("claude", "Claude Code", os.path.join(HOME, ".claude", "skills")),
    ("cursor", "Cursor", os.path.join(HOME, ".cursor", "skills")),
    ("codex", "Codex", os.path.join(HOME, ".codex", "skills")),
    ("gemini", "Gemini CLI", os.path.join(HOME, ".gemini", "skills")),
]


def _install_one(dest_root: str, *, link: bool, dry: bool,
                 force: bool) -> str:
    dest = os.path.join(dest_root, SKILL_NAME)
    if os.path.exists(dest):
        if not force:
            return f"跳过（已存在）: {dest}"
        if not dry:
            shutil.rmtree(dest, ignore_errors=True)
    if dry:
        return f"会写入: {dest}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if link:
        try:
            os.symlink(SKILL_DIR, dest, target_is_directory=True)
            return f"已软链: {dest} → {SKILL_DIR}"
        except OSError as exc:            # 权限不足就诚实退回复制
            print(f"软链失败（{exc}），改为复制。", file=sys.stderr)
    shutil.copytree(SKILL_DIR, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".tmp"))
    return f"已复制: {dest}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安装 cutwright 技能")
    ap.add_argument("--target", default="all",
                    help="all | " + " | ".join(k for k, _, _ in TARGETS) + " | dir")
    ap.add_argument("--dir", help="--target dir 时的父目录")
    ap.add_argument("--link", action="store_true", help="用软链代替复制")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的安装")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(os.path.join(SKILL_DIR, "SKILL.md")):
        print(f"这里不是技能目录: {SKILL_DIR}", file=sys.stderr)
        return 1

    if args.target == "dir":
        if not args.dir:
            print("--target dir 需要同时给 --dir", file=sys.stderr)
            return 2
        roots = [("custom", "自定义目录", os.path.abspath(args.dir))]
    elif args.target == "all":
        roots = TARGETS
    else:
        roots = [t for t in TARGETS if t[0] == args.target]
        if not roots:
            print(f"未知 target: {args.target}", file=sys.stderr)
            return 2

    print(f"真源: {SKILL_DIR}")
    count = 0
    for _key, label, root in roots:
        print(f"\n{label}")
        print("  " + _install_one(root, link=args.link, dry=args.dry_run,
                                  force=args.force))
        count += 1
    print(f"\n{count} 个位置处理完毕"
          + ("（dry-run，未真正写入）" if args.dry_run else "") + "。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
