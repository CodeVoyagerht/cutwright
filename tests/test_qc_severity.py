"""体检的定级与显示必须一致，退出码必须跟着定级走。

这条 bug 的表现是：``qc`` 打出 ``[FAIL] no_long_silence``，同一屏却写着
「全部通过。」，退出码 0。原因有两个叠在一起——

1. 黑场/静音检出一律按 ``severity="warn"`` 存，而 ``QcReport.ok`` 只数 error；
2. 显示层只看 ``ok``，一律打 ``FAIL``。

于是出现「打印 FAIL、结论通过」。现在：整片黑/整片无声＝error，
局部超阈值＝warn，显示层用 ``[FAIL]`` / ``[WARN]`` 区分。
"""
from __future__ import annotations

import os
import subprocess
import unittest

from . import tmpdir

from cutwright import cli, config, qc
from cutwright.qc import QcCheck, QcReport

PATHS = config.discover()
FFMPEG = PATHS.ffmpeg
HAS_FFMPEG = bool(FFMPEG and os.path.isfile(FFMPEG))


class ReportSeverityTest(unittest.TestCase):
    """``ok`` 只数 error 级失败——这条语义本身是对的，要守住。"""

    def test_warn_failure_does_not_fail_report(self):
        report = QcReport()
        report.add("no_long_silence", False, "1 段静音", severity="warn")
        self.assertTrue(report.ok)
        self.assertEqual([c.name for c in report.failed], ["no_long_silence"])

    def test_error_failure_fails_report(self):
        report = QcReport()
        report.add("no_long_silence", False, "整片无声", severity="error")
        self.assertFalse(report.ok)


class MarkTest(unittest.TestCase):
    """标记 = 定级。混用会让人（和 agent）读错。"""

    def test_ok(self):
        self.assertEqual(cli._mark({"ok": True, "severity": "error"}), "OK  ")

    def test_error_failure_is_fail(self):
        self.assertEqual(cli._mark({"ok": False, "severity": "error"}), "FAIL")

    def test_warn_failure_is_warn(self):
        self.assertEqual(cli._mark({"ok": False, "severity": "warn"}), "WARN")


def _make_video(path: str, *, silent: bool) -> None:
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=0x405060:s=320x240:d=2:r=25"]
    if silent:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2"]
    else:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    cmd += ["-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", path]
    subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, check=True)


@unittest.skipUnless(HAS_FFMPEG, "没有 ffmpeg，跳过真片体检")
class RenderSilenceTest(unittest.TestCase):
    def test_whole_silent_film_is_error(self):
        with tmpdir("qcsilent") as root:
            film = os.path.join(root, "silent.mp4")
            _make_video(film, silent=True)
            report = qc.check_render(film, expect_duration=2.0,
                                     expect_canvas=(320, 240))
            check = next(c for c in report.checks if c.name == "no_long_silence")
            self.assertFalse(check.ok, "整片无声没被检出")
            self.assertEqual(check.severity, "error", "整片无声应判 error")
            self.assertFalse(report.ok, "整片无声时报告不该判通过")

    def test_film_with_sound_passes(self):
        with tmpdir("qcsound") as root:
            film = os.path.join(root, "sound.mp4")
            _make_video(film, silent=False)
            report = qc.check_render(film, expect_duration=2.0,
                                     expect_canvas=(320, 240))
            check = next(c for c in report.checks if c.name == "no_long_silence")
            self.assertTrue(check.ok)
            self.assertTrue(report.ok)

    def test_cli_exit_code_follows_severity(self):
        """整片无声时 ``qc`` 必须返回 1，输出里不能出现「全部通过」。"""
        with tmpdir("qcexit") as root:
            film = os.path.join(root, "silent.mp4")
            _make_video(film, silent=True)
            shotlist = os.path.join(root, "shotlist.json")
            with open(shotlist, "w", encoding="utf-8") as handle:
                handle.write('{"name":"x","canvas":{"width":320,"height":240},'
                             '"fps":25,"video_segments":[{"file":"silent.mp4",'
                             '"src_start":0,"src_dur":2}]}')
            code = cli.main(["qc", shotlist, "--mp4", film])
            self.assertEqual(code, 1)


def _make_partial_silence(path: str) -> None:
    """3 秒片子：0–0.3s 有声、0.3–2.7s 静、2.7–3.0s 有声。

    静音 2.4s 超过 1.0s 的阈值，但不到全片九成（2.7s），所以该判 warn。
    """
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", "color=c=0x405060:s=320x240:d=3:r=25",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
           "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
           "-filter_complex",
           "[1:a]atrim=0:0.3,asetpts=PTS-STARTPTS[a1];"
           "[2:a]atrim=0:2.4,asetpts=PTS-STARTPTS[a2];"
           "[3:a]atrim=0:0.3,asetpts=PTS-STARTPTS[a3];"
           "[a1][a2][a3]concat=n=3:v=0:a=1[aout]",
           "-map", "0:v", "-map", "[aout]",
           "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", path]
    subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, check=True)


@unittest.skipUnless(HAS_FFMPEG, "没有 ffmpeg，跳过真片体检")
class WarnPathTest(unittest.TestCase):
    def test_partial_silence_is_warn_not_fail(self):
        """局部静音是 warn：打印 ``[WARN]``、结论通过、退出码 0。

        这条用例顺带守住另一件事——``qc`` 的检查项是 ``QcCheck`` 对象，
        显示层如果按 dict 喂就会抛 ``AttributeError``（真发生过）。
        """
        import contextlib
        import io

        with tmpdir("qcwarn") as root:
            film = os.path.join(root, "partial.mp4")
            _make_partial_silence(film)
            shotlist = os.path.join(root, "shotlist.json")
            with open(shotlist, "w", encoding="utf-8") as handle:
                handle.write('{"name":"x","canvas":{"width":320,"height":240},'
                             '"fps":25,"video_segments":[{"file":"partial.mp4",'
                             '"src_start":0,"src_dur":3}]}')

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = cli.main(["qc", shotlist, "--mp4", film])
            printed = buffer.getvalue()

            self.assertNotIn("AttributeError", printed)
            self.assertIn("[WARN] no_long_silence", printed)
            self.assertNotIn("[FAIL] no_long_silence", printed)
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()


def _timeline_with_captions(*texts):
    """造一条只有字幕的时间线：texts 是 (文案, 时长) 二元组。"""
    from cutwright.schema import Canvas, Shotlist
    from cutwright.timeline import Timeline

    segments = [{"file": "a.mp4", "src_start": 0.0, "src_dur": 4.0}]
    cursor = 0.0
    for body, dur in texts:
        segments.append(segments[0])
        cursor += dur
    raw = {"name": "cap", "canvas": {"width": 1080, "height": 1920}, "fps": 30,
           "video_segments": [{"file": "a.mp4", "src_start": 0.0, "src_dur": 4.0}],
           "text_segments": [{"text": body, "tl_start": i * 1.0, "tl_dur": dur}
                             for i, (body, dur) in enumerate(texts)]}
    return Timeline.from_shotlist(Shotlist.from_dict(raw))


class CaptionReadableTest(unittest.TestCase):
    """qc 不只查「字幕在画面内」，还要查「读不读得完」。"""

    def test_too_many_chars_per_second_warns(self):
        tl = _timeline_with_captions(
            ("这一条字幕特意写得很长很长很长三十个字左右", 1.2))
        report = qc.check_timeline(tl)
        check = [c for c in report.checks if c.name == "text_readable"][0]
        self.assertFalse(check.ok, "25 字/秒必须被判不可读")
        self.assertEqual(check.severity, "warn")
        # 读得慢是提醒：Q105 只能是 warn，绝不能升级成 error。
        self.assertTrue([i for i in report.issues
                         if i.code == "Q105" and i.level == "warn"])
        self.assertFalse([i for i in report.issues
                          if i.code == "Q105" and i.level == "error"])

    def test_comfortable_speed_passes(self):
        # 19 字挂 3.8 秒 ≈ 5 字/秒，属于舒适范围。
        tl = _timeline_with_captions(("我觉得你该先睡一觉，再决定要不要停", 3.8))
        report = qc.check_timeline(tl)
        check = [c for c in report.checks if c.name == "text_readable"][0]
        self.assertTrue(check.ok, check.detail)

    def test_invisible_voiceover_is_ignored(self):
        from cutwright.schema import TextStyle
        from cutwright.timeline import Timeline
        from cutwright.schema import Shotlist

        raw = {"name": "vo", "canvas": {"width": 1080, "height": 1920}, "fps": 30,
               "video_segments": [{"file": "a.mp4", "src_start": 0.0, "src_dur": 3.0}],
               "vo_segments": [{"text": "这句旁白很长很长很长很长很长很长", "tl_dur": 1.0}],
               "styles": {"vo_hidden": {"role": "vo_hidden", "invisible": True}}}
        report = qc.check_timeline(Timeline.from_shotlist(Shotlist.from_dict(raw)))
        check = [c for c in report.checks if c.name == "text_readable"][0]
        self.assertTrue(check.ok, "旁白不出字，不该按字幕算可读性")
