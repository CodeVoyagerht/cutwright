"""读剪映草稿的测试。

最重要的一条是**往返**：把一条 Timeline 写成草稿，再读回来，排布必须一模一样。
它同时盯住两件事——写的时候字段放对了，读的时候知道去哪儿拿。
其余用例覆盖「读不了」的几种情形，避免把坏输入读成一份错的分镜表。
"""

from __future__ import annotations

import json
import os
import unittest

from cutwright.backends.jianying import draft as jy_draft
from cutwright.backends.jianying.reader import DraftError, read_content, read_draft
from cutwright.schema import (AudioSegment, Canvas, Shotlist, TextSegment,
                              VideoSegment)
from cutwright.timeline import Timeline

from . import tmpdir

SHOTS = [
    ("clip01.mp4", 0.5, 2.0, 1.0),
    ("clip02.mp4", 0.0, 1.5, 2.0),
    ("clip03.mp4", 1.0, 2.5, 1.25),
]
TEXTS = [("第一句", 0.4, 1.6), ("第二句", 2.2, 1.4)]
AUDIO = ("bgm.mp3", 0.0, 4.0)


def _shotlist(base: str = "C:/cutwright-tests/materials") -> Shotlist:
    """造一份分镜表；素材路径不必存在，读草稿时也不该去碰文件。"""
    segments = []
    cursor = 0.0
    for name, src_start, src_dur, speed in SHOTS:
        segments.append(VideoSegment(file=name, src_start=src_start,
                                     src_dur=src_dur, tl_start=cursor,
                                     speed=speed, shot=len(segments) + 1))
        cursor = round(cursor + src_dur / speed, 6)
    sl = Shotlist(name="reader-case", material_dir=base, asset_dir=base,
                  canvas=Canvas(1080, 1920), fps=30.0,
                  video_segments=segments,
                  text_segments=[TextSegment(text=text, tl_start=start, tl_dur=dur)
                                 for text, start, dur in TEXTS],
                  audio_segments=[AudioSegment(file=AUDIO[0], tl_start=AUDIO[1],
                                               tl_dur=AUDIO[2], kind="music")])
    sl.base_dir = base
    return sl


def _write_draft(directory: str, *, encrypted: bool = False) -> str:
    shotlist = _shotlist()
    timeline = Timeline.from_shotlist(shotlist)
    content = jy_draft.build_content(timeline)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "draft_content.json")
    raw = json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
    if encrypted:
        import base64
        raw = base64.b64encode(raw)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


class RoundTripTest(unittest.TestCase):
    def test_write_then_read_gives_same_layout(self) -> None:
        with tmpdir("reader") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir)
            draft = read_draft(draft_dir)
            self.assertEqual(len(draft.videos), len(SHOTS))
            for got, (name, src_start, src_dur, speed) in zip(draft.videos, SHOTS):
                self.assertTrue(got["file"].replace("\\", "/").endswith(name))
                self.assertAlmostEqual(got["src_start"], src_start, places=3)
                self.assertAlmostEqual(got["src_dur"], src_dur, places=3)
                self.assertAlmostEqual(got["speed"], speed, places=3)
            self.assertEqual([t["text"] for t in draft.texts],
                             [text for text, _, _ in TEXTS])
            self.assertEqual(len(draft.audios), 1)
            self.assertAlmostEqual(draft.audios[0]["tl_dur"], AUDIO[2], places=3)
            self.assertEqual((draft.canvas.width, draft.canvas.height), (1080, 1920))

    def test_to_shotlist_is_valid_and_keeps_timing(self) -> None:
        with tmpdir("reader-sl") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir)
            shotlist = read_draft(draft_dir).to_shotlist()
            self.assertEqual(len(shotlist.video_segments), len(SHOTS))
            self.assertEqual(len(shotlist.text_segments), len(TEXTS))
            total = sum(seg.tl_dur for seg in shotlist.video_segments)
            self.assertAlmostEqual(shotlist.video_duration(), total, places=2)
            self.assertAlmostEqual(shotlist.video_segments[2].speed, 1.25, places=3)

    def test_missing_files_are_reported_not_hidden(self) -> None:
        with tmpdir("reader-missing") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir)
            draft = read_draft(draft_dir)
            # 素材路径是假的，所以每一条都该被点名，而不是静默生成一份到处 E017 的表
            self.assertEqual(len(draft.missing_files()), len(SHOTS) + 1)


class EncryptedTest(unittest.TestCase):
    def test_plain_only_refuses_encrypted_draft(self) -> None:
        with tmpdir("reader-enc") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir, encrypted=True)
            with self.assertRaises(DraftError) as ctx:
                read_draft(draft_dir, crypto=False)
            self.assertIn("加密", str(ctx.exception))

    def test_plain_text_is_flagged_as_not_encrypted(self) -> None:
        with tmpdir("reader-plain") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir)
            self.assertFalse(read_draft(draft_dir).encrypted)

    def test_round_trip_through_real_cipher(self) -> None:
        """有剪映加密 dll 时，真密文也要能读回来。没有就跳过（不是失败）。"""
        from cutwright.backends.jianying import crypto as jycrypto

        if not jycrypto.available():
            self.skipTest("本机没有可用的 videoeditor.dll，跳过加密往返")
        with tmpdir("reader-real-enc") as scratch:
            draft_dir = os.path.join(scratch, "case")
            os.makedirs(draft_dir)
            content = jy_draft.build_content(
                Timeline.from_shotlist(_shotlist()))
            plain = json.dumps(content, ensure_ascii=False).encode("utf-8")
            with jycrypto.get_crypto() as crypto:
                cipher = crypto.encrypt_bytes(plain)
            self.assertFalse(cipher[:1] == b"{", "密文不该以 { 开头")
            with open(os.path.join(draft_dir, "draft_content.json"), "wb") as handle:
                handle.write(cipher)
            draft = read_draft(draft_dir)
            self.assertTrue(draft.encrypted)
            self.assertEqual(len(draft.videos), len(SHOTS))
            self.assertAlmostEqual(draft.duration, _shotlist().video_duration(), places=2)


class BadInputTest(unittest.TestCase):
    def test_directory_without_content_file(self) -> None:
        with tmpdir("reader-empty") as scratch:
            os.makedirs(os.path.join(scratch, "nothing"))
            with self.assertRaises(DraftError) as ctx:
                read_draft(os.path.join(scratch, "nothing"))
            self.assertIn("draft_content.json", str(ctx.exception))

    def test_not_a_directory(self) -> None:
        with tmpdir("reader-file") as scratch:
            path = os.path.join(scratch, "case.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            with self.assertRaises(DraftError):
                read_draft(path)

    def test_broken_json(self) -> None:
        with tmpdir("reader-broken") as scratch:
            os.makedirs(os.path.join(scratch, "case"))
            with open(os.path.join(scratch, "case", "draft_content.json"), "wb") as handle:
                handle.write(b"{\"tracks\": [")
            with self.assertRaises(DraftError) as ctx:
                read_draft(os.path.join(scratch, "case"))
            self.assertIn("JSON", str(ctx.exception))

    def test_content_without_tracks(self) -> None:
        with self.assertRaises(DraftError):
            read_content({"materials": {}})

    def test_dangling_material_reference_is_a_warning(self) -> None:
        content = {
            "canvas_config": {"width": 720, "height": 1280},
            "materials": {"videos": [], "audios": [], "texts": []},
            "tracks": [{"type": "video", "segments": [
                {"material_id": "gone", "source_timerange": {"start": 0, "duration": 1000000},
                 "target_timerange": {"start": 0, "duration": 1000000}}]}],
        }
        draft = read_content(content)
        self.assertEqual(draft.videos, [])
        joined = " ".join(draft.warnings)
        self.assertIn("gone", joined)
        self.assertIn("没有视频段", joined)
        self.assertEqual((draft.canvas.width, draft.canvas.height), (720, 1280))

    def test_unreadable_crypto_object_explains_itself(self) -> None:
        with tmpdir("reader-crypto") as scratch:
            draft_dir = os.path.join(scratch, "case")
            _write_draft(draft_dir, encrypted=True)
            with self.assertRaises(DraftError) as ctx:
                read_draft(draft_dir, crypto=object())
            self.assertIn("decrypt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
