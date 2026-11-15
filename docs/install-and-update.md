# 装给别人用 / 自己怎么迭代

这份文档解决两件事：**把 cutwright 交给一个朋友**，以及**你自己改完之后怎么让各方都吃到更新**。

---

## 一、先看你朋友要哪一档

| 想要的效果 | 需要做什么 | 需要装什么 |
|---|---|---|
| 只会敲命令出片 | 装 CLI（第一节） | Python 3.9+、ffmpeg |
| 让 AI（Claude Code / Cursor / Codex / Gemini / DSH）会用 | CLI + 装技能（第二节） | 同上 |
| 在 DSH 里用「剪匠」这个 agent 预设 | CLI + 技能 + 预设（第三节） | 同上 + DeepSeek Harness |
| 剪映草稿那条线 | 以上 + Windows + 剪映 11.x | 剪映专业版 |
| 达芬奇那条线 | 以上 + Resolve **Studio** | 外部脚本开关打开 |

macOS / Linux 只能跑 `ffmpeg` 后端；`jianying` 后端依赖 Windows 上的剪映 DLL，
`resolve` 后端在两个平台都行但要 Studio。

---

## 二、装 CLI

```bash
git clone https://github.com/CodeVoyagerht/cutwright.git
cd cutwright
python -m pip install -e .          # 核心零第三方依赖
cutwright doctor                    # 先看环境，缺什么会直说
```

ffmpeg 单独装：`winget install Gyan.FFmpeg`（Windows）/ `brew install ffmpeg`（macOS）/
`sudo apt install ffmpeg`（Linux）。装在奇怪的地方就设 `CUTWRIGHT_FFMPEG` 指向 exe。

想烧字幕、出花字，再装 `python -m pip install -e ".[render]"`（Pillow）。

---

## 三、装技能（让 AI 会用）

技能是**一份真源、多处落地**：仓库里的 `skills/cutwright/` 永远是唯一真源，
装到各家只是复制（或软链）一份进去。

```bash
python skills/cutwright/scripts/install.py --dry-run     # 先看会写哪几个目录
python skills/cutwright/scripts/install.py --target all  # 一间间装
```

支持的 `--target`：

| 键 | 装到 | 谁读 |
|---|---|---|
| `dsh` | `~/.dsh/skills` | DeepSeek Harness（本仓的「剪匠」预设靠它） |
| `agents` | `~/.agents/skills` | Cursor / Codex / Gemini 的原生位置 |
| `claude` | `~/.claude/skills` | Claude Code |
| `cursor` | `~/.cursor/skills` | Cursor |
| `codex` | `~/.codex/skills` | Codex |
| `gemini` | `~/.gemini/skills` | Gemini CLI |

装到别处（比如团队共享盘）：`--target dir --dir "D:\team\skills"`。

装完**新开一个会话**，说一句「用 cutwright 把 E:\素材 剪成 30 秒竖屏」就该触发。
技能没出现，先查两件事：目录名是不是 `cutwright`（= SKILL.md 里的 `name`），以及
客户端是不是需要重启才能重扫技能目录。

---

## 四、装 DSH 预设「剪匠」

```bash
# Windows
xcopy /E /I /Y preset "%USERPROFILE%\.dsh\.agent-presets\cutwright"

# macOS / Linux
cp -r preset ~/.dsh/.agent-presets/cutwright
```

然后**重启 DSH**，新建会话时在预设选择器里选「剪匠」。

两个硬要求：

- 目录名必须是 `cutwright`（= 预设 id），改了名字选择器认不出；
- 先装完技能再开预设，否则 persona 里那句「用 `skill` 加载 cutwright」指向空气。

---

## 五、给朋友的一句话（可以直接复制）

> ```bash
> git clone https://github.com/CodeVoyagerht/cutwright.git && cd cutwright
> python -m pip install -e .
> python skills/cutwright/scripts/install.py --target all
> cutwright doctor
> ```
> 然后新开会话说「用 cutwright 剪一条」就行。缺 ffmpeg 的话 `doctor` 会告诉你怎么装。

---

## 六、更新：拉完代码还要重新同步

这是最容易漏的一步：**技能装过去的是复制品，不会跟着 `git pull` 自动变**。

```bash
cd cutwright
git pull
python -m pip install -e .                              # 只有改了依赖/入口才需要
python skills/cutwright/scripts/install.py --target all --force   # 覆盖旧的那几份
```

`--force` 是必须的：不加它，安装器看到目标已存在就跳过（这是为了不误删你手改过的副本）。

`--force` 也不是「见到同名就删」：它先看目标目录里有没有 `SKILL.md`、里面的 `name`
是不是 `cutwright`。对不上就拒绝——这样万一你把 `--dir` 指到了自己的代码目录、
里面刚好有个同名项目，也不会被一条命令删掉。真要把那种目录清掉，加 `--force-any`
（名字已经说明它危险，先自己确认一遍）。

预设更新同样要重新复制一遍，然后重启 DSH——**预设只在启动时读一次**：

```bash
xcopy /E /I /Y preset "%USERPROFILE%\.dsh\.agent-presets\cutwright"
```

### 不想每次都 `--force`？用软链

```bash
python skills/cutwright/scripts/install.py --target dsh --link
```

软链后 `git pull` 即生效，省掉同步。代价：Windows 上要管理员或开发者模式
（没有权限时安装器会打印原因并**自动退回复制**，不会半途失败）。

---

## 七、你迭代自己的流程

日常循环：

```bash
# 1. 改 src/ 或 skills/
python -m unittest discover -s tests -t .     # 158 个用例，零依赖
python tools/lint_skill.py                    # SKILL.md 的格式合规（CI 也跑这条）

# 2. 动过 schema 就必须同步字段文档
#    skills/cutwright/references/shotlist-schema.md

# 3. 发版：四处版本号一起改
#    pyproject.toml  /  src/cutwright/__init__.py __version__
#    skills/cutwright/SKILL.md  metadata.version  /  CHANGELOG.md

# 4. 提交、打 tag、推
git commit -am "feat: ...——为什么"
git tag v0.2.0 && git push --follow-tags
```

CI（`.github/workflows/ci.yml`）会在 push 后跑三件事：lint、四组 os×py 的单元测试、
以及**真渲一条**（装 ffmpeg → 合成素材 → validate → build → qc → 传 artifact）。
冒烟那条在 2 核 runner 上约 10 分钟，改渲染参数时看它的结果比看本地快。

想在本机边改边验证技能而不用反复 install：

```bash
python skills/cutwright/scripts/install.py --target dsh --link   # 改源码即刻生效
# 或者干脆不装，直接跑源码：
$env:PYTHONPATH="src"; python -m cutwright doctor
```

---

## 八、排错清单

| 现象 | 原因与处理 |
|---|---|
| 技能目录里没有 cutwright | 装错位置了。`--dry-run` 看它到底往哪写；目录名必须等于 `SKILL.md` 的 `name` |
| AI 不提这个技能 | 客户端没重扫。重启客户端；有的产品还要在设置里打开 skills |
| `ffmpeg 未找到` | `cutwright doctor` 会列出找过的路径；设 `CUTWRIGHT_FFMPEG` 或装到 PATH |
| 剪映草稿打不开 | 这个版本的 DLL 加密自检没过（11.7 已知如此）。11.6 的 `videoeditor.dll` 可用，`doctor` 会说明原因 |
| 剪映草稿同名重建报「内容已损坏」 | 索引里留了悬空条目。`build ... --opt update_index=false` 或者删掉旧草稿目录 |
| 达芬奇报 `import DaVinciResolveScript` 失败 | 外部脚本只在 **Studio** 版：Preferences → General → External scripting 设为 Local；改完要重启达芬奇 |
| 达芬奇调色没生效 | LUT 必须先 `RefreshLUTList()`；后端已自动调用，若版本不支持会整段跳过并在报告里写 `lut.method` |

改坏了不用怕：删掉装歪的那份重装即可，真源永远在仓库里。
