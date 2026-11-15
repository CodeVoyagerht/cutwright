"""锁住 CLI 的对外契约：``--json`` 的字段与退出码。

为什么值得单独一个测试文件：技能里的 ``scripts/verify.py`` 与外面可能有的
CI/自动化都靠这两样东西判成败。字段改名、退出码从 1 变 2，都不会让任何
单元测试变红——但会让调用方静默跑错分支。所以这里逐字段断言。

约定（README 与 SKILL.md 里都写了）：

* 退出码 ``0`` 成功、``1`` 有错误/未通过、``2`` 命令用法错误。
* ``--json`` 一律输出到 stdout，诊断信息走 stderr，stdout 里只有 JSON。
"""
from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from . import tmpdir

from cutwright import cli

CLEAN = {
    "name": "contract",
    "canvas": {"width": 1080, "height": 1920},
    "fps": 30,
    "video_segments": [
        {"file": "a.mp4", "src_start": 0.0, "src_dur": 2.0, "tl_start": 0.0},
        {"file": "b.mp4", "src_start": 1.0, "src_dur": 2.5, "tl_start": 2.0},
    ],
    "text_segments": [
        {"text": "第一句", "tl_start": 0.2, "tl_dur": 1.5},
    ],
}

BROKEN = {
    "name": "broken",
    "canvas": {"width": 1080, "height": 1920},
    "fps": 30,
    "video_segments": [
        {"file": "a.mp4", "src_start": 0.0, "src_dur": -1.0, "tl_start": 0.0},
    ],
}


def _run(argv):
    """跑一次 CLI，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _json_stdout(argv):
    code, out, _ = _run(argv)
    try:
        return code, json.loads(out)
    except json.JSONDecodeError as exc:      # 契约破坏：stdout 混进了别的东西
        raise AssertionError(f"stdout 不是纯 JSON（{exc}）：{out!r}")


class TestShotlistWriting(unittest.TestCase):
    def setUp(self):
        self._tmp = tmpdir("cli-contract")
        self.dir = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return path

    # -- validate ----------------------------------------------------------- #
    def test_validate_clean_is_zero(self):
        path = self._write("ok.json", CLEAN)
        code, data = _json_stdout(["validate", path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"ok", "errors", "warnings", "issues", "summary"})
        self.assertTrue(data["ok"])
        self.assertEqual(data["errors"], 0)
        self.assertEqual(data["summary"]["shots"], 2)
        self.assertAlmostEqual(data["summary"]["duration"], 4.5, places=3)

    def test_validate_broken_is_one(self):
        path = self._write("bad.json", BROKEN)
        code, data = _json_stdout(["validate", path, "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertGreaterEqual(data["errors"], 1)
        codes = {i["code"] for i in data["issues"]}
        self.assertIn("E012", codes)          # src_dur <= 0
        for key in ("level", "code", "message", "where"):
            self.assertIn(key, data["issues"][0])

    # -- plan --------------------------------------------------------------- #
    def test_plan_reports_summary(self):
        path = self._write("ok.json", CLEAN)
        code, data = _json_stdout(["plan", path, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"report", "summary", "out"})
        summary = data["summary"]
        for key in ("shots", "duration", "duration_hms", "canvas", "fps",
                    "avg_shot", "min_shot", "max_shot", "texts",
                    "vo_segments", "overlays", "per_act"):
            self.assertIn(key, summary)
        self.assertEqual(summary["shots"], 2)

    def test_plan_target_writes_shotlist(self):
        path = self._write("ok.json", CLEAN)
        out = os.path.join(self.dir, "fitted.json")
        code, data = _json_stdout(["plan", path, "--target", "3.6",
                                   "--strategy", "trim", "--out", out, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(data["out"], out)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            fitted = json.load(fh)
        total = sum(s["src_dur"] / float(s.get("speed", 1.0))
                    for s in fitted["video_segments"])
        self.assertAlmostEqual(total, 3.6, places=2)

    # -- qc ----------------------------------------------------------------- #
    def test_qc_shape(self):
        path = self._write("ok.json", CLEAN)
        code, data = _json_stdout(["qc", path, "--json"])
        self.assertIn("ok", data)
        self.assertIn("checks", data)
        self.assertTrue(data["checks"])
        for check in data["checks"]:
            for key in ("name", "ok", "detail", "severity"):
                self.assertIn(key, check)
        self.assertEqual(code, 0 if data["ok"] else 1)

    # -- doctor / backends -------------------------------------------------- #
    def test_doctor_shape(self):
        code, data = _json_stdout(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertIsInstance(data, dict)
        for key in ("version", "python", "platform", "paths", "backends"):
            self.assertIn(key, data)
        self.assertIsInstance(data["backends"], list)

    def test_backends_is_a_list(self):
        code, data = _json_stdout(["backends", "--json"])
        self.assertEqual(code, 0)
        self.assertIsInstance(data, list)

    def test_backends_entries_have_contract_fields(self):
        _, data = _json_stdout(["backends", "--json"])
        by_name = {item["name"]: item for item in data}
        self.assertIn("ffmpeg", by_name)      # 至少 ffmpeg 后端要能登记上
        for item in data:
            for key in ("name", "available", "reason"):
                self.assertIn(key, item)
            self.assertIsInstance(item["available"], bool)

    # -- 退出码 ------------------------------------------------------------- #
    def test_unknown_command_exits_two(self):
        code, _, err = _run(["nosuchcmd"])
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_help_exits_zero(self):
        code, out, _ = _run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("cutwright", out)

    def test_missing_shotlist_arg_exits_two(self):
        code, _, _ = _run(["validate"])
        self.assertEqual(code, 2)

    def test_missing_file_is_reported_not_traced(self):
        code, _, err = _run(["validate", os.path.join(self.dir, "nope.json")])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
