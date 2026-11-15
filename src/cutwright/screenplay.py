"""剧本层：把「一集短剧的剧本」读进来。

分镜表（``shotlist.json``）回答的是「第几秒放哪一段素材」，它精确，但要求人先
把整条片子排好——口播脚本可以这么写，短剧不行：短剧的量在剧本里（场次、台词、
谁说什么），而分镜是它的下游产物。

这一层只做一件事：**剧本是数据，别让它变成一堆自然语言**。一个
``screenplay.json`` 里是场次与台词；``story`` 模块负责把它变成分镜表。

文件形状（最简可用）::

    {
      "name": "第 1 集",
      "canvas": {"width": 1080, "height": 1920},
      "fps": 30,
      "cast": {"lin": "林队", "zhou": "周小满"},
      "scenes": [
        {"id": "S1", "slug": "地铁站台 · 夜", "tags": ["subway", "night"],
         "lines": [
           {"type": "action", "text": "末班车进站，人群散开"},
           {"who": "zhou", "text": "你确定他会来？"},
           {"who": "lin", "type": "voiceover", "text": "那晚我数到第七班车"}
         ]}
      ]
    }

``type`` 只有四种：``dialogue``（对白，出字幕）、``voiceover``（旁白，默认不出
字幕、单独占一条不可见文字轨）、``action``（动作描写，出字幕当叙事卡）、
``title``（场标/片名卡）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .schema import ERROR, WARN, Canvas, Issue, TextStyle

SCREENPLAY_VERSION = "1.0"
LINE_TYPES: Tuple[str, ...] = ("dialogue", "voiceover", "action", "title")

#: 剧本行类型 → 分镜表文字样式（未在剧本里覆盖时用这个）
LINE_STYLE: Dict[str, str] = {
    "dialogue": "caption",
    "voiceover": "vo_hidden",
    "action": "caption",
    "title": "title",
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
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


def _as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part for part in (value.replace("，", ",").split(",")) if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Line:
    """一句词。``duration`` 不写时由字数估算，或者用旁白音频的真实时长。"""

    text: str = ""
    type: str = "dialogue"
    who: str = ""
    tags: List[str] = field(default_factory=list)
    duration: Optional[float] = None
    pause: float = 0.0
    shot: str = ""          # 硬指定素材（相对 material_dir），跳过自动挑选
    vocal: str = ""         # 旁白/对白的音频文件（相对 vo_dir）
    style: str = ""         # 覆盖 LINE_STYLE[type]
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Line":
        known = {"text", "type", "who", "tags", "duration", "pause", "shot",
                 "vocal", "style"}
        text = data.get("text", data.get("line", data.get("say", "")))
        return cls(
            text=str(text or "").strip(),
            type=str(data.get("type") or "dialogue").strip().lower(),
            who=str(data.get("who") or "").strip(),
            tags=_as_list(data.get("tags")),
            duration=_as_opt_float(data.get("duration")),
            pause=_as_float(data.get("pause"), 0.0),
            shot=str(data.get("shot") or "").strip(),
            vocal=str(data.get("vocal") or "").strip(),
            style=str(data.get("style") or "").strip(),
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type, "text": self.text}
        if self.who:
            out["who"] = self.who
        if self.tags:
            out["tags"] = list(self.tags)
        if self.duration is not None:
            out["duration"] = self.duration
        if self.pause:
            out["pause"] = self.pause
        if self.shot:
            out["shot"] = self.shot
        if self.vocal:
            out["vocal"] = self.vocal
        if self.style:
            out["style"] = self.style
        out.update(self.extra)
        return out

    @property
    def caption_style(self) -> str:
        return self.style or LINE_STYLE.get(self.type, "caption")

    @property
    def speaks(self) -> bool:
        """这句要不要出字幕。旁白默认不出（它在画外，字幕会抢戏）。"""
        return self.type in ("dialogue", "action", "title")


@dataclass
class Scene:
    """一场戏。``tags`` 决定从素材库里挑哪些镜头。"""

    id: str = ""
    slug: str = ""
    tags: List[str] = field(default_factory=list)
    lines: List[Line] = field(default_factory=list)
    duration: Optional[float] = None     # 目标时长（不写就按台词估）
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Scene":
        known = {"id", "slug", "tags", "lines", "duration", "location", "mood"}
        tags = _as_list(data.get("tags"))
        for key in ("location", "mood"):
            value = data.get(key)
            if value:
                tags.append(str(value).strip())
        return cls(
            id=str(data.get("id") or "").strip(),
            slug=str(data.get("slug") or data.get("title") or "").strip(),
            tags=tags,
            lines=[Line.from_dict(item) for item in (data.get("lines") or [])],
            duration=_as_opt_float(data.get("duration")),
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id}
        if self.slug:
            out["slug"] = self.slug
        if self.tags:
            out["tags"] = list(self.tags)
        if self.duration is not None:
            out["duration"] = self.duration
        out["lines"] = [line.to_dict() for line in self.lines]
        out.update(self.extra)
        return out


@dataclass
class Screenplay:
    name: str = "screenplay"
    canvas: Canvas = field(default_factory=Canvas)
    fps: float = 30.0
    cast: Dict[str, str] = field(default_factory=dict)
    scenes: List[Scene] = field(default_factory=list)
    material_dir: str = ""
    vo_dir: str = ""
    music: Dict[str, Any] = field(default_factory=dict)
    styles: Dict[str, TextStyle] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCREENPLAY_VERSION
    base_dir: str = ""              # 只在内存里用，不写进 to_dict()

    # -- 载入/导出 ---------------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Screenplay":
        known = {"name", "canvas", "fps", "cast", "scenes", "material_dir",
                 "vo_dir", "music", "styles", "schema_version"}
        canvas = Canvas.from_dict(data.get("canvas") or {})
        styles: Dict[str, TextStyle] = {}
        for role, spec in (data.get("styles") or {}).items():
            if isinstance(spec, dict):
                styles[role] = TextStyle.from_dict({**spec, "role": role})
        cast_raw = data.get("cast") or {}
        cast = {str(k): str(v) for k, v in cast_raw.items()} if isinstance(cast_raw, dict) else {}
        # fps 不合法时**保留原值**，让 validate 报 S003——把它悄悄改成 30 会让
        # 「剧本里 fps 写错了」变成「排出来的时长对不上」，排查成本高得多。
        fps = _as_float(data.get("fps"), 30.0)
        if fps <= 0 and "fps" not in data:
            fps = 30.0
        return cls(
            name=str(data.get("name") or "screenplay"),
            canvas=canvas,
            fps=fps,
            cast=cast,
            scenes=[Scene.from_dict(item) for item in (data.get("scenes") or [])],
            material_dir=str(data.get("material_dir") or ""),
            vo_dir=str(data.get("vo_dir") or ""),
            music=dict(data.get("music") or {}),
            styles=styles,
            extra={k: v for k, v in data.items() if k not in known},
            schema_version=str(data.get("schema_version") or SCREENPLAY_VERSION),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "canvas": self.canvas.to_dict(),
            "fps": self.fps,
            "scenes": [scene.to_dict() for scene in self.scenes],
        }
        if self.cast:
            out["cast"] = dict(self.cast)
        if self.material_dir:
            out["material_dir"] = self.material_dir
        if self.vo_dir:
            out["vo_dir"] = self.vo_dir
        if self.music:
            out["music"] = dict(self.music)
        if self.styles:
            out["styles"] = {role: style.to_dict() for role, style in self.styles.items()}
        out.update(self.extra)
        return out

    # -- 便利 --------------------------------------------------------------- #
    def lines(self) -> Iterable[Tuple[Scene, Line]]:
        for scene in self.scenes:
            for line in scene.lines:
                yield scene, line

    def speaker_name(self, who: str) -> str:
        return self.cast.get(who, who)

    def resolve_vo(self, relative: str) -> str:
        if not relative:
            return ""
        if os.path.isabs(relative):
            return relative
        base = self.base_dir or "."
        return os.path.normpath(os.path.join(base, self.vo_dir or ".", relative))


# --------------------------------------------------------------------------- #
# 读写与校验
# --------------------------------------------------------------------------- #
def load_screenplay(path: str) -> Screenplay:
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    screenplay = load_screenplay_str(text)
    screenplay.base_dir = os.path.dirname(os.path.abspath(path))
    return screenplay


def load_screenplay_str(text: str) -> Screenplay:
    return Screenplay.from_dict(json.loads(text))


def validate_screenplay(screenplay: Screenplay,
                        check_files: bool = False) -> List[Issue]:
    """查结构问题。错误码 S0xx——和分镜表的 E0xx 分开，方便一眼看出是哪一层。"""
    issues: List[Issue] = []

    if not screenplay.scenes:
        issues.append(Issue(ERROR, "S001", "剧本里一场戏都没有", "scenes"))
    if screenplay.canvas.width <= 0 or screenplay.canvas.height <= 0:
        issues.append(Issue(ERROR, "S002",
                            f"画布尺寸非法: {screenplay.canvas.width}x"
                            f"{screenplay.canvas.height}", "canvas"))
    if not (1 <= float(screenplay.fps or 0) <= 240):
        issues.append(Issue(ERROR, "S003", f"fps 非法: {screenplay.fps}", "fps"))

    seen_ids: Dict[str, int] = {}
    for index, scene in enumerate(screenplay.scenes):
        where = f"scenes[{index}]"
        if not scene.id:
            issues.append(Issue(WARN, "S010", "场次没写 id，报告里不好指认", where))
        elif scene.id in seen_ids:
            issues.append(Issue(ERROR, "S011",
                                f"场次 id 重复: {scene.id}（第 {seen_ids[scene.id]} 场已经用过）",
                                where))
        else:
            seen_ids[scene.id] = index
        if not scene.lines:
            issues.append(Issue(WARN, "S012", f"第 {scene.id or index} 场没有台词/动作", where))
        if scene.duration is not None and scene.duration <= 0:
            issues.append(Issue(ERROR, "S013", "场次目标时长必须大于 0", where))

        for line_index, line in enumerate(scene.lines):
            lwhere = f"{where}.lines[{line_index}]"
            if line.type not in LINE_TYPES:
                issues.append(Issue(ERROR, "S020",
                                    f"未知的行类型 {line.type!r}，只支持 "
                                    f"{'/'.join(LINE_TYPES)}", lwhere))
            if line.type != "title" and not line.text:
                issues.append(Issue(ERROR, "S021", "空台词：这一行没有 text", lwhere))
            if line.duration is not None and line.duration <= 0:
                issues.append(Issue(ERROR, "S022", "行时长必须大于 0", lwhere))
            if line.who and screenplay.cast and line.who not in screenplay.cast:
                issues.append(Issue(WARN, "S023",
                                    f"人物 {line.who!r} 不在 cast 里，字幕会直接用这个代号",
                                    lwhere))
            if check_files and line.vocal:
                path = screenplay.resolve_vo(line.vocal)
                if not os.path.isfile(path):
                    issues.append(Issue(ERROR, "S024", f"旁白音频不存在: {path}", lwhere))

    music_file = str(screenplay.music.get("file") or "")
    if check_files and music_file:
        path = music_file if os.path.isabs(music_file) else os.path.join(
            screenplay.base_dir or ".", music_file)
        if not os.path.isfile(path):
            issues.append(Issue(ERROR, "S030", f"配乐不存在: {path}", "music.file"))

    return issues


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(issue.level == ERROR for issue in issues)
