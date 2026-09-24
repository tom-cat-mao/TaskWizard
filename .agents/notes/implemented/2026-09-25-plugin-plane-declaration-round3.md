# Agent Note: 插件平面 Round 3 —— 运行身份服务、挂载顺序提示与 hard 档中断表收口

Status: implemented

## Problem

Round 1/2 把声明面铺到了"工具风险 / pin 前缀 / mode / 配置键 / 记账角色 / 脱敏字面量"，剩下的三个缺口都是
**运行期**的，声明面本身不再够用：

1. **run 身份没有公开来源**。插件想拿到"我在为哪个 run 服务"，只能去碰 core 私有工厂：`compact` 的
   `_compact_memory_state`、`deliverable` 的 run-id 闭包、`obs_archive_factory`（`agent.py` 里那批
   `self._xxx` 闭包）都是 `ThinPhoneAgent` 的私有产物，随实现改名/搬家而失效。`run_id` 在 `__init__` 就
   定了、`actor_model` 在装配中段定型、`goal` 只在 `run()` 才存在——三者的可用时刻完全不同，谁都没有
   把它整理成一个契约。
2. **挂载顺序只能靠"注册顺序 + deps 副作用"**。监听器在 `apply` 里注册，所以**谁先 apply 谁在外层**：
   `boundary_compact` 必须在 `compact` 之后 apply（它要 `compact_instance` 才挂得上），今天靠
   `deps=("compact",)` 顺带保证。但 `deps` 同时表达门控（依赖 off ⇒ pending），一个只想"排在某能力之后"
   而不想被它的开关带走的插件无话可说；顺序是隐式事实，装配期不做任何校验。
3. **`hard` 档的中断表与分类器不一致**（Round 1 已知缺口）。`classify_tool_call` 对未声明工具 fail-closed
   按 `actuation` 判定，而 `hard` 的中断表只列**已声明** `actuation` 的名字：安全能力是第 3 个 apply 的，
   插件工具挂在最后，所以"沉默的插件工具"在 `hard` 档连中断表都进不去——同一个调用在 `wary` 档会被拦，
   在 `hard` 档反而畅通，这是纯粹的 fail-open。

## Decision

三件事，纪律不变：**声明只影响分类/记账/脱敏/顺序，绝不决定是否被允许**。

- **运行身份（G2）**：新类型 `RunContext`（`phone_agent/v2/capabilities.py`）+ harness-owned 服务键
  `run_context`（常量 `RUN_CONTEXT_SERVICE`）。服务随 `CapabilityAssemblyContext` 一起发布（即**早于
  provider bootstrap 那一趟装配**），`run()` 在**任何 run hook/监听器之前** `_begin(task)`。字段可用性与
  生命周期：
  - `run_id` 从第一次 `apply` 起就可读；`actor_model` 在 actor 模型构建后由 harness 填入（bootstrap 阶段
    读到的是 `""`），未解析同样是 `""`，不编造；
  - `goal` 只属于"已经开始的那次 run"：装配到 `run()` 之间是 `""`、`started=False`；
  - **消费端可见的失败**：需要 goal 又不想自己判 `started` 的用 `require_goal()`，未启动直接
    `RuntimeError`；CLI 维护路径（`main_v2.py::_build_cli_capability_context`）压根没有 run，服务**不存在**
    （`ctx.service(...) is None`），不发布一个假身份。
  - 发布点只有 `ThinPhoneAgent.__init__` 一处，`runner.py`（Web）与 `main_v2.py`（headless run）都经该
    构造函数，因此两条路径天然一致。
  - 明确不做：**不迁移** compact/deliverable 的私有工厂（留给后续 dogfood 批次），本批只让服务可用。
- **挂载顺序提示（G3）**：`CapabilitySpec.before=()` / `after=()`，只影响 apply 顺序（因而也影响监听器注册
  顺序），**不影响门控**。装配期 `_mount_order` 做一次确定性拓扑排序：边来自 `deps`（门控顺带的顺序）与
  `before`/`after`，同层按注册顺序（无提示者保持原位）。fail-visible 两条：提示指向**未注册**的能力名、
  或与其他提示/`deps` 构成**环**，都在装配期 raise；指向"已注册但未挂载（off/pending）"的能力只是无约束
  （跳过该边），而**不挂载的能力本身不校验提示**——把出问题的能力关掉必须能让整批装配重新跑起来。
  真实落点选了唯一一处真依赖：`boundary_compact` 在 `deps=("compact",)` 之外补 `after=("compact",)`，
  把"监听器挂在 compact 内层"从注册顺序的副产品变成被解析、被校验的声明。既有 `prepend=True` 一律不动，
  两条惯例并存（`prepend` 决定单个监听器在最外层，提示决定两个能力谁先 apply）。
- **hard 档中断表收口**：中断集合 = **除显式 `readonly` 外的一切已注册工具**（未声明同样中断，
  fail-closed 与分类器一致）。实现为 `ActuationInterruptTable`（`Mapping` 活视图），成员判定**每次查表时
  重新读 `tool_risk_registry`**——安全能力先于插件工具装配，任何构建期快照都盖不住后注册的工具。`risks=None`
  的直接调用方（无注册表）保持旧行为：`LEGACY_ACTUATION_GATED_TOOLS` 四个内建执行工具。内置 1:1 不变：所有
  内建工具都显式声明，表就是旧四名。

## Alternatives considered

- **run 身份只在 `run()` 开始时发布**。最强的理由：那时三个字段全都真实存在，服务内不存在"半空"状态。
  为何被否：`run_id` 在装配上下文构造时就定了，而插件最需要它的场合之一正是 `apply`（给产物命名、建目录、
  绑定 trace）；只允许运行期读会把插件推回私有工厂，等于本批目的落空。代价用一个显式 `started` 标志 +
  `require_goal()` 兜住。
- **在主装配处（provider bootstrap 之后）一次性发布完整对象**。最强的理由：`run_id` 与 `actor_model` 都已
  定型，服务不会出现"有 id 没模型"的中间态。为何被否：依赖 `providers` 的插件由 provider bootstrap 那一趟
  装配挂载，而主装配因为它"已挂载且档位未变"不会重跑——这些插件的 `apply` 会读到 `None`（= "没有 run"），
  对真实存在的 run 说了假话。改为随上下文发布 + actor 模型构建后补填 `actor_model`，代价是多一个
  harness-only 填值点（`_set_actor_model`）与文档里一句"bootstrap 阶段该字段为空"。
- **把 goal 做成"未启动即 raise 的属性"**。最强的理由：消灭一切误读可能。为何被否：只想要 `run_id` 的
  消费者（多数）会连服务都读不了；把失败精确放到需要 goal 的那一刻（`require_goal`）比一刀切更可用。
- **`RunContext` 不可变、`run()` 时整对象替换**。最强的理由：没有可变句柄，插件改不动。为何被否：监听器
  多数在 `apply` 里注册并顺手捕获 `ctx.service("run_context")`，替换对象会让它们永久持有旧的空壳；可变
  句柄（属性只读 + 私有 `_begin`）是这里唯一的活视图。
- **三个字段拆成三个服务键**。最强的理由：字段可用时刻不同，各发各的。为何被否：三个键 = 三份 owner
  登记、三种缺失语义，消费端要判三次 None；一个键 + `started` 只说一次。
- **用 `deps` 独占顺序、不引入提示**。最强的理由：现有机制已经保证 `boundary_compact` 在 `compact` 之后，
  零改动零风险。为何被否：`deps` 把"门控"和"顺序"焊死，插件无法只声明顺序；且顺序是隐式的，谁都不知道
  哪对能力有顺序要求，装配期也不校验。提示补的正是这块，`deps` 语义一字不动。
- **把 `boundary_compact` 的 `deps` 换成 `after`**。最强的理由：一处声明只表达一件事，最干净。为何被否：
  `deps` 还决定状态行——`compact` 关掉时该能力应为 `pending`（它确实没有可折叠的实例）；换成 `after` 后
  状态显示 `active` 而实际 inert，是状态面对行为撒谎。
- **绝对序号（`order=120`）而不是相对提示**。最强的理由：实现最省（与 middleware 的 order 一致）。
  为何被否：序号是全局共享的隐式坐标，插件之间无法协商，插在中间必然要重排一批数字；相对提示是局部的，
  与谁相邻由名字说清。
- **把提示实现为事件总线上的重排（给监听器排序）**。最强的理由：直接命中"顺序"这件事本身。为何被否：
  洋葱顺序由注册顺序 + `prepend` 决定，是既有约定；给监听器做全局排序要改动全部注册路径，且无法表达
  "compact 用 prepend 而 boundary 不用"这种混合意图。排序落在 apply 层，两条惯例都能保留。
- **hard 档构建期做一次快照（不引入活视图）**。最强的理由：`interrupt_on` 保持普通 dict，测试/序列化零
  改动。为何被否：安全能力 apply 时插件工具还没注册，快照在真实装配里永远缺这批名字——正是要修的洞。
- **给 `ControlHitlListener` 加"默认中断"分支，`interrupt_on` 只留控制项**。最强的理由：不必引入
  `Mapping` 子类。为何被否：`interrupt_on` 是明确的兼容形状（Round 1 的测试与外部调用方都在读它），
  把执行工具从表里搬走等于改契约；活视图让表内容仍可枚举、可 `set(...)`。
- **未声明工具在 `hard` 档一律按 `readonly` 放行（维持现状）**。最强的理由：内建行为零变化。为何被否：
  与 `wary`/`reviewer` 已经执行的 fail-closed 判定矛盾，同一个调用换档位就换结论，等于给"沉默"发免死
  金牌。

## Consequences

- **收益**：插件不再需要 core 私有工厂就能读到 run 身份，且 Web/headless 由同一构造函数保证一致；
  顺序依赖第一次成为**被声明、被解析、被校验**的事实（未知名字与成环都会挡在装配期）；`hard` 档不再有
  "沉默即绕过"的通道，未声明工具与声明 `actuation` 的工具在三个档位下结论一致。三项都有新测试：
  `tests/v2/harness/test_run_context.py`（生命周期、harness-owned、真实 agent 装配与 `run()` 前后）、
  `tests/v2/providers/test_plugin_mount_order.py`（排序解析、环、未知名字、off 目标、真实 boundary_compact
  对）、`tests/v2/providers/test_plugin_declaration_plane.py`（活的 hard 中断表 + 内建 1:1）。
- **代价**：`interrupt_on` 在 `hard` 档不再是普通 dict，而是每次查表都读注册表的活视图——`dict(...)` 只能是
  一次快照，判定路径每次多两次注册表读取（名字量级很小，可忽略）；`RunContext` 的属性只读是约定而非强制
  （`_goal` 仍可被外部代码改写，属插件自担）；`boundary_compact` 的 `after` 与 `deps` 今天指向同一条边，
  提示在当前组合里是**冗余的显式声明**（它的独立价值由"剥掉 deps 后仍能排序"的测试锁定），去掉 `deps`
  会改变状态语义，所以两条都留。
- **代价**：`ctx._capability_order` 现在只含本次真正挂载的能力（此前含全部 spec），绝对值会变、相对顺序
  不变；`_mount` 用它给工具/提示块/CLI 排序，能力之间以及"能力 vs core"的相对位置都未变。
- **代价**：文档预算再上调一档——`pages/plugins.md` 3100→3400（运行身份服务与 `before`/`after` 提示的
  用法与失败面），`pages/safety.md` 1900→2000（hard 档中断表口径）。根 `AGENTS.md`（2190）与
  `phone_agent/v2/AGENTS.md`（745）在原预算内吸收声明面清单的措辞变化；`README.md` 的"插件系统"一行按本批
  范围未动（见下）。
- **遗留**：`README.md` 能力表里"插件系统"一行仍只列到"工具风险/CLI/服务/配置键/记账/脱敏声明"（`含…`，
  非穷举），挂载顺序与运行身份未写进去；该文件只剩 3 词余量，写进去要么削减他处要么上调预算——留给
  coordinator 决定。compact/deliverable 的私有工厂迁移（本批只发布服务）与"按提示排队的插件示例"同属
  Round 5 的 dogfood 面。
