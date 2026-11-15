"""时间线中间表示（IR）。

后端之间唯一的共同语言就是它：``Timeline``。流程永远是

    分镜表 shotlist  ──build──▶  Timeline IR  ──▶  剪映 / 达芬奇 / ffmpeg

这么切分的好处是**后端只负责「把这份确定的时间线翻译成目标格式」**，
不需要再理解分镜表里那些业务字段（幕、描述、镜头号）。加一个新后端
（Premiere、Final Cut、剪映海外版）只需要实现一个 ``build(timeline)``。

IR 里的时间一律是**秒**（浮点），单位换算只发生在最外层的写出阶段；
需要帧号的地方用 ``Timeline.frames()`` 现算。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema import (Canvas, Issue, Shotlist, TextStyle, validate)

__all__ = [
    "TimelineError",
    "TimelineClip",
    "TimelineText",
    "TimelineAudio",
    "TimelineOverlay",
    "Timeline",
]


class TimelineError(ValueError):
    """分镜表无法构成一条可渲染的时间线。带结构化问题列表。"""

    def __init__(self, message: str, issues: Optional[List[Issue]] = None):
        self.issues = issues or []
        detail = "\n".join("  " + str(i) for i in self.issues)
        super().__init__(f"{message}\n{detail}" if detail else message)


@dataclass
class TimelineClip:
    """一个镜头在时间线上的落地。``source`` 是绝对路径。"""

    index: int
    source: str
    src_in: float
    src_out: float
    tl_start: float
    speed: float = 1.0
    zoom: str = "none"
    zoom_amount: float = 0.05
    cx: float = 0.5
    act: str = ""
    shot: Optional[int] = None
    desc: str = ""

    @property
    def src_dur(self) -> float:
        return max(0.0, self.src_out - self.src_in)

    @property
    def tl_dur(self) -> float:
        sp = self.speed if self.speed and self.speed > 0 else 1.0
        return self.src_dur / sp

    @property
    def tl_end(self) -> float:
        return self.tl_start + self.tl_dur

    @property
    def name(self) -> str:
        return os.path.basename(self.source)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "source": self.source,
            "src_in": round(self.src_in, 4), "src_out": round(self.src_out, 4),
            "tl_start": round(self.tl_start, 4), "tl_dur": round(self.tl_dur, 4),
            "speed": self.speed, "zoom": self.zoom, "cx": self.cx,
            "act": self.act, "shot": self.shot, "desc": self.desc,
        }


@dataclass
class TimelineText:
    index: int
    text: str
    tl_start: float
    tl_dur: float
    style_name: str
    style: TextStyle
    role: str = "text"          # text | vo

    @property
    def head(self) -> str:
        return self.text.split("|", 1)[0]

    @property
    def keyword(self) -> str:
        parts = self.text.split("|", 1)
        return parts[1] if len(parts) > 1 else ""

    @property
    def visible(self) -> bool:
        """旁白文字靠「文本朗读」出声，本身不能出现在画面上。"""
        return not self.style.invisible

    @property
    def tl_end(self) -> float:
        return self.tl_start + self.tl_dur

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "role": self.role, "text": self.text,
            "tl_start": round(self.tl_start, 4), "tl_dur": round(self.tl_dur, 4),
            "style": self.style_name, "visible": self.visible,
        }


@dataclass
class TimelineAudio:
    index: int
    source: str
    tl_start: float
    tl_dur: float
    src_in: float = 0.0
    gain_db: float = 0.0
    kind: str = "music"

    @property
    def tl_end(self) -> float:
        return self.tl_start + self.tl_dur

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "source": self.source, "kind": self.kind,
            "tl_start": round(self.tl_start, 4), "tl_dur": round(self.tl_dur, 4),
            "src_in": round(self.src_in, 4), "gain_db": self.gain_db,
        }


@dataclass
class TimelineOverlay:
    index: int
    image: str
    tl_start: float
    tl_dur: float
    fade: float = 0.18
    fade_out: bool = False
    slide_px: float = 0.0
    slide_dur: float = 0.24
    float_amp: float = 0.0
    float_period: float = 4.0

    @property
    def tl_end(self) -> float:
        return self.tl_start + self.tl_dur

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "image": self.image,
            "tl_start": round(self.tl_start, 4), "tl_dur": round(self.tl_dur, 4),
            "fade": self.fade, "fade_out": self.fade_out,
            "slide_px": self.slide_px, "float_amp": self.float_amp,
        }


@dataclass
class Timeline:
    """一条可以直接交给后端渲染的完整时间线。"""

    name: str = "untitled"
    canvas: Canvas = field(default_factory=Canvas)
    fps: float = 30.0
    duration: float = 0.0
    clips: List[TimelineClip] = field(default_factory=list)
    texts: List[TimelineText] = field(default_factory=list)
    audios: List[TimelineAudio] = field(default_factory=list)
    overlays: List[TimelineOverlay] = field(default_factory=list)
    acts: List[Dict[str, Any]] = field(default_factory=list)
    styles: Dict[str, TextStyle] = field(default_factory=dict)
    shotlist: Optional[Shotlist] = None
    issues: List[Issue] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def from_shotlist(cls, shotlist: Shotlist, strict: bool = True,
                      check_files: bool = False) -> "Timeline":
        """把分镜表编译成时间线。

        没写 ``tl_start`` 的段落会被**顺序排列**（这就是「我不想手算时间轴」
        的那条路）；写了 ``tl_start`` 的按原值保留，只做一致性检查。

        ``strict=True`` 时，任何 error 级问题都抛 :class:`TimelineError`。
        """
        issues = validate(shotlist, check_files=check_files)
        if strict and any(i.level == "error" for i in issues):
            raise TimelineError(f"分镜表 {shotlist.name!r} 校验未通过", issues)

        # --- 视频段 ---
        clips: List[TimelineClip] = []
        cursor = 0.0
        for i, seg in enumerate(shotlist.video_segments):
            tl_start = seg.tl_start if seg.tl_start is not None else cursor
            clips.append(TimelineClip(
                index=i,
                source=shotlist.resolve_material(seg.file),
                src_in=seg.src_start,
                src_out=seg.src_start + seg.src_dur,
                tl_start=tl_start,
                speed=seg.speed if seg.speed > 0 else 1.0,
                zoom=seg.zoom,
                zoom_amount=seg.zoom_amount,
                cx=seg.cx,
                act=seg.act,
                shot=seg.shot,
                desc=seg.desc,
            ))
            cursor = tl_start + clips[-1].tl_dur

        duration = max((c.tl_end for c in clips), default=0.0)

        # --- 文字 ---
        texts: List[TimelineText] = []
        for i, seg in enumerate(shotlist.text_segments):
            style = shotlist.styles.get(seg.style) or TextStyle()
            texts.append(TimelineText(
                index=len(texts), text=seg.text,
                tl_start=seg.tl_start if seg.tl_start is not None else 0.0,
                tl_dur=seg.tl_dur, style_name=seg.style, style=style, role="text",
            ))
        for i, seg in enumerate(shotlist.vo_segments):
            style = shotlist.styles.get(seg.style) or TextStyle(invisible=True)
            texts.append(TimelineText(
                index=len(texts), text=seg.text,
                tl_start=seg.tl_start if seg.tl_start is not None else 0.0,
                tl_dur=seg.tl_dur, style_name=seg.style, style=style, role="vo",
            ))
        texts.sort(key=lambda t: (t.tl_start, t.index))

        # --- 音频 ---
        audios: List[TimelineAudio] = []
        for i, seg in enumerate(shotlist.audio_segments):
            tl_dur = seg.tl_dur if seg.tl_dur is not None else max(0.0, duration - seg.tl_start)
            audios.append(TimelineAudio(
                index=i, source=shotlist.resolve_asset(seg.file),
                tl_start=seg.tl_start, tl_dur=tl_dur,
                src_in=seg.src_start, gain_db=seg.gain_db, kind=seg.kind,
            ))

        # --- 叠加层 ---
        overlays: List[TimelineOverlay] = []
        for i, seg in enumerate(shotlist.image_overlays):
            tl_dur = seg.tl_dur if seg.tl_dur is not None else max(0.0, duration - seg.tl_start)
            overlays.append(TimelineOverlay(
                index=i, image=shotlist.resolve_asset(seg.file),
                tl_start=seg.tl_start, tl_dur=tl_dur,
                fade=seg.fade, fade_out=seg.fade_out,
                slide_px=seg.slide_px, slide_dur=seg.slide_dur,
                float_amp=seg.float_amp, float_period=seg.float_period,
            ))

        return cls(
            name=shotlist.name,
            canvas=shotlist.canvas,
            fps=shotlist.fps if shotlist.fps > 0 else 30.0,
            duration=duration,
            clips=clips,
            texts=texts,
            audios=audios,
            overlays=overlays,
            acts=[a.to_dict() for a in shotlist.acts],
            styles=dict(shotlist.styles),
            shotlist=shotlist,
            issues=issues,
        )

    # ------------------------------------------------------------------ #
    # 时间换算
    # ------------------------------------------------------------------ #
    def frames(self, seconds: float) -> int:
        """秒 → 帧号（四舍五入，避免 29.97 下的累计漂移）。"""
        return int(round(seconds * self.fps))

    def seconds(self, frames: int) -> float:
        return frames / self.fps if self.fps else 0.0

    @property
    def total_frames(self) -> int:
        return self.frames(self.duration)

    def clip_at(self, seconds: float) -> Optional[TimelineClip]:
        """某个时间点落在哪个镜头上（做封面、抽帧时用）。"""
        for clip in self.clips:
            if clip.tl_start <= seconds < clip.tl_end:
                return clip
        return self.clips[-1] if self.clips else None

    # ------------------------------------------------------------------ #
    # 报告
    # ------------------------------------------------------------------ #
    def summary(self) -> Dict[str, Any]:
        """给 ``cutwright plan`` 打印的结构化摘要。"""
        n = len(self.clips)
        durations = [c.tl_dur for c in self.clips]
        per_act: Dict[str, float] = {}
        for c in self.clips:
            if c.act:
                per_act[c.act] = per_act.get(c.act, 0.0) + c.tl_dur
        return {
            "name": self.name,
            "duration": round(self.duration, 3),
            "duration_hms": _hms(self.duration),
            "canvas": f"{self.canvas.width}x{self.canvas.height}",
            "fps": self.fps,
            "shots": n,
            "avg_shot": round(sum(durations) / n, 3) if n else 0.0,
            "min_shot": round(min(durations), 3) if n else 0.0,
            "max_shot": round(max(durations), 3) if n else 0.0,
            "texts": sum(1 for t in self.texts if t.visible),
            "vo_segments": sum(1 for t in self.texts if t.role == "vo"),
            "overlays": len(self.overlays),
            "audios": len(self.audios),
            "per_act": {k: round(v, 3) for k, v in per_act.items()},
            "errors": sum(1 for i in self.issues if i.level == "error"),
            "warnings": sum(1 for i in self.issues if i.level == "warn"),
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "canvas": self.canvas.to_dict(),
            "fps": self.fps,
            "duration": round(self.duration, 4),
            "clips": [c.to_dict() for c in self.clips],
            "texts": [t.to_dict() for t in self.texts],
            "audios": [a.to_dict() for a in self.audios],
            "overlays": [o.to_dict() for o in self.overlays],
            "acts": self.acts,
            "issues": [i.to_dict() for i in self.issues],
        }


def _hms(seconds: float) -> str:
    total = max(0.0, seconds)
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:05.2f}"
    return f"{m}:{s:05.2f}"
