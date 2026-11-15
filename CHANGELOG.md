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
- **剪映草稿读入（`cutwright import`）**：草稿目录 → 分镜表，加密草稿自动解密
  （`videoeditor.dll`），`--plain-only` 时只读明文、遇密文直接报错而不是给空表。
  拿回排布（源文件、入点、时长、变速、时间线位置、字幕文案与时间、音频位置），
  不还原样式/转场/关键帧/特效；旁白与字幕分不开、换机器后素材路径失效这两条
  都写进 `warnings` 与 `missing_files()`。新增 `src/cutwright/backends/jianying/reader.py`
  与 `tests/test_jianying_reader.py`（12 例，含真密文往返）。
- **静帧镜头（`"still": true`）**：分镜表里的 `file` 可以是一张图，按 `src_dur` 铺时长。
  三条产线都认：ffmpeg 用 `-loop 1` 铺帧、不抽原声，剪映写成 `type=photo` 素材
  （`PHOTO_DURATION_SENTINEL`），达芬奇照常进媒体池。顺带把 `import` 从「图片段已略过」
  改成保留成静帧——真实的剪映草稿里图片段很常见，丢掉就是时间线凭空短几秒。
  新增 `tests/test_still_clips.py`（15 例）与 `W042`/`W043` 两条校验。
- **剪映草稿里的相对素材路径**：`##_draftpath_placeholder_<uuid>_##/textReading/…`
  这类路径现在还原成草稿目录下的真实路径。以前它被当成相对当前目录，报出来的是一条
  跟用户无关的假路径（`workspace/##_draftpath…`），真正的文件反而找不到。
- **切片缓存与并行、快编档**：`--opt jobs=N` 并行切镜，切片按内容摘要命名
  （`_build/seg_<digest>.mp4`）跨次复用，`--opt preview=true` 出低码率快编版。
  摘要只含画面相关的量（源文件 mtime/size、入点、时长、变速、缩放、画幅、fps），
  改字幕或换配乐不会触发重切。实测 8 镜 12.9s：9.44s → 7.85s → 5.58s（命中缓存）。

### 修复

- **快编档与正式档的帧数一致性有回归用例**：`preview=true` 只该改画质，不该改时长——
  若哪天差一帧，`overlay` 的 `enable` 窗口就会错位。`test_preview_keeps_the_same_frame_count`
  逐帧数两条成片（实测同为 414 帧 / 13.800s）并断言相等。

- **配乐给人声让路（ducking）**：混音时以环境声为侧链跑 `sidechaincompress`，
  人声一进来配乐自己退下去、说完再回来。以前配乐是固定音量相加，音乐一响就压住台词。
  三个旋钮：`--opt duck=false` 关掉、`duck_threshold`（默认 0.05）、`duck_ratio`（默认 8）。
  实测（配乐比人声响 12 dB 时）开启后整体电平低 4.6 dB；没有环境声时自动跳过。
  新增 `test_music_ducks_under_ambient_voice` 与 `test_ducking_is_skipped_without_ambient`。

- **成片响度归一化**：混完过 `loudnorm` 拉到 `--opt loudness`（默认 -14 LUFS、峰值 -1 dBTP）。
  以前只有「每个镜头对齐到 -20 dBFS 均值」这一步，混出来整体偏轻——66 镜那条成片峰值
  -15.3 dB、RMS -19.6 dB，手机外放明显比参考片小一截；现在同一条片子积分响度落在 -14 附近。
  `--opt loudness=false` 可关掉。新增两个用例（归一化后 ≈ -14 LUFS、关掉后仍 < -16）。

- **配乐比成片短时尾巴整段静音**：`amix` 以环境声长度为准，配乐放完就是彻底没声，
  听起来像忘了配音。现在默认 `aloop` 循环铺满，并在报告里说清「配乐只有 5.0s，要铺 12.0s；
  已循环，接缝可能有跳音」；`--opt music_loop=false` 可关掉只保留警告。
  新增 `tests/test_ffmpeg_backend.py` 两条用例（循环后尾巴不静音、开关生效）。
- **中文折行会把「，」扔到下一行行首**：加了避头尾（禁则）——断点落在收尾标点前就
  把标点带回上一行（最多多带 2 字），行尾也不许留「（「『」。新增
  `tests/test_text.py::KinsokuTest` 4 例。

- **`import` 认得出字幕的颜色与位置**：以前读回来一律白字贴底，现在从草稿里取
  `text_color` / `clip.transform.y` / `border_width` / `subtitle_keywords`，把颜色、
  上下位置、描边有无、关键词高亮还原成分组样式（`import_N`），关键词自动包成
  `｜…｜` 走换色。字号与描边粗细仍是画布默认——剪映 `font_size` 没有公开单位，
  猜 px 比不猜更糟，这条写进 `warnings`。新增 `tests/test_jianying_reader.py`
  的 `StyleRestoreTest`（4 例）。

- **长台词拆成多镜时字幕锚错镜头**：字幕原先挂在最后一镜上，压到下一句头上
  （长剧本压测里实测重叠 2.10s、一次三条）。现在锚在这一句的第一镜，时长覆盖它占的全部镜头。
  新增 `tests/test_story.py::test_long_line_caption_covers_all_its_shots`。
- **`story` 报告加上素材复用**：`reuse` 字段给出用得最多的 5 段素材，命令行打印
  「14 段撑 66 镜（最多 7 次）」，6 次以上提示补拍——素材数是硬上限，机器替不了。
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
