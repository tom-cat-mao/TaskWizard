# 模型提供方与路由

本页是模型提供方、角色路由与传输机制的详细手册；全部配置键、类型与默认值的唯一清单在
[配置参考](configuration.md)，插件接缝与 manifest 规则在[插件开发](plugins.md)。默认零配置：env 合成的
`gateway` provider（`openai-completions`）复现单网关构建，带浏览器式 User-Agent 与 CF Access 头；需要第二模型
或额外提供方时才引入 `models.json`。

## 单层 models.json {#models-file}

查找顺序（后者优先）：运行目录下的 `.taskwizard.models.json`，然后 `PHONE_AGENT_MODELS_FILE` 显式指定的文件；
没有用户级层。合并语义：provider 按 id 合并（provider 级字段整体替换，未写的字段保留已注册值），models 按 id
upsert；只带 `baseUrl`/`headers`/`compat` 的条目为 override-only，保留现有模型目录。顶层 `roles` 按角色名整条
替换。`apiKey`、`baseUrl` 与 header 值支持 `$ENV` / `${ENV}` 单次解析，缺失的环境变量在解析期报错；`!command`
执行不实现。

条目字段：

| 层级 | 字段 |
|---|---|
| provider | `api`（内置三种或注册的自定义传输）、`baseUrl`、`apiKey`、`headers`、`compat`、`models[]`、`modelOverrides` |
| model | `id`、`name`、`contextWindow`、`maxTokens`、`reasoning`、`inputModalities`、`samplingParams`、`thinkingLevelMap`、`streaming`、`headers`、`compat` |

`inputModalities` 是声明元数据（如 `["text", "image"]`），不参与端点能力探测。`modelOverrides` 部分覆盖模型条目
（不存在则先合成默认条目）：`samplingParams` 与 `headers` 合并，其余字段替换。采样参数合并顺序：模型条目 <
环境变量 < `roles.<role>.samplingParams`；headers 合并顺序：provider → model → 角色覆盖。未知 compat 键忽略
（向前兼容）。

运行时装配（`build_provider_registry`）可用性优先：文件缺失、损坏或部分条目坏时逐项跳过，把
来源/范围/名称/错误写进 `registry.declaration_warnings`（路径与错误类型，脱敏、有界），并记一条有界日志告警，
run 内另落 `models_declaration_warning` trace 事件。env 合成的 gateway 始终可用。严格解析函数
（`load_raw_document` / `parse_models_document` / `parse_models_json`）保留 fail-closed 语义，供显式校验使用。
显式的 `requestApi`/`cachePolicy` 拼写错误标记 `blocks_selection`：该 provider/model 不可被选择，避免坏声明被
跳过后静默按 `auto` 执行；同一提供方后续出现的合法模型声明可以解除该封锁。`--list-models` 打印生效注册表。

## 传输与注册表 {#transports}

provider 的 `api` 字段查传输注册表得到 builder：内置 `openai-completions` → `ChatOpenAI`、`anthropic-messages`
→ `ChatAnthropic`、`google-generative-ai` → `ChatGoogleGenerativeAI`。`register_api_builder` 注册自定义传输
（`api_type` 为小写字母/数字/连字符，`override=True` 原子替换内置实现），`unregister_api_builder` 卸载自定义实现
或还原内置默认。注册会扩展声明用的 api family（append-only）；builder 缺失时构建 fail-closed，错误信息列出当前
已注册类型，绝不静默换传输。注册表是进程级装配期设施，不是运行中热插拔点，也不受 capability release 管理——
插件需自行 `ctx.on_dispose` 清理。

builder 返回 `BaseChatModel`；`build_model_from_resolved` 负责合并（sampling、headers、thinking、compat、
streaming）后分发。`register_provider(ctx, spec)` 在 run 的 registry 上 upsert 一个 provider（同 id 重注册覆盖
字段、保留已合并模型目录）；能力声明 `deps=("providers",)` 即可在 actor 构建前贡献提供方。解析遵循
`provider:model` 寻址：带冒号按第一段选 provider，未知 provider 报错；裸模型名走默认 provider `gateway`，未知
模型 id 以模型默认参数直通。外部 provider 能力在 actor 构建前先注册传输，随后 `apply_provider_declarations` 应用
models.json 声明（`build_provider_registry` 为普通调用方一次完成两段）。

## 角色路由 {#role-routing}

角色为 `actor`、`memory`、`verifier`、`safety_reviewer`、`distill`。模型引用解析顺序（越具体越优先，同具体度
env 胜）：

| 角色 | env 层 | `roles.<role>.model` | 回落链 |
|---|---|---|---|
| actor | `PHONE_AGENT_MODEL`（恒已设置） | 仅当 env 为空 | — |
| memory | `PHONE_AGENT_MEMORY_MODEL` | 次选 | actor |
| verifier | `PHONE_AGENT_VERIFIER_MODEL` | 次选 | actor |
| safety_reviewer | `PHONE_AGENT_SAFETY_REVIEWER_MODEL` | 次选 | verifier → actor |
| distill | 无专属 env | 首选 | memory → actor |

模型引用是裸模型名（默认 provider）或 `provider:model`。构建失败时除 actor 外的角色只降一跳：按回落链取下一个
目标构建，并写 `model_fallback` 审计（stage=build、role、requested、actual、reason=错误类型）；safety_reviewer
的 reviewer 档构建失败同理一跳回落到 verifier 或 actor 主模型。

### 可用性 fallback {#fallback}

`PHONE_AGENT_FALLBACK_MODEL` 是 actor 的可选备用目标：合法 `provider:model`，或按已有默认 gateway 解析的裸模型
名。缺省、与主目标同一解析目标（含构建降级后的实际目标）、或无法构建时保持惰性——不猜端点、密钥或目录条目，
也不循环。首选**构建**失败时先降级一次；调用在传输自身重试耗尽后仍失败时，再经备用调用一次，两次都失败才让
该调用失败。备用按自身 ModelSpec 与全局配置构建，不继承首选专属的 `roles` 采样/thinking/streaming；降级写
`model_fallback` 审计（stage/role/requested/actual/reason/outcome，只含引用与错误类型，不含密钥与正文；运行
trace 记录器就绪前先暂存，就绪后直写）。

## compat 段 {#compat}

`compat` 可写在 provider 或模型条目上；模型只覆盖明确设置的字段，`null`/省略表示继承，显式 `auto`/`off` 可以
撤销 provider 层的对应选择。

| 字段 | 默认 | 语义 |
|---|---|---|
| `requestApi` | `auto` | 仅 OpenAI family：`auto` 保留 SDK 选择（模型名含 `codex` 或 Responses-only 特性可能选择 Responses），`chat` / `responses` 锁定请求格式；与 `samplingParams.use_responses_api` 矛盾、强制 Chat 却出现 Responses-only 设置都会可见失败 |
| `cachePolicy` | `off` | `stable-prefix` 显式启用，并声明网关支持原生缓存标记；OpenAI 必须同时明确 `requestApi=responses`，Anthropic 使用 ephemeral 标记，Google 的显式缓存资源未实现，启用即报错 |
| `supportsUsageInStreaming` | 未声明 | usage 上报能力声明，不是 streaming 开关：显式声明时 openai 路径翻译为传输层 `stream_usage`、anthropic 路径翻译为是否从流事件采集 usage；未声明不下发参数。Google 协议没有等价的请求侧开关，该声明在 Google 路径没有 wire 效果 |
| `maxTokensField` | `max_tokens` | token 上限写进哪个请求字段 |
| `thinkingFormat` | 不发送 | 端点如何表达思考级别，见下节 |
| `supportsParallelToolCalls` | 接受 | 端点是否接受 `parallel_tool_calls`；显式 `false` 时不发送该 hint |
| `extraBody` | 无 | 原样并入请求的额外字段（openai 路径） |

例：已确认支持 Responses 显式缓存的端点写 `{"requestApi": "responses", "cachePolicy": "stable-prefix"}`；原生
Anthropic 只写 `{"cachePolicy": "stable-prefix"}`。`stable-prefix` 在本次调用副本上标记稳定文本，最多选择固定
前缀、上一兼容请求的端点和新稳定端点；遇到仍含图片、完整 marks、当前任务板或不能识别的原生内容即停止。原生
Anthropic 会提升尾部 system，有这种动态块时只装饰前部稳定 system。压缩或内容变化会使已标记的端点失效；不持久写入协议
标记、不修改工具参数、不建立 Google 缓存资源。缓存准备要求 harness 持有完整历史，不同时启用
`use_previous_response_id` 链。

## thinking 翻译 {#thinking}

`PHONE_AGENT_THINKING` 只接受 `off`/`minimal`/`low`/`medium`/`high`；`xhigh` 等 provider 专属档位不是合法 env
值，只能在模型条目或 `roles.<role>.samplingParams` 里按端点约定传入。级别先经模型条目的 `thinkingLevelMap`
映射（缺省 = 内置/直通规则，字符串 = 原样发送，`null` = 不支持、静默省略，对象 = 逐档映射、缺失档回退默认），
再按 `compat.thinkingFormat` 翻译：

- `reasoning_effort`：发送 `reasoning_effort`（值非字符串即省略）；
- `enable_thinking`：写入请求 `extra_body` 的 `enable_thinking`（openai 路径）；
- `anthropic_thinking`：发送 `{"type": "enabled", "budget_tokens": …}`，无显式预算时按级别取 minimal 1024 /
  low 2048 / medium 4096 / high 8192；思考开启时冲突的 temperature≠1 静默丢弃。

Google 路径只接受 minimal/low/medium/high 四档，其余静默省略。不支持的组合一律静默省略，端点不会收到思考
参数。

## streaming 决策 {#streaming-decision}

三层优先级：`roles.<role>.streaming` > 模型条目（含 `modelOverrides`）`streaming` > 全局
`PHONE_AGENT_STREAMING`，默认 `off`；`off`/未声明不下发参数，默认可与单网关构建逐字段一致。有效决策由 builders
翻译为三种 transport 各自的 `streaming` 参数，SDK 自行流式接收并聚合成同一条完整消息——headless CLI、各 aux
角色与 Web 都据此生效。工具只从聚合完成的消息执行一次；动作序列、安全策略、重试与 usage 计账不变。Web 控制台
只是在同一次调用上挂观察者（模型副本 + callbacks），不决定启用与否，也不会把声明 `off` 的模型强制流式；每次
尝试有独立身份，失败或中断的尝试保留已收到的部分文本并标注原因，后续尝试绝不与之拼接。

`PHONE_AGENT_PARALLEL_TOOL_CALLS` 默认 `false`：openai 路径以模型默认参数发送 `parallel_tool_calls=false`
（`bind_tools` 重绑后仍生效）；provider 声明 `supportsParallelToolCalls=false` 时不发送；设为 `true` 也不发送该
hint。两种取值都不解除单批执行（工具调用仍由 admission 串行）。

可用性 fallback 按其自身模型声明 + 全局配置构建，不继承首选角色的 streaming 覆盖。已验证范围为离线 fake
client（httpx MockTransport）下的三协议请求 wire、chunk 聚合出的完整消息（含 tool delta 与 usage）与 fallback
按自身声明构建；尚未对真实网关/模型做流式端到端验证，也未验证各网关对 `stream_options` 的接受度——连真实端点
前建议按端点声明 `supportsUsageInStreaming`，端点不支持流式时用模型条目 `streaming: off` 固定关闭。流式下未
声明 usage 支持时，该调用的 token 统计退回估算口径。

## 模型上下文支持（可选） {#context-support}

自定义 API builder 仍返回 LangChain `BaseChatModel`；不要求实现另一套 wire IR。可用
`phone_agent.v2.providers.bind_context_support(model, support)` 将支持对象私有地绑定到模型：

```python
from phone_agent.v2.providers import (
    ModelContextProfile, ModelInputEstimate, bind_context_support,
)

class ContextSupport:
    def profile(self, model, tools=()):
        return ModelContextProfile(context_window=48000, request_api="my-api", source="plugin")

    def estimate(self, model, messages, tools=()):
        # Replace with your local counter; explicitly state coverage and precision.
        count = local_estimate(messages, tools)
        return ModelInputEstimate(count, includes_tools=True, complete=False, source="plugin-local")

# In your existing builder, after constructing the BaseChatModel:
# return bind_context_support(model, ContextSupport())
```

上述方法是可选的。无支持对象的模型继续走原调用与通用估算；普通 `model_copy()` / `bind_tools()` 后，公共
helpers 仍能找到模型的支持对象。不要把支持对象放进发给 SDK/浏览器的 `metadata` 或 `model_kwargs`。
`model_context_profile`、`estimate_model_input` 报告实际模型容量/输出 cap，以及计数是否覆盖 tools/images、
是否完整、来源；插件计数方法必须是纯本地操作，不能下载图片或调用计数 API。内置支持的输出 cap 取自 SDK 本地
报文序列化（含 alias 合并与最终 `extra_body` 覆盖），无法确定时报告 unknown。

registry 声明的 `ModelSpec.contextWindow` 即使自定义 builder 没有实现 context support，也会私有绑定到构建
结果，供主模型与备用模型最终准入使用；它不替换插件的 estimate/prepare/usage，也不从 `maxTokens` 猜未知
serializer 的输出上限。声明窗口只收紧不放大：支持对象报告值与声明值同时存在时取较小者。

可选 `prepare(model, messages, tools=())` 返回 `PreparedModelMessages(messages, model_kwargs)`；输入已复制。
没有提供此方法的模型/仅计数插件保持原消息与既有缓存字段等价，仅作深复制。有明确 prepare 方法时，
`prepare_model_messages` 才去掉前一协议缓存标记再调用它，并检查去掉缓存元数据后的消息语义与基线一致；删除
历史、修改工具参数/状态、抛异常等会退回合法基线。仅支持缓存元数据装饰，不允许在这个接缝里做语义 compact。
最终原生参数仍由自定义模型 serializer 负责，公共层不按 provider 名拼报文。

准备阶段的调用参数默认只接受 `prompt_cache_options`、`prompt_cache_key`、`prompt_cache_retention`、
`cache_control` 和 `cached_content`，也可放在 `extra_body` 中。插件可用
`cache_parameter_names = ("vendor_cache",)` 声明自己的缓存参数名；未声明参数会拒绝。声明不能把 `truncation`、
历史/模型/工具字段、输出上限、采样或输出格式等语义参数重新归类为缓存，包括嵌套 `extra_body` 和常见
camelCase 形式；违规准备退回基线，不能借缓存提示让服务器截历史。

可选 `protected_message_ids(model, messages)` 返回不可拆原生块所属消息 id；可选 `normalize_usage(message)`
返回 `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens` 四个非负整数或 `None`。
解释不了的字段保留 `None`，不制造零；不返回其他 raw 字段。公共 helper 实名为
`model_protected_message_ids(model, messages)` 与 `normalize_model_usage(model, message)`；原
`AIMessage.usage_metadata` 仍是预算的基础协议。

支持对象由模型持有，没有新增进程全局注册表。`register_api_builder` 是进程级接口，`register_provider` 对已有
registry 的内部修改也不会自动成为 owner 挂载；插件仍须为这类资源安排 `ctx.on_dispose` 清理，避免按名字卸载时
覆盖别的注册者。原生 continuation/compact、服务端缓存资源、价格和金额预算没有在这一接口中实现。

标准 text/image 块也可能在 `extras.signature` 等嵌套字段携带原生重放状态。公共 `phone_agent.v2.native_content`
的检测与 token 估计共用；签名/加密状态所属消息保持不可拆，`__openai_function_call_ids__` 等已知 SDK
bookkeeping 不会把普通工具组永久钉住。`phone_agent.v2.providers` 的公开导出面以该包 `__init__.py` 的 `__all__`
为准：registry 与 registry 构建、builder 注册、角色解析、类型与上述上下文支持 helpers。
