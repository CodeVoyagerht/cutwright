"""剧本 → 分镜表。

``screenplay.py`` 只负责把剧本读成结构；这里负责把它排成分镜表——也就是
「这场戏挑哪几段素材、每段多长、字幕什么时候出现」。做完之后交给
``planner``/``build``，和手写分镜表走的是同一条链路，所以三条产线都自动支持。

三条硬规则（都是踩过坑之后加的）：

1. **帧网格**：所有时长先换算成整数帧再分配，最后求和精确等于总帧数。
   直接用浮点秒累加，30fps 下 200 个镜头会漂出半秒，字幕和口型就对不上了。
2. **相邻不同源**：两个连续镜头不许来自同一个文件的同一段——除非素材池里
   真的只剩这一个来源（会记一条 warning，而不是静默照做）。
3. **台词时长优先于字数估算**：给了旁白音频就按音频真实时长排，别再猜。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import media
from .planner import default_styles
from .schema import (Act, AudioSegment, Canvas, Shotlist, TextSegment,
                     TextStyle, VideoSegment, VoSegment)

# --------------------------------------------------------------------------- #
# 素材索引
# --------------------------------------------------------------------------- #
class StoryError(ValueError):
    """剧本本身没法排片（不是「排得不好看」）。"""


@dataclass
class Piece:
    """素材里可用的一个镜头片段。"""

    file: str
    start: float
    dur: float
    tags: List[str] = field(default_factory=list)
    who: List[str] = field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], base_dir: str = "") -> "Piece":
        file = str(raw.get("file") or "")
        if file and base_dir and not os.path.isabs(file):
            file = os.path.normpath(os.path.join(base_dir, file))
        return cls(
            file=file,
            start=float(raw.get("start") or 0.0),
            dur=float(raw.get("dur") or raw.get("duration") or 0.0),
            tags=[str(t).strip().lower() for t in (raw.get("tags") or []) if str(t).strip()],
            who=[str(w).strip() for w in (raw.get("who") or []) if str(w).strip()],
            note=str(raw.get("note") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"file": self.file, "start": round(self.start, 3),
                               "dur": round(self.dur, 3)}
        if self.tags:
            out["tags"] = list(self.tags)
        if self.who:
            out["who"] = list(self.who)
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class MaterialIndex:
    """素材库：一段素材被切成若干可挑选的镜头，每个镜头带标签。"""

    pieces: List[Piece] = field(default_factory=list)
    source: str = ""

    def __len__(self) -> int:
        return len(self.pieces)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "source": self.source,
            "pieces": [piece.to_dict() for piece in self.pieces],
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], base_dir: str = "") -> "MaterialIndex":
        items = raw.get("pieces") or raw.get("clips") or []
        return cls(pieces=[Piece.from_dict(item, base_dir) for item in items],
                   source=str(raw.get("source") or ""))

    @classmethod
    def from_json(cls, path: str) -> "MaterialIndex":
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        return cls.from_dict(raw, os.path.dirname(os.path.abspath(path)))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)

    # -- 自动建库 ---------------------------------------------------------- #
    @classmethod
    def from_dir(cls, directory: str, *, scene_threshold: float = 0.4,
                 min_shot: float = 0.8, max_shot: float = 6.0,
                 paths: Optional[Any] = None) -> "MaterialIndex":
        """扫目录、按场景切点切镜头。**它不懂画面内容**，标签只能从文件名来。

        写进索引的路径是绝对路径：索引会被 ``story`` 再读一遍，而读它的进程不一定
        在同一个工作目录下——相对路径在这里是一个「换个目录就全丢」的坑。
        """
        files = media.scan_media(directory, recursive=True, exts=media.VIDEO_EXT)
        pieces: List[Piece] = []
        for path in files:
            path = os.path.abspath(path)
            info = media.probe(path, paths)
            if not info.has_video or info.duration <= 0:
                continue
            tags = _tokens_from_filename(path)
            cuts = [0.0] + media.scene_cuts(path, scene_threshold, paths) + [info.duration]
            cuts = sorted({round(c, 3) for c in cuts if 0 <= c <= info.duration})
            for start, end in zip(cuts, cuts[1:]):
                span = end - start
                if span < min_shot:
                    continue
                count = max(1, int(round(span / max_shot)))
                step = span / count
                if step < min_shot:
                    count, step = 1, span
                for k in range(count):
                    pieces.append(Piece(file=path, start=round(start + k * step, 3),
                                        dur=round(step, 3), tags=list(tags)))
        return cls(pieces=pieces, source=os.path.abspath(directory))


def _tokens_from_filename(path: str) -> List[str]:
    """``S01_subway_night_wide_take2.mp4`` → ``["s01", "subway", "night", "wide"]``。

    去掉拖尾的 ``take2``/``001`` 这类序号——它们对挑镜头没有信息量。
    """
    # 两种分隔符都当分隔符：索引可能在 Windows 上建、在 Linux 上读，
    # 反过来也一样，别让 `C:\x\S01_night.mp4` 在那台机器上变成一个长标签。
    text = str(path).replace("\\", "/")
    name = text.rstrip("/").rsplit("/", 1)[-1]
    stem = os.path.splitext(name)[0]
    raw = []
    token = ""
    for ch in stem:
        if ch.isalnum() or ch in "-_":
            token += ch
        else:
            if token:
                raw.append(token)
            token = ""
    if token:
        raw.append(token)
    out: List[str] = []
    for item in raw:
        for part in item.replace("-", "_").split("_"):
            part = part.strip().lower()
            if not part:
                continue
            if part.isdigit():
                continue
            if part.startswith("take") and part[4:].isdigit():
                continue
            out.append(part)
    return out


# --------------------------------------------------------------------------- #
# 选项与报告
# --------------------------------------------------------------------------- #
@dataclass
class StoryOptions:
    """把剧本排成分镜表的旋钮。默认值按竖屏短剧的节奏定的。"""

    cps_dialogue: float = 4.6        # 中文字/秒（对白）
    cps_voiceover: float = 4.2       # 旁白通常念得稳一点
    action_seconds: float = 1.6      # 动作行没有台词，给一个默认镜头长度
    title_seconds: float = 1.4       # 场标卡
    pause_default: float = 0.12      # 每句后面留的气口
    min_shot: float = 1.0
    max_shot: float = 3.6
    rhythm: bool = True              # 同一句拆多镜时长短交错，别像机器排的
    captions: bool = True
    vo_captions: bool = False        # 旁白要不要也出字幕（默认不出，画外音抢戏）
    caption_lead: float = 0.0        # 字幕比镜头晚多少秒出现
    speaker_prefix: bool = False      # 字幕前加「名字：」
    music_gain_db: float = -18.0
    music_file: str = ""
    music_fade_out: float = 1.0
    target_duration: Optional[float] = None
    paths: Optional[Any] = None


@dataclass
class StoryReport:
    ok: bool = True
    scenes: int = 0
    lines: int = 0
    shots: int = 0
    duration: float = 0.0
    per_scene: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    # 素材复用：{文件名: 用了几次}，按次数降序、最多 5 条。
    # 素材数远少于镜头数时，真正决定观感的就是这张表——7 次和 2 次看着不一样。
    reuse: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "scenes": self.scenes,
            "lines": self.lines,
            "shots": self.shots,
            "duration": round(self.duration, 3),
            "per_scene": self.per_scene,
            "warnings": self.warnings,
            "unresolved": self.unresolved,
            "reuse": self.reuse,
        }

    def summary(self) -> str:
        head = (f"{self.scenes} 场 / {self.lines} 句 / {self.shots} 镜 / "
                f"{self.duration:.2f}s")
        if self.warnings:
            head += f"，{len(self.warnings)} 条警告"
        return head


# --------------------------------------------------------------------------- #
# 排片
# --------------------------------------------------------------------------- #
def build_shotlist(screenplay: Any, index: MaterialIndex,
                   options: Optional[StoryOptions] = None
                   ) -> Tuple[Shotlist, StoryReport]:
    """剧本 + 素材库 → 分镜表。返回值里的第二项是给人看的报告。"""
    options = options or StoryOptions()
    fps = float(screenplay.fps or 0.0)
    if fps <= 0:
        raise StoryError("剧本 fps 非法（不能排片）：先修 S003 再重试")
    report = StoryReport(scenes=len(screenplay.scenes))
    report.lines = sum(len(scene.lines) for scene in screenplay.scenes)

    videos: List[VideoSegment] = []
    texts: List[TextSegment] = []
    vos: List[VoSegment] = []
    audios: List[AudioSegment] = []
    acts: List[Act] = []
    styles: Dict[str, TextStyle] = default_styles(screenplay.canvas)
    styles.update(screenplay.styles)

    frame = 0                      # 游标：帧，用来算时长分配
    clock = 0.0                    # 游标：秒，用来写 tl_start（两者必须同步推进）
    prev_key: Optional[Tuple[str, float]] = None
    used: Dict[Tuple[str, float], int] = {}

    for scene in screenplay.scenes:
        scene_start_frame = frame
        for line in scene.lines:
            seconds = _line_seconds(line, screenplay, options, report)
            line_frames = max(1, int(round(seconds * fps)))
            chunks = _allocate_frames(line_frames, options, fps)

            placed = 0          # 帧数，用来判断这句到底排上没有
            placed_shots = 0    # 镜数，用来回看「这句占用了哪几镜」
            for chunk in chunks:
                shot_seconds = chunk / fps
                piece, speed = _pick_piece(index, scene, line, shot_seconds,
                                           prev_key, used, report)
                if piece is None:
                    # 没素材也不许留空：留一段黑场会被 qc 抓，但比整条片子断掉好
                    report.ok = False
                    report.unresolved.append(
                        f"{scene.id or '?'}「{line.text[:12]}」找不到可用素材")
                    break
                used[(piece.file, piece.start)] = used.get((piece.file, piece.start), 0) + 1
                prev_key = (piece.file, piece.start)

                if speed == 1.0:
                    src_dur = round(min(shot_seconds, piece.dur), 4)
                    speed_used = 1.0
                else:
                    src_dur = round(piece.dur, 4)
                    speed_used = round(src_dur / shot_seconds, 6)

                videos.append(VideoSegment(
                    file=piece.file,
                    src_start=round(piece.start, 3),
                    src_dur=src_dur,
                    tl_start=round(clock, 4),
                    speed=speed_used,
                    act=scene.id,
                    shot=f"{scene.id}-{len(videos):03d}",
                    desc=(line.text[:24] or piece.note),
                ))
                # 时间线按「上一镜的结束」推进，而不是按帧网格——两者差 1e-5 级，
                # 但 validate 是拿 tl_start/tl_dur 互相校对的，差一点就报警。
                clock = round(clock + src_dur / speed_used, 6)
                frame += chunk
                placed += chunk
                placed_shots += 1

            if placed == 0:              # 一句都没排上（没素材）
                frame += line_frames
                continue
            # 这一句占了几镜就回看几镜——长台词会被拆成 2–3 镜，
            # 字幕必须从**第一镜**开始，否则它会压到下一句上（实测重叠过 2.1s）。
            line_videos = videos[-placed_shots:]
            line_start = line_videos[0].tl_start
            line_dur = round(sum(v.src_dur / (v.speed or 1.0)
                                for v in line_videos), 4)

            spoken = line.text
            if options.speaker_prefix and line.who and line.type == "dialogue":
                spoken = f"{screenplay.speaker_name(line.who)}：{line.text}"

            if options.captions and line.speaks and spoken:
                texts.append(TextSegment(
                    text=spoken,
                    tl_start=round(line_start + options.caption_lead, 4),
                    tl_dur=line_dur,
                    style=line.caption_style,
                ))
            elif line.type == "voiceover" and spoken:
                # 旁白占位在 vo 轨上（剪映里好对齐）。默认不出字幕，但轨道要有东西，
                # 否则「这句旁白配了多久的画面」在下游工具里就丢了。
                vos.append(VoSegment(
                    text=spoken,
                    tl_start=line_start,
                    tl_dur=line_dur,
                    act=scene.id,
                    style="vo_hidden" if not options.vo_captions else "caption",
                ))

            audio = _line_audio(line, screenplay, options, line_start, line_dur, report)
            if audio is not None:
                audios.append(audio)

        acts.append(Act(key=scene.id, name=scene.slug or scene.id,
                        start=round(scene_start_frame / fps, 4),
                        dur=round((frame - scene_start_frame) / fps, 4)))
        report.per_scene.append({
            "id": scene.id,
            "slug": scene.slug,
            "shots": sum(1 for v in videos if v.act == scene.id),
            "dur": round((frame - scene_start_frame) / fps, 3),
        })

    total = frame / fps
    counts: Dict[str, int] = {}
    for item in videos:
        key = os.path.basename(item.file)
        counts[key] = counts.get(key, 0) + 1
    report.reuse = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5])
    music = _music(screenplay, options, total, report)
    if music is not None:
        audios.append(music)

    shotlist = Shotlist(
        name=screenplay.name,
        material_dir="",
        asset_dir="",
        canvas=screenplay.canvas,
        fps=fps,
        total_duration=round(total, 4),
        acts=acts,
        video_segments=videos,
        text_segments=texts,
        vo_segments=vos,
        audio_segments=audios,
        styles=styles,
    )
    report.shots = len(videos)
    report.duration = round(total, 4)
    if not videos:
        report.ok = False
        report.warnings.append("一个镜头都没排上——检查素材库是否为空，或剧本标签与素材对不上")
    return shotlist, report


# --------------------------------------------------------------------------- #
# 内部：时长、分配、挑素材
# --------------------------------------------------------------------------- #
def _line_seconds(line: Any, screenplay: Any, options: StoryOptions,
                  report: StoryReport) -> float:
    """这句占多长时间。优先级：显式 duration > 音频真实时长 > 字数估算。"""
    pause = options.pause_default if line.pause == 0 else line.pause
    if line.duration:
        return float(line.duration)

    if line.vocal:
        path = screenplay.resolve_vo(line.vocal)
        if os.path.isfile(path):
            info = media.probe(path, options.paths)
            if info.duration > 0:
                return round(info.duration + pause, 4)
        else:
            report.warnings.append(f"旁白音频不存在，按字数估算: {path}")
    elif line.type == "voiceover":
        voice_dir = getattr(screenplay, "vo_dir", "")
        if voice_dir:
            candidate = screenplay.resolve_vo(f"{_slug(line.text)}.wav")
            if os.path.isfile(candidate):
                info = media.probe(candidate, options.paths)
                if info.duration > 0:
                    return round(info.duration + pause, 4)

    if line.type == "title":
        return options.title_seconds
    if line.type == "action" and not line.text:
        return options.action_seconds
    chars = len(line.text.replace(" ", ""))
    if line.type == "action":
        return round(max(options.min_shot, chars / (options.cps_dialogue * 1.25)) + pause, 4)
    cps = options.cps_voiceover if line.type == "voiceover" else options.cps_dialogue
    return round(max(options.min_shot * 0.8, chars / cps) + pause, 4)


def _slug(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())[:24]


def _allocate_frames(total_frames: int, options: StoryOptions,
                     fps: float) -> List[int]:
    """把一句的总帧数拆成若干镜头的帧数，**求和精确等于 total_frames**。

    用「最大余数法」：先按节奏权重算理想帧数，取整后把剩下的帧补给余数最大的
    那几个——这样既有长短交错，又不会因为四舍五入漂移。
    """
    max_frames = max(1, int(round(options.max_shot * fps)))
    min_frames = max(1, int(round(options.min_shot * fps)))
    if total_frames <= max_frames:
        return [total_frames]

    count = max(1, int(math.ceil(total_frames / max_frames)))
    # 太碎就少拆几刀：整句 2 秒时拆成 2 段每段 1 秒，不如一段
    while count > 1 and total_frames / count < min_frames:
        count -= 1

    # 权重是相对的，拆得多不代表每段都落到 max_shot 以内：节奏系数 1.18 会让某段
    # 冲过上限。宁可多拆一刀，也不让任何一镜超过 max_shot——那是分辨率的上限，
    # 不是偏好。
    while True:
        weights = [1.0]
        pattern = (0.82, 1.18, 0.94, 1.06)
        for i in range(1, count):
            weights.append(pattern[(i - 1) % len(pattern)] if options.rhythm else 1.0)
        scale = total_frames / sum(weights)
        ideal = [w * scale for w in weights]
        floors = [int(math.floor(x)) for x in ideal]
        remainder = total_frames - sum(floors)
        order = sorted(range(count), key=lambda i: ideal[i] - floors[i], reverse=True)
        for i in order[:remainder]:
            floors[i] += 1
        if max(floors) <= max_frames or count >= total_frames:
            break
        count += 1
    # 兜底：任何一段都不许是 0 帧
    for i in range(len(floors)):
        if floors[i] <= 0:
            donor = max(range(len(floors)), key=lambda k: floors[k])
            if floors[donor] > 1:
                floors[donor] -= 1
                floors[i] = 1
    return floors


def _pick_piece(index: MaterialIndex, scene: Any, line: Any, needed: float,
                prev_key: Optional[Tuple[str, float]],
                used: Dict[Tuple[str, float], int],
                report: StoryReport) -> Tuple[Optional[Piece], float]:
    """挑一个能撑满 ``needed`` 秒的镜头，返回 (片段, 速度)。

    打分权重是调过的，别随手改——第一版把「避免重复用同一段」罚得比「标签命中」
    还重，结果同一场戏里宁可跳到别的场的素材，也不肯复用本场的镜头。

    硬规则：**相邻两镜不许是同一段素材**。分数调不动的时候靠这条兜底——从候选里
    排掉上一段再选第二好的，而不是指望权重刚好压住。
    """
    scene_tags = {tag.lower() for tag in getattr(scene, "tags", [])}
    line_tags = {tag.lower() for tag in getattr(line, "tags", [])}
    wanted = scene_tags | line_tags

    scored: List[Tuple[float, Piece, float]] = []
    for piece in index.pieces:
        if piece.dur <= 0.05:
            continue
        speed = 1.0
        if piece.dur + 1e-6 < needed:
            # 素材比需要的短：慢放补足。慢到 0.26 倍以下就糊了，直接放弃这段。
            speed = round(piece.dur / needed, 4)
            if speed < 0.26 or piece.dur / speed < needed - 0.02:
                continue

        piece_tags = {t.lower() for t in piece.tags}
        score = 3.0 * len(wanted & piece_tags)
        if line.who:
            score += 4.0 if line.who in piece.who else -3.0
        if scene_tags and not (scene_tags & piece_tags):
            score -= 3.0                       # 整场戏的标签一个都没对上
        score -= 1.0 * used.get((piece.file, piece.start), 0)
        score -= 0.35 * abs(piece.dur - needed)
        if speed != 1.0:
            score -= 1.2
        if prev_key is not None and (piece.file, piece.start) == prev_key:
            score -= 12.0                      # 只是惩罚；真正的兜底在下面
        scored.append((score, piece, speed))

    if not scored:
        return None, 1.0

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best, best_speed = scored[0]
    if prev_key is not None and (best.file, best.start) == prev_key and len(scored) > 1:
        best_score, best, best_speed = scored[1]

    if wanted and not (wanted & {t.lower() for t in best.tags}):
        report.warnings.append(
            f"{scene.id or '?'} 的标签 {sorted(wanted)[:4]} 没有任何素材命中，退而用 "
            f"{os.path.basename(best.file)}")
    if prev_key is not None and (best.file, best.start) == prev_key:
        report.warnings.append(
            f"{scene.id or '?'} 连着用了同一个片段 {os.path.basename(best.file)}"
            f"（素材池只有这一段能撑满 {needed:.2f}s）")
    return best, best_speed


def _line_audio(line: Any, screenplay: Any, options: StoryOptions,
                start_seconds: float, dur_seconds: float,
                report: StoryReport) -> Optional[AudioSegment]:
    """把对白/旁白的音频挂到时间线上（有的话）。"""
    if not line.vocal:
        return None
    path = screenplay.resolve_vo(line.vocal)
    if not os.path.isfile(path):
        return None
    info = media.probe(path, options.paths)
    dur = info.duration or dur_seconds
    return AudioSegment(file=path, tl_start=round(start_seconds, 4),
                        tl_dur=round(min(dur, dur_seconds), 4), kind="vo")


def _music(screenplay: Any, options: StoryOptions, total: float,
           report: StoryReport) -> Optional[AudioSegment]:
    spec = dict(getattr(screenplay, "music", {}) or {})
    file = str(spec.get("file") or options.music_file or "")
    if not file:
        return None
    if not os.path.isabs(file):
        base = getattr(screenplay, "base_dir", "") or "."
        resolved = os.path.join(base, file)
        file = resolved if os.path.isfile(resolved) else file
    if not os.path.isfile(file):
        report.warnings.append(f"配乐文件不存在，已跳过: {file}")
        return None
    gain = float(spec.get("gain_db", options.music_gain_db))
    return AudioSegment(file=os.path.abspath(file), tl_start=0.0,
                        tl_dur=round(total, 4), gain_db=gain, kind="music")


def read_index(screenplay: Any, index_path: str = "",
               material_dir: str = "", options: Optional[StoryOptions] = None
               ) -> MaterialIndex:
    """优先用现成索引；没有就扫目录建一个（会调用 ffmpeg 切点检测）。"""
    if index_path:
        return MaterialIndex.from_json(index_path)
    directory = material_dir or getattr(screenplay, "material_dir", "")
    if not directory:
        return MaterialIndex()
    base = getattr(screenplay, "base_dir", "") or ""
    if base and not os.path.isabs(directory):
        candidate = os.path.join(base, directory)
        if os.path.isdir(candidate):
            directory = candidate
    options = options or StoryOptions()
    return MaterialIndex.from_dir(directory, paths=options.paths)
