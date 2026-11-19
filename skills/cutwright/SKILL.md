---
name: cutwright
description: 把分镜表 JSON 或短剧剧本剪成成片、剪映草稿或达芬奇时间线。Use when 用户要自动剪辑、批量出片、把一堆素材排成分镜表、按剧本/台词剪短剧、生成剪映草稿或达芬奇时间线、或者要求"按文案配画面"；do not use for 需要逐帧手工创意特效、音效设计、或必须人工审美判断的精剪。
license: MIT
compatibility: 需要 Python 3.9+。ffmpeg 后端需 ffmpeg/ffprobe 在 PATH；剪映后端需剪映专业版 11.x；达芬奇后端需 Resolve Studio 且开启外部脚本。核心零第三方依赖，烧字幕需 Pillow。
metadata:
  author: CodeVoyagerht
  version: "0.1.0"
  repo: https://github.com/CodeVoyagerht/cutwright
---

# 剪匠 cutwright

**一份分镜表进，一条片子出。** 三条产线共用同一份 IR：直出 MP4、剪映草稿、达芬奇时间线。

剪辑里真正耗时的部分——36 个镜头按 86 秒排好、21 条字幕卡在呼吸点上、6 段旁白对齐、
配乐在结尾刚好收住——都是**排布问题**，不是审美问题。排布问题写成 JSON，
剩下的交给 `cutwright`。

## 什么时候用

| 用户说的是 | 用不用 | 怎么做 |
|---|---|---|
| "把这些素材剪成一条竖屏视频" | ✅ | 工作流 A |
| "按这份文案配画面" | ✅ | 先把文案拆成分镜表，再工作流 A |
| "按这个剧本剪一集短剧" | ✅ | 工作流 D（剧本 → 分镜表 → 成片） |
| "这堆素材你自己挑镜头排" | ✅ | 工作流 D 的第一步 `index`，再手排或 `story` |
| "给我生成剪映草稿，我要自己再改" | ✅ | 工作流 B |
| "进达芬奇调色" | ✅ | 工作流 C |
| "帮我批量出 20 条同模板视频" | ✅ | 工作流 A + 循环换分镜表 |
| "把这个镜头的转场改成故障风" | ❌ | 这需要逐帧创意，直接说做不到并给剪映/达芬奇的手动路径 |
| "帮我想一个爆款选题" | ❌ | 不是剪辑问题 |

## 铁律（不许跳）

1. **先 `doctor`**。不知道哪个后端可用就开始 build，等于盲开。缺什么就照它说的装，
   装不了就换后端，别硬上。
2. **`validate` 有 error 就不许 build**。`--force` 只在用户明确说"我知道有问题，先出个能看的"时用。
3. **不许说"应该没问题"**。要么跑 `qc` 拿到数字，要么说"我没验证"。片子是你交的，
   黑场和空档也是你交的。
4. **素材缺失就明说缺哪几个文件**，不要用占位符糊过去——渲染会成功，片子是废的。
5. **写完分镜表一定要 `plan` 看一遍统计**。镜头数、平均镜长、分幕时长，这三个数字
   不对，后面全白干。

## 工作流 A：从素材到成片

```bash
cutwright doctor                                  # ① 确认 ffmpeg 可用
cutwright init work                               # ② 生成骨架（不需要素材）
```

> **`cutwright` 报 command not found？** pip 装完之后 `Scripts\` 常常没进 PATH（Windows 尤其常见）。
> 这时 `python -m cutwright doctor` 跟 `cutwright doctor` 完全等价，下面所有命令都能这么写，
> 别在 PATH 上卡住。

**③ 写分镜表**。这是唯一需要判断力的步骤，看下面「剪辑判断规则」。
素材放 `work/materials/`，文件名与 `video_segments[].file` 对齐。

```bash
cutwright validate work/shotlist.json --check-files   # ④ 有 error 就改，别往下走
cutwright plan     work/shotlist.json --target 45     # ⑤ 看统计，凑时长
cutwright build    work/shotlist.json -b ffmpeg -o work/out   # ⑥ 出片 + 自动体检
```

第 ⑥ 步结尾会自动打印成片体检结果。**任何一项 FAIL 都要处理或如实报告**，
尤其是 `duration_match`（时长漂移）、`av_duration_match`（画面与声音不等长，
通常意味着某段切片坏了）和 `no_long_black`（黑场）。

## 工作流 B：剪映草稿

```bash
cutwright build work/shotlist.json -b jianying --opt install=true
```

- `install=true` 会装进草稿库并改 `root_meta_info.json`（自动备份 `.bak-<时间戳>`）；
  只想要草稿目录不想动索引，就传 `install=false`，之后手动导入。
- **剪映进程开着时会拒绝执行**，这是故意的：写到一半的草稿会让剪映崩。
- 字体路径写进草稿后是绝对路径，跨机打开会退默认字体，这属于剪映的行为，不是 bug。

## 工作流 C：达芬奇

```bash
cutwright build work/shotlist.json -b resolve --opt grade=path/to/lut.cube
```

- 需要 Resolve **Studio** 且 Preferences 里打开外部脚本（免费版没有这个开关）。
- 建线后逐镜分析、逐镜套 LUT。LUT 必须先 `RefreshLUTList()`，否则静默失败——
  这一步已经封装好了，不要自己去调 API。
- 切点由 ffmpeg 算，**不要用 `DetectSceneCuts()`**：它会当场在时间线上打切点，
  是破坏性操作，不是只读分析。

## 工作流 D：剧本 → 分镜表 → 成片

有剧本（短剧、对话戏、解说台本）时走这条。人不写镜头，只写戏。

```bash
cutwright index materials -o materials.json        # ① 素材入库：按文件名打标签
cutwright story screenplay.json --index materials.json -o shotlist.json   # ② 排片
cutwright validate shotlist.json --check-files     # ③ 有 error 别往下走
cutwright build shotlist.json -o out               # ④ 出片（或 -b jianying 进剪映）
```

- **顺序不能反**：`story` 只认索引里的时长，不扫文件系统。换了素材必须重跑 `index`。
- **`story` 的报告必须看 `unresolved`**：非空意味着某句台词没有画面，成片里是一段空白。
- 场次的 `tags`（地点+时间+气氛）决定挑素材的质量，写漏了机器就满库乱挑。
- 素材命名约定 `场次_场景_时间_景别_谁`（`S01_subway_night_wide.mp4`），`index` 会自动
  抽成标签；`who` 能对上 `cast` 时，台词优先选这个人的镜头。
- 完整字段与打分规则见 [references/screenplay-schema.md](references/screenplay-schema.md)，
  命名、验收、坑见 [references/story-workflow.md](references/story-workflow.md)。

## 剪辑判断规则

这些是"分镜表里该填什么数"的经验，不是代码能替你决定的。

**节奏**
- 竖屏口播：平均镜长 **2.0–3.0s**；开场前 3 秒必须有两个以上镜头切换，否则划走。
- 单一动作的特写可以到 4s；纯空镜不要超过 2.5s。
- `speed` 只用 1.0 / 1.25 / 1.5 三档。0.8 这种值会让动作看起来"卡"，不自然。
- 同一镜头连续两次用 `zoom: "in"` 会显得刻意，隔一镜用一次。

**字幕**
- `tl_start` 比镜头切换晚 **0.15–0.25s**，`tl_dur` 比句子读完早 **0.1–0.2s**。齐着切点
  是最常见的业余感来源。
- 一行不超过 **14 个中文字**（用 `|` 或 `｜` 把关键词成对标出来换色，别指望它换行）。
- 同屏只留一条字幕。两条叠在一起时，先看是不是 `vo_hidden` 没设 `invisible`。

**旁白**
- `vo_segments` 的样式**必须**是 `invisible`（默认就是），它只出声不出字。
- 旁白句尾留 **0.3s** 空白再切镜，否则听起来赶。
- 旁白和字幕不要逐字重复——重复会让观众同时读两遍。

**音乐**
- 配乐 `tl_dur` 比成片短 **0.5–1.0s**，让最后一下收在片尾淡出里，而不是被硬切。
- `kind: "music"` 默认音量已经压到 0.30，不要再加 `gain_db`，除非原曲特别轻。
- 找卡点用 `cutwright` 的 `beats` 能力（`pip install -e ".[beats]"`），
  不要凭感觉写 `tl_start`。

**构图**
- 横屏素材进竖屏：`cx` 决定裁哪边。人偏左就给 `cx: 0.35–0.4`，居中就省略。
- `zoom: "in"` + `zoom_amount: 0.05–0.08` 是安全区；超过 0.12 会明显糊。
- HDR 素材（iPhone 的 HLG/Dolby）不用特殊处理，管线会自动 tonemap 到 709。

**短剧（走工作流 D 时）**
- 一句台词 1 镜最舒服；一句 6 秒以上才需要拆（`--max-shot 2.5` 左右），拆出来的
  长短交错是节奏，均分是机械感。
- 对话戏的平均镜长比口播短：**1.6–2.4s**。过 3s 就像舞台剧录像。
- 一场戏至少 2 句台词、2 个镜头，否则观众会觉得画面卡住了。
- 旁白（`voiceover`）默认不出字是对的——内心独白变成字幕会显得廉价，
  除非这是解说类内容（那时加 `--vo-captions`）。
- `who` 一定要写全，它是「这段画面里有没有这个人在场」的唯一线索。

## 命令速查

| 命令 | 用途 |
|---|---|
| `doctor` | 环境自检，先跑这个 |
| `validate <s>` | 静态检查，`--check-files` 连素材一起查 |
| `plan <s> --target N` | 统计 + 时长拟合，`--out` 写出规整后的表 |
| `build <s> -b <backend> -o <dir>` | 出产物，`--opt k=v` 传后端选项 |
| `qc <s> [--mp4 f]` | 体检时间线，给 mp4 则连成片一起 |
| `init <dir>` | 生成示例 |
| `index <素材目录> -o idx.json` | 扫素材：按文件名打标签、量可用时长 |
| `story <剧本> --index idx.json -o s.json` | 剧本 → 分镜表，打印分场报告 |
| `import <草稿目录> -o s.json` | 剪映草稿 → 分镜表（`--plain-only` 表示绝不加载 dll） |

`build` 的提速选项：`--fast`（= `--opt preview=true`，低码率快编一版看节奏；**结尾会提醒
别拿去交付**）、`--opt jobs=4`（多镜并行）、`--dry-run`（只列每段切片的来源/入点/时长，
不建目录不起 ffmpeg）。切片默认按内容摘要缓存，同一份素材第二次渲直接复用；
预览与交付的切片缓存键不同，互不污染。

赶进度用 `--fast` 出预览、定下节奏后再去掉它渲交付档——这是本工具推荐的用法，
不是「先快后忘」的借口：交付的那一版必须是不带 `--fast` 的。

省多少要看片子：编码重的省得多。推近（Ken-Burns）以前是最贵的一步，现在默认走
「2 倍画布 + 逐帧改缩放 + 居中裁剪」，一镜 3 秒从 115 秒降到 3.8 秒（SSIM 0.985），
所以冷启一条 7 镜竖屏片十几秒就够。老实现可用 `--opt zoom_engine=zoompan` 退回。
完整走查（含本机耗时表、手边没素材也能跑）：`references/quickstart.md`。

全命令支持 `--json`：stdout 只有 JSON，日志走 stderr。给机器读就用它。

## 验收清单

交付前逐条过，**每条都要有输出**，不能靠记忆：

- [ ] `cutwright doctor` → 目标后端显示 `[OK]`
- [ ] `cutwright validate <s> --check-files` → 退出码 0
- [ ] `cutwright plan <s>` → 镜头数、平均镜长、分幕时长都符合预期
- [ ] `cutwright build ...` → 退出码 0，且成片体检全 `[OK]`
- [ ] 成片时长与 `plan` 报的偏差 < 0.25s
- [ ] 随机抽 3 个镜头，确认没有黑场、没有裁错边
- [ ] 字幕没有越界、没有两条同屏、关键词换色生效
- [ ] 走剧本产线时：`story` 报告的 `unresolved` 为空，每场镜头数 ≥ 台词数一半
- [ ] 交付时给出**产物路径 + 时长 + 分辨率 + 体检结论**，缺一不可

## Gotchas

真实踩过的坑，按出现频率排：

1. **`tl_start` 写死又改了镜头顺序** → 空档和重叠一起出现。删掉 `tl_start` 让它自动排。
2. **`vo_hidden` 样式被复制成可见样式** → 旁白变成糊在画面上的大字幕。`qc` 会报
   `vo_hidden` 失败，看到就查样式表。
3. **剪映草稿只写根 `draft_content.json`** → 打开还是旧内容。11.6 有
   `Timelines/<UUID>/` 镜像，必须两处都写（后端已处理，别自己改）。
4. **字体文件不存在** → 渲染时退默认字体，字全挤在一起。先 `fc-list` 或翻
   `C:\Windows\Fonts` 确认真有。
5. **ffmpeg 不在 PATH** → `doctor` 显示 MISS。用 `CUTWRIGHT_FFMPEG` 指路径，
   不要去改系统 PATH（沙箱里改不动）。
6. **`plan --target` 报偏差** → 素材余量不够或镜头都到 `min_shot` 下限了。
   加镜头数，别调 tolerance 骗自己。
7. **成片比预期长** → 大概率最后一条 `image_overlay` 的 `tl_dur` 超了片尾，
   `qc` 的 `overlays_valid` 会告诉你。
8. **`story` 排出来全是同一段素材** → 素材文件名没有可用的标签（`VID_2026.mp4`），
   改文件名重跑 `index`，或者手写索引的 `tags`。
9. **剧本改了 fps 之后时长全错** → `S003` 会说 fps 非法；这支工具不会悄悄把 fps
   修成 30，因为那会把「写错了」伪装成「算错了」。
10. **`import` 读完一重建，旁白跑到画面上** → 草稿里旁白也是文字轨，读的时候分不开，
    一律按字幕给。重建前把 `vo_hidden` 那几条改回旁白，或者 `--opt` 时关掉字幕。
11. **`import` 报「解不开草稿」** → 先 `cutwright doctor` 看剪映的加密 dll 可用性；
    11.7 的 dll 会解不开自己的密文（已知），要 11.6 那份。只想读明文就加 `--plain-only`。

## 文件索引

- [references/shotlist-schema.md](references/shotlist-schema.md) —— 分镜表全部字段、类型、默认值、校验码
- [references/screenplay-schema.md](references/screenplay-schema.md) —— 剧本/素材索引字段、S0xx 校验码、排片三规则
- [references/story-workflow.md](references/story-workflow.md) —— 短剧产线四步、素材命名、验收清单、坑
- [references/backends.md](references/backends.md) —— 三个后端的选项、产物结构、依赖矩阵
- [references/troubleshooting.md](references/troubleshooting.md) —— 报错原文 → 原因 → 处理
- `scripts/verify.py` —— 一步跑完 validate + plan + qc，退出码即结论（CI 与 agent 都用它）
- `scripts/install.py` —— 把本技能装到 DSH / Claude Code / Cursor / Codex / Gemini 的 skill 目录
- `assets/example-shotlist.json` —— 可直接 `build` 的最小分镜表
