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

## 事件

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

## 其他接缝

除监听器外，`apply(ctx)` 里还可以挂：工具（模型可见）、提示块（system 前缀/后缀/消息）、run hooks（开始/结束）、CLI 子命令。完整 API 见 `phone_agent/v2/capabilities.py::CapabilityAssemblyContext`。

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
`prepare_model_messages` 会去掉前一协议缓存标记再调用它，并检查去掉缓存元数据后的消息语义与基线一致；
删除历史、修改工具参数/状态、抛异常等会退回合法基线。仅支持缓存元数据装饰，不允许在这个接缝里做语义
compact。最终原生参数仍由自定义模型 serializer 负责，公共层不按 provider 名拼报文。

可选 `protected_message_ids(model, messages)` 返回不可拆原生块所属消息 id；可选 `normalize_usage(message)`
返回 `input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens` 四个非负整数或 `None`。
解释不了的字段保留 `None`，不制造零；不返回其他 raw 字段。原 `AIMessage.usage_metadata` 仍是预算的基础协议。

支持对象由模型持有，没有新增进程全局注册表。现有 `register_api_builder` 是旧的进程级接口，
`register_provider` 对已有 registry 的内部修改也不会自动成为 owner 挂载；插件仍须为这类资源安排
`ctx.on_dispose` 清理，避免按名字卸载时覆盖别的注册者。本文新增的私有模型绑定不改变旧注册 API 的签名、
override/unregister 语义或静态装配规则。原生 continuation/compact、服务端缓存资源、价格和金额预算没有
在这一接口中实现。

## 打包分发

插件以 pip 包或本地目录分发；`plugin add` 只做 pip 安装或在 manifest 里登记本地路径（`cmd_add` 不见其他资源注入逻辑，
包括应用词表），需要注入 App-KB 时走 `main_v2.py --learn-alias`。社区仓库 `plugins/index.json` 暂未上架任何包。

!!! note
    `PLUGIN_API_VERSION = 1` 为 provisional：出现第二个真实实现方并完成验证前，事件与契约可能调整。
