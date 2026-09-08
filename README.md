# TaskWizard

LLM 驱动的安卓手机操作 Agent：看一眼屏幕、想一步、动一下，带安全预警与自积累记忆。

[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](setup.py)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-lightgrey?logo=githubactions)](https://github.com/tom-cat-mao/TaskWizard/actions)

TaskWizard 采用 thin-loop v2：模型每轮观察真实设备、决定一个工具调用并执行一步；harness 只负责工具、安全边界、上下文卫生和可观测性，不替模型编排工作流。

## Demo

![TaskWizard 控制台：真实运行中的步骤时间线与接管终态](pages/assets/console-run.png)

## Features

- **Marks-first grounding**：执行动作绑定当前屏幕元素，过期、歧义或未命中的目标会 fail-closed。
- **高精度视觉定位**：`locate` 默认把原分辨率截图交给视觉定位器，优先利用“外观 + 可见文字 + 相对位置”提示；歧义时可用当前批次的容器或上下锚点 mark 做原图 scope 裁剪，结果仿射回映射为全屏 mark。
- **观测加固**：默认在每次原子观测前静置 300ms 并识别 `FLAG_SECURE` 均匀黑屏；执行工具可用 `settle_ms` 替代全局静置（搜索/提交/开页建议 1500–2500ms）。
- **安全预警制**：风险动作先返回警告与选项，模型明确确认后才执行（confirm-to-execute）。
- **可信完成**：TaskDoc 任务板与流程线持续记录进度；finish 两段式确认，并可交给独立上下文验收器复核。
- **HTML 产出物**：攻略、计划、比价报告等成果可由 `write_document` / `update_document` 写成本 run 的自包含单页 HTML；路径由 run id 派生，内容限 256 KiB，模型不能指定任意文件。
- **App-KB 自积累记忆**：同步本机应用名称；验证启动成功后沉淀非敏感别名，并累计成功反馈。同一 run 中未知中文名失败、回执列出的包名随后启动成功时，自动把该中文名写为 `learned` 别名（隐式纠正，证据闭环）。dream 还能从最小化工具事件识别“启动 A→1–2 步内明确自述开错并退出→成功启动 B”，删除错误 learned 映射并写入 B；用户可通过 CLI 写入最高信任的 `user` 别名或忘记 user/learned 别名。
- **类型化 App 名解析**：统一做 NFKC/大小写/空白归一化，再从 exact、lexical、pinyin、embedding 四路生成候选；候选携带 `match_type` / `authority`，默认按证据类型与三态代价决策，`rank_score` 只参与排序和分差。歧义时只返回排序后的 top-K，装机事实与 launch policy 仍独立 fail-closed。
- **经验数据面**：每次 run 结束以固定 schema 落盘 episode outcome 与工具结果分类（字符串原文照存，工具事件另含模型逐步自述的 intent/note；schema 之外的内容直接丢弃），持久化分角色 token 账本；数据采集全程 observe-only，并审计本轮实际注入的 lesson id。
- **经验提炼与晋升**：离线 `--distill` 以水位线批次蒸馏 episode，产出单条 rule 与多步过程卡 procedure 两类候选；第二次调用对照事实单统一自判分级 `auto_approved` / `needs_review`（拿不准一律 needs_review），人工 CLI 是纠正通道而非闸门。仅 `PHONE_AGENT_MEMORY_RAG=on` 时，approved / auto_approved lesson 才受控注入：规则在 run 开局以"参考、非规则"的 L0 Mirror 注入，过程卡按三个确定性时机投递（见下）。
- **RAG shadow 召回**：sqlite-vec + FTS5 混合检索历史 episode 与 App 别名；默认只写 trace 并按实际启动应用统计命中率，绝不注入 actor 上下文。
- **能力装配层 + 事件总线**：十个内建能力经稳定 `cap_id` 与 `apply/release` 生命周期挂载；所有策略行为都是事件总线上的监听器，LangChain 栈只剩桥接器（嵌套顺序 = 注册顺序，safety 恒居最内）。能力快照每次 run 写入 trace 与 episode。
- **插件系统**：外部插件以 CapabilitySpec 挂入同一装配层——pip 包声明 `taskwizard.capabilities` entry point，或 `plugin add` 本地目录；插件包可携带应用词表注入 App-KB。API 暂为 provisional v1。
- **长任务可控**：token 预算限制成本，两级 auto-compact 在接近上下文窗口时保留关键状态。

## 快速开始

需要 Python 3.10+；Android 7.0+ 设备需开启 USB 调试、能被 `adb devices` 识别，并安装 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk)。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env  # 填写模型网关、模型名与 API Key
```

`PHONE_AGENT_LOCATE_MAX_SIZE=0` 保持 `locate` 原图输入；低配机器可设为正整数限制最长边。`PHONE_AGENT_LOCATEANYTHING_CONTEXT_MAX_CHARS` 限制 intent/可见文字提示长度，`PHONE_AGENT_SCOPE_PADDING_RATIO` 控制可选 scope 裁剪的边缘扩展比例。
`PHONE_AGENT_MARKS_WINDOWED=auto|on|off`（默认 `auto`）控制窗口感知 marks（纯展示层）：`auto` 先试 `uiautomator dump --windows`，设备不支持则回退旧的单根 dump；`on` 强制 `--windows`（不支持则报错可见）；`off` 保持旧平铺渲染。模型看到的 marks 会按窗口分组、标注可操作性四档（`confirmed`/`likely`/`blocked`/`unknown`，启发式窗口不会给 `blocked`）和稀疏语义容器路径（`path=`，最多 3 层）；分组渲染带 `windowed/v1 source=<src>` 徽标、窗口头补 `active`/`focus` 裸标记，头部在解析器给出 `total_candidates` 时显示 `marks (K/total)`。这是纯展示升级——mark id 徽章、`resolve_mark`、工具执行、安全门、折叠与 `locate` 一寸不动，`op=blocked` 本包只展示不拦截。accessibility dump 若为瞬时失败（timeout/解析错/provider 错，以及 `on` 模式设备不支持）会作为观测不稳定重试一次；截图有效但 marks dump 仍失败时提交一帧带标注的零 mark 观测（OBS 头 `marks (0) [accessibility:<code>]`），dump 失败不再被当成“无控件”。`locate` 命中开新批次（epoch+1、旧 marks 失效、只把命中 mark 铸入新批），铸造 id 掺单调序号避免同批碰撞。
`PHONE_AGENT_OBSERVE_SETTLE_MS=300` 控制观测前静置（`0` 关闭），`PHONE_AGENT_BLACK_SCREEN_DETECT=on|off` 控制保护页黑屏检测；动作参数 `settle_ms` 会 clamp 到 0–5000ms，并替代而非叠加全局值。
`PHONE_AGENT_IMPLICIT_ALIAS=on|off` 控制 App-KB 的证据闭环隐式纠正（默认 `on`）；无失败回执候选证据时不会猜测或写入。
App 名解析默认 `PHONE_AGENT_RESOLVER_DECISION_MODE=typed`：lexical / pinyin / embedding 仍由 `PHONE_AGENT_RESOLVER_LEXICAL`、`PHONE_AGENT_RESOLVER_PINYIN`、`PHONE_AGENT_RESOLVER_EMBED` 控制，但候选会先标注 `match_type` 与 `authority`，再做三态决策。`exact_alias` / `exact_label` / `exact_package` / `exact_package_segment` / `registered_containment` 可自动 resolved；`fuzzy` / `pinyin_full` / `pinyin_initials` / `embedding` 只用于澄清，不会单独自动启动。`rank_score = RESOLVER_W_SIM*sim + RESOLVER_W_PRIOR*prior` 仅作排序、margin、回执和 trace 信号；`PHONE_AGENT_RESOLVER_TYPED_MARGIN=0.08` 控制强证据 top2 分差。包名分段由 `PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_MIN_LEN` 与 `PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_STOPWORDS` 控制，按 `.`/`_`/`-`/camelCase 切完整段，不做任意 substring。需要旧阈值行为时可设 `PHONE_AGENT_RESOLVER_DECISION_MODE=legacy`，此时 `PHONE_AGENT_RESOLVER_MIN_SCORE` / `PHONE_AGENT_RESOLVER_MARGIN` 恢复旧语义。授权边界不变，resolved 后仍须过装机事实和 launch policy。
`PHONE_AGENT_ALIAS_OVERWRITE=on|off` 控制 dream 的错误别名覆盖（默认 `on`）；`PHONE_AGENT_ALIAS_OVERWRITE_NOTES` 是逗号分隔的明确自述词表，默认 `开错,不对,不是,错了,wrong app`。事件只保存命中的词，不保存完整模型 note。
`PHONE_AGENT_DELIVERABLE=on|off` 控制 `deliverable` 能力（默认 `on`）；开启后模型可把文档成果写入 `PHONE_AGENT_DELIVERABLE_DIR/<run_id>.html`（默认 `outputs/deliverables`），只能创建或全量更新本 run 的 UTF-8 单页 HTML，大小上限 256 KiB。成功路径会进入 episode 的可选 `deliverable_path` 字段，生产 trace 只记录 HTML 字节数，不记录正文。

**模型提供方插件（models.json）**：默认零配置——所有角色都走 `.env` 里的 `PHONE_AGENT_MODEL` 网关，行为与旧版逐字段一致。需要多提供方/多模型时放一个 `models.json`（项目级 `.taskwizard.models.json` 或用户级 `~/.taskwizard/models.json`，`PHONE_AGENT_MODELS_FILE` 可显式指定，pi 风格两层合并）：声明 `providers`（`api: openai-completions | anthropic-messages | google-generative-ai`、`baseUrl`、`apiKey: "$ENV_VAR"`、每模型 `contextWindow/maxTokens/samplingParams/thinkingLevelMap`），之后角色环境变量（`PHONE_AGENT_MODEL`、`PHONE_AGENT_MEMORY_MODEL`、`PHONE_AGENT_VERIFIER_MODEL`、`PHONE_AGENT_SAFETY_REVIEWER_MODEL`；distill 沿用 memory 回落链）都可以写成 `provider:model` 把某个角色路由到第二模型，例如让 actor 留在本地网关、让 memory/verifier 走 `claude:claude-sonnet-4-5`。`PHONE_AGENT_THINKING=off|minimal|low|medium|high` 按模型声明的 `thinkingLevelMap` 翻译为各家思考配置，不支持的静默省略。`--list-models` 打印当前生效的提供方注册表。第三方插件也可以在装配期注册自己的 provider。未识别的 model 名落到默认 gateway 合成条目；未识别的 provider 快速失败并给出可见错误。

```bash
.venv/bin/python main_v2.py "打开设置进入 WLAN" --device-id <serial>
.venv/bin/python main_v2.py "在飞猪查询 10 月 2 日上海飞桃仙的最低价机票" --max-steps 40
.venv/bin/python -m phone_agent.web --device-id <serial> --port 8080   # Web 控制台
.venv/bin/python main_v2.py --dream    # 手动整理本地 App-KB 与经验库
.venv/bin/python main_v2.py --learn-alias "小红书=com.xingin.xhs"  # 写入最高信任 user 别名
.venv/bin/python main_v2.py --forget-alias "小红书"                # 删除该名称的 user/learned 别名
.venv/bin/python main_v2.py --rebuild-vec  # 从 episode/App-KB 全量重建语义索引
.venv/bin/python main_v2.py --list-models  # 打印生效的模型提供方注册表
.venv/bin/python main_v2.py --distill     # 离线蒸馏，自判分级落 auto_approved / needs_review
.venv/bin/python main_v2.py --review-lessons
.venv/bin/python main_v2.py --approve-lesson <lesson-id>
.venv/bin/python main_v2.py --revoke-lesson <lesson-id> "原因"
.venv/bin/python main_v2.py --supersede-lesson <lesson-id> "修订后的规则"
.venv/bin/pytest tests -q
```

RAG 默认 `PHONE_AGENT_MEMORY_RAG=shadow`。每次 run 结束会把通过
`PHONE_AGENT_INDEX_MIN_STEPS=2` 质量闸门的 episode 与本 run 变更的 App-KB alias
增量写入 sqlite-vec；dream 负责从 JSON 权威源补漏并清除失效项。App alias 通过静态 registry
与 learned/user 名称做确定性 mention 匹配，episode 独立走默认 top-1 语义榜（门槛 0.50）。向量模型
`Qwen/Qwen3-Embedding-0.6B` 仅在索引或非空 episode 召回第一次真正 embed 时懒加载；
`PHONE_AGENT_MEMORY_RAG=on` 会在 run 开局一次性注入 approved / auto_approved lesson（人工批准与蒸馏自批两类皆可），默认上限为
`PHONE_AGENT_LESSON_INJECT_MAX=3` 条、`PHONE_AGENT_LESSON_INJECT_TOKENS=800` 估算 token；
设备 scope 必须匹配，开局未知 app 时不会选择 app 级 lesson。提示明确标为历史参考而非规则，
lesson 视图缺失或损坏时 fail-open。`shadow` 仍只做 trace 召回与命中统计，`off` 不召回也不注入。
召回统计以 evaluations 为 run 级统一分母，输出 `hit_at_1`、`contaminated_run_rate`、
`conditional_hit_rate`、`precision_at_k` / `recall_at_k` 与 package 级 precision/recall；
旧 `false_hits` / `false_hit_rate` 字段暂时保留给现有 Web 控制台读取。

过程卡（procedure）走同一索引的独立召回通道：可注入的过程卡按 `title + steps` 建向量进入 `procedure`
namespace，run-end 增量与 dream 对账都随 lesson 状态同步（变为可注入即写入，撤销/降级即删除）。选择先做
app 包名精确相等 + device scope 硬过滤，再按 embedding cosine 取 top-1，阈值 `recall.PROCEDURE_MIN_SCORE`
（默认 0.30，真实 episode 数据标定）。注入额度独立于 rule：过程卡每次最多 1 张、约 300 token，app 规则每个
App ≤2 条/200 token，超出则截断，提示标注"参考不是规则"并具名来源卡 id。注入有三个确定性时机，共享「每包每
run 至多一次」去重：run 开局选通用卡；mention 预取——goal 文本经 typed resolver 判为 resolved 的 App（≤2 个）
在规划前预取其卡 + app 专属规则；进场注入——`launch_app` 成功或前台包检测进入 App 时送该 App 的卡 + 规则
（`PHONE_AGENT_FOREGROUND_EVENT_BLOCKED_PACKAGES` 可向前台包过滤名单追加）。`on` 档注入，`shadow` 只把
每轮的候选数/命中与未命中原因记入 `recall_stats.json`。
runner 的 `control.jsonl` 接受 `revoke_lesson` 紧急撤销消息：它会立即把 lesson store 标为 revoked，
并让本 run 的后续注入点排除该 id。已经发送给模型的历史消息不可撤回，不会伪装成已从上下文删除。

Web 控制台默认只监听 `127.0.0.1:8080`：输入任务后可实时查看手机画面、步骤时间线、任务板与终局状态，
并处理 `ask_user` / `take_over` / hard 档安全确认。任务由独立 runner 子进程执行，控制台重启后会从
`PHONE_AGENT_RUNS_DIR`（默认 `memory/runs`）回放事件并重连仍存活的任务；无界面用法仍保持进程内直跑。

经验数据默认写入 `memory/experience/{events.jsonl,episodes.json}`；前者是追加式事实日志，后者是按
`run_id` 索引、可重建的物化视图。`PHONE_AGENT_EXPERIENCE=off` 可完全关闭写入；`--dream` 按
`PHONE_AGENT_EPISODE_KEEP` / `PHONE_AGENT_EPISODE_ARCHIVE_DAYS` 将旧全文折叠为无原文的类别成功率统计。

`PHONE_AGENT_EVOLUTION=manual` 仅开放显式离线命令；候选写入
`memory/lessons/{events.jsonl,lessons.json}`。蒸馏以水位线批次处理新 episode（上限 40 条），call-1 输入含
完整目标、逐步 intent/note 账本与机械算出的 struggle_markers（报错步、弯路重走、finish 驳回数），输出
`{"rules": [...], "procedures": [...]}`。harness 拒绝的只有客观错误：严格 JSON、证据 run_id/task_keys/scope
必须逐字引自本批次、kind 形状（procedure 须有非空 steps + app_scope）、语义步（不含坐标/mark id/工具字面量）；
证据校验逐候选跳过而非整批拒绝。无失败锚定与复现硬门槛。
第二次调用对照 harness 事实单（证据结局、每 run 报错回执数、跨任务复现数、与已批准课的矛盾、support 实算，
过程卡加 app_scope 包名验证）对两类候选统一自判分级 `auto_approved` / `needs_review` 并写一句依据——非对称
风险标尺，拿不准一律 `needs_review`；分级失败 fail-open 同样落 `needs_review`。grade/依据/事实单只落
`events.jsonl`。
dream 会对账：证据被折叠后不再够格的 approved 与 auto_approved 经验自动降回草案（lesson_demoted，过程卡落到 needs_review），并按注入组/未注入组成功率给出建议撤销清单（仅提醒）。
离线管线不参与 actor prompt；proposed/needs_review/revoked 永不注入，approved / auto_approved（两类皆可）才可注入。默认 `shadow` 继续只观测，只有显式
`PHONE_AGENT_MEMORY_RAG=on` 才按上述边界注入，并在 trace 与 episode 记录 id。

exemplar（成功先例回注）通道尚未上线，其离线评估用 `python -m phone_agent.v2.replay`：它按时间序在内存 `VecIndex` 里重放 `memory/experience/events.jsonl`，模拟每次 run 开局只能看到先于它结束的 episode，输出 coverage/relevance/steps-delta 三道闸的 JSON 指标；全程 observe-only，不触碰生产索引。

过程卡通道（WP-WF2b）用 `python -m phone_agent.v2.replay --channel procedure [--sweep]` 离线评估：lesson 事件日志先被重放成「时间戳 → 当时可注入过程卡集合」的时间线（后批准的卡、已撤销/降级的卡对之前的 episode 不可见），每个 episode 先硬过滤（卡的 `app_scope` 必须属于它自己成功 launch 过的包名，取不到回执的 episode 走 `general` 卡池）再按 goal 与「卡 title+steps」的 cosine 取 top-1。覆盖 = 有命中的 episode 占比，相关 = 命中里 app 落地的占比（**只是代理**：语义是否真对齐要等线上数据，见模块 docstring），steps-delta 无法离线反事实、记 N/A 且不参与判定；`--sweep` 扫 `min_score` 0.30→0.70（步进 0.05）并给出「relevance ≥ 0.6 里 coverage 最高」的拐点阈值。卡池为空时输出 `candidate pool empty` 并正常退出。

## 插件与扩展

策略层全事件化（栈上只剩桥接器），插件与内建能力共用同一装配层，可挂事件监听器、工具、提示块、run hooks、CLI 命令。

```bash
.venv/bin/python main_v2.py plugin add ./my-plugin   # 或 pip 安装声明了 taskwizard.capabilities entry point 的包
```

`plugin add` 的目录会在下次启动时执行其 `plugin.py`——与 `pip install` 同级信任，只加你信任的源码。开发指南见[文档站插件页](https://tom-cat-mao.github.io/TaskWizard/plugins/)。

## 文档

📖 **完整文档站：<https://tom-cat-mao.github.io/TaskWizard/>**

| 主题 | 入口 |
|---|---|
| 快速开始 | [文档站](https://tom-cat-mao.github.io/TaskWizard/quickstart/) · [`.env` 模板](.env.example) |
| 配置参考（全量） | [文档站配置页](https://tom-cat-mao.github.io/TaskWizard/configuration/) |
| 架构 / 安全 / 记忆 / 路线图 / 插件 | [文档站](https://tom-cat-mao.github.io/TaskWizard/) |
| Agent 开发约定 | [AGENTS.md](AGENTS.md) |

## Contributing

欢迎提交 Issue 和 Pull Request；开始编码前请先阅读 [AGENTS.md](AGENTS.md) 的开发契约与 P0 约束。

## License

本项目基于 [Apache License 2.0](LICENSE) 开源。
