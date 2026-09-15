# TaskWizard Agent Guide

> LLM 驱动的安卓手机操作 Agent（thin-loop v2）：看一眼屏幕、想一步、动一下。
> 本文件只放**军规与索引**；契约正文在 `pages/` 对应锚点，取舍与代价在 `.agents/notes/`。
> 路径简写：`v2/…` 即 `phone_agent/v2/…`，`middleware/…` 即 `phone_agent/v2/middleware/…`；`adb/`、
> `grounding/`、`config/` 在 `phone_agent/` 下。

## What This Is

每步一次模型调用（LangChain `create_agent`）；harness 只提供工具、执行安全边界、上下文卫生、trace 与固定
schema 经验档案，**不做工作流路由**。所有策略行为都是事件总线上的监听器，编译后的中间件栈只剩桥接器加
可选 `extra_middleware` 观察者。可选 `phone_agent/web/` NiceGUI 前端启动
`python -m phone_agent.runner`，经 `PHONE_AGENT_RUNS_DIR` 的追加式文件观察；web 进程不得拥有设备访问、
工具执行或工作流路由，headless 的 `ThinPhoneAgent.run(...)` 必须始终可用。

## Development Commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/pytest tests -q
.venv/bin/python -m pytest tests -q
.venv/bin/ruff check .
```

真机诊断从 `.agents/skills/phone-agent-live-diagnosis/SKILL.md` 开始；运行或监控设备任务前先读该 skill。
诊断使用 Case + 正式 runner/IPC，报告分开呈现终局事实、检查点证据与诊断推断；`dry-run` 仅验证合成管线，
不等于真机验收。该目录是唯一权威源，`.claude/skills/…` 与 `.codebuddy/skills/…` 是随仓库维护的相对
符号链接，勿另行复制成独立版本。

## P0 Contracts (Must Never Violate)

一行一条军规；**完整语义以「正文」列链接为准**，本表不重复展开。

| # | 领域 | 军规 | 正文 |
|---|------|------|------|
| 1 | **Coordinate** | 0-1000 相对坐标→绝对像素的换算只在工具内；模型永远看不到绝对像素。 | [#coords](pages/architecture.md#coords) |
| 2 | **Marks-first** | 执行动作必须绑定 mark（唯一解，歧义/未命中 fail-closed）；裸坐标只留给 `swipe` 兜底。 | [#marks-first](pages/architecture.md#marks-first)、[#windowed-marks](pages/architecture.md#windowed-marks) |
| 3 | **Image Hygiene** | 每次模型调用前滚动剪除历史截图，只保留最新 K 条含图消息；原生签名冲突 fail-closed。 | [#image-hygiene](pages/architecture.md#image-hygiene) |
| 4 | **Safety Warning Flow** | 默认 `wary`：风险调用不执行、不召唤人工，只回预警；带 `confirm_irreversible=true` 重发才执行；reviewer 档构建链只一跳 `safety_reviewer`→`verifier`。 | [#safety-classification](pages/safety.md#safety-classification)、[#safety-warning](pages/safety.md#safety-warning)、[#safety-reviewer](pages/safety.md#safety-reviewer)、[#human-interrupt](pages/safety.md#human-interrupt) |
| 5 | **Tool Fail-Closed** | 失败返回错误文本，绝不伪装成功；结果不明只说未知，不声称未执行。 | [#tool-fail-closed](pages/architecture.md#tool-fail-closed) |
| 6 | **Trace Redaction** | 每个 model/tool 事件落 trace：文本截断 64 字、敏感子串脱敏、截图 base64 永不落盘。 | [#trace-redaction](pages/architecture.md#trace-redaction) |
| 7 | **Device via DeviceFactory** | 所有设备操作经 `DeviceFactory` → `adb/`；无直接 ADB 调用。 | [#device-factory](pages/architecture.md#device-factory) |
| 8 | **Config via V2Config** | CLI > env > `.env` > 默认；无硬编码端点/密钥/参数。显式引用构建失败可见，不静默换网关。 | [#config-precedence](pages/configuration.md#config-precedence) |
| 9 | **No Force Push** | 绝不 `git push --force` 到 `main` 或 `feature/thin-loop-v2`。 | 仓库规则，无正文 |
| 10 | **No Auto-Commit** | 未被明确要求时不要创建 commit。 | 仓库规则，无正文 |
| 11 | **TaskDoc Board + Flow Line** | `goal_base` 只由 harness 播种；有 open 路线项时 `finish` fail-closed；迁移必须经 `in_progress`。 | [#taskdoc-board](pages/architecture.md#taskdoc-board) |
| 11b | **Output Contract** | 每个工具带 `intent`（本步目标）+ `note`；回执写实际发生了什么，流程线据此派生。 | [#output-contract](pages/architecture.md#output-contract) |
| 12 | **Finish Two-Step + Verifier** | `finish` 两段式（复核包 → `confirm=true`）；被接受即终局；验收器不看 actor transcript，故障 fail-open。 | [#finish-two-step](pages/architecture.md#finish-two-step) |
| 13 | **Token Budget** | 成本按 token 计；无 usage 时回退 CJK 感知估算 + 每图 1500。达阈值停止，不承诺零超额。 | [#token-budget](pages/configuration.md#token-budget) |
| 14 | **Context Work Target + Auto-Compact** | 软工作目标默认 32k；micro 先行、保护组不被截断；`model/pre_request` 监听器是纯全列表变换。 | [#auto-compact](pages/configuration.md#auto-compact) |
| 15 | **Atomic Observation** | `session.observe()` 是唯一观测生产者；单批执行由 admission 互斥 + `max_concurrency=1` 保序。 | [#atomic-observation](pages/architecture.md#atomic-observation)、[#single-batch-execution](pages/architecture.md#single-batch-execution) |
| 16 | **Experience Plane** | 每个完成的 run 恰好追加一条固定 schema 事件；observe-only、fail-open，校验而不转换。 | [#experience-schema](pages/memory.md#experience-schema) |
| 16a | **RAG Recall + Lesson Injection** | 默认 `shadow` 只写 trace；`on` 只注入 approved/auto_approved lesson。 | [#lesson-injection](pages/memory.md#lesson-injection) |
| 16b | **Implicit App Alias** | unknown `launch_app` 失败回执里真实出现的包名才是 run 内证据；设备确认后才可写 `learned`。 | [#implicit-alias](pages/memory.md#implicit-alias) |
| 16c | **Alias Correction** | dream 只对同 run 签名覆盖 `learned` 别名，绝不覆盖 `kind=user`。 | [#alias-correction](pages/memory.md#alias-correction) |
| 17 | **Lesson Evolution** | `evolution.py` 保持离线；语义判断归模型自评，harness 只拒客观假话；撤销的 id 重提即降级。 | [#lesson-evolution](pages/memory.md#lesson-evolution) |
| 18 | **Capability Mount + Plugins** | 十一个能力经五接缝挂载（middleware / tool / prompt / hook / CLI，另有 `register_service`）；插件是外部代码，`plugin add` 即执行授权。 | [#capability-mount](pages/plugins.md#capability-mount)、[#plugin-authorization](pages/plugins.md#plugin-authorization) |
| 19 | **Run-bound Deliverable** | 模型只给 HTML、绝不给路径；目标固定 `<run_id>.html`；失败返回错误字符串且不改动旧文档。 | [#deliverable](pages/architecture.md#deliverable) |
| 20 | **Typed App Name Resolution** | `v2/names.py` 唯一归属；弱证据永不单独 auto-resolve（`learned` 别名带成功计数除外）。 | [#app-name-resolution](pages/architecture.md#app-name-resolution) |
| 21 | **Web Projection** | 控制台只是观察层，不拥有设备/工具/路由；被动读 App-KB 表不构造 store；只有终局事件推进状态。 | [#web-projection](pages/console.md#web-projection) |
| 22 | **Model Streaming** | `PHONE_AGENT_STREAMING` 默认 `off`；决策由 builders 翻译为协议参数，Web 只挂观察者。 | [#streaming](pages/configuration.md#streaming) |
| 23 | **Decision Notes** | 非平凡改动必须带决策笔记：六类命中即写、四格格式、与代码同批提交。 | [笔记规则](.agents/notes/AGENTS.md) |

## Module Map

| Area | Entry |
|---|---|
| CLI / run 入口 | `main_v2.py`（任务或显式离线命令） |
| Agent 装配与终局 | `v2/agent.py`（五条桥接器、core 监听器、`RunResult`） |
| 能力装配 | `v2/capabilities.py`（cap-id、mode、release 簿记） |
| 观测与设备状态 | `v2/session.py`（epoch/marks/参考图/locate） |
| 工具 | `v2/tools/`（感知、操作、TaskDoc、deliverable、finish、HITL） |
| 事件总线与插件 | `v2/{events,plugins,pins}.py` |
| 策略监听器 | `v2/middleware/`（safety、images、compact、budget、trace、procedure、diagnostic、streaming） |
| 任务/解析/验收 | `v2/{taskdoc,resolver,review,verify,names}.py` |
| 经验与进化 | `v2/{experience,evolution,replay}.py` |
| 模型与配置 | `v2/{model,config,prompts}.py`、`v2/providers/` |
| App 知识 | `v2/{appkb,dream}.py` |
| Web runner | `v2/{runner,run_ipc,run_events}.py`、`phone_agent/web/` |
| 保留库 | `phone_agent/{adb,grounding,config}/`、`device_factory.py` |

v1 的 `graph/`、`actions/`、`checkpoint/`、旧 `agent.py`/`main.py`、`evals/` 已删除；不要重建，也不要让新
行为经由它们路由。

## Data & Privacy

- `.env`、`memory/`、`outputs/`、trace 与真实设备画面是本机私有数据：不提交、不发布、不进 Pages/Issue；
  公开演示只用 synthetic/fake 数据。
- Web 只投影事件，不向浏览器发送 API key、认证头或完整配置；生产 trace 脱敏且无截图 base64。
- 经验/记忆写入 observe-only：schema 外字段（工具参数/回执、输入文本、mark 文本、截图、推理）直接丢弃。

## Environment Gotchas

- **只用 `.venv/bin/python -m pytest` / `-m pip`**，绝不用系统 Python。
- **文件搜索**用 `rg` / `rg --files`；不要用 `find` 或 `grep`。
- **模型网关在 Cloudflare 后**：需要浏览器式 User-Agent（`v2/model.py::build_default_headers` 已处理）；
  裸 openai client 会 `403 Your request was blocked`。
- **网关按模型强制采样限制**（如只允许 `temperature=1`）：按部署用 `PHONE_AGENT_TEMPERATURE` /
  `PHONE_AGENT_TOP_P` / `PHONE_AGENT_FREQUENCY_PENALTY` 覆盖，绝不硬编码。
- **Sandbox**：ADB / Metal / 网络 `Operation not permitted` → 经提权机制带可读理由重跑；绝不为破坏性命令
  （`rm`、`git reset`、force push）放宽 sandbox。

## Doc Routing (load on demand)

| When you need... | Load... |
|---|---|
| Install / run / CLI flags / examples | `README.md`、`.venv/bin/python main_v2.py --help` |
| Config keys | `pages/configuration.md`（唯一详细配置页）+ `.env.example` + `v2/config.py` |
| 用户手册与契约正文（P0 表锚点都在这里） | `pages/*.md`（文档站源） |
| 实现状态、延期项与批次记录 | `docs/future-roadmap.md`（批次原始记录是本机私有文档，不入库） |
| 模块契约 | 对应模块 docstring；预算/压缩另见 `middleware/{budget,compact,_tokens}.py` |
| 真机诊断 | `.agents/skills/phone-agent-live-diagnosis/SKILL.md` |
| 决策笔记（为什么这样设计） | `.agents/notes/AGENTS.md`；某篇的正文在 `.agents/notes/<status>/` |
| 批次执行/验收 | `docs/future-roadmap.md` 的「本分支已落地」段 |

注意：`docs/` 默认被 `.gitignore` 忽略，只有显式加入索引的文件才入库。

## Version Management

- 内部状态在 `docs/future-roadmap.md`；公开能力状态在 `pages/roadmap.md`。
- 架构/工具/配置变化时，同一 commit 同步 `README.md` 与 `AGENTS.md`；用户可见行为同步 `pages/`。
- 非平凡改动同一 commit 带决策笔记（见 P0 #23）；只有被明确要求时才 commit。
