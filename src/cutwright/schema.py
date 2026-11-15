"""分镜表（shotlist）的权威定义、解析与校验。

一个 shotlist 描述「一条要成片的视频」：画布、帧率、幕结构、视频段、字幕、
旁白、音频、图片叠加、文字样式。它是 cutwright 的唯一输入格式，也是所有
后端（剪映 / 达芬奇 / ffmpeg）共同的中间表示。

三条设计原则：

1. **未知字段一律保留**。用户加自己的 ``note`` / ``tags`` / 自定义键不会被丢弃，
   于是同一个 shotlist 既能喂给 cutwright，也能喂给别的工具。
2. **解析与校验分离**。``load_shotlist()`` 只把 JSON 变成对象，绝不抛业务错误；
   ``validate()`` 只产出结构化 ``Issue`` 列表，不改数据。调用方决定多严。
3. **单位是秒**。帧号、微秒都只是派生的表现层，不进 schema —— 帧号在写剪映
   草稿时才由 ``fps`` 换算，避免同一份数据两套时间基准互相漂移。

字段名与中文影视工业习惯对齐（``src_start`` 是素材内入点，``tl_start`` 是
时间线位置），这样从分镜脚本翻译过来不用二次翻译。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "Canvas",
    "VideoSegment",
    "TextSegment",
    "VoSegment",
    "AudioSegment",
    "ImageOverlay",
    "Act",
    "TextStyle",
    "Shotlist",
    "Issue",
    "load_shotlist",
    "load_shotlist_str",
    "validate",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = "1.0"

# 关键词分隔符：半角与全角都认。中文输入法打出来的是全角「｜」，
# 只认半角就等于让一半人在成片里看到一根竖线。
KEYWORD_SEPARATORS = "|｜"

# 校验等级：error 表示后端一定会产出坏结果，warn 表示可疑但可渲染。
ERROR = "error"
WARN = "warn"


def split_keywords(text: str) -> Tuple[str, List[str]]:
    """按分隔符把文案切成「常规段 + 关键词段列表」。

    返回 ``(第一段, [关键词段...])``：``"a|b"`` → ``("a", ["b"])``，
    ``"a|b|c"`` → ``("a", ["b"])``（第三段是常规色，只影响渲染，见 ``text.py``）。
    没有分隔符时返回 ``(原文, [])``。
    """
    parts: List[str] = []
    cur: List[str] = []
    for ch in text or "":
        if ch in KEYWORD_SEPARATORS:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts[0], parts[1::2]


# --------------------------------------------------------------------------- #
# 基础构件
# --------------------------------------------------------------------------- #
@dataclass
class Canvas:
    """画布尺寸。竖屏 1080x1920 是短剧/带货的默认值。"""

    width: int = 1080
    height: int = 1920
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "Canvas":
        if not isinstance(raw, dict):
            return cls()
        known = {"width", "height"}
        return cls(
            width=_as_int(raw.get("width"), 1080),
            height=_as_int(raw.get("height"), 1920),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"width": self.width, "height": self.height}
        d.update(self.extra)
        return d

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


@dataclass
class Act:
    """幕。用于旁白分段和节奏统计，不参与渲染计算。"""

    key: str = ""
    name: str = ""
    start: float = 0.0
    dur: float = 0.0
    vo: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Act":
        known = {"key", "name", "start", "dur", "vo"}
        return cls(
            key=str(raw.get("key", "")),
            name=str(raw.get("name", "")),
            start=_as_float(raw.get("start"), 0.0),
            dur=_as_float(raw.get("dur"), 0.0),
            vo=str(raw.get("vo", "")),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"key": self.key, "name": self.name, "start": self.start,
             "dur": self.dur, "vo": self.vo}
        d.update(self.extra)
        return d


@dataclass
class VideoSegment:
    """一个镜头。素材内的入点/出点 + 时间线上的落点。"""

    file: str = ""
    src_start: float = 0.0
    src_dur: float = 0.0
    tl_start: Optional[float] = None
    speed: float = 1.0
    act: str = ""
    shot: Optional[int] = None
    desc: str = ""
    # 构图/运镜提示，ffmpeg 后端会真的用上，NLE 后端只用来生成备注
    zoom: str = "none"          # none | in | out
    zoom_amount: float = 0.05
    cx: float = 0.5             # 裁切中心（0=左, 1=右）
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VideoSegment":
        known = {"file", "src", "src_start", "src_dur", "tl_start", "speed", "act",
                 "shot", "desc", "zoom", "zoom_amount", "cx", "tl_dur"}
        # 兼容两种写法：{file: ...} 与 {src: ...}
        f = raw.get("file", raw.get("src", ""))
        seg = cls(
            file=str(f or ""),
            src_start=_as_float(raw.get("src_start", raw.get("in")), 0.0),
            src_dur=_as_float(raw.get("src_dur"), 0.0),
            tl_start=_as_opt_float(raw.get("tl_start")),
            speed=_as_float(raw.get("speed"), 1.0),
            act=str(raw.get("act", "")),
            shot=_as_opt_int(raw.get("shot")),
            desc=str(raw.get("desc", "")),
            zoom=str(raw.get("zoom", "none") or "none"),
            zoom_amount=_as_float(raw.get("zoom_amount"), 0.05),
            cx=_as_float(raw.get("cx"), 0.5),
            extra={k: v for k, v in raw.items() if k not in known},
        )
        # 只给了 src_dur 与 src_start/out 之一时补齐
        if seg.src_dur <= 0:
            out = raw.get("src_end", raw.get("out"))
            if out is not None:
                seg.src_dur = max(0.0, _as_float(out, 0.0) - seg.src_start)
        return seg

    @property
    def tl_dur(self) -> float:
        """时间线上占据的时长（变速后）。"""
        sp = self.speed if self.speed and self.speed > 0 else 1.0
        return self.src_dur / sp

    @property
    def src_end(self) -> float:
        return self.src_start + self.src_dur

    def to_dict(self) -> Dict[str, Any]:
        d = {"file": self.file, "src_start": self.src_start, "src_dur": self.src_dur,
             "tl_start": self.tl_start, "speed": self.speed, "act": self.act,
             "shot": self.shot, "desc": self.desc, "zoom": self.zoom,
             "zoom_amount": self.zoom_amount, "cx": self.cx}
        d.update(self.extra)
        return d


@dataclass
class TextSegment:
    """字幕 / 花字 / 标题。关键词用分隔符括起来，渲染成高亮色。

    分隔符同时接受半角 ``|`` 和全角 ``｜``——中文输入法打出来的是后者，
    只认一个就等于让一半人在屏幕上看到一根竖线。规则是**交替**的：
    分隔符之外是常规色，第一对分隔符之间是高亮色，第二对之间又回到常规色……
    ``"自动裁剪｜变速｜补增益"`` → 常规「自动裁剪」+ 高亮「变速」+ 常规「补增益」。
    """

    text: str = ""
    tl_start: Optional[float] = None
    tl_dur: float = 0.0
    style: str = "caption"
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TextSegment":
        known = {"text", "tl_start", "tl_dur", "style"}
        return cls(
            text=str(raw.get("text", "")),
            tl_start=_as_opt_float(raw.get("tl_start")),
            tl_dur=_as_float(raw.get("tl_dur"), 0.0),
            style=str(raw.get("style", "caption") or "caption"),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"text": self.text, "tl_start": self.tl_start,
             "tl_dur": self.tl_dur, "style": self.style}
        d.update(self.extra)
        return d

    @property
    def head(self) -> str:
        """第一个分隔符前面（常规色）的部分。"""
        return split_keywords(self.text)[0]

    @property
    def keyword(self) -> str:
        """第一对分隔符之间（关键词色）的部分，没有则为空。"""
        segments = split_keywords(self.text)[1]
        return segments[0] if segments else ""


@dataclass
class VoSegment:
    """旁白。剪映里靠「文本朗读」出配音，所以这层文字必须不可见。"""

    text: str = ""
    tl_start: Optional[float] = None
    tl_dur: float = 0.0
    act: str = ""
    style: str = "vo_hidden"
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "VoSegment":
        known = {"text", "tl_start", "tl_dur", "act", "style"}
        return cls(
            text=str(raw.get("text", "")),
            tl_start=_as_opt_float(raw.get("tl_start")),
            tl_dur=_as_float(raw.get("tl_dur"), 0.0),
            act=str(raw.get("act", "")),
            style=str(raw.get("style", "vo_hidden") or "vo_hidden"),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"text": self.text, "tl_start": self.tl_start, "tl_dur": self.tl_dur,
             "act": self.act, "style": self.style}
        d.update(self.extra)
        return d


@dataclass
class AudioSegment:
    """配乐 / 音效 / 现成配音轨。"""

    file: str = ""
    tl_start: float = 0.0
    tl_dur: Optional[float] = None
    src_start: float = 0.0
    gain_db: float = 0.0
    kind: str = "music"         # music | sfx | vo
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AudioSegment":
        known = {"file", "tl_start", "tl_dur", "src_start", "gain_db", "kind"}
        return cls(
            file=str(raw.get("file", "")),
            tl_start=_as_float(raw.get("tl_start"), 0.0),
            tl_dur=_as_opt_float(raw.get("tl_dur")),
            src_start=_as_float(raw.get("src_start"), 0.0),
            gain_db=_as_float(raw.get("gain_db"), 0.0),
            kind=str(raw.get("kind", "music") or "music"),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"file": self.file, "tl_start": self.tl_start, "tl_dur": self.tl_dur,
             "src_start": self.src_start, "gain_db": self.gain_db, "kind": self.kind}
        d.update(self.extra)
        return d


@dataclass
class ImageOverlay:
    """透明 PNG 叠加层：边框、装饰、贴纸、水印。"""

    file: str = ""
    tl_start: float = 0.0
    tl_dur: Optional[float] = None
    fade: float = 0.18
    fade_out: bool = False
    slide_px: float = 0.0
    slide_dur: float = 0.24
    float_amp: float = 0.0
    float_period: float = 4.0
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ImageOverlay":
        known = {"file", "tl_start", "tl_dur", "fade", "fade_out", "slide_px",
                 "slide_dur", "float_amp", "float_period"}
        return cls(
            file=str(raw.get("file", "")),
            tl_start=_as_float(raw.get("tl_start"), 0.0),
            tl_dur=_as_opt_float(raw.get("tl_dur")),
            fade=_as_float(raw.get("fade"), 0.18),
            fade_out=bool(raw.get("fade_out", False)),
            slide_px=_as_float(raw.get("slide_px"), 0.0),
            slide_dur=_as_float(raw.get("slide_dur"), 0.24),
            float_amp=_as_float(raw.get("float_amp"), 0.0),
            float_period=_as_float(raw.get("float_period"), 4.0),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"file": self.file, "tl_start": self.tl_start, "tl_dur": self.tl_dur,
             "fade": self.fade, "fade_out": self.fade_out, "slide_px": self.slide_px,
             "slide_dur": self.slide_dur, "float_amp": self.float_amp,
             "float_period": self.float_period}
        d.update(self.extra)
        return d


@dataclass
class TextStyle:
    """一种文字外观。像素值以 shotlist 的 canvas 为基准，后端自己换算。"""

    role: str = ""
    font_height_px: float = 100.0
    color: str = "#FFFFFF"
    keyword_color: str = "#FFD54A"
    stroke_color: str = "#000000"
    stroke_width_px: float = 6.0
    align: str = "center"
    bottom_margin_px: Optional[float] = None
    center_ratio_y: Optional[float] = None
    max_chars_per_line: int = 0
    rotation_deg: float = 0.0
    invisible: bool = False
    anim: str = "none"
    font: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TextStyle":
        known = {"role", "font_height_px", "color", "keyword_color", "stroke_color",
                 "stroke_width_px", "align", "bottom_margin_px", "center_ratio_y",
                 "max_chars_per_line", "rotation_deg", "invisible", "anim", "font",
                 "note"}
        return cls(
            role=str(raw.get("role", "")),
            font_height_px=_as_float(raw.get("font_height_px"), 100.0),
            color=str(raw.get("color", "#FFFFFF")),
            keyword_color=str(raw.get("keyword_color", "#FFD54A")),
            stroke_color=str(raw.get("stroke_color", "#000000")),
            stroke_width_px=_as_float(raw.get("stroke_width_px"), 6.0),
            align=str(raw.get("align", "center")),
            bottom_margin_px=_as_opt_float(raw.get("bottom_margin_px")),
            center_ratio_y=_as_opt_float(raw.get("center_ratio_y")),
            max_chars_per_line=_as_int(raw.get("max_chars_per_line"), 0),
            rotation_deg=_as_float(raw.get("rotation_deg"), 0.0),
            invisible=bool(raw.get("invisible", False)),
            anim=str(raw.get("anim", "none") or "none"),
            font=str(raw.get("font", "")),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {"role": self.role, "font_height_px": self.font_height_px,
             "color": self.color, "keyword_color": self.keyword_color,
             "stroke_color": self.stroke_color, "stroke_width_px": self.stroke_width_px,
             "align": self.align, "bottom_margin_px": self.bottom_margin_px,
             "center_ratio_y": self.center_ratio_y,
             "max_chars_per_line": self.max_chars_per_line,
             "rotation_deg": self.rotation_deg, "invisible": self.invisible,
             "anim": self.anim, "font": self.font}
        d.update(self.extra)
        return d


# --------------------------------------------------------------------------- #
# 顶层对象
# --------------------------------------------------------------------------- #
@dataclass
class Shotlist:
    name: str = "untitled"
    material_dir: str = ""
    asset_dir: str = ""
    canvas: Canvas = field(default_factory=Canvas)
    fps: float = 30.0
    total_duration: Optional[float] = None
    acts: List[Act] = field(default_factory=list)
    video_segments: List[VideoSegment] = field(default_factory=list)
    text_segments: List[TextSegment] = field(default_factory=list)
    vo_segments: List[VoSegment] = field(default_factory=list)
    audio_segments: List[AudioSegment] = field(default_factory=list)
    image_overlays: List[ImageOverlay] = field(default_factory=list)
    styles: Dict[str, TextStyle] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)
    # 分镜表文件所在目录。**不写进 to_dict()**：它只是让「相对路径」有意义，
    # 写出去会把绝对路径泄漏进可提交的文件里。手搓 Shotlist() 时为空 =
    # 相对 cwd 解析（旧行为）。
    base_dir: str = ""

    # -- 解析 ------------------------------------------------------------- #
    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Shotlist":
        known = {"name", "material_dir", "asset_dir", "canvas", "fps",
                 "total_duration", "acts", "video_segments", "text_segments",
                 "vo_segments", "audio_segments", "image_overlays", "styles",
                 "schema_version"}
        styles_raw = raw.get("styles") or {}
        return cls(
            name=str(raw.get("name", "untitled")),
            material_dir=str(raw.get("material_dir", "") or ""),
            asset_dir=str(raw.get("asset_dir", "") or ""),
            canvas=Canvas.from_dict(raw.get("canvas")),
            fps=_as_float(raw.get("fps"), 30.0),
            total_duration=_as_opt_float(raw.get("total_duration")),
            acts=[Act.from_dict(x) for x in (raw.get("acts") or []) if isinstance(x, dict)],
            video_segments=[VideoSegment.from_dict(x) for x in
                            (raw.get("video_segments") or []) if isinstance(x, dict)],
            text_segments=[TextSegment.from_dict(x) for x in
                           (raw.get("text_segments") or []) if isinstance(x, dict)],
            vo_segments=[VoSegment.from_dict(x) for x in
                         (raw.get("vo_segments") or []) if isinstance(x, dict)],
            audio_segments=[AudioSegment.from_dict(x) for x in
                            (raw.get("audio_segments") or []) if isinstance(x, dict)],
            image_overlays=[ImageOverlay.from_dict(x) for x in
                            (raw.get("image_overlays") or []) if isinstance(x, dict)],
            styles={str(k): TextStyle.from_dict(v) for k, v in styles_raw.items()
                    if isinstance(v, dict)},
            schema_version=str(raw.get("schema_version", SCHEMA_VERSION)),
            extra={k: v for k, v in raw.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "name": self.name,
            "material_dir": self.material_dir,
            "asset_dir": self.asset_dir,
            "canvas": self.canvas.to_dict(),
            "fps": self.fps,
            "total_duration": self.total_duration,
            "acts": [a.to_dict() for a in self.acts],
            "video_segments": [v.to_dict() for v in self.video_segments],
            "text_segments": [t.to_dict() for t in self.text_segments],
            "vo_segments": [v.to_dict() for v in self.vo_segments],
            "audio_segments": [a.to_dict() for a in self.audio_segments],
            "image_overlays": [i.to_dict() for i in self.image_overlays],
            "styles": {k: v.to_dict() for k, v in self.styles.items()},
        }
        d.update(self.extra)
        return d

    # -- 便捷属性 ---------------------------------------------------------- #
    def video_duration(self) -> float:
        """按时间线顺序累加得到的成片时长。"""
        t = 0.0
        for seg in self.video_segments:
            start = seg.tl_start if seg.tl_start is not None else t
            t = max(t, start + seg.tl_dur)
        return t

    def _resolve(self, directory: str, file: str) -> str:
        """相对路径的解析规则：**先按分镜表所在目录，再按 cwd**。

        为什么是这个顺序：一份分镜表和它的 `materials/` 通常放在一起，
        `examples/demo/shotlist.json` 里写 `"material_dir": "materials"` 就应当
        指向 `examples/demo/materials/`，而不是「你恰好从哪里启动的命令行」。
        找不到时回落到 cwd，兼容老的、按 cwd 写的分镜表。
        """
        if not file:
            return ""
        if os.path.isabs(file):
            return file
        candidates = []
        if self.base_dir:
            candidates.append(os.path.normpath(
                os.path.join(self.base_dir, directory or ".", file)))
        candidates.append(os.path.normpath(os.path.join(directory or ".", file)))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[0]        # 都不存在：给"期望"的路径，报错时更有用

    def resolve_material(self, file: str) -> str:
        """把 ``file`` 解析成路径（相对 ``material_dir``，再相对分镜表目录）。"""
        return self._resolve(self.material_dir, file)

    def resolve_asset(self, file: str) -> str:
        """把素材（PNG 叠加 / 封面）解析成路径（相对 ``asset_dir``）。"""
        return self._resolve(self.asset_dir, file)


@dataclass
class Issue:
    """一条校验结论。``where`` 尽量写成用户可以 grep 的定位串。"""

    level: str
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f" [{self.where}]" if self.where else ""
        return f"{self.level.upper():5s} {self.code}{loc} {self.message}"

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "code": self.code,
                "message": self.message, "where": self.where}


def load_shotlist(path: str) -> Shotlist:
    """读取 shotlist JSON。文件不存在 / 非法 JSON 会抛异常，其余问题交给 validate()。

    读取时会记下分镜表自身所在目录（``base_dir``），让表里的相对路径
    （``material_dir: "materials"``）相对**这张表**解析，而不是相对你当时恰好
    站在哪个目录下敲的命令。
    """
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"shotlist must be a JSON object, got {type(raw).__name__}")
    shotlist = Shotlist.from_dict(raw)
    shotlist.base_dir = os.path.dirname(os.path.abspath(path))
    return shotlist


def load_shotlist_str(text: str) -> Shotlist:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("shotlist must be a JSON object")
    return Shotlist.from_dict(raw)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
BUILTIN_STYLE_ROLES = ("caption", "title", "huazi", "end", "vo_hidden")
"""内置样式角色。不写进 ``styles`` 也能用——由 :class:`TextStyle` 的默认值兜底。

理由：一张最小的分镜表不该为了出片而把五种样式全抄一遍。这几只是**留白就有
合理默认**的位置，不是自由命名的占位符。写错了名字（``capiton``）仍然报 E031，
所以拼写保护还在。
"""


def validate(shotlist: Shotlist, check_files: bool = False) -> List[Issue]:
    """对 shotlist 做静态体检，返回问题列表（空列表 = 可以渲染）。

    ``check_files=True`` 时额外检查素材文件是否存在——这一步会碰磁盘，
    批处理很多条 shotlist 时通常关掉。
    """
    out: List[Issue] = []

    def err(code: str, msg: str, where: str = "") -> None:
        out.append(Issue(ERROR, code, msg, where))

    def warn(code: str, msg: str, where: str = "") -> None:
        out.append(Issue(WARN, code, msg, where))

    # --- 画布与帧率 ---
    if shotlist.canvas.width <= 0 or shotlist.canvas.height <= 0:
        err("E001", f"canvas 尺寸非法 {shotlist.canvas.width}x{shotlist.canvas.height}")
    if shotlist.fps <= 0:
        err("E002", f"fps 必须为正数，当前 {shotlist.fps}")

    # --- 视频段 ---
    if not shotlist.video_segments:
        err("E010", "video_segments 为空，没有内容可剪")

    seen_t = 0.0
    for i, seg in enumerate(shotlist.video_segments):
        where = f"video_segments[{i}]"
        if not seg.file:
            err("E011", "缺少 file 字段", where)
        if seg.src_dur <= 0:
            err("E012", f"src_dur 必须为正数，当前 {seg.src_dur}", where)
        if seg.src_start < 0:
            err("E013", f"src_start 不能为负，当前 {seg.src_start}", where)
        if seg.speed <= 0:
            err("E014", f"speed 必须为正数，当前 {seg.speed}", where)
        if seg.zoom not in ("none", "in", "out"):
            warn("W015", f"未知 zoom 值 {seg.zoom!r}，按 none 处理", where)
        if not (0.0 <= seg.cx <= 1.0):
            warn("W016", f"cx 应在 0..1，当前 {seg.cx}", where)
        if check_files and seg.file:
            p = shotlist.resolve_material(seg.file)
            if not os.path.exists(p):
                err("E017", f"素材不存在: {p}", where)
        # 时间线连续性：显式给了 tl_start 就必须接得上，没给就自动顺序排
        if seg.tl_start is not None and seg.tl_start > seen_t + 1e-6:
            warn("W018", f"上一镜结束于 {seen_t:.3f}s，本镜从 {seg.tl_start:.3f}s 开始，留了空档", where)
        elif seg.tl_start is not None and seg.tl_start < seen_t - 1e-6:
            warn("W019", f"本镜从 {seg.tl_start:.3f}s 开始，与上一镜（结束 {seen_t:.3f}s）重叠", where)
        seen_t = max(seen_t, (seg.tl_start if seg.tl_start is not None else seen_t) + seg.tl_dur)

    # --- 时长一致性 ---
    derived = shotlist.video_duration()
    if shotlist.total_duration is not None and derived > 0:
        drift = abs(shotlist.total_duration - derived)
        if drift > max(0.05, derived * 0.01):
            warn("W020", f"total_duration={shotlist.total_duration:.3f}s 与实际累加 "
                         f"{derived:.3f}s 差 {drift:.3f}s")

    # --- 文字 ---
    for group, items in (("text_segments", shotlist.text_segments),
                         ("vo_segments", shotlist.vo_segments)):
        for i, seg in enumerate(items):
            where = f"{group}[{i}]"
            if not seg.text.strip():
                err("E030", "文字内容为空", where)
            if seg.style not in shotlist.styles and seg.style not in BUILTIN_STYLE_ROLES:
                err("E031", f"引用了未定义的文字样式 {seg.style!r}，"
                            f"已定义: {sorted(shotlist.styles) or '无'}；"
                            f"内置角色: {list(BUILTIN_STYLE_ROLES)}", where)
            if seg.tl_dur <= 0:
                err("E032", f"tl_dur 必须为正数，当前 {seg.tl_dur}", where)
            if seg.tl_start is not None and seg.tl_start < 0:
                err("E033", f"tl_start 不能为负，当前 {seg.tl_start}", where)
            if seg.tl_start is not None and derived > 0 and seg.tl_start >= derived - 1e-6:
                warn("W034", f"起始 {seg.tl_start:.3f}s 已在成片末尾（{derived:.3f}s）之后，永远不会显示", where)

    # --- 叠加层 / 音频 ---
    for i, ov in enumerate(shotlist.image_overlays):
        where = f"image_overlays[{i}]"
        if not ov.file:
            err("E040", "缺少 file 字段", where)
        if check_files and ov.file:
            p = shotlist.resolve_asset(ov.file)
            if not os.path.exists(p):
                err("E041", f"叠加素材不存在: {p}", where)
    for i, au in enumerate(shotlist.audio_segments):
        where = f"audio_segments[{i}]"
        if not au.file:
            err("E050", "缺少 file 字段", where)
        if check_files and au.file:
            p = shotlist.resolve_asset(au.file)
            if not os.path.exists(p):
                err("E051", f"音频不存在: {p}", where)

    # --- 样式本身 ---
    for name, st in shotlist.styles.items():
        if st.font_height_px <= 0:
            err("E060", f"字体高度必须为正数，当前 {st.font_height_px}", f"styles.{name}")

    return out


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(i.level == ERROR for i in issues)


# --------------------------------------------------------------------------- #
# 宽松的类型转换：分镜表是手写的，一个字符 "0.5" 不应该炸掉整条流水线
# --------------------------------------------------------------------------- #
def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
