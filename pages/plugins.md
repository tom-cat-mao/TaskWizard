# 插件开发

插件是一个导出 `CapabilitySpec` 的 Python 模块（例如 `my-plugin/plugin.py`），与内建能力走同一装配层。
`CapabilitySpec` 字段：`cap_id`、`title`、`mode`、`deps=()`、`before=()`、`after=()`、`apply=None`、
`release=None`、`provides=None`。`mode` 只有 `off` / `shadow` / `on` 三个合法值，其余字符串装配期报错
（内建把 `safety_mode`、`finish_verify`、`dream_mode` 这类域内档位翻译成 `on`，状态不变）。

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

`deps` 同时约束装配顺序与状态：依赖 off 或待定时本能力为 pending，装配时依赖先行。`before` / `after` 是
**纯排序提示**：`after=("compact",)` 表示本能力排在 `compact` 之后 apply（它的监听器因此注册在后者内层），
`before` 相反；装配期按注册顺序做一次确定性拓扑排序，提示互指成环、或与 `deps` 冲突、或指向未注册的能力名
都在装配期报错，指向已关闭能力则视为无约束（`pending` 语义仍只由 `deps` 决定）。`ctx.on(..., prepend=True)`
把单个监听器插到最外层，与排序提示并存。`provides` 与 `ctx.register_service(name, value)` 配套，声明了服务
却没有真正注册会在装配期报错。`ctx.on(...)` 会把监听器归属到当前 capability；正常 release 或 `apply` 失败
时，装配层都会清理该监听器。

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
都重新 `exec_module`；其余子命令不加载插件模块。插件不支持运行中热重载，内建能力同样是静态装配，要变更需
重启进程。`ctx.on_dispose(...)` 可注册 release 或失败清理回调，归属当前 capability。

### 授权与发现 {#plugin-authorization}

| 机制 | 内容 |
|---|---|
| entry points | pip 包声明 `taskwizard.capabilities` 组即被自动发现 |
| manifest | 用户级 `~/.taskwizard/profile.toml` 与项目级 `<repo>/.taskwizard.toml`（可用 `PHONE_AGENT_PLUGIN_MANIFEST` 覆盖路径）；同名条目项目级优先 |
| 清单写入 | `plugin add` 只做两件事：pip 安装，或在 manifest 里登记本地路径（不注入其它资源，包括应用词表）；相对路径按项目清单目录解析 |
| 失败可见 | 没有宽松开关：已启用条目加载失败、API 版本不匹配或未提供 `CapabilitySpec` 一律抛错；只有显式 `enabled=false` 才跳过 |
| 总开关 | `PHONE_AGENT_PLUGINS=false` 关闭全部外部插件，内建能力不受影响 |

manifest 条目字段：`name`、`version`、`enabled`（默认 `true`）、`path`、`[plugin.config]` 键值表。读写往返支持
`[plugin.config]`，但 emitter 只接受 string/bool 值。加载时该表随 spec 透传进插件：`apply(ctx)` 里用
`ctx.plugin_config()` 读取任意键，同时它是本插件**声明配置键**的覆盖层（见[其他接缝](#other-seams)）。

`REQUIRES_API` 是受限 PEP-440 子集：逗号组合 `>=`、`<=`、`==`、`=`、`>`、`<`、`!=`，裸版本号视为 `==`，
缺省或空字符串放行任意版本。模块级 `REQUIRES_API` 优先于 spec 对象上的同名属性；不满足即报错。

Web 进程没有插件加载路径，决定是否激活的是 runner 装配阶段，每个 run 装配一次；需要给 App-KB 注入词表时走
`main_v2.py --learn-alias`，不经插件通道。

## 事件 {#events}

观察型监听器 `fn(payload) -> None`；洋葱型 `fn(payload, next) -> result`，先注册者居外，不调 `next()` 即短路，内层短路的结果对外层照常可见。自定义事件名必须匹配 `^[a-z]+/[a-z_]+$`，内置事件常量属于保留集、始终合法；`emit` 监听器抛错只记日志、被吞掉，不阻断发事件方。各事件 payload 契约见 `phone_agent/v2/events.py` 模块 docstring。

| 事件 | 类型 | 返回契约 |
|---|---|---|
| `run/start` / `run/end` / `observe` / `app/launched` / `taskdoc/completed` / `model/post_request` / `agent/after` | emit | 忽略 |
| `capability/tools_undeclared` | emit | 忽略；未声明 `risk` 的工具装配期点名一次（另发 stderr/trace），payload 为 `{"tools", "owners"}`，分类行为不变 |
| `tool/pre_execute` | 洋葱 | 与 `tool/execute` 同形；已定义、保留并导出，当前没有生产代码 emit |
| `tool/execute` | 洋葱 | `next(request)` 的结果，或返回 `REJECT` 短路（桥转成"已拦截"ToolMessage） |
| `model/pre_request` | 洋葱 | 返回变换后的完整消息列表；插件**不得**返回 `RemoveMessage`，也不要用 `jump_to` 做流程控制。桥接器对历史形态（含 `jump_to:"end"` 的 dict）保持宽容兼容，`JUMP_END` 是 core 熔断用的哨兵——兼容不等于推荐用法 |
| `model/request` | 洋葱 | `next(request)` 的结果（包模型调用：计时、观测） |

`app/launched` payload 为 `{"package", "device_id", "source"}`：`source` 取 `launch_app`（设备确认的启动成功）或
`foreground`（已提交观测里前台包变化到尚未播报的包，系统包不播报），每 run 每包至多发一次。

`taskdoc/completed` payload 为 `{"item_ids", "screen_seq", "epoch"}`：一次提交的路线项
`in_progress → completed` 迁移即发一次，fail-open，不改变工具回执。

`observe` payload 为 `{"epoch", "screen_seq", "marks_count", "marks_failure_code", "screen_hash"}`：
`screen_hash` 是已提交观测帧截图的短 sha256，供监听器判断“画面是否实质变化”，事件本体不含图像或 base64。

内建嵌套顺序（插件监听器装配期注册，位于 safety 之内；`sibling_receipts` 装配后追加，是 `tool/execute` 最内层）：

```
tool/execute:       trace → diagnostic → admission → control_hitl → budget → safety → sibling_receipts（最内；纯呈现层，rejection / terminal skip 不运行）
model/pre_request:  compact → taskdoc → budget → boundary_compact → procedure → diagnostic → model_limit
model/request:      trace → diagnostic → budget（最内）
```

budget 的 tool 闸以 `prepend=True` 注册，位于 safety 之外；`safety_mode=off` 时 `tool/execute` 没有 safety 节点；
`compact_enabled=false` 时 `model/pre_request` 最外层是一条只做图像/marks 清理的 prune-only 监听器（没有 compact
监听器）。`procedure` 是 recall 能力的预请求注入器，进场投递在 `app/launched` 时暂存。

## 其他接缝 {#other-seams}

除监听器外，`apply(ctx)` 里还可以挂：

- **工具**（`register_tool(tool, risk=...)`，模型可见）：同名工具覆盖 baseline，release 后 baseline 回到原位；
  同一能力重名、或两个挂载能力抢同一名字都在装配期报错。`risk` 取 `actuation`（进安全分类器）或
  `readonly`（只读、不判定），省略时读工具自带的 `metadata["risk"]`（内建工具在 builder 里自声明）；
  **未声明按 `actuation` 分类（fail-closed）**，所以沉默换不到只读通道。装配结束时未声明的工具会被点名
  （stderr/trace/`capability/tools_undeclared`），提醒补声明；
- **提示块**（`add_prompt_block`）：只有 `system_suffix` 与 `system_message` 两种 placement，provider 可返回裸字符串（按 `system_message` 处理）；
- **run hooks**（`add_run_hook`，相位 `start`/`end`）：内建排序 start 为 taskdoc 10 → app_kb 20 → experience 35 → recall 40，end 为 experience 40 → recall 50 → dream 90，未列出的 owner 默认 50、同序按注册先后；
- **CLI 子命令**（`add_cli_command`）：能力在装配期注册名字与 handler，维护路径用 `--<name>`（可带
  `=VALUE`）调用，名字按 `-`/`_` 归一，handler 收到解析后的命名空间、返回值即退出码。内建命令
  （`--dream`、`--learn-alias` 等）走同一张注册表，多个命令同时给出即报错；`--` 之后的 token 一律算任务
  文本，不会被匹配成命令；
- **运行身份**（harness 发布的只读服务 `run_context`）：`ctx.service("run_context")` 返回一个 `RunContext`，
  属性 `run_id`、`actor_model`、`goal`、`started`，另有 `require_goal()`。服务随装配上下文一起发布，
  `run_id` 在任何 `apply` 里都可读；`actor_model` 在 actor 模型构建后填入（provider bootstrap 阶段挂载的
  能力读到的是 `""`）；`goal` 只在 run 启动那一刻填入、且早于任何 run hook。未启动时 `goal` 为 `""`、
  `started` 为 `False`，需要 goal 又不想自己判 `started` 的用 `require_goal()`，未启动直接报错。CLI
  维护路径没有 run，该服务不存在（`None`）；Web runner 与 headless 走同一个构造函数，发布方式一致；服务
  属 harness，能力不能覆盖；
- **服务**（`register_service`）：进入共享服务命名空间、按 `cap_id` 记录 owner，release 时零残留；同名 key 被两个挂载能力注册即 fail-visible 冲突；harness 发布的服务（`event_bus` / `session` / `config` 及各工厂）登记为 harness owner，能力（含插件）不能覆盖，只能写入自己名下的 key；
- **pin 前缀**（`register_pin_prefix`）：pinned 消息块的前缀从 `phone_agent/v2/pins.py` 取；声明后可用
  `pin_prefix_registry` 服务的 `pin_id(prefix, suffix)` 铸 id，未声明前缀会报错，harness 的
  `__taskdoc__` / `__compact__` 不许被能力认领；
- **配置键**（`register_setting(key, env_var=..., default=..., description=...)`）：声明本能力的配置键并直接拿到
  解析值。`env_var` 必须以 `PHONE_AGENT_` 开头（`.env` 只加载该前缀），默认值只支持 str/bool/int/float/None；
  解析顺序与内建键同链——harness CLI 覆盖（`setting_overrides` 服务）> shell 环境变量 / `.env` > 本插件 manifest
  `[plugin.config]` 同名值 > 声明的默认值。结果写进 `V2Config.plugin_settings` 只读映射、release 时随能力撤销；
  第二个能力声明同一键即报错，取值读不成声明类型（如 int 键写成 `abc`）也装配期报错，不静默回落默认值；
- **记账角色**（`register_usage_role(role, unit="tokens"|"calls")`）：harness 预注册
  `actor`/`compact`/`verifier`/`reviewer`/`distill`（均为 token），插件再加自己的角色。`tokens` 角色进 token
  预算裁决；`calls` 角色按调用次数记账（`UsageLedger.calls_by_role()` / `calls_total`）、**不进** token 总额，
  因此预算裁决点仍归 harness。未声明角色调 `record()` 仍报错；两能力抢同名角色、非法单位均装配期报错；
- **脱敏字面量**（`register_redaction(literal)`）：把本能力自己的敏感子串交给 v2 出口脱敏（trace / 诊断证据流 /
  Web 事件 / 流式预览共用的 `redact_text`），命中替换为 `<redacted>`。字面量不是正则，必须单行、至少 4 字符
  （更短会误伤无关文本）；只改脱敏——安全分类与提示词侧清洗仍用内建 pattern；release 时撤销，同一字面量可被
  多个能力共同持有；
- `ctx.on(..., prepend=True)` 把监听器插到最外层，返回的 disposer 幂等可重复调用。

core 侧另有 `register_core_middleware` / `register_core_tool` / `add_core_run_hook`：同一有序集合、owner 为
`__core__`，能力 release 不会摘除它们。`register_middleware` 与 `MiddlewareReplacement` 是给第三方桥接中间件
预留的接缝（默认 order=50），当前没有内建调用方、没有测试；策略行为仍必须走事件总线。插件注册的进程级资源
（如 `register_api_builder` 的传输）同样不被 release 管理，需自行 `ctx.on_dispose` 清理。

跨模块共享的 pinned id 前缀从 `phone_agent/v2/pins.py` 导入（`TASKDOC_ID_PREFIX`、`COMPACT_ID_PREFIX`），不要
硬编码字符串；插件要自己的 pinned 块时先 `ctx.register_pin_prefix("__my_plugin__")` 认领（形状
`__小写标识符__`），再经 `pin_prefix_registry.pin_id(...)` 铸 id。当前折叠保护仍只认两个内建前缀。

provider 侧还有可选的模型上下文支持接缝：自定义 API builder 用 `bind_context_support` 挂 profile/estimate/
prepare/usage 支持对象，缓存参数白名单见[模型提供方与路由](providers.md#context-support)。

## 装配契约 {#capability-mount}

`phone_agent/v2/capabilities.py` 是唯一装配器：十三个内建能力（providers、taskdoc、safety、budget、compact、boundary_compact、finish_verify、deliverable、app_kb、dream、experience、recall、obs_archive）经五条接缝挂载——`register_middleware`、`register_tool`、`add_prompt_block`、`add_run_hook`、`add_cli_command`；`register_service` 是第六接缝，把能力服务发布进 harness 服务命名空间。内建策略全部是事件总线监听器，因此策略顺序由注册顺序决定。

除挂载外，装配上下文还是**声明面**：工具的 `risk`、pin 前缀、能力 `mode`、配置键、记账角色与脱敏字面量都在
装配期登记进对应注册表（`tool_risk_registry` / `pin_prefix_registry` / `setting_registry` /
`usage_role_registry` / `redaction_registry`，均可经 `ctx.service(...)` 读取），缺失声明按安全默认值处理
（工具算 `actuation`）。重复声明、非法值、覆盖 harness 服务都在装配期报错。声明只决定“是否被分类 / 记账 /
脱敏”，不决定“是否被允许”，也不提供任何放行开关。

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
