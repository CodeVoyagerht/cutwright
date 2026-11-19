"""剧本层：解析、校验、素材匹配、帧级排片。

这里一条 ffmpeg 都不需要——排片是纯计算，素材文件都不用真的存在（只按索引里的
时长算）。真渲那条链路由 ``test_ffmpeg_backend`` 与 CI 的冒烟 job 负责。
"""
from __future__ import annotations

import json
import os
import unittest

from cutwright import screenplay as sp_mod
from cutwright import story

CANVAS = {"width": 1080, "height": 1920}


def make_screenplay(**over) -> sp_mod.Screenplay:
    raw = {
        "name": "测试剧",
        "canvas": CANVAS,
        "fps": 30,
        "cast": {"lin": "林队", "zhou": "周小满"},
        "scenes": [
            {"id": "S1", "slug": "站台", "tags": ["subway", "night"], "lines": [
                {"type": "action", "text": "末班车进站"},
                {"who": "zhou", "text": "你确定他会来？"},
                {"who": "lin", "text": "他欠我一条命"},
            ]},
            {"id": "S2", "slug": "车厢", "tags": ["train"], "lines": [
                {"who": "lin", "type": "voiceover", "text": "那晚我数到第七班车"},
                {"who": "zhou", "text": "别回头"},
            ]},
        ],
    }
    raw.update(over)
    return sp_mod.Screenplay.from_dict(raw)


def make_index() -> story.MaterialIndex:
    """四个片段：两个 subway、一个 train、一个 detail。时长都够长。"""
    return story.MaterialIndex(pieces=[
        story.Piece(file="/m/subway_wide.mp4", start=0.0, dur=4.0,
                    tags=["subway", "night", "wide"]),
        story.Piece(file="/m/subway_zhou.mp4", start=0.0, dur=3.0,
                    tags=["subway", "night", "close"], who=["zhou"]),
        story.Piece(file="/m/train_lin.mp4", start=0.0, dur=4.0,
                    tags=["train", "close"], who=["lin"]),
        story.Piece(file="/m/detail_hand.mp4", start=0.0, dur=2.0,
                    tags=["train", "detail"]),
    ])


class ScreenplayParseTest(unittest.TestCase):
    def test_basic_fields(self):
        sp = make_screenplay()
        self.assertEqual(sp.name, "测试剧")
        self.assertEqual((sp.canvas.width, sp.canvas.height), (1080, 1920))
        self.assertEqual(len(sp.scenes), 2)
        self.assertEqual(len(sp.scenes[0].lines), 3)
        self.assertEqual(sp.speaker_name("lin"), "林队")
        self.assertEqual(sp.speaker_name("unknown"), "unknown")

    def test_line_type_defaults_to_dialogue(self):
        line = sp_mod.Line.from_dict({"text": "喂"})
        self.assertEqual(line.type, "dialogue")
        self.assertEqual(line.caption_style, "caption")

    def test_voiceover_defaults_to_hidden_style(self):
        line = sp_mod.Line.from_dict({"text": "画外音", "type": "voiceover"})
        self.assertEqual(line.caption_style, "vo_hidden")
        self.assertFalse(line.speaks)

    def test_aliases_line_and_say(self):
        self.assertEqual(sp_mod.Line.from_dict({"line": "甲"}).text, "甲")
        self.assertEqual(sp_mod.Line.from_dict({"say": "乙"}).text, "乙")

    def test_tags_accept_comma_string(self):
        scene = sp_mod.Scene.from_dict({"id": "S1", "tags": "subway，night"})
        self.assertEqual(scene.tags, ["subway", "night"])

    def test_location_and_mood_become_tags(self):
        scene = sp_mod.Scene.from_dict({"id": "S1", "location": "subway",
                                       "mood": "紧张"})
        self.assertIn("subway", scene.tags)
        self.assertIn("紧张", scene.tags)

    def test_unknown_fields_survive_round_trip(self):
        raw = {"name": "x", "scenes": [], "my_key": 42,
               "scenes_extra": None}
        sp = sp_mod.Screenplay.from_dict(raw)
        self.assertEqual(sp.to_dict()["my_key"], 42)

    def test_styles_parsed_with_role(self):
        sp = sp_mod.Screenplay.from_dict({
            "name": "x", "scenes": [],
            "styles": {"caption": {"font_height_px": 88}},
        })
        self.assertEqual(sp.styles["caption"].role, "caption")
        self.assertEqual(sp.styles["caption"].font_height_px, 88)

    def test_load_from_file_sets_base_dir(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "examples", "story", "screenplay.json")
        sp = sp_mod.load_screenplay(os.path.abspath(path))
        self.assertEqual(sp.name, "地铁追凶 第1集")
        self.assertTrue(sp.base_dir.endswith(os.path.join("examples", "story")))

    def test_resolve_vo_uses_base_dir(self):
        # 用 os.path.join 拼期望值：这条测的是「相对 base_dir + vo_dir」，
        # 不是「Windows 分隔符长什么样」，别在 Linux 上红。
        sp = make_screenplay(vo_dir="vo")
        sp.base_dir = os.path.join(os.sep, "drama")
        line = sp_mod.Line(text="一", vocal="a.wav")
        self.assertEqual(sp.resolve_vo(line.vocal),
                         os.path.normpath(os.path.join(sp.base_dir, "vo", "a.wav")))


class ScreenplayValidateTest(unittest.TestCase):
    def codes(self, sp, **kw):
        return [i.code for i in sp_mod.validate_screenplay(sp, **kw)]

    def test_clean_screenplay_has_no_issues(self):
        self.assertEqual(self.codes(make_screenplay()), [])

    def test_no_scenes(self):
        self.assertIn("S001", self.codes(make_screenplay(scenes=[])))

    def test_bad_canvas(self):
        self.assertIn("S002", self.codes(make_screenplay(canvas={"width": 0, "height": 0})))

    def test_bad_fps(self):
        self.assertIn("S003", self.codes(make_screenplay(fps=0)))

    def test_duplicate_scene_id(self):
        raw = {"name": "t", "canvas": CANVAS, "scenes": [
            {"id": "S1", "lines": [{"text": "a"}]},
            {"id": "S1", "lines": [{"text": "b"}]},
        ]}
        self.assertIn("S011", self.codes(sp_mod.Screenplay.from_dict(raw)))

    def test_unknown_line_type(self):
        raw = {"name": "t", "canvas": CANVAS, "scenes": [
            {"id": "S1", "lines": [{"text": "a", "type": "sing"}]}]}
        self.assertIn("S020", self.codes(sp_mod.Screenplay.from_dict(raw)))

    def test_empty_text_is_error(self):
        raw = {"name": "t", "canvas": CANVAS, "scenes": [
            {"id": "S1", "lines": [{"type": "dialogue", "text": "  "}]}]}
        self.assertIn("S021", self.codes(sp_mod.Screenplay.from_dict(raw)))

    def test_negative_duration(self):
        raw = {"name": "t", "canvas": CANVAS, "scenes": [
            {"id": "S1", "lines": [{"text": "a", "duration": -1}]}]}
        self.assertIn("S022", self.codes(sp_mod.Screenplay.from_dict(raw)))

    def test_unknown_cast_is_warning(self):
        raw = {"name": "t", "canvas": CANVAS, "cast": {"lin": "林队"}, "scenes": [
            {"id": "S1", "lines": [{"text": "a", "who": "nobody"}]}]}
        issues = sp_mod.validate_screenplay(sp_mod.Screenplay.from_dict(raw))
        self.assertEqual([i.code for i in issues], ["S023"])
        self.assertFalse(sp_mod.has_errors(issues))

    def test_missing_vocal_only_with_check_files(self):
        sp = make_screenplay()
        sp.scenes[0].lines[0].vocal = "nope.wav"
        sp.base_dir = os.path.abspath(os.sep)
        self.assertNotIn("S024", self.codes(sp))
        self.assertIn("S024", self.codes(sp, check_files=True))

    def test_has_errors(self):
        self.assertTrue(sp_mod.has_errors(sp_mod.validate_screenplay(
            make_screenplay(scenes=[]))))
        self.assertFalse(sp_mod.has_errors(sp_mod.validate_screenplay(make_screenplay())))


class MaterialIndexTest(unittest.TestCase):
    def test_tokens_from_filename(self):
        tags = story._tokens_from_filename(r"C:\x\S01_subway_night_wide_take2.mp4")
        self.assertEqual(tags, ["s01", "subway", "night", "wide"])

    def test_tokens_drop_pure_digits(self):
        self.assertEqual(story._tokens_from_filename("12_subway_007.mp4"), ["subway"])

    def test_tokens_handle_hyphen_and_space(self):
        tags = story._tokens_from_filename("S01-subway night close.mov")
        self.assertEqual(tags, ["s01", "subway", "night", "close"])

    def test_from_dict_resolves_relative_to_json_dir(self):
        base = os.path.join(os.sep, "proj")
        index = story.MaterialIndex.from_dict(
            {"pieces": [{"file": "m/a.mp4", "dur": 2.0}]}, base_dir=base)
        self.assertEqual(index.pieces[0].file,
                         os.path.normpath(os.path.join(base, "m", "a.mp4")))

    def test_round_trip(self):
        index = make_index()
        again = story.MaterialIndex.from_dict(json.loads(
            json.dumps(index.to_dict())))
        self.assertEqual(len(again.pieces), 4)
        self.assertEqual(again.pieces[1].who, ["zhou"])
        self.assertEqual(again.pieces[0].tags, ["subway", "night", "wide"])


class AllocateFramesTest(unittest.TestCase):
    """分配器的唯一硬要求：求和精确等于总帧数，且没有 0 帧的镜头。"""

    def test_short_line_is_one_shot(self):
        self.assertEqual(story._allocate_frames(24, story.StoryOptions(), 30.0), [24])

    def test_sum_is_exact_for_many_totals(self):
        opts = story.StoryOptions()
        for total in range(1, 400):
            chunks = story._allocate_frames(total, opts, 30.0)
            self.assertEqual(sum(chunks), total, f"total={total} chunks={chunks}")
            self.assertTrue(all(c >= 1 for c in chunks), f"total={total} 有 0 帧镜头")

    def test_respects_max_shot(self):
        opts = story.StoryOptions(max_shot=2.0)
        chunks = story._allocate_frames(180, opts, 30.0)   # 6s，最长 2s → 至少 3 段
        self.assertGreaterEqual(len(chunks), 3)
        self.assertLessEqual(max(chunks), 60 + 1)

    def test_long_line_is_split(self):
        opts = story.StoryOptions(max_shot=2.0)
        self.assertGreater(len(story._allocate_frames(150, opts, 30.0)), 1)

    def test_no_rhythm_is_uniform(self):
        opts = story.StoryOptions(rhythm=False, max_shot=2.0)
        chunks = story._allocate_frames(120, opts, 30.0)
        self.assertEqual(len(set(chunks)), 1, f"不均匀: {chunks}")

    def test_rhythm_makes_them_differ(self):
        opts = story.StoryOptions(rhythm=True, max_shot=2.0)
        chunks = story._allocate_frames(150, opts, 30.0)
        self.assertGreater(len(set(chunks)), 1, f"没有长短交错: {chunks}")


class LineSecondsTest(unittest.TestCase):
    def setUp(self):
        self.sp = make_screenplay()
        self.opts = story.StoryOptions()
        self.report = story.StoryReport()

    def secs(self, line):
        return story._line_seconds(line, self.sp, self.opts, self.report)

    def test_explicit_duration_wins(self):
        line = sp_mod.Line(text="很短", duration=3.0)
        self.assertEqual(self.secs(line), 3.0)

    def test_longer_text_takes_longer(self):
        short = self.secs(sp_mod.Line(text="跑"))
        long = self.secs(sp_mod.Line(text="你确定他真的会来这趟末班车吗"))
        self.assertGreater(long, short)

    def test_pause_is_added(self):
        a = self.secs(sp_mod.Line(text="一样长", pause=0.0))
        b = self.secs(sp_mod.Line(text="一样长", pause=1.0))
        self.assertAlmostEqual(b - a, 1.0 - self.opts.pause_default, places=3)

    def test_voiceover_uses_slower_cps(self):
        """旁白的字数/秒更小 → 同样的字要念更久（原来这条断言方向写反了）。"""
        dialogue = self.secs(sp_mod.Line(text="同样一句话在这里", type="dialogue"))
        voice = self.secs(sp_mod.Line(text="同样一句话在这里", type="voiceover"))
        self.assertGreater(voice, dialogue)

    def test_title_has_fixed_length(self):
        self.assertEqual(self.secs(sp_mod.Line(text="", type="title")),
                         self.opts.title_seconds)

    def test_missing_vocal_warns_and_falls_back(self):
        sp = make_screenplay()
        sp.base_dir = os.path.abspath(os.sep)
        line = sp_mod.Line(text="旁白", type="voiceover", vocal="nope.wav")
        self.assertGreater(story._line_seconds(line, sp, self.opts, self.report), 0)
        self.assertTrue(any("不存在" in w for w in self.report.warnings))


class BuildShotlistTest(unittest.TestCase):
    def build(self, **opts):
        sp = make_screenplay()
        options = story.StoryOptions(**opts)
        return (*story.build_shotlist(sp, make_index(), options), options)

    def test_every_line_gets_a_shot(self):
        sl, report, _ = self.build()
        self.assertEqual(len(sl.video_segments), 5)     # 5 句 → 5 镜
        self.assertTrue(report.ok)
        self.assertEqual(report.shots, 5)

    def test_timeline_is_continuous(self):
        sl, _, _ = self.build()
        clock = 0.0
        for seg in sl.video_segments:
            self.assertAlmostEqual(seg.tl_start, clock, places=3)
            clock += seg.tl_dur
        self.assertAlmostEqual(sl.video_duration(), clock, places=3)

    def test_long_line_caption_covers_all_its_shots(self):
        """长台词会被拆成 2 镜，字幕必须从**第一镜**开始。

        这条是压测逼出来的：字幕原先锚在最后一镜上，于是它压到下一句头上，
        屏幕上同时挂两行字（实测重叠 2.1s）。
        """
        sp = sp_mod.Screenplay(
            name="长句", canvas=sp_mod.Canvas(1080, 1920), fps=30.0,
            material_dir="m",
            scenes=[sp_mod.Scene(id="S1", tags=["subway", "night"], lines=[
                sp_mod.Line(text="这一句特意写得足够长，长到一镜放不下必须拆成两镜" * 2),
                sp_mod.Line(text="短句"),
            ])],
        )
        sl, report = story.build_shotlist(sp, make_index(), story.StoryOptions())
        self.assertGreaterEqual(len(sl.video_segments), 2, "长句应当被拆成多镜")
        first, second = sl.text_segments[0], sl.text_segments[1]
        line_shots = [v for v in sl.video_segments if v.tl_start < second.tl_start]
        self.assertGreaterEqual(len(line_shots), 2)
        self.assertAlmostEqual(first.tl_start, line_shots[0].tl_start, places=3)
        covered = sum(v.tl_dur for v in line_shots)
        self.assertAlmostEqual(first.tl_dur, covered, places=3)
        self.assertLessEqual(first.tl_start + first.tl_dur,
                             second.tl_start + 1e-3,
                             "字幕不能压到下一句上")

    # -- 节奏建议 ---------------------------------------------------------- #
    def _one_piece_index(self):
        return story.MaterialIndex(pieces=[
            story.Piece(file="/m/subway_wide.mp4", start=0.0, dur=6.0,
                        tags=["subway", "night", "wide"]),
        ])

    def test_scene_with_one_piece_is_called_out(self):
        sp = sp_mod.Screenplay(
            name="单段", canvas=sp_mod.Canvas(1080, 1920), fps=30.0,
            material_dir="m",
            scenes=[sp_mod.Scene(id="S1", tags=["subway", "night"], lines=[
                sp_mod.Line(text="第一句话在这里"),
                sp_mod.Line(text="第二句话也在这里"),
                sp_mod.Line(text="第三句话还在这里"),
                sp_mod.Line(text="第四句话仍在这里"),
            ])],
        )
        _, report = story.build_shotlist(sp, self._one_piece_index(),
                                         story.StoryOptions())
        joined = " ".join(report.rhythm)
        self.assertIn("S1", joined)
        self.assertIn("1 段素材", joined)

    def test_uniform_lengths_are_called_out(self):
        # 五句一样长 → 无节奏权重差异时长度几乎一致，应当提醒「节奏单一」
        lines = [sp_mod.Line(text="这是一句长度完全相同的台词") for _ in range(8)]
        sp = sp_mod.Screenplay(
            name="齐整", canvas=sp_mod.Canvas(1080, 1920), fps=30.0,
            material_dir="m",
            scenes=[sp_mod.Scene(id="S1", tags=["subway", "night"], lines=lines)],
        )
        index = story.MaterialIndex(pieces=[
            story.Piece(file=f"/m/p{i}.mp4", start=0.0, dur=6.0,
                        tags=["subway", "night"]) for i in range(4)
        ])
        _, report = story.build_shotlist(sp, index, story.StoryOptions(rhythm=False))
        joined = " ".join(report.rhythm)
        self.assertIn("节奏单一", joined)

    def test_short_film_gets_no_noise(self):
        sl, report, _ = self.build()
        for note in report.rhythm:
            self.assertNotIn("建议优化", note, "别写这种没信息量的建议")

    def test_rhythm_notes_are_in_report_dict(self):
        _, report, _ = self.build()
        self.assertIn("rhythm", report.to_dict())

    def test_report_counts_reuse(self):
        sl, report, _ = self.build()
        self.assertEqual(sum(report.reuse.values()), len(sl.video_segments))
        counts = list(report.reuse.values())
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertLessEqual(len(report.reuse), 5)

    def test_frame_grid_is_respected(self):
        sl, _, _ = self.build()
        for seg in sl.video_segments:
            frames = seg.tl_dur * 30.0
            self.assertLess(abs(frames - round(frames)), 0.02,
                            f"{seg.shot} 不在帧网格上: {frames}")

    def test_no_adjacent_same_piece(self):
        sl, _, _ = self.build()
        for prev, cur in zip(sl.video_segments, sl.video_segments[1:]):
            self.assertNotEqual((prev.file, prev.src_start),
                                (cur.file, cur.src_start))

    def test_same_scene_tags_prefer_matching_material(self):
        sl, _, _ = self.build()
        first = sl.video_segments[0]
        self.assertIn("subway", first.file)      # S1 的标签是 subway/night

    def test_dialogue_makes_captions(self):
        sl, _, _ = self.build()
        self.assertEqual(len(sl.text_segments), 4)   # 3 句 + action 1 句

    def test_voiceover_goes_to_hidden_track(self):
        sl, _, _ = self.build()
        self.assertEqual(len(sl.vo_segments), 1)
        self.assertEqual(sl.vo_segments[0].style, "vo_hidden")

    def test_vo_captions_option_switches_style(self):
        sl, _, _ = self.build(vo_captions=True)
        self.assertEqual(sl.vo_segments[0].style, "caption")

    def test_captions_can_be_disabled(self):
        sl, _, _ = self.build(captions=False)
        self.assertEqual(len(sl.text_segments), 0)

    def test_speaker_prefix(self):
        sl, _, _ = self.build(speaker_prefix=True)
        joined = " ".join(t.text for t in sl.text_segments)
        self.assertIn("林队：", joined)

    def test_acts_follow_scenes(self):
        sl, report, _ = self.build()
        self.assertEqual([a.key for a in sl.acts], ["S1", "S2"])
        self.assertEqual(len(report.per_scene), 2)
        self.assertAlmostEqual(sum(a.dur for a in sl.acts), sl.video_duration(), places=2)

    def test_total_duration_matches_report(self):
        sl, report, _ = self.build()
        self.assertAlmostEqual(report.duration, sl.video_duration(), places=3)

    def test_short_material_is_slowed_down(self):
        index = story.MaterialIndex(pieces=[
            story.Piece(file="/m/short.mp4", start=0.0, dur=1.0, tags=["subway", "night"]),
            story.Piece(file="/m/long.mp4", start=0.0, dur=1.0, tags=["subway", "night"]),
        ])
        sp = make_screenplay(scenes=[{"id": "S1", "tags": ["subway"], "lines": [
            {"text": "这一句很长很长很长很长很长很长"}]}])
        sl, report = story.build_shotlist(sp, index, story.StoryOptions())
        self.assertEqual(len(sl.video_segments), 1)
        seg = sl.video_segments[0]
        self.assertLess(seg.speed, 1.0)
        self.assertAlmostEqual(seg.tl_dur, seg.src_dur / seg.speed, places=3)
        self.assertTrue(report.ok)

    def test_too_short_material_is_skipped_not_slowed_absurdly(self):
        index = story.MaterialIndex(pieces=[
            story.Piece(file="/m/tiny.mp4", start=0.0, dur=0.2, tags=["subway"]),
        ])
        sp = make_screenplay(scenes=[{"id": "S1", "tags": ["subway"], "lines": [
            {"text": "很长的台词" * 5, "duration": 8.0}]}])
        sl, report = story.build_shotlist(sp, index, story.StoryOptions())
        # 0.2s 素材撑 4s 需要 0.05 倍速 → 放弃，宁可不排也不出糊到看不清的慢放
        self.assertEqual(len(sl.video_segments), 0)
        self.assertFalse(report.ok)
        self.assertTrue(report.unresolved)

    def test_no_matching_tags_still_returns_shots_and_warns(self):
        index = story.MaterialIndex(pieces=[
            story.Piece(file="/m/other.mp4", start=0.0, dur=5.0, tags=["desert"]),
        ])
        sp = make_screenplay(scenes=[{"id": "S1", "tags": ["subway"], "lines": [
            {"text": "找不到对应的素材"}]}])
        sl, report = story.build_shotlist(sp, index, story.StoryOptions())
        self.assertEqual(len(sl.video_segments), 1)
        self.assertTrue(any("没有任何素材命中" in w for w in report.warnings))

    def test_empty_index_is_not_ok(self):
        sp = make_screenplay(scenes=[{"id": "S1", "lines": [{"text": "一句话"}]}])
        sl, report = story.build_shotlist(sp, story.MaterialIndex(),
                                          story.StoryOptions())
        self.assertEqual(len(sl.video_segments), 0)
        self.assertFalse(report.ok)
        self.assertTrue(report.warnings)

    def test_output_passes_shotlist_validation(self):
        from cutwright.schema import validate
        sl, _, _ = self.build()
        errors = [i for i in validate(sl) if i.level == "error"]
        self.assertEqual(errors, [], [str(e) for e in errors])


if __name__ == "__main__":
    unittest.main()
