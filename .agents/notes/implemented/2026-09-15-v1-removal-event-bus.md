# Agent Note: 删除 v1 LangGraph 节点架构，改为「每步一次 create_agent 调用 + 事件总线监听器」

Status: implemented

## Problem

v1 是跑在 LangGraph 上的节点图：goal → plan → execute → reflect → acceptance。重构前的现场记录
（`docs/refactor-thin-loop-v2.md`，commit `ac4fa34` 引入，随后 `5d619f7` 起不再跟踪，只能用
`git show ac4fa34:docs/refactor-thin-loop-v2.md` 读）§1 写下了三类问题：

- **流程过重**：每个决策周期两次模型调用（plan 一次、reflect 一次）。
- **mark 管线与观测解耦，导致死锁**（原文称 "step-16 事故"）。
- **validation / replan / guidance 机制复杂且低效**。

同时策略实现分散在节点与 LangGraph 中间件两处，"谁拥有决策"没有单一归属：安全、剪图、限额各自写在各处，
run 的可观测面（trace、终局）也由节点顺序隐式决定。

切换是「文档先于代码」的一次性动作：2026-08-27 当天先落重构文档（`ac4fa34`），同一天删除 v1（`3ff77e4`）、
落地 v2 core（`6da04d9`）与工具层，v1/v2 没有长期并存期。此后策略层再经过一次形态变更：从 LangChain
中间件迁到事件总线（`cf80f6a`）。

（本篇 2026-09-15 回填，日期为补写日。）

## Decision

删掉 v1 节点架构，改为**薄 loop**：模型每步一次 `create_agent` 调用，通过工具感知与操作设备；harness 只
提供工具、执行安全边界、上下文卫生、trace 与固定 schema 经验档案，**不做工作流路由**（任务规划交给模型用
TaskDoc 自行维护，见 `pages/architecture.md`：「harness 不做流程编排：没有节点、没有路由」）。

落地事实：

- 删除范围：`phone_agent/graph/`、`phone_agent/actions/`、`phone_agent/checkpoint/`、`phone_agent/agent.py`、
  `main.py`、`evals/` 与 v1 耦合测试。commit `3ff77e4`（2026-08-27）一次删除 152 个文件、-62292 行。
- 保留为**库**（不属工作流）：`phone_agent/adb/`、`phone_agent/device_factory.py`、`phone_agent/grounding/`、
  `phone_agent/config/{policy,app_registry,redact}.py`；`v2/session.py`、`v2/middleware/safety.py`、
  `v2/tools/actuation.py` 等直接引用它们。`phone_agent/grounding/` 里 v1 遗留的 marks/objects 死代码随后被
  单独删除（commit `9968f8d`，1622 行），只留 MarkProvider 体系。
- 策略层最终**完全事件化**（commit `cf80f6a`，2026-09-07）：`v2/capabilities.py` 是唯一装配器，能力经五条
  接缝挂载（有序中间件、工具、prompt providers、start/end run hooks、CLI 命令）；内建策略全部是
  `v2/events.py` 事件总线上的监听器。core `tool/execute` 的最终序是
  `trace → diagnostic → admission → control HITL → capability 链（safety 最内）`（见 `v2/agent.py`）。
- 编译后的 LangChain 中间件栈只剩桥接器 + 可选 `extra_middleware` 观察者；`v2/capabilities.py` 明确
  「Middleware is now reserved for LangChain bridge middleware only; all policy behavior lives on the
  event bus」。

（理由为回溯性重建：「为什么把策略也搬到事件总线」的动机来自实现自述与 P0 #18，当时没有留下独立决策记录。）

## Alternatives considered

**保留 LangGraph 节点图（裁节点、升级框架）**

- **最强的理由**：显式状态机可审计、可复现；节点级 checkpoint / HITL / 重试是现成能力；多步推理的边界由
  代码而不是模型决定，跑批时更可控。
- **为何被否**：正是这些能力成了成本——每步两次模型调用（plan + reflect），mark 管线与观测解耦直接导致
  step-16 死锁，validation/replan/guidance 层复杂且低效。问题不是「节点写得不好」，而是「用节点图承载一个
  模型已经能自己做的循环」。

**工作流路由留在 harness（在 create_agent 之上再叠计划/路由层）**

- **最强的理由**：确定性与可测试性最强；弱模型也能被流程兜住；失败可归因到具体阶段。
- **为何被否**：与「薄 loop + 工具化」目标直接冲突：只要 harness 继续编排，模型就仍然看不到真实世界状态，
  观测与决策解耦的问题（step-16）会以另一种形式回来。v2 的选择是把规划暴露给模型，用 TaskDoc 任务板承载
  目标与路线，harness 只做 pin 与校验（P0 #11）。

**采用 create_deep_agent 的 filesystem/shell 电池**

- **最强的理由**：开箱即用，省掉自建工具与提示词的时间。
- **为何被否**：重构文档 §1 把它列为「技术轨道」之外的选项并明确不用：本项目的工具语义是手机操作
  （marks、观测、安全门），filesystem/shell 电池与设备无关，带不来收益却扩大攻击面。

**在 v1 上做增量修补（保留节点图，只砍 validation/replan）**

- **最强的理由**：改动面最小，checkpoint、评估体系、诊断 skill 都不用动；step-16 事故可以只当作一个 bug 修。
- **为何被否**：三类问题里有两类来自结构本身——每步两次模型调用是节点划分的直接结果，mark 管线与观测解耦是
  「观测不是原子生产者」的结果（见 `2026-09-15-atomic-observation-single-batch.md`）。只砍 validation 层会
  留下同一个循环的另一个版本，重构文档因此选择了全量替换。

## Consequences

- **收益**：一个决策周期一次模型调用；策略可插拔（内建与插件共用同一装配层与事件总线）；终局、上下文、
  trace、安全各自拥有单一归属；v1 时代 6 万余行代码被删除，维护面收窄。
- **代价**：v1 现成的 checkpoint/HITL 需要重建（v2 用 `ControlHitlListener` + 中断恢复实现）；v1 的评估体系
  `evals/` 整体删除后需要重建；`.agents/skills/phone-agent-live-diagnosis` 当时依赖 v1 run 结构，需要适配
  （重构文档 §2.3 记录在案）；`README.md` / `AGENTS.md` 一度描述 v1，需要同步。
- **已知瑕疵（2026-09-15 已修复）**：`phone_agent/v2/agent.py` 的 `ThinPhoneAgent` docstring 曾写 "only four bridge middlewares"
  却列出五个；本次文档重构已改为 five，`pages/architecture.md` 记的是 5 个。
- **边界**：删除是「不再经由 v1 路由」，不是「v1 的一切都消失」：`adb/`、`grounding/`、`config/` 作为库
  继续被 v2 调用；`phone_agent/{graph,actions,checkpoint}` 不得重建，新行为也不得经由它们路由。
