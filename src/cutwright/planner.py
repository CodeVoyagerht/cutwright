"""把「一堆素材 + 一个目标时长」变成一条能剪的分镜表。

这是 cutwright 里唯一带**判断**的模块，其余模块都是在搬运数据。三件事：

1. ``default_styles()`` —— 给一份能直接用的文字样式预设。新手最痛的不是
   排版太丑，而是「不知道要写哪些字段」，所以样式全给默认值、全部可覆盖。
2. ``normalize_shotlist()`` —— 把「没写 tl_start」的分镜表排成首尾相接。
   人写分镜时只关心镜序，不该手算 0.00 / 2.35 / 4.10 这些数。
3. ``plan_from_materials()`` —— 只有素材和时长目标时，用 ffmpeg 的场景切点
   把素材切成候选镜头，再挑一组凑够目标时长。这是「自动剪辑」里最粗的那层
   智能：**它不懂内容，只懂节奏**。文档里明确写清这一点，不夸大。

设计约束：所有函数都是**纯函数**（除 ``plan_from_materials`` 要探素材），
输入 Shotlist 输出新的 Shotlist，绝不原地改——这样调用方可以反复试参数。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config, media
from .schema import (
    Act,
    Canvas,
    ImageOverlay,
    Issue,
    Shotlist,
    TextStyle,
    VideoSegment,
    _CHAIN_EPS,
)

__all__ = [
    "PlanReport",
    "Planner",
    "default_styles",
    "normalize_shotlist",
    "fit_duration",
    "plan_from_materials",
]

# 文字样式的基准画布：所有像素值按 1080x1920 标定，换画布等比缩放。
_BASE_W, _BASE_H = 2160, 3840


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
@dataclass
class PlanReport:
    """一次规划的结果。``ok`` 为 False 时看 ``messages``。"""

    ok: bool = True
    target: Optional[float] = None
    achieved: float = 0.0
    drift: float = 0.0
    shots: int = 0
    actions: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "target": self.target,
            "achieved": round(self.achieved, 3),
            "drift": round(self.drift, 3),
            "drift_pct": round(self.drift / self.target * 100, 2) if self.target else 0.0,
            "shots": self.shots,
            "actions": self.actions, "messages": self.messages,
            "stats": self.stats,
        }


# --------------------------------------------------------------------------- #
# 样式预设
# --------------------------------------------------------------------------- #
def default_styles(canvas: Optional[Canvas] = None) -> Dict[str, TextStyle]:
    """一套「竖屏口播/旅拍」能直接用的文字样式。

    数值来自实拍项目标定（2160x3840 的竖屏成片，字幕 145px），换画布时等比缩放，
    这样 720p 和 4K 出来的相对观感一致。
    """
    canvas = canvas or Canvas()
    s = canvas.height / _BASE_H if _BASE_H else 1.0
    bottom = round(520 * s)
    return {
        "caption": TextStyle(
            role="caption", font_height_px=round(145 * s),
            color="#FFFFFF", keyword_color="#FFD54A",
            stroke_color="#000000", stroke_width_px=round(12 * s),
            align="center", bottom_margin_px=bottom,
            max_chars_per_line=14,
        ),
        "title": TextStyle(
            role="title", font_height_px=round(150 * s),
            color="#FFFFFF", keyword_color="#FFD54A",
            stroke_color="#000000", stroke_width_px=round(12 * s),
            align="center", center_ratio_y=0.68,
            max_chars_per_line=12,
        ),
        "huazi": TextStyle(
            role="huazi", font_height_px=round(235 * s),
            color="#FFD400", keyword_color="#FF5252",
            stroke_color="#111111", stroke_width_px=round(27 * s),
            align="center", center_ratio_y=0.66,
            max_chars_per_line=8, rotation_deg=-7.0,
            anim="slide_in_left_zoom",
        ),
        "end": TextStyle(
            role="end", font_height_px=round(195 * s),
            color="#FFFFFF", keyword_color="#FFD54A",
            stroke_color="#000000", stroke_width_px=round(14 * s),
            align="center", center_ratio_y=0.62,
            max_chars_per_line=10,
        ),
        # 旁白只出声不出字：剪映里靠「文本朗读」配音，所以文字层必须不可见。
        "vo_hidden": TextStyle(
            role="vo_hidden", font_height_px=round(60 * s),
            color="#000000", stroke_color="#000000", stroke_width_px=0,
            align="center", center_ratio_y=1.4, invisible=True,
        ),
    }


def _with_default_styles(sl: Shotlist) -> Shotlist:
    """分镜表没写 styles 时补一套默认值；写了就一个都不动。"""
    if sl.styles:
        return sl
    fresh = Shotlist.from_dict(sl.to_dict())
    fresh.styles = default_styles(sl.canvas)
    return fresh


# --------------------------------------------------------------------------- #
# 排版
# --------------------------------------------------------------------------- #
def normalize_shotlist(sl: Shotlist, *, trust_existing: bool = True
                       ) -> Tuple[Shotlist, List[str]]:
    """把视频段排成首尾相接，返回新分镜表 + 改动说明。

    ``trust_existing=True`` 时尊重已写的 ``tl_start``（只给没写的补位），
    ``False`` 时全部重排——后者适合「我改了镜头顺序但懒得改时间」。
    """
    out = Shotlist.from_dict(sl.to_dict())
    actions: List[str] = []
    cursor = 0.0
    for i, seg in enumerate(out.video_segments):
        if trust_existing and seg.tl_start is not None:
            start = seg.tl_start
            if abs(start - cursor) > _CHAIN_EPS:
                actions.append(
                    f"shot {i}: 保留显式 tl_start={start:.3f}s（与前一镜结束 "
                    f"{cursor:.3f}s 不连续）")
        else:
            start = cursor
            if seg.tl_start is not None:
                actions.append(
                    f"shot {i}: tl_start {seg.tl_start:.3f}s → {start:.3f}s（重排）")
            seg.tl_start = start
        cursor = start + seg.tl_dur
    out.total_duration = round(cursor, 6)

    # 文字/音频/叠加层没写 tl_start 的，默认落在 0（渲染时会显示在最前）
    for group in (out.text_segments, out.vo_segments):
        for seg in group:
            if seg.tl_start is None:
                seg.tl_start = 0.0
    return out, actions


# --------------------------------------------------------------------------- #
# 时长拟合
# --------------------------------------------------------------------------- #
def fit_duration(sl: Shotlist, target: float, *, tolerance: float = 0.02,
                 min_shot: float = 0.8, strategy: str = "trim",
                 paths: Optional[config.Paths] = None
                 ) -> Tuple[Shotlist, PlanReport]:
    """把总时长凑到 ``target`` 秒，误差目标 ``tolerance``（比例）。

    两种策略：

    * ``trim``：等比缩每个镜头的 ``src_dur``（默认）。不改变素材内容，只剪短
      或（素材还有余量时）拉长。低于 ``min_shot`` 的镜头不再缩，剩下的差距
      摊给别的镜头。
    * ``speed``：整体微调 ``speed`` 把时长拉准，**不动剪点**。适合「剪点定死了，
      只差一点点」的情况，但对白/音乐的节奏会跟着变，慎用。

    到不了目标时 ``report.ok=False``，并把还差多少写进 ``messages``——不装作
    成功，也不偷偷丢镜头。
    """
    out = Shotlist.from_dict(sl.to_dict())
    report = PlanReport(target=float(target), shots=len(out.video_segments))
    if not out.video_segments:
        report.ok = False
        report.messages.append("没有视频段，无法拟合时长")
        return out, report
    if target <= 0:
        report.ok = False
        report.messages.append(f"目标时长必须为正数，当前 {target}")
        return out, report

    current = out.video_duration()
    report.stats["before"] = round(current, 3)
    if current <= 0:
        report.ok = False
        report.messages.append("当前总时长为 0，检查 src_dur 是否都是正数")
        return out, report

    if strategy == "speed":
        ratio = current / target          # 需要把每个镜头压到这张比例
        if ratio <= 0:
            report.ok = False
            report.messages.append("时长比非法")
            return out, report
        # 每个镜头在**自己原有** speed 上等比缩放。统一覆盖成同一个值会把
        # 用户特意做的高速慢镜都拉平，那不是微调，是重剪。
        speeds = []
        for seg in out.video_segments:
            seg.speed = round(min(4.0, max(0.25, seg.speed * ratio)), 4)
            speeds.append(seg.speed)
        out, _ = normalize_shotlist(out, trust_existing=False)
        report.achieved = out.video_duration()
        report.drift = report.achieved - target
        span = f"{min(speeds):.3f}-{max(speeds):.3f}" if speeds else "n/a"
        report.actions.append(f"speed ×{ratio:.4f}（范围 {span}）")
        report.ok = abs(report.drift) <= max(0.02, target * tolerance)
        if not report.ok:
            report.messages.append(
                f"speed 已到 0.25x-4x 边界，实际 {report.achieved:.3f}s，"
                f"距目标 {report.drift:+.3f}s")
        return out, report

    # ---- trim：多轮等比收缩/扩张 ----
    headroom = _headroom(out, paths) if current < target else {}
    for _ in range(8):
        current = out.video_duration()
        drift = target - current
        if abs(drift) <= max(0.02, target * tolerance):
            break
        if drift < 0:
            # 需要变短：等比缩，但不低于 min_shot
            factor = target / current
            shrunk = 0.0
            for i, seg in enumerate(out.video_segments):
                want = max(min_shot, seg.src_dur * factor)
                shrunk += seg.src_dur - want
                seg.src_dur = round(want, 4)
            report.actions.append(f"等比缩短 {abs(drift):.3f}s（本镜共缩 "
                                  f"{shrunk:.3f}s）")
            if shrunk < 1e-4:
                report.messages.append("所有镜头都已到 min_shot 下限，无法继续缩短")
                break
        else:
            # 需要变长：优先用每个镜头素材里还没用掉的部分
            gained = _extend_by_headroom(out, headroom, drift)
            report.actions.append(f"延长 {gained:.3f}s（目标还差 {drift:.3f}s）")
            if gained < 1e-4:
                report.messages.append(
                    f"素材余量只够再撑 {gained:.3f}s，距目标还差 {drift:.3f}s")
                break
        out, _ = normalize_shotlist(out, trust_existing=False)

    out, _ = normalize_shotlist(out, trust_existing=False)
    report.achieved = out.video_duration()
    report.drift = report.achieved - target
    report.shots = len(out.video_segments)
    report.stats["after"] = round(report.achieved, 3)
    report.ok = abs(report.drift) <= max(0.02, target * tolerance)
    if not report.ok and not report.messages:
        report.messages.append(f"偏差 {report.drift:+.3f}s 超出容差 "
                               f"±{max(0.02, target * tolerance):.3f}s")
    return out, report


def _headroom(sl: Shotlist, paths: Optional[config.Paths]) -> Dict[int, float]:
    """每个镜头在自己素材里还能往后拉多少秒（-1 表示未知）。"""
    paths = paths or config.discover()
    out: Dict[int, float] = {}
    cache: Dict[str, float] = {}
    for i, seg in enumerate(sl.video_segments):
        path = sl.resolve_material(seg.file)
        if path not in cache:
            info = media.probe(path, paths) if os.path.exists(path) else None
            cache[path] = info.duration if info and info.duration else -1.0
        total = cache[path]
        out[i] = (total - seg.src_end) if total > 0 else -1.0
    return out


def _extend_by_headroom(sl: Shotlist, headroom: Dict[int, float],
                        want: float) -> float:
    """按余量把 ``want`` 秒摊给各镜头，返回实际拿到的秒数。"""
    usable = {i: h for i, h in headroom.items() if h > 0.01}
    if not usable:
        return 0.0
    total = sum(usable.values())
    gained = 0.0
    for i, h in usable.items():
        share = min(h, want * (h / total)) if total else 0.0
        if share <= 0:
            continue
        sl.video_segments[i].src_dur = round(sl.video_segments[i].src_dur + share, 4)
        gained += share
    return gained


# --------------------------------------------------------------------------- #
# 从素材自动组片
# --------------------------------------------------------------------------- #
def plan_from_materials(
    material_dir: str,
    *,
    target_duration: float = 45.0,
    canvas: Optional[Canvas] = None,
    fps: float = 30.0,
    name: str = "auto_plan",
    scene_threshold: float = 0.4,
    min_shot: float = 1.2,
    max_shot: float = 6.0,
    paths: Optional[config.Paths] = None,
) -> Tuple[Shotlist, PlanReport]:
    """扫一个素材目录，用场景切点切出候选镜头，凑成 ``target_duration``。

    **它不懂画面内容**：会不会挑到废片取决于素材本身是否干净。它只保证
    ①相邻镜头不来自同一段连续素材 ②单镜长度在 ``min_shot..max_shot`` 之间
    ③总时长贴近目标。要「挑好镜头」请在分镜表里人工指定——这也是本项目
    坚持「分镜表是主入口、自动组片是辅助」的原因。
    """
    paths = paths or config.discover()
    report = PlanReport(target=float(target_duration))
    canvas = canvas or Canvas()

    files = media.scan_media(material_dir, recursive=True,
                             exts=media.VIDEO_EXT)
    if not files:
        report.ok = False
        report.messages.append(f"目录里没有视频素材: {material_dir}")
        return Shotlist(name=name, canvas=canvas, fps=fps), report

    candidates: List[Tuple[str, float, float]] = []   # (file, src_start, src_dur)
    for path in files:
        info = media.probe(path, paths)
        if not info.has_video or info.duration <= 0:
            continue
        cuts = [0.0] + media.scene_cuts(path, scene_threshold, paths) + [info.duration]
        cuts = sorted(set(round(c, 3) for c in cuts if 0 <= c <= info.duration))
        pieces = []
        for a, b in zip(cuts, cuts[1:]):
            length = b - a
            if length < min_shot:
                continue
            # 太长的段按 max_shot 均分，避免一个镜头占满半条片子
            n = max(1, int(round(length / max_shot)))
            step = length / n
            if step < min_shot:
                n, step = 1, length
            for k in range(n):
                pieces.append((path, round(a + k * step, 3), round(step, 3)))
        candidates.extend(pieces)
        report.stats.setdefault("per_file", {})[os.path.basename(path)] = len(pieces)

    if not candidates:
        report.ok = False
        report.messages.append(
            f"切不出符合 {min_shot}-{max_shot}s 的镜头，试着调小 min_shot 或降低 "
            f"scene_threshold（当前 {scene_threshold}）")
        return Shotlist(name=name, canvas=canvas, fps=fps), report

    picked = _round_robin(candidates, target_duration)
    segs = [
        VideoSegment(file=os.path.abspath(f), src_start=s, src_dur=d,
                     zoom="none", desc=f"auto {i:02d}")
        for i, (f, s, d) in enumerate(picked)
    ]
    sl = Shotlist(
        name=name, canvas=canvas, fps=fps,
        material_dir=os.path.abspath(material_dir),
        video_segments=segs,
        styles=default_styles(canvas),
    )
    sl, _ = normalize_shotlist(sl, trust_existing=False)
    sl, fit = fit_duration(sl, target_duration, paths=paths)
    report.ok = fit.ok
    report.achieved = fit.achieved
    report.drift = fit.drift
    report.shots = len(sl.video_segments)
    report.actions.extend(fit.actions)
    report.messages.extend(fit.messages)
    report.stats["candidates"] = len(candidates)
    report.stats["files"] = len(files)
    return sl, report


def _round_robin(candidates: Sequence[Tuple[str, float, float]],
                 target: float) -> List[Tuple[str, float, float]]:
    """按文件轮转取镜头，凑到目标时长——避免成片全是同一个机位。"""
    by_file: Dict[str, List[Tuple[str, float, float]]] = {}
    for c in candidates:
        by_file.setdefault(c[0], []).append(c)
    queues = [sorted(v, key=lambda x: x[1]) for v in by_file.values()]
    for q in queues:
        q.reverse()          # 用 pop() 从前往后取
    picked: List[Tuple[str, float, float]] = []
    total = 0.0
    while queues and total < target:
        for q in list(queues):
            if not q:
                queues.remove(q)
                continue
            item = q.pop()
            picked.append(item)
            total += item[2]
            if total >= target:
                break
        if all(not q for q in queues):
            break
    return picked


# --------------------------------------------------------------------------- #
# Planner：把上面几步串起来，给 CLI 和 Agent 用的门面
# --------------------------------------------------------------------------- #
class Planner:
    """面向调用方的门面。所有方法都返回 ``(新分镜表, 报告)``。"""

    def __init__(self, paths: Optional[config.Paths] = None):
        self.paths = paths or config.discover()

    def normalize(self, sl: Shotlist) -> Tuple[Shotlist, PlanReport]:
        out = _with_default_styles(sl)
        out, actions = normalize_shotlist(out)
        return out, PlanReport(ok=True, achieved=out.video_duration(),
                               shots=len(out.video_segments), actions=actions)

    def fit(self, sl: Shotlist, target: float, **kw) -> Tuple[Shotlist, PlanReport]:
        out = _with_default_styles(sl)
        return fit_duration(out, target, paths=self.paths, **kw)

    def from_materials(self, material_dir: str, **kw) -> Tuple[Shotlist, PlanReport]:
        kw.setdefault("paths", self.paths)
        return plan_from_materials(material_dir, **kw)
