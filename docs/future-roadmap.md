# Future Roadmap

> 本文档记录 roadmap 的阶段进展与未来方向。硬性执行约束见仓库根 `AGENTS.md`（P0 表）；v2 模块契约以 `phone_agent/v2/` docstring 为准（重构规格文档属工作稿，不入库）；历史批次执行任务书见 `docs/archive/`。
>
> **最新状态（2026-08-30）**：经验数据面（WP-I）、RAG shadow 回想（WP-I2）、隐式纠正（WP-I3）、能力注册表（WP-J）、观测加固（WP-O）、compact 记忆状态与缓存计量（WP-M）、经验提炼+晋升（WP-L）、受控经验回注（WP-A3，自进化闭环完成）、runner 子进程化（WP-R）、能力挂载层（WP-C2）、**插件系统全量（WP-PLUGIN A–E：服务容器 + 事件总线 + manifest/CLI/发现 + 钉板契约 + 全部策略件迁为监听器，LangChain 栈只剋桥接器）**均已落地。通用长期记忆与经验注入已实现。剩余方向：prefix-cache 优化（评审中）、user 别名写入入口（规划中）、插件 API 真实插件验证（S3）、workflow 记忆与模型路由（远期）。

---

## Thin-Loop v2（当前架构）

> 由三个并行 worktree（core / tools / middleware）按重构规格实施并合并验收。

- **v1 退役**：LangGraph goal→plan→execute→reflect→acceptance 节点图（`phone_agent/graph/`、`phone_agent/agent.py`、`phone_agent/actions/`、`phone_agent/checkpoint/`、旧 `main.py`、`evals/`）已删除。保留并作为库复用的是 `phone_agent/adb`、`device_factory.py`、`phone_agent/grounding`、`phone_agent/config/{policy,app_registry,apps,i18n,timing,redact}.py`。
- **薄 loop 已落地**：LLM 每步一次调用，经 LangChain `create_agent` 的 tool-calling loop 感知/操作设备；harness 只做工具供给、安全边界、context 卫生与可观测，不做工作流路由。入口改为 `main_v2.py`（task 为位置参数）。
  - **工具层**（`phone_agent/v2/tools/`）：actuation（`tap`/`long_press`/`type_text`/`scroll`/`swipe`/`back`/`home`/`launch_app`/`wait`）、perception（`read_screen`/`locate`）、control（`finish`/`ask_user`/`take_over`）。执行类工具成功后自动附带 `[OBS]` 观测块。工具返回 `str`，失败返回错误文本，fail-closed。
  - **grounding**：marks-first；`tap` 双寻址 `target_mark_id` | `target_description`（解析为唯一 mark，歧义/无匹配 fail-closed 不执行）。坐标 0-1000 → 绝对像素只在工具内（`v2/coords.py`）。
  - **事件总线与桥接器（WP-PLUGIN E 终态）**：策略行为全部是 `v2/events.py` 总线上的监听器；LangChain 栈只剋 5 个桥接器（`tool/execute`、`model/pre_request`、`model/request`、`model/post_request`、`agent/after`）加可选 `extra_middleware`。嵌套顺序 = 注册顺序（core 先注册居外）：`tool/execute` 上 trace → diagnostic → control_hitl → safety（最内）；`model/pre_request` 上 compact（prepend；方法体内先调 `context_pruner` 再折叠）→ taskdoc → budget → diagnostic → model_limit；熔断由监听器返回 `JUMP_END`、桥转成 `jump_to:"end"`。各策略语义见 AGENTS.md P0 #3/#4/#13/#14。
  - **Agent 装配**（`phone_agent/v2/agent.py`）：`ThinPhoneAgent(config, checkpointer=None, extra_middleware=None)` 默认 `MemorySaver`；`extra_middleware` 是可选观察扩展点（内建层之后、安全预警层之前），省略时保持纯 headless；`run(task, hitl_handler=input)` 返回 `RunResult(success, reason, steps, trace_path)`；结束条件为 `session.finished` | `session.takeover_reason` | 模型无 tool_call | token 预算耗尽（`token_budget_exhausted`）| 死循环保险丝（`loop_fuse`）。安全裁决居 `tool/execute` 洋葱最内层，外层 trace/diagnostic 仍能记录被拦调用的结果。
  - **配置**（`phone_agent/v2/config.py`）：`V2Config` 三级解析 CLI > shell env > `.env` > 默认；`PHONE_AGENT_VERIFIER_MODEL` / `PHONE_AGENT_SAFETY_REVIEWER_MODEL` / `PHONE_AGENT_MEMORY_MODEL`（A4 起 auto-compact 摘要 writer，缺省回落主模型）均已启用。
  - **finish 兜底（S2 已落地）**：`finish` 两段式复核（第一次给世界镜像复核包不落定，`finish(confirm=true)` 才定稿，seq 守卫防陈旧）；`completed` 项强制 `evidence_note`。独立 context **finish verifier**（`phone_agent/v2/verify.py`）在高风险目标（goal 命中 policy 敏感词）/ 硬矛盾坚持 confirm / `FINISH_VERIFY=always` 时触发：只喂权威目标 + 带证据路线 + 尾帧截图，**不喂 actor 自辩**；REJECT 带内回传，累计 2 次转 `take_over`（L2→L3）。与安全门相反，verifier 故障 **fail-open**（L1 两段式已是有效兜底，不因验收器抖动卡死正确完成）。三档 `PHONE_AGENT_FINISH_VERIFY=off|auto|always`（默认 `auto`）。
- **TaskDoc 任务板已落地**（增量一）：goal 与 plan 合为同一文档的两个段落（目标 / 路线 / 关键事实），形态 = state（`PhoneSession.task_doc`）+ 工具写入（`update_task_doc` 是模型唯一写入者）+ `before_model` 渲染钩子。
  - **播种与写入边界**：run 启动时 harness 播种 `TaskDoc(goal_base=task)`；`goal_base` 只有 harness 能写，模型经 `update_task_doc` 全量替换 items / 追加 amendments / 替换 facts，validate 失败不写入。
  - **状态迁移纪律**（A4）：`validate(previous=...)` 对照写入前任务板判定——拒绝把 `pending` 直接标 `completed`（须先 `in_progress`，证明确实做过该步），拒绝单次调用批量把多项 `pending` 标 `completed`（补标绕过 finish 门）。新增项直接 `completed` 不算跳变（仍受结构化 `evidence_note` 门约束）。
  - **渲染钩子 + 流程线**（`phone_agent/v2/middleware/taskdoc.py`）：`TaskDocMiddleware.before_model` 在 messages 尾部注入 `[TASK_DOC]\n<render>` system 消息（pinned，压缩免疫，每轮移除旧块重贴新块）；空文档不注入。块尾附一条**流程线**（U3）：从 transcript 的 `AIMessage.tool_calls`（含 U3 输出契约的 `intent`/`note`）+ 匹配 `ToolMessage` 回执**纯派生**，渲染 `#N <intent> → <tool><target> → <result>`（最近 8 条），intent 缺失记"（未声明）"，无需任何新 session 字段。
  - **输出契约（intent 入 args，U3）**：所有工具签名加 `intent: str`（本步意图，system prompt 要求必填）+ `note: str | None`（本步发现）；执行类工具回执写实际执行内容（如 `已点击「上海」(ax_3)`），供流程线派生真实"账"。**删除停滞轻推**：旧 `session.seen_states`/`session.nudged`/`PHONE_AGENT_TASKDOC_NUDGE_STEPS` 检测机制已删除（`observe()` 仍算 `screen_hash` 仅供审计/screen binding）；持续可见的流程线是比一次性 nudge 更强、且不依赖启发式的非指导性防打转信号。`taskdoc_nudge_steps` 配置字段保留但标注废弃、构造 kwarg 变 no-op。
  - **finish 守卫**（`phone_agent/v2/tools/control.py`）：evidence 校验保留；新增 `task_doc.has_open_items()` 为真时拒绝 finish 并列出未完成项。
  - **配置**：`PHONE_AGENT_TASKDOC`（默认 true，`0/false/no/off` 关闭时不注册中间件不渲染）、`PHONE_AGENT_TASKDOC_NUDGE_STEPS`（**已废弃/no-op**，U3 用流程线替代停滞轻推，保留仅为兼容）。
- **测试门禁**：`.venv/bin/pytest tests -q` 全绿（全部 fake，无真机无 MLX）。LangChain 网关兼容 spike：`scripts/spike_langchain_compat.py`。
- **Web 控制台已落地并升级**：`phone_agent/web/` NiceGUI 实时控制台；run 改由独立 runner 子进程执行（WP-R，控制台重启不中断、可重连回放），`WebEventMiddleware` 只镜像 model/tool/screen/safety/TaskDoc/run_end 事件到内存队列；页面展示最新截图、步骤账本、任务板与 token/终局状态，并通过 `threading.Event` 回答 HITL。Web 层不直接访问 ADB、不写工作流状态；入口为 `python -m phone_agent.web [--device-id X] [--model M] [--port 8080]`，默认仅监听 `127.0.0.1`。
- **A4 已落地（预算/压缩/迁移纪律/去重删除）**：token 预算（L0 余量镜子 + 硬成本上限）、两级 auto-compact（handoff 摘要）、TaskDoc 状态迁移纪律、删除图片同屏去重死代码，均已合入并有单测覆盖。
- **App-KB 骨架与验证启动写回已落地**：设备可启动应用 label 在 run 首同步到本地 `memory/app_kb/`，system prompt 注入有界规范名清单，`launch_app` 通过持久 `AppKnowledge` 别名槽解析并在失败时回传候选；设备确认启动成功后，KB 命中会追加成功计数，非敏感的新说法会沉淀为 `kind=learned` 全局别名。支持 `--dream` 手动整理和 `PHONE_AGENT_DREAM=auto` 的 run 后轻量合并+对账（不删除）。显式用户纠正写 `kind=user` 尚未接入；`get_app_labels` 已真机验证；ColorOS 等厂商砍掉 label 命令时降级为包名当名字。
- **本轮不做（后续迭代项）**：
  - ~~通用长期记忆~~ **已实现**：episode 经验档案（experience.py）、RAG shadow 回想（recall.py）、经验提炼/晋升/回注（evolution.py + WP-A3）。
  - **plan 工具**：显式规划/TodoList 工具未纳入本轮。
  - **finish verifier 多帧输入**：verifier 当前默认单帧（`FINISH_VERIFY_K=1`）；`K>1` 需 S1 历史帧保留能力，尾帧多帧输入延后。
  - **compact 多帧/异步**：auto-compact 当前同步、单次 LLM 调用；异步水位线压缩延后。
- **实机诊断 skill（v2 已重写 + A5 全保真）**：`.agents/skills/phone-agent-live-diagnosis/` 已完成 v2 薄 loop 适配——生产侧 opt-in `phone_agent/v2/middleware/diagnostic.py`（证据流：多模态 text/image 拆分、JSONL 永不含 base64），skill 侧 scripts 拆包（run_diagnosis / evidence / taxonomy / analyze / sourcemap / report）。A5 把诊断产物改为**本机自用全保真**：`V2Config.diagnostic_unredacted`（env `PHONE_AGENT_DIAG_UNREDACTED`，skill 驱动置真）令证据流不脱敏、不截断；截图解码落盘到 `<run_dir>/screenshots/screen-<seq>.png`（幂等、0600），evidence `image` 增加相对 `path`；`report.html` 逐步回放（真实截图缩略图 + 模型思考全文 + 工具调用/结果/延迟）；`--share` 产出脱敏且无截图引用的 `report-share.html`。生产 `trace.py` 的 P0 #6（64 字截断 + 脱敏 + 无 base64）一字未动，全保真只活在诊断模式。
- **U1 观测生命周期已落地（地基）**：把观测收束为**唯一原子生产者 + 批次工牌 + 验鲜**，杜绝"marks 一帧、像素另一帧"的 TOCTOU 错位。
  - **原子 `observe()`（单一生产者）**：一个采样窗口内取齐 foreground-before → 截图 → accessibility dump（`refresh_marks(shot)` 复用同一张截图，不再二次截图）→ foreground-after。前后台组件在窗口内变化 = 帧不一致，整窗**重试一次**；再不稳 = 观测失败，抛 `ScreenshotError`。`refresh_marks()` 保留无参形态（外部裸调用自取一张）向后兼容。
  - **批次工牌（epoch）**：`PhoneSession.epoch` 每次成功 `observe()` +1；对外 mark ID 带批次后缀 `ax_1@e12`（provider 内部 ID 仅作 provenance 前缀），`MarkCandidate.epoch` / `Observation.epoch` / `ScreenBinding.observation_epoch` 同步写入。
  - **验鲜门（fail-closed）**：`resolve_mark` 先解析工牌批次，非当前 epoch 直接抛 `StaleMarkError`（先于 marks 查表，即使同名 provider id 在新批次复现也不误命中）；工具层照旧返回 stale 提示且不执行设备动作。
  - **失败整批作废**：观测失败（截图无效 / 持续不稳）时 `marks` 清空、不 bump epoch——绝不残留旧批次寻址权限。
  - **locate 同批次 + 同帧**：`locate` 把命中 mark 铸入**当前批次**（带当前 epoch 工牌，不 bump），并暂存其视觉模型所用截图；locate 工具经 `last_locate_frame()` 直接回该同帧（text + 该截图），不额外 `observe`。
  - **禁并行 tool calls**：`build_chat_model` 以 `model_kwargs={"parallel_tool_calls": False}` 下发（`create_agent` 每轮 `bind_tools` 不带该 flag，故设为模型默认使其跨 rebind 存活）；`PHONE_AGENT_PARALLEL_TOOL_CALLS`（默认 false）可在网关拒绝该参数时置真退出。
  - **剪除联动不动**：`images.py` 仍保留最新 2 张含图消息，U1 只改生产侧原子性，不动历史端剪除。
  - **测试**：`tests/v2/test_observation_lifecycle.py`（真 `PhoneSession` + fake 设备）覆盖原子性/单生产者/工牌不复用/验鲜拒点/重试一次/持续不稳与截图失败作废/locate 同批次同帧；全套 402 绿。
- **U4 grounding 死代码清除已落地**：删除 v1 退役时搬来的 `phone_agent/grounding/marks.py`（`Mark`/`MarkRegistry`/`build_screen_id`/`build_mark_topology_digest`/`build_ax_mark_digest` 等，503 行）与 `objects.py`（`StructureNode`/`ScreenStructure`/`ScreenObject`/`ObjectRegistry`/`build_object_registry` 等，1119 行）整文件——v2 薄 loop 从不消费这两者（`session.refresh_marks` 只取 `result.marks`）。
  - **accessibility.py 改造**：删除 `screen_structure` sidecar 构建链（`_structure_from_root` / `parse_uiautomator_structure` / `parse_uiautomator_summary` 里的 `structure_node_count` / 死路径 helper `_normalize_bounds`/`_safe_node_summary`/`_hash_value`/`_node_to_mark`）；provider 只产 `MarkCandidate`，`MarkProviderResult.screen_structure(s)` 契约字段保留但恒 `None`/`[]`（`fallback`/`locateanything` 的 visual structure 活路径不动）。失败码 `accessibility_structure_missing` 随 sidecar 一并移除。
  - **policy.py 清理**：删除仅服务 `marks.py` 的验证阈值 `mark_min_confidence` / `perceptual_hash_max_distance` 与常量 `LOCATE_INHERIT_PHASH_MAX_DISTANCE`（全仓无其它消费者）。
  - **测试**：`tests/grounding/test_fallback_usability.py` 无需改写（不触 structure sidecar），`tests/test_policy_registry.py` 结构性断言仍绿；全套 412 绿。共删 1622 行遗留代码。


- **U2 安全预警制已落地（推翻硬拦默认）**：把安全从"硬拦 interrupt"改为"预警制"——检出风险不再默认打断人或阻塞，而是把选择权交回模型。
  - **预警流（`SafetyWarningMiddleware`）**：`wary` 档下 `wrap_tool_call` 对执行类调用（tap/long_press/type_text/launch_app）先跑 `classify_tool_call`；命中风险且未带 `confirm_irreversible=true` 时**短路——不执行设备动作、不叫人**，返回一段预警 `ToolMessage`（世界事实"目标是「确认支付」，属不可逆动作" + 选项空间"带 confirm_irreversible=true 重发 / 放弃 / ask_user|take_over"），同时向 stdout 打印非阻塞提示（trace/diagnostic 端记为该 tool 结果）。模型带 `confirm_irreversible=true` 重发即放行执行。
  - **工具签名**：actuation 执行类工具加 `confirm_irreversible: bool = False`（确认放行）+ `sensitive: bool = False`（模型自申报，永远触发预警流）。
  - **检出层语义不变**：沿用 `classify_tool_call` 的 hard 判定（不可逆动词共现 / 密码框 / 凭据输入 / 自我申报）作为预警触发；`recall` 档软候选默认不预警（召回≠预警）；`reviewer` 档过第二模型精排判可逆性（不可用/异常 fail-closed 预警）。self-declared 提前到所有工具（含可逆的 launch_app）之前判定。
  - **human 通道不动**：`ask_user`/`take_over` 任何档都保留 interrupt（控制中断，非安全）。
  - **四档模式**：`PHONE_AGENT_SAFETY_MODE=off|wary|hard|reviewer`（默认 `wary`）。`wary`/`reviewer` 走预警流、`build_hitl_middleware` 不对执行类工具挂 interrupt；`hard` 保留旧 HITL 硬拦（approve/reject，挂机/无人值守用）；`off` 全关。`build_safety_warning_middleware` 仅在 `wary`/`reviewer` 返回中间件，装在中间件栈最内层。
  - **测试**：`tests/v2/test_safety_warning.py`（20 例）覆盖预警不执行 / confirm 重发执行 / 自申报触发 / wary·hard·off·reviewer 四档 / ask_user·take_over 仍 interrupt / 密码框凭据预警 / launch_app 软候选不预警但自申报触发；`test_safety_layers.py`·`test_middleware.py` 同步更新。全套 433 绿。

---
