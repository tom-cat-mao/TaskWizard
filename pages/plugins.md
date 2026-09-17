# 插件开发

插件是一个导出 `CapabilitySpec` 的 Python 模块（例如 `my-plugin/plugin.py`），与内建能力走同一装配层。
`CapabilitySpec` 字段：`cap_id`、`title`、`mode`、`deps=()`、`apply=None`、`release=None`、
`provides=None`。

```python
from phone_agent.v2.capabilities import CapabilitySpec


def apply(ctx):
    def on_observe(payload):
        print(f"[my-plugin] {payload}")

    ctx.on("observe", on_observe)


def release(ctx):
    print("[my-plugin] released")


CAPABILITY = CapabilitySpec(
    cap_id="my_plugin",
    title="My Plugin",
    mode="on",
    deps=(),
    apply=apply,
    release=release,
)
```

`deps` 同时约束装配顺序与状态：依赖 off 或待定时本能力为 pending，装配时依赖先行。`provides` 与
`ctx.register_service(name, value)` 配套，声明了服务却没有真正注册会在装配期报错。`ctx.on(...)` 会把监听器
归属到当前 capability；正常 release 或 `apply` 失败时，装配层都会清理该监听器。

目录只需一个导出 `CAPABILITY` 的 `plugin.py`：

```
my-plugin/
└── plugin.py
```

## 安装

`plugin add` 默认把条目写入项目 `.taskwizard.toml`；`list`、`remove`、`update`、`search` 管理清单与本地/社区包：

```bash
python main_v2.py plugin add ./my-plugin
python main_v2.py plugin list
```

- `add`：本地路径以目录名登记 `path=`，不做 pip 安装；包名走 `pip install` 并以包名登记；
- `remove NAME`：包条目先 `pip uninstall -y` 再从清单删除，本地路径条目只删登记；
- `update NAME` 或 `update --all`：对已启用的包条目执行 `pip install -U`；
- `add` / `remove` 都接受 `--user` / `--project` 选择清单作用域（默认项目级）；
- `search [term]`：读 `PHONE_AGENT_PLUGIN_INDEX`（URL 或本地 json）。

pip 包则声明 entry point 后被自动发现，entry point 名必须等于清单条目名（`plugin add <包名>` 即以包名登记）：

```toml
[project.entry-points."taskwizard.capabilities"]
my_plugin = "my_plugin.plugin:CAPABILITY"
```

entry point 也可以直接指向一个 `CapabilitySpec` 对象，不必经过 `CAPABILITY` 属性。

!!! danger
    `plugin add` 即代码执行授权：该目录的 `plugin.py` 会在下次启动时执行，与 `pip install` 同级信任。只添加你审过源码的目录或包。

CLI 与 runner 使用同一批授权入口，runner 启动装配时 import/apply（Web 空闲/启动只序列化配置、不执行插件代码）。
`plugin list` 在装配之外还会把每个启用条目加载两次（一次探测装配状态、一次取 `cap_id`），路径插件每次加载
都重新 `exec_module`；其余子命令不加载插件模块。插件不支持运行中热重载——内建能力同样是静态装配（无文件
监视、无运行中工具表重建），要变更需重启进程。`ctx.on_dispose(...)` 可注册 release 或失败清理回调，归属当前
capability。

### 授权与发现 {#plugin-authorization}

| 机制 | 内容 |
|---|---|
| entry points | pip 包声明 `taskwizard.capabilities` 组即被自动发现 |
| manifest | 用户级 `~/.taskwizard/profile.toml` 与项目级 `<repo>/.taskwizard.toml`（可用 `PHONE_AGENT_PLUGIN_MANIFEST` 覆盖路径）；同名条目项目级优先 |
| 清单写入 | `plugin add` 只做两件事：pip 安装，或在 manifest 里登记本地路径（不注入其它资源，包括应用词表）；相对路径按项目清单目录解析 |
| 失败可见 | 没有宽松开关：已启用条目加载失败、API 版本不匹配或未提供 `CapabilitySpec` 一律抛错；只有显式 `enabled=false` 才跳过 |
| 总开关 | `PHONE_AGENT_PLUGINS=false` 关闭全部外部插件，内建能力不受影响 |

manifest 条目字段：`name`、`version`、`enabled`（默认 `true`）、`path`、`[plugin.config]` 键值表。读写往返支持
`[plugin.config]`，但 emitter 只接受 string/bool 值；加载路径不把 config 传给插件——`plugin.py` 当前拿不到
manifest 里配的值（已知限制）。

`REQUIRES_API` 是受限 PEP-440 子集：逗号组合 `>=`、`<=`、`==`、`=`、`>`、`<`、`!=`，裸版本号视为 `==`，
缺省或空字符串放行任意版本。模块级 `REQUIRES_API` 优先于 spec 对象上的同名属性；不满足即报错。

Web 进程没有插件加载路径，决定是否激活的是 runner 装配阶段，每个 run 装配一次；需要给 App-KB 注入词表时走
`main_v2.py --learn-alias`，不经插件通道。

## 事件 {#events}

观察型监听器 `fn(payload) -> None`；洋葱型 `fn(payload, next) -> result`，先注册者居外，不调 `next()` 即短路，内层短路的结果对外层照常可见。自定义事件名必须匹配 `^[a-z]+/[a-z_]+$`，内置事件常量属于保留集、始终合法；`emit` 监听器抛错只记日志、被吞掉，不阻断发事件方。各事件 payload 契约见 `phone_agent/v2/events.py` 模块 docstring。

| 事件 | 类型 | 返回契约 |
|---|---|---|
| `run/start` / `run/end` / `observe` / `app/launched` / `model/post_request` / `agent/after` | emit | 忽略 |
| `tool/pre_execute` | 洋葱 | 与 `tool/execute` 同形；已定义、保留并导出，当前没有生产代码 emit |
| `tool/execute` | 洋葱 | `next(request)` 的结果，或返回 `REJECT` 短路（桥转成"已拦截"ToolMessage） |
| `model/pre_request` | 洋葱 | 返回变换后的完整消息列表；插件**不得**返回 `RemoveMessage`，也不要用 `jump_to` 做流程控制。桥接器对历史形态（含 `jump_to:"end"` 的 dict）保持宽容兼容，`JUMP_END` 是 core 熔断用的哨兵——兼容不等于推荐用法 |
| `model/request` | 洋葱 | `next(request)` 的结果（包模型调用：计时、观测） |

`app/launched` payload 为 `{"package", "device_id", "source"}`：`source` 取 `launch_app`（设备确认的启动成功）或
`foreground`（已提交观测里前台包变化到尚未播报的包，系统包不播报），每 run 每包至多发一次。

内建嵌套顺序（插件监听器装配期注册，居于 core 之内）：

```
tool/execute:       trace → diagnostic → admission → control_hitl → budget → safety（最内）
model/pre_request:  compact → taskdoc → budget → procedure → diagnostic → model_limit
model/request:      trace → diagnostic → budget（最内）
```

budget 的 tool 闸以 `prepend=True` 注册，位于 safety 之外；`safety_mode=off` 时 `tool/execute` 没有 safety 节点；
`compact_enabled=false` 时 `model/pre_request` 最外层是一条只做图像/marks 清理的 prune-only 监听器（没有 compact
监听器）。`procedure` 是 recall 能力的预请求注入器，进场投递在 `app/launched` 时暂存。

## 其他接缝 {#other-seams}

除监听器外，`apply(ctx)` 里还可以挂：

- **工具**（`register_tool`，模型可见）：同名工具覆盖 baseline，release 后 baseline 回到原位；
- **提示块**（`add_prompt_block`）：只有 `system_suffix` 与 `system_message` 两种 placement，provider 可返回裸字符串（按 `system_message` 处理）；
- **run hooks**（`add_run_hook`，相位 `start`/`end`）：内建排序 start 为 taskdoc 10 → app_kb 20 → experience 35 → recall 40，end 为 experience 40 → recall 50 → dream 90，未列出的 owner 默认 50、同序按注册先后；
- **CLI 子命令**（`add_cli_command`）；
- **服务**（`register_service`）：进入共享服务命名空间、按 `cap_id` 记录 owner，release 时零残留；同名 key 被两个挂载能力注册即 fail-visible 冲突；
- `ctx.on(..., prepend=True)` 把监听器插到最外层，返回的 disposer 幂等可重复调用。

core 侧另有 `register_core_middleware` / `register_core_tool` / `add_core_run_hook`：同一有序集合、owner 为
`__core__`，能力 release 不会摘除它们。`register_middleware` 与 `MiddlewareReplacement` 是给第三方桥接中间件
预留的接缝（默认 order=50），当前没有内建调用方、没有测试；策略行为仍必须走事件总线。插件注册的进程级资源
（如 `register_api_builder` 的传输）同样不被 release 管理，需自行 `ctx.on_dispose` 清理。

跨模块共享的 pinned id 前缀从 `phone_agent/v2/pins.py` 导入（`TASKDOC_ID_PREFIX`、`COMPACT_ID_PREFIX`），不要
硬编码字符串。

provider 侧还有可选的模型上下文支持接缝：自定义 API builder 用 `bind_context_support` 挂 profile/estimate/
prepare/usage 支持对象，缓存参数白名单见[模型提供方与路由](providers.md#context-support)。

## 装配契约 {#capability-mount}

`phone_agent/v2/capabilities.py` 是唯一装配器：十一个内建能力（providers、taskdoc、safety、budget、compact、finish_verify、deliverable、app_kb、dream、experience、recall）经五条接缝挂载——`register_middleware`、`register_tool`、`add_prompt_block`、`add_run_hook`、`add_cli_command`；`register_service` 是第六接缝，把能力服务发布进 harness 服务命名空间。内建策略全部是事件总线监听器，因此策略顺序由注册顺序决定。

- **core 监听器顺序**：见上表；插件在能力链之后注册，因此插件的 `tool/execute` 监听器排在 safety 之内；
- **归属与释放**：`ctx.on` / `ctx.on_dispose` 把订阅与清理绑定到当前 capability。`release` 先反序跑清理回调，再摘除该能力注册的中间件、工具、提示块、run hooks、CLI 命令与服务；正常模式变更在该 release 之后仍会 apply 新能力，只有在清理报错时才不再替换<!-- allow:不再 -->；
- **依赖状态**：能力的对外状态由依赖推导（off 优先；依赖为 off 或待定时为 pending；就绪且档位为 shadow 时为影子；否则生效）；
- **静态装配**：内建能力无文件监视、无运行中工具表重建，变更需重启进程；
- **provider bootstrap**：只支持声明 `providers` 与外部 helper，缺失、循环依赖或依赖 runtime 内建能力都在启动时可见失败，不做静默降级；
- **in-run 控制**：runner 唯一的运行中能力变更控制是 `revoke_lesson`——撤销 lesson 只影响后续投递，已经发送出去的上下文不可撤回；停止与 HITL 等既有控制照常存在。

## 打包分发

`plugin add` 只做 pip 安装或登记 `path=`，不注入其它资源（包括应用词表）；社区仓库 `plugins/index.json`
暂未上架任何包。

!!! note
    `PLUGIN_API_VERSION = 1` 为 provisional：出现第二个真实实现方并完成验证前，事件与契约可能调整。
