# Agent Note: 插件平面 Round 1 —— 工具风险、mode、CLI、服务归属与 pin 前缀的声明面

Status: implemented

## Problem

五个各自独立的缺口，共同点是"没有声明时行为不可见"：

1. **安全真空**：`middleware/safety.py` 把参与判定的工具写死成 `ACTUATION_GATED_TOOLS` 四名，名单外一律
   `not_actuation` 放行。插件注册的工具天然在名单外——它不需要做错任何事，只要不出现，就绕过了安全分类器。
2. **mode 无校验**：`CapabilitySpec.mode` 当时只拒绝空字符串，任何其它值都按 active 挂载；而内建能力把
   `safety_mode=wary`、`finish_verify=auto`、`dream_mode=manual` 这类**运行档位**直接塞进同一个字段。
3. **CLI 接缝没接线**：`main_v2.py::_build_cli_capability_context` 装配时不纳入 `discover_external_specs`，
   插件的 `add_cli_command` 注册得到却永远调不到；`_maintenance_command` 另有一份与 CLI 声明平行的写死名单。
4. **服务可被顶替**：`register_service` 只记录能力 owner，harness 预置的 `event_bus`/`session`/`config` 与
   一批工厂都不在 owner 表里，插件写同名 key 可以静默替换掉运行期依赖。
5. **pin 前缀无归属**：`pins.py` 只有两个常量，没有"谁能 pin"的概念；任何代码都能造一个看起来像 pinned
   的 id，而折叠逻辑只认那两个内建前缀。

## Decision

把"声明面"补成与行为缝并列的一层，纪律是：**声明只影响是否被分类/记账/投影，绝不影响是否被允许**。

- **工具风险**：`register_tool(tool, *, risk="actuation"|"readonly"|None)`。声明入装配期
  `tool_risk_registry`（服务），safety 查找顺序 = 声明表 → **未声明按 `actuation`（fail-closed）**。
  内建工具在各自的 builder 里用 `tool_risk.declare_builtin_risks` 打 `metadata["risk"]` 戳，注册缝读回戳，
  所以内建走的是同一套 API，而风险表本身在 `phone_agent/v2/tool_risk.py`。
  内建 1:1 靠两条锁死：`BUILTIN_TOOL_RISKS` 的 `actuation` 集合 == 旧四名，且一个等价性测试对每个内建工具
  断言"带注册表分类 == 不带注册表分类"（gate / level / reason 三者全等）。
  同名工具在能力平面内重名即 raise；core 同名替换（finish 验收器）保留，替换声明压栈、release 后回到
  core 的声明。
- **mode**：合法值 {`off`,`shadow`,`on`}，其余装配期 raise；域内档位由 `_capability_mode` 翻译——`off`/`shadow`
  透传，其它非空值（`wary`/`auto`/`manual`…）翻译成 `on`，即旧 `status()` 的"非 off 即 active"语义，状态行不变。
- **CLI（G1）**：CLI 装配纳入 `discover_external_specs`；`_maintenance_command` 只从 `build_parser` 声明的
  维护 flag dests 取值，插件命令由 leftover argv 经注册的 `cli_handlers` 解析成 `--<name>[=VALUE]`。
  内建 handler 仍用最终 config 构建（`--device-id` 等覆盖照旧生效），插件命令拿到解析后的命名空间。
- **服务归属（G6）**：harness 发布的服务（构造期 services + `set_service`）登记 `HARNESS_OWNER`；能力
  `register_service`/`set_service` 覆盖 harness-owned key 即 raise，写自己名下的 key 自由增减，release 只回收
  自己的。规则对能力一视同仁（不区分内建与插件）。
- **pin 前缀（G4）**：`ctx.register_pin_prefix("__my_plugin__")` 认领，registry 提供
  `pin_id(prefix, suffix)`（未声明前缀 raise）；harness 两个前缀不许被认领，名字非法即 raise，release 回收。
  **消费端不动**：`compact.py` 的折叠保护本批仍只认 `__taskdoc__`/`__compact__`。

明确不做：不给声明面任何"放行"语义（没有 `safe=true` 之类开关）；不改安全分类规则本身；不改折叠/图片
清理等 harness 保留语义。

## Alternatives considered

- **删掉写死表、未声明按 `readonly` 处理**。最强的理由：改动最小、零行为漂移，插件沉默即保持现状。
  为何被否：这正是要补的真空——沉默等于绕开分类器，而插件工具（可能带 `target_description` 这类文本）
  比内建更需要判断；fail-closed 的代价只是多进一次分类器，不会误放行。
- **把域内档位收进 `CapabilitySpec`**（safety 自带 `wary/hard/reviewer`）。最强的理由：一个字段表达
  能力的真实状态，控制台不必再读 config。为何被否：`mode` 回答"挂不挂载"，域内档位回答"挂上后怎么跑"；
  合并会把 `shadow/on` 的派生状态与行为档位混成一个词，且 release/重建要重算 spec。翻译层保留两条语义。
- **给 argparse 动态注册插件 flag**（`parser.add_argument(f"--{name}")`）。最强的理由：插件命令能进
  `--help`、参数语义交给 argparse。为何被否：命令名只有 apply 之后才知道，而 flag 必须在 `parse_args`
  之前声明——要么在 CLI 路径把插件 apply 跑两遍，要么把内建 handler 对 config 的绑定推迟（`--device-id`
  之类的覆盖会跟着变）。改为 `parse_known_args` + 注册表解析 leftover，代价是插件命令不出现在 `--help`。
- **服务归属只约束插件**（按内建/外部 cap_id 分档）。最强的理由：测试里内建能力预置 `compact_instance`
  这种 stub 的场景可以不动。为何被否：要给装配层引入信任分层；生产路径上内建注册的服务名与 harness
  服务名本来不重叠，统一规则的收益（易审、无例外）大于测试改动成本。
- **pin 前缀写成 `CapabilitySpec.pin_prefixes` 字段**。最强的理由：与 `provides` 对称、状态表可见。
  为何被否：前缀是 apply 期资源（与 `register_service` 同层），放 spec 会强迫所有插件在注册表构造期声明，
  且 release/重建要重算 spec；本批也没有消费端需要提前知道前缀。

## Consequences

- **收益**：插件工具默认进分类器；五个声明点全部 fail-visible（重名工具、非法 risk、非法 mode、覆盖
  harness 服务、未声明前缀）；插件 CLI 命令第一次真正可调；内建风险集合变成可断言的数据（等价性测试
  锁住 1:1）；风险声明与 pin 前缀都能被控制台/测试读出（`ctx.service("tool_risk_registry")` /
  `pin_prefix_registry`）。
- **代价**：内建风险表从 safety.py 移到 `tool_risk.py`，并多一层"工具对象 metadata 戳"的间接（为了
  在测试替换 `phone_agent.v2.tools` 的环境下仍能自声明）；`hard` 档的中断表只覆盖**声明**为 `actuation`
  的工具，未声明的插件工具靠默认 `wary`/`reviewer` 分类器兜底，`hard` 档下不会中断——这是与改动前一致的
  行为，但仍是缺口，留给后续批次（需要装配期拿到全部工具名才能补）。
- **代价**：pin 前缀声明暂无消费端（折叠保护仍只看两个内建前缀），所以本批它只是"认证 + 铸 id 校验"，
  不等于插件块真的免折叠。
- **代价**：CLI 维护路径在需要时才装配（避免任务运行重复 apply 插件），插件命令不进 `--help`；插件
  apply 在 CLI 上下文里拿不到 device session（与内建 CLI 能力同一约束）。
- **代价**：文档预算上调一档——`AGENTS.md` 2100→2200（P0 #18 新增声明面红线 + Module Map 收编
  `tool_risk.py`），`pages/plugins.md` 2100→2600（新增四个声明点与插件 CLI 用法）；`pages/safety.md`
  在 1900 内吸收（工具覆盖改为"声明驱动 + 未声明 fail-closed"）。

## 交叉复审裁决

复审提出 6 条，coordinator 裁决：H1/H2.1/H3/H5 驳回，H2.4/H4/H6/H8 采纳并已随本批落地（工作区未提交）。

**驳回（各自的事实依据）**

- **H1「`scroll`/`swipe` 声明 `readonly` 是语义漂移」**：驳回。旧代码里 `ACTUATION_GATED_TOOLS` 是**唯一**
  gate 表（用于 `classify_tool_call`、预警监听、`build_hitl_middleware`、`build_safety_hard_hitl_listener`
  四处），`scroll`/`swipe`/`back`/`home` 从未被判定过；声明面只是把这张表搬到工具侧，逐字节等价，等价性测试
  已经证明每个内建工具的 gate/level/reason 三者不变。把它们改判为 `actuation` 反而是行为漂移。
- **H2.1「`declare_risk` 失败会静默」**：驳回。`declare_risk` 对非法 risk 抛 `ValueError`，对不可写
  `metadata` 的工具抛 `TypeError`，两条路径都不是静默；只有"未声明"是刻意的 fail-closed 默认。
- **H3「`tests/web` 失败由本批引入」**：驳回。3 个失败在干净 `HEAD` worktree 上同样存在，属 anthropic SDK
  环境漂移（与 2 个 `test_provider_context` 失败同源）。
- **H5「release 回收 pin 前缀/风险声明会丢状态」**：基本驳回。声明按 owner 压栈、release 后回落到被替换者
  （或回到未声明），这是与工具挂载一致的**设计语义**：能力撤销后它的声明不该继续生效。

**采纳（H2.4 / H4 / H6 / H8）**

1. **未声明工具的装配期可见性（H2.4）**：`assemble_capabilities` 收尾调用 `_report_undeclared_tools`——把
   当前注册表里"活跃声明为空"的工具名同时发到三处（都 fail-open，绝不让装配失败）：stderr 一行、session 的
   `resolution_trace_recorder`（生产路径写进 run trace）、事件总线上的新话题
   `capability/tools_undeclared`（payload `{"tools", "owners"}`）。**分类行为不变**（仍按 `actuation`
   fail-closed），列表每次装配重算，release 后不再点名。新事件在 `events.py` 保留集与 `pages/plugins.md`
   事件表登记。
2. **CLI `--` 分隔符（H4）**：`_split_argv` 显式切出分隔符之后的 token，重新拼回 `parse_known_args` 以保留
   argparse 的位置参数分配，并据此禁止把 leftover 当能力命令匹配（分隔符后的多余 token 一律按 argv 错误
   报出）。`-- ping` / `-- --ping` 现在是任务文本，`--dream -- X` 仍是"命令与任务不可混用"错误；不带分隔符
   的 `--ping-task` 依旧报错（原行为锁定）。
3. **mode 报错指引（H6）**：`CapabilitySpec.mode` 非法值现在在消息里点名合法值，并说明
   `wary`/`hard`/`auto`/`manual` 是配置侧 `*_mode` 档位、由 `_capability_mode` 翻译，不是合法 spec 值。
4. **补测试（H8）**：服务名 release 后可被下一个 owner reclaim；`hard` 档中断表不覆盖未声明工具（docstring
   注明是已知缺口，排期 Round 3，同时锁定 `wary` 预警流程仍然 fail-closed 覆盖它）；`--` 分隔符四条用例。

复审小修后 `pages/plugins.md` 仍在上调后的 2600 词预算内（2594），`AGENTS.md` 与 `pages/safety.md` 不变。
