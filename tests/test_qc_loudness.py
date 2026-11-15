"""qc 的响度 / 峰值检查。

后端会把成片拉到 -14 LUFS，但「做了」不等于「做到了」——这一层必须能自己量出来。
这些用例都用 ffmpeg 现合成素材，不依赖任何真实文件。

注意：`sine` 源实测比 0 dBFS 低约 18 dB，所以用例不靠「生成器给多大」来断言，
而是先用 `loudnorm` 把测试片做成已知响度，再让 qc 去量。
"""

from __future__ import annotations

import os
import unittest

from cutwright import config, media, qc

from . import tmpdir


def _paths():
    """没装 ffmpeg 的机器上这些用例没有意义，跳过而不是报错。"""
    paths = config.discover()
    if not (paths.ffmpeg and paths.ffprobe):
        raise unittest.SkipTest("没有 ffmpeg/ffprobe，跳过响度检查用例")
    return paths


def _make_mp4(path: str, *, target_lufs: float, duration: float = 2.0,
              with_audio: bool = True) -> str:
    """造一条纯色 + 单音的测试片，响度由 ``loudnorm`` 定到 ``target_lufs``。"""
    paths = _paths()
    cmd = [paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=0x336699:s=128x72:d={duration}:r=30"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:sample_rate=48000:duration={duration}",
                "-af", f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", path]
    else:
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", path]
    media.run(cmd)
    return path


def _check(path: str) -> dict:
    report = qc.check_render(path, paths=_paths())
    return {check.name: check for check in report.checks}


class LoudnessCheckTest(unittest.TestCase):
    def test_normalized_film_passes(self) -> None:
        with tmpdir("qc_loud_ok") as scratch:
            path = _make_mp4(os.path.join(scratch, "ok.mp4"), target_lufs=-14)
            checks = _check(path)
            self.assertIn("loudness_match", checks)
            self.assertTrue(checks["loudness_match"].ok,
                            msg=checks["loudness_match"].detail)

    def test_quiet_film_is_flagged_but_does_not_fail_the_report(self) -> None:
        with tmpdir("qc_loud") as scratch:
            path = _make_mp4(os.path.join(scratch, "quiet.mp4"), target_lufs=-26)
            report = qc.check_render(path, paths=_paths())
            checks = {c.name: c for c in report.checks}
            quiet = checks["loudness_match"]
            self.assertFalse(quiet.ok, "比目标低 12 LU 不该判成「响度合适」")
            self.assertEqual(quiet.severity, "warn", "响度偏轻是提醒，不是片子废了")
            self.assertTrue(report.ok, "warn 不该让整份体检失败")

    def test_no_audio_means_no_loudness_check(self) -> None:
        with tmpdir("qc_loud_none") as scratch:
            path = _make_mp4(os.path.join(scratch, "mute.mp4"), target_lufs=0,
                             with_audio=False)
            checks = _check(path)
            self.assertNotIn("loudness_match", checks)
            self.assertNotIn("true_peak", checks)


if __name__ == "__main__":
    unittest.main()
