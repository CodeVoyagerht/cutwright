"""qc 的节奏检查：素材是不是被一段反复用、镜长是不是出格。

这些判断原先只在 `story`（走剧本）里做，手写分镜表的人拿不到；
现在 `qc` 自己也能说，因为 `qc` 只看时间线，不需要知道分镜表怎么来的。
"""
from __future__ import annotations

import unittest

from cutwright.qc import check_timeline
from cutwright.schema import Shotlist
from cutwright.timeline import Timeline


def _timeline(shots) -> Timeline:
    """shots: [(文件名, 时长秒, 场次)]"""
    return Timeline.from_shotlist(Shotlist.from_dict({
        "name": "rhythm",
        "canvas": {"width": 1080, "height": 1920},
        "fps": 30,
        "video_segments": [
            {"file": name, "src_start": 0.0, "src_dur": dur, "act": act}
            for name, dur, act in shots
        ],
    }))


def _check(report, name):
    return next(check for check in report.checks if check.name == name)


class RhythmCheckTest(unittest.TestCase):
    def test_same_piece_repeated_in_a_scene_warns(self):
        report = check_timeline(_timeline([("a.mp4", 2.0, "S1")] * 6))
        check = _check(report, "shot_rhythm")
        self.assertFalse(check.ok)
        self.assertEqual(check.severity, "warn")
        codes = [issue.code for issue in report.issues]
        self.assertIn("Q106", codes)
        self.assertIn("1 段素材", check.detail)

    def test_two_pieces_for_many_shots_warns(self):
        shots = [("a.mp4", 2.0, "S1"), ("b.mp4", 2.0, "S1")] * 4
        report = check_timeline(_timeline(shots))
        self.assertIn("Q106", [issue.code for issue in report.issues])

    def test_uniform_lengths_warn_about_single_tempo(self):
        shots = [(f"c{i}.mp4", 2.0, "S1") for i in range(8)]
        report = check_timeline(_timeline(shots))
        self.assertIn("Q108", [issue.code for issue in report.issues])

    def test_varied_lengths_and_pieces_pass(self):
        shots = [("a.mp4", 1.0, "S1"), ("b.mp4", 1.6, "S1"),
                 ("c.mp4", 2.2, "S1"), ("d.mp4", 3.1, "S1"),
                 ("e.mp4", 3.6, "S1")]
        report = check_timeline(_timeline(shots))
        self.assertTrue(_check(report, "shot_rhythm").ok)

    def test_long_shot_warns(self):
        shots = [("a.mp4", 5.0, "S1"), ("b.mp4", 1.5, "S1"),
                 ("c.mp4", 2.0, "S1"), ("d.mp4", 1.2, "S1"),
                 ("e.mp4", 3.0, "S1"), ("f.mp4", 1.0, "S1")]
        report = check_timeline(_timeline(shots))
        self.assertIn("Q108", [issue.code for issue in report.issues])
        self.assertTrue(any("超过 4.0s" in issue.message for issue in report.issues))


if __name__ == "__main__":
    unittest.main()
