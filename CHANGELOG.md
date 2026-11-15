# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
