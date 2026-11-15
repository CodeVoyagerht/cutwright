"""核心链路测试：schema → timeline → planner → qc → cli。

这些用例一条 ffmpeg 都不用跑，所以永远能在 CI 里跑。真渲染的用例在
``test_ffmpeg_backend.py``（那个会 skip）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import tmpdir

from cutwright import Shotlist, cli, planner, qc
from cutwright.schema import validate
from cutwright.timeline import Timeline, TimelineError


def _mini_shotlist(**over):
    base = {
        "name": "mini",
        "canvas": {"width": 1080, "height": 1920},
        "fps": 30,
        "video_segments": [
            {"file": "a.mp4", "src_start": 0.0, "src_dur": 2.0},
            {"file": "b.mp4", "src_start": 1.0, "src_dur": 3.0},
            {"file": "c.mp4", "src_start": 0.0, "src_dur": 1.5, "speed": 1.5},
        ],
        "text_segments": [
            {"text": "开头|第一个关键词", "tl_start": 0.2, "tl_dur": 1.8,
             "style": "caption"},
        ],
        "vo_segments": [
            {"text": "旁白不该出现在画面上", "tl_start": 0.5, "tl_dur": 2.0,
             "style": "vo_hidden"},
        ],
        "styles": {
            "caption": {"font_height_px": 145, "bottom_margin_px": 520,
                        "max_chars_per_line": 10},
            "vo_hidden": {"invisible": True},
        },
    }
    base.update(over)
    return Shotlist.from_dict(base)


class TestSchema(unittest.TestCase):
    def test_unknown_fields_survive_roundtrip(self):
        """用户自定义键不能被吃掉——同一个文件还要喂给别的工具。"""
        sl = Shotlist.from_dict({
            "name": "x", "note": "我自己加的备注", "tags": ["a"],
            "video_segments": [{"file": "a.mp4", "src_dur": 1.0, "shot": 7}],
        })
        out = sl.to_dict()
        self.assertEqual(out["note"], "我自己加的备注")
        self.assertEqual(out["tags"], ["a"])

    def test_segment_accepts_src_alias_and_end(self):
        """``src`` 与 ``src_end`` 两种写法都要认，手写分镜表容错第一。"""
        sl = Shotlist.from_dict({"video_segments": [
            {"src": "a.mp4", "src_start": 1.0, "src_end": 4.0}]})
        self.assertEqual(sl.video_segments[0].file, "a.mp4")
        self.assertAlmostEqual(sl.video_segments[0].src_dur, 3.0)

    def test_keyword_split(self):
        sl = Shotlist.from_dict({"text_segments": [
            {"text": "前半|关键词", "tl_dur": 1}]})
        seg = sl.text_segments[0]
        self.assertEqual(seg.head, "前半")
        self.assertEqual(seg.keyword, "关键词")

    def test_validate_e01x(self):
        sl = Shotlist.from_dict({"video_segments": [
            {"file": "", "src_dur": 0.0}]})
        codes = {i.code for i in validate(sl)}
        self.assertIn("E011", codes)
        self.assertIn("E012", codes)

    def test_validate_style_reference(self):
        sl = _mini_shotlist()
        sl.text_segments[0].style = "不存在的样式"
        issues = [i for i in validate(sl) if i.code == "E031"]
        self.assertEqual(len(issues), 1)

    def test_validate_file_check(self):
        sl = _mini_shotlist()
        with tmpdir("case") as tmp:
            sl.material_dir = tmp
            codes = {i.code for i in validate(sl, check_files=True)}
            self.assertIn("E017", codes)   # 三个素材都不存在

    def test_total_duration_drift_warning(self):
        sl = _mini_shotlist(total_duration=100.0)
        self.assertIn("W020", {i.code for i in validate(sl)})


class TestTimeline(unittest.TestCase):
    def test_sequential_tl_start(self):
        """不写 tl_start 时按顺序首尾相接，并且变速要参与计算。"""
        tl = Timeline.from_shotlist(_mini_shotlist())
        self.assertEqual([c.index for c in tl.clips], [0, 1, 2])
        self.assertAlmostEqual(tl.clips[0].tl_start, 0.0)
        self.assertAlmostEqual(tl.clips[1].tl_start, 2.0)
        self.assertAlmostEqual(tl.clips[2].tl_start, 5.0)
        # 第三镜 1.5s 素材 1.5 倍速 → 时间线上只占 1.0s
        self.assertAlmostEqual(tl.clips[2].tl_dur, 1.0)
        self.assertAlmostEqual(tl.duration, 6.0)

    def test_explicit_tl_start_kept(self):
        sl = _mini_shotlist()
        sl.video_segments[1].tl_start = 4.0
        tl = Timeline.from_shotlist(sl, strict=False)
        self.assertAlmostEqual(tl.clips[1].tl_start, 4.0)

    def test_text_and_vo_classified(self):
        tl = Timeline.from_shotlist(_mini_shotlist())
        self.assertEqual(len(tl.texts), 2)
        vis = {t.role: t.visible for t in tl.texts}
        self.assertTrue(vis["text"])
        self.assertFalse(vis["vo"])

    def test_strict_raises_on_error(self):
        sl = _mini_shotlist()
        sl.video_segments[0].src_dur = 0.0
        with self.assertRaises(TimelineError):
            Timeline.from_shotlist(sl)

    def test_summary_shape(self):
        s = Timeline.from_shotlist(_mini_shotlist()).summary()
        for key in ("duration", "shots", "avg_shot", "texts", "vo_segments"):
            self.assertIn(key, s)
        self.assertEqual(s["shots"], 3)
        self.assertEqual(s["vo_segments"], 1)

    def test_frames_roundtrip(self):
        tl = Timeline.from_shotlist(_mini_shotlist())
        self.assertEqual(tl.frames(2.0), 60)
        self.assertAlmostEqual(tl.seconds(60), 2.0)


class TestPlanner(unittest.TestCase):
    def test_default_styles_scale_with_canvas(self):
        """字号随画布等比放大，但落在实测标定的整数像素上，所以按比例断言。"""
        small = planner.default_styles(__import__("cutwright").Canvas(1080, 1920))
        big = planner.default_styles(__import__("cutwright").Canvas(2160, 3840))
        for role, style in small.items():
            self.assertAlmostEqual(big[role].font_height_px,
                                   style.font_height_px * 2,
                                   delta=style.font_height_px * 0.05,
                                   msg=f"{role} 没有按画布等比放大")
        # 1080 画布上字幕必须放得下：72px 字号 × 14 字不进 1080 宽就该被折行逻辑接管
        self.assertLess(small["caption"].font_height_px, 90)

    def test_normalize_fills_gaps(self):
        sl = _mini_shotlist()
        sl.video_segments[1].tl_start = 9.0     # 故意留空档
        out, actions = planner.normalize_shotlist(sl, trust_existing=False)
        self.assertAlmostEqual(out.video_segments[1].tl_start, 2.0)
        self.assertTrue(actions)

    def test_fit_shrinks_to_target(self):
        sl = _mini_shotlist()
        out, rep = planner.fit_duration(sl, 4.0)
        self.assertTrue(rep.ok, rep.messages)
        self.assertAlmostEqual(out.video_duration(), 4.0, delta=0.1)

    def test_fit_respects_min_shot(self):
        """镜头太短就别再缩了，宁可报偏差也不要产出 0.2s 的闪帧。"""
        sl = _mini_shotlist()
        out, rep = planner.fit_duration(sl, 0.5, min_shot=0.8)
        self.assertFalse(rep.ok)
        for seg in out.video_segments:
            self.assertGreaterEqual(seg.src_dur, 0.8 - 1e-6)

    def test_fit_speed_strategy(self):
        sl = _mini_shotlist()
        out, rep = planner.fit_duration(sl, 8.0, strategy="speed")
        self.assertLess(out.video_segments[0].speed, 1.0)   # 拉长 → 减速
        self.assertAlmostEqual(out.video_duration(), 8.0, delta=0.2)


class TestQc(unittest.TestCase):
    def _tl(self):
        return Timeline.from_shotlist(_mini_shotlist())

    def test_gap_detected(self):
        sl = _mini_shotlist()
        sl.video_segments[1].tl_start = 5.0
        tl = Timeline.from_shotlist(sl, strict=False)
        report = qc.check_timeline(tl)
        self.assertFalse(report.ok)
        names = {c.name for c in report.failed}
        self.assertIn("no_gaps", names)

    def test_overlap_detected(self):
        sl = _mini_shotlist()
        sl.video_segments[1].tl_start = 0.5
        tl = Timeline.from_shotlist(sl, strict=False)
        self.assertIn("no_overlaps", {c.name for c in qc.check_timeline(tl).failed})

    def test_visible_vo_is_error(self):
        sl = _mini_shotlist()
        sl.styles["vo_hidden"].invisible = False
        tl = Timeline.from_shotlist(sl, strict=False)
        report = qc.check_timeline(tl)
        self.assertIn("vo_hidden", {c.name for c in report.failed})

    def test_missing_source_detected(self):
        sl = _mini_shotlist()
        sl.material_dir = "C:/nope/definitely/not/here"
        tl = Timeline.from_shotlist(sl, strict=False)
        self.assertIn("sources_exist", {c.name for c in qc.check_timeline(tl).failed})

    def test_clean_timeline_passes(self):
        with tmpdir("case") as tmp:
            sl = _mini_shotlist()
            sl.material_dir = tmp
            for name in ("a.mp4", "b.mp4", "c.mp4"):
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(b"0")
            tl = Timeline.from_shotlist(sl, strict=False)
            report = qc.check_timeline(tl)
            self.assertTrue(report.ok, [c.name for c in report.failed])

    def test_render_missing_file(self):
        report = qc.check_render("C:/nope/x.mp4")
        self.assertFalse(report.ok)
        self.assertEqual(report.failed[0].name, "file_exists")


class TestCli(unittest.TestCase):
    def _run(self, argv):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            code = cli.main(argv)
        return code, buf_out.getvalue(), buf_err.getvalue()

    def test_init_then_validate_and_plan(self):
        with tmpdir("case") as tmp:
            code, out, _ = self._run(["init", tmp, "--name", "t", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(os.path.exists(payload["shotlist"]))

            code, out, _ = self._run(["validate", payload["shotlist"]])
            self.assertEqual(code, 0, out)

            code, out, _ = self._run(["plan", payload["shotlist"], "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["summary"]["shots"], 3)

    def test_validate_json_on_error(self):
        with tmpdir("case") as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"video_segments": []}, fh)
            code, out, _ = self._run(["validate", path, "--json"])
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(out)["ok"])

    def test_plan_target_fits(self):
        with tmpdir("case") as tmp:
            code, out, _ = self._run(["init", tmp, "--json"])
            shotlist = json.loads(out)["shotlist"]
            code, out, _ = self._run(["plan", shotlist, "--target", "6", "--json"])
            self.assertEqual(code, 0)
            rep = json.loads(out)["report"]
            self.assertTrue(rep["ok"], rep)

    def test_backends_json(self):
        code, out, _ = self._run(["backends", "--json"])
        self.assertEqual(code, 0)
        names = {b["name"] for b in json.loads(out)}
        self.assertIn("ffmpeg", names)

    def test_unknown_command_exit_2(self):
        code, _, _ = self._run(["nope"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
