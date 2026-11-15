# 参与贡献

先说清楚这个项目**想要什么**、**不想要什么**——省得双方白花时间。

## 想要的

- **真实片子暴露的问题**：你拿它剪了一条片，哪儿不对。带上分镜表片段
  和 `cutwright qc` 的输出，比任何描述都有用。
- **新后端**：Premiere / FCP / 剪映国际版……只要它有一个能写文件的格式或者
  一套能调的 API。后端接口只有 `available()` + `build()` 两个方法。
- **校验码**：手写分镜表时你踩过的坑，转成一个 `E0xx` / `W0xx`。
- **文档里的错**：尤其字段名、默认值、报错原文对不上的地方。

## 不想要的

- **加依赖**。核心零第三方依赖（`pillow` 只有渲染字幕时才需要），
  这条不想破。
- **「智能剪辑」**：自动选镜头、自动调色风格、AI 认场景。分镜表已经表达了
  创作者的意图，这个项目负责**忠实执行**。
- **自动导出成片**：剪映的 UI 自动化在这个生态里没做稳过，别让它变成
  一个常年坏的按钮（详见 `docs/design-notes.md` 第 11 条）。
- **只改版本号 / 改错别字之外的大重构**：先开 issue 说清楚为什么。

## 开发环境

```bash
git clone <repo> && cd cutwright
python -m pip install -e ".[render,dev]"

# 不装也行：测试只依赖标准库
python -m unittest discover -s tests -t . -v
```

没有 pytest，故意用 stdlib `unittest`——工具链越少，别人越可能真跑一下。

## 提 PR 之前

四件事，缺一件我会问你要：

1. **测试**。新行为要有用例；修 bug 要有能复现那条 bug 的用例。
   `tests/__init__.py` 提供 `tmpdir()` 上下文管理器——**别用
   `tempfile.TemporaryDirectory`**，Windows 沙箱里它在清理阶段会抛
   `PermissionError`。
2. **`python tools/lint_skill.py` 通过**。改了 `skills/` 就一定要跑。
3. **文档同步**。改 `schema.py` 的字段或校验码，必须同步
   `skills/cutwright/references/shotlist-schema.md`——那是校验码的人类字典，
   不同步等于让文档说谎。
4. **一句话说明为什么**。改了什么代码 diff 里看得出来，为什么改看不出来。
   写进 commit message 或 PR 描述。

## 风格

- 中文注释与文档为主，代码标识符用英文。
- 注释解释**为什么**，不解释**是什么**。`# 加一` 这种没有价值；
  `# 帧号按四舍五入，因为剪映和 Resolve 都按帧取整，各自取整会差 1 帧` 才有。
- 常量带出处：实测的标 `实测 <版本>`，查文档得的标 `见 <链接>`。
- 不要 `except Exception: pass`。至少要 `_err(...)` 说一句。

## 提交与分支

- 分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
- commit message：`<范围>: <做了什么>——<为什么>`，例如
  `ffmpeg: 推镜改用 2x 超采样——直接 zoompan 会糊`。
- 一个 PR 一件事。顺手改的东西单独提。

## 报告问题

开 issue 时请带上：

```
cutwright doctor --json          的完整输出
cutwright validate <表> --json    的相关片段（去掉路径里的用户名）
cutwright qc <表> --mp4 <片>       如果是成片问题
操作系统 / Python 版本 / ffmpeg -version
```

**不要**贴带个人路径或账号信息的完整剪映草稿目录。
