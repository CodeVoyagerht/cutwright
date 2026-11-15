"""达芬奇 Resolve 后端的单元测试。

**全程 mock：不连接、不启动真实达芬奇。** 测试里唯一出现
``DaVinciResolveScript`` 名字的地方，是把它塞进 ``sys.modules`` 当作替身，
或者故意让它找不到（``available()`` 的负例）。

跑法（项目根目录）::

    python -m unittest discover -s tests -t .

``tests/__init__.py`` 已经把 ``src/`` 塞进 ``sys.path``。
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _import_modules() -> None:
    """把被测模块装进本模块的全局名字，返回 None。

    并行开发期 ``cutwright/__init__.py`` 会引用还没落盘的同级模块
    （``planner`` / ``qc``），于是 ``import cutwright`` 直接抛
    ``ModuleNotFoundError``。这里先试真 import；**只在「缺同级模块」这一种
    情况下**退化成空壳包（只带 ``__path__``），让子模块照常导入——被测代码
    并不依赖顶层包的再导出。其它错误一律原样抛出，不掩盖真 bug。
    """
    try:
        import cutwright  # noqa: F401
    except ModuleNotFoundError as exc:
        if "cutwright." not in str(exc):
            raise
        pkg = types.ModuleType("cutwright")
        pkg.__path__ = [str(_SRC / "cutwright")]
        pkg.__package__ = "cutwright"
        sys.modules["cutwright"] = pkg
        importlib.invalidate_caches()


_import_modules()

ir = importlib.import_module("cutwright.timeline")
schema = importlib.import_module("cutwright.schema")
config = importlib.import_module("cutwright.config")
bridge_mod = importlib.import_module("cutwright.backends.resolve.bridge")
tl_mod = importlib.import_module("cutwright.backends.resolve.timeline")
backend_mod = importlib.import_module("cutwright.backends.resolve")


# --------------------------------------------------------------------------- #
# 临时目录
# --------------------------------------------------------------------------- #
def _scratch_dir(prefix: str) -> str:
    """建一个「可写、可删」的临时目录，返回路径。

    为什么不用 ``tempfile.mkdtemp`` / ``TemporaryDirectory``：它们把目录建成
    0o700，在「写权限按 ACL 授予、且不覆盖新建私有目录」的受限沙箱里，这个
    目录反而写不进去、也删不掉（本机 DSH 沙箱正是如此：``open()`` 直接
    PermissionError）。``os.makedirs`` 建出来的目录继承父目录的 ACL，两边都能用。

    优先系统临时目录，写不进去再退回仓库内的 ``tests/_tmp``；两处都不行就
    跳过测试，而不是让测试因环境而「失败」。
    """
    writable = [tempfile.gettempdir(), str(_ROOT / "tests" / "_tmp")]
    for base in writable:
        path = os.path.join(base, prefix + uuid.uuid4().hex[:8])
        try:
            os.makedirs(path, exist_ok=False)
            probe = os.path.join(path, ".writable")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("x")
            os.remove(probe)
            return path
        except OSError:
            continue
    raise unittest.SkipTest("没有可写的临时目录")


# --------------------------------------------------------------------------- #
# 替身：只实现真的会被调用到的方法，其余一概不提供（方法名写错就 AttributeError）
# --------------------------------------------------------------------------- #
class _FakeGraph:
    """替身 NodeGraph（19.0.0+ 的逐镜 LUT 入口）。"""

    def __init__(self) -> None:
        self.lut_calls: List[Tuple[int, str]] = []

    def GetNumNodes(self) -> int:
        return 2

    def SetLUT(self, node_index: int, lut_path: str) -> bool:
        self.lut_calls.append((int(node_index), lut_path))
        return True


class _FakeMediaPoolItem:
    """替身 MediaPoolItem。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def GetName(self) -> str:
        return os.path.basename(self.path)


class _FakeTimelineItem:
    """替身 TimelineItem（Resolve 19+）：**故意不提供 SetLUT**。

    19.0.0 起 ``TimelineItem.SetLUT()`` / ``GetLUT()`` 已被移除，所以这个替身
    只有 ``GetNodeGraph()``——后端若还在调老 API，这里会直接 AttributeError，
    测试就会把它抓出来。
    """

    def __init__(self, info: Dict[str, Any], start_frame: int = 100) -> None:
        self.info = info
        self._start = start_frame
        self.graph = _FakeGraph()

    def GetStart(self) -> int:
        return self._start

    def GetEnd(self) -> int:
        return self._start + self.GetDuration()

    def GetDuration(self) -> int:
        return int(self.info["endFrame"]) - int(self.info["startFrame"])

    def GetMediaPoolItem(self) -> Any:
        return self.info["mediaPoolItem"]

    def GetNodeGraph(self) -> _FakeGraph:
        return self.graph


class _LegacyTimelineItem(_FakeTimelineItem):
    """替身 TimelineItem（19.0.0 之前）：有 SetLUT、没有 GetNodeGraph。"""

    def __init__(self, info: Dict[str, Any], start_frame: int = 100) -> None:
        super().__init__(info, start_frame)
        self.lut: Optional[str] = None

    def SetLUT(self, lut_path: str) -> bool:      # noqa: N802 —— 故意照官方命名
        self.lut = lut_path
        return True

    def GetNodeGraph(self) -> None:               # noqa: N802
        raise AssertionError("19.0.0 之前不该走 GetNodeGraph()")


class _FakeResolve:
    """``Resolve`` → ``ProjectManager`` → ``Project`` → ``MediaPool`` 的替身链。"""

    def __init__(self, *, item_cls: type = _FakeTimelineItem,
                 current_project: bool = True,
                 refresh_lut: bool = True) -> None:
        self.imported: Dict[str, _FakeMediaPoolItem] = {}
        self.appended: List[_FakeTimelineItem] = []
        self.item_cls = item_cls

        self.media_pool = mock.MagicMock(name="MediaPool")
        self.project = mock.MagicMock(name="Project")
        self.manager = mock.MagicMock(name="ProjectManager")
        self.resolve = mock.MagicMock(name="Resolve")

        self.resolve.GetProjectManager.return_value = self.manager
        self.manager.GetCurrentProject.return_value = (
            self.project if current_project else None)
        self.project.GetName.return_value = "DEMO_PROJECT"
        self.project.GetMediaPool.return_value = self.media_pool
        # 缺方法要真的「缺」：置 None 而不是删属性（MagicMock 会自动重建子属性）
        self.project.RefreshLUTList = (
            mock.MagicMock(return_value=True) if refresh_lut else None)

        self.media_pool.ImportMedia.side_effect = self._import_media
        self.media_pool.CreateEmptyTimeline.return_value = self._new_timeline()
        self.media_pool.AppendToTimeline.side_effect = self._append
        self.project.SetCurrentTimeline.return_value = True
        self.project.GetCurrentTimeline.return_value = \
            self.media_pool.CreateEmptyTimeline.return_value

    # -- 行为 -------------------------------------------------------------- #
    def _import_media(self, paths: List[str]) -> List[_FakeMediaPoolItem]:
        out = []
        for path in paths:
            if path not in self.imported:
                self.imported[path] = _FakeMediaPoolItem(path)
            out.append(self.imported[path])
        return out

    def _new_timeline(self) -> mock.MagicMock:
        timeline = mock.MagicMock(name="Timeline")
        timeline.GetName.return_value = "TL"
        return timeline

    def _append(self, infos: List[Dict[str, Any]]) -> List[_FakeTimelineItem]:
        out = []
        for info in infos:
            item = self.item_cls(info)
            self.appended.append(item)
            out.append(item)
        return out


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class _Case(unittest.TestCase):
    """公共夹具：临时目录 + 两个真实存在的（空）素材文件。

    临时目录用 :func:`_scratch_dir` 建：**不能用** ``tempfile.mkdtemp`` /
    ``TemporaryDirectory``——它把目录建成 0o700，在「写权限按 ACL 授予、
    不覆盖新建私有目录」的受限沙箱里会被拒绝写入，连删都删不掉。本机的
    DSH 文件沙箱就是这样，所以这里退回 ``os.makedirs``（继承父目录 ACL）。
    """

    def setUp(self) -> None:
        # 每个用例都从「两个脚本 API 环境变量不存在」开始：bridge.prepare_env()
        # 会往 os.environ 里写 RESOLVE_SCRIPT_API，别的测试模块也会调它；不隔离
        # 的话，``scripting_dir()`` 会优先读环境变量，于是「部件缺失」的用例会
        # 拿到本机真实的 ProgramData 路径而误判为可用（全量 discover 时踩过）。
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RESOLVE_SCRIPT_API", None)
        os.environ.pop("RESOLVE_SCRIPT_LIB", None)
        self.tmp = _scratch_dir("resolve_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_dir = os.path.join(self.tmp, "out")
        self.media_a = self._touch("a.mp4")
        self.media_b = self._touch("b.mp4")

    def _touch(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(b"\x00")
        return path

    def make_timeline(self, *, with_texts: bool = True,
                      fps: float = 30.0) -> Any:
        """标准夹具时间线：3 个镜头（a、a、b，其中第 2 个就是帧号用例）。

        落点按 ``Timeline.frames`` 排得**严丝合缝**（无空档），免得
        ``recordFrame`` 那条未验证分支掺进来干扰断言：

        镜头 1：a 0.0..1.0 @0.0   → 源帧 0..30，落点 0，占 30 帧
        镜头 2：a 0.5..2.52 @1.0  → 源帧 15..76，落点 30，占 61 帧
        镜头 3：b 0.0..2.0 @3.02  → 源帧 0..60，落点 91
        """
        clips = [
            ir.TimelineClip(index=1, source=self.media_a,
                            src_in=0.0, src_out=1.0, tl_start=0.0),
            ir.TimelineClip(index=2, source=self.media_a,
                            src_in=0.5, src_out=2.52, tl_start=1.0),
            ir.TimelineClip(index=3, source=self.media_b,
                            src_in=0.0, src_out=2.0, tl_start=3.02),
        ]
        texts: List[Any] = []
        if with_texts:
            texts = [
                ir.TimelineText(index=1, text="开场白|关键词", tl_start=1.5,
                                tl_dur=2.5, style_name="title",
                                style=schema.TextStyle()),
                ir.TimelineText(index=2, text="这句是旁白不该出画面",
                                tl_start=0.0, tl_dur=1.0, style_name="vo",
                                style=schema.TextStyle(invisible=True)),
            ]
        return ir.Timeline(name="demo", fps=fps, duration=5.0,
                           clips=clips, texts=texts)

    def make_backend(self, options: Optional[Dict[str, Any]] = None,
                     bridge: Any = None) -> Any:
        backend = backend_mod.ResolveBackend(paths=config.Paths(),
                                             options=options or {})
        if bridge is not None:
            backend.bridge = bridge
        return backend

    def make_bridge(self, fake: _FakeResolve,
                    lut_dir: Optional[str] = None) -> mock.MagicMock:
        """替身 ResolveBridge：只有 connect / version / is_studio / lut_dir 有意义。"""
        bridge = mock.MagicMock(name="ResolveBridge")
        bridge.connect.return_value = fake.resolve
        bridge.version.return_value = "20.3.0"
        bridge.is_studio.return_value = True
        bridge.lut_dir.return_value = lut_dir
        bridge.available.return_value = True
        return bridge


# --------------------------------------------------------------------------- #
# 一、bridge：加载与探测
# --------------------------------------------------------------------------- #
class BridgeTests(_Case):
    """脚本 API 的探测。**一次也不真的 import 那个模块。**"""

    def test_available_false_without_env_or_paths(self) -> None:
        """RESOLVE_SCRIPT_API 缺失、推断路径也找不到时，``available()`` 为 False。

        本机确实装了 Resolve，所以默认推断会成功——这里必须把
        ``_default_support_dir`` 也打成 None，才能验证「什么都没有」的分支。
        """
        bridge = bridge_mod.ResolveBridge(paths=config.Paths())
        bridge_mod.reset_cache()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESOLVE_SCRIPT_API", None)
            os.environ.pop("RESOLVE_SCRIPT_LIB", None)
            with mock.patch.object(bridge_mod, "_default_support_dir",
                                   return_value=None):
                self.assertFalse(bridge.available())
                self.assertTrue(bridge.last_error)

                backend = self.make_backend(bridge=bridge)
                self.assertFalse(backend.available())
                why = backend.why_unavailable()
                self.assertIn("Studio", why)
                self.assertIn("External scripting", why)

    def test_import_is_lazy(self) -> None:
        """模块被 import 时**不加载**脚本模块（``_DVR`` 必须还是空的）。"""
        bridge_mod.reset_cache()
        self.assertIsNone(bridge_mod._DVR)
        # 构造 bridge 本身也不该触发加载
        bridge_mod.ResolveBridge(paths=config.Paths())
        self.assertIsNone(bridge_mod._DVR)

    def test_stub_module_available_and_connect(self) -> None:
        """替身模块路径：``available()`` 只看部件、``connect()`` 才 import。

        这同时证明了两件事：``available()`` 不要求 Resolve 在运行（部件齐备即
        True），以及它**不 import**（``_DVR`` 在 connect 之前一直是空的）。
        """
        scripting = os.path.join(self.tmp, "Scripting")
        os.makedirs(os.path.join(scripting, "Modules"), exist_ok=True)

        fake = _FakeResolve()
        fake.resolve.GetVersionString.return_value = "20.3.1"
        fake.resolve.GetProductName.return_value = "DaVinci Resolve Studio"

        stub = types.ModuleType("DaVinciResolveScript")
        stub.scriptapp = mock.MagicMock(return_value=fake.resolve)  # type: ignore[attr-defined]

        saved_path = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), saved_path)
        self.addCleanup(bridge_mod.reset_cache)
        self.addCleanup(sys.modules.pop, "DaVinciResolveScript", None)

        bridge_mod.reset_cache()
        sys.modules["DaVinciResolveScript"] = stub
        bridge = bridge_mod.ResolveBridge(
            paths=config.Paths(resolve_scripting=scripting))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESOLVE_SCRIPT_API", None)
            os.environ.pop("RESOLVE_SCRIPT_LIB", None)
            self.assertTrue(bridge.available())
            self.assertIsNone(bridge_mod._DVR)               # available 不 import
            self.assertEqual(os.environ["RESOLVE_SCRIPT_API"], scripting)
            self.assertNotIn("RESOLVE_SCRIPT_LIB", os.environ)  # 不自动注入

            # connect 会先起子进程预检。测试里必须打桩：真的去 import 本机的
            # fusionscript.dll 会把测试进程直接打死（0xC0000005）。
            with mock.patch.object(bridge_mod.ResolveBridge, "probe_import",
                                   return_value=(True, "")):
                resolve = bridge.connect()
            self.assertIs(resolve, fake.resolve)
            self.assertIs(bridge_mod._DVR, stub)             # 这时才缓存
            stub.scriptapp.assert_called_once_with("Resolve")  # type: ignore[attr-defined]
            self.assertEqual(bridge.version(), "20.3.1")
            self.assertIs(bridge.is_studio(), True)

    def test_available_does_not_import_the_real_module(self) -> None:
        """部件齐备 → ``available()`` 为 True，但一个字节都不 import。

        防回归用：本机实测 Resolve 没在运行时 import fusionscript 会让进程以
        0xC0000005 退出，``doctor`` 只要 import 一次就可能整条命令崩掉。
        """
        scripting = os.path.join(self.tmp, "ScriptingLazy")
        modules = os.path.join(scripting, "Modules")
        os.makedirs(modules, exist_ok=True)
        with open(os.path.join(modules, "DaVinciResolveScript.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("raise AssertionError('available() 不该 import 这个模块')\n")
        lib = os.path.join(self.tmp, "fusionscript.dll")
        with open(lib, "wb") as fh:
            fh.write(b"MZ")                      # 只要是个文件，内容无所谓

        bridge_mod.reset_cache()
        self.addCleanup(bridge_mod.reset_cache)
        self.addCleanup(sys.modules.pop, "DaVinciResolveScript", None)
        sys.modules.pop("DaVinciResolveScript", None)
        bridge = bridge_mod.ResolveBridge(
            paths=config.Paths(resolve_scripting=scripting))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESOLVE_SCRIPT_API", None)
            os.environ.pop("RESOLVE_SCRIPT_LIB", None)
            with mock.patch.object(bridge, "fusionscript_lib", return_value=lib):
                self.assertTrue(bridge.available())
        self.assertIsNone(bridge_mod._DVR)
        self.assertNotIn("DaVinciResolveScript", sys.modules)

    def test_available_false_when_module_file_missing(self) -> None:
        """Modules 目录在、但 ``DaVinciResolveScript.py`` 不在 → False + 说明。"""
        scripting = os.path.join(self.tmp, "ScriptingEmpty")
        os.makedirs(os.path.join(scripting, "Modules"), exist_ok=True)
        bridge_mod.reset_cache()
        self.addCleanup(bridge_mod.reset_cache)
        self.addCleanup(sys.modules.pop, "DaVinciResolveScript", None)
        sys.modules.pop("DaVinciResolveScript", None)
        bridge = bridge_mod.ResolveBridge(
            paths=config.Paths(resolve_scripting=scripting))
        self.assertFalse(bridge.available())
        self.assertIn("DaVinciResolveScript.py", bridge.last_error)

    def test_probe_crash_becomes_actionable_error(self) -> None:
        """子进程 0xC0000005 崩溃 → 翻译成人话，且父进程绝不 import。"""
        bridge_mod.reset_cache()
        self.addCleanup(bridge_mod.reset_cache)
        proc = mock.Mock(returncode=-1073741819, stdout=b"", stderr=b"")
        bridge = bridge_mod.ResolveBridge(paths=config.Paths())
        with mock.patch.object(bridge_mod.subprocess, "run", return_value=proc):
            ok, detail = bridge.probe_import()
            self.assertFalse(ok)
            self.assertIn("0xC0000005", detail)
            self.assertIn("崩溃", detail)

            with mock.patch.object(bridge_mod.ResolveBridge, "_load_dvr",
                                   side_effect=AssertionError("探针失败时不许 import")):
                with self.assertRaises(bridge_mod.ResolveError) as ctx:
                    bridge.connect()
        self.assertIn("0xC0000005", str(ctx.exception))
        self.assertIn("没在运行", str(ctx.exception))
        self.assertIsNone(bridge_mod._DVR)

    def test_probe_import_failure_points_at_install(self) -> None:
        """探针里 import 失败（故意 exit 3）→ 指向安装/环境变量，而不是崩溃。"""
        proc = mock.Mock(returncode=3,
                         stdout=b"IMPORT_FAILED ImportError: DLL load failed\n",
                         stderr=b"")
        bridge = bridge_mod.ResolveBridge(paths=config.Paths())
        with mock.patch.object(bridge_mod.subprocess, "run", return_value=proc):
            ok, detail = bridge.probe_import()
        self.assertFalse(ok)
        self.assertIn("import DaVinciResolveScript 失败", detail)
        self.assertIn("DLL load failed", detail)

    def test_probe_scriptapp_empty_means_not_running(self) -> None:
        """探针连上了但 ``scriptapp`` 返回空 → 「没在运行」那一档。"""
        proc = mock.Mock(returncode=0,
                         stdout=b"IMPORT_OK\nCONNECT_EMPTY\n", stderr=b"")
        bridge = bridge_mod.ResolveBridge(paths=config.Paths())
        with mock.patch.object(bridge_mod.subprocess, "run", return_value=proc):
            ok, detail = bridge.probe_import()
            self.assertFalse(ok)
            self.assertIn("scriptapp", detail)
            with self.assertRaises(bridge_mod.ResolveError) as ctx:
                bridge.connect()
        self.assertIn("没在运行", str(ctx.exception))
        self.assertIn("External scripting", str(ctx.exception))

    def test_connect_reports_actionable_hint(self) -> None:
        """``scriptapp()`` 返回空 → ResolveError 里带「没启动/免费版/偏好项」。"""
        stub = types.ModuleType("DaVinciResolveScript")
        stub.scriptapp = mock.MagicMock(return_value=None)  # type: ignore[attr-defined]
        scripting = os.path.join(self.tmp, "Scripting2")
        os.makedirs(os.path.join(scripting, "Modules"), exist_ok=True)

        saved_path = list(sys.path)
        self.addCleanup(sys.path.__setitem__, slice(None), saved_path)
        self.addCleanup(bridge_mod.reset_cache)
        self.addCleanup(sys.modules.pop, "DaVinciResolveScript", None)
        bridge_mod.reset_cache()
        sys.modules["DaVinciResolveScript"] = stub
        bridge = bridge_mod.ResolveBridge(
            paths=config.Paths(resolve_scripting=scripting))

        with mock.patch.object(bridge_mod.ResolveBridge, "probe_import",
                               return_value=(True, "")):
            with self.assertRaises(bridge_mod.ResolveError) as ctx:
                bridge.connect()
        text = str(ctx.exception)
        self.assertIn("没在运行", text)
        self.assertIn("External scripting", text)


# --------------------------------------------------------------------------- #
# 二、计划：帧号换算、命名、SRT
# --------------------------------------------------------------------------- #
class PlanTests(_Case):
    """纯计算部分，一个 API 都不调。"""

    def test_frame_conversion(self) -> None:
        """``src_in=0.5, src_out=2.52, fps=30`` → ``startFrame=15, endFrame=76``。

        取舍说明：两端**各自** ``round(秒 * fps)``（走 ``Timeline.frames``），
        而不是「起点 + round(时长*fps)」。这样每个端点都落在真实帧上、不会
        随镜头数累计漂移；代价是当两端的小数部分恰好一进一舍时，帧数会比
        ``round(时长*fps)`` 差 1 帧。本例恰好一致：
        ``round(2.52*30) - round(0.5*30) = 76 - 15 = 61 = round(2.02*30)``。
        ``endFrame`` 是**排除式**（0..52 表示 52 帧），所以帧数 = end - start。
        """
        timeline = self.make_timeline(with_texts=False)
        plans, _ = tl_mod.plan_clips(timeline)

        self.assertEqual(timeline.frames(0.5), 15)
        self.assertEqual(timeline.frames(2.52), 76)
        self.assertEqual(round(2.52 * 30), 76)
        self.assertEqual((plans[1].start_frame, plans[1].end_frame), (15, 76))

        self.assertEqual(plans[1].frame_count, 61)
        self.assertEqual(plans[1].frame_count, int(round((2.52 - 0.5) * 30)))

        # 首镜与末镜也顺手钉住
        self.assertEqual((plans[0].start_frame, plans[0].end_frame), (0, 30))
        self.assertEqual((plans[2].start_frame, plans[2].end_frame), (0, 60))

    def test_short_clip_extended_to_one_frame(self) -> None:
        """不足一帧的源区间补到 1 帧并记 warning（Resolve 会拒绝 0 帧）。"""
        clip = ir.TimelineClip(index=1, source=self.media_a,
                               src_in=1.0, src_out=1.0, tl_start=0.0)
        timeline = ir.Timeline(name="tiny", fps=30.0, clips=[clip])
        plans, warnings = tl_mod.plan_clips(timeline)
        self.assertEqual((plans[0].start_frame, plans[0].end_frame), (30, 31))
        self.assertTrue(any("不足 1 帧" in w for w in warnings))

    def test_timeline_name(self) -> None:
        """默认 ``CUTWRIGHT_{名字}_{时间戳}``，可用 options 覆盖，非法字符替换。"""
        timeline = self.make_timeline(with_texts=False)
        self.assertEqual(
            tl_mod.resolve_timeline_name(timeline, {"timestamp": "20260101_000000"}),
            "CUTWRIGHT_demo_20260101_000000")
        self.assertEqual(
            tl_mod.resolve_timeline_name(timeline, {"timeline_name": "  我的手剪  "}),
            "我的手剪")
        warnings: List[str] = []
        name = tl_mod.resolve_timeline_name(
            timeline, {"timeline_name": "a/b:c", "timestamp": "x"}, warnings)
        self.assertEqual(name, "a_b_c")
        self.assertTrue(warnings)

    def test_srt_time_format(self) -> None:
        """时间码格式与毫秒进位。"""
        self.assertEqual(tl_mod.srt_time(1.5), "00:00:01,500")
        self.assertEqual(tl_mod.srt_time(4.0), "00:00:04,000")
        self.assertEqual(tl_mod.srt_time(0.0), "00:00:00,000")
        self.assertEqual(tl_mod.srt_time(3661.999), "01:01:01,999")
        self.assertEqual(tl_mod.srt_time(-3.0), "00:00:00,000")

    def test_build_srt_skips_invisible(self) -> None:
        """不可见文字（旁白）不进字幕；内容去掉 ``|`` 关键词分隔符。"""
        timeline = self.make_timeline()
        content = tl_mod.build_srt(timeline.texts)
        blocks = content.strip().split("\n\n")
        self.assertEqual(len(blocks), 1)
        lines = blocks[0].split("\n")
        self.assertEqual(lines[0], "1")
        self.assertEqual(lines[1], "00:00:01,500 --> 00:00:04,000")
        self.assertEqual(lines[2], "开场白关键词")
        self.assertNotIn("旁白", content)


# --------------------------------------------------------------------------- #
# 三、dry_run
# --------------------------------------------------------------------------- #
class DryRunTests(_Case):
    """dry_run 是 CI 的主力路径：只算，不连。"""

    def test_dry_run_never_connects(self) -> None:
        """``dry_run=True`` 时**一次都不调** ``bridge.connect()``。"""
        timeline = self.make_timeline()
        bridge = bridge_mod.ResolveBridge(paths=config.Paths())

        with mock.patch.object(bridge_mod.ResolveBridge, "connect",
                               autospec=True,
                               side_effect=AssertionError("dry_run 不允许连接 Resolve")
                               ) as fake_connect:
            report = tl_mod.build_timeline(
                bridge, timeline,
                options={"dry_run": True, "timeline_name": "MY_TL",
                         "track_index": 2, "out_dir": self.out_dir})
            fake_connect.assert_not_called()

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["timeline_name"], "MY_TL")
        self.assertEqual(report["track"], 2)
        self.assertEqual(report["clips"][1]["start_frame"], 15)
        self.assertEqual(report["clips"][1]["end_frame"], 76)

        calls = report["calls"]
        self.assertEqual([c[0] for c in calls][:4],
                         ["ImportMedia", "ImportMedia",
                          "CreateEmptyTimeline", "SetCurrentTimeline"])
        self.assertEqual(calls[0][1], [self.media_a])
        self.assertEqual(calls[1][1], [self.media_b])      # 同一素材只导一次

        appends = [c for c in calls if c[0] == "AppendToTimeline"]
        self.assertEqual(len(appends), 3)
        for _, info in appends:
            self.assertIsInstance(info["mediaPoolItem"], str)   # 用路径代替对象
            self.assertEqual(info["trackIndex"], 2)
        self.assertEqual((appends[1][1]["startFrame"], appends[1][1]["endFrame"]),
                         (15, 76))
        self.assertNotIn("recordFrame", appends[2][1])          # 无空档就不传
        for _, info in appends:                                # 清单必须能 JSON 化
            json.dumps(info)

        # 字幕：只说清楚要生成什么，不落盘
        self.assertEqual(report["texts"]["method"], "srt_sidecar")
        self.assertIn("00:00:01,500 --> 00:00:04,000", report["texts"]["content"])
        self.assertFalse(os.path.exists(self.out_dir))          # 一个文件都没写

    def test_dry_run_lut_lists_calls(self) -> None:
        """给了 LUT 时清单里要有 RefreshLUTList 与逐镜 SetLUT。"""
        lut_dir = os.path.join(self.tmp, "LUT")
        os.makedirs(lut_dir, exist_ok=True)
        lut = os.path.join(self.tmp, "look.cube")
        with open(lut, "w", encoding="utf-8") as handle:
            handle.write("LUT_3D_SIZE 2\n")

        timeline = self.make_timeline(with_texts=False)
        bridge = mock.MagicMock(name="ResolveBridge")
        bridge.lut_dir.return_value = lut_dir
        bridge.connect.side_effect = AssertionError("dry_run 不允许连接 Resolve")

        report = tl_mod.build_timeline(
            bridge, timeline,
            options={"dry_run": True, "lut": lut, "out_dir": self.out_dir})

        names = [c[0] for c in report["calls"]]
        self.assertIn("RefreshLUTList", names)
        self.assertEqual(names.count("SetLUT"), 3)
        expected = os.path.join(lut_dir, "cutwright", "look.cube")
        self.assertEqual(report["lut"]["path"], expected)
        self.assertTrue(any("复制到" in w for w in report["warnings"]))
        self.assertFalse(os.path.exists(expected))       # dry_run 不落盘
        bridge.connect.assert_not_called()

    def test_dry_run_missing_media_is_warning(self) -> None:
        """素材不存在时 dry_run 不炸，只记 warning（真跑才会失败）。"""
        clip = ir.TimelineClip(index=1, source=os.path.join(self.tmp, "nope.mp4"),
                               src_in=0.0, src_out=1.0, tl_start=0.0)
        timeline = ir.Timeline(name="ghost", fps=30.0, clips=[clip])
        report = tl_mod.build_timeline(
            bridge_mod.ResolveBridge(paths=config.Paths()), timeline,
            options={"dry_run": True})
        self.assertTrue(any("不存在" in w for w in report["warnings"]))
        self.assertEqual(report["clips"][0]["ok"], True)


# --------------------------------------------------------------------------- #
# 四、真跑（Resolve 侧全替身）
# --------------------------------------------------------------------------- #
class RealRunTests(_Case):
    """走完 build 的完整链路，但 Resolve 全部是替身。"""

    def test_import_media_once_per_file(self) -> None:
        """同一素材被两个镜头引用时只 ``ImportMedia`` 一次。"""
        fake = _FakeResolve()
        backend = self.make_backend(bridge=self.make_bridge(fake))
        timeline = self.make_timeline()

        result = backend.build(timeline, self.out_dir)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(fake.media_pool.ImportMedia.call_count, 2)   # a、b 各一次
        imported_paths = [c.args[0][0]
                          for c in fake.media_pool.ImportMedia.call_args_list]
        self.assertEqual(imported_paths, [self.media_a, self.media_b])

        # 逐条追加：3 个镜头 → 3 次调用；前两镜共用同一个 MediaPoolItem
        self.assertEqual(fake.media_pool.AppendToTimeline.call_count, 3)
        infos = [c.args[0][0] for c in fake.media_pool.AppendToTimeline.call_args_list]
        self.assertIs(infos[0]["mediaPoolItem"], infos[1]["mediaPoolItem"])
        self.assertEqual((infos[1]["startFrame"], infos[1]["endFrame"]), (15, 76))
        self.assertEqual(infos[1]["trackIndex"], 1)
        # 夹具的落点是严丝合缝排的，所以不该出现 recordFrame
        self.assertNotIn("recordFrame", infos[0])
        self.assertNotIn("recordFrame", infos[2])

        # 报告里的实测落点（GetStart/GetEnd/GetDuration 复核）
        self.assertEqual(result.data["clips"][1]["placement"],
                         {"start": 100, "end": 161, "duration": 61})
        self.assertEqual(result.data["project"], "DEMO_PROJECT")

    def test_no_project_is_actionable_error(self) -> None:
        """没有打开的工程时给出可操作错误，**不新建工程**。"""
        fake = _FakeResolve(current_project=False)
        backend = self.make_backend(bridge=self.make_bridge(fake))
        result = backend.build(self.make_timeline(), self.out_dir)

        self.assertFalse(result.ok)
        self.assertIn("没有打开的工程", result.error)
        self.assertIn("不会替你新建", result.error)
        fake.media_pool.CreateEmptyTimeline.assert_not_called()

    def test_texts_become_srt_sidecar(self) -> None:
        """有可见文字 → 写 BOM 的 srt，报告里明说是装不进去的降级方案。"""
        fake = _FakeResolve()
        backend = self.make_backend(bridge=self.make_bridge(fake))
        result = backend.build(self.make_timeline(), self.out_dir)

        self.assertTrue(result.ok, msg=result.error)
        texts = result.data["texts"]
        self.assertEqual(texts["method"], "srt_sidecar")
        self.assertEqual(texts["count"], 1)
        self.assertIn("File > Import > Subtitle", texts["note"])

        srt_path = texts["path"]
        self.assertTrue(srt_path.endswith(".srt"))
        self.assertIn(srt_path, result.artifacts)
        raw = Path(srt_path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "SRT 必须是 UTF-8 with BOM")
        content = raw.decode("utf-8-sig")
        self.assertIn("00:00:01,500 --> 00:00:04,000", content)
        self.assertTrue(content.startswith("1\r\n") or content.startswith("1\n"))

        # 报告 JSON 落盘且能读回来
        report_path = os.path.join(self.out_dir, "demo.resolve.report.json")
        self.assertEqual(result.artifacts[0], report_path)
        with open(report_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["timeline_name"], result.data["timeline_name"])
        for key in ("timeline_name", "track", "clips", "texts", "lut", "warnings"):
            self.assertIn(key, saved)
        for clip in saved["clips"]:
            for key in ("index", "source", "start_frame", "end_frame", "ok"):
                self.assertIn(key, clip)

    def test_lut_through_node_graph(self) -> None:
        """LUT 走 19.0.0+ 的 ``GetNodeGraph().SetLUT()``，并先 RefreshLUTList。"""
        lut_dir = os.path.join(self.tmp, "LUT")
        os.makedirs(lut_dir, exist_ok=True)
        lut = os.path.join(self.tmp, "look.cube")
        with open(lut, "w", encoding="utf-8") as handle:
            handle.write("LUT_3D_SIZE 2\n")

        fake = _FakeResolve()
        backend = self.make_backend(
            options={"lut": lut}, bridge=self.make_bridge(fake, lut_dir=lut_dir))
        result = backend.build(self.make_timeline(with_texts=False), self.out_dir)

        self.assertTrue(result.ok, msg=result.error)
        fake.project.RefreshLUTList.assert_called_once()
        expected = os.path.join(lut_dir, "cutwright", "look.cube")
        self.assertTrue(os.path.isfile(expected))            # 复制进 LUT 目录

        for item in fake.appended:
            self.assertEqual(item.graph.lut_calls, [(2, expected)])
        self.assertEqual(result.data["lut"]["method"], "node_graph")
        self.assertEqual(result.data["lut"]["applied"], 3)
        self.assertEqual(result.data["lut"]["failed"], 0)
        self.assertNotIn("SetLUT", dir(type(fake.appended[0])))   # 没碰老 API

    def test_lut_legacy_api_branch(self) -> None:
        """Resolve < 19 走 ``TimelineItem.SetLUT()``。"""
        lut = os.path.join(self.tmp, "legacy.cube")
        with open(lut, "w", encoding="utf-8") as handle:
            handle.write("LUT_3D_SIZE 2\n")

        fake = _FakeResolve(item_cls=_LegacyTimelineItem)
        bridge = self.make_bridge(fake)
        bridge.version.return_value = "18.6.6"
        backend = self.make_backend(options={"lut": lut}, bridge=bridge)
        result = backend.build(self.make_timeline(with_texts=False), self.out_dir)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.data["lut"]["method"], "timeline_item")
        self.assertEqual(result.data["lut"]["api"], "timeline_item")
        self.assertEqual(result.data["lut"]["applied"], 3)
        # lut_dir 探测不到 → 直接用原路径
        for item in fake.appended:
            self.assertEqual(item.lut, lut)

    def test_lut_skipped_without_refresh(self) -> None:
        """没有 ``RefreshLUTList()`` 就跳过并记 warning，不硬失败。"""
        lut = os.path.join(self.tmp, "skip.cube")
        with open(lut, "w", encoding="utf-8") as handle:
            handle.write("LUT_3D_SIZE 2\n")

        fake = _FakeResolve(refresh_lut=False)
        backend = self.make_backend(options={"lut": lut},
                                    bridge=self.make_bridge(fake))
        result = backend.build(self.make_timeline(with_texts=False), self.out_dir)

        self.assertTrue(result.ok, msg=result.error)
        self.assertEqual(result.data["lut"]["method"], "skipped_no_refresh")
        self.assertTrue(any("RefreshLUTList" in w for w in result.warnings))
        for item in fake.appended:
            self.assertEqual(item.graph.lut_calls, [])


# --------------------------------------------------------------------------- #
# 五、后端边界：失败不外抛
# --------------------------------------------------------------------------- #
class BackendTests(_Case):
    """后端是流水线的一环：失败要变成 ``ok=False``，不能抛。"""

    def test_connect_error_returns_result(self) -> None:
        """``bridge.connect()`` 抛错 → ``ok=False`` + 可操作提示，不外抛。"""
        bridge = mock.MagicMock(name="ResolveBridge")
        bridge.connect.side_effect = bridge_mod.ResolveError(
            "连不上正在运行的 DaVinci Resolve",
            hint="先手动启动 DaVinci Resolve（cutwright 不会替你启动）")
        bridge.diagnose.return_value = {"support": None}
        backend = self.make_backend(bridge=bridge)

        result = backend.build(self.make_timeline(), self.out_dir)

        self.assertFalse(result.ok)
        self.assertIn("提示:", result.error)
        self.assertIn("不会替你启动", result.error)
        self.assertEqual(result.artifacts, [])
        self.assertIn("diagnostics", result.data)
        # 失败时不留半成品
        self.assertFalse(
            os.path.exists(os.path.join(self.out_dir, "demo.resolve.report.json")))
        self.assertEqual([n for n in os.listdir(self.out_dir) if n.endswith(".srt")], [])

    def test_append_failure_carries_context(self) -> None:
        """追加失败的错误里要有「第几个镜头、哪一步」。"""
        fake = _FakeResolve()

        def boom(infos: List[Dict[str, Any]]) -> Any:
            if fake.appended:
                raise RuntimeError("trackIndex out of range")
            return fake._append(infos)

        fake.media_pool.AppendToTimeline.side_effect = boom
        backend = self.make_backend(bridge=self.make_bridge(fake))
        result = backend.build(self.make_timeline(), self.out_dir)

        self.assertFalse(result.ok)
        self.assertIn("AppendToTimeline", result.error)
        self.assertIn("镜头 2", result.error)
        self.assertIn("trackIndex out of range", result.error)

    def test_unknown_backend_still_registered(self) -> None:
        """``register`` 生效：注册表里能看到 resolve。"""
        base = importlib.import_module("cutwright.backends.base")
        self.assertIn("resolve", base.BACKENDS)
        self.assertIs(base.BACKENDS["resolve"], backend_mod.ResolveBackend)
        self.assertEqual(backend_mod.ResolveBackend.name, "resolve")
        self.assertEqual(backend_mod.ResolveBackend.description, "生成达芬奇 Resolve 时间线")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
