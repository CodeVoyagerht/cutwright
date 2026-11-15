# 设计笔记：抄了什么，扔了什么

这个项目是从两堆已有的东西里长出来的：一堆是**我自己跑通过的剪辑脚本**
（一份剪映 11.6 草稿生成器、一套 ffmpeg 渲染管线、一个达芬奇插件），
另一堆是**别人的 Agent / Skill 工程实践**。前者决定它能干什么，后者决定它
怎么被使用。

下面每条都标了出处和「为什么不照抄」。没有出处的属于本项目的判断，标 `—`。

## 一、从 Agent Skills 规范抄的

**1. frontmatter 只用 6 个字段。** `name` / `description` / `license` /
`compatibility` / `metadata` / `allowed-tools`。
出处：anthropics/skills 的 spec 与 Claude Code 打包校验——带 `argument-hint`
之类扩展字段的技能，在打包到 claude.ai 时会**硬报错**：
`Unexpected key(s) in SKILL.md frontmatter`。一个技能要能被别处安装，
就得待在跨端的交集里。扩展需求全塞 `metadata`（string→string 映射）。

**2. 三层披露：元数据常驻、正文按需、资源懒加载。**
出处：同一份规范。这里的具体做法是——`SKILL.md` 正文 ≈170 行，
分镜表字段全表放 `references/shotlist-schema.md`，报错处置放
`references/troubleshooting.md`，只有真遇到时才读。**persona 里一个字都不写域知识**，
因为 persona 每轮都进上下文，而技能正文只在激活时进一次。

**3. 把确定性的事写成脚本，别让模型算。**
出处：anthropics/skills 的 `pdf` 技能（校验脚本化，模型只做编排）、
`skill-creator` 的 `package_skill.py`。
这里落地成 `scripts/verify.py`：依次跑 `validate --check-files` → `plan` →
`qc`，**任一层失败即停**，末行打 `PASS` / `FAIL`。判完成靠退出码，不靠模型自评。
同理，帧号换算、音量增益、`tl_dur = src_dur / speed` 全在 Python 里算，
不写进提示词让模型推。

**4. description 写「做什么 + 何时用 + 何时别用」。**
出处：规范里 description 是唯一的**路由键**——模型只靠它决定要不要加载这个技能。
本项目写成：
> 把分镜表（shotlist.json）变成成片…**Use when** 用户要按镜头表批量出片、批量加字幕/旁白、把素材剪成竖屏短视频…**Do not use for** 需要逐帧精修或调色的项目（那该在达芬奇/PR里做）、只剪一刀的简单裁剪。

**5. 正文控制在 500 行以内，引用只隔一层。**
出处：规范硬建议 `<500 lines`、引用不得深链。深链会让「按需加载」退化成
链式加载，token 反而变多。本项目只有 `SKILL.md → references/*.md` 一层。

## 二、从优秀 agent 工程里抄的

**6. 把「必须先检查」做成硬闸门（hard gate）。**
出处：obra/superpowers 的启动契约式技能——技能里写死检查点，而不是靠提示词
「请务必仔细」。
这里落成技能里的**五条铁律**，每条都是可判定的：
- 先 `cutwright doctor`（不知道哪个后端可用就 build ＝ 盲开）
- `validate` 有 error 不许 build
- 不许说「应该没问题」（要么给 `qc` 的数字，要么承认没验证）
- 素材缺失要明说是哪几个文件（渲染会成功，片子是废的）
- 交付必须带证据（路径 + 时长 + 分辨率 + 体检结论）

**7. 域知识与人格分离。**
出处：wshobson/agents 的分层——agent 说「谁来做」，skill 说「怎么做」。
这里 agent 预设只放行为约束（≈40 行 persona），剪辑规则、字段表、报错字典
全在技能与 `references/` 里。好处是换模型、换人格都不动知识。

**8. 先 plan-validate 再 execute。**
出处：规范里的 `plan → validate → execute` 模式。
这里 `cutwright plan` 只做只读统计（每镜起止帧、总时长、空档、样式引用），
不写一个字节。**排布交给数据，审美交给人**——这是本项目的核心分工。

## 三、从剪映/达芬奇生态抄的

**9. 用 dataclass 建模分镜表，而不是裸 dict。**
出处：pyJianYingDraft（4.9k★，Apache-2.0）用 dataclass 建模草稿片段，
比手搓 dict 少一半的拼写错误。这里升级成「**宽松解析**」：字符串 `"0.5"`
也当数字吃，坏值回落默认值——手写分镜表不该因为一个字段炸掉整条片。
但**校验器严格**：宽松解析吞掉的东西，`validate` 会用 E/W 码明确指出来。

**10. 加密草稿 + 双写 + 索引备份。**
出处：pyJianYingDraft 的 aoguai fork（加密并自动注册草稿）。
这里额外确认了三条本机实测的事实，都写进了代码注释：
- 剪映 **6.0 起加密** `draft_content.json`，11.6 还有
  `Timelines/<UUID>/` 下的多份加密镜像，**只写根文件是白改**；
- 加密要走 `videoeditor.dll` 的 `EncryptUtils::encrypt/decrypt`（MSVC 修饰符号，
  ctypes 调）；
- `root_meta_info.json` 里留**悬空条目**会让同名重建报「草稿内容已损坏」，
  而「不碰索引」只是 warning → 所以给了 `update_index` 开关，默认照原实现
  「改索引 + 备份 `.bak-<时间戳>`」，关掉就完全不碰。

**11. 不实现「自动导出成片」。**
出处：pyJianYingDraft、jianying-tool（PyPI 0.5.0）都**没有**这个能力。
剪映 11.x 是 Qt/QML，UIA 树里一个可点控件都不暴露，只能截图模板匹配。
这是**能力边界，不是待办**：草稿就位、导出点一下，比一个三天两头失效的
自动点击器诚实。

**12. 调色走 NodeGraph，不用已移除的 API。**
出处：达芬奇 19.0.0 的 API 变更。`TimelineItem.SetLUT/GetLUT` 在 19.0.0
**被移除**（我上一版插件用的就是它，在 20.3 上靠 16.2 起的
`GetNodeGraph().SetLUT(nodeIndex, path)` 才对）。另外两个坑：
LUT 必须先 `Project.RefreshLUTList()` 否则静默失败；Windows 的 LUT 目录是
`C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT\`。

**13. 切点用 ffmpeg 算，不用 `DetectSceneCuts()`。**
出处：Resolve API 里确实有这个函数，但它**直接在时间线打切点**（破坏性副作用）
且异步返回。分镜表已经有切点了，我们要的是「素材里第几秒切」，ffmpeg 的
`select='gt(scene,0.3)'` 更快也更安全。

## 四、从自己的旧脚本抄的（这部分是「已验证」而不是「看起来对」）

**14. zscale 的 HDR 处理顺序。**
`zscale=t=linear:npl=100 → format=gbrpf32le → tonemap=hable → zscale=t=bt709…`
顺序错一步，高光就灰掉。这条是从成片里调出来的，不是查文档得的。

**15. 音量自动补增益用 `volumedetect` 反推。**
逐镜 `-af volumedetect` 量 `mean_volume`，然后
`gain = clamp(-20.0 - mean, -10.0, 24.0)`，再加 30ms 淡入淡出与
`alimiter=limit=0.95`。为什么不用 loudnorm：两遍处理太慢，逐镜 loudnorm 在
切点处会「呼吸」。这套反推在 20 条成片上够用。

**16. `zoompan` 放大到 2 倍再缩回。**
竖屏推镜直接把 1080 宽的素材 zoompan 会糊；先 `scale` 到 2160x3840 再
zoompan 到 1080x1920，等效 2x 超采样。代价是渲染慢 30%，值得。

**17. 剪映的 `PHOTO_DURATION_SENTINEL = 10800000000`。**
照片素材的时长哨兵值，写错会让照片在时间线上变成 0 帧。这类常量只能实测。

## 五、明确扔掉的

| 扔掉的 | 为什么 |
|---|---|
| 让模型自己去「数帧 / 算时长」 | 采样误差不可复现；一条 86.5s 的片差 0.3s，观众看得出来，你也复现不出来 |
| 一个技能塞多个工作流 | 激活精度下降，常驻正文膨胀；这个技能只干「分镜表 → 成片」 |
| 假设「你装过 ffmpeg / Pillow」 | 换机器即失败，错误信息还指向技能外部；所以 `doctor` 先说清楚缺什么 |
| 明文草稿（只写 `draft_content.json`） | 剪映 6.0+ 读不了，等于没装；老版本才吃得下 |
| 固定字体绝对路径 | 跨机失效；现在找不到就省略并记 warning，不炸 |
| 用口号代替步骤（「务必专业地剪辑」） | 不可执行；要么给数字（镜长 2.0–3.0s），要么给命令 |
| `argument-hint` 这类扩展字段 | 打包到 claude.ai 直接报错 |
| 自研一套「智能剪辑算法」 | 分镜表已经表达了创作者的意图；我们要做的是**忠实执行**，不是猜 |

## 六、当前已知的边界

- **不会自动导出**：三条产线都只做到「素材就位」。剪映要点一下导出，
  达芬奇要按一下渲染。
- **剪映后端绑定版本**：加密接口是逆出来的，剪映升级可能失效。`doctor`
  会报版本与 dll 大小，便于判断。
- **Resolve 只测过 Windows + Studio 20.3**：macOS 路径已写在 config 里，
  但没有实机验证；免费版没有外部脚本开关。
- **音乐分析是启发式**：`analyze_music` 出 BPM / RMS / 低频占比，用来建议切点，
  不做自动卡点。真正的卡点还是要人听。
- **没有 ML**：不认人脸、不认场景、不写字幕文本识别。这些交给别的工具，
  本项目只吃结构化输入。
