"""达芬奇 Resolve 后端：把 :class:`~cutwright.timeline.Timeline` 建成 Resolve 时间线。

分工：

* :mod:`~cutwright.backends.resolve.bridge` —— 脚本 API 的加载与路径探测；
* :mod:`~cutwright.backends.resolve.timeline` —— 真正的时间线构建与报告；
* 本模块 —— 把它包成 :class:`~cutwright.backends.base.Backend`，
  落报告 JSON、收拢错误。

本后端**只加时间线，不删不改用户已有的东西**；干不了的活（字幕）在报告里
明说，见 :data:`~cutwright.backends.resolve.timeline.SRT_NOTE`。
"""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..base import Backend, BuildResult, register
from .bridge import ResolveBridge, ResolveError
from .timeline import build_timeline

if TYPE_CHECKING:  # 只给类型检查用，运行期不额外 import
    from ...config import Paths
    from ...timeline import Timeline

__all__ = ["ResolveBackend", "REPORT_SUFFIX"]

# 报告文件的后缀。完整文件名是 ``{timeline.name}{REPORT_SUFFIX}``
# 即 ``我的片子.resolve.report.json``——和后端名绑定，一眼看出是谁产的。
REPORT_SUFFIX = ".resolve.report.json"


@register
class ResolveBackend(Backend):
    """生成达芬奇 Resolve 时间线。

    可用性判断走 :meth:`ResolveBridge.available`：脚本模块与 fusionscript 库
    **部件齐备**就算可用，它刻意**不 import** 那个模块——本机实测在 Resolve
    没运行时 import ``fusionscript`` 会让进程以 0xC0000005 直接退出，``doctor``
    不能冒这个险。真正的连接问题在 ``build()`` 里变成
    ``BuildResult.ok = False``，而不是让整条流水线崩掉。
    """

    name = "resolve"
    description = "生成达芬奇 Resolve 时间线"

    def __init__(self, paths: Optional["Paths"] = None,
                 options: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(paths, options)
        self.bridge = ResolveBridge(self.paths)

    # -- 能力 -------------------------------------------------------------- #
    def available(self) -> bool:
        """脚本 API 装了没有。只 import 模块，不连 Resolve。"""
        try:
            return self.bridge.available()
        except Exception:      # 探测本身不该成为崩溃点
            return False

    def why_unavailable(self) -> str:
        """不可用的原因 + 该怎么修。把探测到的路径一起写出来，省一轮来回。"""
        if self.available():
            return ("脚本 API 可用；若 build 仍失败，通常是 Resolve 没在运行、"
                    "或 Resolve 里没有打开的工程")
        lines: List[str] = [self.bridge.last_error or "无法加载达芬奇脚本 API"]
        try:
            info = self.bridge.diagnose()
        except Exception:      # diagnose 只是锦上添花
            info = {}
        if info:
            lines.append("探测结果：")
            for key, label in (("support", "Support 目录"),
                               ("scripting", "Scripting 目录"),
                               ("modules", "Modules 目录"),
                               ("lib", "fusionscript 库"),
                               ("lut_dir", "LUT 目录"),
                               ("bundled_python", "Resolve 自带 Python")):
                lines.append(f"  {label}: {info.get(key) or '未找到'}")
        if info:
            lines.extend(self._env_suggestions(info))
        lines.append(
            "请依次检查："
            "1) 装的是 DaVinci Resolve Studio（免费版只有应用内脚本，外部 Python 调不到）；"
            "2) Resolve 的 Preferences > System > General 里把 External scripting using "
            "设为 Local（免费版没有这一项）；"
            "3) 设置 RESOLVE_SCRIPT_API 指向 ...\\DaVinci Resolve\\Support\\Developer\\Scripting，"
            "RESOLVE_SCRIPT_LIB 指向 ...\\fusionscript.dll；"
            "4) 启动 Resolve 并打开一个工程。")
        return "\n".join(lines)

    # -- 构建 -------------------------------------------------------------- #
    def build(self, timeline: "Timeline", out_dir: str) -> BuildResult:
        """把时间线建进 Resolve，把报告写到 ``out_dir``。

        ``options`` 里认的键见
        :func:`~cutwright.backends.resolve.timeline.build_timeline`：
        ``dry_run`` / ``timeline_name`` / ``lut`` / ``track_index``（以及
        ``lut_node`` / ``timestamp`` / ``start_frame``）。
        ``out_dir`` 会覆盖 ``options["out_dir"]``——调用方给的那个才算数。

        失败（连不上 Resolve、没有工程、追加被拒……）一律转成
        ``ok = False`` + 可操作的 ``error``，**不往外抛**：后端是流水线的一环，
        一个后端不可用不该炸掉整次运行。此时不写任何文件，``data`` 里带
        一份 ``diagnostics`` 供排查。
        """
        options: Dict[str, Any] = dict(self.options or {})
        options["out_dir"] = out_dir
        self.ensure_out_dir(out_dir)

        try:
            report = build_timeline(self.bridge, timeline, options=options)
        except ResolveError as exc:
            return self._failure(out_dir, timeline, exc, hint=exc.hint)
        except Exception as exc:       # 兜底：不把意外异常甩给调用方
            return self._failure(out_dir, timeline, exc,
                                 hint="这是未预期的错误，请把上下文反馈给维护者")

        artifacts: List[str] = []
        report_path = os.path.join(out_dir, f"{timeline.name}{REPORT_SUFFIX}")
        report["report_path"] = report_path
        try:
            with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            artifacts.append(report_path)
        except Exception as exc:       # OSError / 不可序列化都归到这里
            return self._failure(
                out_dir, timeline, exc,
                hint=f"写报告到 {report_path} 失败，检查 out_dir 是否可写")

        srt_path = (report.get("texts") or {}).get("path")
        if srt_path and os.path.isfile(srt_path):
            artifacts.append(str(srt_path))

        return BuildResult(backend=self.name, ok=True, artifacts=artifacts,
                           data=report,
                           warnings=list(report.get("warnings") or []))

    # -- 内部 -------------------------------------------------------------- #
    @staticmethod
    def _env_suggestions(info: Dict[str, Any]) -> List[str]:
        """把探测到的路径翻译成「复制粘贴就能用」的环境变量建议。

        只给建议、不替用户设：``RESOLVE_SCRIPT_LIB`` 一旦写进环境变量，在
        Resolve 没运行时下一次 import 就会把进程打崩（0xC0000005），这个决定
        必须由用户在自己确认 Resolve 已启动之后做。本机就是这种布局——
        Resolve 装在 ``D:\\DaVinci Resolve``，官方默认路径下没有
        ``fusionscript.dll``，所以不设这个变量脚本 API 永远加载不起来。
        """
        tips: List[str] = []
        scripting = info.get("scripting")
        if scripting and os.environ.get("RESOLVE_SCRIPT_API") != scripting:
            tips.append(f"  建议设置：RESOLVE_SCRIPT_API={scripting}")
        lib = info.get("lib")
        if lib and os.environ.get("RESOLVE_SCRIPT_LIB") != lib:
            tips.append(f"  建议设置：RESOLVE_SCRIPT_LIB={lib}"
                        "（先启动 Resolve 再设；没启动时 import 会崩）")
        return tips

    def _failure(self, out_dir: str, timeline: "Timeline", exc: BaseException,
                 hint: str = "") -> BuildResult:
        """把异常整理成可操作的 ``BuildResult``（并且**不写文件**）。"""
        message = str(exc) or type(exc).__name__
        if hint and hint not in message:
            message = f"{message}\n提示: {hint}"
        try:
            diagnostics = self.bridge.diagnose()
        except Exception:
            diagnostics = {}
        if diagnostics:
            extra = self._env_suggestions(diagnostics)
            if extra:
                message = message + "\n" + "\n".join(line.strip() for line in extra)
        data: Dict[str, Any] = {
            "timeline_name": timeline.name,
            "error": message,
            "diagnostics": diagnostics,
        }
        return BuildResult(backend=self.name, ok=False, data=data,
                           error=message,
                           warnings=[f"构建失败，未写入任何文件（out_dir={out_dir}）"])
