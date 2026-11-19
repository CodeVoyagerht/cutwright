# 分镜表（shotlist.json）字段全表

一份分镜表就是一个 JSON 对象。**未知字段会被保留**（写进输出文件时原样带回），
所以你可以加自己的 `note` / `tags`，拿来给上游工具传话。

单位统一：时间一律**秒**（浮点），尺寸一律**像素**（整数），颜色一律 `#RRGGBB`。

## 顶层

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | str | `"untitled"` | 片名，也用作草稿名/时间线名的前缀 |
| `material_dir` | str | `""` | 视频素材根目录；`file` 相对它解析 |
| `asset_dir` | str | `""` | 叠加素材（PNG/字体等）根目录；为空则相对 `material_dir` |
| `canvas` | obj | `1080×1920` | `{"width":1080,"height":1920}`，成片画布 |
| `fps` | float | `30` | 时间线帧率，影响 zoompan 与草稿帧号 |
| `total_duration` | float? | `null` | 声明总时长；与实测偏差 >0.1s 会报 `W020` |
| `acts` | list | `[]` | 分幕，见下 |
| `video_segments` | list | `[]` | **必填**，至少一条 |
| `text_segments` | list | `[]` | 画面字幕/花字 |
| `vo_segments` | list | `[]` | 旁白（只出声不出字） |
| `audio_segments` | list | `[]` | 配乐/音效 |
| `image_overlays` | list | `[]` | 贴图叠加（PNG，带 alpha） |
| `styles` | obj | 内置默认 | 文字样式表，键名即样式名 |

## video_segments[]

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | str | — | **必填**，相对 `material_dir` 的路径 |
| `src_start` | float | `0` | 从素材第几秒开始取；别名 `src_start` / `start` |
| `src_dur` | float | — | 取多长；必填，除非给了 `src_end` 由它反算 |
| `src_end` | float | `null` | 取到素材第几秒（与 `src_dur` 二选一） |
| `tl_start` | float? | `null` | 时间线起点；**不写则自动首尾相接** |
| `speed` | float | `1.0` | 变速；`tl_dur = src_dur / speed` |
| `zoom` | str | `"none"` | `none` / `in` / `out`，Ken Burns 推拉 |
| `zoom_amount` | float | `0.05` | 推拉幅度，0.05–0.08 安全，>0.12 会糊 |
| `cx` | float | `0.5` | 横屏裁竖屏时的水平取景（0=左，1=右） |
| `still` | bool | `false` | 静帧：`file` 是图片（png/jpg/webp），按 `src_dur` 铺时长。没有入点、不能变速——写了 `src_start`/`speed` 会报 `W042`/`W043` 并忽略。图片忘写这个字段会报 `E061`（否则只出 1 帧），视频误加会报 `W044` |
| `act` | str | `""` | 所属幕，用于统计 |
| `shot` | int/str | `null` | 镜号，纯标记，后端用它做命名 |
| `desc` | str | `""` | 人看的备注，不参与渲染 |

## text_segments[]

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `text` | str | — | **必填**；用 `|` 或 `｜`（半角/全角都行）括起的关键词单独换色，见下 |
| `tl_start` | float? | `null` | 不写则接在上一段文字之后 |
| `tl_dur` | float | — | **必填**，显示时长 |
| `style` | str | `"caption"` | 引用 `styles` 里的键；未定义报 `E031` |

**关键词换色规则**：分隔符 `|` 与 `｜` 等价（中文输入法打的是后者），
**成对交替**——分隔符外是正文色，第一对之间是 `keyword_color`，第二对之间
又回到正文色。分隔符本身不显示、也不占字宽：

```
"自动裁剪｜变速｜补增益"     → 自动裁剪(白) + 变速(黄) + 补增益(白)
"普通一句没有关键词"         → 普通一句没有关键词(全白)
```

## vo_segments[]

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `text` | str | — | **必填**，旁白文本（给 TTS 或只做对轴用） |
| `tl_start` | float? | `null` | 不写则接在上一段旁白之后 |
| `tl_dur` | float | — | **必填** |
| `style` | str | `"vo_hidden"` | **必须是不可见样式**，否则文字会糊在画面上 |
| `act` | str | `""` | 所属幕 |

## audio_segments[]

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | str | — | **必填**，相对 `material_dir` |
| `tl_start` | float | `0` | 起点 |
| `tl_dur` | float? | `null` | 不写则到素材结束；建议比片长短 0.5–1.0s |
| `src_start` | float | `0` | 从音乐第几秒进 |
| `gain_db` | float | `0` | 额外增益，`music` 类默认已压到 0.30，一般不用加 |
| `kind` | str | `"music"` | `music` / `sfx` / `vo`，决定默认音量与 ducking |

## image_overlays[]

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | str | — | **必填**，PNG（带 alpha），相对 `asset_dir` |
| `tl_start` | float | `0` | 起点 |
| `tl_dur` | float? | `null` | 不写则到片尾 |
| `fade` | float | `0.18` | 淡入时长 |
| `fade_out` | bool | `false` | 是否淡出（默认硬切走） |
| `slide_px` | float | `0` | 水平滑入距离 |
| `slide_dur` | float | `0.24` | 滑入时长 |
| `float_amp` | float | `0` | 上下浮动幅度（px） |
| `float_period` | float | `4.0` | 浮动周期（秒） |

## styles{}

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `font_height_px` | float | `100` | 字号（像素高） |
| `color` | str | `#FFFFFF` | 正文色 |
| `keyword_color` | str | `#FFD54A` | `|` 后关键词的颜色 |
| `stroke_color` | str | `#000000` | 描边色 |
| `stroke_width_px` | float | `6` | 描边宽度 |
| `align` | str | `center` | `left` / `center` / `right` |
| `bottom_margin_px` | float? | `null` | 距底距离；与 `center_ratio_y` 二选一 |
| `center_ratio_y` | float? | `null` | 垂直位置比例（0=顶，1=底），花字/标题用 |
| `max_chars_per_line` | int | `0` | 每行最大字数，`0` 表示不自动折 |
| `rotation_deg` | float | `0` | 旋转角度，花字常用 `-7` |
| `invisible` | bool | `false` | 不可见（旁白层用） |
| `anim` | str | `none` | `none` / `slide_in_left_zoom` / `fade` |
| `font` | str | `""` | 字体文件路径；空则按系统字体回落 |

## 校验码对照

`validate` 的输出形如 `error E012 [video_segments[3]] src_dur 必须为正数`。

| 码 | 级别 | 含义 | 怎么修 |
|---|---|---|---|
| `E001` | error | canvas 宽高非法 | 给正整数，竖屏用 1080×1920 |
| `E002` | error | fps ≤ 0 | 一般 30 或 60 |
| `E010` | error | 没有 video_segments | 至少一个镜头 |
| `E011` | error | 镜头缺 `file` | 填文件名 |
| `E012` | error | `src_dur` ≤ 0 | 给正数，或改用 `src_end` |
| `E013` | error | `src_start` < 0 | 不能取负数区间 |
| `E014` | error | `speed` ≤ 0 | 常用 1.0 / 1.25 / 1.5 |
| `W015` | warn | 未知 `zoom` 值 | 只用 none/in/out |
| `W016` | warn | `cx` 越界 | 夹在 0–1 |
| `E017` | error | 素材不存在（`--check-files`） | 放对目录或改文件名 |
| `W018` | warn | 时间线有空档 | 删掉手写的 `tl_start` 让它自动排 |
| `W019` | warn | 镜头重叠 | 同上 |
| `W020` | warn | `total_duration` 与实际漂移 | 更新或删掉该字段 |
| `E030` | error | 文字为空 | 删掉这条或填内容 |
| `E031` | error | 引用未定义样式 | 在 `styles` 里补，或改用内置名 |
| `E032` | error | `tl_dur` ≤ 0 | 给正数 |
| `E033` | error | `tl_start` 为负 | 从 0 开始 |
| `W034` | warn | 文字起点超出片尾 | 缩短 `tl_start` 或加镜头 |
| `W042` | warn | 静帧写了 `src_start` | 删掉；静帧没有入点 |
| `W043` | warn | 静帧写了 `speed` | 删掉；静帧不变速 |
| `E061` | error | 图片没写 `still`（会被当成视频，只出 1 帧） | 给这条镜头加 `"still": true` |
| `W044` | warn | 静帧标记打在了视频上（会 `-loop 1` 重复那一帧） | 确认是有意的，否则删掉 `still` |
| `E040` | error | 叠加层缺 `file` | 补文件名 |
| `E041` | error | 叠加素材不存在 | 同上 |
| `E047` | error | 叠加层 `tl_dur` ≤ 0 | 给正数 |
| `E048` | error | 叠加层 `tl_start` 为负 | 从 0 开始 |
| `W047` | warn | 叠加层起点在片尾之后（永远不会出现） | 挪进时间线内 |
| `W048` | warn | 叠加层结束超出片尾（会被截断） | 缩短 `tl_dur` |
| `E050` | error | 音频缺 `file` | 补文件名 |
| `E051` | error | 音频不存在 | 同上 |
| `E052` | error | 音频 `tl_dur` ≤ 0 | 给正数 |
| `E053` | error | 音频 `tl_start` 为负 | 从 0 开始 |
| `W045` | warn | 音乐起点在片尾之后（永远不会响） | 挪进时间线内 |
| `W046` | warn | 音乐结束超出片尾（会被截断） | 缩短 `tl_dur`，或把片子加长 |
| `E060` | error | `font_height_px` ≤ 0 | 给正数 |

## 宽松解析（故意的）

数值字段能吃字符串：`"src_dur": "2.5"` 可以。字段别名认 `src`（= `file`）、
`start`（= `src_start`）、`out`（= `src_end`）。坏值**静默回落默认值**而不是抛异常。

理由：分镜表通常由另一个脚本生成，或由 agent 手写。一个字段类型不对就让整份
表打不开，会把错误推到最远的地方才暴露。`validate` 负责把可疑之处喊出来，
解析器负责别让流程断掉。
