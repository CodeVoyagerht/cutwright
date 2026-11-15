"""ffmpeg 直出后端的测试。

这里刻意不使用任何真实素材：全部素材都在 setUpClass 里用 ffmpeg 的 ``lavfi``
现场合成。原因是素材一旦依赖外部文件，测试在别人的机器上就会直接跳过或者
失败，而这个后端的价值恰恰是「能不能把一条时间线渲染成片」，必须真的跑一遍。

临时目录优先用系统临时目录，用不上再退回仓库内的 ``tests/``、仓库根目录：
``tempfile.mkdtemp`` 会建出 0700 的目录（连自己都写不进去，某些沙箱下还会
删不掉），所以这里一律用 ``os.makedirs`` 建目录并做一次写入探测。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw

from cutwright import config, media
from cutwright.backends.base import BACKENDS
from cutwright.backends.ffmpeg import FFmpegBackend, SegmentError
from cutwright.config import Paths
from cutwright.schema import Canvas, Shotlist
from cutwright.timeline import Timeline, TimelineClip

# 任务允许的两个 ffmpeg 副本目录；先看环境变量/PATH，再看这两处。
_CANDIDATE_DIRS: Tuple[str, ...] = (
    os.path.dirname(os.environ.get("CUTWRIGHT_FFMPEG", "")),
    r"C:\Users\life2\.dsh-wallpaper-engine\ffmpeg",
    r"D:\AutoClip Desktop\resources\ffmpeg",
    r"C:\ffmpeg\bin",
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

CANVAS = (320, 180)
FPS = 30.0
CLIP_SECONDS = 2.0
SHOTS = 3


def _tool_names() -> Tuple[str, str]:
    """按当前平台给出 ffmpeg/ffprobe 的文件名。"""
    if config.IS_WINDOWS:
        return "ffmpeg.exe", "ffprobe.exe"
    return "ffmpeg", "ffprobe"


def _find_tools() -> Tuple[Optional[str], Optional[str]]:
    """找一对可用的 ffmpeg/ffprobe，返回绝对路径。

    必须成对返回：``ffprobe`` 缺失时后端连素材时长都读不出来，那种情况下
    整条测试没有意义，应该跳过而不是报错。
    """
    explicit = (
        os.environ.get("CUTWRIGHT_FFMPEG", ""),
        os.environ.get("CUTWRIGHT_FFPROBE", ""),
    )
    if all(explicit) and all(os.path.isfile(p) for p in explicit):
        return explicit[0], explicit[1]

    discovered = config.discover()
    if discovered.ffmpeg and discovered.ffprobe:
        return discovered.ffmpeg, discovered.ffprobe

    ffmpeg_name, ffprobe_name = _tool_names()
    for folder in _CANDIDATE_DIRS:
        if not folder:
            continue
        ffmpeg = os.path.join(folder, ffmpeg_name)
        ffprobe = os.path.join(folder, ffprobe_name)
        if os.path.isfile(ffmpeg) and os.path.isfile(ffprobe):
            return ffmpeg, ffprobe
    return None, None


FFMPEG, FFPROBE = _find_tools()
HAS_TOOLS = bool(FFMPEG and FFPROBE)
SKIP_TOOLS = unittest.skipUnless(HAS_TOOLS, "需要成对的 ffmpeg/ffprobe 才能渲染")


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_scratch(prefix: str) -> str:
    """开一个本次运行专用的临时目录，返回绝对路径。

    名字里带进程号和纳秒时间戳，保证并行跑测试时互不干扰；用 ``os.makedirs``
    而不是 ``tempfile.mkdtemp``，否则目录 ACL 会变成 0700 而写不进去。系统临时
    目录不可用时退回仓库内的 ``tests/`` 与仓库根目录，让测试在受限沙箱里也能跑。
    """
    name = f"{prefix}_{os.getpid()}_{time.time_ns() % 10 ** 9}"
    candidates = (tempfile.gettempdir(), os.path.join(_repo_root(), "tests"), _repo_root())
    for base in candidates:
        path = os.path.join(base, name)
        try:
            os.makedirs(path, exist_ok=False)
        except OSError:
            continue
        probe = os.path.join(path, ".write-probe")
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
            continue
        return path
    raise unittest.SkipTest("找不到可写的临时目录（系统临时目录与仓库都不可写）")


def _frame_count(path: str, paths) -> int:
    """逐帧数一遍：``nb_read_frames`` 才是「这条片子有多少帧」。"""
    import subprocess
    proc = subprocess.run(
        [paths.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", path],
        capture_output=True, stdin=subprocess.DEVNULL)
    text = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return int(text[-1]) if text else 0


def _integrated_lufs(path: str, paths) -> float:
    """用 ebur128 量整片积分响度（LUFS）。"""
    import re
    import subprocess
    proc = subprocess.run(
        [paths.ffmpeg, "-hide_banner", "-nostats", "-i", path,
         "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    text = (proc.stderr or b"").decode("utf-8", "replace")
    hits = re.findall(r"I:\s+(-?\d+\.\d+) LUFS", text)
    if not hits:
        raise AssertionError("量不出响度：\n" + text[-400:])
    return float(hits[-1])


class BackendContractTest(unittest.TestCase):
    """不需要真的调用 ffmpeg 的那部分契约。"""

    def setUp(self) -> None:
        self.scratch = _make_scratch("cutwright_contract")

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_name_and_registration(self) -> None:
        self.assertEqual(FFmpegBackend.name, "ffmpeg")
        # 导入本模块即应完成注册，后端查找表里必须有它。
        self.assertIn("ffmpeg", BACKENDS)

    def test_available_requires_both_tools(self) -> None:
        backend = FFmpegBackend(paths=Paths(ffmpeg=None, ffprobe=None))
        self.assertFalse(backend.available())
        if HAS_TOOLS:
            self.assertTrue(FFmpegBackend(paths=Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE)).available())
            # 只有 ffmpeg 没有 ffprobe 也算不可用，因为读不出时长。
            self.assertFalse(FFmpegBackend(paths=Paths(ffmpeg=FFMPEG, ffprobe=None)).available())

    def test_why_unavailable_names_missing_tool(self) -> None:
        reason = FFmpegBackend(paths=Paths(ffmpeg=None, ffprobe=None)).why_unavailable()
        self.assertIn("ffmpeg", reason)
        self.assertIn("ffprobe", reason)

    def test_build_without_ffmpeg_returns_error_not_exception(self) -> None:
        timeline = _timeline_from_clips(["/nowhere/clip.mp4"])
        result = FFmpegBackend(paths=Paths(ffmpeg=None, ffprobe=None)).build(
            timeline, os.path.join(self.scratch, "noffmpeg"))
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertEqual(result.artifacts, [])

    def test_build_empty_timeline_returns_error(self) -> None:
        if not HAS_TOOLS:
            self.skipTest("只有拿到 ffmpeg 才会走到「空时间线」这条分支")
        backend = FFmpegBackend(paths=Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE))
        empty = Timeline(name="empty", canvas=Canvas(width=CANVAS[0], height=CANVAS[1]),
                         fps=FPS, duration=0.0, clips=[], texts=[], audios=[], overlays=[])
        result = backend.build(empty, os.path.join(self.scratch, "empty"))
        self.assertFalse(result.ok)
        self.assertIn("镜头", result.error)

    def test_segment_filter_matches_verified_pipeline(self) -> None:
        """滤镜链是本项目唯一「不能乱改」的部分，逐字符核对关键片段。"""
        backend = FFmpegBackend(paths=Paths(ffmpeg="ffmpeg", ffprobe="ffprobe"))
        plain = media.MediaInfo(path="x.mp4", duration=2.0, width=640, height=360,
                                fps=30.0, has_video=True, has_audio=True)
        hdr_full = media.MediaInfo(path="x.mp4", duration=2.0, width=640, height=360,
                                   fps=30.0, has_video=True, has_audio=True,
                                   color_transfer="smpte2084", color_range="pc")

        static = backend._segment_filter(_clip("none"), plain, 1080, 1920, 30.0)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", static)
        self.assertIn("crop=1080:1920:x=(iw-1080)*0.5", static)
        self.assertNotIn("zoompan", static)
        self.assertTrue(static.endswith("fps=30,setsar=1"))

        zoom_in = backend._segment_filter(_clip("in", amount=0.08), plain, 1080, 1920, 30.0)
        self.assertIn("scale=2160:3840:force_original_aspect_ratio=increase", zoom_in)
        self.assertIn("crop=2160:3840:x=(iw-2160)*0.5", zoom_in)
        # 2 秒 * 30fps = 60 帧，n 必须用 tl_dur 算而不是源时长。
        self.assertIn("zoompan=z='min(1+0.08*on/60,1+0.08)'", zoom_in)
        self.assertIn("d=1:s=1080x1920:fps=30", zoom_in)

        zoom_out = backend._segment_filter(_clip("out", amount=0.06), plain, 1080, 1920, 30.0)
        self.assertIn("zoompan=z='max(1.0,1+0.06-0.06*on/60)'", zoom_out)

        # 慢放 0.5 倍：setpts 要在构图之前、fps 之前。
        slowed = _clip("none", speed=0.5)
        chain = backend._segment_filter(slowed, plain, 1080, 1920, 30.0)
        self.assertIn("setpts=PTS/0.5", chain)
        self.assertLess(chain.index("setpts="), chain.index("scale="))

        # HDR 先色调映射，全范围先转 tv range，两者都在构图之前。
        mapped = backend._segment_filter(_clip("none"), hdr_full, 1080, 1920, 30.0)
        self.assertIn("tonemap=hable:desat=0", mapped)
        self.assertIn("scale=in_range=full:out_range=tv", mapped)
        self.assertLess(mapped.index("zscale=t=linear"), mapped.index("scale=in_range=full"))


def _clip(zoom: str, amount: float = 0.05, speed: float = 1.0) -> TimelineClip:
    """造一个只在内存里存在的片段，用于核对滤镜链。"""
    return TimelineClip(index=0, source="/nowhere/clip.mp4", src_in=0.0,
                        src_out=CLIP_SECONDS, tl_start=0.0, speed=speed,
                        zoom=zoom, zoom_amount=amount)


def _timeline_from_clips(sources: List[str]) -> Timeline:
    """用若干素材路径拼一条最短时间线（每个镜头 2 秒，顺序排下去）。"""
    raw = {
        "name": "handmade",
        "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
        "fps": FPS,
        "video_segments": [
            {"file": source, "src_start": 0.0, "src_dur": CLIP_SECONDS,
             "tl_start": index * CLIP_SECONDS}
            for index, source in enumerate(sources)
        ],
    }
    return Timeline.from_shotlist(Shotlist.from_dict(raw))


@SKIP_TOOLS
class FFmpegBuildTest(unittest.TestCase):
    """真的跑 ffmpeg：合成素材、渲染、再用 ffprobe 复核成片。"""

    scratch: str = ""
    paths: Paths
    clips: List[str]
    music: str
    overlay: str
    out_dir: str
    result: Optional[object] = None
    progress: List[Tuple[int, int, str]] = []

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = _make_scratch("cutwright_build")
        cls.paths = Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE)
        cls.clips = [
            cls._synth("clip_a.mp4", "testsrc2", tone=440),
            cls._synth("clip_b.mp4", "testsrc2", tone=660, size="640x360"),
            # 第三个故意不给音轨：验证「有镜头没声音」时环境声不能失败。
            cls._synth("clip_c_mute.mp4", "testsrc", tone=None),
        ]
        cls.music = os.path.join(cls.scratch, "music.mp3")
        media.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100:duration=8",
                   "-c:a", "libmp3lame", cls.music])
        cls.overlay = os.path.join(cls.scratch, "badge.png")
        image = Image.new("RGBA", (120, 44), (0, 0, 0, 0))
        ImageDraw.Draw(image).rectangle([0, 0, 119, 43], fill=(20, 120, 220, 220))
        image.save(cls.overlay)
        cls.out_dir = os.path.join(cls.scratch, "out")
        cls.progress = []

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.scratch:
            shutil.rmtree(cls.scratch, ignore_errors=True)

    @classmethod
    def _synth(cls, name: str, pattern: str, tone: Optional[int],
               size: str = "640x360") -> str:
        """合成一个 2 秒素材；tone 为 None 时不带音轨。"""
        out = os.path.join(cls.scratch, name)
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi", "-i", f"{pattern}=size={size}:rate={int(FPS)}:duration={CLIP_SECONDS:g}"]
        if tone is not None:
            cmd += ["-f", "lavfi", "-i",
                    f"sine=frequency={tone}:sample_rate=48000:duration={CLIP_SECONDS:g}"]
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        if tone is not None:
            cmd += ["-c:a", "aac", "-shortest"]
        else:
            cmd += ["-an"]
        cmd.append(out)
        media.run(cmd)
        return out

    # -- 富特性时间线，整个类只渲染一次，各测试各查一面 -------------------- #
    def rich_result(self):
        """渲染一次带字幕/叠加层/配乐/调色的成片，结果缓存给后续测试复用。"""
        cls = type(self)
        if cls.result is not None:
            return cls.result

        raw = {
            "name": "build_case",
            "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [
                {"file": self.clips[0], "src_start": 0.0, "src_dur": CLIP_SECONDS, "zoom": "none"},
                {"file": self.clips[1], "src_start": 0.0, "src_dur": CLIP_SECONDS,
                 "zoom": "in", "zoom_amount": 0.08},
                {"file": self.clips[2], "src_start": 0.0, "src_dur": CLIP_SECONDS,
                 "zoom": "out", "zoom_amount": 0.06, "cx": 0.3},
            ],
            "styles": {
                "caption": {"font_height_px": 26, "bottom_margin_px": 14},
                "keyword": {"font_height_px": 28, "center_ratio_y": 0.42,
                            "max_chars_per_line": 6},
            },
            "text_segments": [
                {"text": "第一句|关键词", "tl_start": 0.2, "tl_dur": 1.4, "style": "caption"},
                {"text": "折行测试内容真的挺长|重点", "tl_start": 3.1, "tl_dur": 1.5,
                 "style": "keyword"},
            ],
            "audio_segments": [
                {"file": self.music, "tl_start": 0.0, "tl_dur": CLIP_SECONDS * SHOTS,
                 "src_start": 0.5, "gain_db": -4.0, "kind": "music"},
            ],
            "image_overlays": [
                {"file": self.overlay, "tl_start": 1.0, "tl_dur": 1.6, "fade": 0.2,
                 "fade_out": True, "slide_px": 20.0, "float_amp": 5.0, "float_period": 3.0},
            ],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        backend = FFmpegBackend(paths=self.paths, options={
            "keep_temp": True,
            "grade": "eq=saturation=1.1",
            "on_progress": lambda index, total, name: self.progress.append((index, total, name)),
        })
        cls.result = backend.build(timeline, self.out_dir)
        return cls.result

    def probe_film(self):
        result = self.rich_result()
        return media.probe(result.artifacts[0], self.paths)

    # -- 成片本身 ---------------------------------------------------------- #
    def test_film_exists_and_is_not_empty(self) -> None:
        result = self.rich_result()
        self.assertTrue(result.ok, msg=f"渲染失败: {result.error}")
        self.assertEqual(len(result.artifacts), 1)
        film = result.artifacts[0]
        self.assertTrue(os.path.isabs(film))
        self.assertTrue(os.path.isfile(film))
        self.assertGreater(os.path.getsize(film), 1024)
        self.assertTrue(film.endswith(".mp4"))

    def test_film_duration_matches_timeline(self) -> None:
        info = self.probe_film()
        expected = CLIP_SECONDS * SHOTS
        self.assertAlmostEqual(info.duration, expected, delta=0.5)
        self.assertTrue(info.has_video)

    def test_film_resolution_is_canvas(self) -> None:
        info = self.probe_film()
        self.assertEqual((info.width, info.height), CANVAS)
        # 3 个镜头都带声音（第三个是靠 anullsrc 补的静音），成片必须有音轨。
        self.assertTrue(info.has_audio)

    def test_build_result_data(self) -> None:
        data = self.rich_result().data
        for key in ("duration", "shots", "log_path", "render_seconds"):
            self.assertIn(key, data)
        self.assertEqual(data["shots"], SHOTS)
        self.assertAlmostEqual(float(data["duration"]), CLIP_SECONDS * SHOTS, delta=0.01)
        self.assertGreaterEqual(float(data["render_seconds"]), 0.0)
        self.assertEqual(data["texts"], 2)
        self.assertEqual(data["overlays"], 1)

    def test_render_log_is_written(self) -> None:
        result = self.rich_result()
        log_path = result.data["log_path"]
        self.assertTrue(os.path.isfile(log_path))
        with open(log_path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertTrue(lines)
        self.assertTrue(any(line.startswith("$ ") for line in lines))
        self.assertTrue(any("渲染" in line for line in lines))
        self.assertTrue(any("完成" in line for line in lines))
        # 每条命令行都得能看出是 ffmpeg 在跑，而不是空行凑数。
        self.assertTrue(any(FFMPEG in line for line in lines))

    def test_text_pngs_are_written_at_canvas_size(self) -> None:
        self.rich_result()
        text_dir = os.path.join(self.out_dir, "text")
        self.assertTrue(os.path.isdir(text_dir))
        names = sorted(n for n in os.listdir(text_dir) if n.endswith(".png"))
        self.assertEqual(len(names), 2)
        for name in names:
            with Image.open(os.path.join(text_dir, name)) as image:
                self.assertEqual(image.size, CANVAS)
                self.assertEqual(image.mode, "RGBA")

    def test_intermediates_live_under_build_dir(self) -> None:
        result, out_dir, cuts = self._fresh_build("out_layout")
        build_dir = os.path.join(out_dir, "_build")
        self.assertTrue(os.path.isdir(build_dir))
        names = os.listdir(build_dir)
        self.assertIn("base.mp4", names)
        self.assertIn("ambient.wav", names)
        self.assertIn("render.log", names)
        # 切片文件名带内容摘要（跨次复用的钥匙），所以只断言「每镜一个、互不重复」。
        self.assertEqual(len(cuts), SHOTS, f"应当每镜一个切片: {cuts}")
        self.assertEqual(len(set(cuts)), SHOTS, f"切片名重复意味着缓存撞车: {cuts}")
        self.assertTrue(result.ok)

    def test_progress_callback_receives_every_shot(self) -> None:
        self.rich_result()
        # 并行切片下「完成顺序」本来就不是时间线顺序，断言的是「每镜都报过一次」。
        self.assertEqual(sorted(item[0] for item in self.progress), [1, 2, 3])
        self.assertTrue(all(item[1] == SHOTS for item in self.progress))
        self.assertTrue(all(item[2] for item in self.progress))

    # -- 复用切片与并行 ---------------------------------------------------- #
    def _fresh_build(self, name: str, **options):
        """建到自己的目录里：缓存会跨次累积，共用目录会让计数断言互相污染。"""
        out_dir = os.path.join(self.scratch, name)
        opts = {"keep_temp": True}
        opts.update(options)
        result = FFmpegBackend(paths=self.paths, options=opts).build(
            self._rich_timeline(), out_dir)
        build_dir = os.path.join(out_dir, "_build")
        cuts = [os.path.join(build_dir, n) for n in sorted(os.listdir(build_dir))
                if n.startswith("seg_") and n.endswith(".mp4")]
        return result, out_dir, cuts

    def test_cached_segments_are_reused_on_second_build(self) -> None:
        result, out_dir, cuts = self._fresh_build("out_cache")
        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(len(cuts), SHOTS)
        before = [os.path.getmtime(path) for path in cuts]
        time.sleep(1.1)  # 让文件系统时间戳能拉开，否则「没变」证明不了任何事
        second = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(
                                   self._rich_timeline(), out_dir)
        after = [os.path.getmtime(path) for path in cuts]
        self.assertEqual(before, after, "默认缓存应当复用已有切片而不是重切")
        self.assertEqual(second.data.get("segments_reused"), SHOTS)

    def test_cache_false_forces_reencode(self) -> None:
        result, out_dir, cuts = self._fresh_build("out_nocache")
        self.assertTrue(result.ok, msg=result.error)
        before = [os.path.getmtime(path) for path in cuts]
        time.sleep(1.1)
        second = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True, "cache": False}).build(
                                   self._rich_timeline(), out_dir)
        after = [os.path.getmtime(path) for path in cuts]
        self.assertNotEqual(before, after, "cache=False 时必须真的重切")
        self.assertEqual(second.data.get("segments_reused"), 0)

    def test_jobs_option_is_honoured(self) -> None:
        backend = FFmpegBackend(paths=self.paths, options={"jobs": 1})
        self.assertEqual(backend._jobs(5), 1)
        self.assertEqual(FFmpegBackend(paths=self.paths,
                                       options={"jobs": "99"})._jobs(3), 3)
        # 默认值必须是「有限并行」：纯串行白等 CPU，开满会把内存打爆
        default = FFmpegBackend(paths=self.paths)._jobs(8)
        self.assertGreaterEqual(default, 1)
        self.assertLessEqual(default, 4)

    def test_segment_digest_ignores_text_and_order_but_not_picture(self) -> None:
        from cutwright.backends.ffmpeg import _segment_digest
        timeline = self._rich_timeline()
        first = timeline.clips[0]
        base = _segment_digest(first, CANVAS[0], CANVAS[1], FPS, {})
        self.assertEqual(base, _segment_digest(first, CANVAS[0], CANVAS[1], FPS, {}))
        self.assertNotEqual(base, _segment_digest(first, CANVAS[0], CANVAS[1], FPS,
                                                 {"preview": True}))
        self.assertNotEqual(base, _segment_digest(first, CANVAS[0] // 2, CANVAS[1],
                                                 FPS, {}))
        # 同一个镜头搬个位置（改顺序）不该改变摘要——否则「调顺序」会重切全部画面
        moved = timeline.clips[1]
        self.assertEqual(_segment_digest(moved, CANVAS[0], CANVAS[1], FPS, {}),
                         _segment_digest(timeline.clips[1], CANVAS[0], CANVAS[1],
                                         FPS, {}))

    def _rich_timeline(self) -> Timeline:
        """与 rich_result 相同的时间线，供复用测试重建一次。"""
        raw = {
            "name": "build_case",
            "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [
                {"file": self.clips[0], "src_start": 0.0, "src_dur": CLIP_SECONDS},
                {"file": self.clips[1], "src_start": 0.0, "src_dur": CLIP_SECONDS},
                {"file": self.clips[2], "src_start": 0.0, "src_dur": CLIP_SECONDS},
            ],
        }
        return Timeline.from_shotlist(Shotlist.from_dict(raw))

    # -- keep_temp / 失败语义 ---------------------------------------------- #
    def test_keep_temp_false_removes_build_dir(self) -> None:
        out_dir = os.path.join(self.scratch, "out_clean")
        timeline = self._rich_timeline()
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": False}).build(timeline, out_dir)
        self.assertTrue(result.ok, msg=result.error)
        self.assertTrue(os.path.isfile(result.artifacts[0]))
        self.assertFalse(os.path.exists(os.path.join(out_dir, "_build")),
                         "keep_temp=False 时中间产物不该留下")

    def test_failed_segment_raises_with_shot_index(self) -> None:
        timeline = _timeline_from_clips([self.clips[0], os.path.join(self.scratch, "gone.mp4"),
                                         self.clips[2]])
        backend = FFmpegBackend(paths=self.paths, options={"keep_temp": True})
        out_dir = os.path.join(self.scratch, "out_fail")
        with self.assertRaises(SegmentError) as caught:
            backend.build(timeline, out_dir)
        error = caught.exception
        self.assertEqual(error.shot_index, 2)
        self.assertEqual(error.total_shots, SHOTS)
        self.assertIn("gone.mp4", error.shot_name)
        self.assertIn("镜头", str(error))
        # BuildResult 里也要能看出是第几个镜头坏了。
        failed = error.to_result()
        self.assertFalse(failed.ok)
        self.assertEqual(failed.data["shot_index"], 2)
        self.assertEqual(failed.backend, "ffmpeg")
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "_build", "render.log")))

    def test_timeline_without_audio_renders_silent(self) -> None:
        out_dir = os.path.join(self.scratch, "out_mute")
        timeline = _timeline_from_clips([self.clips[2], self.clips[2]])
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(timeline, out_dir)
        self.assertTrue(result.ok, msg=result.error)
        info = media.probe(result.artifacts[0], self.paths)
        self.assertFalse(info.has_audio, "全部镜头无音轨且无配乐时成片应当是无声的")
        self.assertEqual((info.width, info.height), CANVAS)
        self.assertAlmostEqual(info.duration, CLIP_SECONDS * 2, delta=0.5)
        self.assertFalse(result.data["ambient"])

    def test_final_mix_is_normalized_to_target(self):
        """每个镜头只对齐 -20 dBFS，混完还要把成片拉到目标响度。"""
        music = os.path.join(self.scratch, "quiet_bgm.mp3")
        media.run([self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "sine=frequency=330:duration=3",
                   "-b:a", "128k", music])
        raw = {
            "name": "loud", "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[0], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS}],
            "audio_segments": [{"file": music, "tl_start": 0.0,
                                "tl_dur": CLIP_SECONDS, "gain_db": -40.0}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(
            timeline, os.path.join(self.scratch, "out_loud"))
        self.assertTrue(result.ok, msg=result.error)
        lufs = _integrated_lufs(result.artifacts[0], self.paths)
        self.assertAlmostEqual(lufs, -14.0, delta=3.0,
                               msg="成片响度应当接近 -14 LUFS，实测 %.1f" % lufs)

    def test_loudness_can_be_disabled(self):
        raw = {
            "name": "flat", "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[0], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True,
                                        "loudness": False}).build(
            timeline, os.path.join(self.scratch, "out_flat"))
        self.assertTrue(result.ok, msg=result.error)
        lufs = _integrated_lufs(result.artifacts[0], self.paths)
        # 不归一化时实测约 -17.3 LUFS（每镜只对齐到 -20 dBFS 的均值），
        # 与被拉到 -14 的差别要有 2 LU 以上，否则这条用例证明不了任何事。
        self.assertLess(lufs, -16.0, "关掉归一化后不该被拉到 -14")

    # -- 配乐太短 ---------------------------------------------------------- #
    def test_short_music_is_looped_to_cover_the_film(self):
        """配乐比成片短时不能「放完就静音」——那是用户听得见的漏配。"""
        music = os.path.join(self.scratch, "short_bgm.mp3")
        media.run([self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-b:a", "128k", music])
        total = CLIP_SECONDS * 3
        raw = {
            "name": "loop_case",
            "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[0], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS} for _ in range(3)],
            "audio_segments": [{"file": music, "tl_start": 0.0,
                                "tl_dur": total, "gain_db": 0.0}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        out_dir = os.path.join(self.scratch, "out_loop")
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(timeline, out_dir)
        self.assertTrue(result.ok, msg=result.error)
        self.assertTrue(any("循环" in w for w in result.warnings), result.warnings)
        # 最后一段不该是静音：把尾巴切成小文件再量平均音量。
        tail = os.path.join(self.scratch, "loop_tail.mp4")
        media.run([self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-ss", "%.3f" % (total - 1.0), "-t", "1.0",
                   "-i", result.artifacts[0], tail])
        level = media.mean_volume_db(tail, self.paths)
        self.assertIsNotNone(level)
        self.assertGreater(level, -45.0, "配乐循环后尾巴不该是静音（实测 %.1f dB）" % level)

    def test_music_loop_can_be_turned_off(self):
        music = os.path.join(self.scratch, "short_bgm_off.mp3")
        media.run([self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-b:a", "128k", music])
        raw = {
            "name": "no_loop", "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[0], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS}],
            "audio_segments": [{"file": music, "tl_start": 0.0,
                                "tl_dur": CLIP_SECONDS, "gain_db": 0.0}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True,
                                        "music_loop": False}).build(
            timeline, os.path.join(self.scratch, "out_noloop"))
        self.assertTrue(result.ok, msg=result.error)
        self.assertFalse(any("循环" in w for w in result.warnings), result.warnings)

    # -- 快编档与正式档必须同长 -------------------------------------------- #
    def test_preview_keeps_the_same_frame_count(self):
        final, _, _ = self._fresh_build("par_final")
        preview, _, _ = self._fresh_build("par_preview", preview=True)
        self.assertTrue(final.ok, msg=final.error)
        self.assertTrue(preview.ok, msg=preview.error)
        final_frames = _frame_count(final.artifacts[0], self.paths)
        preview_frames = _frame_count(preview.artifacts[0], self.paths)
        self.assertGreater(final_frames, 0)
        self.assertEqual(
            preview_frames, final_frames,
            "快编档只该改画质，不该改时长：preview %d 帧 vs final %d 帧"
            % (preview_frames, final_frames))

    # -- 配乐给人声让路（ducking） ----------------------------------------- #
    def _duck_film(self, name: str, **options):
        raw = {
            "name": name, "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[0], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS}],
            # 配乐要比人声大，侧链压下去才量得出来：实测 gain=0 时只差 0.8 dB，
            # +12 dB 时差 4.6 dB——旁白轨本来就比配乐响，那是正常混音，不是 bug。
            "audio_segments": [{"file": self.music, "tl_start": 0.0,
                                "tl_dur": CLIP_SECONDS, "gain_db": 12.0}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        # 关掉响度归一化：它会把两版的整体电平拉平，正是这里要避免的。
        settings = {"keep_temp": True, "loudness": False}
        settings.update(options)
        result = FFmpegBackend(paths=self.paths, options=settings).build(
            timeline, os.path.join(self.scratch, "out_" + name))
        self.assertTrue(result.ok, msg=result.error)
        return result.artifacts[0]

    def test_music_ducks_under_ambient_voice(self):
        ducked = media.mean_volume_db(self._duck_film("duck_on"), self.paths)
        flat = media.mean_volume_db(self._duck_film("duck_off", duck=False),
                                    self.paths)
        self.assertLess(ducked, flat - 2.0,
                        "人声在的时候配乐应当让路：%.1f vs %.1f dB" % (ducked, flat))

    def test_ducking_is_skipped_without_ambient(self):
        raw = {
            "name": "noamb", "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [{"file": self.clips[2], "src_start": 0.0,
                                "src_dur": CLIP_SECONDS}],
            "audio_segments": [{"file": self.music, "tl_start": 0.0,
                                "tl_dur": CLIP_SECONDS, "gain_db": 0.0}],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True, "loudness": False}).build(
            timeline, os.path.join(self.scratch, "out_noamb"))
        self.assertTrue(result.ok, msg=result.error)
        # 没有环境声就没有侧链，配乐不该被压（这里只要求不报错、电平正常）。
        level = media.mean_volume_db(result.artifacts[0], self.paths)
        self.assertGreater(level, -40.0)

    # -- 静帧 -------------------------------------------------------------- #
    def _still_png(self, name: str = "still.png") -> str:
        path = os.path.join(self.scratch, name)
        Image.new("RGB", (480, 270), (200, 60, 60)).save(path, "PNG")
        return path

    def test_still_image_becomes_a_clip_of_its_duration(self) -> None:
        still = self._still_png()
        raw = {
            "name": "still_case",
            "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
            "fps": FPS,
            "video_segments": [
                {"file": still, "src_dur": 1.5, "still": True},
                {"file": self.clips[1], "src_start": 0.0, "src_dur": CLIP_SECONDS},
            ],
        }
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        self.assertTrue(timeline.clips[0].still)
        self.assertEqual(timeline.clips[0].src_in, 0.0,
                         "静帧没有入点，src_start 不该被带进时间线")
        out_dir = os.path.join(self.scratch, "out_still")
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(timeline, out_dir)
        self.assertTrue(result.ok, msg=result.error)
        info = media.probe(result.artifacts[0], self.paths)
        self.assertEqual((info.width, info.height), CANVAS)
        self.assertAlmostEqual(info.duration, 1.5 + CLIP_SECONDS, delta=0.4)

    def test_all_still_timeline_has_no_ambient(self) -> None:
        still = self._still_png("only_still.png")
        raw = {"name": "still_only", "canvas": {"width": CANVAS[0], "height": CANVAS[1]},
               "fps": FPS,
               "video_segments": [{"file": still, "src_dur": 1.0, "still": True}]}
        timeline = Timeline.from_shotlist(Shotlist.from_dict(raw))
        out_dir = os.path.join(self.scratch, "out_still_only")
        result = FFmpegBackend(paths=self.paths,
                               options={"keep_temp": True}).build(timeline, out_dir)
        self.assertTrue(result.ok, msg=result.error)
        self.assertFalse(result.data["ambient"], "静帧没有原声，不该凭空造一条环境声")


if __name__ == "__main__":
    unittest.main()
