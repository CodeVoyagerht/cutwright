"""cutwright · 剪匠 —— 分镜表进，成片出。

一份 ``shotlist.json`` 描述你想怎么剪，cutwright 把它翻译成三种目标之一：

    * 剪映草稿（可直接在剪映里继续手调）
    * 达芬奇时间线（出来就能调色、套 LUT）
    * 一条 MP4（无 NLE 依赖，CI 可测）

典型的三种用法::

    import cutwright

    sl = cutwright.load_shotlist("shotlist.json")
    issues = cutwright.validate(sl)

    tl = cutwright.Timeline.from_shotlist(sl)
    print(tl.summary())

    from cutwright.backends import get_backend
    res = get_backend("ffmpeg").build(tl, out_dir="./out")

命令行入口见 ``cutwright.cli``。
"""
from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "__version__",
    "Canvas", "VideoSegment", "TextSegment", "VoSegment", "AudioSegment",
    "ImageOverlay", "Act", "TextStyle", "Shotlist", "Issue",
    "load_shotlist", "load_shotlist_str", "validate", "SCHEMA_VERSION",
    "Timeline", "TimelineClip", "TimelineText", "TimelineAudio",
    "TimelineOverlay", "TimelineError",
    "Planner", "plan_from_materials", "normalize_shotlist", "fit_duration",
    "qc", "Issue",
]

from .schema import (  # noqa: E402
    SCHEMA_VERSION,
    Act,
    AudioSegment,
    Canvas,
    ImageOverlay,
    Issue,
    Shotlist,
    TextSegment,
    TextStyle,
    VideoSegment,
    VoSegment,
    load_shotlist,
    load_shotlist_str,
    validate,
)
from .timeline import (  # noqa: E402
    Timeline,
    TimelineAudio,
    TimelineClip,
    TimelineError,
    TimelineOverlay,
    TimelineText,
)
from .planner import (  # noqa: E402
    Planner,
    fit_duration,
    normalize_shotlist,
    plan_from_materials,
)
from . import qc  # noqa: E402
