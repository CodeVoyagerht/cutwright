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
from unittest import mock

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


class TestFastSwitch(unittest.TestCase):
    """``--fast`` 的契约：它是预览档的快捷方式，并且必须**说清楚**。

    这条开关的意义是「先看一眼」，所以两件事都不能变：JSON 里 ``preview`` 要说真话，
    人读的输出里要有「别拿去交付」的提醒。少了提醒，早晚有人把预览片交出去。
    """

    def setUp(self):
        self._tmp = tmpdir("cli-fast")
        self.dir = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _write(self, name, payload):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def test_fast_sets_preview_in_json(self):
        path = self._write("ok.json", CLEAN)
        code, data = _json_stdout(["build", path, "-o", self.dir, "--dry-run",
                                   "--json", "--fast", "--force"])
        self.assertEqual(code, 0)
        self.assertTrue(data["preview"])

    def test_without_fast_preview_is_false(self):
        path = self._write("ok.json", CLEAN)
        _, data = _json_stdout(["build", path, "-o", self.dir, "--dry-run", "--json", "--force"])
        self.assertFalse(data["preview"])

    def test_fast_warns_before_delivering(self):
        path = self._write("ok.json", CLEAN)
        _, _, err = _run(["build", path, "-o", self.dir, "--dry-run", "--fast", "--force"])
        self.assertIn("--fast", err)
        self.assertIn("交付", err)

    def test_opt_preview_still_works(self):
        """老写法 ``--opt preview=true`` 不能被 --fast 挤掉。"""
        path = self._write("ok.json", CLEAN)
        _, data = _json_stdout(["build", path, "-o", self.dir, "--dry-run",
                                "--json", "--opt", "preview=true", "--force"])
        self.assertTrue(data["preview"])


class TestShippedExamples(unittest.TestCase):
    """随包发出的分镜表要一直是合法的——CI 的冒烟直接渲它们。"""

    def test_ci_shotlist_validates(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "examples", "demo", "shotlist-ci.json")
        self.assertTrue(os.path.exists(path), f"缺少 {path}")
        code, _, err = _run(["validate", path])
        self.assertEqual(code, 0, err)

    def test_ci_shotlist_is_small(self):
        """这份小样的存在意义就是「几秒钟能渲完」，别让它长回 1080p 长片。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "examples", "demo", "shotlist-ci.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertLessEqual(data["canvas"]["width"], 720)
        self.assertLessEqual(data["total_duration"], 8.0)
        self.assertLessEqual(len(data["video_segments"]), 4)


class TestDryRunPlan(unittest.TestCase):
    """``--dry-run`` 一定要给出一张「第几段、用哪一秒、缓存名是什么」的表。

    以前 ffmpeg 后端不认这个开关（只有剪映/达芬奇认），于是它既不出表也不出片，
    用户以为「预演成功了」——其实是照渲不误。现在它必须：不建目录、不调 ffmpeg，
    并且把计划打成人能读的样子。
    """

    def setUp(self):
        self._tmp = tmpdir("cli-dryrun")
        self.dir = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _write(self, name, payload):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return path

    def test_plan_lists_every_shot(self):
        path = self._write("ok.json", CLEAN)
        code, out, _ = _run(["build", path, "-o", self.dir, "--dry-run", "--force"])
        self.assertEqual(code, 0)
        self.assertIn("计划:", out)
        self.assertIn("2 段切片", out)
        for name in ("a.mp4", "b.mp4"):
            self.assertIn(name, out)
        self.assertIn("_build/seg_", out)

    def test_dry_run_writes_nothing(self):
        path = self._write("ok.json", CLEAN)
        out_dir = os.path.join(self.dir, "out")
        code, _, _ = _run(["build", path, "-o", out_dir, "--dry-run", "--force"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(out_dir), "预演不该创建输出目录")

    def test_plan_says_preview_when_fast(self):
        path = self._write("ok.json", CLEAN)
        _, out, _ = _run(["build", path, "-o", self.dir, "--dry-run",
                          "--fast", "--force"])
        self.assertIn("预览档", out)


class TestDryRunWithoutTools(unittest.TestCase):
    """没装 ffmpeg 的机器上，预演也得能看方案。

    CI 的单元测试机就是不装 ffmpeg 的：``build`` 以前先判后端可用性、后看
    ``dry_run``，于是 ``build --dry-run`` 在那种机器上直接返回 1（真实踩过，
    四个 job 一起红）。预演只算计划、不起 ffmpeg，所以它不该被可用性挡住。
    """

    def setUp(self):
        self._tmp = tmpdir("cli-notools")
        self.dir = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_dry_run_needs_no_ffmpeg(self):
        from cutwright import config

        fake = config.Paths(ffmpeg=None, ffprobe=None)
        path = os.path.join(self.dir, "ok.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(CLEAN, fh, ensure_ascii=False)
        with mock.patch.object(config, "discover", return_value=fake):
            code, data = _json_stdout(["build", path, "-o", self.dir,
                                       "--dry-run", "--json", "--force"])
        self.assertEqual(code, 0)
        self.assertTrue(data["result"]["data"]["dry_run"])
        self.assertEqual(len(data["result"]["data"]["shots"]), 2)

    def test_real_build_still_refuses(self):
        """反过来说：真要渲染时，缺 ffmpeg 必须明确拒绝，不能假装成功。"""
        from cutwright import config

        fake = config.Paths(ffmpeg=None, ffprobe=None)
        path = os.path.join(self.dir, "ok.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(CLEAN, fh, ensure_ascii=False)
        with mock.patch.object(config, "discover", return_value=fake):
            code, _, err = _run(["build", path, "-o", self.dir, "--force"])
        self.assertEqual(code, 1)
        self.assertIn("ffmpeg", err)


if __name__ == "__main__":
    unittest.main()
