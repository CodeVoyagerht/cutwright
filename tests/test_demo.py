"""演示素材合成的测试。

分两层：
* 不依赖 ffmpeg 的部分（PNG 编码器）必须永远能跑——它是"零依赖出片"的地基；
* 依赖 ffmpeg 的部分（合成视频/音频）在没有 ffmpeg 的机器上 skip。
"""
from __future__ import annotations

import os
import struct
import zlib

import unittest
from unittest import mock

from cutwright import demo
from . import tmpdir


def _decode_rgba_png(path):
    """把 write_rgba_png 写出来的图读回 (宽, 高, 像素取值函数)。

    刻意不复用 Pillow：这里要验证的正是"没有 Pillow 也能出正确的图"。
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "PNG 签名不对"

    width = height = None
    idat = b""
    pos = 8
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos:pos + 4])
        tag = blob[pos + 4:pos + 8]
        payload = blob[pos + 8:pos + 8 + length]
        pos += 12 + length                    # 长度 + tag + 数据 + crc
        if tag == b"IHDR":
            width, height, depth, color_type = struct.unpack(">IIBB", payload[:10])
            assert depth == 8 and color_type == 6, "只支持 8bit RGBA"
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break

    raw = zlib.decompress(idat)
    stride = width * 4
    assert len(raw) == height * (1 + stride), "扫描线长度对不上"

    def pixel(x, y):
        base = y * (1 + stride) + 1 + x * 4     # 跳过每行的 filter 字节
        return tuple(raw[base:base + 4])

    return width, height, pixel


class PngWriterTest(unittest.TestCase):
    def test_writes_valid_rgba_png(self):
        with tmpdir("png") as tmp:
            path = os.path.join(tmp, "x.png")
            demo.write_rgba_png(path, 4, 3, lambda x, y: (x * 10, y * 20, 7, 255))
            width, height, pixel = _decode_rgba_png(path)
            self.assertEqual((width, height), (4, 3))
            self.assertEqual(pixel(0, 0), (0, 0, 7, 255))
            self.assertEqual(pixel(2, 1), (20, 20, 7, 255))
            self.assertEqual(pixel(3, 2), (30, 40, 7, 255))

    def test_frame_center_is_really_transparent(self):
        """画框中间必须 alpha=0——不透明的话成片里是个黑窟窿（实测踩过）。"""
        with tmpdir("png") as tmp:
            path = os.path.join(tmp, "frame.png")
            self.assertTrue(demo.make_frame_png(path))
            width, height, pixel = _decode_rgba_png(path)
            self.assertEqual((width, height), demo.FRAME_SIZE)
            self.assertEqual(pixel(450, 750)[3], 0, "中心不透明")
            self.assertEqual(pixel(450, 900)[3], 0, "中心不透明")
            # 外框那几圈不透明，且是白色系；内框是金的
            self.assertEqual(pixel(450, 9), (255, 255, 255, 216))
            self.assertEqual(pixel(450, 25), (212, 175, 55, 153))

    def test_existing_frame_is_not_overwritten_unless_forced(self):
        with tmpdir("png") as tmp:
            path = os.path.join(tmp, "frame.png")
            demo.make_frame_png(path)
            before = os.stat(path).st_mtime_ns
            self.assertFalse(demo.make_frame_png(path))
            self.assertTrue(demo.make_frame_png(path, force=True))
            self.assertGreaterEqual(os.stat(path).st_mtime_ns, before)


HAVE_FFMPEG = bool(demo.find_ffmpeg())


class MediaSynthesisTest(unittest.TestCase):
    def test_missing_ffmpeg_raises_with_actionable_message(self):
        with tmpdir("nofm") as tmp:
            with mock.patch.object(demo, "find_ffmpeg", return_value=""):
                with self.assertRaises(demo.DemoMediaError) as ctx:
                    demo.make_demo_media(tmp, with_bgm=False)
            message = str(ctx.exception)
            self.assertIn("ffmpeg", message)
            self.assertIn("把素材放进", message)     # 至少告诉人还能手动放素材

    @unittest.skipUnless(HAVE_FFMPEG, "需要 ffmpeg 才能合成素材")
    def test_makes_clip_and_bgm_and_skips_on_second_run(self):
        with tmpdir("media") as tmp:
            clips = [("c1.mp4", 320, 240, 1.0, "t")]
            first = demo.make_demo_media(tmp, clips=clips, bgm_duration=2.0)
            self.assertEqual(len(first["created"]), 3)   # 视频 + 画框 + 配乐
            for path in first["created"]:
                self.assertTrue(os.path.exists(path), path)
                self.assertGreater(os.path.getsize(path), 0, path)

            second = demo.make_demo_media(tmp, clips=clips, bgm_duration=2.0)
            self.assertEqual(second["created"], [])
            self.assertEqual(len(second["skipped"]), 3)


if __name__ == "__main__":
    unittest.main()
