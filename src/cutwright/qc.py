"""成片质检：能不能渲、渲出来有没有问题。

分两层，对应「动手前」和「动手后」：

* :func:`check_timeline` —— 时间线健康度。空档、重叠、越界文字、可见的旁白层。
  纯内存计算，不碰 ffmpeg，可以每改一版就疯狂跑。
* :func:`check_render` —— 成片体检。时长、分辨率、有没有音轨、黑场、静音段。
  这一层必须跑 ffmpeg，所以只在真渲完之后跑一次。

为什么值得单独一个模块：剪完看一遍是人的本能，但 agent 不会「看」，它只会
读到 ``ok: true``。所以这里的判定必须**可复现、可解释**——每条结论带上实测
数字和判定阈值，出错时人能立刻知道是片子的问题还是阈值的问题。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config, media
from .schema import Issue
from .timeline import Timeline

__all__ = [
    "QcCheck",
    "QcReport",
    "check_timeline",
    "check_render",
    "run_all",
    "DEFAULT_BLACK_MIN_DUR",
    "DEFAULT_SILENCE_MIN_DUR",
]

DEFAULT_BLACK_MIN_DUR = 0.35      # 超过这个长度的纯黑段算问题（转场黑一闪是正常的）
DEFAULT_SILENCE_MIN_DUR = 1.0     # 超过这个长度的静音段算问题


@dataclass
class QcCheck:
    """一条可判定的结论。``detail`` 里必须带实测值，不能只写结论。"""

    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"        # error | warn | info

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "severity": self.severity}


@dataclass
class QcReport:
    """一次质检的完整结果。``ok`` = 没有任何 error 级失败。"""

    checks: List[QcCheck] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "",
            severity: str = "error") -> QcCheck:
        check = QcCheck(name, ok, detail, severity)
        self.checks.append(check)
        return check

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.severity == "error")

    @property
    def failed(self) -> List[QcCheck]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "issues": [i.to_dict() for i in self.issues],
            "failed": [c.name for c in self.failed],
        }


# --------------------------------------------------------------------------- #
# 时间线健康度
# --------------------------------------------------------------------------- #
def check_timeline(tl: Timeline, *, gap_tolerance: float = 0.02,
                   overlap_tolerance: float = 0.02) -> QcReport:
    """检查时间线本身（不碰 ffmpeg）。"""
    report = QcReport()

    if not tl.clips:
        report.add("clips_present", False, "时间线没有任何镜头")
        return report
    report.add("clips_present", True, f"{len(tl.clips)} 个镜头")

    # --- 首尾与总长 ---
    first_gap = tl.clips[0].tl_start
    report.add("starts_at_zero", abs(first_gap) <= gap_tolerance,
               f"首镜起点 {first_gap:.3f}s")
    if abs(first_gap) > gap_tolerance:
        report.issues.append(Issue("warn", "Q001",
                                   f"首镜不从 0s 开始，成片开头会有 {first_gap:.3f}s 黑场",
                                   "clips[0]"))

    # --- 空档 / 重叠 / 零长 ---
    gaps, overlaps, zeros = [], [], []
    for prev, cur in zip(tl.clips, tl.clips[1:]):
        delta = cur.tl_start - prev.tl_end
        if delta > gap_tolerance:
            gaps.append((cur.index, delta))
        elif delta < -overlap_tolerance:
            overlaps.append((cur.index, -delta))
    for clip in tl.clips:
        if clip.tl_dur <= 0:
            zeros.append(clip.index)

    report.add("no_gaps", not gaps,
               "无空档" if not gaps else
               "空档: " + ", ".join(f"#{i} {d:.3f}s" for i, d in gaps))
    report.add("no_overlaps", not overlaps,
               "无重叠" if not overlaps else
               "重叠: " + ", ".join(f"#{i} {d:.3f}s" for i, d in overlaps))
    report.add("no_zero_length", not zeros,
               "无零长镜头" if not zeros else f"零长镜头: {zeros}")
    for index, delta in gaps:
        report.issues.append(Issue("error", "Q010",
                                   f"镜头 #{index} 前有 {delta:.3f}s 空档，会变黑场",
                                   f"clips[{index}]"))
    for index, delta in overlaps:
        report.issues.append(Issue("error", "Q011",
                                   f"镜头 #{index} 与前镜重叠 {delta:.3f}s", f"clips[{index}]"))
    for index in zeros:
        report.issues.append(Issue("error", "Q012", "镜头时长为 0", f"clips[{index}]"))

    # --- 变速是否离谱 ---
    wild = [c.index for c in tl.clips if c.speed < 0.25 or c.speed > 4.0]
    report.add("speed_sane", not wild,
               "变速都在 0.25x-4x 内" if not wild else f"极端变速: {wild}",
               severity="warn")

    # --- 文字 ---
    out_of_range = []
    for text in tl.texts:
        if text.tl_end > tl.duration + 0.05:
            out_of_range.append((text.index, text.tl_end - tl.duration))
        if text.role == "vo" and text.visible:
            report.issues.append(Issue(
                "error", "Q020",
                "旁白层是可见的——它会以字幕形式糊在画面上，应该用 invisible 样式",
                f"texts[{text.index}]"))
    report.add("texts_in_range", not out_of_range,
               "文字都在成片范围内" if not out_of_range else
               "越界: " + ", ".join(f"#{i} 超出 {d:.2f}s" for i, d in out_of_range))
    vo_visible = any(t.role == "vo" and t.visible for t in tl.texts)
    report.add("vo_hidden", not vo_visible,
               "旁白层不可见" if not vo_visible else "存在可见的旁白层")

    # --- 叠加层 / 音频 ---
    bad_overlays = [o.index for o in tl.overlays
                    if o.tl_dur <= 0 or o.tl_start < 0]
    report.add("overlays_valid", not bad_overlays,
               "叠加层有效" if not bad_overlays else f"无效叠加层: {bad_overlays}")
    for audio in tl.audios:
        if not os.path.exists(audio.source):
            report.add("audio_exists", False, f"音频不存在: {audio.source}")
            report.issues.append(Issue("error", "Q030",
                                       f"音频不存在: {audio.source}",
                                       f"audios[{audio.index}]"))
            break
    else:
        if tl.audios:
            report.add("audio_exists", True, f"{len(tl.audios)} 条音频都在盘上")

    # --- 素材存在性（一次探测，别重复开文件） ---
    missing = [c.index for c in tl.clips if not os.path.exists(c.source)]
    report.add("sources_exist", not missing,
               "所有镜头素材都在" if not missing else f"缺失素材: {missing}")
    for index in missing:
        report.issues.append(Issue("error", "Q040", "素材文件不存在",
                                   f"clips[{index}]"))

    report.add("duration_positive", tl.duration > 0,
               f"总时长 {tl.duration:.3f}s")
    return report


# --------------------------------------------------------------------------- #
# 成片体检
# --------------------------------------------------------------------------- #
def check_render(mp4: str, *, expect_duration: Optional[float] = None,
                 expect_canvas: Optional[tuple] = None,
                 duration_tolerance: float = 0.25,
                 require_audio: bool = True,
                 black_min_dur: float = DEFAULT_BLACK_MIN_DUR,
                 silence_min_dur: float = DEFAULT_SILENCE_MIN_DUR,
                 paths: Optional[config.Paths] = None) -> QcReport:
    """对渲出来的 MP4 做实测体检。纯读，不修改文件。"""
    paths = paths or config.discover()
    report = QcReport()

    if not os.path.exists(mp4):
        report.add("file_exists", False, f"成片不存在: {mp4}")
        return report
    size = os.path.getsize(mp4)
    report.add("file_exists", True, f"{mp4} ({size / 1048576:.1f} MB)")
    report.add("file_nonempty", size > 1024, f"{size} 字节")

    info = media.probe(mp4, paths)
    if not info.has_video:
        report.add("has_video", False, "探测不到视频流——可能渲染中途失败")
        return report
    report.add("has_video", True,
               f"{info.width}x{info.height} @ {info.fps:.3f}fps, "
               f"{info.duration:.3f}s, {info.pix_fmt}")

    # --- 时长 ---
    if expect_duration is not None:
        drift = info.duration - expect_duration
        ok = abs(drift) <= duration_tolerance
        report.add("duration_match", ok,
                   f"实际 {info.duration:.3f}s vs 期望 {expect_duration:.3f}s "
                   f"（差 {drift:+.3f}s，容差 ±{duration_tolerance:.2f}s）")
        if not ok:
            report.issues.append(Issue("error", "Q100",
                                       f"时长偏差 {drift:+.3f}s 超容差"))

    # --- 分辨率 ---
    if expect_canvas:
        w, h = expect_canvas
        ok = (info.width, info.height) == (int(w), int(h))
        report.add("canvas_match", ok,
                   f"实际 {info.width}x{info.height} vs 期望 {int(w)}x{int(h)}")
        if not ok:
            report.issues.append(Issue("error", "Q101",
                                       f"分辨率不符：{info.width}x{info.height}"))

    # --- 音轨 ---
    if require_audio:
        report.add("has_audio", info.has_audio,
                   "有音轨" if info.has_audio else "没有音轨——检查 amix 输入是否为空")

    # --- 响度 / 峰值 ---
    # 后端会把成片拉到 target LUFS，但「做了」不等于「做到了」——这一层只读地量一遍。
    if info.has_audio:
        measured = _measure_loudness(mp4, paths)
        if measured is not None:
            lufs, peak = measured
            drift = lufs - media.DEFAULT_LOUDNESS
            ok = abs(drift) <= media.LOUDNESS_TOLERANCE
            report.add("loudness_match", ok,
                       "积分响度 %.1f LUFS（目标 %.1f，差 %+.1f，容差 ±%.0f）"
                       % (lufs, media.DEFAULT_LOUDNESS, drift,
                          media.LOUDNESS_TOLERANCE),
                       severity="info" if ok else "warn")
            if not ok:
                report.issues.append(Issue(
                    "warn", "Q103",
                    "响度 %.1f LUFS 偏离目标 %.1f%+.1f——检查 --opt loudness 是否被关掉"
                    % (lufs, media.DEFAULT_LOUDNESS, drift), mp4))
            if peak is not None:
                hot = peak > -1.0
                report.add("true_peak", not hot,
                           "真峰值 %.1f dBFS（上限 -1.0）" % peak,
                           severity="warn" if hot else "info")
                if hot:
                    report.issues.append(Issue(
                        "warn", "Q104",
                        "真峰值 %.1f dBFS 太顶，平台转码容易削波" % peak, mp4))

    # --- 黑场 / 静音 ---
    # 定级规则：整片黑 / 整片无声是「片子废了」，按 error 判；局部超阈值只是
    # 「这一处该看一眼」，按 warn 判。两者在报告里用不同的标记显示，不能混。
    black = _scan(mp4, f"blackdetect=d={black_min_dur}:pix_th=0.10", paths)
    segments = re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", black)
    total_black = sum(float(b) - float(a) for a, b in segments)
    whole_black = bool(segments) and info.duration > 0 and total_black >= info.duration * 0.9
    if not segments:
        report.add("no_long_black", True, "无长黑场", severity="info")
    else:
        detail = (f"{len(segments)} 段黑场，共 {total_black:.2f}s: " +
                  ", ".join(f"{float(a):.2f}-{float(b):.2f}s" for a, b in segments[:5]))
        if whole_black:
            detail = f"整片几乎全黑（{total_black:.2f}s / {info.duration:.2f}s）—— 检查拼接是否真的产出了画面"
            report.add("no_long_black", False, detail, severity="error")
        else:
            report.add("no_long_black", False, detail, severity="warn")

    silent = _scan(mp4, f"silencedetect=n=-45dB:d={silence_min_dur}", paths)
    sil = re.findall(r"silence_start:\s*(-?[\d.]+)", silent)
    sil_spans = [(float(a), float(b)) for a, b in
                 re.findall(r"silence_start:\s*(-?[\d.]+).*?silence_end:\s*([\d.]+)", silent, re.S)]
    total_silence = sum(b - a for a, b in sil_spans)
    whole_silent = (bool(sil) and info.duration > 0
                    and total_silence >= info.duration * 0.9)
    if not sil:
        report.add("no_long_silence", True, "无长静音", severity="info")
    elif whole_silent:
        report.add("no_long_silence", False,
                   f"整片几乎无声（{total_silence:.2f}s / {info.duration:.2f}s 低于 -45dB）"
                   "—— 检查环境声/BGM 是否真的混进来了",
                   severity="error")
        report.issues.append(Issue("error", "Q102",
                                   f"整片无声：{total_silence:.2f}s 静音占成片 "
                                   f"{total_silence / max(info.duration, 1e-6) * 100:.0f}%",
                                   mp4))
    else:
        report.add("no_long_silence", False,
                   f"{len(sil)} 段超 {silence_min_dur}s 的静音，共 {total_silence:.2f}s",
                   severity="warn")

    return report


def _measure_loudness(mp4: str, paths: config.Paths) -> Optional[tuple]:
    """量成片的积分响度与真峰值。量不出来返回 ``None``（老 ffmpeg 没有 ebur128）。

    和 :func:`_scan` 一样只读 stderr：ebur128 的报告写在 stderr，退出码恒为 0。
    """
    text = _scan(mp4, "ebur128=peak=true", paths)
    if not text:
        return None
    lufs_hits = re.findall(r"I:\s+(-?\d+\.\d+) LUFS", text)
    if not lufs_hits:
        return None
    peak_hits = re.findall(r"Peak:\s+(-?\d+\.\d+) dBFS", text)
    peak = float(peak_hits[-1]) if peak_hits else None
    return float(lufs_hits[-1]), peak


def _scan(mp4: str, afilter: str, paths: config.Paths) -> str:
    """跑一个「把报告写在 stderr」的 ffmpeg 滤镜，返回 stderr 文本。

    ``blackdetect`` / ``silencedetect`` 都只能通过 stderr 读结果，所以这里
    不用 :func:`media.run`（它按退出码判成败，而这类滤镜的退出码恒为 0）。
    """
    ffmpeg = paths.ffmpeg
    if not ffmpeg:
        return ""
    import subprocess
    cmd = [ffmpeg, "-hide_banner", "-i", mp4, "-vf" if "blackdetect" in afilter else "-af",
           afilter, "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    except OSError:
        return ""
    return (proc.stderr or b"").decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# 合起来
# --------------------------------------------------------------------------- #
def run_all(tl: Timeline, mp4: Optional[str] = None,
            *, tolerance: float = 0.25,
            paths: Optional[config.Paths] = None) -> QcReport:
    """时间线 + 成片一起查。``mp4`` 给了才查第二层。"""
    report = check_timeline(tl)
    if mp4:
        rendered = check_render(mp4, expect_duration=tl.duration,
                                expect_canvas=(tl.canvas.width, tl.canvas.height),
                                duration_tolerance=tolerance, paths=paths)
        report.checks.extend(rendered.checks)
        report.issues.extend(rendered.issues)
    return report
