# 配置参考

本页是配置项的**唯一详细手册**。所有运行时配置为 `PHONE_AGENT_*` 环境变量，优先级：**CLI 参数 > shell
环境变量 > `.env` > 默认值**。默认值与支持取值以 `phone_agent/v2/config.py` 为准，模板与逐项注释见
[`.env.example`](https://github.com/tom-cat-mao/TaskWizard/blob/main/.env.example)。

## 模型

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_BASE_URL` | url | `http://localhost:8000/v1` | OpenAI-compatible 网关地址；默认面向本地无鉴权网关，正式部署按需指定 |
| `PHONE_AGENT_MODEL` | str | `autoglm-phone-9b` | 主模型 id，需视觉多模态能力；正式部署按需指定 |
| `PHONE_AGENT_API_KEY` | str | `EMPTY` | 网关 API key；本地无鉴权网关可用默认值 |
| `PHONE_AGENT_MODEL_TIMEOUT` | int | `180` | 单次模型请求超时（秒） |
| `PHONE_AGENT_MODEL_MAX_RETRIES` | int | `2` | 模型请求重试次数 |
| `PHONE_AGENT_TEMPERATURE` | float | 不发送 | 采样参数；网关限制固定值时在此覆盖 |
| `PHONE_AGENT_TOP_P` | float | 不发送 | 同上 |
| `PHONE_AGENT_FREQUENCY_PENALTY` | float | 不发送 | 同上 |
| `PHONE_AGENT_STREAMING` | `off`/`on` | `off` | 模型流式。`off` 保持历史非流式调用路径不变；`on` 时按各协议正式参数（`streaming`）构建模型，SDK 自行流式接收并聚合成完整消息——headless、各 aux 角色与 Web 都生效，Web 控制台只是在同一事件接缝上增量观察（见[控制台](console.md)）。不改变动作序列、安全策略、重试与 usage 计账；可用性 fallback 按其自身模型声明 + 全局配置构建，不继承首选角色覆盖。优先级：`roles.<role>.streaming` > 模型条目 `streaming` > 本开关 |
| `PHONE_AGENT_HTTP_HEADERS` | str | 无 | 附加请求头，格式 `K1=V1;K2=V2` |
| `PHONE_AGENT_USER_AGENT` | str | 内置浏览器式 UA | 覆盖默认 User-Agent；网关在 Cloudflare 后时不要清空 |
| `PHONE_AGENT_CF_ACCESS_CLIENT_ID` | str | 无 | Cloudflare Access 客户端 id；与 secret 必须成对配置 |
| `PHONE_AGENT_CF_ACCESS_CLIENT_SECRET` | str | 无 | Cloudflare Access 客户端 secret |

`PHONE_AGENT_VERIFIER_MODEL` / `PHONE_AGENT_MEMORY_MODEL` / `PHONE_AGENT_SAFETY_REVIEWER_MODEL` 见下方
对应章节，都是“缺省回落”型可选项，不需要为了跑通任务而设置。

### 多提供方（models.json）

默认零配置：所有角色走上面的网关，与旧版行为一致。声明额外提供方/模型时使用单层 `models.json`（项目根 `.taskwizard.models.json`，`PHONE_AGENT_MODELS_FILE` 显式指定时优先；用户级文件已不再读取），之后各角色模型变量都可写成 `provider:model` 路由到第二模型。

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_MODELS_FILE` | path | 无 | 显式指定 models.json，优先级最高 |
| `PHONE_AGENT_THINKING` | `off`/`minimal`/`low`/`medium`/`high` | 不发送 | 思考级别；按模型声明的 `thinkingLevelMap` 翻译为各家原生配置（Anthropic budget_tokens / OpenAI reasoning_effort / Gemini thinkingConfig），不支持的静默省略 |

`PHONE_AGENT_THINKING` 只接受上面五个值；`xhigh` 等 provider 专属档位不是合法的 env 值。确有端点支持
的专属 effort（例如某传输接受 `reasoning_effort=xhigh`）时，只能在 models.json 的模型条目或
`roles.<role>.samplingParams` 里按该端点约定传入，harness 不做通用枚举。

models.json 条目字段：`api`（`openai-completions`/`anthropic-messages`/`google-generative-ai`）、`baseUrl`、`apiKey`（支持 `"$ENV_VAR"` 引用）、`headers`、`compat`（如 `thinkingFormat`、`supportsUsageInStreaming`）、`models[]`（`id`、`contextWindow`、`maxTokens`、`samplingParams`、`thinkingLevelMap`、`streaming`）、`modelOverrides`、可选顶层 `roles` 段（见下）。采样参数合并顺序：模型条目 < 环境变量 < 角色覆盖。`--list-models` 打印生效注册表。

`streaming`（`off`/`on`，可写在模型条目、`modelOverrides` 与 `roles.<role>`）控制该模型/角色的流式调用，优先级为 **角色 > 模型条目 > `PHONE_AGENT_STREAMING`**：模型条目的声明视为端点能力事实（某模型端点不能流式时，即使全局开也可保持 `off`），角色声明是最具体的调用级覆盖。有效决策被翻译为各协议正式参数（OpenAI/Anthropic/Google 的传输层 `streaming`），由 SDK 流式接收并聚合出完整消息；`off`/未声明不下发该参数，默认构建不变。非法取值由严格解析函数（显式校验路径）fail-closed 报错；运行时装配逐项跳过并计入 `declaration_warnings`。

`compat.supportsUsageInStreaming` 是 **usage 上报能力声明，不是 streaming 开关**：显式声明时，openai 路径翻译为传输层 `stream_usage`（流式请求携带 `stream_options.include_usage`），anthropic 路径翻译为是否从流式事件采集 usage；**未声明时不下发任何参数**，保持 SDK/legacy 默认（零配置构建与旧客户端逐字段一致）。Google 协议没有等价的请求侧开关（SDK 始终从流读取 `usageMetadata`），因此该声明在 Google 路径没有 wire 效果——如实界定，不做假装翻译。

运行时装配（registry 构建）是可用性优先：声明文件缺失、损坏或部分条目坏时逐项跳过，并把每处跳过的
来源/范围/名称/错误写入结构化 `declaration_warnings`（路径与错误类型，脱敏、有界）加一条有界日志告警，
run 内另落 `models_declaration_warning` trace 事件；env 合成的 gateway 始终可用。严格解析函数
（`load_raw_document` / `parse_models_document`）保留，显式校验时仍 fail-closed 报配置错误。显式未知
provider、无法构建的显式引用不会静默改用其它 gateway；唯显式配置 `PHONE_AGENT_FALLBACK_MODEL` 时按该
备用降级一次（见下）。

#### roles 段（每角色调用配置）

`models.json` 顶层可选 `roles` 段，键为角色名，值为该角色的调用配置：

| 键 | 说明 | 优先级 |
|---|---|---|
| `roles.<role>.model` | 模型引用（裸模型名或 `provider:model`） | 角色环境变量 > 此处 > 原回落链；actor 的 `PHONE_AGENT_MODEL` 总是已设置，故 `roles.actor.model` 不生效 |
| `roles.<role>.samplingParams` | 采样参数对象 | 模型条目 < 全局环境变量 < 此处（最强） |
| `roles.<role>.thinking` | `off`/`minimal`/`low`/`medium`/`high` | 全局 `PHONE_AGENT_THINKING` < 此处 |
| `roles.<role>.streaming` | `off`/`on` | 全局 `PHONE_AGENT_STREAMING` < 模型条目 `streaming` < 此处（最强） |

`<role>` ∈ `actor`/`memory`/`verifier`/`safety_reviewer`/`distill`；严格解析对未知角色名或非法 thinking 值报错（fail-closed）；运行时装配把坏角色条目逐项跳过并计入 `declaration_warnings`。

!!! note "流式的验收范围（未做真实网关流式测试）"
    流式是**模型层**配置：有效 global/model/role 决策经 `build_model_from_resolved` 翻译成各协议正式参数，SDK 流式接收并聚合完整消息，
    headless CLI 与各角色调用因此同样生效；Web 只是在同一次调用上挂观察者（不改变启用与否）。已验证范围为离线 fake client
    （httpx MockTransport）下：三协议各自的请求 wire（OpenAI/Responses、Anthropic、Google 均可见 `stream: true`）、chunk 聚合出的完整
    `AIMessage`（含 tool delta 与 usage）、`build_chat_model(role=...)` 配置链、以及 fallback 按自身声明构建（备用 `off` 时不被强制流式）。
    **尚未对真实网关/模型做流式端到端验证**，也未验证各网关对 `stream_options` 的接受度——连真实端点前建议按端点声明
    `"supportsUsageInStreaming"`，端点不支持流式时用模型条目 `"streaming": "off"` 固定关闭。流式下若未声明 usage 支持，
    该调用的 token 统计会退回估算口径。

### 可用性 fallback

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_FALLBACK_MODEL` | str | 无 | actor 的可选备用目标：合法 `provider:model`，或按已有默认 gateway 解析的裸模型名。缺省、与主目标相同或无法构建时**不启用**（不猜端点/密钥、不循环）。首选**构建**失败时先降级一次；调用在传输自身重试耗尽后仍失败时，再经备用调用一次——两次都失败才让该调用失败。备用按自身 model metadata + 全局配置构建，不复制首选专属 role 采样/thinking；降级写 `model_fallback` 审计（stage/role/requested/actual/reason/outcome，不含密钥与正文；带 run trace 时落 trace，之前先进暂存）。其余角色**仅构建期**降一跳：memory/verifier → 主模型，safety_reviewer → verifier 或主模型，distill → memory 或主模型；不宣称所有调用都会自动多级链。 |

## 运行控制

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_DEVICE_ID` | str | 自动识别 | ADB 设备序列号；多设备时必填 |
| `PHONE_AGENT_LANG` | `cn`/`en` | `cn` | 提示词语言 |
| `PHONE_AGENT_MAX_STEPS` | int | `100` | 单轮最大模型调用数；仅作失控保险丝，非成本手段 |
| `PHONE_AGENT_MAX_HITL_RESUMES` | int | `20` | 人工中断恢复次数上限 |

## 预算与上下文压缩

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_TOKEN_BUDGET` | int | `1000000` | 单轮累计 input+output token 阈值，达到后停止；已发有效 finish 复核包有一次确认续办机会（见下文） |
| `PHONE_AGENT_TOKEN_WARN_REMAINING` | int | `100000` | 剩余低于该值时向模型注入一次余量提醒 |
| `PHONE_AGENT_COMPACT` | bool | `true` | auto-compact 总开关 |
| `PHONE_AGENT_COMPACT_WARN_RATIO` | float | `0.75` | 上下文占窗口比例达到此值时提醒模型收敛 |
| `PHONE_AGENT_COMPACT_TRIGGER_RATIO` | float | `0.92` | 达到此值时生成 handoff 摘要并折叠历史 |
| `PHONE_AGENT_COMPACT_SCHEMA_RESERVE` | int | `3000` | T1/T2 比较时为随每轮请求发送的序列化工具 schema 预留的 token 数（估算不可见部分） |
| `PHONE_AGENT_COMPACT_OUTPUT_RESERVE` | int | `2000` | T1/T2 比较时为下一轮回复预留的 token 数 |
| `PHONE_AGENT_CONTEXT_WINDOW` | int | 按实际构建的 actor 推断，兜底 `256000` | 手动覆盖上下文窗口大小；显式值优先，未设置时按**实际构建**的 actor 模型（含构建降级后的备用目标）窗口推断 |
| `PHONE_AGENT_MEMORY_MODEL` | str | 主模型 | compact 摘要使用的模型 |
| `PHONE_AGENT_IMAGE_KEEP` | int | `2` | 历史中保留的含图消息数 |
| `PHONE_AGENT_OBS_MARKS_KEEP` | int | `2` | 历史中保留完整 marks 摘要的观测数 |

Token 预算在模型调用边界检查，已发生的调用与验收用量仍完整累计，因此最终用量可以超过阈值。
若达到阈值时已有由成功观测产生的 finish 复核包，且屏幕序号、目标与关闭的任务板仍匹配，本 run
最多再给模型一次真实回复机会，处理该复核包的 `finish(confirm=true)`；不增加 `MAX_STEPS`，不自动完成。
这份续办额度只覆盖该响应中的一次有效确认，其他工具操作返回 error-status 未执行回执。重复复核、
确认被拒或人工中断恢复都不会补发额度；`ask_user` / `take_over` 的人工控制与其他停止条件继续生效。
`FINISH_VERIFY=off` 不使用这份续办额度。独立验收器的拒绝与故障 `skipped` 语义保持原样。
复核后再次委托执行普通工具时，旧复核立即失效；即使命令可能已派发但回执失败、没有新观测，也不能
沿用旧复核续办。续办响应中被预算直接拒绝、未委托执行的普通工具不会撤销同响应的合法确认机会。

## 界面落地（Grounding）

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_GROUNDING_PROVIDER` | `hybrid`/`accessibility`/`locateanything` | `hybrid` | mark 来源；hybrid = 控件树优先，视觉兜底 |
| `PHONE_AGENT_ACCESSIBILITY_TIMEOUT` | float | `3.0` | 控件树抓取超时（秒） |
| `PHONE_AGENT_ACCESSIBILITY_MAX_MARKS` | int | `80` | 单次观测最多输出的 mark 数 |
| `PHONE_AGENT_MARKS_WINDOWED` | `auto`/`on`/`off` | `auto` | 窗口感知 marks（纯展示层）。`auto` 先试 `uiautomator dump --windows`，不支持则回退单根 dump；`on` 强制 `--windows`（不支持报错可见）；`off` 旧平铺渲染。仅影响分组/标注/渲染，寻址/执行/安全门/折叠/locate 不变，`op=blocked` 仅展示不拦截 |
| `PHONE_AGENT_LOCATEANYTHING_MODEL` | path | 无 | 本地视觉定位模型路径；不配置则视觉定位不可用 |
| `PHONE_AGENT_LOCATEANYTHING_MAX_SIZE` | int | `960` | 视觉定位 provider 自身输入图的最长边上限（模型侧档位）。与工具侧 `LOCATE_MAX_SIZE` 独立 |
| `PHONE_AGENT_LOCATE_MAX_SIZE` | int | `0` | locate 工具输入图最长边；`0` = 原图 |
| `PHONE_AGENT_SCOPE_PADDING_RATIO` | float | `0.05` | scope 区域裁剪的边缘扩展比例 |
| `PHONE_AGENT_LOCATEANYTHING_CONTEXT_MAX_CHARS` | int | `200` | locate 指令中单字段提示长度上限 |
| `PHONE_AGENT_PARALLEL_TOOL_CALLS` | bool | `false` | 仅 provider hint：默认不向 OpenAI 兼容传输发送 `parallel_tool_calls`；设 `true` 只是不再发送该 hint，不解除单批执行（工具调用仍由 admission 串行） |

## 观测

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_OBSERVE_SETTLE_MS` | int | `300` | 每次观测采样前的静置毫秒数；`0` 关闭。应对加载延迟的页面 |
| `PHONE_AGENT_BLACK_SCREEN_DETECT` | bool | `true` | 全黑截图判定为 FLAG_SECURE 保护屏，不下发黑图 |

## 安全与验收

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_SAFETY_MODE` | `off`/`wary`/`hard`/`reviewer` | `wary` | 执行类动作门控，详见[安全模式](safety.md) |
| `PHONE_AGENT_SAFETY_REVIEWER_MODEL` | str | 回落 `VERIFIER_MODEL` | `reviewer` 档的风险精排模型；两者皆空（且 models.json `roles` 未指定）时精排不可用，该档按 fail-closed 预警处理——**不**回落主模型 |
| `PHONE_AGENT_FINISH_VERIFY` | `off`/`auto`/`always` | `auto` | finish 独立验收器触发策略；`off` 退化为单段落定。验收器故障 **fail-open**：放行并在审计记 `skipped`，绝不记 `pass` |
| `PHONE_AGENT_FINISH_VERIFY_K` | int | `1` | 验收器查看的尾部截图数 |
| `PHONE_AGENT_VERIFIER_MODEL` | str | 主模型 | 验收器模型 |

## 记忆

### App-KB

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_APP_KB` | bool | `true` | App-KB 总开关（同步/读取/写回/注入） |
| `PHONE_AGENT_MEMORY_DIR` | path | `memory` | 记忆根目录；`EXPERIENCE_DIR` / `LESSONS_DIR` / `VEC_DB` / `RUNS_DIR` 可各自独立覆盖 |
| `PHONE_AGENT_APP_LIST_MAX` | int | `40` | 注入提示词的应用名数量上限 |
| `PHONE_AGENT_DREAM` | `off`/`auto`/`manual` | `manual` | 记忆整理时机；`manual` 仅 `--dream` |
| `PHONE_AGENT_IMPLICIT_ALIAS` | bool | `true` | 隐式纠正：叫法失败→候选包名成功时自动记别名 |
| `PHONE_AGENT_ALIAS_OVERWRITE` | bool | `true` | dream 是否从同 run 的“开错并秒退→随后成功”证据覆盖错误 learned 别名 |
| `PHONE_AGENT_ALIAS_OVERWRITE_NOTES` | comma-separated str | `开错,不对,不是,错了,wrong app` | 模型明确自述开错应用的匹配词表；事件只落命中的词，不落完整 note |

手工纠正可用 `main_v2.py --learn-alias "名称=包名"` 写入最高信任的全局 `user` 别名；即使包未安装也会在警告后保存。`main_v2.py --forget-alias "名称"` 只删除该名称的全局 `user` / `learned` 条目，不影响设备清单。两者都会把实际变更追加到 `memory/app_kb/events.jsonl`。

### App 名解析

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_RESOLVER_DECISION_MODE` | enum | `typed` | `typed` 使用证据类型化三态决策；`legacy` 回退旧 score 阈值决策 |
| `PHONE_AGENT_RESOLVER_MIN_SCORE` | float | `0.90` | legacy 模式 top1 成为 resolved 的最低综合分；typed 模式只作为弱候选展示阈值 |
| `PHONE_AGENT_RESOLVER_MARGIN` | float | `0.08` | legacy 模式 top1 相对 top2 的最小领先分差；不足则 ambiguous |
| `PHONE_AGENT_RESOLVER_TYPED_MARGIN` | float | `0.08` | typed 模式强证据 top1 相对 top2 的最小 `rank_score` 分差；不足则 ambiguous |
| `PHONE_AGENT_RESOLVER_TOP_K` | int | `10` | 结构化结果、trace 与失败回执最多保留的排序候选数 |
| `PHONE_AGENT_RESOLVER_LEXICAL` | bool | `true` | 启用归一化变体、字符 bigram/trigram 与 difflib 候选路 |
| `PHONE_AGENT_RESOLVER_PINYIN` | bool | `true` | 启用全拼与首字母候选路；pypinyin 不可用时 fail-open 跳过 |
| `PHONE_AGENT_RESOLVER_EMBED` | bool | `true` | 启用 `vec.db` App alias 向量候选路；索引/模型不可用时 fail-open 跳过 |
| `PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_MIN_LEN` | int | `4` | 包名分段强证据的最小段长 |
| `PHONE_AGENT_RESOLVER_PACKAGE_SEGMENT_STOPWORDS` | csv | `com,org,net,android,example,app,mobile,free,debug,release` | 包名按 `.`/`_`/`-`/camelCase 切段后过滤的无意义段 |
| `PHONE_AGENT_RESOLVER_AUTO_MATCH_TYPES` | csv | `exact_alias,exact_label,exact_package,exact_package_segment,registered_containment` | typed 模式允许自动 resolved 的强证据类型 |
| `PHONE_AGENT_RESOLVER_CLARIFY_MATCH_TYPES` | csv | `fuzzy,pinyin_full,pinyin_initials,embedding` | typed 模式只用于澄清/候选展示、不会单独自动 resolved 的弱证据类型 |
| `PHONE_AGENT_RESOLVER_W_SIM` | float | `0.8` | 综合分中的相似度权重 |
| `PHONE_AGENT_RESOLVER_W_PRIOR` | float | `0.2` | 综合分中的 App-KB 先验权重 |

`rank_score = W_SIM * sim + W_PRIOR * prior`。在 typed 模式下它只用于排序、margin、回执和 trace，不单独赋予执行 authority。`exact_package_segment` 只接受完整分段相等，例如 `Firefox` 匹配 `org.mozilla.firefox`，但 `fox` 不匹配；`PiliPlus` 匹配 `com.example.piliplus`，但 `plus` 不匹配。名称候选胜出后仍须通过设备安装事实和 launch policy；解析配置不能扩大启动权限。

### 经验与回想

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_EXPERIENCE` | bool | `true` | episode 档案记录开关（observe-only） |
| `PHONE_AGENT_EXPERIENCE_DIR` | path | `memory/experience` | 档案目录 |
| `PHONE_AGENT_EPISODE_KEEP` | int | `500` | 保留的完整档案数；更老的归档为聚合统计 |
| `PHONE_AGENT_EPISODE_ARCHIVE_DAYS` | int | `90` | 超过该天数的档案在 dream 时归档 |
| `PHONE_AGENT_MEMORY_RAG` | `off`/`shadow`/`on` | `shadow` | 语义回想档位；`shadow` 只观测不注入；`on` 只注入可注入 lesson（approved / auto_approved，两类皆可），**不**注入召回 episode；proposed/needs_review/revoked 永不注入 |
| `PHONE_AGENT_EMBED_MODEL` | str | `Qwen/Qwen3-Embedding-0.6B` | 本地嵌入模型（MLX） |
| `PHONE_AGENT_EMBED_DIM` | int | `1024` | 嵌入向量维度 |
| `PHONE_AGENT_VEC_DB` | path | `memory/vec.db` | 向量索引文件；run 结束增量更新，dream 对账 |
| `PHONE_AGENT_INDEX_MIN_STEPS` | int | `2` | episode 索引质量闸门；更短的 run 只留档，alias 不受影响 |
| `PHONE_AGENT_RECALL_TOP_K` | int | `1` | episode 语义榜名额；app mention 独立返回、不占名额 |
| `PHONE_AGENT_RECALL_MIN_SCORE` | float | `0.50` | episode 语义榜的起始门槛（基于实测噪音分布，可按部署重标定）；app mention 走独立确定性榜，不占名额 |
| `PHONE_AGENT_RECALL_DECAY_LAMBDA` | float | `0.02` | 时间衰减速率（按天），只用于语义同分决胜 |
| `PHONE_AGENT_EVOLUTION` | `off`/`manual` | `manual` | 经验提炼开关；`manual` 由 `--distill` 触发 |
| `PHONE_AGENT_LESSONS_DIR` | path | `memory/lessons` | 经验库存储目录 |
| `PHONE_AGENT_LESSON_INJECT_MAX` | int | `3` | 单次注入的经验条数上限 |
| `PHONE_AGENT_LESSON_INJECT_TOKENS` | int | `800` | 注入内容的 token 上限 |
| `PHONE_AGENT_FOREGROUND_EVENT_BLOCKED_PACKAGES` | csv | 空 | app/launched 进场注入的前台包过滤补充名单；叠加内建系统包名单 |

## 任务板与记录

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_TASKDOC` | bool | `true` | TaskDoc 任务板开关 |
| `PHONE_AGENT_TASKDOC_NUDGE_STEPS` | int | `5` | **已废弃/no-op**：停滞轻推已由流程线取代，保留仅为兼容，不再生效 |
| `PHONE_AGENT_TRACE` | bool | `true` | JSONL trace 开关（脱敏，不含截图） |
| `PHONE_AGENT_TRACE_DIR` | path | `.traces` | trace 目录 |
| `PHONE_AGENT_DIAG_EVIDENCE` | bool | `false` | 诊断证据流（live-diagnosis 用；生产默认关闭、零成本） |
| `PHONE_AGENT_DIAG_EVIDENCE_DIR` | path | `outputs/live-diagnosis/.evidence` | 诊断证据流目录 |
| `PHONE_AGENT_DIAG_UNREDACTED` | bool | `false` | 本机诊断全保真模式（仅影响证据流，生产 trace 始终脱敏） |
| `PHONE_AGENT_RUNS_DIR` | path | `memory/runs` | runner 子进程运行目录（事件/控制通道） |
| `PHONE_AGENT_DELIVERABLE` | bool | `true` | run 级 HTML 产出物能力（`write_document`/`update_document`） |
| `PHONE_AGENT_DELIVERABLE_DIR` | path | `outputs/deliverables` | 产出物目录；文件固定为 `<run_id>.html`，上限 256 KiB |

## 插件

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_PLUGINS` | bool | `true` | 外部插件总开关；`false` 关闭全部插件，内建能力不受影响 |
| `PHONE_AGENT_PLUGIN_MANIFEST` | path | `<repo>/.taskwizard.toml` | 项目级插件清单路径覆盖 |
| `PHONE_AGENT_PLUGIN_INDEX` | str | `plugins/index.json` | `plugin search` 索引源（URL 或本地 json） |

!!! note "保留但无读取方的字段"
    `PHONE_AGENT_BUDGET_WARN_RATIO`（旧模型调用预算时代的阈值）已无读取方，仅为 env 兼容保留；成本控制
    请使用 `PHONE_AGENT_TOKEN_BUDGET` 与 `PHONE_AGENT_TOKEN_WARN_REMAINING`。`PHONE_AGENT_TASKDOC_NUDGE_STEPS`
    见上表，为 no-op。
