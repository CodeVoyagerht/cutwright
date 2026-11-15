"""静帧（图片当镜头）的支持测试。

为什么要有这条产线：真实的剪映草稿里图片段很常见（封面、观点卡、图文），
以前读回来会被「已略过」，于是时间线凭空短几秒，而且用户看不出是丢在哪。
现在图片段是一等公民：分镜表里 ``"still": true``，三条产线都认。
"""

from __future__ import annotations

import os
import unittest

from cutwright.backends.jianying import draft as jy_draft
from cutwright.backends.jianying import reader as jy_reader
from cutwright.schema import Canvas, Shotlist, VideoSegment, validate
from cutwright.timeline import Timeline

from . import tmpdir


def _still_shotlist(png: str = "C:/clips/card.png", **over) -> Shotlist:
    seg = VideoSegment(file=png, src_dur=2.0, tl_start=0.0, still=True, **over)
    return Shotlist(name="still-case", material_dir="", asset_dir="",
                    canvas=Canvas(1080, 1920), fps=30.0,
                    video_segments=[seg])


class SchemaStillTest(unittest.TestCase):
    def test_from_dict_reads_still(self) -> None:
        raw = {"video_segments": [{"file": "a.png", "src_dur": 2.0, "still": True}]}
        sl = Shotlist.from_dict(raw)
        self.assertTrue(sl.video_segments[0].still)

    def test_missing_still_means_video(self) -> None:
        sl = Shotlist.from_dict({"video_segments": [{"file": "a.mp4", "src_dur": 2.0}]})
        self.assertFalse(sl.video_segments[0].still)

    def test_to_dict_omits_false_still(self) -> None:
        sl = Shotlist.from_dict({"video_segments": [{"file": "a.mp4", "src_dur": 2.0}]})
        self.assertNotIn("still", sl.video_segments[0].to_dict(),
                         "普通分镜表不该多出一个恒为 false 的字段")

    def test_to_dict_keeps_true_still_and_round_trips(self) -> None:
        seg = Shotlist.from_dict(
            {"video_segments": [{"file": "a.png", "src_dur": 2.0, "still": True}]}
        ).video_segments[0]
        self.assertIs(seg.to_dict().get("still"), True)
        again = VideoSegment.from_dict(seg.to_dict())
        self.assertTrue(again.still)

    def test_still_with_source_in_point_warns(self) -> None:
        sl = _still_shotlist(src_start=1.0)
        codes = [issue.code for issue in validate(sl)]
        self.assertIn("W042", codes)

    def test_still_with_speed_warns(self) -> None:
        sl = _still_shotlist(speed=2.0)
        codes = [issue.code for issue in validate(sl)]
        self.assertIn("W043", codes)

    def test_clean_still_has_no_issue(self) -> None:
        self.assertEqual(validate(_still_shotlist()), [])


class TimelineStillTest(unittest.TestCase):
    def test_still_drops_source_in_and_speed(self) -> None:
        timeline = Timeline.from_shotlist(_still_shotlist(src_start=3.0, speed=2.0))
        clip = timeline.clips[0]
        self.assertTrue(clip.still)
        self.assertEqual(clip.src_in, 0.0, "静帧没有入点，src_start 必须被丢掉")
        self.assertEqual(clip.speed, 1.0, "静帧不变速")
        self.assertAlmostEqual(clip.tl_dur, 2.0, places=3)


class DraftStillTest(unittest.TestCase):
    def test_still_becomes_photo_material(self) -> None:
        timeline = Timeline.from_shotlist(_still_shotlist())
        content = jy_draft.build_content(timeline)
        photos = [m for m in content["materials"].get("videos", [])
                  if str(m.get("type")) == "photo"]
        self.assertEqual(len(photos), 1, "静帧要写成 type=photo 的素材")
        self.assertEqual(photos[0]["duration"], jy_draft.PHOTO_DURATION_SENTINEL)

    def test_still_segment_has_no_speed(self) -> None:
        timeline = Timeline.from_shotlist(_still_shotlist())
        content = jy_draft.build_content(timeline)
        segment = content["tracks"][0]["segments"][0]
        self.assertEqual(float(segment.get("speed", 1.0)), 1.0)
        self.assertEqual(segment["source_timerange"]["start"], 0)


PHOTO_DRAFT = {
    "canvas_config": {"width": 1080, "height": 1920},
    "config": {"fps": 30},
    "duration": 0,
    "tracks": [
        {"id": "t1", "type": "video", "segments": [
            {"id": "s1", "material_id": "P1",
             "source_timerange": {"duration": 3000000},
             "target_timerange": {"start": 0, "duration": 3000000}},
        ]},
    ],
    "materials": {
        "videos": [
            {"id": "P1", "type": "photo", "path": "C:/clips/card.png",
             "duration": 10800000000, "width": 1080, "height": 1920},
        ],
        "texts": [], "audios": [],
    },
}


class ReaderStillTest(unittest.TestCase):
    def test_photo_segment_is_kept_as_still(self) -> None:
        draft = jy_reader.read_content(PHOTO_DRAFT, path=os.path.join("tmp", "case"))
        self.assertEqual(len(draft.videos), 1)
        self.assertTrue(draft.videos[0]["still"], "图片段不该被当成没画面而丢掉")
        shotlist = draft.to_shotlist()
        self.assertTrue(shotlist.video_segments[0].still)
        self.assertEqual(shotlist.video_segments[0].src_start, 0.0)
        self.assertAlmostEqual(shotlist.video_segments[0].src_dur, 3.0, places=3)

    def test_photo_warning_says_it_was_kept(self) -> None:
        draft = jy_reader.read_content(PHOTO_DRAFT)
        joined = " ".join(draft.warnings)
        self.assertIn("静帧", joined)
        self.assertNotIn("已略过", joined)


class PlaceholderPathTest(unittest.TestCase):
    def test_placeholder_is_resolved_against_draft_dir(self) -> None:
        draft_dir = os.path.join(os.sep, "drafts", "ep01")
        path = "##_draftpath_placeholder_0E68_##/textReading/a.wav"
        got = jy_reader._resolve_media(path, draft_dir)
        self.assertEqual(got, os.path.normpath(
            os.path.join(draft_dir, "textReading", "a.wav")))

    def test_absolute_path_is_untouched(self) -> None:
        path = "C:/clips/a.mp4" if jy_reader.os.name == "nt" else "/clips/a.mp4"
        self.assertEqual(jy_reader._resolve_media(path, os.path.join(os.sep, "d")),
                         os.path.normpath(path))

    def test_relative_path_lands_in_draft_dir(self) -> None:
        got = jy_reader._resolve_media("Resources/a.png", os.path.join(os.sep, "d", "x"))
        self.assertEqual(got, os.path.normpath(
            os.path.join(os.sep, "d", "x", "Resources", "a.png")))


if __name__ == "__main__":
    unittest.main()
