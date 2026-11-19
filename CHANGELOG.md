# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- **半截的切片不会再被当成缓存复用（会造成静默的画面缺失）**：以前只按「文件非空」
  判缓存命中，编码被中断后留下的几 KB 半截 mp4 会被直接拼进成片——画面在某一镜之后
  提前结束，音轨却还是全长，而容器时长、体积、响度全都正常。现在三条防线：
  1. 切片先写 `seg_<摘要>.part.mp4`、成功后再改名，中断留下的是临时文件，永远不会
     出现在缓存路径上；
  2. 复用前用 ffprobe 核一遍时长（容差 `max(0.15s, 5%)`），对不上就重切并汇报；
     想省这一遍可以 `--opt verify_cache=false`；
  3. 成片体检新增 `av_duration_match`：视频流与音轨各量一次，差超过 0.25s 判失败
     （问题码 `Q102`）——以前只比容器时长，正好看不出来。
- **同一段素材在分镜表里出现两次时，只切一次**：两处摘要相同、目标路径也相同，
  以前并行切片时两个 ffmpeg 同时写同一个文件，会互相踩；现在按路径去重，
  第二处直接复用第一处的产物（`segments_deduped` 里能看到省了几次）。

### 变更

- **Ken-Burns（推近/拉远）换实现，冷启一条 7 镜片从 203 秒降到 18.7 秒**：以前用
  `zoompan`——单线程、每帧重新采样，一镜 3 秒要 115 秒，是全流程最贵的一步。
  现在改成「2 倍画布 + `scale=…:eval=frame` 逐帧改缩放 + 定尺寸居中裁剪」，
  同一镜 3.8 秒（**37 倍**），两条链的画面 SSIM 0.985（肉眼看不出差别）。
  demo 全量实测：交付档 203s → **18.7s**、预览档 185s → **16.9s**、热缓存 12.5s。
  老实现没删，`--opt zoom_engine=zoompan` 可退回；切片摘要把引擎算进去（只影响带推近的镜头，
  其余镜头缓存照旧复用）。

### 新增

- **`build --fast`（预览档的快捷方式）**：切片与成片都切到更快的编码档
  （切片 veryfast/crf28、成片 veryfast/crf26），编码重的片子上省得明显；
  它省的是编码、不是滤镜——推近换实现之后，这条链已经不再是瓶颈
  （demo 全量交付 18.7 秒 vs 预览 16.9 秒）。切片摘要里带着这面旗，所以**预览切片不会被
  当成交付切片复用**。人读的输出与 `--json` 的 `preview` 字段
  都会说清楚这是预览，结尾还有一句「要交付就去掉 `--fast` 再渲一次」——少了这句话，
  早晚有人把预览片交出去。
- **ffmpeg 后端的 `--dry-run` 真的预演了**：以前 `--dry-run` 只有剪映/达芬奇认，
  ffmpeg 后端会照渲不误。现在它不建目录、不起 ffmpeg、不写日志，先把每一段切片的
  来源 / 入点 / 时长 / 切片缓存名与成片路径列出来——想估「这次要渲几段、命不命中缓存」
  不必再真渲一遍。
- **CI 冒烟不再跑 19 分钟**：新增 `examples/demo/shotlist-ci.json`（540×960 / 4 镜 / 6 秒），
  冒烟改用这份小样 + `--fast`，本机 6 秒渲完、成片体检全绿；render job 加
  `timeout-minutes: 20`，挂了就红，不再占着默认 6 小时上限。
  冒烟证明的是「克隆 → 装 → 出片 → 体检能跑通」，不是画质评审——交付档的质量默认值一分没降。
- **`--dry-run` 不再在结尾误报失败**：以前预演打完计划表还会掉进失败分支，打一行
  `构建失败（ffmpeg）: `（错误是空的），让人以为预演出错了。现在预演只打计划表，
  `--json` 里也是 `ok: true` + `data.dry_run: true`。
- **`docs/quickstart.md`（三条命令的走查）**：先 `--dry-run` 看计划 → `--fast` 看节奏 →
  去掉 `--fast` 出交付版，附本机实测耗时表与「先数 `zoom` 再调 `-preset`」的结论。
  手边没有素材也能跑（第 0 步现造）。
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

- **重叠的镜头会被悄悄渲错**：`--force` 原来会放过「本镜与上一镜重叠」这种错误，
  concat 把两段接起来、`-t 总长` 再把尾巴切掉——于是「从 1.0s 开始」变成「从 2.0s 开始」，
  时长还正好对得上，`duration_match` 查不出来。现在渲染前直接拒绝：把重叠的说清楚是几处、
  从哪儿开始，改完再渲（截断前镜还是后移后镜是创作决定，机器不替用户选）。
  新增 `test_overlapping_timeline_is_refused`。

- **时间线有空档时整条片子左移**：concat 是首尾相接的，`tl_start` 跳过的那段被吃掉，
  后面的画面和声音整体提前——字幕写着 5s，画面里那一镜已经到 2s。现在空档补一段同长黑场
  （声音补等长静音），成片时长与时间线一致。实测：两段 2s、第二段从 5.0s 起，修前成片
  6.200s（`duration_match` 差 -0.800s），修后 **7.000s**、空档处 `YAVG=16`（纯黑）且
  `mean_volume -91 dB`（静音）。`build` 不带 `--force` 时仍会先拦下来问一句。
  新增 `test_gap_is_padded_with_black_instead_of_shifting`。

- **`qc` 也查节奏**：手写分镜表的人走不到 `story` 的节奏建议，所以 `qc` 新增
  `shot_rhythm` 一查——同一场镜头多（≥4）而素材少（≤2）、平均镜长偏长/偏碎、
  六成镜长挤在均值 ±0.2s、存在 >4s 长镜，逐条报 `Q106`/`Q107`/`Q108`（warn，不影响退出码）。
  新增 `tests/test_qc_rhythm.py` 五例。

- **几乎无声的片子被拉响度会放大底噪**：`loudnorm` 把一条 -65 LUFS 的无声片拉到
  -14 LUFS，等于把底噪抬 50 dB——成片里全是电流声。现在混音前先量环境声积分响度，
  低于 -50 LUFS（且没有配乐）就跳过归一化，并记一条「整片几乎没有声音…跳过响度归一化」。
  新增 `test_near_silent_film_skips_normalization`，断言成片仍 < -40 LUFS。

- **`story` 给出节奏建议**：每场只用了 1–2 段素材、平均镜长出格（>3.0s 或 <1.2s）、
  六成镜头的长度都挤在均值 ±0.2s、存在 >4.0s 的长镜——这几种机器判断得了的问题现在
  逐条写出来，且都指名场景、带数字与可执行动作（补拍几段 / 加切点 / 混短镜）。
  `--json` 里是 `report.rhythm` 数组。新增 `tests/test_story.py` 四条用例。

- **`import` 说清「丢了多少」**：以前只有一句笼统的「转场、关键帧、特效不还原」。
  现在从草稿里数出转场 / 特效·滤镜 / 关键帧各多少个（`materials.transitions`、
  `video_effects`/`effects`/`filters`、段落 `common_keyframes` 与全局 `keyframes`），
  命令行打印「不还原: 转场 2 个、特效/滤镜 1 个、关键帧 2 个」，`--json` 里是 `lost`。
  新增 `tests/test_jianying_reader.py::LostFeatureTest`（3 例）。
- **草稿片段缺 `source_timerange` 时读错长度**：原来回落到「素材全长」，于是一个 2 秒的
  片段被按 4 秒素材算，成片凭空多 2 秒。现在优先用片段在时间线上的长度（`target_timerange`），
  再退素材时长。新增 `MissingSourceRangeTest`。

- **qc 查「字幕读不读得完」**：新增 `text_readable`（第 23 项检查），按字数 ÷ 时长算
  字/秒，超过 `CAPTION_CPS_MAX = 12` 报 `warn` + `Q105`。以前只查「字幕在画面内」，
  可 30 个字挂 1.2 秒（25 字/秒）也在画面内，只是没人读得完。旁白层不可见时不计入。
  新增 `tests/test_qc_severity.py::CaptionReadableTest`（3 例）。

- **`qc` 会量响度与真峰值了**（20 项 → 22 项）：`loudness_match` 检查积分响度是否落在
  `-14 LUFS ±3`、`true_peak` 检查真峰值不超过 `-1 dBTP`。后端把片子拉到目标响度是一件事，
  真的拉到了是另一件事——以前没人验。偏轻/偏顶判 `warn`（不让整份体检失败），
  但报告里会写明差多少。新增 `tests/test_qc_loudness.py`（3 例）。

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
