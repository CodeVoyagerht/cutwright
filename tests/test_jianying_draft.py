"""剪映后端的单元测试。

只用合成数据：临时目录里的假素材 + 手写的小时间线（3 段视频 / 2 条字幕 /
1 段旁白 / 1 个图片叠加层，画布 1080x1920）。**任何用例都不会写真实草稿
目录**——``build_draft_dir`` 落盘到仓库内的临时目录，``install_draft`` 只跑
``dry_run``。

跑法（仓库根目录）::

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
from typing import List

from cutwright.backends.jianying import JianyingBackend
from cutwright.backends.jianying import crypto as jy_crypto
from cutwright.backends.jianying.draft import (
    CHECK_FLAG_VIDEO,
    NEW_VERSION,
    PHOTO_DURATION_SENTINEL,
    STICKER_ANIMATION_TYPE,
    STYLE_TRACK_ORDER,
    TEXT_RENDER_INDEX,
    VERSION,
    build_content,
    validate_content,
)
from cutwright.backends.jianying.install import (
    build_draft_dir,
    install_draft,
    list_installed,
    uninstall_draft,
    verify_draft,
)
from cutwright.schema import Canvas, TextStyle
from cutwright.timeline import (
    Timeline,
    TimelineAudio,
    TimelineClip,
    TimelineOverlay,
    TimelineText,
)

#: 顶层必须齐全的键（实测规格 14 键 + 时长）。
TOP_KEYS = {
    "id", "version", "new_version", "duration", "config", "canvas_config",
    "tracks", "materials", "keyframes", "platform", "last_modified_platform",
    "render_index_track_mode_on", "path", "uneven_animation_template_info",
    "smart_ads_info", "function_assistant_info",
}

#: 关键词换色（与 TextStyle.keyword_color 一致）。
KW_COLOR = "#FFD54A"

#: 临时目录的根。放在仓库里的原因是受限环境里系统临时目录未必可写，
#: 而这里只是些几十字节的占位素材，跑完即删。
BASE_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".tmp-jianying")


#: 本进程建过的临时目录（tearDownModule 只清这些）。
_MADE: List[str] = []


def _mkdtemp(prefix: str) -> str:
    """在仓库内的临时根下建一个测试目录。

    刻意不用 :func:`tempfile.mkdtemp`：它按 ``0o700`` 建目录，在某
    些 Windows 受限环境里会落成拒绝 ACE，之后写文件直接 PermissionError。

    目录名里带进程号：两个测试进程同时跑时（CI、并行跑不同模块），谁都不该
    删掉对方正在用的目录。
    """
    os.makedirs(BASE_TMP, exist_ok=True)
    path = os.path.join(
        BASE_TMP, "%s%d-%s" % (prefix, os.getpid(), uuid.uuid4().hex[:8]))
    os.makedirs(path, exist_ok=True)
    _MADE.append(path)
    return path


def tearDownModule() -> None:
    """只清本进程建过的临时目录，不碰临时根（可能有别的进程在用）。"""
    for path in _MADE:
        shutil.rmtree(path, ignore_errors=True)


def _touch(path: str, payload: bytes = b"fake") -> str:
    """造一个占位素材文件（内容无关，草稿只引用路径）。"""
    with open(path, "wb") as fh:
        fh.write(payload)
    return path


def make_timeline(root: str):
    """造一条覆盖四种素材的小时间线，返回 ``(timeline, media)``。"""
    videos = [
        _touch(os.path.join(root, "clip1.mp4"), b"\x00" * 32),
        _touch(os.path.join(root, "clip2.mp4"), b"\x00" * 32),
        _touch(os.path.join(root, "clip3.mp4"), b"\x00" * 32),
    ]
    image = _touch(os.path.join(root, "overlay.jpg"), b"\xff\xd8\xff")
    music = _touch(os.path.join(root, "music.mp3"), b"\x00" * 16)

    clips = [
        TimelineClip(index=0, source=videos[0], src_in=0.0, src_out=2.02,
                     tl_start=0.0),
        TimelineClip(index=1, source=videos[1], src_in=0.5, src_out=2.5,
                     tl_start=2.02),
        TimelineClip(index=2, source=videos[2], src_in=0.0, src_out=1.5,
                     tl_start=4.02),
    ]
    styles = {
        "caption": TextStyle(role="caption", font_height_px=100.0,
                             color="#FFFFFF", keyword_color=KW_COLOR,
                             stroke_color="#000000", stroke_width_px=6.0,
                             bottom_margin_px=760.0),
        "vo_hidden": TextStyle(role="vo", font_height_px=100.0,
                               invisible=True),
    }
    texts = [
        TimelineText(index=0, text="开场|第一句", tl_start=0.2, tl_dur=1.5,
                     style_name="caption", style=styles["caption"]),
        TimelineText(index=1, text="第二句字幕", tl_start=2.5, tl_dur=1.6,
                     style_name="caption", style=styles["caption"]),
        TimelineText(index=2, text="旁白不该被看见", tl_start=0.5, tl_dur=2.0,
                     style_name="vo_hidden", style=styles["vo_hidden"],
                     role="vo"),
    ]
    overlays = [TimelineOverlay(index=0, image=image, tl_start=3.0, tl_dur=1.2)]
    audios = [TimelineAudio(index=0, source=music, tl_start=0.0, tl_dur=5.52)]
    timeline = Timeline(
        name="单元测试草稿", canvas=Canvas(1080, 1920), fps=30.0,
        duration=5.52, clips=clips, texts=texts, audios=audios,
        overlays=overlays, styles=styles)
    media = {
        os.path.abspath(videos[0]): {"path": videos[0], "kind": "video",
                                     "duration_us": 3_000_000,
                                     "width": 1080, "height": 1920},
        os.path.abspath(videos[1]): {"path": videos[1], "kind": "video",
                                     "duration_us": 3_000_000,
                                     "width": 1080, "height": 1920},
        os.path.abspath(videos[2]): {"path": videos[2], "kind": "video",
                                     "duration_us": 3_000_000,
                                     "width": 1080, "height": 1920},
        os.path.abspath(image): {"path": image, "kind": "photo",
                                 "duration_us": 0, "width": 1080,
                                 "height": 1920},
    }
    return timeline, media


def _materials_by_id(content):
    """把 materials 摊平成 ``{id: (类别, 素材)}``。"""
    out = {}
    for category, items in (content.get("materials") or {}).items():
        for item in items or []:
            out[item["id"]] = (category, item)
    return out


class TestDraftContent(unittest.TestCase):
    """Timeline → draft_content 的纯函数测试。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = _mkdtemp("content-")
        cls.timeline, cls.media = make_timeline(cls.tmp)
        cls.content = build_content(cls.timeline, media=cls.media)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_top_level_shape(self):
        """① 顶层关键字段齐全，画布/版本对得上。"""
        missing = TOP_KEYS - set(self.content)
        self.assertFalse(missing, "草稿顶层缺少 %s" % sorted(missing))
        self.assertEqual(self.content["version"], VERSION)
        self.assertEqual(self.content["new_version"], NEW_VERSION)
        self.assertEqual(self.content["canvas_config"],
                         {"width": 1080, "height": 1920})
        self.assertNotIn("ratio", self.content["canvas_config"])
        self.assertEqual(self.content["render_index_track_mode_on"], True)
        self.assertIsInstance(self.content["id"], str)
        self.assertTrue(self.content["id"])
        self.assertIsInstance(self.content["duration"], int)

    def test_02_video_segment_refs_resolve(self):
        """② 每个 video segment 的 material_id 与 extra_material_refs 都能解析。"""
        by_id = _materials_by_id(self.content)
        video_tracks = [t for t in self.content["tracks"] if t["type"] == "video"]
        self.assertEqual(len(video_tracks), 2, "应有主视频轨 + 图片叠加轨")
        seg_count = 0
        for track in video_tracks:
            for seg in track["segments"]:
                seg_count += 1
                self.assertIn(seg["material_id"], by_id,
                              "material_id 悬空: %r" % (seg["material_id"],))
                self.assertEqual(by_id[seg["material_id"]][0], "videos")
                self.assertEqual(len(seg["extra_material_refs"]), 6,
                                 "视频段必须带 6 类辅助素材")
                for ref in seg["extra_material_refs"]:
                    self.assertIn(ref, by_id,
                                  "extra_material_refs 悬空: %r" % (ref,))
        self.assertEqual(seg_count, 4, "3 段视频 + 1 个叠加层")

    def test_03_track_order_and_layering(self):
        """③ 轨道顺序 = video / 图片叠加 / caption / vo_hidden，层级递增。"""
        tracks = self.content["tracks"]
        types = [t["type"] for t in tracks]
        self.assertEqual(types[0], "video")
        self.assertNotIn("flag", tracks[0], "主视频轨不写 flag")
        self.assertEqual(types[1], "video")
        self.assertEqual(tracks[1].get("flag"), 2, "图片叠加轨 flag=2")
        text_tracks = [t for t in tracks if t["type"] == "text"]
        self.assertEqual(len(text_tracks), 2, "caption 与 vo_hidden 各一条轨")
        # 样式顺序：caption 在 vo_hidden 之前（数组越靠后越上层）
        self.assertLess(STYLE_TRACK_ORDER.index("caption"),
                        STYLE_TRACK_ORDER.index("vo_hidden"))
        for idx, track in enumerate(tracks):
            for seg in track["segments"]:
                if track["type"] == "text":
                    self.assertEqual(seg["render_index"], TEXT_RENDER_INDEX)
                    self.assertEqual(seg["track_render_index"], idx,
                                     "track_render_index 必须等于轨在 tracks 里的下标")

    def test_04_text_content_json(self):
        """④ 文字素材 content 是合法 JSON，且含关键词换色。"""
        by_id = _materials_by_id(self.content)
        keyword = 0
        for track in self.content["tracks"]:
            if track["type"] != "text":
                continue
            for seg in track["segments"]:
                category, material = by_id[seg["material_id"]]
                self.assertEqual(category, "texts")
                self.assertEqual(material["type"], "text")
                payload = json.loads(material["content"])
                self.assertIn("text", payload)
                self.assertIn("styles", payload)
                self.assertNotIn("|", payload["text"], "content 里不该残留 | 分隔符")
                for part in payload["styles"]:
                    self.assertEqual(len(part["range"]), 2)
                    self.assertLessEqual(part["range"][1], len(payload["text"]))
                    self.assertIn("fill", part)
                if len(payload["styles"]) > 1:
                    keyword += 1
                    first = payload["styles"][0]["fill"]["content"]["solid"]["color"]
                    second = payload["styles"][1]["fill"]["content"]["solid"]["color"]
                    self.assertNotEqual(first, second, "关键词换色没生效")
                    self.assertEqual(first, [1.0, 1.0, 1.0], "正文应为白色")
                    # 实现里 hex -> [0,1] 会保留 6 位小数，这里按同样精度比对
                    self.assertEqual(
                        second,
                        [round(255 / 255.0, 6), round(213 / 255.0, 6),
                         round(74 / 255.0, 6)],
                        "关键词色应为 #FFD54A")
        self.assertGreaterEqual(keyword, 1, "至少要有一条带 | 换色的字幕")

    def test_05_vo_hidden_is_invisible(self):
        """旁白文字必须在画布外（y<=-2.0），不能真的显示出来。"""
        by_id = _materials_by_id(self.content)
        hidden = 0
        for track in self.content["tracks"]:
            if track["type"] != "text":
                continue
            for seg in track["segments"]:
                _, material = by_id[seg["material_id"]]
                if json.loads(material["content"])["text"] != "旁白不该被看见":
                    continue
                hidden += 1
                transform = seg["clip"]["transform"]
                self.assertLessEqual(transform["y"], -2.0,
                                     "旁白应移出画布（y<=-2.0）")
        self.assertEqual(hidden, 1, "旁白段应恰好一条")

    def test_06_microsecond_conversion(self):
        """⑤ 2.02s → 2020000µs，且总时长 = 最后一段结束。"""
        first = self.content["tracks"][0]["segments"][0]
        self.assertEqual(first["target_timerange"]["start"], 0)
        self.assertEqual(first["target_timerange"]["duration"], 2_020_000)
        self.assertEqual(first["source_timerange"]["duration"], 2_020_000)
        self.assertEqual(self.content["duration"], 5_520_000)
        for track in self.content["tracks"]:
            for seg in track["segments"]:
                for key in ("start", "duration"):
                    self.assertIsInstance(seg["target_timerange"][key], int)

    def test_07_photo_overlay_material(self):
        """图片叠加用 type=photo 素材，时长是占位哨兵值。"""
        by_id = _materials_by_id(self.content)
        overlay = self.content["tracks"][1]["segments"][0]
        _, material = by_id[overlay["material_id"]]
        self.assertEqual(material["type"], "photo")
        self.assertEqual(material["duration"], PHOTO_DURATION_SENTINEL)
        self.assertFalse(material["has_audio"])
        self.assertNotIn("start", overlay["source_timerange"])

    def test_08_check_flag_and_platform(self):
        """视频素材带 check_flag，路径用正斜杠，platform 是剪映认的那套键。"""
        by_id = _materials_by_id(self.content)
        video = by_id[self.content["tracks"][0]["segments"][0]["material_id"]][1]
        self.assertEqual(video["check_flag"], CHECK_FLAG_VIDEO)
        self.assertEqual(video["type"], "video")
        self.assertIn(".mp4", video["path"])
        self.assertNotIn("\\", video["path"], "素材路径要用正斜杠")
        for key in ("os", "app_id", "app_version", "device_id"):
            self.assertIn(key, self.content["platform"])

    def test_09_audio_refs(self):
        """音频段的 extra_material_refs 是 4 类（与视频段不同）。"""
        by_id = _materials_by_id(self.content)
        audio_tracks = [t for t in self.content["tracks"] if t["type"] == "audio"]
        self.assertTrue(audio_tracks)
        for track in audio_tracks:
            for seg in track["segments"]:
                self.assertEqual(by_id[seg["material_id"]][0], "audios")
                self.assertEqual(len(seg["extra_material_refs"]), 4)
                for ref in seg["extra_material_refs"]:
                    self.assertIn(ref, by_id)

    def test_10_text_anim_sticker(self):
        """每段文字挂一条 sticker_animation 素材。"""
        by_id = _materials_by_id(self.content)
        animations = [v for v in by_id.values() if v[0] == "material_animations"]
        segs = sum(len(t["segments"]) for t in self.content["tracks"]
                   if t["type"] == "text")
        self.assertEqual(len(animations), segs)
        for _, material in animations:
            self.assertEqual(material["type"], STICKER_ANIMATION_TYPE)

    def test_11_validate_ok(self):
        """⑥ validate_content 返回 ok=True，且 stats 有内容。"""
        report = validate_content(self.content)
        self.assertTrue(report["ok"], "校验错误: %s" % report["errors"])
        self.assertEqual(report["errors"], [])
        stats = report["stats"]
        self.assertEqual(stats["duration_us"], 5_520_000)
        self.assertEqual(stats["text_tracks"], 2)
        self.assertGreaterEqual(stats["segments"], 8)

    def test_12_validate_catches_dangling_ref(self):
        """校验器真的会抓悬空引用（否则上面的 ok=True 没意义）。"""
        broken = json.loads(json.dumps(self.content))
        broken["tracks"][0]["segments"][0]["material_id"] = "NOT-EXIST"
        report = validate_content(broken)
        self.assertFalse(report["ok"])
        self.assertTrue(any("悬空" in e for e in report["errors"]))

    def test_13_validate_catches_overlap(self):
        """同轨重叠要报错。"""
        broken = json.loads(json.dumps(self.content))
        track = broken["tracks"][0]
        track["segments"][1]["target_timerange"]["start"] = 1_000_000
        report = validate_content(broken)
        self.assertFalse(report["ok"])
        self.assertTrue(any("重叠" in e for e in report["errors"]))

    def test_14_no_hardcoded_project_values(self):
        """原项目的绝对路径 / 项目名不能漏进草稿。"""
        blob = json.dumps(self.content, ensure_ascii=False)
        for bad in ("宝妈第三期", "D:\\deskstop", "C:\\Users\\life2",
                    "JianyingPro Drafts"):
            self.assertNotIn(bad, blob)


class TestDraftDir(unittest.TestCase):
    """build_draft_dir + verify_draft + dry-run 安装。"""

    def setUp(self):
        self.tmp = _mkdtemp("dir-")
        self.timeline, self.media = make_timeline(self.tmp)
        self.content = build_content(self.timeline, media=self.media)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_build_and_verify(self):
        """草稿目录该有的文件都在，回读校验通过。"""
        target = os.path.join(self.tmp, "out", "草稿A")
        report = build_draft_dir(self.content, target, name="草稿A")
        self.assertTrue(report["ok"], report.get("warnings"))
        for rel in ("draft_content.json", "draft_meta_info.json",
                    "draft_settings", "timeline_layout.json",
                    "Timelines/project.json"):
            path = os.path.join(target, rel.replace("/", os.sep))
            self.assertTrue(os.path.isfile(path), "缺少 %s" % rel)
        timeline_id = report["timeline_id"]
        sub = os.path.join(target, "Timelines", timeline_id, "draft_content.json")
        self.assertTrue(os.path.isfile(sub), "多时间线双写缺失")

        # 两份 draft_content 必须内容一致
        with open(os.path.join(target, "draft_content.json"), "rb") as fh:
            root_content = json.loads(fh.read().decode("utf-8"))
        with open(sub, "rb") as fh:
            sub_content = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(root_content, sub_content)
        self.assertEqual(root_content["id"], timeline_id)

        with open(os.path.join(target, "draft_meta_info.json"), "rb") as fh:
            meta = json.loads(fh.read().decode("utf-8"))
        self.assertEqual(meta["draft_name"], "草稿A")
        self.assertEqual(meta["tm_duration"], self.content["duration"])
        self.assertEqual(meta["draft_id"], report["draft_id"])
        self.assertNotEqual(meta["draft_id"], timeline_id,
                            "容器 id 与时间线 id 必须是两个")
        self.assertNotIn("_project_id", meta)

        verify = verify_draft(target, expect_name="草稿A")
        self.assertTrue(verify["ok"], verify["checks"])
        failed = [c["name"] for c in verify["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(verify["content_id"], timeline_id)

    def test_02_rebuild_is_allowed(self):
        """自己生成过的目录可以重建（有标记文件）。"""
        target = os.path.join(self.tmp, "out", "草稿B")
        build_draft_dir(self.content, target, name="草稿B")
        build_draft_dir(self.content, target, name="草稿B")   # 不应抛
        self.assertTrue(verify_draft(target)["ok"])

    def test_03_refuse_foreign_dir(self):
        """非空且不是本工具生成的目录：拒绝覆盖，且不删。"""
        target = os.path.join(self.tmp, "user-draft")
        os.makedirs(target)
        keep = os.path.join(target, "draft_content.json")
        _touch(keep, b'{"id":"user"}')
        with self.assertRaises(FileExistsError):
            build_draft_dir(self.content, target, name="x")
        self.assertTrue(os.path.isfile(keep), "拒绝覆盖时绝不能删用户文件")

    def test_04_dry_run_install_plan(self):
        """dry_run 只出计划，草稿库不落盘、白名单不变。"""
        target = os.path.join(self.tmp, "out", "草稿C")
        build_draft_dir(self.content, target, name="草稿C")
        drafts_root = os.path.join(self.tmp, "drafts")
        os.makedirs(drafts_root)
        install_target = os.path.join(drafts_root, "草稿C")

        before = list_installed(drafts_root)
        plan = install_draft(target, drafts_root=drafts_root, name="草稿C",
                             dry_run=True)
        self.assertTrue(plan["ok"], plan.get("errors"))
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["installed"])
        self.assertTrue(plan["planned"])
        self.assertEqual(os.path.normcase(plan["target"]),
                         os.path.normcase(install_target))
        self.assertTrue(plan["steps"])
        self.assertFalse(os.path.exists(install_target), "dry_run 不许写盘")
        self.assertEqual(list_installed(drafts_root), before)
        self.assertFalse(
            os.path.exists(os.path.join(drafts_root, "root_meta_info.json")),
            "dry_run 不许碰 root_meta_info.json")

    def test_05_dry_run_uninstall_unknown(self):
        """没装过的东西不能卸（只碰自己的）。"""
        drafts_root = os.path.join(self.tmp, "drafts2")
        os.makedirs(drafts_root)
        report = uninstall_draft("别人的草稿", drafts_root=drafts_root,
                                 dry_run=True)
        self.assertFalse(report["ok"])
        self.assertTrue(report.get("error"))

    def test_06_encrypt_without_dll(self):
        """没 DLL 时要加密：直接抛 CryptoError 且不写半个草稿。"""
        if jy_crypto.available():
            self.skipTest("本机有 videoeditor.dll，跳过降级用例")
        target = os.path.join(self.tmp, "out", "草稿D")
        with self.assertRaises(jy_crypto.CryptoError):
            build_draft_dir(self.content, target, name="草稿D", encrypt=True)
        self.assertFalse(os.path.isdir(target), "失败时不该留下目录")
        self.assertIn("videoeditor.dll", jy_crypto.NO_DLL_HINT)


class TestCrypto(unittest.TestCase):
    """加解密：没有 DLL 的部分永远可测，有 DLL 的部分才跑。"""

    def test_plain_json_detection(self):
        self.assertTrue(jy_crypto.is_plain_json(b'{"id":"x"}'))
        self.assertTrue(jy_crypto.is_plain_json(b"  [1,2]"))
        self.assertFalse(jy_crypto.is_plain_json(b"gAEBAAAA"))
        self.assertFalse(jy_crypto.is_plain_json(b""))

    def test_content_file_whitelist(self):
        self.assertTrue(jy_crypto.is_content_file_path("draft_content.json"))
        self.assertTrue(jy_crypto.is_content_file_path("draft_meta_info.json"))
        self.assertTrue(jy_crypto.is_content_file_path(
            "Timelines/ABC/draft_content.json"))
        self.assertFalse(jy_crypto.is_content_file_path(
            "Timelines/ABC/template.tmp"))
        self.assertFalse(jy_crypto.is_content_file_path(
            "common_attachment/x.json"))
        self.assertFalse(jy_crypto.is_content_file_path("Timelines/project.json"))

    def test_install_dir(self):
        found = jy_crypto.find_install_dir()
        if found is None:
            self.assertFalse(jy_crypto.available())
            return
        self.assertTrue(os.path.isfile(os.path.join(found, "videoeditor.dll")))

    def test_no_dll_error_is_actionable(self):
        """找不到 DLL 时错误信息要给出可执行建议，而不是一句失败。"""
        if jy_crypto.available():
            self.skipTest("本机有 videoeditor.dll")
        with self.assertRaises(jy_crypto.CryptoError) as ctx:
            jy_crypto.encrypt_bytes(b'{"id":"x","new_version":"1"}')
        self.assertIn("videoeditor.dll", str(ctx.exception))
        self.assertIn("--no-encrypt", str(ctx.exception))

    @unittest.skipUnless(jy_crypto.available(), "本机没有剪映 videoeditor.dll")
    def test_encrypt_decrypt_roundtrip(self):   # pragma: no cover - 需真实 DLL
        tmp = _mkdtemp("crypto-")
        try:
            timeline, media = make_timeline(tmp)
            content = build_content(timeline, media=media)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        plain = json.dumps(content, ensure_ascii=False).encode("utf-8")
        encrypted = jy_crypto.encrypt_bytes(plain)
        self.assertFalse(jy_crypto.is_plain_json(encrypted))
        self.assertEqual(jy_crypto.decrypt_bytes(encrypted), plain)


class TestBackend(unittest.TestCase):
    """JianyingBackend.build 的端到端行为。"""

    def setUp(self):
        self.tmp = _mkdtemp("be-")
        self.timeline, _ = make_timeline(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_register(self):
        from cutwright.backends.base import BACKENDS
        self.assertIn("jianying", BACKENDS)
        self.assertEqual(BACKENDS["jianying"].name, "jianying")

    def test_02_build_artifacts(self):
        """产物 = 草稿目录 + 明文 draft.json + report.json，内容可读。"""
        out = os.path.join(self.tmp, "out")
        backend = JianyingBackend(paths=None, options={
            "name": "后端测试", "drafts_root": self.tmp})
        result = backend.build(self.timeline, out)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(result.artifacts), 3)
        draft_dir, draft_json, report_json = result.artifacts
        self.assertTrue(os.path.isdir(draft_dir))
        self.assertTrue(os.path.isfile(draft_json))
        self.assertTrue(os.path.isfile(report_json))
        with open(draft_json, "r", encoding="utf-8") as fh:
            content = json.load(fh)
        self.assertEqual(content["canvas_config"],
                         {"width": 1080, "height": 1920})
        with open(report_json, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertTrue(report["content_ok"])
        self.assertEqual(report["name"], "后端测试")
        self.assertIn("stats", result.data)
        self.assertIn("duration_us", result.data["stats"])

    def test_03_available_without_drafts_root(self):
        backend = JianyingBackend(paths=None, options={})
        self.assertIsInstance(backend.available(), bool)
        if not backend.available():
            self.assertTrue(backend.why_unavailable())

    def test_04_no_install_by_default(self):
        """没写 install=True 就不许碰草稿库。"""
        drafts_root = os.path.join(self.tmp, "drafts")
        os.makedirs(drafts_root)
        out = os.path.join(self.tmp, "out2")
        backend = JianyingBackend(paths=None, options={
            "name": "不安装", "drafts_root": drafts_root})
        backend.build(self.timeline, out)
        self.assertEqual(os.listdir(drafts_root), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
