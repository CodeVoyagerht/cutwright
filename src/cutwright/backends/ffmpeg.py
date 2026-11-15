"""ffmpeg 直出后端：把 :class:`~cutwright.timeline.Timeline` 渲染成一条 MP4。

为什么是「一串小进程」而不是一条巨型 filter_complex：

1. **失败要能说清是第几个镜头**。一次调用里塞进 40 个镜头时，ffmpeg 只会告诉你
   「某个 filter 报了 Invalid argument」。切片是一镜一调用，出错当场就能把镜头
   序号、源文件、滤镜链一起抛出来。
2. **中间结果可复用**。切片落在 ``_build/`` 里，改字幕或改 BGM 时不必重切画面
   （``skip_segments``），调一次片尾只需要重跑最后一条命令。
3. **变速与 Ken-Burns 只在切片阶段生效**。``setpts`` / ``zoompan`` 放在拼接之后
   会把整条片子的时间轴搅乱。

代价是磁盘上多一轮中间文件（几十秒的片子在几百 MB 量级），所以默认保留
``_build/``：日志和切片都在里面，出问题能直接拿中间产物复现。

管线（每一步都能单独重跑）::

    切片 × N  →  concat  →  base.mp4
    环境声 × N →  concat  →  ambient.wav
    字幕/叠加层 + 配乐 + 收尾滤镜  →  {name}.mp4

质量参数刻意写死成「肉眼无损」档：切片用 ``crf 15``，成片用 ``crf 18 + preset slow``。
两次编码已经掉了足够多的码率，再把切片质量压低会在拼接处露出色块。
"""
from __future__ import annotations

import hashlib
import os
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Sequence, Tuple

from .. import media, text
from ..media import FFmpegError
from ..timeline import (Timeline, TimelineAudio, TimelineClip, TimelineOverlay,
                        TimelineText)
from .base import Backend, BuildResult, register

__all__ = ["FFmpegBackend", "SegmentError"]

# HDR → SDR 的 tonemap 链。顺序不能动：先线性化才能 tonemap，最后必须回到
# bt709/yuv420p，否则下游的 scale/crop 拿到的是浮点平面。
HDR_TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)

# 切片质量。15 比成片的 18 高，是为了让「二次编码」几乎不损失细节。
SEGMENT_CRF = 15

# 预览档：只求「能看、能对时间」，几分钟的片子几十秒出。
# crf 28 + veryfast 的观感是「够看清字幕和构图」，不会用于交付。
# 成片目标响度：短视频平台常见 -14 LUFS，峰值 -1 dBTP。
DEFAULT_LOUDNESS = media.DEFAULT_LOUDNESS
PREVIEW_SEGMENT_CRF = 30
PREVIEW_PRESET = "veryfast"

# 环境声的淡入淡出上限：0.03s 只够去掉拼接处的爆音，再长就会吃掉字头。
AMBIENT_FADE = 0.03

# 字幕淡入淡出的时长。剪映默认约 0.2s，这里取 0.15s：更短会显得硬切，
# 更长会让下一句已经出现时上一句还没退干净。
TEXT_FADE = 0.15

# 成片整体淡入/淡出的默认值（秒）。0.12/0.35 是刷短视频时最常见的手感。
DEFAULT_FADE_IN = 0.12
DEFAULT_FADE_OUT = 0.35


class SegmentError(FFmpegError):
    """某个镜头的 ffmpeg 调用失败。

    继承 :class:`~cutwright.media.FFmpegError`，所以既有的 ``except FFmpegError``
    照样能抓住；额外带上镜头序号，:meth:`to_result` 可以转成 ``BuildResult``
    （``data["shot_index"]``），让 CLI 把「第几个镜头坏了」写进 JSON 报告。

    为什么既抛异常又提供 BuildResult：静默跳过是绝对不行的（用户会拿到一条
    少了一段却「成功」的片子），但上层有时更想收集错误继续做别的活儿。
    """

    def __init__(self, cmd: Sequence[str], code: int, stderr: str, *,
                 shot_index: int, shot_name: str, total_shots: int,
                 log_path: str = ""):
        super().__init__(cmd, code, stderr)
        self.shot_index = shot_index
        self.shot_name = shot_name
        self.total_shots = total_shots
        self.log_path = log_path

    def __str__(self) -> str:            # noqa: D105 - 只是加一句人话前缀
        return (f"第 {self.shot_index}/{self.total_shots} 个镜头"
                f"（{self.shot_name}）渲染失败\n{super().__str__()}")

    def to_result(self) -> BuildResult:
        """把这次失败包成 ``ok=False`` 的 :class:`BuildResult`。"""
        return BuildResult(
            backend="ffmpeg", ok=False, error=str(self),
            data={"shot_index": self.shot_index, "shot_name": self.shot_name,
                  "total_shots": self.total_shots, "log_path": self.log_path},
        )


@register
class FFmpegBackend(Backend):
    """用系统 ffmpeg 把时间线渲成一条 MP4。"""

    name = "ffmpeg"
    description = "ffmpeg 直出：切片 → 拼接 → 环境声 → 字幕/叠加 → 配乐 → 收尾"

    # -- 能力 -------------------------------------------------------------- #
    def available(self) -> bool:
        return bool(self.paths.ffmpeg and self.paths.ffprobe)

    def why_unavailable(self) -> str:
        missing = self.paths.missing_for(self.name)
        if missing:
            return (f"缺少 {', '.join(missing)}。装一个 ffmpeg 并让它在 PATH 里，"
                    f"或用环境变量 CUTWRIGHT_FFMPEG / CUTWRIGHT_FFPROBE 指定路径。")
        return super().why_unavailable()

    # -- 构建 -------------------------------------------------------------- #
    def build(self, timeline: Timeline, out_dir: str) -> BuildResult:
        """渲染整条时间线，返回成片路径与统计。

        ``out_dir`` 会被创建；中间产物在 ``{out_dir}/_build/``，字幕图在
        ``{out_dir}/text/``，成片是 ``{out_dir}/{timeline.name}.mp4``。

        任何一个镜头切片失败都会抛 :class:`SegmentError`（``FFmpegError`` 的子类），
        不会静默跳过。
        """
        if not self.available():
            return BuildResult(backend=self.name, ok=False, error=self.why_unavailable())
        if not timeline.clips:
            return BuildResult(backend=self.name, ok=False,
                               error="时间线里没有任何镜头，无法渲染")

        started = time.time()
        self.ensure_out_dir(out_dir)
        build_dir = os.path.join(out_dir, "_build")
        text_dir = os.path.join(out_dir, "text")
        os.makedirs(build_dir, exist_ok=True)
        os.makedirs(text_dir, exist_ok=True)

        log_path = os.path.join(build_dir, "render.log")
        self._log_path = log_path
        self._log_handle = open(log_path, "w", encoding="utf-8")
        warnings: List[str] = []

        width = int(timeline.canvas.width)
        height = int(timeline.canvas.height)
        fps = float(timeline.fps or 30.0)
        total = self._total_duration(timeline)

        try:
            self._note(f"渲染 {timeline.name!r}: {len(timeline.clips)} 个镜头, "
                       f"{width}x{height} @ {fps:g}fps, 时长 {total:.3f}s")

            segments = self._render_segments(timeline, build_dir, width, height, fps)
            base = self._concat_segments(segments, build_dir)
            ambient = self._build_ambient(timeline, build_dir, warnings)
            texts = self._render_texts(timeline, text_dir, width, height)
            overlays = self._collect_overlays(timeline, warnings)

            film = os.path.abspath(os.path.join(out_dir, f"{timeline.name}.mp4"))
            self._compose(base, film, total, fps, texts, overlays, ambient,
                          timeline, warnings)
            # 收尾日志必须在关掉句柄之前写，否则就是对已关闭文件写入（ValueError）。
            # 放在 try 里而不是 try/finally 之后，是因为计时也要覆盖到收尾这一步。
            elapsed = time.time() - started
            self._note(f"完成: {film} ({elapsed:.1f}s)")
        finally:
            self._log_handle.close()

        if not self.options.get("keep_temp", True):
            self._cleanup(build_dir)

        return BuildResult(
            backend=self.name,
            artifacts=[film],
            data={
                "duration": round(total, 3),
                "shots": len(timeline.clips),
                "log_path": log_path,
                "render_seconds": round(elapsed, 2),
                "resolution": [width, height],
                "fps": fps,
                "texts": len(texts),
                "overlays": len(overlays),
                "ambient": bool(ambient),
                "audio": bool(ambient or self._music_items(timeline)),
                "preview": bool(self.options.get("preview", False)),
                "segments_reused": getattr(self, "_segments_reused", 0),
            },
            warnings=warnings,
        )

    # -- 切片 -------------------------------------------------------------- #
    def _render_segments(self, timeline: Timeline, build_dir: str,
                         width: int, height: int, fps: float) -> List[str]:
        """逐镜切片（可并行、可跨次复用），返回片子路径（顺序即时间线顺序）。

        两个提速开关，默认都开：

        - ``cache``（默认 ``True``）：切片文件名带**内容摘要**，改字幕/改 BGM/调镜头顺序
          都不会让已切好的画面重切。只要源文件与入点不变，摘要就不变。
        - ``jobs``（默认 ``min(4, CPU)``）：ffmpeg 是独立进程，多开几个能吃到多核；
          纯 Python 侧没有 GIL 问题，只有写日志需要加锁。
        """
        total = len(timeline.clips)
        use_cache = bool(self.options.get("cache", True))
        plans: List[Tuple[int, TimelineClip, str]] = []
        for position, clip in enumerate(timeline.clips, start=1):
            digest = _segment_digest(clip, width, height, fps, self.options)
            plans.append((position, clip, os.path.join(build_dir, f"seg_{digest}.mp4")))

        reused = [p for p in plans if use_cache and _has_bytes(p[2])]
        pending = [p for p in plans if p not in reused]
        for position, clip, out_path in reused:
            self._note(f"[{position}/{total}] 复用切片 {os.path.basename(out_path)}"
                       f"（{clip.name}）")
            self._progress(position, total, clip.name)
        if reused:
            self._note(f"复用 {len(reused)} 段，省掉 {len(reused)} 次编码")

        jobs = self._jobs(len(pending))
        if pending and jobs > 1:
            self._note(f"并行切片 {len(pending)} 段（{jobs} 路）")
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {
                    pool.submit(self._encode_one, position, clip, out_path,
                                total, width, height, fps): (position, clip)
                    for position, clip, out_path in pending
                }
                for future in as_completed(futures):
                    position, clip = futures[future]
                    future.result()          # 失败在这里抛出，带镜头序号
                    self._progress(position, total, clip.name)
        else:
            for position, clip, out_path in pending:
                self._note(f"[{position}/{total}] 切片 {clip.name}")
                self._encode_one(position, clip, out_path, total, width, height, fps)
                self._progress(position, total, clip.name)

        out_paths = [p[2] for p in plans]
        self._segments_reused = len(reused)
        self._note(f"切片完成: {len(out_paths)} 段")
        return out_paths

    def _encode_one(self, position: int, clip: TimelineClip, out_path: str,
                    total: int, width: int, height: int, fps: float) -> None:
        """切一段，失败时把错误升级成带镜头序号的那个版本。"""
        try:
            self._encode_segment(clip, out_path, position, total, width, height, fps)
        except SegmentError:
            raise
        except FFmpegError as exc:
            raise SegmentError(exc.cmd, exc.code, exc.stderr,
                               shot_index=position, shot_name=clip.name,
                               total_shots=total, log_path=self._log_path) from exc

    def _jobs(self, pending: int) -> int:
        """并行路数：选项优先，否则按 CPU 与待切数量取小的那个。"""
        raw = self.options.get("jobs")
        if raw is None or raw == "":
            cpu = os.cpu_count() or 1
            return max(1, min(4, cpu, max(1, pending)))
        try:
            return max(1, min(32, int(raw), max(1, pending)))
        except (TypeError, ValueError):
            return 1

    def _encode_segment(self, clip: TimelineClip, out_path: str, position: int,
                        total: int, width: int, height: int, fps: float) -> None:
        """把一个镜头切成标准化的 h264 片段（固定尺寸/帧率/像素格式）。"""
        info = None if clip.still else media.probe(clip.source, self.paths)
        chain = self._segment_filter(clip, info, width, height, fps)
        preview = bool(self.options.get("preview", False))
        crf = str(PREVIEW_SEGMENT_CRF if preview else SEGMENT_CRF)
        preset = PREVIEW_PRESET if preview else "medium"
        if clip.still:
            # 静帧：一张图铺成 src_dur 长的视频。没有入点，也没法 -ss 到图片里。
            source_args = ["-loop", "1", "-t", _f(clip.src_dur), "-i", clip.source]
        else:
            source_args = ["-ss", _f(clip.src_in), "-t", _f(clip.src_dur),
                           "-i", clip.source]
        cmd = [
            self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            *source_args,
            "-filter:v", chain,
            "-an",
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-r", _f(fps), "-g", str(max(1, int(round(fps * 2)))),
            out_path,
        ]
        try:
            self._run(cmd)
        except FFmpegError as exc:
            # 附带镜头序号往上抛：不带序号的话，40 个镜头里坏一个只能靠猜。
            raise SegmentError(exc.cmd, exc.code, exc.stderr,
                               shot_index=position, shot_name=clip.name,
                               total_shots=total, log_path=self._log_path) from exc

    def _segment_filter(self, clip: TimelineClip, info: Optional[media.MediaInfo],
                        width: int, height: int, fps: float) -> str:
        """拼一个镜头的滤镜链。顺序按「色彩 → 时间 → 构图」排，不能随意调。

        ``zoompan`` 必须在 2 倍尺寸上做：在原尺寸上推近会立刻糊掉，因为每帧的
        采样源只有实际像素那么多。
        """
        parts: List[str] = []
        if info is not None and info.is_hdr:
            parts.append(HDR_TONEMAP)
        if info is not None and info.is_full_range:
            # 手机/相机常见的 full range 素材直接进 tv range 会过曝。
            parts.append("scale=in_range=full:out_range=tv")
        if not clip.still and abs(float(clip.speed) - 1.0) > 1e-6:
            parts.append(f"setpts=PTS/{_f(clip.speed)}")

        cx = _f(clip.cx)
        if clip.zoom in ("in", "out"):
            big_w, big_h = width * 2, height * 2
            parts.append(f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase")
            parts.append(f"crop={big_w}:{big_h}:x=(iw-{big_w})*{cx}")
            amt = abs(float(clip.zoom_amount))
            frames = max(1, int(round(clip.tl_dur * fps)))
            if clip.zoom == "in":
                expr = f"min(1+{_f(amt)}*on/{frames},1+{_f(amt)})"
            else:
                expr = f"max(1.0,1+{_f(amt)}-{_f(amt)}*on/{frames})"
            parts.append(
                f"zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:s={width}x{height}:fps={_f(fps)}"
            )
        else:
            parts.append(f"scale={width}:{height}:force_original_aspect_ratio=increase")
            parts.append(f"crop={width}:{height}:x=(iw-{width})*{cx}")
        parts.append(f"fps={_f(fps)}")
        parts.append("setsar=1")
        return ",".join(parts)

    def _concat_segments(self, segments: Sequence[str], build_dir: str) -> str:
        """用 concat demuxer 拼片段。``-c copy`` 不重编码，所以很快。"""
        base = os.path.join(build_dir, "base.mp4")
        listing = os.path.join(build_dir, "segments.txt")
        self._write_concat_list(listing, segments)
        cmd = [
            self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", listing,
            "-c", "copy", "-movflags", "+faststart", base,
        ]
        self._run(cmd)
        return base

    # -- 环境声 ------------------------------------------------------------ #
    def _build_ambient(self, timeline: Timeline, build_dir: str,
                       warnings: List[str]) -> Optional[str]:
        """从原素材抽回每个镜头的现场声，拼成一条与画面等长的环境声。

        全部镜头都没有音轨时返回 ``None``（整条片子静音），这是正常情况：
        GIF、屏幕录制、部分 A7S3 视频都可能没有音轨，为此报错没有意义。
        """
        infos: List[Optional[media.MediaInfo]] = []
        for clip in timeline.clips:
            # 静帧没有音轨，也不该去 ffprobe 一张 png 的时长。
            infos.append(None if clip.still else media.probe(clip.source, self.paths))
        if not any(info is not None and info.has_audio for info in infos):
            self._note("所有素材都没有音轨，跳过环境声")
            return None

        chunks: List[str] = []
        for clip, info in zip(timeline.clips, infos):
            raw = os.path.join(build_dir, f"amb_{clip.index:03d}.wav")
            if info is not None and info.has_audio:
                self._extract_ambient(clip, raw, warnings)
            else:
                # 没音轨的镜头（含静帧）补等长短静音，否则后面每一段都会右移，
                # 环境声就和画面对不上了。
                self._silence(clip.tl_dur, raw)
            chunks.append(self._level_ambient(raw, clip, build_dir))

        ambient = os.path.join(build_dir, "ambient.wav")
        self._concat_audio(chunks, ambient)
        return ambient

    def _extract_ambient(self, clip: TimelineClip, out_wav: str,
                         warnings: List[str]) -> None:
        """抽一个镜头的现场声。``highpass=f=50`` 去掉风声/空调声的低频轰鸣。

        变速的镜头必须让声音跟着变：画面用了 ``setpts``，声音若原样抽出来再截到
        ``tl_dur``，听到的是「前 N 秒原声」而不是「同一段被加快」，声音和嘴型
        （或动作声）就对不上了。``atempo`` 保音高不变，正好配 ``setpts``。
        """
        chain = ["highpass=f=50"]
        speed = float(clip.speed)
        if abs(speed - 1.0) > 1e-6:
            chain.append(_atempo_chain(speed))
        cmd = [
            self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", _f(clip.src_in), "-t", _f(clip.src_dur), "-i", clip.source,
            "-vn", "-ac", "2", "-ar", "48000", "-af", ",".join(chain),
            out_wav,
        ]
        try:
            self._run(cmd)
        except FFmpegError as exc:
            # 有音轨但抽不出来（坏数据流 / 编码不支持）不该毁掉整条片子。
            warnings.append(f"镜头 {clip.index} 环境声抽取失败，已用静音代替: {exc.code}")
            self._silence(clip.tl_dur, out_wav)

    def _silence(self, duration: float, out_wav: str) -> None:
        """生成一段等长静音（48kHz 立体声），用来占位。"""
        cmd = [
            self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", _f(max(0.01, duration)), "-c:a", "pcm_s16le", out_wav,
        ]
        self._run(cmd)

    def _level_ambient(self, raw_wav: str, clip: TimelineClip, build_dir: str) -> str:
        """把一段环境声压到统一响度（约 -20 dBFS）并加淡入淡出。

        统一到 -20 而不是 -14：环境声是**底**，不是主体，人声/BGM 才是。
        增益限制在 [-10, +24] dB：再压就听不见了，再抬就把底噪放大成噪音。
        """
        mean = media.mean_volume_db(raw_wav, self.paths)
        if mean is None:
            gain = 0.0
        else:
            gain = max(-10.0, min(24.0, -20.0 - mean))

        duration = max(0.05, float(clip.tl_dur))
        fade = min(AMBIENT_FADE, duration / 4.0)
        chain = (
            f"volume={_f(gain)}dB,"
            f"afade=t=in:st=0:d={_f(fade)},"
            f"afade=t=out:st={_f(duration - fade)}:d={_f(fade)},"
            f"alimiter=limit=0.95"
        )
        out_wav = os.path.join(build_dir, f"amb_g_{clip.index:03d}.wav")
        cmd = [
            self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", raw_wav, "-af", chain, "-t", _f(duration),
            "-ac", "2", "-ar", "48000", out_wav,
        ]
        try:
            self._run(cmd)
        except FFmpegError:
            # 量不出音量时的窄带极端情况：退回原始音频，至少别丢声。
            return raw_wav
        return out_wav

    def _concat_audio(self, chunks: Sequence[str], out_wav: str) -> None:
        """把逐段音频拼成一条。

        用 ``concat`` 滤镜而不是 concat demuxer：wav 的头里有 RIFF 长度字段，
        demuxer + copy 拼出来的时长经常对不上，而这里所有块都是 48k/立体声/s16，
        滤镜拼接没有格式协商问题。
        """
        cmd: List[str] = [self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
        for chunk in chunks:
            cmd.extend(["-i", chunk])
        streams = "".join(f"[{i}:a]" for i in range(len(chunks)))
        graph = f"{streams}concat=n={len(chunks)}:v=0:a=1[out]"
        cmd.extend(["-filter_complex", graph, "-map", "[out]",
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", out_wav])
        self._run(cmd)

    # -- 字幕 / 叠加层 ------------------------------------------------------ #
    def _render_texts(self, timeline: Timeline, text_dir: str,
                      width: int, height: int) -> List[Tuple[TimelineText, str]]:
        """把可见字幕逐条渲成 canvas 同尺寸的透明 PNG。

        文件名带上文案与样式的摘要：只带 index 会让「改了文案但没换 index」的
        重渲复用旧图，那是比多几张 PNG 严重得多的 bug。
        """
        out: List[Tuple[TimelineText, str]] = []
        for item in timeline.texts:
            if not item.visible:
                continue
            digest = _style_digest(item)
            png = os.path.join(text_dir, f"text_{item.index:03d}_{digest}.png")
            if not (os.path.exists(png) and os.path.getsize(png) > 0):
                text.render_text_png(item.text, item.style, (width, height), png)
                self._note(f"字幕 #{item.index} → {os.path.basename(png)}")
            out.append((item, png))
        return out

    def _collect_overlays(self, timeline: Timeline,
                          warnings: List[str]) -> List[Tuple[TimelineOverlay, str]]:
        """收集叠加层图片。缺文件的记 warning 并跳过——一个 logo 丢了不该毁掉成片。"""
        out: List[Tuple[TimelineOverlay, str]] = []
        for item in timeline.overlays:
            if os.path.isfile(item.image):
                out.append((item, item.image))
            else:
                warnings.append(f"叠加层 #{item.index} 图片不存在，已跳过: {item.image}")
                self._note(f"叠加层 #{item.index} 缺文件，跳过: {item.image}")
        return out

    # -- 合成 -------------------------------------------------------------- #
    def _compose(self, base: str, film: str, total: float, fps: float,
                 texts: Sequence[Tuple[TimelineText, str]],
                 overlays: Sequence[Tuple[TimelineOverlay, str]],
                 ambient: Optional[str], timeline: Timeline,
                 warnings: List[str]) -> None:
        """叠字幕/叠加层、混音、整体淡入淡出，编出成片。

        每个图片输入都用 ``-loop 1 -t {时长}`` 变成一段有时长的视频流，再靠
        ``setpts`` 挪到它在时间线上的位置。这样 ``fade`` 的 ``st`` 可以直接写
        绝对时间，不必给每条字幕单独造一个 shift 变量。
        """
        cmd: List[str] = [self.paths.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                          "-i", base]
        index = 1
        layers: List[str] = []          # 每一层已经算好的 [label]
        graph: List[str] = []

        for item, png in texts:
            start = max(0.0, float(item.tl_start))
            dur = max(0.04, float(item.tl_dur))
            cmd.extend(["-loop", "1", "-framerate", _f(fps), "-t", _f(dur), "-i", png])
            label = f"t{index}"
            graph.append(self._layer_graph(index, label, start, dur, TEXT_FADE, TEXT_FADE))
            layers.append(f"[{label}]")
            index += 1

        for item, png in overlays:
            start = max(0.0, float(item.tl_start))
            dur = max(0.04, float(item.tl_dur))
            fade_in = max(0.0, float(item.fade))
            fade_out = fade_in if item.fade_out else 0.0
            cmd.extend(["-loop", "1", "-framerate", _f(fps), "-t", _f(dur), "-i", png])
            label = f"o{index}"
            graph.append(self._layer_graph(index, label, start, dur, fade_in, fade_out))
            layers.append(f"[{label}]")
            index += 1

        # 视频主链：底片 → 调色 → 逐层叠加 → 整体淡入淡出 → yuv420p
        current = "0:v"
        grade = str(self.options.get("grade") or "").strip()
        if grade:
            graph.append(f"[{current}]{grade}[g0]")
            current = "g0"
        for number, layer in enumerate(layers, start=1):
            out_label = f"v{number}"
            options = self._overlay_options(texts, overlays, number)
            graph.append(f"[{current}]{layer}{options}[{out_label}]")
            current = out_label

        fade_in = float(self.options.get("fade_in", DEFAULT_FADE_IN))
        fade_out = float(self.options.get("fade_out", DEFAULT_FADE_OUT))
        tail: List[str] = []
        if fade_in > 0:
            tail.append(f"fade=t=in:st=0:d={_f(fade_in)}")
        if fade_out > 0:
            start = max(0.0, total - fade_out)
            tail.append(f"fade=t=out:st={_f(start)}:d={_f(fade_out)}")
        tail.append("format=yuv420p")
        graph.append(f"[{current}]{','.join(tail)}[vout]")

        audio_graph: List[str] = []
        music_pads: List[str] = []
        ambient_src: Optional[str] = None

        # 环境声是「主音轨」：amix 的 duration=first 以它为总长度，画面有多长
        # 音轨就有多长，不会被某段 BGM 截短。
        if ambient:
            cmd.extend(["-i", ambient])
            ambient_src = f"{index}:a"
            index += 1

        for item in self._music_items(timeline):
            start = max(0.0, float(item.tl_start))
            dur = max(0.04, float(item.tl_dur))
            src_in = max(0.0, float(item.src_in))
            if not os.path.isfile(item.source):
                warnings.append(f"配乐 #{item.index} 文件不存在，已跳过: {item.source}")
                continue
            cmd.extend(["-ss", _f(src_in), "-t", _f(dur), "-i", item.source])
            pad = f"m{index}"
            need_loop = self._music_needs_loop(item, src_in, dur, warnings)
            audio_graph.append(self._music_graph(index, pad, item, start, dur,
                                                 loop=need_loop))
            music_pads.append(f"[{pad}]")
            index += 1

        # 配乐链要先声明：下面的混音图会引用这些 [mN] 标签。
        graph.extend(audio_graph)

        mix_out = "[aout]"
        mixed = bool(ambient_src) or bool(music_pads)
        if ambient_src and music_pads and self._duck_enabled():
            # 人声盖住配乐：环境声当侧链，BGM 在被压的那几秒自己让路。
            # 固定 0.30 音量做不到这件事——它只能让配乐整体偏小。
            graph.append(f"[{ambient_src}]asplit=2[amb0][amb1]")
            if len(music_pads) == 1:
                graph.append(f"{music_pads[0]}anull[mus0]")
            else:
                graph.append("".join(music_pads)
                             + f"amix=inputs={len(music_pads)}:duration=first"
                               ":normalize=0[mus0]")
            graph.append(
                f"[mus0][amb1]sidechaincompress="
                f"threshold={_f(self._duck_threshold())}"
                f":ratio={_f(self._duck_ratio())}"
                ":attack=20:release=350[mus1]")
            graph.append("[amb0][mus1]amix=inputs=2:duration=first:normalize=0[aout]")
        elif mixed:
            pads = ([f"[{ambient_src}]"] if ambient_src else []) + music_pads
            if len(pads) == 1:
                graph.append(f"{pads[0]}anull[aout]")
            else:
                graph.append("".join(pads)
                             + f"amix=inputs={len(pads)}:duration=first:normalize=0"
                             + "[aout]")

        if mixed:
            target = self._loudness_target()
            if target is not None:
                # 每个镜头只对齐到 -20 dBFS，混完整体还是偏轻；成片这一层必须
                # 有响度归一化，否则手机上听着比参考片小一截。
                graph.append(f"[aout]loudnorm=I={_f(target)}:TP=-1.0:LRA=11[afin]")
                mix_out = "[afin]"

        cmd.extend(["-filter_complex", ";".join(graph), "-map", "[vout]"])
        if mixed:
            cmd.extend(["-map", mix_out, "-c:a", "aac", "-b:a", "192k",
                        "-ar", "48000", "-ac", "2"])
        else:
            cmd.append("-an")

        crf = int(self.options.get("crf", 26 if self.options.get("preview") else 18))
        preset = str(self.options.get("preset")
                     or ("veryfast" if self.options.get("preview") else "slow"))
        cmd.extend([
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-t", _f(total), film,
        ])
        self._run(cmd)

    def _overlay_options(self, texts: Sequence[Tuple[TimelineText, str]],
                         overlays: Sequence[Tuple[TimelineOverlay, str]],
                         number: int) -> str:
        """算出某层的 overlay 参数（位置、enable 窗口）。

        ``texts``/``overlays`` 与 ``layers`` 顺序一致：前 N 个是字幕，后面是叠加层。
        """
        text_count = len(texts)
        if number <= text_count:
            item = texts[number - 1][0]
            start, end = float(item.tl_start), float(item.tl_start) + float(item.tl_dur)
            return f"overlay=0:0:enable='between(t,{_f(start)},{_f(end)})'"

        item = overlays[number - text_count - 1][0]
        start = float(item.tl_start)
        end = start + float(item.tl_dur)
        x = int(self.options.get("overlay_x", 0) or 0)
        offset = self._offset_expression(item, start)
        if offset:
            return (f"overlay=x={x}:y='{offset}'"
                    f":enable='between(t,{_f(start)},{_f(end)})'")
        return f"overlay=x={x}:y=0:enable='between(t,{_f(start)},{_f(end)})'"

    def _offset_expression(self, item: TimelineOverlay, start: float) -> str:
        """叠加层的 y 偏移表达式：滑动入场 + 正弦浮动，都写成 t 的函数。

        用表达式而不是给每层造一格 ``setpts`` 动画：overlay 的 y 每帧求值，
        一层一个串就够了，滤镜图也短。
        """
        terms: List[str] = []
        slide = float(item.slide_px or 0.0)
        if abs(slide) > 1e-6:
            slide_dur = max(1e-3, float(item.slide_dur or 0.0))
            terms.append(
                f"{_f(slide)}*max(0,1-min(1,max(0,(t-{_f(start)})/{_f(slide_dur)})))"
            )
        amp = float(item.float_amp or 0.0)
        if abs(amp) > 1e-6:
            period = max(1e-3, float(item.float_period or 4.0))
            terms.append(
                f"{_f(amp)}*sin(2*PI*(t-{_f(start)})/{_f(period)})"
            )
        if not terms:
            return ""
        return "+".join(terms) if len(terms) > 1 else terms[0]

    def _layer_graph(self, input_index: int, label: str, start: float, dur: float,
                     fade_in: float, fade_out: float) -> str:
        """把一张静止 PNG 变成本层的一段 rgba 视频流（按时序淡入淡出）。"""
        parts = ["format=rgba", f"setpts=PTS+{_f(start)}/TB"]
        if fade_in > 0:
            parts.append(f"fade=t=in:st={_f(start)}:d={_f(min(fade_in, dur))}:alpha=1")
        if fade_out > 0:
            fade_start = max(0.0, start + dur - min(fade_out, dur))
            parts.append(f"fade=t=out:st={_f(fade_start)}:d={_f(min(fade_out, dur))}:alpha=1")
        return f"[{input_index}:v]{','.join(parts)}[{label}]"

    def _duck_enabled(self) -> bool:
        """配乐要不要给人声让路。默认开；``--opt duck=false`` 关掉。"""
        return bool(self.options.get("duck", True))

    def _duck_threshold(self) -> float:
        raw = self.options.get("duck_threshold", 0.05)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.05
        return value if 0.001 <= value <= 1.0 else 0.05

    def _duck_ratio(self) -> float:
        raw = self.options.get("duck_ratio", 8.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 8.0
        return value if 1.0 <= value <= 20.0 else 8.0

    def _loudness_target(self) -> Optional[float]:
        """成片目标响度（LUFS）。``loudness=false`` / ``0`` 关掉这一层。"""
        raw = self.options.get("loudness", DEFAULT_LOUDNESS)
        if raw is None or raw is False or raw == 0:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_LOUDNESS
        return value if -40.0 < value < 0.0 else DEFAULT_LOUDNESS

    def _music_needs_loop(self, item: TimelineAudio, src_in: float, dur: float,
                          warnings: List[str]) -> bool:
        """配乐比该段短的时候要不要循环。

        不循环的后果不是「音乐短一点」，而是**后半段彻底静音**——`amix` 以环境声
        长度为准，配乐早没了就是没了，用户听起来像忘了配音。默认循环；
        ``music_loop=false`` 时只警告不循环，留给知道自己在干什么的人。
        """
        if not bool(self.options.get("music_loop", True)):
            return False
        try:
            available = float(media.probe(item.source, self.paths).duration) - src_in
        except Exception:            # 探不出来就别自作主张，按原样铺
            return False
        if available <= 0 or available >= dur - 0.05:
            return False
        warnings.append(
            "配乐只有 %.1fs，要铺 %.1fs：已循环播放（接缝处可能有跳音），"
            "换更长的曲子更稳妥" % (max(0.0, available), dur))
        return True

    def _music_graph(self, input_index: int, pad: str, item: TimelineAudio,
                     start: float, dur: float, *, loop: bool = False) -> str:
        """配乐：按 gain_db 调音量、裁到该段时间、延迟到 tl_start、末尾淡出。"""
        fade = min(1.0, dur / 3.0)
        delay_ms = max(0, int(round(start * 1000)))
        chain: List[str] = []
        if loop:
            # aloop 的 size 是样本数，给个足够大的值等于「无限循环」；
            # 真正的长度由后面的 atrim 决定。
            chain.append("aloop=loop=-1:size=2147483647")
        chain += [
            f"volume={_f(float(item.gain_db))}dB",
            f"atrim=0:{_f(dur)}",
            "asetpts=PTS-STARTPTS",
            f"adelay={delay_ms}|{delay_ms}",
        ]
        if fade > 0:
            chain.append(f"afade=t=out:st={_f(max(0.0, dur - fade))}:d={_f(fade)}")
        # sidechaincompress 要求主路与侧链采样率/声道一致，这里统一成成片规格。
        chain.append("aresample=48000")
        chain.append("aformat=sample_fmts=fltp:channel_layouts=stereo")
        return f"[{input_index}:a]{','.join(chain)}[{pad}]"

    # -- 小工具 ------------------------------------------------------------ #
    def _music_items(self, timeline: Timeline) -> List[TimelineAudio]:
        return [item for item in timeline.audios if item.kind == "music"]

    def _total_duration(self, timeline: Timeline) -> float:
        if timeline.duration and timeline.duration > 0:
            return float(timeline.duration)
        return sum(float(clip.tl_dur) for clip in timeline.clips)

    def _progress(self, index: int, total: int, name: str) -> None:
        """调用 options 里的 ``on_progress(idx, total, name)``。

        回调炸了不能影响渲染：进度条是给人看的，不是渲染的一部分。
        """
        callback = self.options.get("on_progress")
        if callable(callback):
            try:
                callback(index, total, name)
            except Exception as exc:      # noqa: BLE001 - 故意吞掉
                self._note(f"on_progress 回调异常（已忽略）: {type(exc).__name__}: {exc}")

    def _write_concat_list(self, listing: str, media_paths: Sequence[str]) -> None:
        """写 concat demuxer 的清单文件。

        路径里的单引号要按 ffmpeg 的规则写成 ``'\\''``；反斜杠统一换成正斜杠，
        免得 Windows 路径里的 ``\\`` 被当成转义。
        """
        with open(listing, "w", encoding="utf-8") as handle:
            handle.write("ffconcat version 1.0\n")
            for path in media_paths:
                clean = os.path.abspath(path).replace("\\", "/").replace("'", r"'\''")
                handle.write(f"file '{clean}'\n")

    def _run(self, cmd: Sequence[str]) -> str:
        """执行一条 ffmpeg 命令，成功返回 stdout，失败由 :func:`media.run` 抛错。

        每条命令都写进 render.log：出问题时用户把日志一贴，就能看出是哪一步、
        哪条滤镜链坏了。
        """
        self._note("$ " + _join_cmd(cmd))
        return media.run(list(cmd))

    def _note(self, message: str) -> None:
        handle = getattr(self, "_log_handle", None)
        if handle is not None:
            # 切片是并行的，日志句柄必须串行写：两个线程同时 write+flush
            # 会把行交错成半句，出问题时的日志就成了新的问题。
            lock = getattr(self, "_log_lock", None)
            if lock is None:
                lock = self._log_lock = threading.Lock()
            with lock:
                handle.write(message + "\n")
                handle.flush()

    def _cleanup(self, build_dir: str) -> None:
        import shutil

        shutil.rmtree(build_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 格式化小工具
# --------------------------------------------------------------------------- #
def _f(value: float, digits: int = 4) -> str:
    """把数字写成 ffmpeg 喜欢的短形式：``0.5`` 而不是 ``0.500000``。

    定点数越短，日志越好读；顺便避免浮点尾巴（``1.20000000001``）被当成不同
    的时间点。整数会退化成 ``1``，ffmpeg 接受。
    """
    text_value = f"{float(value):.{digits}f}"
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return text_value or "0"


def _atempo_chain(speed: float) -> str:
    """把任意变速比拆成 ffmpeg ``atempo`` 能接受的一串（单级只吃 0.5–2.0）。

    2.0 以上的倍速（或 0.25 的长慢镜）单级 ``atempo`` 会直接报错，
    所以按 2 / 0.5 逐级拆开再串起来。``planner`` 把 speed 限在 0.25–4.0，
    但手写分镜表可以写任意值，这里不假设输入合法。
    """
    ratio = float(speed)
    if ratio <= 0:
        return "atempo=1.0"
    parts: List[str] = []
    while ratio > 2.0:
        parts.append(f"atempo={_f(2.0)}")
        ratio /= 2.0
    while ratio < 0.5:
        parts.append(f"atempo={_f(0.5)}")
        ratio /= 0.5
    parts.append(f"atempo={_f(ratio)}")
    return ",".join(parts)


def _has_bytes(path: str) -> bool:
    """文件存在且非空——编码到一半被杀掉会留下 0 字节的片子，别信它。"""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _segment_digest(clip: TimelineClip, width: int, height: int, fps: float,
                    options: dict) -> str:
    """切片的内容摘要，用来做跨次复用的文件名。

    参与摘要的一切都必须**影响像素或时长**：源文件（含 mtime/size，同名换了内容
    也要重切）、入点、时长、变速、构图、画布、帧率、预览档。字幕、BGM、镜头的
    先后顺序**不进摘要**——它们不改画面，改了也不该让 40 个镜头重切。
    """
    stat_key = ""
    try:
        st = os.stat(clip.source)
        stat_key = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        stat_key = "missing"
    payload = "\n".join([
        f"v2", os.path.abspath(clip.source), stat_key,
        "still" if getattr(clip, "still", False) else "motion",
        _f(clip.src_in), _f(clip.src_dur), _f(getattr(clip, "speed", 1.0)),
        str(getattr(clip, "zoom", "none")),
        _f(float(getattr(clip, "zoom_amount", 0.0))),
        _f(float(getattr(clip, "cx", 0.5))),
        str(width), str(height), _f(fps),
        "1" if bool(options.get("preview", False)) else "0",
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def _join_cmd(cmd: Sequence[str]) -> str:
    """命令行的可读形式（仅用于日志，不用于执行）。"""
    try:
        return shlex.join([str(part) for part in cmd])
    except (TypeError, ValueError):       # 非字符串的怪参数
        return " ".join(str(part) for part in cmd)


def _style_digest(item: TimelineText) -> str:
    """字幕内容的短摘要，用来给缓存文件名去重。

    只取影响像素的几个字段（文案、字号、颜色、版式），这样「时间改了但画面没
    变」的字幕还能命中缓存。
    """
    style = item.style
    payload = "|".join([
        # 排版版本号必须进摘要：只摘要「文案 + 样式」的话，升级渲染器之后
        # 同样的文件名会命中旧 PNG，成片看起来「代码改了却没变化」。
        f"v{text.RENDER_VERSION}",
        item.text or "",
        _f(float(getattr(style, "font_height_px", 0.0)), 2),
        str(getattr(style, "color", "")),
        str(getattr(style, "keyword_color", "")),
        str(getattr(style, "stroke_color", "")),
        _f(float(getattr(style, "stroke_width_px", 0.0)), 2),
        str(getattr(style, "align", "")),
        str(getattr(style, "max_chars_per_line", "")),
        _f(float(getattr(style, "rotation_deg", 0.0)), 2),
        str(getattr(style, "bottom_margin_px", "")),
        str(getattr(style, "center_ratio_y", "")),
        str(getattr(style, "font", "")),
        "1" if getattr(style, "invisible", False) else "0",
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
