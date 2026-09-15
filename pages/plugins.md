# 插件开发

插件是一个导出 `CapabilitySpec` 的 Python 模块（例如 `my-plugin/plugin.py`），与内建能力走同一装配层。

```python
from phone_agent.v2.capabilities import CapabilitySpec


def apply(ctx):
    def on_observe(payload):
        print(f"[my-plugin] {payload}")

    ctx.on("observe", on_observe)


CAPABILITY = CapabilitySpec(
    cap_id="my_plugin", title="My Plugin", mode="on", apply=apply
)
```

`ctx.on(...)` 会将监听器归属到当前 capability；正常 release 或 `apply`
失败时，装配层都会清理该监听器。

目录只需一个导出 `CAPABILITY` 的 `plugin.py`：

```
my-plugin/
└── plugin.py
```

## 安装

`plugin add` 默认把条目写入项目 `.taskwizard.toml`；`plugin list`、`remove`、`update`、`search` 管理清单与
本地/社区包：

```bash
python main_v2.py plugin add ./my-plugin
python main_v2.py plugin list
```

pip 包则声明 entry point 后被自动发现：

```toml
[project.entry-points."taskwizard.capabilities"]
my_plugin = "my_plugin.plugin:CAPABILITY"
```

!!! danger
    `plugin add` 即代码执行授权：该目录的 `plugin.py` 会在下次启动时执行，与 `pip install` 同级信任。只添加你审过源码的目录或包。

插件在**装配期加载一次**：CLI 与 runner 使用同一批授权入口，Web 空闲/启动只序列化配置、不执行插件代码；
实际 runner 启动后才 import/apply。插件不支持运行中热重载——内建能力同样是静态装配（无文件监视、无运行中
工具表重建），要变更需重启进程。`ctx.on_dispose(...)` 可注册 release 或失败清理回调，归属当前 capability。

### 授权与发现 {#plugin-authorization}

| 机制 | 内容 |
|---|---|
| entry points | pip 包声明 `taskwizard.capabilities` 组即被自动发现 |
| manifest | 用户级 `~/.taskwizard/profile.toml` 与项目级 `<repo>/.taskwizard.toml`（可用 `PHONE_AGENT_PLUGIN_MANIFEST` 覆盖路径）；同名条目项目级优先 |
| 清单写入 | `plugin add` 只做两件事：pip 安装，或在 manifest 里登记本地路径（不注入其它资源，包括应用词表） |
| 严格模式 | 默认 strict：已启用条目加载失败或 API 版本不匹配即报错；只有显式 `enabled=false` 才跳过 |
| 总开关 | `PHONE_AGENT_PLUGINS=false` 关闭全部外部插件，内建能力不受影响 |

`plugin add` 就是执行授权：登记或安装之后，该插件的 `plugin.py` 会在 runner 装配时执行，与 `pip install` 同级信任。Web 进程没有插件加载路径（也不执行插件代码）；决定是否激活的是 runner 装配阶段，每个 run 装配一次。需要给 App-KB 注入词表时走 `main_v2.py --learn-alias`，不经插件通道。

自定义 API builder 即使没有实现 context support，注册表声明的 `ModelSpec.context_window` 也会私有绑定到
构建结果，供主模型与备用模型最终准入使用。它不替换插件的 estimate/prepare/usage，也不会猜测未知
serializer 如何处理 `maxTokens`；没有窗口声明时保持 unknown。

## 事件 {#events}

观察型监听器 `fn(payload) -> None`；洋葱型 `fn(payload, next) -> result`，先注册者居外，不调 `next()` 即短路，内层短路的结果对外层照常可见。

| 事件 | 类型 | 返回契约 |
|---|---|---|
| `run/start` / `run/end` / `observe` / `model/post_request` / `agent/after` | emit | 忽略 |
| `tool/execute` | 洋葱 | `next(request)` 的结果，或返回 `REJECT` 短路（桥转成"已拦截"ToolMessage） |
| `model/pre_request` | 洋葱 | 返回变换后的完整消息列表；插件**不得**返回 `RemoveMessage`，也不要用 `jump_to` 做流程控制。桥接器对历史形态（含 `jump_to:"end"` 的 dict）保持宽容兼容，`JUMP_END` 是 core 熔断用的哨兵——兼容不等于推荐用法 |
| `model/request` | 洋葱 | `next(request)` 的结果（包模型调用：计时、观测） |

内建嵌套顺序（插件监听器装配期注册，居于 core 之内）：

```
tool/execute:       trace → diagnostic → admission → control_hitl → safety（最内）
model/pre_request:  compact → taskdoc → budget → diagnostic → model_limit
model/request:      trace → diagnostic
```

## 其他接缝 {#other-seams}

除监听器外，`apply(ctx)` 里还可以挂：工具（模型可见）、提示块（system 前缀/后缀/消息）、run hooks（开始/结束）、CLI 子命令。完整 API 见 `phone_agent/v2/capabilities.py::CapabilityAssemblyContext`。

## 装配契约 {#capability-mount}

`phone_agent/v2/capabilities.py` 是唯一装配器：十一个内建能力（providers、taskdoc、safety、budget、compact、finish_verify、deliverable、app_kb、dream、experience、recall）经五条接缝挂载——`register_middleware`、`register_tool`、`add_prompt_block`、`add_run_hook`、`add_cli_command`。内建策略全部是事件总线监听器，因此策略顺序由注册顺序决定。

- **core 监听器顺序**：`tool/execute` 为 trace → diagnostic → admission → control HITL → 能力链，safety 位于能力链最内。插件在能力链之后注册，因此插件的 `tool/execute` 监听器排在 safety 之内；
- **归属与释放**：`ctx.on` / `ctx.on_dispose` 把订阅与清理绑定到当前 capability。`release` 先反序跑清理回调，再摘除该能力注册的中间件、工具、提示块、run hooks 与 CLI 命令；正常模式变更在该 release 之后仍会 apply 新能力，只有在清理报错时才不再替换；
- **依赖状态**：能力的对外状态由依赖推导（off 优先；依赖为 off 或待定时为 pending；就绪且档位为 shadow 时为影子；否则生效）；
- **静态装配**：内建能力无文件监视、无运行中工具表重建，变更需重启进程；
- **provider bootstrap**：只支持声明 `providers` 与外部 helper，缺失、循环依赖或依赖 runtime 内建能力都在启动时可见失败，不做静默降级；
- **in-run 控制**：runner 唯一的运行中能力变更控制是 `revoke_lesson`——撤销 lesson 只影响后续投递，已经发送出去的上下文不可撤回；停止与 HITL 等既有控制照常存在。

## 可选模型上下文支持

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

上述方法是可选的。无支持对象的旧模型继续走原调用与通用估算；普通 `model_copy()` / `bind_tools()` 后，
公共 helpers 仍能找到模型的支持对象。不要把支持对象放进发给 SDK/浏览器的 `metadata` 或 `model_kwargs`。
`model_context_profile`、`estimate_model_input` 报告实际模型容量/输出 cap，以及计数是否覆盖 tools/images、
是否完整、来源；插件计数方法必须是纯本地操作，不能偷偷下载图片或调用计数 API。

可选 `prepare(model, messages, tools=())` 返回 `PreparedModelMessages(messages, model_kwargs)`；输入已复制。
没有提供此方法的旧模型/仅计数插件保持原消息与既有缓存字段等价，仅作深复制。有明确 prepare 方法时，
`prepare_model_messages` 才去掉前一协议缓存标记再调用它，并检查去掉缓存元数据后的消息语义与基线一致；
删除历史、修改工具参数/状态、抛异常等会退回合法基线。仅支持缓存元数据装饰，不允许在这个接缝里做语义
compact。最终原生参数仍由自定义模型 serializer 负责，公共层不按 provider 名拼报文。

准备阶段的调用参数默认只接受 `prompt_cache_options`、`prompt_cache_key`、`prompt_cache_retention`、
`cache_control` 和 `cached_content`，也可放在 `extra_body` 中。插件可用
`cache_parameter_names = ("vendor_cache",)` 声明自己的缓存参数名；未声明参数会拒绝。
声明不能把 `truncation`、历史/模型/工具字段、输出上限、采样或输出格式等语义参数重新归类为缓存，
包括嵌套 `extra_body` 和常见 camelCase 形式；违规准备退回基线，不能借缓存提示让服务器偷偷截历史。

可选 `protected_message_ids(model, messages)` 返回不可拆原生块所属消息 id；可选 `normalize_usage(message)`
返回 `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens` 四个非负整数或 `None`。
解释不了的字段保留 `None`，不制造零；不返回其他 raw 字段。原 `AIMessage.usage_metadata` 仍是预算的基础协议。

支持对象由模型持有，没有新增进程全局注册表。现有 `register_api_builder` 是旧的进程级接口，
`register_provider` 对已有 registry 的内部修改也不会自动成为 owner 挂载；插件仍须为这类资源安排
`ctx.on_dispose` 清理，避免按名字卸载时覆盖别的注册者。本文新增的私有模型绑定不改变旧注册 API 的签名、
override/unregister 语义或静态装配规则。原生 continuation/compact、服务端缓存资源、价格和金额预算没有
在这一接口中实现。

标准 text/image 块也可能在 `extras.signature` 等嵌套字段携带原生重放状态。公共
`phone_agent.v2.native_content` 的检测与 token 估计共用；签名/加密状态所属消息保持不可拆，
`__openai_function_call_ids__` 等已知 SDK bookkeeping 不会把普通工具组永久钉住。

## 打包分发

插件以 pip 包或本地目录分发；`plugin add` 只做 pip 安装或在 manifest 里登记本地路径（`cmd_add` 不见其他资源注入逻辑，
包括应用词表），需要注入 App-KB 时走 `main_v2.py --learn-alias`。社区仓库 `plugins/index.json` 暂未上架任何包。

!!! note
    `PLUGIN_API_VERSION = 1` 为 provisional：出现第二个真实实现方并完成验证前，事件与契约可能调整。
