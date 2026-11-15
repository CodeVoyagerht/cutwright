# 剪匠 agent preset

一个 DeepSeek Harness 的 **agent 预设**：把标准模式的能力面，换上剪辑工作的行为约束。

```
preset/
├── preset.yml        # 名字与描述（显示在预设选择器里）
└── agent.cordis.yml  # 组合文件：挂哪些插件、persona 写什么
```

## 安装

DSH 的用户预设目录是 `~/.dsh/.agent-presets/`。把本目录整个复制进去：

```bash
# Windows
xcopy /E /I preset "%USERPROFILE%\.dsh\.agent-presets\cutwright"
# macOS / Linux
cp -r preset ~/.dsh/.agent-presets/cutwright
```

然后**重启 DSH**，新建会话时在预设选择器里选「剪匠」。

⚠️ 目录名必须是 `cutwright`（= 预设 id），改名会导致选择器里认不出。

## 先装技能，再开预设

persona 里让 agent「用 `skill` 工具加载 cutwright」——那是指仓库里
`skills/cutwright/` 这份技能。装好它，预设才有领域知识：

```bash
python skills/cutwright/scripts/install.py --target dsh
```

（`--target all` 会同时装到 Claude Code / Cursor / Codex / Gemini 的目录。）

## 与 standard 预设的差别

**只有 persona。** 其余行（shell / 文件 / 搜索 / 计划 / 压缩 / 委派 / 提问 /
待办 / 联网 / 交付）逐行照抄自带 `standard` 预设的定义。

这是刻意的：能力面跟着官方走，升级 DSH 不会因为漏挂一行工具而瘸腿；
这里只改「这个人是谁、守什么规矩」。

## 写自己的 persona 时的三个坑

1. **只用 `{{model}}` / `{{provider}}`**。这两个由 agent-loop 注册。写 `{{cwd}}`
   这类不存在的变量，整份 preset 渲染失败，新会话直接起不来（报
   `unknown prompt variable ...`）。
2. **域知识不要写进 persona**。persona 常驻上下文、每轮都付费；分镜表字段、后端
   选项、报错处置应该待在技能里，按需加载。这份 persona 只有 40 行左右，就是为了
   守住这条。
3. **`!!js` 行照抄别自创**。`disabled: !!js process.platform === 'win32'` 这类
   表达式在加载时求值；写成别的形式会静默变成字符串 `"false"`，插件照挂。

## 加/减能力

改 `agent.cordis.yml` 即可。常用的几行：

| 想要 | 加哪一行 |
|---|---|
| 定时出片（每天自动剪一条） | `@deepseek-ai/dsh-tool-schedule` |
| 读 Office 表格当分镜表 | `@deepseek-ai/dsh-tool-office` |
| 桌面自动化（操作剪映 UI） | 见 `dsh-doc` 技能里 desktop 的挂法 |

改完重启 DSH。改坏了就删掉 `~/.dsh/.agent-presets/cutwright/` 重装，别在那儿修。
