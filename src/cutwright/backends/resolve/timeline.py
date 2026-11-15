"""``Timeline`` IR → 达芬奇 Resolve 时间线。

这是 resolve 后端的**全部真活**：把一份已经确定的时间线翻译成 Resolve
脚本 API 的调用序列，并把「实际发生了什么」诚实地写进报告。

三条原则
--------

1. **不新建工程**。工程是用户的资产：``GetCurrentProject()`` 为空就报错，
   让用户自己去建。我们只往里面加时间线。
2. **诚实降级**。脚本 API 建不了字幕对象（它只能建 TimelineItem/Generator），
   所以可见文字导出成同名 ``.srt`` 放在旁边，报告里写清
   ``method="srt_sidecar"`` 与导入路径。不假装成功，也不假装失败。
3. **不假设成功**。``AppendToTimeline`` 的返回值、``trackIndex`` /
   ``recordFrame`` 的组合在不同大版本行为有差异，所以追加完立刻用
   ``GetStart()/GetEnd()/GetDuration()`` 复核，把实测值写进报告。

帧语义（照 ``docs/v0.2-contract.md`` 的 E2E 实测结论）
-----------------------------------------------------
* clipInfo 的 ``startFrame`` / ``endFrame`` 是**素材内的源帧**，且
  ``endFrame`` 是**排除式**（``0..52`` 表示 52 帧）。
* 换算走 :meth:`cutwright.timeline.Timeline.frames`（``round(秒 * fps)``）。
  **取舍**：两端各自独立四舍五入，而不是对时长四舍五入。这样每个端点都
  落在真实帧上、不会随镜头数累计漂移；代价是单镜头的帧数可能比
  ``round(时长*fps)`` 差 1 帧（``src_in=0.5, src_out=2.52, fps=30`` →
  ``15..76``，共 61 帧，而 ``round(2.02*30)=61`` 恰好一致）。帧数优先保
  端点真实，这是剪辑里更重要的那一边。

未验证项（标出来，别当事实用）
--------------------------
* ``AppendToTimeline`` 的 clipInfo 是否认 ``recordFrame``（有空白档时才传）。
* ``TimelineItem.GetNodeGraph()`` 的返回是否可能为 ``None``（按可为空处理）。
* ``CreateEmptyTimeline`` 之后是否**自动**成为当前时间线——这里显式
  ``SetCurrentTimeline`` 一次，不依赖自动行为。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from .bridge import ResolveError, at_least, parse_version

if TYPE_CHECKING:  # 只为类型检查引入，避免运行期把 IR 拖进来
    from ...timeline import Timeline, TimelineClip, TimelineText

__all__ = [
    "build_timeline",
    "plan_clips",
    "resolve_timeline_name",
    "build_srt",
    "srt_time",
    "ClipPlan",
    "NAME_PREFIX",
    "SRT_NOTE",
]

NAME_PREFIX = "CUTWRIGHT"
SRT_NOTE = ("Resolve 脚本 API 不支持直接建字幕，请用 "
            "File > Import > Subtitle 导入这个 srt")
LUT_NOTE = ("SetLUT 只对 Resolve 已经发现的 LUT 生效，所以写之前会先调 "
            "project.RefreshLUTList()")

# 时间线名里不能出现路径分隔符一类的字符（未验证 Resolve 的实际限制，先规范化）。
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# --------------------------------------------------------------------------- #
# 计划：把 IR 变成「要执行的调用」
# --------------------------------------------------------------------------- #
@dataclass
class ClipPlan:
    """一个镜头在 Resolve 里的落地参数。全部是帧号（源帧 / 时间线帧）。"""

    index: int
    source: str
    start_frame: int
    end_frame: int
    record_frame: Optional[int]
    speed: float
    note: str = ""

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame

    def clip_info(self, item: Any, track_index: int) -> Dict[str, Any]:
        """``AppendToTimeline`` 的 clipInfo。字段名按官方文档，不要改。"""
        info: Dict[str, Any] = {
            "mediaPoolItem": item,
            "startFrame": self.start_frame,
            "endFrame": self.end_frame,
            "trackIndex": track_index,
        }
        if self.record_frame is not None:
            # 只在有空白档时才传：不传 recordFrame 是「顺序追加」的默认行为，
            # 传了会覆盖落点。字段存在性未验证，见模块 docstring。
            info["recordFrame"] = self.record_frame
        return info


def plan_clips(timeline: "Timeline", track_index: int = 1,
               options: Optional[Dict[str, Any]] = None) -> Tuple[List[ClipPlan], List[str]]:
    """把 ``timeline.clips`` 换算成 :class:`ClipPlan` 列表 + 警告。

    纯计算：不碰 Resolve、不碰磁盘（只做 ``os.path.isfile`` 只读检查）。
    """
    opts: Dict[str, Any] = dict(options or {})
    plans: List[ClipPlan] = []
    warnings: List[str] = []
    cursor = int(opts.get("start_frame") or 0)  # 上一条追加后会落在哪一帧

    for clip in timeline.clips:
        start = timeline.frames(clip.src_in)
        end = timeline.frames(clip.src_out)
        note = ""
        if end <= start:
            # 不足一帧的源区间：Resolve 会拒绝，补到 1 帧并说清楚。
            end = start + 1
            warnings.append(
                f"镜头 {clip.index} 的源区间不足 1 帧"
                f"（src_in={clip.src_in:.3f}s, src_out={clip.src_out:.3f}s），已补到 1 帧")

        want = timeline.frames(clip.tl_start)
        gap = want - cursor
        record: Optional[int] = None
        if gap > 0:
            record = want
            note = f"与上一镜之间有 {gap} 帧空档"
            warnings.append(
                f"镜头 {clip.index} 的落点 {want} 帧与上一镜结束的 {cursor} 帧之间有 "
                f"{gap} 帧空档，将尝试用 recordFrame={want} 定位（该字段未验证）")
        elif gap < 0:
            note = f"与上一镜重叠 {-gap} 帧"
            warnings.append(
                f"镜头 {clip.index} 与上一镜在时间线上重叠 {-gap} 帧；"
                f"AppendToTimeline 只能顺序追加，重叠不会被保留")

        speed = float(clip.speed or 1.0)
        if abs(speed - 1.0) > 1e-6:
            warnings.append(
                f"镜头 {clip.index} 设了 speed={speed}，但 clipInfo 没有变速字段；"
                f"这里按完整源区间 {start}..{end} 追加，变速请在 Resolve 里手动设置")

        if not os.path.isfile(clip.source):
            warnings.append(f"镜头 {clip.index} 的素材不存在：{clip.source}（真跑时会失败）")

        plans.append(ClipPlan(index=clip.index, source=clip.source,
                              start_frame=start, end_frame=end,
                              record_frame=record, speed=speed, note=note))
        # 顺序追加时，下一镜的落点 = 当前落点 + 实际追加的帧数
        base = want if record is not None else cursor
        cursor = base + (end - start)

    return plans, warnings


def resolve_timeline_name(timeline: "Timeline", options: Optional[Dict[str, Any]] = None,
                          warnings: Optional[List[str]] = None) -> str:
    """时间线名：默认 ``CUTWRIGHT_{名字}_{时间戳}``，可用 ``options["timeline_name"]`` 覆盖。

    ``options["timestamp"]`` 可以给一个固定串，方便测试与 CI 得到可复现的名字。
    """
    opts: Dict[str, Any] = dict(options or {})
    explicit = opts.get("timeline_name")
    if explicit:
        candidate = str(explicit).strip()
    else:
        stamp = opts.get("timestamp") or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = f"{NAME_PREFIX}_{timeline.name}_{stamp}"
    cleaned = _BAD_NAME_CHARS.sub("_", candidate)
    if cleaned != candidate and warnings is not None:
        warnings.append(f"时间线名 {candidate!r} 含非法字符，已替换为 {cleaned!r}")
    return cleaned or f"{NAME_PREFIX}_{timeline.name}"


# --------------------------------------------------------------------------- #
# 字幕：诚实降级成 srt
# --------------------------------------------------------------------------- #
def srt_time(seconds: float) -> str:
    """秒 → ``00:00:01,500``。按毫秒四舍五入，先取整再拆分以免浮点误差。"""
    total_ms = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _text_body(text: "TimelineText") -> str:
    """SRT 里要显示的正文。

    IR 用 ``"正文|关键词"`` 表示「关键词换色」，竖线本身不是内容——所以拼回去
    但去掉竖线。空行会截断 SRT 的条目，逐行 strip 后保留单行。
    """
    body = (text.head + text.keyword).strip()
    return " ".join(part.strip() for part in body.splitlines() if part.strip())


def build_srt(texts: Sequence["TimelineText"],
              warnings: Optional[List[str]] = None) -> str:
    """把可见文字编成一份标准 SRT（不含 BOM，写盘时由调用方加）。"""
    blocks: List[str] = []
    ordered = sorted([t for t in texts if t.visible],
                     key=lambda t: (t.tl_start, t.index))
    for text in ordered:
        body = _text_body(text)
        if not body:
            if warnings is not None:
                warnings.append(f"文字 {text.index} 内容为空，已跳过")
            continue
        if text.tl_dur <= 0:
            if warnings is not None:
                warnings.append(f"文字 {text.index} 时长 <= 0，字幕无法显示，已跳过")
            continue
        start = max(0.0, float(text.tl_start))
        end = max(start + 0.001, float(text.tl_end))
        blocks.append(f"{len(blocks) + 1}\n"
                      f"{srt_time(start)} --> {srt_time(end)}\n{body}\n")
    if not blocks:
        return ""
    return "\n".join(blocks)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_timeline(bridge: Any, timeline: "Timeline", *,
                   options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 ``timeline`` 建成 Resolve 里的一条时间线，返回结构化报告。

    ``options`` 支持的键：

    ================ ==========================================================
    ``dry_run``      True 时只算不执行：不连 Resolve、不写 srt、不改任何东西，
                     只返回「将要执行的调用清单」（``report["calls"]``）。
    ``timeline_name`` 覆盖默认时间线名。
    ``timestamp``    固定时间戳（默认 ``%Y%m%d_%H%M%S``），CI 用。
    ``lut``          逐镜套用的 ``.cube`` 路径。
    ``lut_node``     LUT 写到第几个节点（默认最后一个）。
    ``track_index``  视频轨号，默认 1。
    ``out_dir``      srt 落盘目录；不给就只在报告里带内容。
    ``start_frame``  时间线起点帧偏移（默认 0）。
    ================ ==========================================================

    出错一律抛 :class:`~cutwright.backends.resolve.bridge.ResolveError`，
    消息里带「第几镜、哪一步」；``dry_run`` 路径不会抛 Resolve 相关的错。
    """
    opts: Dict[str, Any] = dict(options or {})
    dry_run = bool(opts.get("dry_run"))
    track_index = _track_index(opts)
    warnings: List[str] = []

    name = resolve_timeline_name(timeline, opts, warnings)
    plans, plan_warnings = plan_clips(timeline, track_index, opts)
    warnings.extend(plan_warnings)

    out_dir = opts.get("out_dir")
    srt_text = build_srt(timeline.texts, warnings)
    srt_path: Optional[str] = None
    if out_dir and srt_text:
        srt_path = os.path.join(str(out_dir), f"{name}.srt")

    lut_plan = _plan_lut(bridge, opts, warnings)

    report: Dict[str, Any] = {
        "timeline_name": name,
        "track": track_index,
        "fps": timeline.fps,
        "duration_frames": timeline.frames(timeline.duration),
        "clips": [_clip_entry(p) for p in plans],
        "texts": _texts_report(srt_path, srt_text),
        "lut": lut_plan["report"],
        "imports": _unique([p.source for p in plans]),
        "warnings": warnings,
        "dry_run": dry_run,
        "version": "",
        "project": "",
    }

    # ---- dry_run：整条时间线的调用清单，一个 API 都不调 ----
    if dry_run:
        if srt_text:
            # 内容随报告返回，方便 CI 校验字幕时间码；不落盘。
            report["texts"]["content"] = srt_text
        report["calls"] = _dry_run_calls(plans, name, track_index, lut_plan)
        return report

    # ---- 真跑 ----
    resolve = bridge.connect()          # 连不上会抛带提示的 ResolveError
    version = _bridge_version(bridge)
    studio = _bridge_is_studio(bridge)
    report["version"] = version
    report["is_studio"] = studio

    manager = _required(resolve, "GetProjectManager", "取 ProjectManager")
    # 刻意不用 _required：「没有工程」是最常见的环境问题，要给它一条专门的、
    # 说得清「我不会替你新建工程」的话，而不是通用的「返回空」。
    project = _call(manager, "GetCurrentProject", step="取当前工程")
    if not project:
        raise ResolveError(
            "Resolve 里没有打开的工程",
            hint="先在 Resolve 里新建或打开一个工程——cutwright 不会替你新建"
                 "（那是你的资产），建好后再跑一次",
        )
    report["project"] = _safe_str(project, "GetName")

    media_pool = _required(project, "GetMediaPool", "取媒体池")

    # -- 2. 导入素材（同一文件只导一次）--
    items: Dict[str, Any] = {}
    for plan in plans:
        if plan.source in items:
            continue
        if not os.path.isfile(plan.source):
            raise ResolveError(
                f"导入素材失败（镜头 {plan.index}）：文件不存在 {plan.source}",
                hint="确认素材路径；相对路径是相对 shotlist 的 material_dir 解析的",
            )
        got = _call(media_pool, "ImportMedia", [plan.source],
                    step=f"ImportMedia（镜头 {plan.index}，{os.path.basename(plan.source)}）")
        item = _first_item(got)
        if item is None:
            raise ResolveError(
                f"导入素材失败（镜头 {plan.index}）：ImportMedia 没有返回媒体池条目"
                f"（{plan.source}）",
                hint="常见原因：编码不被支持；或该素材已在媒体池但路径不同，"
                     "Resolve 复用了已有条目却没返回它——可先在 Resolve 里手动导入一次试试",
            )
        items[plan.source] = item

    # -- 3. 建线 --
    new_tl = _call(media_pool, "CreateEmptyTimeline", name,
                   step=f"CreateEmptyTimeline({name!r})")
    if not new_tl:
        raise ResolveError(
            f"创建时间线失败：CreateEmptyTimeline({name!r}) 返回空",
            hint="检查是否已有同名时间线，以及当前工程是否处于可编辑状态（渲染中/锁定会失败）",
        )
    _call(project, "SetCurrentTimeline", new_tl,
          step=f"SetCurrentTimeline({name!r})")
    actual_name = _safe_str(new_tl, "GetName")
    if actual_name:
        report["timeline_actual_name"] = actual_name
    # 复核「当前时间线」确实是新建的那条：后续 AppendToTimeline 都落在当前线上，
    # 搞错就会往用户别的线里追加。GetCurrentTimeline 的返回值语义**未验证**，
    # 所以只在能读出名字、且名字对不上时才记 warning，不当硬失败。
    get_current = getattr(project, "GetCurrentTimeline", None)
    if callable(get_current):
        current = _call(project, "GetCurrentTimeline", step="复核当前时间线")
        if current is not None and current is not new_tl:
            current_name = _safe_str(current, "GetName")
            if current_name and current_name != (actual_name or name):
                warnings.append(
                    f"SetCurrentTimeline 之后当前时间线是 {current_name!r}，"
                    f"与预期的 {name!r} 不一致；追加可能落到别的线里")

    # -- 4. 逐条追加 + 立刻复核 --
    report["clips"] = []
    appended: List[Any] = []
    for plan in plans:
        info = plan.clip_info(items[plan.source], track_index)
        got = _call(media_pool, "AppendToTimeline", [info],
                    step=f"AppendToTimeline（镜头 {plan.index}，"
                         f"{os.path.basename(plan.source)}）")
        item = _first_item(got)
        if item is None:
            raise ResolveError(
                f"追加镜头 {plan.index} 失败：AppendToTimeline 返回空"
                f"（{plan.source} 的 {plan.start_frame}..{plan.end_frame} 帧）",
                hint="多半是 trackIndex 与当前时间线不匹配，或该轨道已被锁定；"
                     "也可能是 endFrame 超出了素材实际帧数",
            )
        appended.append(item)
        entry = _clip_entry(plan)
        placed = _verify_item(item, warnings, plan)
        if placed:
            entry["placement"] = placed
        report["clips"].append(entry)

    # -- 5. 逐镜 LUT（可选）--
    if lut_plan["target"]:
        report["lut"] = _apply_lut(project, plans, appended, lut_plan["target"],
                                   lut_plan["source"], version, opts, warnings)

    # -- 6. 字幕降级成 srt --
    if srt_text and srt_path:
        _write_srt(srt_path, srt_text)

    return report


# --------------------------------------------------------------------------- #
# 报告小工具
# --------------------------------------------------------------------------- #
def _clip_entry(plan: ClipPlan) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "index": plan.index,
        "source": plan.source,
        "start_frame": plan.start_frame,
        "end_frame": plan.end_frame,
        "ok": True,
    }
    if plan.note:
        entry["note"] = plan.note
    return entry


def _texts_report(path: Optional[str], content: str) -> Dict[str, Any]:
    if not content:
        return {"method": "none", "path": None, "count": 0,
                "note": "时间线里没有可见文字"}
    count = sum(1 for line in content.splitlines() if line.strip().isdigit())
    return {"method": "srt_sidecar", "path": path, "count": count, "note": SRT_NOTE}


def _unique(items: Sequence[str]) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _track_index(opts: Dict[str, Any]) -> int:
    raw = opts.get("track_index", 1)
    try:
        index = int(raw)
    except (TypeError, ValueError):
        raise ResolveError(
            f"options['track_index'] 必须是正整数，收到 {raw!r}",
            hint="track_index=1 表示第一条视频轨",
        ) from None
    if index < 1:
        raise ResolveError(
            f"options['track_index'] 必须 >= 1，收到 {index}",
            hint="track_index=1 表示第一条视频轨",
        )
    return index


def _bridge_version(bridge: Any) -> str:
    try:
        raw = bridge.version()
    except Exception:  # 版本号拿不到不该让建线失败
        return ""
    return raw if isinstance(raw, str) else ""


def _bridge_is_studio(bridge: Any) -> Optional[bool]:
    try:
        raw = bridge.is_studio()
    except Exception:
        return None
    return raw if isinstance(raw, bool) else None


def _first_item(got: Any) -> Any:
    """ImportMedia / AppendToTimeline 的返回值可能是 list、单个对象或 False。"""
    if got is None or got is False:
        return None
    if isinstance(got, (list, tuple)):
        return got[0] if got else None
    return got


def _safe_str(obj: Any, method: str) -> str:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return ""
    try:
        value = fn()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _call(obj: Any, method: str, *args: Any, step: str) -> Any:
    """带上下文的 API 调用。``step`` 是「这一步在做什么」，会进错误消息。"""
    fn = getattr(obj, method, None)
    if not callable(fn):
        raise ResolveError(
            f"{step} 失败：当前 Resolve 版本没有 {method}() 方法",
            hint="确认 Resolve 版本（本后端按 19/20/21 的 API 写）；"
                 "过老的版本缺一些方法",
        )
    try:
        return fn(*args)
    except ResolveError:
        raise
    except Exception as exc:
        raise ResolveError(
            f"{step} 失败：{type(exc).__name__}: {exc}",
            hint="把这段错误和 Resolve 版本一起反馈；常见原因是素材编码不支持、"
                 "轨道被锁定，或工程处于渲染中",
        ) from exc


def _required(obj: Any, method: str, step: str) -> Any:
    """取一个对象（ProjectManager / Project / MediaPool），拿不到就带上下文报错。"""
    value = _call(obj, method, step=step)
    if value is None:
        raise ResolveError(
            f"{step} 失败：{method}() 返回空",
            hint="确认 Resolve 里已经打开一个工程，并且没有停在启动页",
        )
    return value


def _verify_item(item: Any, warnings: List[str], plan: ClipPlan) -> Dict[str, int]:
    """追加后立刻复核实际落点。复核失败只记 warning——它不改变已经发生的事。"""
    placed: Dict[str, int] = {}
    for key, method in (("start", "GetStart"), ("end", "GetEnd"),
                        ("duration", "GetDuration")):
        fn = getattr(item, method, None)
        if not callable(fn):
            continue
        try:
            value = fn()
            if isinstance(value, int) and not isinstance(value, bool):
                placed[key] = value
        except Exception as exc:
            warnings.append(f"镜头 {plan.index} 复核 {method}() 失败：{exc}")
    expected = plan.frame_count
    got = placed.get("duration")
    if got is not None and got != expected:
        warnings.append(
            f"镜头 {plan.index} 追加后实测时长 {got} 帧，期望 {expected} 帧"
            f"（源帧 {plan.start_frame}..{plan.end_frame}）；"
            f"Resolve 可能按素材尾部或变速处理了")
    return placed


# --------------------------------------------------------------------------- #
# LUT
# --------------------------------------------------------------------------- #
def _plan_lut(bridge: Any, opts: Dict[str, Any],
              warnings: List[str]) -> Dict[str, Any]:
    """算出 LUT 的目标路径与报告骨架（不写盘、不调 API）。"""
    report: Dict[str, Any] = {"path": None, "method": "none", "applied": 0,
                              "failed": 0, "total": 0, "note": ""}
    raw = opts.get("lut")
    if not raw:
        return {"report": report, "target": None, "source": None}

    source = str(raw)
    if not source.lower().endswith(".cube"):
        warnings.append(f"LUT 文件 {source!r} 不是 .cube；Resolve 只认 .cube，很可能无效")
    if not os.path.isfile(source):
        raise ResolveError(
            f"LUT 文件不存在：{source}",
            hint="给 options['lut'] 一个存在的 .cube 路径；相对路径按当前工作目录解析",
        )

    target = source
    lut_dir = bridge.lut_dir()          # 只探测目录，不连 Resolve、不写盘
    if lut_dir:
        if not _is_under(source, lut_dir):
            target = os.path.join(lut_dir, "cutwright", os.path.basename(source))
            warnings.append(
                f"LUT 不在 Resolve 的 LUT 目录内，将复制到 {target} 后再套用")
    else:
        warnings.append(
            "探测不到 Resolve 的 LUT 目录，将直接使用原始 LUT 路径；"
            "若 SetLUT 静默失败，请手动把它拷进 ...\\Support\\LUT\\ 下")

    report.update({"path": target, "total": None, "note": LUT_NOTE})
    return {"report": report, "target": target, "source": source}


def _apply_lut(project: Any, plans: Sequence[ClipPlan], items: Sequence[Any],
               target: str, source: Optional[str], version: str,
               opts: Dict[str, Any],
               warnings: List[str]) -> Dict[str, Any]:
    """逐镜套 LUT。19.0.0+ 走 ``GetNodeGraph().SetLUT()``，更早走 ``item.SetLUT()``。"""
    report: Dict[str, Any] = {"path": target, "method": "none", "applied": 0,
                             "failed": 0, "total": len(plans), "note": LUT_NOTE}

    refresh = getattr(project, "RefreshLUTList", None)
    if not callable(refresh):
        warnings.append(
            "当前 Resolve 版本没有 project.RefreshLUTList()，无法让 Resolve 发现"
            "新 LUT，已跳过逐镜 LUT")
        report["method"] = "skipped_no_refresh"
        report["failed"] = len(plans)
        return report
    try:
        refresh()
    except Exception as exc:
        warnings.append(f"RefreshLUTList() 失败：{exc}（LUT 可能不被识别）")

    # LUT 先落到 Resolve 能看见的地方。复制失败只降级成 warning：原路径仍可能可用。
    prepared = _prepare_lut_file(target, source, warnings)
    report["path"] = prepared

    parsed = parse_version(version)
    node_first = parsed is None or at_least(parsed, (19, 0, 0))
    allow_legacy = parsed is None or not at_least(parsed, (19, 0, 0))
    report["api"] = "node_graph" if node_first else "timeline_item"

    methods: List[str] = []
    for plan, item in zip(plans, items):
        ok, method, errors = _set_item_lut(item, prepared, node_first,
                                           allow_legacy, opts)
        if ok:
            report["applied"] += 1
            if method and method not in methods:
                methods.append(method)
        else:
            report["failed"] += 1
            detail = ("；".join(errors)) if errors else "SetLUT 返回失败"
            warnings.append(f"镜头 {plan.index} 套 LUT 失败：{detail}")
    if len(methods) == 1:
        report["method"] = methods[0]
    elif methods:
        report["method"] = "mixed:" + ",".join(methods)
    return report


def _set_item_lut(item: Any, lut_path: str, node_first: bool,
                  allow_legacy: bool,
                  opts: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
    """给一个 TimelineItem 套 LUT。返回 ``(成功?, 走的路, 错误列表)``。"""
    errors: List[str] = []
    if node_first:
        graph_fn = getattr(item, "GetNodeGraph", None)
        graph = None
        if callable(graph_fn):
            try:
                graph = graph_fn()
            except Exception as exc:
                errors.append(f"GetNodeGraph() 抛异常：{exc}")
        else:
            errors.append("该 TimelineItem 没有 GetNodeGraph()")
        if graph is not None:
            try:
                count = int(getattr(graph, "GetNumNodes", lambda: 1)() or 1)
            except Exception:
                count = 1
            wanted = opts.get("lut_node") or count
            try:
                node = max(1, min(int(wanted), max(1, count)))
            except (TypeError, ValueError):
                node = max(1, count)
            setter = getattr(graph, "SetLUT", None)
            if not callable(setter):
                errors.append("NodeGraph 没有 SetLUT()")
            else:
                try:
                    if setter(node, lut_path):
                        return True, "node_graph", errors
                    errors.append(f"NodeGraph.SetLUT({node}) 返回 False"
                                  f"（LUT 未被 Resolve 发现？）")
                except Exception as exc:
                    errors.append(f"NodeGraph.SetLUT({node}) 抛异常：{exc}")

    if allow_legacy:
        legacy = getattr(item, "SetLUT", None)
        if callable(legacy):
            try:
                if legacy(lut_path):
                    return True, "timeline_item", errors
                errors.append("TimelineItem.SetLUT() 返回 False")
            except Exception as exc:
                errors.append(f"TimelineItem.SetLUT() 抛异常：{exc}")
    return False, "", errors


def _prepare_lut_file(target: str, source: Optional[str],
                      warnings: List[str]) -> str:
    """把 LUT 复制到 Resolve 的 LUT 目录。返回最终可用的路径。

    ``SetLUT`` 只对 Resolve **已经发现**的 LUT 生效，而 Resolve 只扫自己的
    LUT 目录，所以「LUT 在别的盘上」必须复制过去。复制失败时退回原路径并
    记 warning——原文件本身是好的，只是 Resolve 可能看不见。
    """
    if not source or os.path.normcase(os.path.abspath(source)) == \
            os.path.normcase(os.path.abspath(target)):
        return source or target
    try:
        parent = os.path.dirname(os.path.abspath(target))
        os.makedirs(parent, exist_ok=True)
        with open(source, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    except OSError as exc:
        warnings.append(
            f"复制 LUT 到 {target} 失败：{exc}；将改用原始路径 {source}，"
            f"若 SetLUT 静默失败请手动复制")
        return source
    return target


def _is_under(path: str, directory: str) -> bool:
    try:
        p = os.path.normcase(os.path.abspath(path))
        d = os.path.normcase(os.path.abspath(directory))
    except (TypeError, ValueError):
        return False
    return p == d or p.startswith(d + os.sep)


# --------------------------------------------------------------------------- #
# srt 落盘
# --------------------------------------------------------------------------- #
def _write_srt(path: str, content: str) -> None:
    """写 SRT：UTF-8 **带 BOM**（Resolve 的导入器对无 BOM 的中文容错较差）。"""
    parent = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="\n") as handle:
            handle.write(content)
    except OSError as exc:
        raise ResolveError(
            f"写入 srt 字幕失败：{path}：{exc}",
            hint="检查 out_dir 是否可写、路径是否过长",
        ) from exc


# --------------------------------------------------------------------------- #
# dry_run 的调用清单
# --------------------------------------------------------------------------- #
def _dry_run_calls(plans: Sequence[ClipPlan], name: str, track_index: int,
                   lut_plan: Dict[str, Any]) -> List[List[Any]]:
    """把整条时间线要执行的调用写成可读清单。

    每条是 ``[操作名, 参数]``（JSON 友好，等价于规格里的元组写法）；
    ``mediaPoolItem`` 用素材路径字符串代替——dry_run 里根本没有媒体池对象。
    """
    calls: List[List[Any]] = []
    for source in _unique([p.source for p in plans]):
        calls.append(["ImportMedia", [source]])
    calls.append(["CreateEmptyTimeline", name])
    calls.append(["SetCurrentTimeline", name])
    for plan in plans:
        info: Dict[str, Any] = {
            "mediaPoolItem": plan.source,
            "startFrame": plan.start_frame,
            "endFrame": plan.end_frame,
            "trackIndex": track_index,
        }
        if plan.record_frame is not None:
            info["recordFrame"] = plan.record_frame
        calls.append(["AppendToTimeline", info])
    target = lut_plan["report"].get("path")
    if target:
        parsed = None  # dry_run 不连 Resolve，拿不到版本；两条路都列出来
        calls.append(["RefreshLUTList", None])
        for plan in plans:
            calls.append(["SetLUT", {
                "lut": target,
                "clip_index": plan.index,
                "node": "运行时取 GetNumNodes()（" + str(parsed) + "）",
            }])
    return calls
