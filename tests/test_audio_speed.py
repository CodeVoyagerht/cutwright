"""变速镜头的声音必须跟着变。

这条 bug 是「工具自己告发自己」发现的：一个 4 秒素材，提示音在第 3 秒；
``speed=2.0`` 之后画面 2 秒，但环境声是按原速截取的，于是第 3 秒的提示音
被截掉了，成片整条无声，``qc`` 报 ``no_long_silence``。

修法：抽环境声时按 ``clip.speed`` 串 ``atempo``（保音高），与画面的
``setpts=PTS/speed`` 对齐。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from . import tmpdir

from cutwright import cli, config, media
from cutwright.backends import ffmpeg as ffmpeg_backend

PATHS = config.discover()
FFMPEG = PATHS.ffmpeg
HAS_FFMPEG = bool(FFMPEG and os.path.isfile(FFMPEG))


def _peak_db(path: str, start: float, dur: float) -> float:
    """量某一段的最大音量（dBFS）。够用的粗测：-91 就是基本没声。"""
    cmd = [FFMPEG, "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", path, "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    text = (proc.stderr or b"").decode("utf-8", "replace")
    for line in text.splitlines():
        if "max_volume:" in line:
            return float(line.split("max_volume:")[1].split("dB")[0].strip())
    return -91.0


def _make_beep_clip(path: str) -> None:
    """4 秒素材：第 1 秒和第 3 秒各响 0.2 秒，中间是静音。"""
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x203040:s=540x960:d=4:r=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.2",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=0.2",
        "-filter_complex",
        "[1:a]adelay=1000|1000[b1];[2:a]adelay=3000|3000[b2];"
        "[b1][b2]amix=inputs=2:normalize=0,apad=whole_dur=4[a]",
        "-map", "0:v", "-map", "[a]", "-t", "4",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", path,
    ]
    subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, check=True)


class AtempoChainTest(unittest.TestCase):
    """``atempo`` 单级只吃 0.5–2.0，超出必须拆串——不然 ffmpeg 直接报错。"""

    def test_identity(self):
        self.assertEqual(ffmpeg_backend._atempo_chain(1.0), "atempo=1")

    def test_within_range(self):
        self.assertEqual(ffmpeg_backend._atempo_chain(1.25), "atempo=1.25")
        self.assertEqual(ffmpeg_backend._atempo_chain(0.8), "atempo=0.8")

    def test_above_two(self):
        self.assertEqual(ffmpeg_backend._atempo_chain(4.0), "atempo=2,atempo=2")
        self.assertEqual(ffmpeg_backend._atempo_chain(3.0), "atempo=2,atempo=1.5")

    def test_below_half(self):
        self.assertEqual(ffmpeg_backend._atempo_chain(0.25), "atempo=0.5,atempo=0.5")

    def test_bad_input_does_not_explode(self):
        self.assertEqual(ffmpeg_backend._atempo_chain(0), "atempo=1.0")
        self.assertEqual(ffmpeg_backend._atempo_chain(-2), "atempo=1.0")


@unittest.skipUnless(HAS_FFMPEG, "没有 ffmpeg，跳过真渲")
class SpeedKeepsAudioTest(unittest.TestCase):
    def test_second_beep_survives_speed_up(self):
        """speed=2.0：两个提示音该落在 0.5s 和 1.5s，不能被截掉一个。"""
        with tmpdir("audiospeed") as root:
            materials = os.path.join(root, "materials")
            os.makedirs(materials)
            _make_beep_clip(os.path.join(materials, "beep.mp4"))

            shotlist = {
                "name": "audiospeed",
                "material_dir": "materials",
                "canvas": {"width": 540, "height": 960},
                "fps": 30,
                "video_segments": [
                    {"file": "beep.mp4", "src_start": 0, "src_dur": 4, "speed": 2.0},
                ],
            }
            path = os.path.join(root, "shotlist.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(shotlist, handle, ensure_ascii=False)

            out = os.path.join(root, "out")
            code = cli.main(["build", path, "-o", out, "--json"])
            self.assertEqual(code, 0)

            film = os.path.join(out, "audiospeed.mp4")
            self.assertTrue(os.path.isfile(film), "没出片")

            first = _peak_db(film, 0.40, 0.30)     # 第一个提示音被加速到 0.5s
            second = _peak_db(film, 1.40, 0.30)    # 第二个必须还在 1.5s 附近
            self.assertGreater(first, -45.0, "第一个提示音丢了")
            self.assertGreater(second, -45.0,
                               "第二个提示音没了——变速时环境声被截断，没有跟画面同步")


if __name__ == "__main__":
    unittest.main()
