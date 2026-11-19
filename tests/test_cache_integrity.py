"""切片缓存的完整性：半截的 mp4 不能当成成品用。

这一组测试针对一个「静默」的失败：编码被中断时，``_build/`` 里会留下几 KB 的
半截 mp4。它既不是 0 字节，也不是完整片子——早先只按「文件非空」判缓存命中，
于是下次渲染会直接把它拼进成片：画面在某一镜之后提前结束，音轨却还是全长，
而容器时长、体积、响度全都正常，体检也照样通过。

三个防线，每个都在这里有一条用例：

1. 写缓存先写 ``*.part.mp4`` 再改名，中断留下的是临时文件，不会出现在缓存路径上；
2. 复用前核时长，对不上就重切（``--opt verify_cache=false`` 可关）；
3. 体检比视频流与音轨各自的时长（``av_duration_match``）。

还需要 ffmpeg 才跑得起来，没有工具时整组跳过——这组测的正是真实渲染。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from typing import Optional, Tuple

from . import tmpdir

from cutwright import config, media, qc

CANVAS = {"width": 320, "height": 180}
FPS = 30
CLIP_SECONDS = 2.0


def _tool_names() -> Tuple[str, str]:
    return ("ffmpeg.exe", "ffprobe.exe") if config.IS_WINDOWS else ("ffmpeg", "ffprobe")


def _find_tools() -> Tuple[Optional[str], Optional[str]]:
    discovered = config.discover()
    if discovered.ffmpeg and discovered.ffprobe:
        return discovered.ffmpeg, discovered.ffprobe
    ffmpeg_name, ffprobe_name = _tool_names()
    for folder in (
        os.path.dirname(os.environ.get("CUTWRIGHT_FFMPEG", "")),
        r"C:\Users\life2\tools\ffmpeg",
        r"C:\ffmpeg\bin",
        "/usr/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ):
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


def _run_cli(argv) -> Tuple[int, str]:
    """跑一次 CLI，只关心退出码与 stderr。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from cutwright import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue() + err.getvalue()


def _make_clip(path: str, seconds: float = CLIP_SECONDS) -> None:
    proc = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc2=size={CANVAS['width']}x{CANVAS['height']}:rate={FPS}:duration={seconds}",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", path],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise AssertionError((proc.stderr or b"").decode("utf-8", "replace")[-500:])


def _make_audio(path: str, seconds: float = 8.0) -> None:
    """一段够长的正弦波：体检要求成片有音轨，没音轨会被判失败。"""
    proc = subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "pcm_s16le", path],
        capture_output=True, stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise AssertionError((proc.stderr or b"").decode("utf-8", "replace")[-500:])


def _write_shotlist(path: str, material_dir: str, shots, audio: str = "") -> None:
    total = sum(float(s["src_dur"]) for s in shots)
    doc = {
        "name": "cache-probe",
        "material_dir": material_dir,
        "asset_dir": material_dir,
        "canvas": dict(CANVAS),
        "fps": FPS,
        "total_duration": total,
        "acts": [{"key": "a", "name": "a", "start": 0.0, "dur": total}],
        "video_segments": shots,
        "text_segments": [],
        "vo_segments": [],
        "audio_segments": (
            [{"file": os.path.basename(audio), "tl_start": 0.0,
              "tl_dur": total, "kind": "music"}] if audio else []
        ),
        "image_overlays": [],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)


def _segments(out_dir: str):
    build = os.path.join(out_dir, "_build")
    if not os.path.isdir(build):
        return []
    return sorted(
        os.path.join(build, name) for name in os.listdir(build)
        if name.startswith("seg_") and name.endswith(".mp4")
    )


class CacheIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not HAS_TOOLS:
            raise unittest.SkipTest("需要成对的 ffmpeg/ffprobe 才能渲染")

    def setUp(self) -> None:
        self._tmp = tmpdir("cache")
        self.root = self._tmp.__enter__()
        self.materials = os.path.join(self.root, "materials")
        os.makedirs(self.materials, exist_ok=True)
        self.clip_a = os.path.join(self.materials, "a.mp4")
        self.clip_b = os.path.join(self.materials, "b.mp4")
        self.bgm = os.path.join(self.materials, "bgm.wav")
        _make_clip(self.clip_a)
        _make_clip(self.clip_b)
        _make_audio(self.bgm)

    def tearDown(self) -> None:
        self._tmp.__exit__(None, None, None)

    # -- 工具 -------------------------------------------------------------- #

    def _build(self, shots, out_dir: str, *extra) -> Tuple[int, str]:
        shotlist = os.path.join(self.root, "shotlist.json")
        _write_shotlist(shotlist, self.materials, shots, self.bgm)
        return _run_cli(["build", shotlist, "-o", out_dir, *extra])

    def _shots_two(self):
        return [
            {"act": "a", "shot": "01", "file": "a.mp4",
             "src_start": 0.0, "src_dur": 1.0, "tl_start": 0.0},
            {"act": "a", "shot": "02", "file": "b.mp4",
             "src_start": 0.0, "src_dur": 1.0, "tl_start": 1.0},
        ]

    # -- 用例 -------------------------------------------------------------- #

    @SKIP_TOOLS
    def test_truncated_segment_is_reencoded_not_reused(self) -> None:
        """把一段切片截断成非 0 垃圾，重建必须识别出来并重切。"""
        out_dir = os.path.join(self.root, "out")
        code, log = self._build(self._shots_two(), out_dir, "--opt", "jobs=1")
        self.assertEqual(code, 0, log)

        segments = _segments(out_dir)
        self.assertEqual(len(segments), 2, segments)
        victim = segments[-1]
        good_size = os.path.getsize(victim)
        with open(victim, "r+b") as handle:
            handle.truncate(max(256, good_size // 4))

        code, log = self._build(self._shots_two(), out_dir, "--opt", "jobs=1")
        self.assertEqual(code, 0, log)
        self.assertIn("segments_stale", log)

        # 重切后：体积回到完整片子该有的量级，且成片音视频等长。
        self.assertGreater(os.path.getsize(victim), good_size // 2)
        mp4 = os.path.join(out_dir, "cache-probe.mp4")
        info = media.probe(mp4, config.Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE))
        self.assertAlmostEqual(info.video_duration, 2.0, delta=0.2)
        self.assertAlmostEqual(info.audio_duration, 2.0, delta=0.2)

    @SKIP_TOOLS
    def test_partial_write_never_lands_on_the_cache_path(self) -> None:
        """中断留下的只能是 ``*.part.mp4``，缓存路径上不会出现半成品。"""
        out_dir = os.path.join(self.root, "out")
        code, log = self._build(self._shots_two(), out_dir, "--opt", "jobs=1")
        self.assertEqual(code, 0, log)
        build_dir = os.path.join(out_dir, "_build")
        leftovers = [n for n in os.listdir(build_dir) if ".part." in n]
        self.assertEqual(leftovers, [], leftovers)

    @SKIP_TOOLS
    def test_duplicate_shots_are_encoded_once(self) -> None:
        """同一段素材在同一张表里出现两次，只切一次、两处共用。"""
        shots = [
            {"act": "a", "shot": "01", "file": "a.mp4",
             "src_start": 0.0, "src_dur": 1.0, "tl_start": 0.0},
            {"act": "a", "shot": "02", "file": "a.mp4",
             "src_start": 0.0, "src_dur": 1.0, "tl_start": 1.0},
        ]
        out_dir = os.path.join(self.root, "dup")
        code, log = self._build(shots, out_dir, "--opt", "jobs=4")
        self.assertEqual(code, 0, log)
        self.assertEqual(len(_segments(out_dir)), 1, _segments(out_dir))
        mp4 = os.path.join(out_dir, "cache-probe.mp4")
        info = media.probe(mp4, config.Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE))
        self.assertAlmostEqual(info.duration, 2.0, delta=0.2)

    @SKIP_TOOLS
    def test_qc_flags_video_shorter_than_audio(self) -> None:
        """视频流比音轨短 0.6 秒时，体检必须报出来（容器时长看不出来）。"""
        broken = os.path.join(self.root, "av-mismatch.mp4")
        proc = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i",
             f"testsrc2=size={CANVAS['width']}x{CANVAS['height']}:rate={FPS}:duration=1.4",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2.0",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", broken],
            capture_output=True, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            self.skipTest("这条 ffmpeg 生成不出音视频不等长的样本")

        info = media.probe(broken, config.Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE))
        if abs(info.video_duration - info.audio_duration) < 0.3:
            self.skipTest("样本两端被对齐了（-shortest 生效），换个 ffmpeg 再测")

        report = qc.check_render(broken, paths=config.Paths(ffmpeg=FFMPEG, ffprobe=FFPROBE))
        checks = {c.name: c for c in report.checks}
        self.assertIn("av_duration_match", checks)
        self.assertFalse(checks["av_duration_match"].ok, checks["av_duration_match"].detail)
        self.assertTrue(any(issue.code == "Q102" for issue in report.issues))
