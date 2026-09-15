# step-16 观测死锁：v1 把 mark 管线与观测解耦

> **回溯性重建**（2026-09-15 撰写）。本篇不来自事故当天的现场记录——v1 时代没有留下日志、trace 或报告；
> 全部内容取自 tracked 历史：`git show ac4fa34:docs/refactor-thin-loop-v2.md` §1（当时唯一写下的结论）、
> `ac4fa34` 时刻的 v1 源码，以及 `.agents/notes/implemented/` 里两篇事后笔记。
> 文件名日期不是事故时间戳：事故发生在 v1 时期、日期不可考，取修复落地日 2026-08-29 作锚定日。

## 时间线

| 时间 | 事件 | 出处 |
|---|---|---|
| v1 时期（日期不可考） | run 在第 16 步附近停住不再前进，当时称 "step-16 事故" | 仅见于下方 2026-08-27 的书面记录 |
| 2026-08-27 17:11 | 重构文档落库，§1 把该事故写成公开事实：v1「mark 管线与观测解耦导致死锁」 | `ac4fa34` |
| 2026-08-27 17:18 | v1 架构整体删除：152 个文件、−62292 行 | `3ff77e4` |
| 2026-08-29 14:03 | v2 原子观测落地：epoch 铸造 marks、新鲜度门、唯一生产者 | `42dd61e` |
| 2026-08-29 14:58 | v1 遗留的 marks/objects 死代码清除（1622 行） | `9968f8d` |
| 2026-09-15 | 本篇按上述证据回溯重建 | 本文 |

## 根因

**可核实的事实（结构层面）**

1. **观测不是原子生产者**。v1 的「当前屏幕」由多处分别产出：mark 注册表在 `phone_agent/graph/marks.py`
   （docstring: "Screen mark registry for harness-side GUI grounding"），而执行后的观测采集另有共享节点
   `phone_agent/graph/nodes/observation_capture.py`，其 docstring 明说是给 Reflect 与 Acceptance 两个节点
   共用的，理由是「两个节点不能对『屏幕现在是什么』产生分歧」——两个消费者需要一份共享实现，说明权威帧
   当时没有唯一归属。
2. **mark 的有效性靠事后校验，而不是结构性批次**。v1 的 `Mark` 绑定 `screen_id`，是否还算数由验证策略决定
   （`LOCATE_INHERIT_PHASH_MAX_DISTANCE` 的感知哈希继承距离、`MARK_CONFIDENCE_THRESHOLD` 的置信度下限）。
   「这张 mark 和当前帧对不对得上」因此是一个要在环里反复求解的问题，而不是一次提交就定死的断言。
3. **推进权在路由层的计数器手里**。v1 的边函数按 `observation_retry_count`、`validation_replan_count`、
   重复动作守卫与能力是否 `requires_reobservation` 决定回 `replan` / `reflect` / `takeover` / `acceptance`
   （`phone_agent/graph/edges.py`，`observation_retry_limit` 来自 policy）。
4. **每个决策周期两次模型调用**（plan 一次、reflect 一次），回路每绕一圈都更贵。

**回溯推断（无现场日志佐证）**

- 事故的形态是**互相等待**：route 层在等一个新的有效观测，而「新观测」的产出与 marks 的更新分属不同环节，
  计数器驱动的 replan 回路既没推进状态也没终止——于是 run 停在半途。当时把它叫死锁，指的是这个停滞，
  不是操作系统意义上的锁。
- "step-16" 说明停在第 16 步附近；该步的输入、trace 与画面都没有留下，无法进一步定位到具体工具调用。
- 事故与「每步两次模型调用」「validation/replan/guidance 复杂」共同构成删掉 v1 的直接理由，但三者之间的
  因果权重只能定性，不能定量。

## 修复

**修的是结构，不是 bug**：没有针对死锁打补丁，而是把承载它的架构整体删掉，换成 thin loop——每步一次模型
调用，观测有唯一生产者。落地顺序：

- `3ff77e4`（2026-08-27）：删除 v1 节点图、checkpoint、ActionIR 管线与 v1 耦合测试；`adb/`、`grounding/`、
  `config/` 保留为库。
- `42dd61e`（2026-08-29）：v2 原子观测。`session.observe()` 是唯一观测生产者（同一次采样的截图与 marks、
  前后台 bracketing、整窗重试一次），提交时 epoch 前进、marks 重铸为 `ax_1@e12` 形式的批次 id，跨批引用
  在 `resolve_mark` 处拒绝；观测失败则整批作废。
- 观测时序、失败分支与「参考图不能寻址」的完整语义见 [架构：原子观测](../pages/architecture.md#atomic-observation)
  与 [架构：mark 寻址与批次](../pages/architecture.md#marks-first)。

## 防再发

- **军规**：根 `AGENTS.md` 的 P0 #15（`session.observe()` 是唯一观测生产者）与 P0 #2（执行动作必须绑定
  mark，歧义/未命中 fail-closed）；子树就近约束见 `phone_agent/v2/AGENTS.md`。
- **锚点**：`pages/architecture.md#atomic-observation`、`#marks-first` 承载当下语义，改行为必须同步。
- **测试**：`tests/v2/test_observation_lifecycle.py`（生命周期与批次失效）、`test_observation_failure_codes.py`
  （失败码不伪装成空屏）、`test_observation_reference_frame.py`（未验证参考图不能寻址）、
  `test_observation_hardening.py`（安全画面 fail-closed）。
- **仍然脆弱**：观测最多 4 次设备往返 + 一次整窗重试，默认 300ms 静置直接加在每步延迟上；「刷新一次屏幕」
  在 v2 里是显式动作，模型忘记重观测时表现为 fail-closed 拒绝执行，而不是静默点错目标。
- **同类故障的自检问题**：这条链路上还有谁在等「另一个环节产出的当前状态」？如果答案不是唯一生产者，
  就还没修干净。
