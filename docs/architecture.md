# 架构

三句话：**分镜表是唯一输入**，**Timeline 是中间表示**，**后端只负责落地**。

```
 shotlist.json          ← 人写的（或 agent 写的）
      │  schema.load_shotlist()  宽松解析 + validate()
      ▼
  Shotlist ──timeline.from_shotlist()──▶  Timeline (IR)
      │                                      │
      │  planner.*（补齐缺口 / 对齐时长 / 重排）│  qc.*（黑场 / 空档 / 时长漂移）
      │                                      ▼
      │                         backend.build(timeline, out_dir)
      │                                      │
      │                    ┌─────────────────┼─────────────────┐
      │                    ▼                 ▼                 ▼
      │              ffmpeg 直出        jianying 草稿      resolve 时间线
      │              （MP4）            （draft_content）   （Timeline 对象）
```

## 为什么要有 Timeline 这一层

最省事的写法是「分镜表 → 直接调后端」。三个后端就是三份重复的解析代码，
而且每加一个后端就要重新处理一次 `speed` 换算、`tl_start` 缺省推导、
素材路径解析这些细节——这三件事恰好是最容易出 silent bug 的地方
（剪映里 `tl_dur = src_dur / speed`，写错了不会报错，只会让后面每一条
字幕都错位）。

`Timeline` 把这些**算一次**：

| 字段 | 谁算的 | 为什么在这里算 |
|---|---|---|
| `tl_start` | 缺省时按前一条累加 | 三条产线都要同一个「上一条的结束」 |
| `frames` | `round(t * fps)` | 剪映和 Resolve 都以帧为单位，向后端各自取整会差 1 帧 |
| 素材绝对路径 | `Shotlist.resolve_material()` | 相对 `material_dir`，跨机可携带 |
| 音频总时长 | 视频 + 最长音频尾部 | 剪映草稿要一个总长，改一次全改 |

`Timeline.summary()` 返回人可读的一行统计（镜头数/时长/画布/帧数），
`plan` 命令直接打它——**先看统计再渲染**，这是便宜得多的检查。

## 后端契约

`backends/base.py` 里只有三个东西：

```python
class Backend:
    name: str
    def available(self) -> bool: ...           # 环境够不够
    def why_unavailable(self) -> str: ...      # 不够的话，缺什么（要可操作）
    def build(self, timeline, out_dir) -> BuildResult: ...

@dataclass
class BuildResult:
    backend: str
    ok: bool
    artifacts: list[str]        # 产出的文件路径（MP4 或草稿目录）
    data: dict                  # 后端特有信息，原样进 --json
    error: str
    warnings: list[str]
```

注册是**延迟**的：`load_backends()` 逐个 `importlib.import_module`，
任一后端 import 失败只记进 `IMPORT_ERRORS` 不抛。理由很实际——用户可能
只装了 ffmpeg 一条线，甚至源码被裁剪过；让「装不上的后端」把整个 CLI 弄挂，
是最没必要的一种脆弱。

`available()` 与 `why_unavailable()` 之所以拆成两个方法，是为了让 `doctor` 能说
「剪映装了，但草稿目录没找到」而不是干巴巴一个 ✗。

## 三条产线的差别

| | ffmpeg | jianying | resolve |
|---|---|---|---|
| 产出 | 一个 MP4 | 一个草稿目录 | 一条时间线（在跑着的 Resolve 里） |
| 依赖 | ffmpeg/ffprobe | 已安装的剪映（要 videoeditor.dll 加密） | Resolve Studio + 外部脚本开关 |
| 可复现 | 完全（同命令同结果） | 需要剪映自己的渲染 | 需要 Resolve 自己的渲染 |
| 失败模式 | 滤镜串拼错、字体会掉 | 加密失败、草稿索引悬空 | 版本 API 漂移 |

**ffmpeg 是主产线**：没有 GUI 依赖，CI 里能跑，同一条分镜表渲多少次都一样。
另外两条是「落到用户真正干活的工具里」——不是替代，是分发。

## 错误分三层

1. `validate()` —— 结构错（缺文件、时长≤0、样式未定义）。**便宜，先跑。**
2. `build()` —— 环境错（ffmpeg 找不到、剪映在跑）。**快失败，别渲到一半。**
3. `qc()` —— 成片错（黑场、空档、时长漂移）。**渲完立刻跑，别靠眼看。**

分三层的理由：越早发现越便宜。一条 3 分钟 4K 片渲染 20 分钟，因为
`src_dur` 写成负数而在第 19 分钟失败，是这个工具最难忍受的体验。

## 目录

```
src/cutwright/
├── schema.py      分镜表数据模型 + 校验（650 行，改动要极其克制）
├── config.py      路径发现（ffmpeg / 剪映 / Resolve），环境变量可覆盖
├── media.py       ffprobe 探测、抽帧、取平均音量、扫切点（带缓存）
├── timeline.py    中间表示（IR）
├── planner.py     补齐与对齐：fit_duration / fill_gaps / reorder
├── qc.py          成片体检 + 渲染后体检
├── cli.py         命令入口（validate / doctor / plan / build / qc / install）
└── backends/
    ├── base.py    Backend / BuildResult / 延迟注册表
    ├── ffmpeg.py  主产线
    ├── jianying/  草稿生成 + 加密 + 安装
    └── resolve/   桥接 + 时间线构建
```

`schema.py` 是**唯一权威**：技能里的 `references/shotlist-schema.md` 是它的人类说明，
`validate()` 的校验码是它的错误字典。改 schema 要同步那两份，否则文档在骗人。
