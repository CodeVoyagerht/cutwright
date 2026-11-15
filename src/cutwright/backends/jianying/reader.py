"""读剪映草稿：草稿目录 → :class:`~cutwright.schema.Shotlist`。

写草稿（``draft.py``）之后再补读，是为了让流程闭环：别人用剪映手剪的一段，
也能拿回来变成数据、重新排一遍、或者换个后端出片。

三件必须处理的事实：

1. **11.x 的 ``draft_content.json`` 是 base64 密文**，需要本机 ``videoeditor.dll``
   才能解。没装剪映/装的是自检不通过的版本时，只能读明文草稿并给出原因。
2. **素材路径是绝对路径**，剪映写在 ``materials.videos[i].path``。换了机器这些
   路径就是错的，所以读出来要能告诉调用方「哪些素材已经不在了」，而不是悄悄
   生成一份到处是 E017 的分镜表。
3. **剪映的微秒**：``source_timerange`` / ``target_timerange`` 都是
   ``{start, duration}``，单位微秒。分镜表用秒，这里统一除法一次。

读不出来的东西不编造：字幕样式、转场、关键帧、特效都不还原，只在 ``warnings``
里说清楚丢了什么——猜错的样式比没有样式更糟。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...schema import (AudioSegment, Canvas, Shotlist, TextSegment, TextStyle,
                       VideoSegment)
from . import crypto as jycrypto

__all__ = ["DraftError", "DraftRead", "read_draft", "read_content"]

# 剪映草稿里可能的正文字段名（不同小版本不一样，按优先级试）。
CONTENT_FILES: Tuple[str, ...] = ("draft_content.json", "draft_info.json")
MICROS = 1_000_000.0
# 朗读/字幕生成的音频存在草稿目录里，草稿用这种前缀表示「相对草稿目录」。
_PLACEHOLDER = re.compile(r"^##_draftpath_placeholder_[^#]*##[\\/]?")


class DraftError(ValueError):
    """草稿目录不是一份能读的剪映草稿。"""


@dataclass
class DraftRead:
    """一份草稿读出来的东西，还没决定要变成什么。"""

    name: str
    path: str
    canvas: Canvas
    fps: float
    videos: List[Dict[str, Any]] = field(default_factory=list)
    texts: List[Dict[str, Any]] = field(default_factory=list)
    audios: List[Dict[str, Any]] = field(default_factory=list)
    encrypted: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        end = 0.0
        for item in self.videos + self.texts + self.audios:
            end = max(end, item["tl_start"] + item["tl_dur"])
        return round(end, 3)

    def summary(self) -> str:
        return ("%s：%d 镜 / %d 条字幕 / %d 条音频 / %s / %.2fs%s"
                % (self.name, len(self.videos), len(self.texts), len(self.audios),
                   "%dx%d" % (self.canvas.width, self.canvas.height),
                   self.duration, "（加密草稿）" if self.encrypted else ""))

    def missing_files(self) -> List[str]:
        """素材还在不在。换了机器之后这份清单通常不是空的。"""
        return [item["file"] for item in self.videos + self.audios
                if not os.path.exists(item["file"])]

    def to_shotlist(self, *, name: Optional[str] = None) -> Shotlist:
        """变成分镜表。样式一律用内置默认，字幕内容原样保留。"""
        videos = [VideoSegment(file=item["file"], src_start=item["src_start"],
                               src_dur=item["src_dur"], tl_start=item["tl_start"],
                               speed=item["speed"], act=item.get("act", ""),
                               shot=index + 1, desc=item.get("desc", ""),
                               still=bool(item.get("still", False)))
                  for index, item in enumerate(self.videos)]
        texts = [TextSegment(text=item["text"], tl_start=item["tl_start"],
                             tl_dur=item["tl_dur"],
                             style=item.get("style", "caption"))
                 for item in self.texts]
        audios = [AudioSegment(file=item["file"], tl_start=item["tl_start"],
                               tl_dur=item["tl_dur"], src_start=item["src_start"],
                               kind=item.get("kind", "music"))
                  for item in self.audios]
        shotlist = Shotlist(name=name or self.name, material_dir="",
                            asset_dir="", canvas=Canvas(self.canvas.width,
                                                        self.canvas.height),
                            fps=self.fps, video_segments=videos,
                            text_segments=texts, audio_segments=audios,
                            base_dir=self.path)
        return shotlist


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def read_draft(draft_dir: str, *, crypto: Any = None,
               allow_encrypted: bool = True) -> DraftRead:
    """读一个草稿目录。``crypto`` 传 False 表示「只读明文，别加载 dll」。"""
    path = os.path.abspath(draft_dir)
    if not os.path.isdir(path):
        raise DraftError("不是目录：%s" % path)

    content_path = None
    for candidate in CONTENT_FILES:
        full = os.path.join(path, candidate)
        if os.path.exists(full):
            content_path = full
            break
    if content_path is None:
        raise DraftError("这个目录里没找到 %s，不像剪映草稿：%s"
                         % (" 或 ".join(CONTENT_FILES), path))

    data = _read_bytes(content_path)
    # 先看形状再下结论：以 `{` 开头的就是明文（哪怕它写坏了），
    # 否则会把「少了一个括号」误报成「解不开密文」，让人去查 dll。
    encrypted = not _looks_like_json(data) and not jycrypto.is_plain_json(data)
    if encrypted:
        if crypto is False or not allow_encrypted:
            raise DraftError(
                "草稿是加密的（%s），但这次明确不要加载 videoeditor.dll。\n"
                "剪映草稿加密的说明见 skills/cutwright/references/backends.md"
                % os.path.basename(content_path))
        try:
            data = _decrypt(data, crypto)
        except Exception as error:  # noqa: BLE001 - 错误原因要原样交给用户
            raise DraftError(
                "解不开草稿（%s）。\n"
                "先用 `cutwright doctor` 看剪映的加密 dll 是否可用：%s"
                % (os.path.basename(content_path), error)) from error

    try:
        content = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DraftError("草稿内容不是合法 JSON：%s" % error) from error
    if not isinstance(content, dict):
        raise DraftError("草稿内容顶层不是对象，读不懂")

    draft = read_content(content, path=path)
    draft.encrypted = encrypted
    if encrypted and jycrypto.probe_note():
        draft.warnings.append(jycrypto.probe_note())
    return draft


def read_content(content: Dict[str, Any], *, path: str = "") -> DraftRead:
    """从已经解开的 ``draft_content`` dict 读。测试与二次解析都走这里。"""
    tracks = content.get("tracks")
    if not isinstance(tracks, list):
        raise DraftError("草稿里没有 tracks 数组，读不懂")

    materials = content.get("materials") or {}
    videos_by_id = _by_id(materials.get("videos"))
    audios_by_id = _by_id(materials.get("audios"))
    texts_by_id = _by_id(materials.get("texts"))
    roots = {"videos": videos_by_id, "audios": audios_by_id, "texts": texts_by_id}

    canvas_raw = content.get("canvas_config") or {}
    canvas = Canvas(width=int(canvas_raw.get("width") or 1080),
                    height=int(canvas_raw.get("height") or 1920))
    fps = _read_fps(content)

    draft = DraftRead(name=os.path.basename(path.rstrip("\\/")) or "draft",
                      path=path, canvas=canvas, fps=fps)

    image_count = 0
    for track in tracks:
        if not isinstance(track, dict):
            continue
        kind = track.get("type")
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            material = _lookup(segment, roots)
            if kind == "video" and material is not None:
                item = _video_item(segment, material)
                if item.get("photo"):
                    # 图片段是静帧，不是「没有画面」：保留下来，重建时按 still 处理，
                    # 否则图文草稿读回来会凭空少掉几秒。
                    image_count += 1
                    item["still"] = True
                    item["src_start"] = 0.0
                    item["speed"] = 1.0
                draft.videos.append(item)
            elif kind == "text" and material is not None:
                draft.texts.append(_text_item(segment, material))
            elif kind == "audio" and material is not None:
                draft.audios.append(_audio_item(segment, material))

    for group in (draft.videos, draft.texts, draft.audios):
        for item in group:
            if item.get("file"):
                item["file"] = _resolve_media(item["file"], path)

    draft.videos.sort(key=lambda item: (item["tl_start"], item["file"]))
    draft.texts.sort(key=lambda item: item["tl_start"])
    draft.audios.sort(key=lambda item: item["tl_start"])

    if image_count:
        draft.warnings.append("草稿里有 %d 张图片/照片段，已按静帧（still）读进来"
                              "——重建时会铺成同样时长的画面" % image_count)
    missing_ids = sorted(_dangling_ids(tracks, roots))
    if missing_ids:
        draft.warnings.append("有 %d 个片段引用了 materials 里不存在的素材（id: %s）"
                              % (len(missing_ids), ", ".join(missing_ids[:4])))
    if not draft.videos:
        draft.warnings.append("这份草稿没有视频段——分镜表会是空的")
    draft.warnings.append("字幕样式、转场、关键帧、特效都不还原：读回来的是「排布」，"
                          "不是「剪映长什么样」。样式按内置默认给。")
    if draft.texts:
        draft.warnings.append("旁白/画外音在草稿里也是文字轨，和字幕分不开——读回来一律"
                              "按字幕处理，重建前自己确认哪些不该出字。")
    return draft


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _looks_like_json(data: bytes) -> bool:
    """明文草稿以 ``{`` 开头（可能带 UTF-8 BOM）。密文是 base64，不会这样。"""
    head = data[:8].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"{")


def _decrypt(data: bytes, crypto: Any) -> bytes:
    """按 ``crypto`` 的形态选调用方式：None 走全局，对象走它自己。"""
    if crypto is None:
        return jycrypto.decrypt_bytes(data)
    if hasattr(crypto, "decrypt_bytes"):
        return crypto.decrypt_bytes(data)
    if hasattr(crypto, "decrypt"):
        return crypto.decrypt(data)
    raise DraftError("传进来的 crypto 对象既没有 decrypt_bytes 也没有 decrypt")


def _by_id(items: Any) -> Dict[str, Dict[str, Any]]:
    table: Dict[str, Dict[str, Any]] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            table[str(item["id"])] = item
    return table


def _lookup(segment: Dict[str, Any],
            roots: Dict[str, Dict[str, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """剪映的引用有两种写法：``material_id`` 与 ``extra_material_refs``。

    字幕的样式常常只在 ``extra_material_refs`` 里（``texts`` 那份是文案本体）。
    这里先按 ``material_id`` 找，找不到再顺着 ``extra_material_refs`` 一个个试。
    """
    mid = segment.get("material_id")
    if mid:
        for table in roots.values():
            if str(mid) in table:
                return table[str(mid)]
    for ref in segment.get("extra_material_refs") or []:
        for table in roots.values():
            if str(ref) in table:
                return table[str(ref)]
    return None


def _dangling_ids(tracks: Sequence[Dict[str, Any]],
                  roots: Dict[str, Dict[str, Dict[str, Any]]]) -> List[str]:
    known = set()
    for table in roots.values():
        known.update(table.keys())
    out: List[str] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        for segment in track.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            mid = segment.get("material_id")
            if mid and str(mid) not in known:
                out.append(str(mid))
    return out


def _range(segment: Dict[str, Any], key: str) -> Tuple[float, float]:
    raw = segment.get(key) or {}
    try:
        start = float(raw.get("start") or 0.0) / MICROS
        duration = float(raw.get("duration") or 0.0) / MICROS
    except (TypeError, ValueError):
        return 0.0, 0.0
    return round(max(0.0, start), 4), round(max(0.0, duration), 4)


def _speed(segment: Dict[str, Any]) -> float:
    raw = segment.get("speed")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0 else 1.0


def _material_path(material: Dict[str, Any]) -> str:
    for key in ("path", "local_material_path", "file_path"):
        value = material.get(key)
        if isinstance(value, str) and value:
            return os.path.normpath(value.replace("/", os.sep))
    return ""


def _resolve_media(path: str, draft_dir: str) -> str:
    """把草稿里的素材路径变成能直接打开的本机路径。

    剪映把朗读/字幕音频存在**草稿目录**下，草稿里写成
    ``##_draftpath_placeholder_<uuid>_##/textReading/xxx.wav``。当成绝对路径去
    open 必然失败，报出来的还是跟用户无关的怪路径；这里把占位前缀换回草稿目录。
    """
    if not path:
        return path
    text = str(path).replace("\\", "/")
    match = _PLACEHOLDER.match(text)
    if match:
        text = text[match.end():]
    absolute = os.path.isabs(text) or (len(text) > 1 and text[1] == ":")
    if absolute:
        return os.path.normpath(text)
    if draft_dir:
        return os.path.normpath(os.path.join(draft_dir, text))
    return os.path.normpath(text)


def _video_item(segment: Dict[str, Any], material: Dict[str, Any]) -> Dict[str, Any]:
    src_start, src_dur = _range(segment, "source_timerange")
    tl_start, tl_dur = _range(segment, "target_timerange")
    speed = _speed(segment)
    photo = str(material.get("type") or "").lower() in ("photo", "image")
    if src_dur <= 0.0 and material.get("duration"):
        try:
            src_dur = round(float(material["duration"]) / MICROS, 4)
        except (TypeError, ValueError):
            src_dur = tl_dur
    return {"file": _material_path(material), "src_start": src_start,
            "src_dur": src_dur if src_dur > 0 else tl_dur,
            "tl_start": tl_start, "tl_dur": tl_dur if tl_dur > 0 else src_dur,
            "speed": speed, "photo": photo,
            "desc": str((material.get("material_name") or ""))}


def _text_item(segment: Dict[str, Any], material: Dict[str, Any]) -> Dict[str, Any]:
    tl_start, tl_dur = _range(segment, "target_timerange")
    return {"text": _text_content(material), "tl_start": tl_start,
            "tl_dur": tl_dur, "style": "caption"}


def _audio_item(segment: Dict[str, Any], material: Dict[str, Any]) -> Dict[str, Any]:
    src_start, _ = _range(segment, "source_timerange")
    tl_start, tl_dur = _range(segment, "target_timerange")
    kind = "music" if str(material.get("type") or "").lower() != "extract" else "vo"
    return {"file": _material_path(material), "src_start": src_start,
            "tl_start": tl_start, "tl_dur": tl_dur, "kind": kind}


def _text_content(material: Dict[str, Any]) -> str:
    """文案本体藏在 ``content`` 这个 JSON 字符串里（``{"text": "..."}``）。"""
    raw = material.get("content")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(parsed, dict):
            text = parsed.get("text")
            if isinstance(text, str):
                return text
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("text"), str):
        return raw["text"]
    return str(material.get("text") or "")


def _read_fps(content: Dict[str, Any]) -> float:
    """fps 在剪映里位置不固定，按已知几处挨个找，都没有就 30。"""
    assistant = content.get("function_assistant_info") or {}
    candidates = [
        (assistant.get("fps") or {}).get("value") if isinstance(assistant, dict) else None,
        (content.get("canvas_config") or {}).get("fps"),
        (content.get("config") or {}).get("fps"),
    ]
    for value in candidates:
        try:
            fps = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 1.0 <= fps <= 240.0:
            return round(fps, 3)
    return 30.0
