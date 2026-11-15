# 短剧产线：剧本 → 分镜表 → 成片

适用于**有剧本**的内容：短剧、对话戏、解说、Vlog 有台本的那种。没有剧本、只有一堆素材时，走 `shotlist-schema.md` 那条（手排分镜表）。

---

## 一、四步

```bash
# 1. 素材入库：扫目录，按文件名打标签，量出每段可用时长
cutwright index materials -o materials.json

# 2. 排片：剧本 + 索引 → 分镜表（人不碰镜头，只写戏）
cutwright story screenplay.json --index materials.json -o shotlist.json

# 3. 查：结构错、素材缺失、报告里的 unresolved
cutwright validate shotlist.json --check-files

# 4. 出：成片 + 自动体检
cutwright build shotlist.json -o out
```

先 `index` 再 `story` 是硬顺序——`story` 不扫文件系统，它只认索引里的时长，所以索引过期（换了素材没重扫）会排出错误时长。

到剪映草稿就把第 4 步换掉：

```bash
cutwright build shotlist.json -b jianying --opt install=true
```

---

## 二、剧本怎么写才排得好看

**场次的 `tags` 是唯一的质量杠杆。** 你写 `["subway","night"]`，机器就只在这一场里找带这两个标签的素材；写漏了，它就只能满素材库乱挑。

- 场次标签写**地点 + 时间 + 气氛**三样（`subway` / `night` / `tense`），正好对上文件名里的词。
- 台词级 `tags` 用来区分景别：`{"who":"lin","text":"他欠我一条命","tags":["close"]}`。
- `who` 要写，且 `cast` 里要有对应条目：机器靠它优先选「这个人在画面里」的素材。旁白（`voiceover`）也写 `who`，表示这句话是谁的内心。
- 一场戏的台词别少于 2 句：一句的场次没有镜头变化，看着像卡住了（会出 `S012` 警告）。

**台词时长**默认按 4.6 字/秒（对话）/ 4.2 字/秒（旁白）估，加 0.12s 停顿。要精确就两种办法：写 `duration`，或者给旁白配 `vocal` 音频（用音频真实时长，最准）。

**镜头长度**由 `--min-shot` / `--max-shot` 控制（默认 1.0 / 3.6 秒）：

```bash
cutwright story screenplay.json --index materials.json --min-shot 0.8 --max-shot 2.5
```

一句 6 秒的台词会被切成 3 镜（`--max-shot 2.5`），切法带节奏权重（0.82 / 1.18 / 0.94 / 1.06），不是均分。

---

## 三、什么时候该用、什么时候别用

| 用 | 别用 |
| --- | --- |
| 有剧本/台词的对话戏、短剧、解说 | 纯音乐卡点、无台词的旅拍（手排分镜表更自由） |
| 素材文件名能表达「哪场/场景/景别/谁」 | 文件名是 `VID_20261115_143022.mp4` 这种（先改名，或者手写索引） |
| 一次要出很多集，同一套规则反复用 | 只有一条片子、镜头要逐个精调 |
| 想让人只写戏、不写镜头 | 需要精确到帧的创意剪辑（这支工具不做关键帧动画） |

---

## 四、素材怎么命名（这是最省事的一步）

`index` 从文件名里抽标签，规则是：按 `_`、`-`、空格切开，丢掉纯数字与 `takeN`，全小写。

| 文件名 | 抽到的标签 |
| --- | --- |
| `S01_subway_night_wide_take2.mp4` | `s01 subway night wide` |
| `S02-train-close-lin.mov` | `s02 train close lin` |
| `12_subway_007.mp4` | `subway` |

所以约定一条：**`场次_场景_时间_景别_谁`**。写了谁（`lin`），就能和 `cast` 的代号对上，台词会自动优先选这个人的镜头。

---

## 五、验收清单

跑完 `story`，按顺序看这四个东西，都有一个明确的输出：

1. `story` 打印的场次表：每场的 `镜头数` 是否 ≥ 台词数的一半（一镜一句是理想，一镜 3 句就太闷）。
2. 报告里的 `unresolved`：必须是空。非空说明某句没画面。
3. `validate --check-files`：`0 个错误`；警告逐条读，`S023`（人物不在 cast）通常是笔误。
4. `build` 的成片体检：`duration_match` 的差值应在 ±0.25s 内，`no_long_black` / `no_long_silence` 必须 OK。

再抽 3 帧看字幕位置（`examples/story/out/frames/` 那种），**单测抓不到「字幕被切掉一半」**。

---

## 六、坑

**`story` 的输出路径是绝对的。** 因为读索引的进程不一定在同一个工作目录——但这也意味着 `shotlist.json` 换机器就不能用了，要重新 `index` + `story`。

**索引过期比没有索引更坑。** 换了素材忘了 `index`，排出来的时长全错，而 `validate` 只会说「素材不存在」或者干脆不报。

**`--max-shot` 太小会碎成一堆 0.5 秒的镜头。** 低于 `--min-shot` 时分配器会自己少拆几刀，但台词很短（1 秒以内）时没有可拆的空间，就只能一镜到底。

**旁白默认不出字。** 想让旁白也显示成字幕，加 `--vo-captions`；但注意旁白一旦出字，画面上会同时有「角色说的」和「旁白说的」两种字幕，容易串。

**中文文件名要加引号。** PowerShell 里 `--mp4 out\地铁追凶 第1集.mp4` 会被 argparse 拆成两个参数，必须写成 `--mp4 "out\地铁追凶 第1集.mp4"`。
