# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **`cutwright init demo --with-media`**：连素材一起现造。之前 `init` 只写一份
  引用 `clip01.mp4` 之类文件的空架子，照着 README 敲完第一条命令就撞上
  「素材不存在」——现在默认三镜 + 透明画框 + 配乐全部用 ffmpeg 合成，
  `init` 完可以直接 `build` 出一条真片子。
- **`cutwright.demo` 模块**：素材合成逻辑从 `examples/demo/make_media.py`
  抽出来，CLI 与示例脚本共用一份，避免「demo 能跑而 init 造出来的跑不了」。
  里面的 RGBA PNG 编码器只用 `zlib` + `struct`，所以演示素材不依赖 Pillow。
- **`tests/test_demo.py`**：逐像素验证画框中间真的是 `alpha=0`（黑窟窿那次
  就是没人验证这个），以及第二次运行必须跳过已有素材。

### 修复

- **`init` 生成的画面叠加与配乐找不到**：`shotlist.json` 只写了 `material_dir`
  没写 `asset_dir`，于是 `frame.png` / `bgm.mp3` 被解析到上一级目录，`validate
  --check-files` 报 E041 / E051。
- **demo 配乐只有 6 秒**：BGM 时长取的是「最长的那一段素材」，而成片 13.8 秒，
  音乐在 6 秒处就停了。现在按成片时长合成（14.0 秒，尾部淡出）。
- **关键词只认半角 `|`**：中文输入法打出来的是全角 `｜`，于是分隔符原样印在
  画面上、关键词也不换色。现在两种都认，而且是**成对交替**——`a｜b｜c` 只让
  `b` 变色，不会把后半句整片刷黄。
- **字幕图缓存不认渲染器版本**：缓存文件名只取「文案 + 样式」的摘要，改了排版
  逻辑后旧 PNG 会被继续复用，表现为「代码改了、成片没变」。摘要里加进
  `text.RENDER_VERSION`，改渲染就 +1。
- **`build()` 收尾日志写在关流之后**：最后一条进度写到已关闭的日志句柄，
  触发 `ValueError: I/O operation on closed file`，整条命令以 1 退出，
  尽管成片已经渲染成功。
- **demo 的 `frame.png` 不透明**：用 lavfi `color=c=black@0.0` 生成的"透明"
  PNG 实测 alpha 全 255，贴到画面上是一个黑窟窿，改成用 Pillow 画真透明画框。
- **demo 第一镜是纯黑**：颜色按 `i * 常数` 生成，`i=0` 就是黑场，既难看又顶到
  qc 的黑场阈值。改成从中调起点取色。

## [0.1.0] — 2026-11-15

第一个可用版本。核心是一条链路：**分镜表 → Timeline → 三条产线**。

### 新增

- `shotlist.json` 分镜表 schema（`schema.py`）：7 类素材段、5 种内置文字样式、
  22 个校验码（E001–E060）。**宽松解析**：坏字段回落默认值而不是抛异常，
  但 `validate()` 会把它们逐个指出来。
- `Timeline` 中间表示（`timeline.py`）：`tl_start` 推导、`src_dur/speed` 换算、
  帧号换算、素材路径解析集中在一处。
- `planner`：补齐空档、按目标时长裁剪/变速、分幕统计。
- `qc`：分两层——渲染前查时间线（空档、重叠、时长漂移），渲染后查成片
  （分辨率、时长、黑场、静音）。
- `ffmpeg` 后端：切片 → 拼接 → 环境声增益 → 字幕/叠加 → 配乐 → 收尾。
  HDR 自动 tonemap、横屏按 `cx` 裁进竖屏、推拉镜 2 倍超采样。
- `jianying` 后端：生成剪映 11.x 草稿（`videoeditor.dll` 加密、根目录与
  `Timelines/<UUID>/` 双写、索引更新前自动备份）。
- `resolve` 后端：在运行中的达芬奇里建时间线、逐镜设 LUT
  （`GetNodeGraph().SetLUT`，19.0.0 起的正确姿势）。
- CLI：`doctor` / `backends` / `validate` / `plan` / `build` / `qc` / `init`，
  全部支持 `--json`，退出码 0/1/2 有明确含义。
- Agent Skill（`skills/cutwright/`）：`SKILL.md` + 3 篇 `references/` +
  `scripts/verify.py`（分层验收，输出 `PASS`/`FAIL`）+ `scripts/install.py`
  （装到 DSH / Claude Code / Cursor / Codex / Gemini）。
- DSH agent 预设（`preset/`）：「剪匠」——标准能力面 + 剪辑工作的行为约束。
- 示例工程（`examples/demo/`）：`make_media.py` 用 ffmpeg 合成 7 段素材，
  30 秒造出一个能真渲的项目。CI 里跑这条链路。
- `tools/lint_skill.py`：按 Agent Skills 规范校验 `SKILL.md`（6 个字段、
  name=目录名、正文行数、引用文件存在性）。
- 100 个单元测试（stdlib `unittest`，不依赖 pytest）。

### 说明

- 剪映草稿加密依赖本机安装的剪映自带的 `videoeditor.dll`，不随项目分发。
- 达芬奇外部脚本只在 **Studio** 版可用；免费版没有这个开关。
- 三条产线都不做「自动导出成片」——那是各路开源库都没做、也做不稳的部分。
