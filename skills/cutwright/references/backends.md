# 三个后端：选项、产物、依赖

同一份分镜表，三种落地方式。**选后端只看一个问题：接下来谁要继续动这条片子？**

| 你要的下一步 | 选 | 原因 |
|---|---|---|
| 直接发布 | `ffmpeg` | 一步到位，无需任何 NLE |
| 在剪映里加特效/贴纸/音效库素材 | `jianying` | 草稿可编辑，时间线结构完整 |
| 调色、多版本交付、套 LUT | `resolve` | 节点图与 LUT 是达芬奇的地盘 |

```bash
cutwright build shotlist.json -b ffmpeg   -o out
cutwright build shotlist.json -b jianying --opt install=true
cutwright build shotlist.json -b resolve  --opt grade=lut.cube
```

## ffmpeg 后端

**依赖**：`ffmpeg` + `ffprobe`；烧字幕/花字需要 Pillow（`pip install -e ".[render]"`）。

**产物**：`<out>/<name>.mp4`（H.264 + AAC，`+faststart`），以及 `report.json`。

**选项**（`--opt k=v`）

| 键 | 默认 | 说明 |
|---|---|---|
| `crf` | `18` | 成片质量，17–20 是肉眼无差的区间；降到 14 只为中间产物 |
| `preset` | `slow` | 编码速度/体积；慢机器改 `medium` |
| `audio` | `true` | 是否混音轨；纯画面交付时关掉 |
| `grade` | `""` | 整片调色滤镜串，如 README 里那条 `eq=...` |
| `overlay` | `true` | 是否烧 `image_overlays` |
| `jobs` | `min(4, CPU, 待切镜头数)` | 并行切片数，`1` 是串行；钳在 1–32 |
| `cache` | `true` | 切片按内容摘要复用（`_build/seg_<digest>.mp4`） |
| `preview` | `false` | 快编档：切片 `-crf 30 -preset veryfast`，成片 `26/veryfast`。**只改画质不改时长**——帧数与正式档逐帧一致，预览看到的节奏就是成片的节奏 |
| `keep_temp` | `true` | 保留 `_build/` 中间件（复用靠它） |
| `duck` | `true` | 配乐给人声让路（侧链压缩，以环境声为侧链）。关掉就是固定音量相加 |
| `duck_threshold` | `0.05` | 侧链触发阈值（0.001–1.0，线性）。人声很轻时压不下去就调小 |
| `duck_ratio` | `8` | 压缩比（1–20）。越大让路越狠 |
| `loudness` | `-14` | 成片目标响度（LUFS）。混完统一过 `loudnorm`，峰值压到 -1 dBTP；`false` / `0` 关掉，保留原始电平。**整片低于 -50 LUFS 时自动跳过**并记警告——无声片被拉满只会放大底噪 |
| `music_loop` | `true` | 配乐比该段短时循环铺满；`false` 就只警告、尾巴留静音 |
| `dry_run` | `false` | 只打印命令不执行 |

**缓存摘要里有什么**：源文件绝对路径 + `mtime:size`、`src_in`、`src_dur`、`speed`、
`zoom`/`zoom_amount`/`cx`、画布宽高、fps、是否 preview。**没有**字幕文案、配乐、镜头顺序
——改一句字幕不该重切 40 个镜头。改了切片相关的滤镜逻辑要把 `text.RENDER_VERSION`
那样的版本位一起推进，否则会复用旧片。

实测（8 镜 / 12.9s / 1080×1920）：串行无缓存 9.44s → 4 路并行 7.85s → 并行 + 命中缓存
5.58s。瓶颈在最后那条 compose 命令，它是单进程的。

**管线**（照搬一条已跑通 86 秒真片的实现，不是重写）：

1. 逐镜切片：`-ss/-t` 取段 → HDR 时 `zscale` tonemap → 全范围转 tv →
   变速 `setpts` → 竖屏适配 `scale=...:force_original_aspect_ratio=increase,crop=...`
   → `zoompan` 推拉 → `libx264 -crf 15` 中间编码。
2. 逐镜原声：`highpass=f=50` → 用 `volumedetect` 量 `mean_volume` →
   `gain = clamp(-20 - mean, -10, 24)dB` → 首尾 30ms 淡入淡出 → `alimiter=0.95`。
3. concat 段（`-c copy`），叠加 PNG（带 `fade` 与 `enable=between`），
   `amix` 混配乐（原声 0.9 / 配乐 0.30 / `normalize=0`），片头 0.12s 淡入、
   片尾 0.35s 淡出，最后 `-crf 18 -movflags +faststart`。
   时间线上有空档（`tl_start` 与上一镜不接）时先插一段同长黑场再 concat——concat 会把片段首尾相接，不补就会让后面的镜头整体左移。

## 剪映后端（11.x）

**依赖**：剪映专业版 11.x 已安装（要它的 `videoeditor.dll` 来加密草稿）。

**产物**：`<out>/<name>/` 草稿目录；`--opt install=true` 时再装进草稿库。

**选项**

| 键 | 默认 | 说明 |
|---|---|---|
| `install` | `false` | 装进草稿库并更新 `root_meta_info.json`（自动备份） |
| `update_index` | `true` | 只影响索引；`false` 时不碰 `root_meta_info.json` |
| `encrypt` | 自动 | 检测到 `videoeditor.dll` 就加密，检测不到写明文并 warning |
| `drafts_dir` | 自动 | 覆盖草稿库路径（也认 `CUTWRIGHT_JY_DRAFTS`） |

**必须知道的三件事**

1. **草稿是加密的**。剪映 6.0 起 `draft_content.json` 就不是明文了；盘上是
   base64 文本，解出来是「头部 + 密文」，比明文恒定多 52 字节。
2. **要写两份**。11.6 有 `Timelines/<UUID>/draft_content.json` 镜像，
   只写根目录那份，打开还是旧内容。
3. **改索引前剪映必须退出**。进程开着一律拒绝执行——不是为了礼貌，
   是因为写到一半的索引会让剪映打不开草稿库。

**不做的事**：不替你导出视频。剪映 11.x 是 Qt/QML，UI Automation 一个控件都不暴露，
任何声称「能自动导出」的库都在截图匹配，脆得没法用于交付。草稿给你，导出点按钮。

**读回来（`cutwright import`）**

```bash
cutwright import "<草稿目录>" -o shotlist.json          # 加密草稿会自动解密
cutwright import "<草稿目录>" -o shotlist.json --plain-only   # 绝不加载 dll
```

拿回的是**排布**：每镜的源文件、入点、时长、变速、时间线位置，字幕文案与时间，音频位置。
不还原：字幕样式、转场、关键帧、特效、调色（猜错的样式比没有更糟，宁可让用户重设）。
两个已知的语义损失：

- **旁白和字幕分不开**——草稿里两者都是文字轨，读回来一律当字幕，`warnings` 里会明说。
- **素材路径是绝对路径**——换机器后 `missing_files()` 会列出失效的条目，别当作素材还在。

加密草稿要本机有可用的 `videoeditor.dll`（11.6 那份自检通过；11.7 解不开自己的密文，
`doctor` 会说明原因）。`--plain-only` 时遇到密文直接报错退出，不会给你一份空表。

## 达芬奇后端

**依赖**：Resolve **Studio** 20.x+，Preferences 里打开
`System → General → External scripting using = Local`。
（免费版没有这个开关，这是 Blackmagic 的产品划分，不是本项目的限制。）

**产物**：当前项目里的一个新时间线，逐镜入轨。

**选项**

| 键 | 默认 | 说明 |
|---|---|---|
| `grade` | `""` | LUT 文件路径，逐镜套上 |
| `timeline_name` | `<name>_<时间戳>` | 显式指定时间线名 |
| `fps` | 分镜表 `fps` | 时间线帧率（需与项目帧率兼容） |
| `replace` | `false` | 同名时间线已存在时替换而不是失败 |

**API 上的三个坑**

1. `TimelineItem.SetLUT()` **19.0.0 起已移除**。现在要
   `item.GetNodeGraph().SetLUT(nodeIndex, path)`，`nodeIndex` 从 1 开始。
   写旧 API 的代码在 20.x 上静默失败。
2. 套 LUT 前必须 `Project.RefreshLUTList()`，且 LUT 要放在
   `<Support>/LUT/` 下，否则不报错也不生效。
3. `Timeline.DetectSceneCuts()` 存在，但它是**破坏性**的：直接在你当前时间线上
   打切点，还是异步的。切点一律用 ffmpeg 算好再喂进来。

**为什么不用 `CreateTimelineFromClips`**：它只认
`mediaPoolItem / startFrame / endFrame / recordFrame`，不认轨与类型，也表达不了
「第 3 镜从素材 1.5s 取 2.4s、放在 5.2s」。所以走逐条 `AppendToTimeline`。

## 依赖矩阵

| 能力 | ffmpeg | jianying | resolve |
|---|---|---|---|
| 无 NLE 直出成片 | ✅ | ❌ | ❌ |
| 草稿可再编辑 | ❌ | ✅ | ✅ |
| 烧字幕到画面 | ✅（需 Pillow） | ✅（草稿内） | ❌ |
| 变速 / 推拉 | ✅ | ✅ | ✅ |
| LUT 调色 | 近似（滤镜） | ❌ | ✅（真 LUT） |
| CI 可用 | ✅ | ❌ | ❌ |
| 需要商业授权 | ❌ | 剪映 | Resolve Studio |
