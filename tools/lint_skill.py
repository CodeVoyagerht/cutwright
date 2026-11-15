#!/usr/bin/env python3
"""校验 SKILL.md 是否符合 Agent Skills 规范（CI 里跑）。

这个脚本的存在理由：技能的 frontmatter 写错、目录名与 ``name`` 不一致、
正文太长——这类问题不会在运行时抛异常，它表现为**加载器不认这个技能**，
或者打包到 claude.ai 时直接报错。除了静态检查，没有别的一层会拦住它。

    python tools/lint_skill.py                  # 检查仓库里的所有技能
    python tools/lint_skill.py --json           # 给 CI 用
    python tools/lint_skill.py skills/cutwright # 只查一个

检查项（都对不上就退出码 1）：

1. ``skills/<name>/SKILL.md`` 存在，且 frontmatter 能被解析。
2. ``name`` 与父目录名**逐字相同**；全小写、连字符、≤64 字符、不以 ``-`` 起止、不出现 ``--``。
3. frontmatter **只含规范字段**：name / description / license / compatibility /
   metadata / allowed-tools。多写扩展字段在 claude.ai 打包时会硬报错。
4. ``description`` 1–1024 字符，且包含触发提示（"Use when" / "用于" 之类）。
5. 正文 < 500 行（规范建议）。
6. 正文里引用的相对路径文件**真实存在**（防止 references 改名后文档说谎）。
7. 引用不深链：``references/a.md`` 里再引 ``references/b.md`` 不算错，
   但 SKILL.md 里写 ``references/a/b/c.md`` 会警告（三层披露会退化）。

只依赖标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ALLOWED_KEYS = {"name", "description", "license", "compatibility",
                "metadata", "allowed-tools"}
MAX_NAME = 64
MAX_DESC = 1024
MAX_BODY_LINES = 500
TRIGGER_HINTS = ("use when", "use this", "用于", "何时", "适用", "trigger")


def _parse_frontmatter(text):
    """极简 YAML frontmatter 解析：够用、零依赖。

    只支持本规范会出现的形状：``key: value``、``key: |`` 多行块、
    以及嵌套的 ``metadata:`` 下的一层缩进键值。复杂 YAML 不在这里实现——
    规范本身也不允许复杂结构。
    """
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    block = text[3:end].strip("\n")
    body = text[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    data, key, buf, nested = {}, None, [], False
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[:1] in (" ", "\t")
        if indented:
            buf.append(raw.strip())
            continue
        if buf and key:
            data[key] = "\n".join(buf).strip()
            buf = []
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        nested = value == "" and key == "metadata"
        data[key] = "" if nested else value.strip("'\"")
    if buf and key:
        data[key] = "\n".join(buf).strip()
    return data, body


def _strip_quotes(value):
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def check_skill(skill_dir):
    """返回 (problems, warnings)。problems 非空即失败。"""
    problems, warnings = [], []
    name = os.path.basename(os.path.normpath(skill_dir))
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        return [f"{skill_dir}: 缺少 SKILL.md"], warnings

    with open(skill_md, encoding="utf-8") as fh:
        text = fh.read()
    meta, body = _parse_frontmatter(text)
    if meta is None:
        return [f"{skill_md}: 没有 frontmatter（文件必须以 --- 开头）"], warnings

    extra = set(meta) - ALLOWED_KEYS
    if extra:
        problems.append(
            f"{skill_md}: frontmatter 含非规范字段 {sorted(extra)}；"
            f"只允许 {sorted(ALLOWED_KEYS)}（多写的字段会让 claude.ai 打包硬报错）")

    declared = _strip_quotes(meta.get("name", ""))
    if not declared:
        problems.append(f"{skill_md}: 缺 name")
    else:
        if declared != name:
            problems.append(f"{skill_md}: name={declared!r} 与目录名 {name!r} 不一致")
        if len(declared) > MAX_NAME:
            problems.append(f"{skill_md}: name 超过 {MAX_NAME} 字符")
        if declared != declared.lower():
            problems.append(f"{skill_md}: name 必须全小写")
        if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", declared):
            problems.append(
                f"{skill_md}: name 只能用小写字母/数字/单个连字符（{declared!r}）")

    desc = _strip_quotes(meta.get("description", ""))
    if not desc:
        problems.append(f"{skill_md}: 缺 description")
    else:
        if len(desc) > MAX_DESC:
            problems.append(f"{skill_md}: description 超过 {MAX_DESC} 字符")
        if not any(hint in desc.lower() for hint in TRIGGER_HINTS):
            warnings.append(
                f"{skill_md}: description 里没看到触发提示（Use when / 用于…）；"
                "模型只靠它决定要不要加载这个技能")

    lines = body.count("\n") + 1
    if lines > MAX_BODY_LINES:
        warnings.append(
            f"{skill_md}: 正文 {lines} 行，超过规范建议的 {MAX_BODY_LINES} 行；"
            "长内容应移到 references/")

    # 正文里引用的相对路径必须存在（只看带目录的引用，避免误判普通词）
    for match in re.finditer(r"`([\w./-]+/[\w.-]+\.(?:md|py|json|yml|yaml|txt))`", body):
        rel = match.group(1)
        if rel.startswith(("/", "http")):
            continue
        target = os.path.normpath(os.path.join(skill_dir, rel))
        if not os.path.exists(target):
            problems.append(f"{skill_md}: 引用了不存在的文件 {rel}")
        depth = rel.count("/")
        if depth > 1:
            warnings.append(f"{skill_md}: 深链引用 {rel}（应只隔一层，否则按需加载会退化）")

    return problems, warnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验 Agent Skills 规范")
    ap.add_argument("paths", nargs="*", help="技能目录（默认仓库 skills/ 下全部）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roots = args.paths or [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "skills")]
    dirs = []
    for root in roots:
        if os.path.isfile(os.path.join(root, "SKILL.md")):
            dirs.append(root)
        elif os.path.isdir(root):
            for entry in sorted(os.listdir(root)):
                candidate = os.path.join(root, entry)
                if os.path.isfile(os.path.join(candidate, "SKILL.md")):
                    dirs.append(candidate)
    if not dirs:
        print("没有找到任何 SKILL.md", file=sys.stderr)
        return 1

    report, failed = [], False
    for skill_dir in dirs:
        problems, warnings = check_skill(skill_dir)
        failed = failed or bool(problems)
        report.append({"skill": os.path.basename(skill_dir),
                       "ok": not problems,
                       "problems": problems, "warnings": warnings})
        if not args.json:
            mark = "OK  " if not problems else "FAIL"
            print(f"[{mark}] {os.path.basename(skill_dir)}")
            for problem in problems:
                print(f"    ✗ {problem}")
            for warning in warnings:
                print(f"    · {warning}")

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
