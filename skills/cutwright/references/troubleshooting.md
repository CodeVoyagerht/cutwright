# 报错 → 原因 → 处理

按「你会先看到哪句话」排。每条都给**可执行的下一步**，不给安慰。

## doctor / 环境

**`[MISS] ffmpeg`**
没装或不在 PATH。
→ `winget install Gyan.FFmpeg`（Windows）/ `brew install ffmpeg`（macOS）；
或下载后设 `CUTWRIGHT_FFMPEG=D:\path\to\ffmpeg.exe`（`ffprobe` 会自动在同目录找）。
**不要**去改系统 PATH——受限环境里改不动，还会让问题更难定位。

**`[MISS] 剪映草稿库`**
草稿库里应该有 `root_meta_info.json`，没找到说明路径不对或剪映没真正装过草稿。
→ 用 `CUTWRIGHT_JY_DRAFTS` 显式指定，例如
`CUTWRIGHT_JY_DRAFTS="D:\Jianying\JianyingPro Drafts"`。
也可以在 `--opt drafts_dir=...` 里传。

**`[MISS] 达芬奇 Support`**
没装 Resolve，或装在非默认位置。
→ macOS 在 `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Support`；
Windows 在 `%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support`。

**`模块加载失败: ImportError: No module named 'DaVinciResolveScript'`**
只影响 `resolve` 后端，其余照常可用。这个模块由达芬奇在运行时注入。
→ 在达芬奇里跑脚本，或确认 `RESOLVE_SCRIPT_API` 指向 `.../Developer/Scripting`。

## validate

**`E017 素材不存在`**
`--check-files` 在查素材。`file` 相对 `material_dir`，不是相对当前目录。
→ 看报错里点的那个完整路径；`material_dir` 为空时是相对进程 cwd。

**`W018/W019 空档/重叠`**
手写了 `tl_start`，但改了镜头顺序或改了 `src_dur`。
→ 删掉所有 `tl_start`，让它们自动首尾相接；只有刻意留白的地方才写死。

**`E031 引用未定义样式`**
`styles` 里没有那个键，且不是内置名。
内置名只有：`caption` / `title` / `huazi` / `end` / `vo_hidden`。

**`W020 total_duration 漂移`**
声明的总时长和实际差超过 0.1s。
→ 要么更新数字，要么删掉这个字段（它只用于自检，不参与渲染）。

## plan

**`偏差 -1.2s 超出容差`**
`--target` 到不了。
→ 加镜头（推荐），或放宽 `min_shot`（镜头会变碎），或换 `--strategy speed`
（会改节奏，慎用）。**不要**把 tolerance 调大来骗自己。

**`所有镜头都已到 min_shot 下限`**
素材不够短。
→ 减少 `src_dur` 之外别无他法；真要压时长就加镜头数。

## build

**`剪映正在运行，已中止`**
写到一半的草稿会让剪映崩。
→ 退出剪映（托盘也要退干净）再跑。

**`时长偏差 +0.8s`（成片体检）**
常见原因：最后一条 `image_overlay` 的 `tl_dur` 超出了片尾，或配乐比片长，
`amix` 以 `duration=first` 截断但音频比视频长时首帧会被拖。
→ `qc` 看 `overlays_valid`；把超出的叠加层删掉或缩短。

**`没有音轨`**
`audio_segments` 为空，而原声也被 `an` 掉了。
→ 至少给一条 `audio_segments`，或在没有原声时用静音占位。

**`2 段黑场，共 0.85s`**
镜头之间有未覆盖的空隙，或某段素材本身开头就是黑的。
→ 看黑场时间点，对照 `tl_start` 找那段空隙。

**`字体全挤在一起`**
字体文件不存在 → 回落到默认字体，字宽变化导致排版崩。
→ `C:\Windows\Fonts` 里确认真有那个文件名；或把 `styles[*].font` 留空用系统默认。

**渲染很慢**
`preset=slow` 是给成片的。要快速预览：`--opt preset=veryfast --opt crf=24`。

## qc

**`vo_hidden FAIL`**
旁白样式被改成了可见。
→ `vo_segments[].style` 必须是 `invisible: true` 的样式。

**`sources_exist FAIL`**
素材不在盘上。渲染前就会被 `validate --check-files` 拦下，这里是兜底。

**`no_overlaps FAIL`**
两条镜头在时间线上叠了。剪映/达芬奇能接受（上层覆盖下层），ffmpeg 不能，
后一条会把前一条盖掉。

## 技能本身

**agent 不触发这个技能**
`description` 里要有用户会说的词（剪辑、出片、草稿、达芬奇），且 `name` 必须等于
目录名 `cutwright`。改完重启客户端。

**`找不到 cutwright`（verify.py）**
先 `pip install -e .`；或从仓库根跑（脚本会把 `src/` 加进 `PYTHONPATH`）。
