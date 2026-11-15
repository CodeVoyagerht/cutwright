"""把 ``draft_content`` 封装成完整草稿目录，并装进剪映的草稿库。

这个模块负责「磁盘那一边」的全部事情：

1. :func:`build_draft_dir` —— 生成一个剪映能加载的草稿目录（可选加密）；
2. :func:`install_draft` —— 把它复制进草稿库、更新索引、回读校验；
3. :func:`verify_draft` —— 回读校验（根级与 ``Timelines/`` 两份都读）；
4. :func:`uninstall_draft` —— 只卸载本工具装过的草稿；
5. :func:`list_installed` —— 列出本工具装过的草稿。

多时间线双写（硬要求）
----------------------
剪映 11.6 起，真正被读取的时间线在 ``Timelines/<时间线 UUID>/draft_content.json``；
根目录那份 ``draft_content.json`` 只是「当前主时间线」的镜像。**两份内容必须完全一致**，
少写一份剪映就看不到修改。所以 :func:`build_draft_dir` 两组都写，
:func:`verify_draft` 两组都回读并逐字节比对。

安全规则（一条都不能少）
------------------------
1. 安装/卸载前若发现 ``JianyingPro.exe`` 在跑，**直接拒绝**，绝不替你杀进程；
2. 只增删本工具装过的草稿（白名单记录在 :func:`state_path` 指向的文件里），
   用户原有的草稿永不触碰；
3. 改 ``root_meta_info.json`` 前自动备份成 ``root_meta_info.json.bak-<时间戳>``；
4. ``dry_run=True`` 只返回计划，不写一个字节。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional

from . import crypto as _crypto

__all__ = [
    "META_TEMPLATE",
    "build_draft_dir",
    "find_index_path",
    "install_draft",
    "list_installed",
    "running_processes",
    "state_path",
    "uninstall_draft",
    "verify_draft",
]

#: 剪映进程名（只读检查，不杀）。
JIANYING_EXE = "JianyingPro.exe"

#: 草稿索引文件名。
INDEX_FILENAME = "root_meta_info.json"

#: 安装白名单的默认位置（可用 ``CUTWRIGHT_STATE_DIR`` 改）。
DEFAULT_STATE_DIR = "~/.cutwright"

#: 时间线默认名。
DEFAULT_TIMELINE_NAME = "时间线01"

#: 本工具在草稿目录里留的标记文件。它让 :func:`build_draft_dir` 能区分
#: 「这是我上一次生成的目录」和「用户自己的草稿」，后者绝不覆盖。
MARKER_NAME = ".cutwright-build.json"

#: 附件映射文件的内容（剪映 11.6 空草稿原样）。
_ATTACHMENT_MAPPING = {
    "id_mapping": {
        "mapping": [{"short_id": "1000",
                     "uuid": "F2E41821-37D2-4df7-895E-0B69BBBA0750"}],
        "next_index": 1001,
        "version": "1.0.0",
    }
}

#: ``draft_agency_config.json``（剪映 11.6 原样）。
_AGENCY_CONFIG = {
    "is_auto_agency_enabled": False,
    "is_auto_agency_popup": False,
    "is_single_agency_mode": False,
    "marterials": None,
    "use_converter": False,
    "video_resolution": 720,
}

#: ``performance_opt_info.json``（剪映 11.6 原样）。
_PERFORMANCE_CONFIG = {
    "manual_cancle_precombine_segs": None,
    "need_auto_precombine_segs": None,
}

#: ``draft_meta_info.json`` 的完整字段（51 个）。动态字段在 :func:`_build_meta` 里覆盖。
META_TEMPLATE: Dict[str, Any] = {
    "cloud_draft_cover": False,
    "cloud_draft_sync": False,
    "cloud_package_completed_time": "",
    "draft_cloud_capcut_purchase_info": "",
    "draft_cloud_last_action_download": False,
    "draft_cloud_package_type": "",
    "draft_cloud_purchase_info": "",
    "draft_cloud_template_id": "",
    "draft_cloud_tutorial_info": "",
    "draft_cloud_videocut_purchase_info": "",
    "draft_cover": "draft_cover.jpg",
    "draft_deeplink_url": "",
    "draft_enterprise_info": {
        "draft_enterprise_extra": "",
        "draft_enterprise_id": "",
        "draft_enterprise_name": "",
        "enterprise_material": [],
    },
    "draft_fold_path": "",
    "draft_has_unfinished_aigc_video_effect": False,
    "draft_id": "",
    "draft_is_ae_produce": False,
    "draft_is_ai_packaging_used": False,
    "draft_is_ai_shorts": False,
    "draft_is_ai_translate": False,
    "draft_is_article_video_draft": False,
    "draft_is_cloud_temp_draft": False,
    "draft_is_from_deeplink": "false",
    "draft_is_infinite_canvas_draft": False,
    "draft_is_invisible": False,
    "draft_is_pippit_draft": False,
    "draft_is_web_article_video": False,
    "draft_materials": [
        {"type": 0, "value": []},
        {"type": 1, "value": []},
        {"type": 2, "value": []},
        {"type": 3, "value": []},
        {"type": 6, "value": []},
    ],
    "draft_materials_copied_info": [],
    "draft_name": "",
    "draft_need_rename_folder": False,
    "draft_new_version": "",
    "draft_removable_storage_device": "",
    "draft_root_path": "",
    "draft_segment_extra_info": [],
    "draft_timeline_materials_size_": 0,
    "draft_type": "",
    "draft_web_article_video_enter_from": "",
    "pippit_avatar_url": "",
    "pippit_extra_info": "",
    "pippit_id": "",
    "pippit_user_name": "",
    "tm_draft_cloud_completed": "",
    "tm_draft_cloud_entry_id": -1,
    "tm_draft_cloud_modified": 0,
    "tm_draft_cloud_parent_entry_id": -1,
    "tm_draft_cloud_space_id": -1,
    "tm_draft_cloud_user_id": -1,
    "tm_draft_create": 0,
    "tm_draft_modified": 0,
    "tm_draft_removed": 0,
    "tm_duration": 0,
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _new_id() -> str:
    """剪映习惯的大写 UUID。"""
    return str(uuid.uuid4()).upper()


def _now_us() -> int:
    """当前时间（微秒）。"""
    return time.time_ns() // 1000


def _stamp() -> str:
    """文件名友好的时间戳，用于备份后缀。"""
    return time.strftime("%Y%m%d-%H%M%S")


def _posix(path: str) -> str:
    """正斜杠路径（草稿里所有路径字段都用正斜杠）。"""
    return str(path).replace("\\", "/")


def _win(path: str) -> str:
    """反斜杠路径（``draft_root_path`` 等字段沿用剪映习惯）。"""
    return str(path).replace("/", "\\")


def _norm(path: str) -> str:
    """比较路径是否相同用的规范化形式。"""
    try:
        return os.path.normcase(os.path.abspath(str(path)))
    except (OSError, ValueError):  # pragma: no cover
        return str(path)


def _write_bytes(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _write_text(path: str, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _write_json(path: str, obj: Any) -> None:
    """写紧凑 JSON（剪映自己的文件都是单行、无空格、中文不转义）。"""
    _write_bytes(path, json.dumps(obj, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"))


def _read_json(path: str) -> Any:
    """读一个可能是密文的 JSON 文件。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if not _crypto.is_plain_json(data):
        data = _crypto.decrypt_bytes(data)
    return json.loads(data.decode("utf-8"))


def _drive_of(path: str) -> str:
    """取盘符（``D:\\`` 这种形式），草稿的 ``draft_removable_storage_device`` 用它。"""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return (drive + "\\") if drive else "C:\\"


def _unique_dir(parent: str, prefix: str) -> str:
    """在 ``parent`` 下建一个唯一目录并返回路径。

    刻意不用 :func:`tempfile.mkdtemp`：它按 ``0o700`` 建目录，而在某些
    Windows 受限/沙箱环境里这个权限位会落成一条拒绝 ACE，之后往里写文件
    直接 ``PermissionError``。``os.makedirs`` 的默认权限没有这个问题。
    """
    last_error: Optional[OSError] = None
    for _ in range(100):
        path = os.path.join(parent, prefix + uuid.uuid4().hex[:12])
        try:
            os.makedirs(path, exist_ok=False)
        except FileExistsError as exc:
            last_error = exc
            continue
        return path
    raise last_error or OSError("无法在 %s 下创建临时目录" % parent)


def _staging_dir(target: str) -> str:
    """建一个临时工作目录：优先系统临时目录，不可写就退到 ``target`` 同级。

    沙箱 / 受限环境里系统临时目录可能不可写，而草稿必须先在临时目录里组装
    完整再落地（否则加密失败会留半个草稿）。退化路径清理逻辑与正常路径一致。
    """
    try:
        return _unique_dir(tempfile.gettempdir(), "cutwright-draft-")
    except OSError:
        parent = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(parent, exist_ok=True)
        return _unique_dir(parent, ".cutwright-staging-")


def _ensure_replaceable(target: str) -> None:
    """确认可以安全地重建 ``target`` 目录。

    只允许两种情况：目录不存在 / 目录是空的 / 目录里有本工具的标记文件。
    其它情况一律拒绝——宁可报错，也不删用户自己的草稿。
    """
    if not os.path.exists(target):
        return
    if not os.path.isdir(target):
        raise FileExistsError("目标路径已存在且不是目录：%s" % target)
    try:
        entries = os.listdir(target)
    except OSError as exc:
        raise FileExistsError("无法读取目标目录：%s（%s）" % (target, exc))
    if not entries:
        return
    if MARKER_NAME not in entries:
        raise FileExistsError(
            "目标目录非空且不是本工具生成的草稿，拒绝覆盖：%s"
            "（如果确实要重建，请先自己清理这个目录）" % target)


# --------------------------------------------------------------------------- #
# 草稿骨架
# --------------------------------------------------------------------------- #

def _build_meta(*, name: str, draft_id: str, fold_dir: str, root_path: str,
                duration_us: int, created_us: int, cover: str,
                materials_size: int, materials: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按模板生成 ``draft_meta_info.json``。"""
    meta = copy.deepcopy(META_TEMPLATE)
    meta.update({
        "draft_id": draft_id,
        "draft_name": name,
        "draft_fold_path": _posix(os.path.abspath(fold_dir)),
        "draft_root_path": _win(os.path.abspath(root_path)),
        "draft_removable_storage_device": _drive_of(fold_dir),
        "draft_cover": cover,
        "tm_draft_create": int(created_us),
        "tm_draft_modified": int(created_us),
        "tm_duration": int(duration_us),
        "draft_timeline_materials_size_": int(materials_size),
        "draft_materials": [
            {"type": 0, "value": materials},
            {"type": 1, "value": []},
            {"type": 2, "value": []},
            {"type": 3, "value": []},
            {"type": 6, "value": []},
        ],
    })
    return meta


def _material_rows(content: Mapping[str, Any], now_us: int) -> List[Dict[str, Any]]:
    """从 ``materials.videos`` 生成 ``draft_materials[type=0]`` 的素材清单。"""
    rows: List[Dict[str, Any]] = []
    seen = set()
    now_s = int(now_us / 1_000_000)
    for item in (content.get("materials") or {}).get("videos") or []:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "")
        key = _norm(path)
        if not path or key in seen:
            continue
        seen.add(key)
        is_photo = item.get("type") == "photo"
        rows.append({
            "ai_group_type": "",
            "create_time": now_s,
            "enter_from": 0,
            "extra_info": path.rsplit("/", 1)[-1],
            "file_Path": path,
            "height": int(item.get("height") or 0),
            "id": _new_id(),
            "import_time": now_s,
            "import_time_ms": int(now_us),
            "item_source": 1,
            "material_color_tag": "",
            "md5": "",
            "metetype": "photo" if is_photo else "video",
            "roughcut_time_range": {
                "duration": -1 if is_photo else int(item.get("duration") or 0),
                "start": -1 if is_photo else 0,
            },
            "sub_time_range": {"duration": -1, "start": -1},
            "type": 0,
            "width": int(item.get("width") or 0),
        })
    return rows


def _build_project(timeline_id: str, project_id: str, created_us: int,
                   timeline_name: str) -> Dict[str, Any]:
    """``Timelines/project.json``：把时间线登记到项目里。"""
    return {
        "config": {
            "color_space": -1,
            "hdr_vivid": False,
            "mixed_track_mode_on": False,
            "render_index_track_mode_on": False,
            "use_float_render": False,
        },
        "create_time": int(created_us),
        "id": project_id,
        "main_timeline_id": timeline_id,
        "timelines": [{
            "create_time": int(created_us),
            "id": timeline_id,
            "is_marked_delete": False,
            "name": timeline_name,
            "update_time": int(created_us),
        }],
        "update_time": int(created_us),
        "version": 0,
    }


def _build_layout(timeline_id: str, timeline_name: str) -> Dict[str, Any]:
    """``timeline_layout.json``：编辑器打开时停在哪条时间线。"""
    return {
        "activeTimeline": timeline_id,
        "dockItems": [{
            "dockIndex": 0,
            "ratio": 1,
            "timelineIds": [timeline_id],
            "timelineNames": [timeline_name],
        }],
        "layoutOrientation": 1,
    }


def _build_settings(created_s: int, edited_s: int, width: int, height: int) -> str:
    """``draft_settings``（INI，明文）。"""
    return (
        "[General]\n"
        "custom_ratio_width=%d\n"
        "custom_ratio_height=%d\n"
        "draft_create_time=%d\n"
        "draft_last_edit_time=%d\n"
        "real_edit_seconds=0\n"
        "real_edit_keys=1\n"
    ) % (int(width), int(height), int(created_s), int(edited_s))


def _write_plain_tree(root: str, *, content: Dict[str, Any], meta: Dict[str, Any],
                      timeline_id: str, project_id: str, width: int, height: int,
                      cover: Optional[str], template_dir: Optional[str],
                      timeline_name: str, warnings: List[str]) -> List[str]:
    """写出完整的**明文**草稿目录，返回写出的相对路径列表。"""
    content_bytes = json.dumps(content, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
    written: List[str] = []

    def put(rel: str, data: bytes) -> None:
        path = os.path.join(root, rel)
        _write_bytes(path, data)
        written.append(rel.replace("\\", "/"))

    # -- 根级 ------------------------------------------------------------ #
    put("draft_content.json", content_bytes)
    put("draft_content.json.bak", content_bytes)
    put("template-2.tmp", content_bytes)
    put("draft_meta_info.json", json.dumps(meta, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8"))
    put("draft_settings", _build_settings(
        int(meta["tm_draft_create"] / 1_000_000), int(time.time()), width, height).encode("utf-8"))
    put("timeline_layout.json", json.dumps(
        _build_layout(timeline_id, timeline_name), ensure_ascii=False,
        separators=(",", ":")).encode("utf-8"))
    put("performance_opt_info.json", json.dumps(
        _PERFORMANCE_CONFIG, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8"))
    put("draft_agency_config.json", json.dumps(
        _AGENCY_CONFIG, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    put("draft_biz_config.json", b"")
    put("common_attachment/attachment_id_mapping.json", json.dumps(
        _ATTACHMENT_MAPPING, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8"))

    # -- Timelines/（与根级逐字节一致的那份，剪映真正读的） --------------- #
    project = _build_project(timeline_id, project_id,
                             int(meta["tm_draft_create"]), timeline_name)
    put("Timelines/project.json", json.dumps(
        project, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    put("Timelines/project.json.bak", json.dumps(
        project, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    put("Timelines/%s/draft_content.json" % timeline_id, content_bytes)
    put("Timelines/%s/draft_content.json.bak" % timeline_id, content_bytes)
    put("Timelines/%s/template-2.tmp" % timeline_id, content_bytes)
    put("Timelines/%s/common_attachment/attachment_id_mapping.json" % timeline_id,
        json.dumps(_ATTACHMENT_MAPPING, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8"))

    # -- 可选：来自模板目录的其它文件 ------------------------------------ #
    if template_dir:
        template_dir = os.path.abspath(template_dir)
        src_tmp = _find_template_tmp(template_dir)
        if src_tmp:
            try:
                with open(src_tmp, "rb") as fh:
                    put("Timelines/%s/template.tmp" % timeline_id, fh.read())
            except OSError as exc:
                warnings.append("复制 template.tmp 失败：%s" % exc)
        else:
            warnings.append("模板目录里没有 template.tmp，Timelines/<id>/template.tmp 未写入")
    else:
        warnings.append(
            "未提供 template_dir：Timelines/<id>/template.tmp 未写入"
            "（剪映 11.6 自己生成的草稿里都有这个文件，缺失是否影响加载未验证）")

    # -- 封面 ------------------------------------------------------------ #
    if cover and os.path.isfile(cover):
        try:
            shutil.copy2(cover, os.path.join(root, "draft_cover.jpg"))
            written.append("draft_cover.jpg")
            dst = os.path.join(root, "Timelines", timeline_id, "draft_cover.jpg")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(cover, dst)
            written.append("Timelines/%s/draft_cover.jpg" % timeline_id)
        except OSError as exc:
            warnings.append("复制封面失败：%s" % exc)
    return written


def _find_template_tmp(template_dir: str) -> Optional[str]:
    """在模板目录里找 ``Timelines/<任意>/template.tmp``。"""
    timelines = os.path.join(template_dir, "Timelines")
    if os.path.isdir(timelines):
        for name in sorted(os.listdir(timelines)):
            cand = os.path.join(timelines, name, "template.tmp")
            if os.path.isfile(cand):
                return cand
    return None


# --------------------------------------------------------------------------- #
# 1. 生成草稿目录
# --------------------------------------------------------------------------- #

def build_draft_dir(content: Dict[str, Any], target_dir: str, *, name: str,
                    template_dir: Optional[str] = None, encrypt: bool = False,
                    cover: Optional[str] = None,
                    timeline_name: str = DEFAULT_TIMELINE_NAME,
                    warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """把 ``content`` 写成一个完整的剪映草稿目录。

    **双写**：根级 ``draft_content.json`` 与
    ``Timelines/<content['id']>/draft_content.json`` 写的是同一份字节；
    剪映 11.6 真正读取的是后者。

    参数
    ----
    target_dir
        草稿目录路径。它就是「装好之后」的位置，元信息里的 ``draft_fold_path``
        按它来写（:func:`install_draft` 复制到别处时会再改一次）。
    name
        草稿显示名。
    template_dir
        可选，一个空草稿模板目录（用来提供 ``Timelines/<id>/template.tmp`` 等）。
        给了就用，没给也能生成，只会在报告里记一条 warning。
    encrypt
        ``True`` 时内容文件写成剪映密文（真实安装必须），``False`` 保持明文
        （便于 review / diff，但剪映可能不认）。
    cover
        可选封面图片路径，会被复制成 ``draft_cover.jpg``。

    返回报告 dict（``ok`` / ``target_dir`` / ``files`` / ``warnings`` 等）。
    """
    notes: List[str] = warnings if warnings is not None else []
    if not isinstance(content, Mapping):
        raise ValueError("content 必须是 dict")
    content = copy.deepcopy(dict(content))

    if encrypt and not _crypto.available():
        raise _crypto.CryptoError(_crypto.NO_DLL_HINT)

    timeline_id = str(content.get("id") or "") or _new_id()
    content["id"] = timeline_id
    draft_id = _new_id()
    project_id = _new_id()

    canvas = content.get("canvas_config") or {}
    width = int(canvas.get("width") or 1080)
    height = int(canvas.get("height") or 1920)
    duration_us = int(content.get("duration") or 0)

    target = os.path.abspath(target_dir)
    root_path = os.path.dirname(target)
    now_us = _now_us()
    rows = _material_rows(content, now_us)
    materials_size = len(json.dumps(content, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8"))
    meta = _build_meta(
        name=name, draft_id=draft_id, fold_dir=target, root_path=root_path,
        duration_us=duration_us, created_us=now_us,
        cover="draft_cover.jpg" if (cover and os.path.isfile(cover)) else "",
        materials_size=materials_size, materials=rows,
    )

    # 先写到临时目录（加密失败也不会留下半个草稿），再落到 target。
    staging = _staging_dir(target)
    try:
        plain = os.path.join(staging, "plain")
        os.makedirs(plain, exist_ok=True)
        written = _write_plain_tree(
            plain, content=content, meta=meta, timeline_id=timeline_id,
            project_id=project_id, width=width, height=height, cover=cover,
            template_dir=template_dir, timeline_name=timeline_name,
            warnings=notes)
        _write_bytes(os.path.join(plain, MARKER_NAME), json.dumps(
            {"generator": "cutwright", "name": name, "timeline_id": timeline_id,
             "draft_id": draft_id, "created_at": _now_us()},
            ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        written.append(MARKER_NAME)

        _ensure_replaceable(target)
        if os.path.isdir(target):
            shutil.rmtree(target)  # 已确认是空目录或本工具上次生成的目录
        os.makedirs(target, exist_ok=True)
        if encrypt:
            stats = _crypto.crypt_tree(plain, target, encrypt=True)
            notes.extend("加密失败 %s：%s" % (f["path"], f["error"])
                         for f in stats.get("failed") or [])
            written = list(stats.get("files") or written)
        else:
            for rel in written:
                src = os.path.join(plain, rel)
                dst = os.path.join(target, rel)
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copy2(src, dst)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "ok": True,
        "target_dir": target,
        "name": name,
        "timeline_id": timeline_id,
        "draft_id": draft_id,
        "project_id": project_id,
        "encrypted": bool(encrypt),
        "canvas": {"width": width, "height": height},
        "duration_us": duration_us,
        "files": sorted(written),
        "warnings": notes,
    }


# --------------------------------------------------------------------------- #
# 2. 回读校验
# --------------------------------------------------------------------------- #

def _check(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    """一条校验结果。"""
    item: Dict[str, Any] = {"name": name, "ok": bool(ok)}
    if detail:
        item["detail"] = detail
    return item


def verify_draft(draft_dir: str, *, expect_name: Optional[str] = None) -> Dict[str, Any]:
    """回读一个草稿目录，确认剪映真的能加载。

    检查项包括：根级与 ``Timelines/<id>/`` 两份 ``draft_content.json`` 存在且
    内容一致、``Timelines/project.json`` 是明文且 ``main_timeline_id`` 对得上、
    ``draft_meta_info.json`` 的名字/时长对得上、内容文件确实不是明文。

    返回 ``{"ok": bool, "checks": [{"name", "ok"}], ...}``，``ok`` 为
    ``all(check.ok)``；``checks`` 里失败项会带 ``detail``。
    """
    checks: List[Dict[str, Any]] = []
    warnings: List[str] = []
    draft_dir = os.path.abspath(draft_dir)
    checks.append(_check("草稿目录存在", os.path.isdir(draft_dir), draft_dir))
    if not os.path.isdir(draft_dir):
        return {"ok": False, "checks": checks, "warnings": warnings,
                "draft_dir": draft_dir}

    root_content_path = os.path.join(draft_dir, "draft_content.json")
    meta_path = os.path.join(draft_dir, "draft_meta_info.json")

    # -- 根级内容 -------------------------------------------------------- #
    content: Dict[str, Any] = {}
    encrypted = False
    try:
        with open(root_content_path, "rb") as fh:
            raw = fh.read()
        encrypted = not _crypto.is_plain_json(raw)
        content = _read_json(root_content_path)
        checks.append(_check("draft_content.json 可解析", True))
    except FileNotFoundError:
        checks.append(_check("draft_content.json 存在", False, root_content_path))
    except _crypto.CryptoError as exc:
        checks.append(_check("draft_content.json 可解析", False, str(exc)))
    except (OSError, ValueError) as exc:
        checks.append(_check("draft_content.json 可解析", False, str(exc)))

    timeline_id = str(content.get("id") or "")
    checks.append(_check("draft_content.id 非空", bool(timeline_id), timeline_id))

    # -- 多时间线双写 ---------------------------------------------------- #
    if timeline_id:
        sub_path = os.path.join(draft_dir, "Timelines", timeline_id, "draft_content.json")
        checks.append(_check("Timelines/<id>/draft_content.json 存在",
                             os.path.isfile(sub_path), sub_path))
        if os.path.isfile(sub_path):
            try:
                sub = _read_json(sub_path)
                checks.append(_check("Timelines 与根级内容一致", sub == content))
            except (_crypto.CryptoError, OSError, ValueError) as exc:
                checks.append(_check("Timelines 与根级内容一致", False, str(exc)))

    # 内容文件不能是明文（明文模式下只警告）
    plain_files: List[str] = []
    for root, dirnames, filenames in os.walk(draft_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".recycle_bin")]
        for fname in filenames:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, draft_dir)
            if not _crypto.is_content_file_path(rel):
                continue
            try:
                with open(full, "rb") as fh:
                    if _crypto.is_plain_json(fh.read()):
                        plain_files.append(rel.replace("\\", "/"))
            except OSError:
                continue
    if encrypted:
        checks.append(_check("内容文件均已加密", not plain_files,
                             ", ".join(plain_files)))
    else:
        checks.append(_check("明文草稿（未加密）", True))
        warnings.append("这是明文草稿：剪映可能拒绝加载，安装前请用 --encrypt 加密")

    # -- Timelines/project.json ------------------------------------------ #
    project_path = os.path.join(draft_dir, "Timelines", "project.json")
    try:
        with open(project_path, "rb") as fh:
            project_raw = fh.read()
        checks.append(_check("Timelines/project.json 是明文",
                             _crypto.is_plain_json(project_raw)))
        project = json.loads(project_raw.decode("utf-8"))
        checks.append(_check("project.main_timeline_id 与内容一致",
                             project.get("main_timeline_id") == timeline_id))
        ids = [t.get("id") for t in project.get("timelines") or []
               if isinstance(t, Mapping)]
        checks.append(_check("project.timelines 含该时间线", timeline_id in ids))
    except FileNotFoundError:
        checks.append(_check("Timelines/project.json 存在", False, project_path))
    except (OSError, ValueError) as exc:
        checks.append(_check("Timelines/project.json 可解析", False, str(exc)))

    # -- 元信息 ---------------------------------------------------------- #
    meta: Dict[str, Any] = {}
    try:
        meta = _read_json(meta_path)
        checks.append(_check("draft_meta_info.json 可解析", True))
        checks.append(_check("draft_meta_info.draft_id 非空",
                             bool(meta.get("draft_id"))))
        if expect_name is not None:
            checks.append(_check("草稿名一致", meta.get("draft_name") == expect_name,
                                 "%r != %r" % (meta.get("draft_name"), expect_name)))
        checks.append(_check("meta.tm_duration 与内容一致",
                             int(meta.get("tm_duration") or 0) == int(content.get("duration") or 0),
                             "%s vs %s" % (meta.get("tm_duration"), content.get("duration"))))
        checks.append(_check("meta.draft_fold_path 指向本目录",
                             _norm(str(meta.get("draft_fold_path") or "")) == _norm(draft_dir),
                             str(meta.get("draft_fold_path"))))
    except FileNotFoundError:
        checks.append(_check("draft_meta_info.json 存在", False, meta_path))
    except (_crypto.CryptoError, OSError, ValueError) as exc:
        checks.append(_check("draft_meta_info.json 可解析", False, str(exc)))

    tracks = []
    for track in content.get("tracks") or []:
        if isinstance(track, Mapping):
            tracks.append({"type": track.get("type"),
                           "segments": len(track.get("segments") or [])})

    return {
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
        "warnings": warnings,
        "draft_dir": draft_dir,
        "encrypted": encrypted,
        "draft_id": meta.get("draft_id") or "",
        "draft_name": meta.get("draft_name") or "",
        "content_id": timeline_id,
        "duration_us": int(content.get("duration") or 0),
        "tracks": tracks,
    }


# --------------------------------------------------------------------------- #
# 3. 进程检查
# --------------------------------------------------------------------------- #

def running_processes() -> List[Dict[str, str]]:
    """列出正在运行的 ``JianyingPro.exe`` 进程（只读，绝不杀）。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % JIANYING_EXE, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="ignore", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: List[Dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or JIANYING_EXE.lower() not in line.lower():
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        found.append({"exe": parts[0], "pid": parts[1] if len(parts) > 1 else ""})
    return found


# --------------------------------------------------------------------------- #
# 4. 草稿索引 root_meta_info.json
# --------------------------------------------------------------------------- #

def find_index_path(drafts_root: str) -> Optional[str]:
    """找到这次安装该更新的 ``root_meta_info.json``。

    优先草稿库自己的那份；没有再退到剪映的应用数据目录（仅当它的
    ``root_path`` 指向同一个草稿库时才算数），避免写错库。
    """
    local_index = os.path.join(os.path.abspath(drafts_root), INDEX_FILENAME)
    if os.path.isfile(local_index):
        return local_index

    appdata = os.environ.get("LOCALAPPDATA")
    if not appdata:
        return None
    candidate = os.path.join(appdata, "JianyingPro", "User Data", "Projects",
                             "com.lveditor.draft", INDEX_FILENAME)
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if _norm(str(data.get("root_path") or "")) == _norm(drafts_root):
        return candidate
    return None


def _index_entry(*, name: str, draft_id: str, fold_path: str, root_path: str,
                 duration_us: int, has_cover: bool) -> Dict[str, Any]:
    """一条草稿索引记录（40 个字段，空值照抄剪映自己写的形状）。"""
    fold = _win(os.path.abspath(fold_path))
    now_us = _now_us()
    return {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_package_type": "",
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": (fold + "\\draft_cover.jpg") if has_cover else "",
        "draft_fold_path": _posix(os.path.abspath(fold_path)),
        "draft_id": draft_id,
        "draft_is_ae_produce": False,
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_infinite_canvas_draft": False,
        "draft_is_invisible": False,
        "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": fold + "\\draft_content.json",
        "draft_name": name,
        "draft_new_version": "",
        "draft_root_path": _win(os.path.abspath(root_path)),
        "draft_timeline_materials_size": 0,
        "draft_type": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "tm_duration": int(duration_us),
    }


def _read_index(index_path: str) -> Dict[str, Any]:
    with open(index_path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("root_meta_info.json 不是 JSON 对象")
    data.setdefault("all_draft_store", [])
    return data


def _write_index(index_path: str, data: Dict[str, Any]) -> str:
    """备份后写回索引，返回备份文件路径（空字符串表示没备份）。"""
    backup = ""
    if os.path.isfile(index_path):
        backup = index_path + ".bak-" + _stamp()
        shutil.copy2(index_path, backup)
    with open(index_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    return backup


def _update_index_add(index_path: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条记录插到索引最前面（同名同路径的旧记录先剔除）。"""
    data = _read_index(index_path)
    store = [d for d in data.get("all_draft_store") or [] if isinstance(d, dict)]
    target_fold = _norm(str(entry.get("draft_fold_path") or ""))
    kept = [d for d in store
            if _norm(str(d.get("draft_fold_path") or "")) != target_fold
            and d.get("draft_id") != entry.get("draft_id")]
    removed = len(store) - len(kept)
    data["all_draft_store"] = [entry] + kept
    data["draft_ids"] = len(data["all_draft_store"])
    backup = _write_index(index_path, data)
    return {"ok": True, "index": index_path, "backup": backup,
            "removed": removed, "total": len(data["all_draft_store"])}


def _update_index_remove(index_path: str, *, draft_id: str,
                         fold_path: str) -> Dict[str, Any]:
    """从索引里剔除一条记录（只在真的剔掉了东西时才写盘）。"""
    data = _read_index(index_path)
    store = [d for d in data.get("all_draft_store") or [] if isinstance(d, dict)]
    target_fold = _norm(fold_path)
    kept = [d for d in store
            if _norm(str(d.get("draft_fold_path") or "")) != target_fold
            and d.get("draft_id") != draft_id]
    removed = len(store) - len(kept)
    if not removed:
        return {"ok": True, "index": index_path, "backup": "", "removed": 0,
                "total": len(store)}
    data["all_draft_store"] = kept
    data["draft_ids"] = len(kept)
    backup = _write_index(index_path, data)
    return {"ok": True, "index": index_path, "backup": backup,
            "removed": removed, "total": len(kept)}


# --------------------------------------------------------------------------- #
# 5. 安装白名单
# --------------------------------------------------------------------------- #

def state_path() -> str:
    """安装白名单文件的位置（``~/.cutwright/installed.json``）。"""
    base = os.environ.get("CUTWRIGHT_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(os.path.expanduser(base), "installed.json")


def _state_key(drafts_root: str, name: str) -> str:
    """白名单的键：``草稿库绝对路径::草稿名``，多项目共存也不会串。"""
    return "%s::%s" % (_norm(drafts_root).lower(), name)


def _load_state() -> Dict[str, Any]:
    path = state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    installs = data.get("installs")
    if not isinstance(installs, dict):
        data["installs"] = {}
    return data


def _save_state(data: Dict[str, Any]) -> None:
    path = state_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _backup_dir() -> str:
    """覆盖安装前把旧草稿挪到这里（不放进草稿库，免得被剪映当成草稿）。"""
    base = os.environ.get("CUTWRIGHT_STATE_DIR") or DEFAULT_STATE_DIR
    return os.path.join(os.path.expanduser(base), "backups")


# --------------------------------------------------------------------------- #
# 6. 安装 / 卸载 / 列表
# --------------------------------------------------------------------------- #

def install_draft(build_dir: str, *, drafts_root: str, name: str,
                  dry_run: bool = False, update_index: bool = True,
                  allow_overwrite: bool = False) -> Dict[str, Any]:
    """把一个已经生成好的草稿目录装进剪映草稿库。

    参数
    ----
    build_dir
        :func:`build_draft_dir` 产出的目录。
    drafts_root
        剪映草稿库根目录（含 ``root_meta_info.json`` 的那个）。
    name
        草稿名，同时也是目标目录名。
    dry_run
        ``True`` 时只返回计划，不写盘。
    update_index
        ``True``（默认）更新 ``root_meta_info.json``；``False`` 时完全不碰索引，
        只记一条 warning（「可能需要重启剪映让索引刷新」）。
    allow_overwrite
        目标已存在、且确认是本工具装的，才允许覆盖。

    安全：发现 ``JianyingPro.exe`` 在跑就直接拒绝；目标目录存在但不是本工具装的
    （不在白名单里）也拒绝，用户原有草稿永不触碰。
    """
    warnings: List[str] = []
    errors: List[str] = []
    build_dir = os.path.abspath(build_dir)
    drafts_root = os.path.abspath(drafts_root)
    target = os.path.join(drafts_root, name)

    if not os.path.isdir(build_dir):
        errors.append("待安装目录不存在：%s" % build_dir)
    if not os.path.isdir(drafts_root):
        errors.append("草稿库不存在：%s" % drafts_root)

    state = _load_state()
    key = _state_key(drafts_root, name)
    known = key in state["installs"]
    exists = os.path.isdir(target)
    if exists and not known:
        errors.append("草稿库里已有同名草稿 %r，但它不是本工具安装的，拒绝覆盖" % name)
    if exists and known and not allow_overwrite:
        errors.append("草稿 %r 已存在（本工具安装过），要覆盖请显式允许覆盖" % name)

    index_path = find_index_path(drafts_root) if update_index else None
    if update_index and not index_path:
        warnings.append("没找到 %s，跳过索引更新（剪映可能需要重启才会看到新草稿）"
                        % INDEX_FILENAME)
    if not update_index:
        warnings.append("update_index=False：不改 %s，剪映可能需要重启才会看到新草稿"
                        % INDEX_FILENAME)

    procs = running_processes()
    if procs and not dry_run:
        errors.append(
            "剪映正在运行（%s），已拒绝安装。请完全退出剪映后重试；本工具不会替你关进程。"
            % ", ".join("pid=%s" % p.get("pid") for p in procs))

    steps = [
        "确认剪映未运行",
        "校验构建目录：%s" % build_dir,
        "复制到草稿库：%s" % target,
    ]
    if exists:
        steps.insert(2, "把已有草稿备份到 %s" % _backup_dir())
    if index_path:
        steps.append("备份并更新索引：%s" % index_path)
    steps.append("从安装位置回读校验")

    plan = {
        "ok": False, "action": "install", "dry_run": bool(dry_run),
        "installed": False,
        "name": name, "target": target, "drafts_root": drafts_root,
        "build_dir": build_dir, "exists": exists, "known": known,
        "update_index": bool(update_index), "index": index_path or "",
        "backup_dir": _backup_dir() if exists else "",
        "steps": steps, "errors": errors, "warnings": warnings,
        "processes": procs,
    }
    if errors:
        plan["error"] = "；".join(errors)
        return plan
    if dry_run:
        plan["ok"] = True
        plan["planned"] = True
        return plan

    backup = ""
    try:
        if exists:
            backup_root = _backup_dir()
            os.makedirs(backup_root, exist_ok=True)
            backup = os.path.join(backup_root, "%s-%s" % (name, _stamp()))
            shutil.move(target, backup)
        shutil.copytree(build_dir, target)
    except OSError as exc:
        if backup and not os.path.isdir(target):
            try:
                shutil.move(backup, target)
            except OSError:
                pass
        plan["error"] = "复制草稿失败：%s" % exc
        return plan

    # 元信息里的路径要改成安装后的真实位置
    try:
        _retarget_meta(target)
    except (OSError, ValueError, _crypto.CryptoError) as exc:
        warnings.append("改写 draft_meta_info 路径失败：%s" % exc)

    verify = verify_draft(target, expect_name=name)
    index_report: Dict[str, Any] = {}
    if index_path:
        try:
            entry = _index_entry(
                name=name, draft_id=verify.get("draft_id") or "",
                fold_path=target, root_path=drafts_root,
                duration_us=int(verify.get("duration_us") or 0),
                has_cover=os.path.isfile(os.path.join(target, "draft_cover.jpg")))
            index_report = _update_index_add(index_path, entry)
        except (OSError, ValueError) as exc:
            errors.append("更新 %s 失败：%s" % (INDEX_FILENAME, exc))

    state["installs"][key] = {
        "name": name, "drafts_root": drafts_root, "target": target,
        "build_dir": build_dir, "backup": backup,
        "timeline_id": verify.get("content_id") or "",
        "draft_id": verify.get("draft_id") or "",
        "duration_us": int(verify.get("duration_us") or 0),
        "tracks": verify.get("tracks") or [],
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "index": index_report.get("index") or "",
        "index_backup": index_report.get("backup") or "",
    }
    try:
        _save_state(state)
    except OSError as exc:
        warnings.append("写入安装记录失败（下次可能无法覆盖/卸载）：%s" % exc)

    plan.update({
        "ok": bool(verify.get("ok")) and not errors,
        "installed": True, "backup": backup, "verify": verify,
        "index_report": index_report,
        "errors": errors, "warnings": warnings,
    })
    if not verify.get("ok"):
        plan["error"] = "安装后回读校验失败"
    return plan


def _retarget_meta(target: str) -> None:
    """把草稿里的 ``draft_fold_path`` / ``draft_root_path`` 改成真实位置。"""
    meta_path = os.path.join(target, "draft_meta_info.json")
    if not os.path.isfile(meta_path):
        return
    meta = _read_json(meta_path)
    root_path = os.path.dirname(os.path.abspath(target))
    meta["draft_fold_path"] = _posix(os.path.abspath(target))
    meta["draft_root_path"] = _win(root_path)
    meta["draft_removable_storage_device"] = _drive_of(target)
    meta["tm_draft_modified"] = _now_us()
    data = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # 与草稿其它内容文件一样：原本加密的就加密写回
    with open(meta_path, "rb") as fh:
        was_encrypted = not _crypto.is_plain_json(fh.read())
    if was_encrypted:
        data = _crypto.encrypt_bytes(data)
    _write_bytes(meta_path, data)


def uninstall_draft(name: str, *, drafts_root: str, purge: bool = False,
                    dry_run: bool = False, update_index: bool = True) -> Dict[str, Any]:
    """卸载一个**由本工具安装**的草稿。

    ``purge=False``（默认）时把目录挪到隔离区，可手工找回；
    ``purge=True`` 时彻底删除。索引条目会同步删掉——留着悬空条目会让剪映在
    同名重建时报「草稿内容已损坏」。
    """
    warnings: List[str] = []
    errors: List[str] = []
    drafts_root = os.path.abspath(drafts_root)
    target = os.path.join(drafts_root, name)
    state = _load_state()
    key = _state_key(drafts_root, name)
    entry = state["installs"].get(key)
    if not entry:
        errors.append("%r 不是本工具安装的草稿，拒绝卸载" % name)
    if not os.path.isdir(target):
        errors.append("草稿目录不存在：%s" % target)

    procs = running_processes()
    if procs and not dry_run:
        errors.append(
            "剪映正在运行（%s），已拒绝卸载。请完全退出剪映后重试。"
            % ", ".join("pid=%s" % p.get("pid") for p in procs))

    index_path = find_index_path(drafts_root) if update_index else None
    if update_index and not index_path:
        warnings.append("没找到 %s，跳过索引清理" % INDEX_FILENAME)
    if not update_index:
        warnings.append("update_index=False：索引条目会残留，可能导致同名重建时"
                        "剪映报「草稿内容已损坏」")

    steps = ["确认剪映未运行"]
    if purge:
        steps.append("删除草稿目录：%s" % target)
    else:
        steps.append("把草稿目录移到隔离区：%s" % _backup_dir())
    if index_path:
        steps.append("从索引里删除该条目：%s" % index_path)

    plan = {
        "ok": False, "action": "uninstall", "dry_run": bool(dry_run),
        "name": name, "target": target, "purge": bool(purge),
        "drafts_root": drafts_root, "update_index": bool(update_index),
        "index": index_path or "", "steps": steps,
        "errors": errors, "warnings": warnings, "processes": procs,
    }
    if errors:
        plan["error"] = "；".join(errors)
        return plan
    if dry_run:
        plan["ok"] = True
        plan["planned"] = True
        return plan

    quarantine = ""
    try:
        if purge:
            shutil.rmtree(target)
        else:
            root = _backup_dir()
            os.makedirs(root, exist_ok=True)
            quarantine = os.path.join(root, "%s-%s" % (name, _stamp()))
            shutil.move(target, quarantine)
    except OSError as exc:
        plan["error"] = "删除/移动草稿失败：%s" % exc
        return plan

    index_report: Dict[str, Any] = {}
    if index_path:
        try:
            index_report = _update_index_remove(
                index_path, draft_id=str(entry.get("draft_id") or ""),
                fold_path=target)
        except (OSError, ValueError) as exc:
            warnings.append("清理索引失败：%s" % exc)

    state["installs"].pop(key, None)
    try:
        _save_state(state)
    except OSError as exc:
        warnings.append("更新安装记录失败：%s" % exc)

    plan.update({"ok": True, "removed": True, "quarantine": quarantine,
                 "index_report": index_report})
    return plan


def list_installed(drafts_root: str) -> List[Dict[str, Any]]:
    """列出本工具在某个草稿库里装过、且目录还在的草稿。"""
    root = os.path.abspath(drafts_root)
    state = _load_state()
    out: List[Dict[str, Any]] = []
    for entry in state["installs"].values():
        if not isinstance(entry, dict):
            continue
        if _norm(str(entry.get("drafts_root") or "")) != _norm(root):
            continue
        target = str(entry.get("target") or "")
        item = dict(entry)
        item["exists"] = bool(target) and os.path.isdir(target)
        out.append(item)
    out.sort(key=lambda e: str(e.get("installed_at") or ""), reverse=True)
    return out
