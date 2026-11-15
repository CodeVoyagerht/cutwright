"""剪映 11.6 草稿内容（``draft_content``）的生成与自检。

这个模块是**纯函数**：只把 :class:`cutwright.timeline.Timeline` 翻译成一个
dict，不读磁盘、不调 ffprobe、不碰草稿目录。素材的宽高与源时长由调用方通过
``media=`` 注入（键是素材文件的绝对路径），拿不到时退化成画布尺寸/时间线时长。

时间单位
--------
剪映草稿里所有时间都是**整数微秒**。本项目内部一律用秒，
:func:`us` 是唯一的换算点：``us(2.02) == 2020000``。

坐标单位
--------
``clip.transform.x/y`` 的单位是**半个画布**：``±1`` 正好到画布边缘，
``y`` 为负数表示**向下**。

轨道顺序
--------
``tracks`` 数组越靠后越靠上层。本模块固定输出::

    [0] 主视频轨            type=video
    [1] 图片叠加轨(flag=2)  type=video
    [2] caption             type=text
    [3] title               type=text
    [4] huazi               type=text
    [5] end                 type=text
    [6] vo_hidden           type=text   ← 旁白，不可见
    [7..] 音频轨            type=audio

每个文字轨的 ``track_render_index`` 就是它在 ``tracks`` 里的下标，所以必须等
整张轨道表拼好之后再回填（见 :meth:`_DraftBuilder.build`）。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...timeline import (
    Timeline,
    TimelineAudio,
    TimelineClip,
    TimelineOverlay,
    TimelineText,
)

__all__ = [
    "VERSION",
    "NEW_VERSION",
    "CHECK_FLAG_VIDEO",
    "TEXT_RENDER_INDEX",
    "STICKER_ANIMATION_TYPE",
    "PHOTO_DURATION_SENTINEL",
    "STYLE_TRACK_ORDER",
    "PLATFORM",
    "build_content",
    "build_style_specs",
    "validate_content",
    "us",
]

# --------------------------------------------------------------------------- #
# 常量：剪映 11.6 草稿格式
# --------------------------------------------------------------------------- #

#: ``draft_content.version``，11.6 实测值。
VERSION = 360000

#: ``draft_content.new_version``，11.6 实测值。
NEW_VERSION = "189.0.0"

#: 视频素材的 ``check_flag``（11.6 的固定值，改动会导致素材不被识别）。
CHECK_FLAG_VIDEO = 62978047

#: 文字片段的 ``render_index``（画在特效/贴纸之上）。
TEXT_RENDER_INDEX = 14000

#: 文字片段引用的动画素材类型。
STICKER_ANIMATION_TYPE = "sticker_animation"

#: 图片素材的 ``duration`` 占位值（约 3 小时），真实时长由片段 target 决定。
PHOTO_DURATION_SENTINEL = 10800000000

#: 文字轨的固定出现顺序（与分镜表的 style 名对应）。
STYLE_TRACK_ORDER: Tuple[str, ...] = ("caption", "title", "huazi", "end", "vo_hidden")

#: 字号/描边的换算标定值：1080 高画布下，1 个剪映字号单位 = 3.06 px。
CALIB_PX_PER_SIZE_UNIT = 3.06

#: 1080 高画布下，1 个描边宽度单位 = 39.06 px。
CALIB_STROKE_PX_PER_UNIT = 39.06

#: ``platform`` / ``last_modified_platform`` 的本机剪映标识。
#: 这几个字段是「原样照抄本机剪映值即可」的机器级信息，与具体项目无关。
PLATFORM: Dict[str, Any] = {
    "os": "windows",
    "os_version": "10.0.28120",
    "app_id": 3704,
    "app_version": "11.6.0",
    "app_source": "lv",
    "device_id": "b526a315e05d409ca53656ca1ccb92a3",
    "mac_address": "60df8802e39cf28c3e142a637b950742",
}

#: ``materials`` 下的类别，顺序即序列化顺序（固定，便于 diff）。
MATERIAL_KEYS: Tuple[str, ...] = (
    "videos", "texts", "material_animations", "speeds", "placeholder_infos",
    "canvases", "sound_channel_mappings", "material_colors",
    "vocal_separations", "audios",
)

#: 轨道类型 → 该轨片段 ``material_id`` 必须落在哪个素材类别里。
_SEG_CATEGORY: Dict[str, Tuple[str, ...]] = {
    "video": ("videos",),
    "text": ("texts",),
    "audio": ("audios",),
}

#: 微秒吸附容差（片段相邻误差在这个范围内视为连续）。
_SNAP_US = 5000


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def uid() -> str:
    """生成剪映习惯的大写 UUID 字符串。"""
    return str(uuid.uuid4()).upper()


def us(seconds: float) -> int:
    """秒 → 整数微秒。这是全模块唯一的单位换算点。"""
    return int(round(float(seconds) * 1_000_000))


def posix(path: str) -> str:
    """把 Windows 路径统一成正斜杠（草稿里所有素材路径都用正斜杠）。"""
    return str(path).replace("\\", "/")


def _abspath(path: str) -> str:
    """规范化素材路径，用作 media 映射的键。"""
    try:
        import os

        return os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):  # pragma: no cover - 极端路径
        return str(path)


def hex_to_rgb01(value: Any, default: Tuple[float, float, float] = (1.0, 1.0, 1.0)
                 ) -> List[float]:
    """``#RRGGBB`` / ``#AARRGGBB`` → ``[r, g, b]``（0~1 浮点）。

    解析不了就返回 ``default``，绝不抛异常——一个写错的颜色不该让整个草稿生成失败。
    """
    if not isinstance(value, str):
        return list(default)
    text = value.strip().lstrip("#")
    if len(text) == 8:  # AARRGGBB，丢掉 alpha
        text = text[2:]
    if len(text) != 6:
        return list(default)
    try:
        return [round(int(text[i:i + 2], 16) / 255.0, 6) for i in (0, 2, 4)]
    except ValueError:
        return list(default)


def _assign_tracks(items: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """把带 ``start`` / ``dur``（微秒）的条目贪心分配到互不重叠的轨上。

    同一条轨内的片段不能重叠，否则剪映会丢段。返回每条轨的条目列表，
    轨内按开始时间升序。
    """
    ordered = sorted(items, key=lambda it: (it["start"], it["dur"]))
    tracks: List[Dict[str, Any]] = []
    for item in ordered:
        placed = False
        for lane in tracks:
            if item["start"] >= lane["end"]:
                lane["items"].append(item)
                lane["end"] = item["start"] + item["dur"]
                placed = True
                break
        if not placed:
            tracks.append({"end": item["start"] + item["dur"], "items": [item]})
    return [lane["items"] for lane in tracks]


# --------------------------------------------------------------------------- #
# 文字样式：分镜表的像素值 → 剪映 text 素材参数
# --------------------------------------------------------------------------- #

def _hidden_spec(name: str, role: str = "") -> Dict[str, Any]:
    """不可见文字（旁白）的样式：缩到 10 号并移到画布外下方。"""
    return {
        "name": name, "role": role, "invisible": True,
        "size": 10.0, "color": [1.0, 1.0, 1.0], "color_hex": "#FFFFFF",
        "keyword_color": None, "stroke": None,
        "x": 0.0, "y": -2.0, "rotation": 0.0, "scale": 1.0, "bold": False,
        "align": "center", "anim": "none", "font_height_px": 0.0,
    }


def _style_spec(name: str, style: Any, canvas: Any, font_scale: float = 1.0
                ) -> Dict[str, Any]:
    """把一条 :class:`~cutwright.schema.TextStyle` 换算成剪映参数。

    ``y`` 的两个公式都基于「画布中心为 0、向上为正、单位是半个画布高」：

    * 给了 ``bottom_margin_px`` → 文字块底边离画布底部的像素距离；
    * 给了 ``center_ratio_y``  → 文字中心在画布高度上的比例；
    * 都没给 → ``y = 0``（正中间）。
    """
    role = str(getattr(style, "role", "") or "")
    if getattr(style, "invisible", False) or name == "vo_hidden":
        return _hidden_spec(name, role)

    width = float(getattr(canvas, "width", 1080) or 1080)
    height = float(getattr(canvas, "height", 1920) or 1920)
    size_scale = height / 1080.0

    font_height = float(getattr(style, "font_height_px", 100.0) or 100.0)
    size = font_height / (CALIB_PX_PER_SIZE_UNIT * size_scale) * float(font_scale or 1.0)

    bottom_margin = getattr(style, "bottom_margin_px", None)
    center_ratio = getattr(style, "center_ratio_y", None)
    if bottom_margin is not None:
        center_from_bottom = float(bottom_margin) + font_height / 2.0
        y = -((height / 2.0 - center_from_bottom) / (height / 2.0))
    elif center_ratio is not None:
        center_from_top = float(center_ratio) * height
        y = -((center_from_top - height / 2.0) / (height / 2.0))
    else:
        y = 0.0

    stroke_px = float(getattr(style, "stroke_width_px", 0.0) or 0.0)
    stroke = None
    if stroke_px > 0:
        stroke = {
            "width": round(stroke_px / (CALIB_STROKE_PX_PER_UNIT * size_scale), 4),
            "color": getattr(style, "stroke_color", "#000000") or "#000000",
        }

    color_hex = getattr(style, "color", "#FFFFFF") or "#FFFFFF"
    keyword_hex = getattr(style, "keyword_color", None)
    return {
        "name": name,
        "role": role,
        "invisible": False,
        "size": round(size, 2),
        "color": hex_to_rgb01(color_hex),
        "color_hex": color_hex,
        "keyword_color": hex_to_rgb01(keyword_hex) if keyword_hex else None,
        "stroke": stroke,
        "x": 0.0,
        "y": round(y, 4),
        "rotation": float(getattr(style, "rotation_deg", 0.0) or 0.0),
        "scale": 1.0,
        "bold": name in ("title", "huazi", "end"),
        "align": getattr(style, "align", "center") or "center",
        "anim": getattr(style, "anim", "none") or "none",
        "font_height_px": font_height,
        "_width": width,
    }


def build_style_specs(timeline: Timeline, *, font_scale: float = 1.0
                      ) -> Dict[str, Dict[str, Any]]:
    """把时间线上用到的所有文字样式换算成剪映参数。

    以 ``timeline.styles`` 为准；某条文字引用了表里没有的样式名时，用它自带的
    :class:`~cutwright.schema.TextStyle` 兜底，保证不会出现「引用了不存在的样式」。
    """
    styles: Dict[str, Any] = dict(getattr(timeline, "styles", None) or {})
    canvas = getattr(timeline, "canvas", None)
    names: List[str] = list(styles.keys())
    for text in getattr(timeline, "texts", None) or []:
        name = getattr(text, "style_name", "") or "caption"
        if name not in styles:
            styles[name] = getattr(text, "style", None)
            names.append(name)
    return {name: _style_spec(name, styles.get(name), canvas, font_scale)
            for name in names}


# --------------------------------------------------------------------------- #
# 素材与片段
# --------------------------------------------------------------------------- #

def _video_material(mid: str, path: str, duration_us: int, width: int, height: int) -> Dict[str, Any]:
    """一条视频素材。``duration`` 是**素材全时长**，不是片段时长。"""
    return {
        "id": mid,
        "type": "video",
        "duration": int(duration_us),
        "path": posix(path),
        "width": int(width),
        "height": int(height),
        "material_id": "",
        "crop": {},
        "stable": {"time_range": {}},
        "matting": {"path": ""},
        "check_flag": CHECK_FLAG_VIDEO,
        "video_algorithm": {"path": "", "story_video_modify_video_config": {}},
        "is_copyright": True,
        "beauty_face_auto_preset": {},
        "video_mask_stroke": {"resource_id": "", "path": "", "type": ""},
        "video_mask_shadow": {"resource_id": "", "path": ""},
    }


def _photo_material(mid: str, path: str, width: int, height: int) -> Dict[str, Any]:
    """一条图片素材（仍然放在 ``materials.videos`` 里，``type`` 为 ``photo``）。"""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return {
        "id": mid,
        "type": "photo",
        "duration": PHOTO_DURATION_SENTINEL,
        "path": posix(path),
        "has_audio": False,
        "width": int(width),
        "height": int(height),
        "material_id": "",
        "material_name": name,
        "crop": {},
        "stable": {"time_range": {}},
        "matting": {"path": ""},
        "check_flag": CHECK_FLAG_VIDEO,
        "video_algorithm": {"path": "", "story_video_modify_video_config": {}},
        "beauty_face_auto_preset": {},
        "video_mask_stroke": {"resource_id": "", "path": "", "type": ""},
        "video_mask_shadow": {"resource_id": "", "path": ""},
    }


def _audio_material(mid: str, path: str, duration_us: int) -> Dict[str, Any]:
    """一条本地音频素材。"""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
    return {
        "id": mid,
        "type": "music",
        "name": name,
        "duration": int(duration_us),
        "path": posix(path),
        "category_name": "local",
        "music_id": "",
        "app_id": 0,
        "resource_id": "",
        "category_id": "",
        "music_source": "local",
        "similiar_music_info": {},
        "tts_benefit_info": {"benefit_type": "none"},
    }


def _text_content_json(text: str, spec: Mapping[str, Any], font_mode: str,
                       font: Optional[Mapping[str, Any]]) -> Tuple[str, str]:
    """生成富文本 ``content`` 字符串，返回 ``(JSON 文本, 文本内容)``。

    约定：用 ``|`` 分段，**偶数段**用正文色、**奇数段**用 ``keyword_color``；
    ``range`` 按拆掉 ``|`` 之后的字符下标计算（不是字节，也不是 UTF-16）。
    """
    pieces = [p for p in str(text).split("|") if p]
    display = "".join(pieces)
    styles: List[Dict[str, Any]] = []
    pos = 0
    stroke = spec.get("stroke")
    for i, piece in enumerate(pieces):
        is_keyword = (i % 2 == 1) and spec.get("keyword_color") is not None
        color = spec["keyword_color"] if is_keyword else spec["color"]
        part: Dict[str, Any] = {
            "fill": {"content": {"render_type": "solid",
                                 "solid": {"color": list(color or [1.0, 1.0, 1.0])}}},
            "size": spec.get("size", 13.0),
            "range": [pos, pos + len(piece)],
        }
        if spec.get("bold"):
            part["bold"] = True
        if stroke:
            part["strokes"] = [{
                "content": {"solid": {"alpha": 1.0,
                                      "color": hex_to_rgb01(stroke.get("color"), (0.0, 0.0, 0.0))}},
                "width": stroke.get("width", 0.0),
            }]
        if font_mode == "native" and font and font.get("path"):
            part["font"] = {"path": font.get("path"),
                            "id": font.get("resource_id") or ""}
        styles.append(part)
        pos += len(piece)
    payload = json.dumps({"text": display, "styles": styles},
                         ensure_ascii=False, separators=(",", ":"))
    return payload, display


def _text_material(mid: str, text: str, spec: Mapping[str, Any], font_mode: str,
                   font: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """一条文字素材（23 个基础字段 + 可选的字体字段）。"""
    content, _ = _text_content_json(text, spec, font_mode, font)
    stroke = spec.get("stroke") or {}
    material: Dict[str, Any] = {
        "id": mid,
        "type": "text",
        "content": content,
        "words": {},
        "current_words": {},
        "combo_info": {},
        "caption_template_info": {"resource_id": "", "path": ""},
        "lyrics_template": {"resource_id": "", "path": ""},
        "layer_weight": 1,
        "line_spacing": 0.02,
        "border_width": float(stroke.get("width", 0.0) or 0.0),
        "text_color": spec.get("color_hex", "#FFFFFF"),
        "shadow_alpha": 0.9,
        "shadow_smoothing": 0.45,
        "shadow_distance": 5.0,
        "shadow_point": {"x": 0.6363961030678928, "y": -0.6363961030678928},
        "shadow_angle": -45.0,
        "initial_scale": 1.0,
        "force_apply_line_max_width": True,
    }
    if font_mode == "native" and font and font.get("path"):
        material.update({
            "font_source_platform": int(font.get("source_platform", 1) or 1),
            "font_path": font.get("path"),
            "font_resource_id": str(font.get("resource_id") or ""),
            "font_title": font.get("title") or "",
            "fonts": [{
                "id": font.get("id") or uid(),
                "resource_id": str(font.get("resource_id") or ""),
                "category_id": "text_search",
                "category_name": "剪映预设",
                "source_platform": int(font.get("source_platform", 1) or 1),
                "path": font.get("path"),
                "effect_id": font.get("effect_id") or "",
                "title": font.get("title") or "",
                "request_id": font.get("request_id") or "",
            }],
        })
    return material


# --------------------------------------------------------------------------- #
# 构建器
# --------------------------------------------------------------------------- #

class _DraftBuilder:
    """把一条 :class:`Timeline` 累积成 ``draft_content``。"""

    def __init__(self, timeline: Timeline, *, version: int = VERSION,
                 media: Optional[Mapping[str, Any]] = None,
                 font_scale: float = 1.0, font_mode: str = "none",
                 font: Optional[Mapping[str, Any]] = None,
                 platform: Optional[Mapping[str, Any]] = None,
                 warnings: Optional[List[str]] = None) -> None:
        self.timeline = timeline
        self.version = int(version)
        self.media = {_abspath(k): v for k, v in (media or {}).items()}
        self.font_scale = float(font_scale or 1.0)
        self.font_mode = font_mode or "none"
        self.font = dict(font) if font else None
        self.platform = dict(platform or PLATFORM)
        self.warnings: List[str] = warnings if warnings is not None else []

        canvas = getattr(timeline, "canvas", None)
        self.width = int(getattr(canvas, "width", 1080) or 1080)
        self.height = int(getattr(canvas, "height", 1920) or 1920)

        self.materials: Dict[str, List[Dict[str, Any]]] = {k: [] for k in MATERIAL_KEYS}
        self.specs = build_style_specs(timeline, font_scale=self.font_scale)
        self._video_material_by_path: Dict[str, str] = {}

    # -- 素材登记 ---------------------------------------------------------- #
    def _add(self, category: str, material: Dict[str, Any]) -> str:
        """把一条素材放进对应类别，返回它的 id。"""
        self.materials[category].append(material)
        return material["id"]

    def _add_video_aux(self, speed: float) -> List[str]:
        """每个视频/图片片段独享的一整套辅助素材，返回 ``extra_material_refs``。"""
        speed_material: Dict[str, Any] = {"id": uid(), "type": "speed"}
        if abs(float(speed) - 1.0) > 1e-9:
            speed_material.update({"speed": float(speed), "mode": 0, "curve_speed": None})
        speed_id = self._add("speeds", speed_material)
        placeholder_id = self._add(
            "placeholder_infos",
            {"id": uid(), "type": "placeholder_info", "meta_type": "none"})
        canvas_id = self._add("canvases", {"id": uid(), "type": "canvas_color"})
        mapping_id = self._add("sound_channel_mappings", {"id": uid(), "type": ""})
        color_id = self._add("material_colors", {"id": uid()})
        vocal_id = self._add("vocal_separations", {"id": uid(), "type": "vocal_separation"})
        return [speed_id, placeholder_id, canvas_id, mapping_id, color_id, vocal_id]

    def _add_audio_aux(self) -> List[str]:
        """音频片段引用的辅助素材（只有 4 类，顺序与视频段不同）。"""
        speed_id = self._add("speeds", {"id": uid(), "type": "speed"})
        mapping_id = self._add("sound_channel_mappings", {"id": uid(), "type": ""})
        vocal_id = self._add("vocal_separations", {"id": uid(), "type": "vocal_separation"})
        placeholder_id = self._add(
            "placeholder_infos",
            {"id": uid(), "type": "placeholder_info", "meta_type": "none"})
        return [speed_id, mapping_id, vocal_id, placeholder_id]

    def _media_info(self, path: str) -> Dict[str, Any]:
        """取调用方注入的素材信息（可能为空 dict）。"""
        return dict(self.media.get(_abspath(path)) or {})

    # -- 片段 -------------------------------------------------------------- #
    def _video_segment(self, material_id: str, refs: List[str], *,
                       src_start_us: int, src_dur_us: int,
                       tl_start_us: int, tl_dur_us: int,
                       speed: float = 1.0, hdr: bool = False,
                       render_index: Optional[int] = None,
                       track_render_index: Optional[int] = None) -> Dict[str, Any]:
        """一段主视频/图片叠加片段。"""
        segment: Dict[str, Any] = {
            "id": uid(),
            "source_timerange": {"start": int(src_start_us), "duration": int(src_dur_us)},
            "target_timerange": {"start": int(tl_start_us), "duration": int(tl_dur_us)},
            "render_timerange": {},
            "clip": {"scale": {"x": 1.0, "y": 1.0},
                     "transform": {"x": 0.0, "y": 0.0},
                     "flip": {}},
            "uniform_scale": {},
            "material_id": material_id,
            "extra_material_refs": list(refs),
            "enable_hsl": False,
            "responsive_layout": {},
            "enable_adjust_mask": False,
            "source": "segmentsourcenormal",
        }
        if abs(float(speed) - 1.0) > 1e-9:
            segment["speed"] = float(speed)
        if hdr:
            segment["hdr_settings"] = {"mode": 1}
        if render_index is not None:
            segment["render_index"] = int(render_index)
        if track_render_index is not None:
            segment["track_render_index"] = int(track_render_index)
        return segment

    def _text_segment(self, material_id: str, anim_id: str,
                      spec: Mapping[str, Any], *, tl_start_us: int,
                      tl_dur_us: int) -> Dict[str, Any]:
        """一段文字片段。文字段**没有** ``source_timerange``。"""
        scale = float(spec.get("scale", 1.0) or 1.0)
        transform = {"x": float(spec.get("x", 0.0) or 0.0),
                     "y": float(spec.get("y", 0.0) or 0.0)}
        clip: Dict[str, Any] = {
            "scale": {"x": scale, "y": scale},
            "transform": transform,
            "flip": {},
        }
        rotation = float(spec.get("rotation", 0.0) or 0.0)
        if abs(rotation) > 1e-9:
            clip["rotation"] = rotation
        return {
            "id": uid(),
            "target_timerange": {"start": int(tl_start_us), "duration": int(tl_dur_us)},
            "render_timerange": {},
            "clip": clip,
            "uniform_scale": {},
            "material_id": material_id,
            "extra_material_refs": [anim_id],
            "render_index": TEXT_RENDER_INDEX,
            "enable_lut": False,
            "enable_adjust": False,
            "enable_hsl": False,
            "track_render_index": 0,  # 拼好轨道表后由 build() 回填
            "responsive_layout": {},
            "enable_adjust_mask": False,
            "source": "segmentsourcenormal",
        }

    def _audio_segment(self, material_id: str, refs: List[str], *,
                       src_start_us: int, src_dur_us: int,
                       tl_start_us: int, tl_dur_us: int) -> Dict[str, Any]:
        """一段音频片段。"""
        return {
            "id": uid(),
            "source_timerange": {"start": int(src_start_us), "duration": int(src_dur_us)},
            "target_timerange": {"start": int(tl_start_us), "duration": int(tl_dur_us)},
            "render_timerange": {},
            "clip": {"scale": {"x": 1.0, "y": 1.0},
                     "transform": {"x": 0.0, "y": 0.0},
                     "flip": {}},
            "uniform_scale": {},
            "material_id": material_id,
            "extra_material_refs": list(refs),
            "enable_lut": False,
            "enable_adjust": False,
            "enable_hsl": False,
            "track_render_index": 1,
            "responsive_layout": {},
            "enable_adjust_mask": False,
            "source": "segmentsourcenormal",
        }

    # -- 轨道 -------------------------------------------------------------- #
    def _video_track(self) -> Dict[str, Any]:
        """主视频轨：分镜表里每一段视频一条 segment，按时间线顺序首尾相接。"""
        clips = sorted(getattr(self.timeline, "clips", None) or [],
                       key=lambda c: (float(c.tl_start), int(c.index)))
        segments: List[Dict[str, Any]] = []
        prev_end = 0
        for pos, clip in enumerate(clips):
            info = self._media_info(clip.source)
            src_start_us = us(clip.src_in)
            src_dur_us = us(clip.src_dur)
            tl_start_us = us(clip.tl_start)
            tl_dur_us = us(clip.tl_dur)

            # 与上一段之间的空隙：极小的误差吸附掉，大的留 warning。
            gap = tl_start_us - prev_end
            if pos > 0 and 0 < gap <= _SNAP_US:
                tl_start_us = prev_end
                gap = 0
            if pos > 0 and gap > _SNAP_US:
                self.warnings.append(
                    "主视频轨第 %d 段与上一段之间有 %.3fs 空隙（剪映会显示黑场）"
                    % (pos + 1, gap / 1_000_000.0))

            media_dur = int(info.get("duration_us") or 0)
            if clip.still:
                # 静帧：图片素材没有源区间可裁，剪映按整张图铺 target 时长。
                src_start_us, src_dur_us = 0, tl_dur_us
            elif media_dur and src_start_us + src_dur_us > media_dur:
                trimmed = max(media_dur - src_start_us, 0)
                self.warnings.append(
                    "素材 %s 只有 %.3fs，第 %d 段的源区间被截断到 %.3fs"
                    % (posix(clip.source).rsplit("/", 1)[-1], media_dur / 1e6,
                       pos + 1, trimmed / 1e6))
                src_dur_us = trimmed
            if src_dur_us <= 0 or tl_dur_us <= 0:
                self.warnings.append("第 %d 段时长为 0，已跳过" % (pos + 1))
                continue

            key = _abspath(clip.source)
            material_id = self._video_material_by_path.get(key)
            if material_id is None:
                width = int(info.get("width") or self.width)
                height = int(info.get("height") or self.height)
                if not info.get("width"):
                    self.warnings.append(
                        "未探测到 %s 的尺寸，宽高按画布 %dx%d 处理（未验证）"
                        % (posix(clip.source).rsplit("/", 1)[-1], self.width, self.height))
                if clip.still:
                    material_id = self._add(
                        "videos", _photo_material(uid(), clip.source, width, height))
                else:
                    duration = media_dur or max(us(clip.src_out), src_start_us + src_dur_us)
                    material_id = self._add(
                        "videos",
                        _video_material(uid(), clip.source, duration, width, height))
                self._video_material_by_path[key] = material_id

            refs = self._add_video_aux(float(clip.speed or 1.0))
            segments.append(self._video_segment(
                material_id, refs,
                src_start_us=src_start_us, src_dur_us=src_dur_us,
                tl_start_us=tl_start_us, tl_dur_us=tl_dur_us,
                speed=1.0 if clip.still else float(clip.speed or 1.0),
                hdr=False if clip.still else bool(info.get("hdr")),
            ))
            prev_end = tl_start_us + tl_dur_us

        if not segments:
            self.warnings.append("时间线上没有视频片段，主视频轨为空")
        return {"id": uid(), "type": "video", "is_default_name": True,
                "segments": segments}

    def _overlay_tracks(self) -> List[Dict[str, Any]]:
        """图片叠加轨（画中画）：单独一条 video 轨，``flag = 2``。"""
        overlays = sorted(getattr(self.timeline, "overlays", None) or [],
                          key=lambda o: (float(o.tl_start), int(o.index)))
        if not overlays:
            return []
        items: List[Dict[str, Any]] = []
        for overlay in overlays:
            info = self._media_info(overlay.image)
            width = int(info.get("width") or self.width)
            height = int(info.get("height") or self.height)
            material_id = self._add("videos", _photo_material(uid(), overlay.image, width, height))
            refs = self._add_video_aux(1.0)
            tl_start_us = us(overlay.tl_start)
            tl_dur_us = us(overlay.tl_dur)
            if tl_dur_us <= 0:
                self.warnings.append("叠加层 %s 时长为 0，已跳过"
                                     % posix(overlay.image).rsplit("/", 1)[-1])
                continue
            # 图片片段的 source_timerange 只写 duration，让剪映按整张图处理。
            segment = self._video_segment(
                material_id, refs, src_start_us=0, src_dur_us=0,
                tl_start_us=tl_start_us, tl_dur_us=tl_dur_us,
                render_index=1, track_render_index=1,
            )
            segment["source_timerange"] = {"duration": int(tl_dur_us)}
            items.append({"start": tl_start_us, "dur": tl_dur_us, "seg": segment})

        if items and any((o.fade or o.slide_px or o.float_amp)
                         for o in overlays):
            self.warnings.append(
                "叠加层的淡入/滑动/悬浮动效尚未映射成剪映关键帧（未验证），"
                "草稿里按静态图片写入")
        return [{"id": uid(), "type": "video", "is_default_name": True,
                 "flag": 2, "segments": [it["seg"] for it in lane]}
                for lane in _assign_tracks(items)]

    def _spec_for(self, text: TimelineText) -> Dict[str, Any]:
        """取一条文字该用的样式；旁白（``role == "vo"``）一律不可见。"""
        name = getattr(text, "style_name", "") or "caption"
        if (getattr(text, "role", "") or "") == "vo":
            return _hidden_spec(name, "vo")
        spec = self.specs.get(name)
        if spec is None:
            spec = _style_spec(name, getattr(text, "style", None),
                               getattr(self.timeline, "canvas", None), self.font_scale)
            self.specs[name] = spec
        if spec.get("invisible"):
            return _hidden_spec(name, spec.get("role", ""))
        return spec

    def _text_tracks(self) -> List[Dict[str, Any]]:
        """文字轨：先按固定顺序 caption/title/huazi/end/vo_hidden，再补其它样式。"""
        groups: Dict[str, List[TimelineText]] = {}
        for text in getattr(self.timeline, "texts", None) or []:
            name = (getattr(text, "style_name", "") or "caption")
            if (getattr(text, "role", "") or "") == "vo":
                name = "vo_hidden"
            groups.setdefault(name, []).append(text)
        ordered = [n for n in STYLE_TRACK_ORDER if n in groups]
        ordered += [n for n in groups if n not in ordered]

        tracks: List[Dict[str, Any]] = []
        for name in ordered:
            items: List[Dict[str, Any]] = []
            for text in groups[name]:
                spec = self._spec_for(text)
                material_id = self._add(
                    "texts", _text_material(uid(), text.text, spec, self.font_mode, self.font))
                anim_id = self._add("material_animations",
                                    {"id": uid(), "type": STICKER_ANIMATION_TYPE})
                tl_dur_us = us(text.tl_dur)
                if tl_dur_us <= 0:
                    self.warnings.append("文字 %r 时长为 0，已跳过"
                                         % str(text.text)[:20])
                    continue
                items.append({
                    "start": us(text.tl_start),
                    "dur": tl_dur_us,
                    "seg": self._text_segment(material_id, anim_id, spec,
                                              tl_start_us=us(text.tl_start),
                                              tl_dur_us=tl_dur_us),
                })
            for lane in _assign_tracks(items):
                tracks.append({"id": uid(), "type": "text", "is_default_name": True,
                               "segments": [it["seg"] for it in lane]})
        return tracks

    def _audio_tracks(self) -> List[Dict[str, Any]]:
        """音频轨：按时间贪心分轨，重叠的音频不会互相覆盖。"""
        audios = sorted(getattr(self.timeline, "audios", None) or [],
                        key=lambda a: (float(a.tl_start), int(a.index)))
        items: List[Dict[str, Any]] = []
        for audio in audios:
            info = self._media_info(audio.source)
            tl_dur_us = us(audio.tl_dur)
            if tl_dur_us <= 0:
                self.warnings.append("音频 %s 时长为 0，已跳过"
                                     % posix(audio.source).rsplit("/", 1)[-1])
                continue
            src_start_us = us(audio.src_in)
            src_dur_us = us(audio.tl_dur)
            duration = int(info.get("duration_us") or max(src_start_us + src_dur_us, 0))
            material_id = self._add("audios", _audio_material(uid(), audio.source, duration))
            refs = self._add_audio_aux()
            items.append({
                "start": us(audio.tl_start),
                "dur": tl_dur_us,
                "seg": self._audio_segment(material_id, refs,
                                           src_start_us=src_start_us,
                                           src_dur_us=src_dur_us,
                                           tl_start_us=us(audio.tl_start),
                                           tl_dur_us=tl_dur_us),
            })
        return [{"id": uid(), "type": "audio", "is_default_name": True,
                 "segments": [it["seg"] for it in lane]}
                for lane in _assign_tracks(items)]

    # -- 汇总 -------------------------------------------------------------- #
    def build(self) -> Dict[str, Any]:
        """拼出完整的 ``draft_content``。"""
        tracks: List[Dict[str, Any]] = [self._video_track()]
        tracks += self._overlay_tracks()
        text_start = len(tracks)
        tracks += self._text_tracks()
        tracks += self._audio_tracks()

        # 文字轨的 track_render_index = 它在 tracks 数组里的下标。
        for index in range(text_start, len(tracks)):
            track = tracks[index]
            if track.get("type") != "text":
                continue
            for segment in track["segments"]:
                segment["track_render_index"] = index

        duration = 0
        for track in tracks:
            for segment in track.get("segments") or []:
                timerange = segment.get("target_timerange") or {}
                end = int(timerange.get("start", 0)) + int(timerange.get("duration", 0))
                duration = max(duration, end)
        duration = max(duration, us(getattr(self.timeline, "duration", 0.0) or 0.0))

        return {
            "id": uid(),
            "version": self.version,
            "new_version": NEW_VERSION,
            "duration": int(duration),
            "config": {},
            "canvas_config": {"width": self.width, "height": self.height},
            "tracks": tracks,
            "materials": {k: v for k, v in self.materials.items() if v},
            "keyframes": {},
            "platform": dict(self.platform),
            "last_modified_platform": dict(self.platform),
            "render_index_track_mode_on": True,
            "path": "",
            "uneven_animation_template_info": {},
            "smart_ads_info": {},
            "function_assistant_info": {"fps": {}},
        }


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #

def build_content(timeline: Timeline, *, version: int = VERSION,
                  media: Optional[Mapping[str, Any]] = None,
                  font_scale: float = 1.0, font_mode: str = "none",
                  font: Optional[Mapping[str, Any]] = None,
                  platform: Optional[Mapping[str, Any]] = None,
                  warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """把 :class:`Timeline` 变成剪映的 ``draft_content`` dict。

    参数
    ----
    version
        写进 ``draft_content.version`` 的格式版本，默认 :data:`VERSION`（11.6）。
    media
        可选的素材信息映射，键是素材路径，值至少可含 ``width`` / ``height`` /
        ``duration_us`` / ``hdr``。拿不到时宽高退化成画布、源时长退化成
        ``src_out``（并在 ``warnings`` 里记一条）。
    font_mode
        ``"none"`` 不写字体字段（让剪映用默认字体）；``"native"`` 写
        ``font_path`` 等字段，需要同时给 ``font``。
    font
        字体描述 ``{"path", "resource_id", "title", ...}``，跨机器会失效，
        所以由调用方决定给不给。
    warnings
        传入一个 list 时会把构建过程中的提醒追加进去（不改变返回值结构）。

    返回的 dict 可以直接写盘，也可以先过一遍 :func:`validate_content`。
    """
    builder = _DraftBuilder(
        timeline, version=version, media=media, font_scale=font_scale,
        font_mode=font_mode, font=font, platform=platform, warnings=warnings,
    )
    return builder.build()


def validate_content(content: Mapping[str, Any]) -> Dict[str, Any]:
    """自检一份 ``draft_content``，返回 ``{ok, errors, warnings, stats}``。

    检查项：

    1. 素材 id 在全部类别里唯一；
    2. 每个片段的 ``material_id`` 都能在**对应类别**里找到；
    3. 每个片段的 ``extra_material_refs`` 每一项都能找到；
    4. ``target_timerange`` / ``source_timerange`` 是整数微秒且时长大于 0；
    5. 同一条轨内的片段不重叠，且不超出 ``content.duration``；
    6. 每条轨非空（空轨只 warn，不阻断）；
    7. 文字素材的 ``content`` 能 ``json.loads``，且 ``styles[].range`` 不越界。
    """
    errors: List[str] = []
    warnings: List[str] = []
    if not isinstance(content, Mapping):
        return {"ok": False, "errors": ["draft_content 不是 dict"], "warnings": [],
                "stats": {}}

    materials = content.get("materials") or {}
    if not isinstance(materials, Mapping):
        materials = {}

    # 素材 id → 类别（同时查重）
    owner: Dict[str, str] = {}
    for category, items in materials.items():
        if not isinstance(items, list):
            errors.append("materials.%s 不是数组" % category)
            continue
        for item in items:
            if not isinstance(item, Mapping):
                errors.append("materials.%s 里有非 dict 元素" % category)
                continue
            material_id = item.get("id")
            if not isinstance(material_id, str) or not material_id:
                errors.append("materials.%s 里有一条素材没有 id" % category)
                continue
            if material_id in owner:
                errors.append("重复素材 id: %s（%s 与 %s）"
                              % (material_id, owner[material_id], category))
                continue
            owner[material_id] = category

    tracks = content.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        errors.append("content.tracks 为空")
        tracks = []

    duration = content.get("duration")
    duration_us = int(duration) if isinstance(duration, int) and not isinstance(duration, bool) else 0
    if duration_us <= 0:
        errors.append("content.duration 非法: %r（应为正整数微秒）" % (duration,))

    counts = {"video_tracks": 0, "text_tracks": 0, "audio_tracks": 0}
    segment_count = 0

    for track_index, track in enumerate(tracks):
        if not isinstance(track, Mapping):
            errors.append("tracks[%d] 不是 dict" % track_index)
            continue
        track_type = track.get("type")
        segments = track.get("segments")
        if not isinstance(segments, list):
            errors.append("tracks[%d].segments 不是数组" % track_index)
            continue
        if not segments:
            warnings.append("轨道 %d (%s) 没有 segment" % (track_index, track_type))
        if track_type == "video":
            counts["video_tracks"] += 1
        elif track_type == "text":
            counts["text_tracks"] += 1
        elif track_type == "audio":
            counts["audio_tracks"] += 1
        else:
            warnings.append("轨道 %d 的类型 %r 未识别" % (track_index, track_type))

        allowed = _SEG_CATEGORY.get(str(track_type), ())
        last_end = -1
        for seg_index, segment in enumerate(segments):
            segment_count += 1
            where = "track[%d].seg[%d]" % (track_index, seg_index)
            if not isinstance(segment, Mapping):
                errors.append("%s 不是 dict" % where)
                continue

            material_id = segment.get("material_id")
            if material_id not in owner:
                errors.append("%s material_id 悬空: %r" % (where, material_id))
            elif allowed and owner[material_id] not in allowed:
                errors.append("%s material_id 类别错误: 在 materials.%s 里，轨道类型 %s"
                              % (where, owner[material_id], track_type))

            for ref in segment.get("extra_material_refs") or []:
                if ref not in owner:
                    errors.append("%s extra_material_refs 悬空: %s" % (where, ref))

            timerange = segment.get("target_timerange")
            if not isinstance(timerange, Mapping):
                errors.append("%s 缺少 target_timerange" % where)
            else:
                start, dur = timerange.get("start"), timerange.get("duration")
                if not isinstance(start, int) or isinstance(start, bool) or start < 0:
                    errors.append("%s target.start 非法: %r" % (where, start))
                if not isinstance(dur, int) or isinstance(dur, bool) or dur <= 0:
                    errors.append("%s target.duration 非法: %r" % (where, dur))
                else:
                    end = (start or 0) + dur
                    if duration_us and end > duration_us:
                        errors.append("%s 结束于 %dµs，超出 content.duration=%dµs"
                                      % (where, end, duration_us))
                    if last_end >= 0 and (start or 0) < last_end:
                        errors.append("%s 与同轨上一段重叠（start=%d < %d）"
                                      % (where, start or 0, last_end))
                    last_end = end

            source = segment.get("source_timerange")
            if isinstance(source, Mapping):
                if isinstance(source.get("start", 0), bool) or \
                        not isinstance(source.get("start", 0), int) or source.get("start", 0) < 0:
                    errors.append("%s source.start 非法: %r" % (where, source.get("start")))
                src_dur = source.get("duration")
                if not isinstance(src_dur, int) or isinstance(src_dur, bool) or src_dur <= 0:
                    errors.append("%s source.duration 非法: %r" % (where, src_dur))

    # 文字素材的富文本 JSON
    for item in materials.get("texts") or []:
        if not isinstance(item, Mapping):
            continue
        raw = item.get("content")
        material_id = item.get("id")
        if not isinstance(raw, str):
            errors.append("文字素材 %s 的 content 不是字符串" % material_id)
            continue
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            errors.append("文字素材 %s 的 content 不是合法 JSON: %s" % (material_id, exc))
            continue
        text = payload.get("text") or ""
        for style in payload.get("styles") or []:
            text_range = style.get("range") or []
            if len(text_range) != 2:
                errors.append("文字素材 %s 的 range 非法: %r" % (material_id, text_range))
                continue
            begin, end = text_range
            if begin < 0 or end > len(text) or begin >= end:
                errors.append("文字素材 %s 的 range 越界: %r（文本长 %d）"
                              % (material_id, text_range, len(text)))

    canvas = content.get("canvas_config")
    if isinstance(canvas, Mapping):
        for key in ("width", "height"):
            value = canvas.get(key)
            if not isinstance(value, int) or value <= 0:
                errors.append("canvas_config.%s 非法: %r" % (key, value))
    else:
        errors.append("缺少 canvas_config")

    for key in ("id", "version", "new_version"):
        if not content.get(key):
            errors.append("缺少顶层字段 %s" % key)

    stats = {
        "tracks": len(tracks),
        "video_tracks": counts["video_tracks"],
        "text_tracks": counts["text_tracks"],
        "audio_tracks": counts["audio_tracks"],
        "segments": segment_count,
        "materials": {k: len(v) for k, v in materials.items() if isinstance(v, list)},
        "duration_us": duration_us,
        "duration_s": round(duration_us / 1_000_000.0, 3),
    }
    return {"ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}
