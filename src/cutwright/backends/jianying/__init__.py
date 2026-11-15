"""剪映（JianyingPro）后端。

把 :class:`~cutwright.timeline.Timeline` 变成一份**能被剪映直接打开**的草稿目录，
可选地再装进本机剪映的草稿库（``root_meta_info.json`` 索引 + 目录落地）。

模块分工::

    draft.py    Timeline → draft_content（纯函数，不碰磁盘）
    crypto.py   videoeditor.dll 的 ctypes 封装（找不到 DLL 时降级为明文）
    install.py  明文草稿目录 → 完整草稿目录 → 装进剪映 → 回读校验 → 卸载

设计上有一条硬约束：**生成草稿永远不需要剪映的 DLL**。没有 DLL 时 ``crypto``
优雅降级，后端依然能产出明文草稿供 review 或手工导入；只有真正要加密、要把
草稿装进剪映时才需要它。

``options`` 支持的键：

===================  ==========================================  ==========
键                   含义                                        默认
===================  ==========================================  ==========
name                 草稿名（也是输出子目录名）                  时间线名
install              构建完是否顺手装进剪映草稿库                False
encrypt              内容文件是否加密（真实安装必须）            False
dry_run              安装只出计划、不写盘                        False
drafts_root          剪映草稿根目录                              config 探测
template_dir         空草稿模板目录（提供 template.tmp 等）       None
cover                封面图片路径                                自动抽帧
font_path            字体文件绝对路径                            本机探测
font_mode            ``"none"`` / ``"native"``                   native
update_index         安装时是否维护 root_meta_info 索引           True
timeline_name        剪映里的时间线名                            "时间线01"
===================  ==========================================  ==========
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ...timeline import Timeline
from ..base import Backend, BuildResult, register
from . import crypto as _crypto
from .draft import build_content, validate_content
from .install import build_draft_dir, install_draft

__all__ = ["JianyingBackend"]

#: 草稿名兜底值（时间线没名字时用）。
DEFAULT_NAME = "cutwright"

#: Windows 文件名里不能出现的字符。
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 抽封面时截取第几秒的画面（取素材中点，太短就退回 0）。
_COVER_SEEK_RATIO = 0.5


def _safe_name(name: str) -> str:
    """把任意字符串收拾成能当目录名的草稿名。"""
    cleaned = _BAD_NAME_CHARS.sub("_", str(name or "")).strip().strip(".")
    return cleaned[:80] or DEFAULT_NAME


@register
class JianyingBackend(Backend):
    """剪映草稿后端。"""

    name = "jianying"
    description = "剪映草稿（JianyingPro 11.x）：生成可继续编辑的草稿目录，可选装进剪映草稿库"

    # -- 能力 -------------------------------------------------------------- #
    def drafts_root(self) -> str:
        """草稿根目录：选项优先，其次 :class:`~cutwright.config.Paths` 探测值。"""
        root = str(self.options.get("drafts_root") or "").strip()
        if root:
            return os.path.abspath(os.path.expanduser(root))
        detected = str(getattr(self.paths, "jianying_drafts", "") or "").strip()
        return os.path.abspath(os.path.expanduser(detected)) if detected else ""

    def template_dir(self) -> str:
        """空草稿模板目录（可选）。"""
        return str(self.options.get("template_dir") or "").strip()

    def available(self) -> bool:
        """能不能产出**可装进剪映**的草稿。

        三种情况都算可用：知道草稿根目录（能直接装）、有模板目录、或者本机
        装了剪映（有 ``videoeditor.dll`` 就能加密）。三者都没有时仍然可以
        ``build()`` 出明文草稿，只是装不进剪映——原因由
        :meth:`why_unavailable` 说清楚。
        """
        if self.drafts_root():
            return True
        if self.template_dir():
            return True
        return _crypto.available()

    def why_unavailable(self) -> str:
        parts: List[str] = []
        if not self.drafts_root():
            parts.append("没找到剪映草稿根目录（用 options['drafts_root'] 或环境变量 "
                         "CUTWRIGHT_JY_DRAFTS 指定）")
        if not self.template_dir():
            parts.append("没有模板目录")
        note = _crypto.probe_note()
        if note:
            parts.append(note)
        elif not _crypto.available():
            parts.append("本机没有可用的 videoeditor.dll")
        return "；".join(parts) + "；此时只能生成明文草稿，无法加密、无法装进剪映"

    # -- 构建 -------------------------------------------------------------- #
    def build(self, timeline: Timeline, out_dir: str) -> BuildResult:
        """生成草稿目录 + 明文内容 + 报告，并按需装进剪映。"""
        warnings: List[str] = []
        options = self.options
        raw_name = str(options.get("name") or "").strip() or (
            getattr(timeline, "name", "") or DEFAULT_NAME)
        name = _safe_name(raw_name)
        out = self.ensure_out_dir(out_dir)

        # 1) Timeline → draft_content（纯函数，不碰磁盘）
        media = self.collect_media(timeline, warnings)
        font_mode, font = self._font_options(warnings)
        font_scale = float(options.get("font_scale") or 1.0)
        content = build_content(
            timeline, media=media, font_scale=font_scale,
            font_mode=font_mode, font=font, warnings=warnings)
        validation = validate_content(content)

        # 2) 明文内容落盘：便于 diff / review，也是 --no-encrypt 的成品
        draft_json = os.path.join(out, name + ".draft.json")
        with open(draft_json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(content, fh, ensure_ascii=False, indent=1)

        # 3) 完整草稿目录（draft_meta_info / Timelines 双写 / draft_settings ...）
        draft_dir = os.path.join(out, name)
        encrypt = bool(options.get("encrypt"))
        cover = self._cover(timeline, out, warnings)
        build_report: Dict[str, Any] = {}
        try:
            build_report = build_draft_dir(
                content, draft_dir, name=name,
                template_dir=self.template_dir() or None,
                encrypt=encrypt, cover=cover, warnings=warnings)
        except _crypto.CryptoError as exc:
            # 要加密但本机没 DLL：退化成明文，把话说清楚，别让构建整个失败
            warnings.append("%s；已改为输出**明文**草稿" % exc)
            encrypt = False
            build_report = build_draft_dir(
                content, draft_dir, name=name,
                template_dir=self.template_dir() or None,
                encrypt=False, cover=cover, warnings=warnings)

        # 4) 可选安装（只有显式 install 才动草稿库；drafts_root 单独给了不算数）
        install_report: Dict[str, Any] = {}
        if options.get("install"):
            install_report = self._install(name, draft_dir, warnings)

        # 5) 报告
        artifacts = [draft_dir, draft_json]
        report: Dict[str, Any] = {
            "backend": self.name,
            "name": name,
            "available": self.available(),
            "content_ok": bool(validation.get("ok")),
            "validation": validation,
            "build": build_report,
            "install": install_report,
            "encrypted": bool(build_report.get("encrypted")),
            "options": {k: options.get(k) for k in
                        ("install", "encrypt", "dry_run", "drafts_root",
                         "template_dir", "cover", "font_path", "font_mode",
                         "update_index") if k in options},
            "warnings": warnings,
        }
        report_path = os.path.join(out, name + ".report.json")
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        artifacts.append(report_path)

        ok = bool(validation.get("ok")) and not (
            options.get("install") and not install_report.get("ok", False))
        error = ""
        if not validation.get("ok"):
            error = "内容校验未通过：%s" % "；".join(
                str(e) for e in (validation.get("errors") or [])[:5])
        elif options.get("install") and not install_report.get("ok", False):
            error = str(install_report.get("error")
                        or "；".join(install_report.get("errors") or [])
                        or "安装失败")

        return BuildResult(
            backend=self.name, ok=ok, artifacts=artifacts,
            data={
                "name": name,
                "draft_dir": draft_dir,
                "draft_json": draft_json,
                "report_json": report_path,
                "validation": validation,
                "stats": validation.get("stats") or {},
                "install": install_report,
                "encrypted": bool(build_report.get("encrypted")),
                "media": sorted(media),
                "warnings": warnings,
            },
            error=error, warnings=warnings)

    # -- 辅助 -------------------------------------------------------------- #
    def _install(self, name: str, draft_dir: str,
                 warnings: List[str]) -> Dict[str, Any]:
        """把草稿装进剪映草稿库（``dry_run`` 时只出计划）。"""
        root = self.drafts_root()
        if not root:
            return {"ok": False, "installed": False,
                    "error": "未指定草稿根目录（options['drafts_root']）",
                    "warnings": []}
        report = install_draft(
            draft_dir, drafts_root=root, name=name,
            dry_run=bool(self.options.get("dry_run")),
            update_index=bool(self.options.get("update_index", True)),
            allow_overwrite=bool(self.options.get("overwrite")))
        warnings.extend(report.get("warnings") or [])
        return report

    def _font_options(self, warnings: List[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """决定用不用自定义字体，返回 ``(font_mode, font)``。

        字体绝对路径在别人的机器上必然失效，所以：``options['font_path']``
        优先，其次探测本机；都没有就**省略字体字段**（绝不写死原项目的路径）。
        """
        if str(self.options.get("font_mode") or "") == "none":
            return "none", None
        path = str(self.options.get("font_path") or "").strip()
        if not path:
            path = _detect_font()
        if not path:
            if self.options.get("font_path"):
                warnings.append("指定的 font_path 不存在，已省略字体字段")
            return "none", None
        title = os.path.splitext(os.path.basename(path))[0]
        return "native", {
            "path": _win_slash(path),
            "id": "",
            "resource_id": "",
            "source_platform": 1,
            "title": title,
            "effect_id": "",
            "request_id": "",
        }

    def _cover(self, timeline: Timeline, out: str,
               warnings: List[str]) -> Optional[str]:
        """封面：选项给的图，否则从第一个视频素材中间帧抽一张。"""
        given = str(self.options.get("cover") or "").strip()
        if given:
            if os.path.isfile(os.path.expanduser(given)):
                return os.path.abspath(os.path.expanduser(given))
            warnings.append("指定的封面不存在，已忽略：%s" % given)
        clips = list(getattr(timeline, "clips", []) or [])
        if not clips:
            return None
        clip = clips[0]
        source = getattr(clip, "source", "")
        if not source or not os.path.isfile(source):
            return None
        target = os.path.join(out, "draft_cover.jpg")
        try:
            from ...media import extract_frame
        except Exception:                      # 没有 media 模块就跳过封面
            return None
        try:
            at = float(getattr(clip, "src_in", 0.0) or 0.0)
            dur = float(getattr(clip, "src_dur", 0.0) or 0.0)
            extract_frame(source, target, at + dur * _COVER_SEEK_RATIO, width=720)
        except Exception as exc:               # ffmpeg 缺失/抽帧失败都不致命
            warnings.append("抽封面失败（%s），草稿将没有缩略图" % exc)
            return None
        return target if os.path.isfile(target) else None

    def collect_media(self, timeline: Timeline,
                      warnings: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        """尽力探测时间线用到的素材，返回 ``{绝对路径: 素材信息}``。

        剪映草稿里每个素材都要写真实时长与显示尺寸，否则时间线会错位。探测
        失败不算错误：短视频段按 ``src_out`` 兜底，图片按画布尺寸兜底，缺的
        信息会导致的后果记进 warning。
        """
        notes = warnings if warnings is not None else []
        paths: List[str] = []
        for clip in getattr(timeline, "clips", []) or []:
            paths.append(str(getattr(clip, "source", "") or ""))
        for overlay in getattr(timeline, "overlays", []) or []:
            paths.append(str(getattr(overlay, "image", "") or ""))
        for audio in getattr(timeline, "audios", []) or []:
            paths.append(str(getattr(audio, "source", "") or ""))

        try:
            from ...media import VIDEO_EXT, probe
        except Exception as exc:               # media 模块不可用：全部走兜底
            notes.append("素材探测不可用（%s），将按时间线给出的时长兜底" % exc)
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for path in paths:
            if not path:
                continue
            key = os.path.abspath(path)
            if key in result:
                continue
            if not os.path.isfile(path):
                notes.append("素材不存在：%s（时长按时间线兜底）" % path)
                continue
            try:
                info = probe(path)
            except Exception as exc:           # 缺 ffprobe / 不认识的容器
                notes.append("探测失败 %s：%s" % (os.path.basename(path), exc))
                continue
            ext = os.path.splitext(path)[1].lower()
            result[key] = {
                "path": key,
                "kind": "video" if ext in VIDEO_EXT else "photo",
                "duration_us": int(round(float(info.duration or 0.0) * 1_000_000)),
                "width": int(info.width or 0),
                "height": int(info.height or 0),
                "hdr": bool(info.is_hdr),
            }
        return result


def _detect_font() -> str:
    """探测本机可用的中文字体文件，返回绝对路径或空串。"""
    try:                                   # 有 text 模块就优先用它（同事并行在写）
        from ... import text as _text     # type: ignore[attr-defined]
    except Exception:
        _text = None                       # type: ignore[assignment]
    if _text is not None:
        for attr in ("find_font", "find_cjk_font", "detect_font"):
            func = getattr(_text, attr, None)
            if callable(func):
                try:
                    found = func()
                except Exception:
                    found = None
                if isinstance(found, str) and found:
                    return found
    return ""


def _win_slash(path: str) -> str:
    """剪映草稿里的路径统一用正斜杠。"""
    return str(path).replace("\\", "/")
