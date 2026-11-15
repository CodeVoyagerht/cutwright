# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **剧本产线（`cutwright index` + `cutwright story`）**：短剧/对话戏不必再手写分镜表。
  `index` 扫素材目录，按文件名（`S01_subway_night_wide.mp4`）打标签并量出可用时长；
  `story` 拿剧本 + 索引排出分镜表——按台词估时长（旁白有音频就用音频真实时长）、
  把长台词按节奏权重切镜、按「场次标签 + 人物 + 景别」挑画面，挑不动就写进报告
  的 `unresolved`，绝不拿占位符糊过去。新增 `src/cutwright/screenplay.py`（剧本读写 +
  S0xx 校验）与 `src/cutwright/story.py`（排片），示例在 `examples/story/`。
- **排片的三条硬规则**：帧网格（最大余数法分配，求和精确、无 0 帧镜头）、相邻不同源
  （权重之外再加硬规则兜底）、时长优先于字数估（显式 `duration` > 旁白音频 > 字数/cps）。
- **`story` 的分场报告**：每场镜头数/起止、`warnings`、`unresolved`，`--json` 里是结构化字段。
- **`tests/test_story.py`**（50+ 例）：帧数和精确、相邻不同源、过短素材宁可跳过也不做
  「糊到看不清的慢放」、输出必须过分镜表校验。

### 修复

- **默认字幕在 1080 画布上溢出**：`planner` 的标定基准从 1080×1920 改回成片标定用的
  2160×3840，1080 画布得到 72px 字幕；同时 `text.py` 按画布宽度再折行一次
  （样式管意愿、画布管物理事实）。
- **空档/重叠的容差 1e-6 等于没有容差**：`schema._CHAIN_EPS = 1e-3`，手写时间码与按帧
  网格算出来的秒数差 1e-4 不再被误报成 `W018`/`W019`。
- **剧本 fps 写错会被悄悄改成 30**：现在保留原值让 `S003` 拦下，`story` 直接
  `StoryError`——「写错了」不该伪装成「算错了」。
- **`_allocate_frames` 的某一段会冲过 `--max-shot`**：节奏权重 1.18 会让某镜超过上限，
  现在宁可多拆一刀也不让任何一镜超出（那是分辨率上限，不是偏好）。

- **`cutwright init demo --with-media`**：连素材一起现造。之前 `init` 只写一份
  引用 `clip01.mp4` 之类文件的空架子，照着 README 敲完第一条命令就撞上
  「素材不存在」——现在默认三镜 + 透明画框 + 配乐全部用 ffmpeg 合成，
  `init` 完可以直接 `build` 出一条真片子。
- **`cutwright.demo` 模块**：素材合成逻辑从 `examples/demo/make_media.py`
  抽出来，CLI 与示例脚本共用一份，避免「demo 能跑而 init 造出来的跑不了」。
  里面的 RGBA PNG 编码器只用 `zlib` + `struct`，所以演示素材不依赖 Pillow。
- **`tests/test_demo.py`**：逐像素验证画框中间真的是 `alpha=0`（黑窟窿那次
  就是没人验证这个），以及第二次运行必须跳过已有素材。
- **`tests/test_audio_speed.py`**：造一个「第 1 秒和第 3 秒各响一声」的素材，
  `speed=2.0` 之后两声必须落在 0.5s 与 1.5s；截断会直接让第二个断言失败。
- **`tests/test_qc_severity.py`**：`ok` 的语义、`[FAIL]`/`[WARN]` 的映射、
  整片无声时 `qc` 必须返回 1。
- **`tests/test_install_script.py`**：拒绝删除外来目录、`--force-any` 才允许、
  重装自己的技能必须放行。

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
- **变速镜头的环境声被截断**：画面走 `setpts=PTS/speed`，声音却按原速截到
  `tl_dur`，听到的是「前 N 秒原声」而不是同一段被加速——第 3 秒的提示音在
  `speed=2.0` 时直接消失，成片整条无声。现在抽环境声按 `clip.speed` 串
  `atempo`（保音高），超过 2.0 或低于 0.5 会自动拆成多级。
- **`qc` 打印 `[FAIL]` 却判通过**：黑场/静音检出一律按 `warn` 存、却按 `FAIL`
  显示，而 `ok` 只数 `error`，于是出现「屏幕上有 FAIL、结论是通过、退出码 0」。
  现在整片黑/整片无声判 `error`（片子废了），局部超阈值判 `warn`，显示层用
  `[FAIL]` / `[WARN]` 区分，警告数量会写进结论行。
- **`install.py --force` 会删掉碰巧同名的目录**：把 `--dir` 指到自己的代码
  目录、里面正好有个 `cutwright` 项目，一条命令就整棵删掉。现在覆盖前先确认
  目标里有 `SKILL.md` 且 `name` 匹配，对不上就拒绝并提示；确实要删得加
  `--force-any`。

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
