# 插件开发

插件是一个导出 `CapabilitySpec` 的 Python 模块，与内建能力走同一装配层。

```python
# my-plugin/plugin.py
from phone_agent.v2.capabilities import CapabilitySpec


def apply(ctx):
    bus = ctx.service("event_bus")

    def on_observe(payload):
        print(f"[my-plugin] {payload}")

    # 存到 {cap_id}_event_disposers：release 时装配层自动调用，卸载零残留
    ctx.register_service("my_plugin_event_disposers", [bus.on("observe", on_observe)])


spec = CapabilitySpec(cap_id="my_plugin", title="My Plugin", mode="on", apply=apply)
```

```
my-plugin/
├── plugin.toml    # [plugin] id / title / api_version
└── plugin.py      # 导出 spec
```

## 安装

```bash
python main_v2.py plugin add ./my-plugin   # 写入 ~/.config/taskwizard/profile.toml
python main_v2.py plugin list              # 列出 / remove / update / search
```

pip 包则声明 entry point 后被自动发现：

```toml
[project.entry-points."taskwizard.capabilities"]
my_plugin = "my_plugin.plugin:spec"
```

!!! danger
    `plugin add` 即代码执行授权：该目录的 `plugin.py` 会在下次启动时执行，与 `pip install` 同级信任。只添加你审过源码的目录或包。

## 事件

观察型监听器 `fn(payload) -> None`；洋葱型 `fn(payload, next) -> result`，先注册者居外，不调 `next()` 即短路，内层短路的结果对外层照常可见。

| 事件 | 类型 | 返回契约 |
|---|---|---|
| `run/start` / `run/end` / `observe` / `model/post_request` / `agent/after` | emit | 忽略 |
| `tool/execute` | 洋葱 | `next(request)` 的结果，或返回 `REJECT` 短路（桥转成"已拦截"ToolMessage） |
| `model/pre_request` | 洋葱 | 消息列表（可变换）/ `JUMP_END` 哨兵（熔断跳结束）/ 含 `jump_to:"end"` 或 `messages` 的 dict；其他形态按 messages 透传 |
| `model/request` | 洋葱 | `next(request)` 的结果（包模型调用：计时、观测） |

内建嵌套顺序（插件监听器装配期注册，居于 core 之内）：

```
tool/execute:       trace → diagnostic → control_hitl → safety（最内）
model/pre_request:  compact → taskdoc → budget → diagnostic → model_limit
model/request:      trace → diagnostic
```

## 其他接缝

除监听器外，`apply(ctx)` 里还可以挂：工具（模型可见）、提示块（system 前缀/后缀/消息）、run hooks（开始/结束）、CLI 子命令。完整 API 见 `phone_agent/v2/capabilities.py::CapabilityAssemblyContext`。

## 打包分发

插件目录可附带 `apps.toml` 应用词表，`plugin add` 时注入 App-KB 为 `user` 别名。社区仓库 `plugins/index.json` 暂未上架任何包。

!!! note
    `PLUGIN_API_VERSION = 1` 为 provisional：出现第二个真实实现方并完成验证前，事件与契约可能调整。
