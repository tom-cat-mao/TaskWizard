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

## 打包分发

插件以 pip 包或本地目录分发；`plugin add` 只做 pip 安装或在 manifest 里登记本地路径（`cmd_add` 不见其他资源注入逻辑，
包括应用词表），需要注入 App-KB 时走 `main_v2.py --learn-alias`。社区仓库 `plugins/index.json` 暂未上架任何包。

!!! note
    `PLUGIN_API_VERSION = 1` 为 provisional：出现第二个真实实现方并完成验证前，事件与契约可能调整。
