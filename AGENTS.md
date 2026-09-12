# TaskWizard Agent Guide

> LLM 驱动的安卓手机操作 Agent（thin-loop v2）：看一眼屏幕、想一步、动一下。**编码前先读本文件；P0 契约不可违反。**
> 用户手册在 `pages/`；实现细节以 `phone_agent/v2/` 源码与 docstring 为准。
> 路径简写：`v2/…` 即 `phone_agent/v2/…`，`middleware/…` 即 `phone_agent/v2/middleware/…`；`adb/`、
> `grounding/`、`config/` 在 `phone_agent/` 下。

## What This Is

每步一次模型调用（LangChain `create_agent`）；harness 只提供工具、执行安全边界、上下文卫生、trace 与固定
schema 经验档案，**不做工作流路由**。所有策略行为都是事件总线上的监听器，编译后的中间件栈只剩桥接器加
可选 `extra_middleware` 观察者。v1 的 LangGraph 节点架构已删除（`adb/`、`grounding/`、
`config/{policy,app_registry,redact}` 作为库保留）。可选 `phone_agent/web/` NiceGUI 前端启动
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

## P0 Contracts (Must Never Violate)

| # | Domain | Contract |
|---|--------|----------|
| 1 | **Coordinate** | 0-1000 相对坐标 → 绝对像素的换算**只在工具内**（`v2/coords.py`）；模型永远看不到绝对像素。 |
| 2 | **Marks-first** | 执行动作必须绑定 mark：`tap` 用 `target_mark_id` 或 `target_description`（唯一解，歧义/未命中 fail-closed），裸坐标只留给 `swipe` 兜底。mark id 带批次徽章 `ax_1@e<epoch>`，跨批 id 在 `resolve_mark` fail-closed；成功 `locate` 开启新批次（旧 marks 全部失效，只铸入命中 mark）。窗口化 marks 是纯展示层（`PHONE_AGENT_MARKS_WINDOWED=auto\|on\|off`；`op=blocked` 不门控执行）。详见 `pages/architecture.md`。 |
| 3 | **Image Hygiene** | 每次模型调用前滚动剪除历史截图：只有最新 `PHONE_AGENT_IMAGE_KEEP`（默认 2）条含图消息保留图片块；工具成功总是回传新图。实现：`middleware/images.py`。 |
| 4 | **Safety Warning Flow** | `classify_tool_call` 把调用判定为 `none\|recall\|reviewer\|hard`（不可逆提交、密码框、凭据/验证码输入、模型自申报触发；`launch_app` 可逆）。默认 `wary`：最内层 `tool/execute` 监听器短路风险执行调用——**不执行、不叫人工**——返回预警（世界事实 + 选项），模型带 `confirm_irreversible=true` 重发才执行；`sensitive=true` 自申报总是预警。`hard` 保留 HITL 审批；`off` 关闭门控；`reviewer` = wary + 第二模型精排（出错 fail-closed）。reviewer 构建入口只在 `PHONE_AGENT_SAFETY_REVIEWER_MODEL` / `roles.safety_reviewer.model` / `PHONE_AGENT_VERIFIER_MODEL` 至少配置一个时才尝试创建，全空则跳过精排、该档按 fail-closed 预警处理；已配置首选的**构建**失败时沿角色链再试一跳一次（`safety_reviewer → verifier → actor` 等）并记 `model_fallback` 审计——仅构建期一跳，非每调用自动多级链。`ask_user`/`take_over` 在任何模式都 interrupt。`PHONE_AGENT_SAFETY_MODE=off\|wary\|hard\|reviewer`（默认 wary）。详见 `pages/safety.md`。 |
| 5 | **Tool Fail-Closed** | 工具返回结果**字符串**；失败返回错误文本，绝不伪装成功。可能已下发设备命令但结果不明时，回执只说明结果未知，不声称未执行。ADB Keyboard 已确认切换而 warm-up 失败时，device helper 先恢复一次已知原 IME，再分开报告“用户文本未发送”与恢复结果；发送成功但恢复失败要保留输入成功事实。base64 `--es msg` 负载在派发前必须 shell-quote（空 warm-up 参数要作为真实空参数存活）。错误留在 transcript。 |
| 6 | **Trace Redaction** | 每个 model/tool 事件写 `<trace_dir>/<run_id>.jsonl`：文本 >64 字截断、敏感子串经 `config/redact.py` 脱敏、截图 base64 永不落盘（只记 `screen_seq` + 字节数）。context 请求观测只输出数字/有界标签；按实际 primary/fallback handler 尝试区分，客户端有序消息差异不冒充服务端 token LCP，SDK 隐藏重试不伪造为已计量。input/output/cache read/write 缺失记 null；原 token 预算仍累计完整 input+output。 |
| 7 | **Device via DeviceFactory** | 所有设备操作经 `DeviceFactory` → `phone_agent/adb/`；无直接 ADB 调用。 |
| 8 | **Config via V2Config** | CLI 覆盖 > shell env > `.env`（`PHONE_AGENT_` 前缀）> 默认；无硬编码端点/密钥/参数。模型层在 env 之下加一层单层 `models.json`（`PHONE_AGENT_MODELS_FILE` > 项目根 `.taskwizard.models.json`）+ 可选 `roles` 段；precedence 与字段见 `pages/configuration.md`。有效 actor `contextWindow` 驱动 compact，声明的 `maxTokens` 进入 transport。显式未知 provider 与无法构建的显式引用仍可见失败，绝不静默换网关（唯显式配置 `PHONE_AGENT_FALLBACK_MODEL` 时才按该备用降级一次）；运行时装配对缺失/损坏/部分坏声明逐项跳过并写脱敏 `declaration_warnings`（env gateway 保留），严格解析函数保留供显式校验。OpenAI `compat.requestApi=auto\|chat\|responses` 缺省保留 SDK auto，显式值锁定协议且不做跨协议探测；错误的新协议/缓存声明不得被跳过后静默用于所选目标。`compat.cachePolicy=off\|stable-prefix` 默认 off，原生标记只在已声明支持的调用副本上准备；不改变 canonical 历史、图片或工具语义。Anthropic 尾部 `SystemMessage` 提升保留；实际协议与输出 cap 由模型 context 支持按 SDK 最终合并/alias 报文报告，未知不造值。 |
| 9 | **No Force Push** | 绝不 `git push --force` 到 `main` 或 `feature/thin-loop-v2`。 |
| 10 | **No Auto-Commit** | 未被明确要求时不要创建 commit。 |
| 11 | **TaskDoc Board + Flow Line** | `goal_base` **只**由 harness 在 run 开始时播种；模型只能通过 `update_task_doc` 写板（绝不写 `goal_base`）。`[TASK_DOC]` 块每轮 pin 进上下文（压缩免疫）；有 open 路线项时 `finish` fail-closed。迁移必须经 `in_progress`，禁止单次批量 `pending`→`completed`；先前 `pending` 的 id 从板上消失会被拒绝（只能迁移）。结构上限：item/证据 ≤500 字、amendment ≤10 条、渲染在 4000 字处截断（goal_base 与路线项永不截断）。流程线由 transcript 纯派生（最近 8 步，无 session 状态）。字段定义见 `v2/taskdoc.py`。 |
| 11b | **Output Contract** | 每个工具带 `intent: str`（本步目标，system prompt 强制）+ `note: str \| None`；执行回执写实际发生了什么（如 `已点击「上海」(ax_3)`），流程线据此派生真实账本。停滞轻推已删除；`PHONE_AGENT_TASKDOC_NUDGE_STEPS` 是保留的 deprecated no-op。 |
| 12 | **Finish Two-Step + Verifier** | `finish` 两段式（复核包 → `confirm=true`，seq 守卫）；`completed` 项必须有 `evidence_note`。被接受的 finish 立即终局：后续 sibling 工具调用收到 error-status skipped 回执且不再采样模型；被接受的 takeover 同样终局，被拒绝的 takeover 继续。独立上下文验收器只看目标 + 证据路线 + 尾部截图，**绝不看 actor transcript**；TaskDoc 关闭时 run 原始目标仍是权威。验收器故障 **fail-open**：放行该 finish 并记审计状态 `skipped`，绝不记 `pass`。`PHONE_AGENT_FINISH_VERIFY=off\|auto\|always`（默认 auto，`off` 退化为单段落定）。 |
| 13 | **Token Budget** | 成本以 token 计（`middleware/budget.py`）：累计 `usage_metadata` input+output（无 usage 回退 `(input+output)//4`、CJK 感知、+1500/图），跨压缩仍有效。`PHONE_AGENT_TOKEN_BUDGET`（默认 1M）在调用边界达到阈值后停止（`token_budget_exhausted`），不承诺零超额。唯一续办例外：harness 已发成功观测的有效 finish 复核包，且 seq、原始目标与关闭且合法的 TaskDoc 仍匹配、未被后续普通工具委托取代时，每 run 最多再给一次真实 actor 回复，额度只覆盖该响应内一次同复核包的 `finish(confirm=true)`；普通工具返回 error-status 未执行，不自动确认、不重置用量、不通过重复复核或 HITL 恢复补发额度。独立验收器、人工控制及终局守卫照常；`FINISH_VERIFY=off` 不续办。`PHONE_AGENT_TOKEN_WARN_REMAINING`（默认 100k）注入一次性余量提醒；`PHONE_AGENT_MAX_STEPS`（默认 100）仍是有权阻断续办的 `loop_fuse` 保险丝。 |
| 14 | **Two-Threshold Auto-Compact** | `middleware/compact.py` 作为 `model/pre_request` 监听器最先运行：T1（窗口 0.75）提醒收敛；T2（0.92）用文本模型生成手机交接摘要并重建 transcript。不拆 `tool_use`/`tool_result` 对、不折叠 pinned 块、摘要失败 fail-open。窗口解析：`PHONE_AGENT_CONTEXT_WINDOW` > actor `contextWindow` > 模型名提示 > 256k，并加 schema/output 预留。总开关 `PHONE_AGENT_COMPACT`。**消息协议契约**：`model/pre_request` 监听器是**纯全列表变换**——绝不发 `RemoveMessage`、绝不返回 `jump_to`；列表变化时由桥接中间件统一铸出唯一合法 `[RemoveMessage(REMOVE_ALL), *result]`。 |
| 15 | **Atomic Observation** | `session.observe()` 是**唯一观测生产者**：静置（`PHONE_AGENT_OBSERVE_SETTLE_MS`，默认 300ms）→ 截图 → 同帧 accessibility dump → 前后台比对；前台变化或瞬时 dump 失败（timeout/parse/provider/`on` 不支持）同层级重试**一次**。只有末次可提交：最终截图有效但 dump 仍失败时提交标注过的零 marks 帧；真正空屏不重试。几何只随成功观测提交；黑屏（RGB ≤4）在 `PHONE_AGENT_BLACK_SCREEN_DETECT=on` 下经 `secure_screenshot_blocked` fail-closed。成功观测 epoch+1 并铸批次徽章；旧批次 id 在 `resolve_mark` fail-closed；观测失败整批作废（marks 清空、epoch 冻结）。**失败参考图**：本次取得过有效截图时返回标注过的未验证参考图——不提交 epoch/`screen_seq`/几何、不铸 mark、secure 不保留、无有效截图不造图；动作成功回执不被降级。**单批执行**：execution-admission 监听器串行化工具调用、保持声明顺序并在终局转换后阻断 siblings；`PHONE_AGENT_PARALLEL_TOOL_CALLS` 只是 provider hint，设 true 不解除串行。 |
| 16 | **Experience Plane** | 每个完成的 run 恰好追加一条固定 schema `episode_outcome`；工具回执追加固定 schema 事件。observe-only、所有失败 fail-open；**校验而不转换**：字符串原文照存，schema 外字段（工具参数/回执、输入文本、mark 文本、截图/base64、模型推理）直接丢弃；`injected_lessons` 只含 lesson id，`deliverable_path` 仅在本 run 成功写出后非空。`events.jsonl` 是追加式真相，`episodes.json` 可重建。schema 与字段见 `v2/experience.py`。 |
| 16a | **RAG Recall + Lesson Injection** | `v2/recall.py` 维护可重建索引（episode / app_alias / procedure）。默认 `PHONE_AGENT_MEMORY_RAG=shadow`：run 开局召回只写 trace 与统计，**绝不进 actor 上下文**。`on` 只注入可注入 lesson（approved / auto_approved，两类皆可）：run 开局 L0 Mirror + 过程卡三时机投递（开局 / mention 预取 / 进场），每包每 run 一次；proposed/needs_review/revoked 永不注入。投递前按权威视图复检（存在、可注入、版本匹配），不匹配只抑制该次投递。可选记忆缺失、坏快照、诊断写失败都 fail-open，并区分 `snapshot_missing` / `snapshot_corrupt` / 撤销 / 版本不符 reason，rule 与 card 抑制计数独立。细节与统计口径见 `pages/memory.md`，常量见 `v2/recall.py`。 |
| 16b | **Implicit App Alias** | `PHONE_AGENT_IMPLICIT_ALIAS=on`（默认）时，unknown `launch_app(name)` 只把失败回执里实际出现的包名留作 run 内证据；之后设备确认启动该精确包名时可写 `kind=learned`；空/不匹配证据、跨 run 状态、同词与 `off` 绝不写。 |
| 16c | **Alias Correction** | `PHONE_AGENT_ALIAS_OVERWRITE=on`（默认）时 dream 只对同 run 签名（启动 A → 1–2 步内成功 back/启动他 app 且 note 命中 `PHONE_AGENT_ALIAS_OVERWRITE_NOTES` → 成功启动 B）覆盖 `learned` 别名：delete-old/store-new，绝不覆盖 `kind=user`。`--learn-alias` 写全局 `user`；`--forget-alias` 只删 user/learned。 |
| 17 | **Lesson Evolution** | `v2/evolution.py` 保持离线。harness 只是**事实核验者、供给者与簿记员**；语义判断归模型自评，纠正归人工 CLI + dream 降级。`--distill` 走水位线批次（`(ts_end, run_id)` 游标、上限 40、同一批次连续失败 3 次放弃）；模型只输出语义字段，harness 只拒客观假话（严格 JSON、闭世界引用、kind 形状、语义步无坐标/mark id/工具字面量），**无失败锚定与复现硬门槛**。call-2 按 harness 事实单统一自评分级，拿不准一律 `needs_review`；任何分级失败 fail-open、绝不丢候选。只有 approved / auto_approved 可注入；已被撤销的 id 重提到达即降级。LessonStore 读写在进程锁 + 文件锁内先重放权威事件再原子重写视图；dream 只按证据丢失降级。状态集合与常量见 `v2/evolution.py`。 |
| 18 | **Capability Mount + Plugins** | `v2/capabilities.py` 是装配器：十一个内建能力经五条接缝挂载（有序中间件、工具、prompt providers、start/end run hooks、CLI 命令）；内建策略全部是事件总线监听器（`v2/events.py`）。core `tool/execute` 最终序：trace → diagnostic → admission → control HITL → capability 链（safety 最内）。`ctx.on` / `ctx.on_dispose` 把订阅与清理绑定到 owner；release 会清掉所有残留，正常模式变更在该 release 后仍会 apply 新能力——只有在清理报错时才不再 apply 替换。内建**静态装配**：无文件监视、无运行中工具表重建。provider bootstrap 与完整装配复用同一 context，bootstrap 只支持 `providers` 与外部 helper，缺失/循环/依赖 runtime 内建一律启动时可见失败。**插件是外部代码**：CLI 与 runner 共用授权发现（`taskwizard.capabilities` entry points + manifest，strict 默认）；**`plugin add` 就是执行授权**（pip 同级信任）；Web 空闲/启动不执行插件代码，runner 装配时 import 一次；不支持运行中热重载；`PLUGIN_API_VERSION=1` provisional。`providers/context.py` 提供可选、模型私有绑定的 profile/estimate/cache prepare/usage 接缝，工厂仍返回 `BaseChatModel`；旧插件无支持对象仍可运行，普通 model copy/tool binding 保留支持；不新增无 owner 的全局 context 表；cache prepare 参数是明确 allowlist，不能伪装成截历史/输出控制；`native_content.py` 统一递归识别标准块 extras 中的原生签名/加密状态，已知 SDK bookkeeping 不当作原生依赖。旧 process-global API 注册与 registry 内部 upsert 仍需插件显式清理，不宣称其已自动 owner 化。runner 唯一的 in-run **能力变更**控制是 `revoke_lesson`（已发送上下文不可撤回）；stop / HITL 等既有控制照常存在。详见 `pages/plugins.md`。 |
| 19 | **Run-bound Deliverable** | 仅在 `PHONE_AGENT_DELIVERABLE=on`（默认）挂载 `write_document` / `update_document`：模型只给 HTML、绝不给路径，目标固定 `PHONE_AGENT_DELIVERABLE_DIR/<run_id>.html`（UTF-8，≤256 KiB）。create 拒绝已存在文件，update 拒绝缺失/非普通/symlink；任何失败返回错误字符串且不改动先前文档。生产 trace 不含 HTML 正文；没有 delete 工具。 |
| 20 | **Typed App Name Resolution** | `v2/names.py` 是归一化、四路候选（exact/lexical/pinyin/embedding）、包名去重、先验排序与三态决策的唯一归属。默认 typed；弱证据（fuzzy/pinyin/embedding）永不单独 auto-resolve；`exact_package_segment` 只匹配完整包段；resolved 后仍要过装机清单与 launch policy。`rank_score` 只作排序/分差/回执/trace 信号。 |
| 21 | **Web Projection** | 控制台只是观察层：web 进程不得拥有设备访问、工具执行或工作流路由，只投影 runner 的追加式事件，且浏览器收不到 API key/认证头/完整配置。被动 App-KB 表读取只读 `kb.json` 快照（缺失/损坏/坏行 → 空表或跳过，不构造 store、不重放/改写/触碰文件）。状态只由真实事件推进：`已请求停止` 不等于已停止，只有终局 `run_end` 才转为结束态；模型身份中 requested 只来自请求绑定，actual 只取自 provider 返回元数据，缺失写「未上报」，不得用首选或配置的备用名顶替；无 `screen_seq` 的参考帧标注「未验证」并各自保留身份，不冒充新观测。配置抽屉留空/纯空白设备 serial = 显式自动（解析为 None 后进入序列化/fingerprint），省略 override 才继承 env；run spec 的可选 App-KB generation 以显式 `generation`/`version` 或内容摘要标识，漂移只写 `memory_generation_drift` 审计事件（captured/actual），绝不阻止启动。 |
| 22 | **Model Streaming** | `PHONE_AGENT_STREAMING` 默认 `off`（默认构建与 legacy 逐字段一致，不下发任何流式参数）；有效决策（`roles.<role>.streaming` > 模型条目 `streaming` > 全局 env）由 `providers/builders.py` 翻译为各协议正式参数（三种 transport 的 `streaming`），SDK 自行流式接收并聚合成同一条完整 `AIMessage`——headless、aux 角色与 Web 一致生效。Web 只在该调用上挂观察者（`middleware/streaming.py`，模型副本 + callbacks），不决定启用与否；provider 构建的模型带决策标记，声明 `off` 的模型（含 fallback 按其自身模型+全局配置构建）绝不被观察者强制流式。工具只从聚合消息执行（参数不完整绝不执行），动作序列/安全策略/重试/usage 计账不变；每次尝试独立身份，失败流保留部分文本并记 `ok=false`，后续尝试绝不拼接。增量事件只含文本字段，且按“未闭合敏感段不出站”的 settle 缓冲跨 chunk/flush 脱敏（无 base64/密钥/认证头）；`compat.supportsUsageInStreaming` 是 usage 上报声明而非 streaming 开关：显式声明才翻译为 openai `stream_usage`/anthropic 流式 usage（Google 无等价请求开关），未声明不下发。**未做真实网关流式端到端验证**。 |

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
- 经验/记忆写入 observe-only：schema 外字段（工具参数/回执、输入文本、mark 文本、截图、模型推理）直接丢弃。

## Environment Gotchas

- **只用 `.venv/bin/python` / `.venv/bin/pytest` / `.venv/bin/pip`**，绝不用系统 Python。
- **文件搜索**用 `rg` / `rg --files`；不要用 `find` 或 `grep`。
- **模型网关在 Cloudflare 后**：请求需要浏览器式 User-Agent（`v2/model.py::build_default_headers` 已处理）；
  裸 openai client 会得到 `403 Your request was blocked`。
- **网关按模型强制采样限制**（如只允许 `temperature=1`、`top_p=0.95`、`frequency_penalty=0`）：按部署用
  `PHONE_AGENT_TEMPERATURE` / `PHONE_AGENT_TOP_P` / `PHONE_AGENT_FREQUENCY_PENALTY` 覆盖，绝不硬编码。
- **Sandbox**：ADB / Metal / 网络 `Operation not permitted` → 通过 agent 前端提权机制带用户可读理由重跑；
  绝不为破坏性命令（`rm`、`git reset`、force push）放宽 sandbox。

## Doc Routing (load on demand)

| When you need... | Load... |
|---|---|
| Install / run / CLI flags / examples | `README.md`、`.venv/bin/python main_v2.py --help` |
| Config keys | `pages/configuration.md`（唯一详细配置页）+ `.env.example` + `v2/config.py` |
| 用户手册（架构/安全/控制台/记忆/插件/路线图） | `pages/*.md`（文档站源） |
| 实现状态与延期项 | `docs/future-roadmap.md` |
| 模块契约 | 对应模块 docstring；预算/压缩另见 `middleware/{budget,compact,_tokens}.py` |
| 真机诊断 | `.agents/skills/phone-agent-live-diagnosis/SKILL.md` |
| 批次执行/验收记录 | `docs/execution/`；v1 历史日志在 `docs/archive/` |

注意：`docs/` 默认被 `.gitignore` 忽略，只有显式加入索引的文件才入库。

## Version Management

- 内部状态在 `docs/future-roadmap.md`；公开能力状态在 `pages/roadmap.md`。
- 架构/工具/配置变化时，同一 commit 同步 `README.md` 与 `AGENTS.md`；用户可见行为同步 `pages/`。
- 只有被明确要求时才 commit。
