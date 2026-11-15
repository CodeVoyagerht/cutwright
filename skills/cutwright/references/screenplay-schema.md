# 剧本与素材索引（screenplay / materials）

两条产线的输入不一样，别混：

| 你想剪的东西 | 输入 | 命令 |
| --- | --- | --- |
| 口播、旅拍、产品展示 | 分镜表 `shotlist.json`（人排出每个镜头） | `cutwright build` |
| 短剧、对话戏、有剧本的内容 | 剧本 `screenplay.json` + 素材索引 `materials.json` | `cutwright story` → `build` |

剧本产线最后也是吐出一张分镜表再 `build`——**剧本是更省人力的上游**，不是另一套渲染。

---

## 一、剧本 `screenplay.json`

```json
{
  "name": "地铁追凶 第1集",
  "canvas": {"width": 1080, "height": 1920},
  "fps": 30,
  "cast": {"lin": "林队", "zhou": "周小满"},
  "material_dir": "materials",
  "vo_dir": "vo",
  "music": {"file": "materials/bgm.mp3", "gain_db": -20},
  "scenes": [
    {
      "id": "S1",
      "slug": "站台 · 夜",
      "tags": ["subway", "night"],
      "lines": [
        {"type": "action", "text": "末班车进站，人群散开"},
        {"who": "zhou", "text": "你确定他会来？"},
        {"who": "lin", "text": "他欠我一条命", "duration": 2.4}
      ]
    }
  ]
}
```

### 顶层字段

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | str | `"screenplay"` | 只用于标题/文件名 |
| `canvas` | obj | 1080×1920 | 同分镜表，`width`/`height` |
| `fps` | num | `30` | 必须 1–240。写错会被 `S003` 拦下，**不会**被悄悄改成 30 |
| `cast` | obj | `{}` | `{代号: 中文名}`；`who` 写代号或中文名都行 |
| `material_dir` | str | `""` | 相对剧本文件所在目录 |
| `vo_dir` | str | `""` | 旁白音频目录，相对剧本文件 |
| `music` | obj | `{}` | `{file, gain_db, fade_out}`，缺 `file` 就没有配乐 |
| `styles` | obj | `{}` | 覆盖默认样式，key 是角色名（`caption`/`title`/`huazi`/`end`） |
| `scenes` | list | `[]` | 场次，见下 |

### 场次 `scenes[]`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | str | 必填，`S1`/`S01` 随意，但**要唯一**；也是分镜表里的 `act` |
| `slug` | str | 给人看的标题，会出现在 `plan` 的分幕行 |
| `tags` | list/str | 这场戏挑素材的关键：`["subway","night"]` 或 `"subway，night"` |
| `lines` | list | 台词，见下 |
| `location` / `mood` | str | 会被追加进 `tags`（写剧本时顺手写的位置/情绪） |

### 台词 `lines[]`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `text` | str | — | 必填。`谁说的话`；`action`/`title` 也用它填 |
| `type` | str | `dialogue` | `dialogue` / `voiceover` / `action` / `title` |
| `who` | str | `""` | 代号或中文名；决定挑素材时的「谁在场」加分 |
| `tags` | list | `[]` | 这一句专属的挑素材标签（`["close","hand"]`） |
| `duration` | num | 估算 | 显式秒数，写了就不按字数估 |
| `pause` | num | `0.12` | 这句之后的停顿（秒） |
| `vocal` | str | `""` | 旁白音频，相对 `vo_dir`；存在时**用音频真实时长** |
| `style` | str | 按 `type` | 覆盖字幕样式 |
| `shot` | str | `""` | 备注，写进分镜表的 `shot` |

行类型与默认样式：`dialogue`→`caption`（出字），`action`→`caption`（出字），`title`→`title`（大字卡），`voiceover`→`vo_hidden`（**不出字**，只进旁白轨）。

别名：`line`、`say` 等价于 `text`。

### 校验码（`cutwright validate --screenplay`）

| 码 | 级别 | 含义 |
| --- | --- | --- |
| S001 | error | 一个场次都没有 |
| S002 | error | 画布非法（宽高非正） |
| S003 | error | fps 非法（不在 1–240） |
| S010 | warn | 场次缺 `id` |
| S011 | error | 场次 `id` 重复 |
| S012 | warn | 场次里没有台词 |
| S013 | error | 场次 `duration` ≤ 0 |
| S020 | error | 未知行类型 |
| S021 | error | 台词是空的 |
| S022 | error | 台词 `duration` ≤ 0 |
| S023 | warn | `who` 不在 `cast` 里 |
| S024 | error | `vocal` 指向的音频不存在（只在 `--check-files` 时查） |
| S030 | error | 配乐文件不存在（只在 `--check-files` 时查） |

---

## 二、素材索引 `materials.json`

`cutwright index <素材目录> -o materials.json` 自动生成，也可以手写。

```json
{
  "source": "examples/story/materials",
  "pieces": [
    {"file": "materials/S01_subway_night_wide.mp4", "start": 0.0, "dur": 4.0,
     "tags": ["s01", "subway", "night", "wide"], "who": []}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `file` | 绝对路径，或相对索引文件所在目录 |
| `start` | 在这段素材里的入点（秒） |
| `dur` | 可用时长（秒）；短于需要就被慢放，慢过 0.26 倍就弃用 |
| `tags` | 挑素材的依据，见下 |
| `who` | 这段画面里有谁（`["lin"]`），对台词命中加分 |

`index` 的标签来自**文件名**：`S01_subway_night_wide_take2.mp4` → `s01 subway night wide`（纯数字与 `takeN` 被丢掉）。文件名里写清楚「哪场 + 场景 + 景别 + 谁」就够了，不用额外维护标签文件。

---

## 三、排片有三条硬规则

1. **帧网格**：每个镜头的帧数由最大余数法分配，求和精确等于这句的总帧数，绝不出现 0 帧镜头，也不会因为四舍五入漂移出空档/重叠。
2. **相邻不重样**：相邻两镜不许是同一段素材的同一入点。分数调不开的时候靠硬规则兜底（取第二好的），而不是指望权重刚好压住。
3. **时长优先于字数**：台词时长按 `显式 duration` > `旁白音频真实时长` > `字数 ÷ cps + pause` 的顺序取，估算只是最后手段。

挑素材的打分（照抄自 `story._pick_piece`，改之前先看注释）：

```
3.0 × 标签交集
+ 4.0（who 命中）/ -3.0（who 不命中）
- 3.0（整场标签一个都没对上）
- 1.0 × 这段已被用过几次
- 0.35 × |片段时长 - 需要时长|
- 1.2（要慢放）
- 12.0（与上一镜同源，等于禁用）
```

---

## 四、`story` 的输出报告

`cutwright story` 会打印，`--json` 时整块的 `report`：

| 字段 | 含义 |
| --- | --- |
| `ok` | 是否排出了东西（素材全缺时为 false） |
| `scenes` / `lines` / `shots` | 场次数 / 台词数 / 镜头数 |
| `duration` | 成片总时长 |
| `per_scene[]` | 每场 `{id, slug, shots, start, dur}` |
| `warnings[]` | 「这句找不到可用素材」「旁白文件不存在」之类 |
| `unresolved[]` | 完全排不上的台词（分镜表里就没有它的镜头） |

**报告里有 `unresolved` 就别急着 build**：那意味着某句台词没有画面，成片会是一段空白时间。
