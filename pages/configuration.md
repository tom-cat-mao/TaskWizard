# 配置参考

本页是配置项的**唯一详细手册**。所有运行时配置为 `PHONE_AGENT_*` 环境变量，优先级：**CLI 参数 > shell
环境变量 > `.env` > 默认值**。默认值与支持取值以 `phone_agent/v2/config.py` 为准，模板与逐项注释见
[`.env.example`](https://github.com/tom-cat-mao/TaskWizard/blob/main/.env.example)。

## 配置层级与装配 {#config-precedence}

| 层 | 来源 | 说明 |
|---|---|---|
| 1 | CLI 参数 | 只覆盖少量运行控制项（设备、步数、安全模式、语言等），最强 |
| 2 | shell 环境变量 | 显式导出的 `PHONE_AGENT_*` |
| 3 | `.env` | 仓库根的 `.env`；**只填充未导出的键**，不覆盖已存在的 shell 环境变量 |
| 4 | 默认值 | `v2/config.py` 中的声明 |

模型层在环境变量之下再叠一层单层 `models.json`：`PHONE_AGENT_MODELS_FILE` 显式指定的文件优先，否则读运行目录下的 `.taskwizard.models.json`。它只为声明式提供方、模型条目与可选 `roles` 段服务，不是通用配置层。

装配时的容错与失败边界：

- **可见失败**：显式的未知 provider、无法构建的显式引用不会静默改走其它网关——只有显式配置 `PHONE_AGENT_FALLBACK_MODEL` 时才按该备用降级一次（首选构建失败时降级一次；调用在传输自身重试耗尽后仍失败时再经备用调用一次，同目标不重复）；
- **逐项跳过**：声明文件缺失、损坏或部分条目坏时跳过该项并记录结构化的 `declaration_warnings`（来源、范围、名称、错误；脱敏且有长度上限），run 内另落 `models_declaration_warning` trace 事件；env 合成的 gateway 始终可用；
- **严格解析**：`load_raw_document` / `parse_models_document` 保留完整校验语义，显式校验路径仍 fail-closed 报配置错误；拼写错误的新协议/缓存声明会阻止选择受影响的 provider/model，避免被跳过后静默按默认值执行；
- **窗口绑定**：构建器把声明或探测到的 `contextWindow` 私有绑定到实际模型对象，主模型与备用模型的最终准入使用该上界；已知的实际容量只允许被收紧，不能被较大的覆盖值放宽。未知 serializer 的输出上限报告为 unknown，不从 `maxTokens` 猜一个值。

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

### 多提供方（models.json） {#models-json}

默认零配置：所有角色走上面的网关。声明额外提供方/模型时使用单层 `models.json`（运行目录下的 `.taskwizard.models.json`，`PHONE_AGENT_MODELS_FILE` 显式指定时优先），各角色模型变量随即可写成 `provider:model` 路由到第二模型。

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_MODELS_FILE` | path | 无 | 显式指定 models.json，优先级最高 |
| `PHONE_AGENT_THINKING` | `off`/`minimal`/`low`/`medium`/`high` | 不发送 | 思考级别；按端点声明的 thinking 映射翻译为原生配置，不支持的静默省略 |

条目字段、合并与容错、`--list-models` 与 thinking 翻译细节见[模型提供方与路由](providers.md#models-file)。

#### 请求协议与可选缓存 {#compat-request-cache}

`compat` 可写在 provider 或模型条目上，模型只覆盖明确设置的字段。`requestApi` 选择 OpenAI family 的请求格式
（`auto`/`chat`/`responses`），`cachePolicy` 默认 `off`、`stable-prefix` 显式启用原生缓存标记；冲突与不支持的
组合可见失败，拼写错误会阻止选择受影响的 provider/model。字段语义、示例与缓存准备规则见
[模型提供方与路由](providers.md#compat)。

### 流式决策（streaming） {#streaming}

有效决策 = `roles.<role>.streaming` > 模型条目（含 `modelOverrides`）`streaming` > 全局 `PHONE_AGENT_STREAMING`
（默认 `off`）。`off`/未声明不下发参数；有效决策翻译为三种 transport 各自的 `streaming` 参数，SDK 聚合同一条
完整消息，工具只从聚合消息执行一次，Web 只挂观察者；可用性 fallback 按自身声明与全局配置构建，不继承首选角色
覆盖。细节见[模型提供方与路由](providers.md#streaming-decision)。

#### roles 段（每角色调用配置）

顶层可选 `roles` 段，键为角色名（`actor`/`memory`/`verifier`/`safety_reviewer`/`distill`），值为该角色的
`model`、`samplingParams`、`thinking`、`streaming` 覆盖；模型引用优先级、回落链与构建期降级见
[模型提供方与路由](providers.md#role-routing)。

### 可用性 fallback

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_FALLBACK_MODEL` | str | 无 | actor 的可选备用目标（`provider:model` 或默认 gateway 上的裸模型名）；缺省、同目标或不可构建时不启用。构建失败降级一次，调用失败在传输重试耗尽后再经备用调用一次，降级写 `model_fallback` 审计；其余角色仅构建期降一跳。细节见[模型提供方与路由](providers.md#fallback) |

## 运行控制

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_DEVICE_ID` | str | 自动识别 | ADB 设备序列号；多设备时必填 |
| `PHONE_AGENT_LANG` | `cn`/`en` | `cn` | 提示词语言 |
| `PHONE_AGENT_MAX_STEPS` | int | `100` | 单轮最大模型调用数；仅作失控保险丝，非成本手段 |
| `PHONE_AGENT_MAX_HITL_RESUMES` | int | `20` | 人工中断恢复次数上限 |

## 预算与上下文压缩 {#budget-and-compact}

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `PHONE_AGENT_TOKEN_BUDGET` | int | `1000000` | 单轮累计 input+output token 阈值，达到后停止；已发有效 finish 复核包有一次确认续办机会（见下文） |
| `PHONE_AGENT_TOKEN_WARN_REMAINING` | int | `100000` | 剩余低于该值时向模型注入一次余量提醒 |
| `PHONE_AGENT_COMPACT` | bool | `true` | auto-compact 总开关 |
| `PHONE_AGENT_COMPACT_WARN_RATIO` | float | `0.75` | 上下文占窗口比例达到此值时提醒模型收敛 |
| `PHONE_AGENT_COMPACT_TRIGGER_RATIO` | float | `0.92` | 达到此值时生成 handoff 摘要并折叠历史 |
| `PHONE_AGENT_CONTEXT_WORK_TARGET` | int | `32000` | 完整模型输入的软工作目标，含工具、任务板、历史与图片；`0` 只关闭该目标，物理窗口与 T1/T2 仍有效 |
| `PHONE_AGENT_COMPACT_TARGET_RATIO` | float | `0.7` | 压缩后的软工作目标比例，必须大于 0、小于 1；保护内容无法达到软目标时不强行删除，物理容量风险仍可转为容量修复 |
| `PHONE_AGENT_COMPACT_SUMMARY_TOKENS` | int | `2000` | 摘要表达及接受上限；超长摘要不提交，不是价格预算 |
| `PHONE_AGENT_COMPACT_MIN_REDUCTION_TOKENS` | int | `1000` | 普通工作压缩至少释放的输入 tokens，与相对门槛同时满足；已超物理容量且恢复准入的正净缩减修复除外 |
| `PHONE_AGENT_COMPACT_MIN_REDUCTION_RATIO` | float | `0.1` | 普通工作压缩至少释放原输入的比例；不得大于等于 1 |
| `PHONE_AGENT_COMPACT_SCHEMA_RESERVE` | int | `3000` | T1/T2 比较时为随每轮请求发送的序列化工具 schema 预留的 token 数（估算不可见部分） |
| `PHONE_AGENT_COMPACT_OUTPUT_RESERVE` | int | `2000` | T1/T2 比较时为下一轮回复预留的 token 数 |
| `PHONE_AGENT_CONTEXT_WINDOW` | int | 按实际构建的 actor 推断，兜底 `256000` | 用于窗口规划；最终请求准入只能收紧已知实际模型的窗口声明，不能放大。未设置时按**实际构建**的 actor（含构建降级目标）推断；旧自定义 Provider 没有新 support 时也保留 ModelSpec 的已知窗口，备用模型按自己的声明检查 |
| `PHONE_AGENT_MEMORY_MODEL` | str | 主模型 | compact 摘要使用的模型 |
| `PHONE_AGENT_IMAGE_KEEP` | int | `2` | 历史中保留的含图消息数 |
| `PHONE_AGENT_OBS_MARKS_KEEP` | int | `2` | 历史中保留完整 marks 摘要的观测数 |

### 预算契约 {#token-budget}

成本以 token 计，累计 `usage_metadata` 的 input + output；没有 usage 时回退到本地启发式估算：CJK 字符按约 1 token/字、其余文本按 `len // 4`，每张图片按 1500 token，工具调用参数与未识别的非空 `additional_kwargs` 也计入。用量台账跨压缩持续累加，因此压缩不会重置预算。

Token 预算在模型调用边界检查，已发生的调用与验收用量仍完整累计，因此最终用量可以超过阈值；达到阈值后以 `token_budget_exhausted` 停止本轮。

唯一续办例外：若达到阈值时已有由成功观测产生的 finish 复核包，且屏幕序号、原始目标与关闭且合法的任务板仍匹配，本 run 最多再给模型一次真实回复机会，处理该复核包的 `finish(confirm=true)`；不增加 `MAX_STEPS`、不自动完成。这份续办额度只覆盖该响应中的一次有效确认，其他工具操作返回 error-status 未执行回执。以下情况不补发额度：重复复核、确认被拒、人工中断恢复、复核后再次委托执行普通工具（即使命令可能已派发但回执失败、没有新观测）；续办响应中被预算直接拒绝、未委托执行的普通工具也不会撤销同响应的合法确认机会。`FINISH_VERIFY=off` 不使用这份续办额度。独立验收器的拒绝与故障 `skipped` 语义保持原样，人工控制、`PHONE_AGENT_MAX_STEPS`（`loop_fuse` 保险丝）与已接受的终局优先。

### 压缩契约 {#auto-compact}

工作目标不改变 `contextWindow`。每轮先清理旧图/marks，再按实际模型的可选 context support 估算；provider 已计入工具定义时不重复加 schema reserve。未知/native 内容使用明确的启发式估算，不视为零成本；这不等同于真实 provider tokenizer。

语义压缩只归并完整的已闭合 AI/工具组，保留原始任务、当前 TaskDoc、最新完整组、活跃图/marks 和 opaque 依赖；不截 HTML 调用参数。摘要模型输入过长时按完整组分段后合并，单组装不下则跳过。单次压缩最多 8 次逻辑摘要调用（含本层重试/合并，不包含 SDK 内部 HTTP 重试）；每次调用前检查已有 run token 预算，耗尽后不再付摘要调用。失败、超长或净缩减不足时保留已经完成必要图像清理的基线，不提交部分摘要。最终发送前还需对实际模型、最新 pins 与工具定义做容量准入，不能在 fallback 内单独截断一份临时历史。已超物理窗口的请求只要正净缩减并恢复准入，就不会被普通软收益门槛拒绝。<!-- allow:不再 -->

软目标不能阻止合法的物理容量修复。例如最新必留 HTML 组大于 32k 的低水位、但仍能放进实际模型窗口时，接近/超过物理阈值的请求会按物理目标重新规划；完整工具组、原生依赖与观测仍保留。压缩完成但软目标未达到会记录 `soft_target_unattainable`，不冒充已达到 32k。

原生签名可位于标准 text/image block 的嵌套 `extras` 中，估算与保护会递归识别。普通 SDK function-call id 对照表不因此永久占住压缩边界。若自定义 Provider 把原生签名放在必须移除的旧图片或 OBS marks 块上，当前没有可验证的合法重放投影：micro 在改任何消息前预检并报告 `native_context_pruning_conflict`，不会搬动签名或保留额外旧图。最新 K 的签名块保持原样；同消息其他文本块有签名不妨碍无签名旧图清理。

未识别的非空 `additional_kwargs` 默认作为可能的 Provider 续接载荷保留，并作非零序列化估算。只有已经被结构化调用表示覆盖的 `tool_calls` / `function_call` 和已确认的 `__openai_function_call_ids__` 对照表属于例外；不能因为某个字段不在已知签名列表中，就认为可以丢弃。

**消息协议契约**：`model/pre_request` 监听器是纯的全消息列表变换——**不得**返回 `RemoveMessage`，也不返回 `jump_to` 做流程控制。监听器改动了列表时，由桥接中间件统一铸出唯一合法的 `[RemoveMessage(REMOVE_ALL), *result]` 形态交给下游。

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
| `PHONE_AGENT_PARALLEL_TOOL_CALLS` | bool | `false` | 仅 provider hint：默认向 OpenAI 兼容传输发送 `parallel_tool_calls=false`（provider 声明不支持该参数时不发送），设 `true` 则不发送该 hint（仅供拒绝该参数的网关使用）。两种取值都不解除单批执行（工具调用仍由 admission 串行） |

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
| `PHONE_AGENT_FINISH_VERIFY_K` | int | `1` | 验收器查看的尾部截图数；当前实现恒取一次新观测的当前帧，`K>1` 的历史帧保留未接 |
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
| `PHONE_AGENT_TASKDOC_NUDGE_STEPS` | int | `5` | **no-op**：停滞轻推不产生行为，该键仅为 env 兼容保留 |
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
