# Agent Note: finish 两段式定稿 + 独立上下文验收器（故障 fail-open）

Status: implemented

## Problem

模型说「我做完了」和「真的做完了」是两件事。run 的终局一旦被接受就立即生效：后续 sibling 调用被跳过、
不再采样模型。因此终局需要一个检查点，而检查点本身会引入新风险：

- 检查太弱（模型一句话就落定）→ 未完成被当真，回执与产出物不可信；
- 检查太强（验收失败就拒绝定稿）→ 一个不稳定的模型能把正确完成卡死，而 run 已经没有交互预算了。

实现顺序：finish 两段式复核包 + 证据锚点先落地（commit `fb9f820`，2026-08-28），独立验收器随后加入
（commit `e04f2ce`，2026-08-28）。两条都写进了 P0 表（P0 #12）：两段式、`completed` 必须有 `evidence_note`、
验收器「绝不看 actor transcript」、故障 fail-open 记 `skipped` 绝不记 `pass`。

（本篇 2026-09-15 回填，日期为补写日。）

## Decision

**两段式 finish**：第一次 `finish` 只返回复核包（`[FINISH 复核包]`：目标、路线完成度、疑点、选项），模型
确认无误后再调用 `finish(confirm=true)` 才定稿；复核包与确认之间有 seq 守卫
（`v2/tools/control.py`：`if confirm and reviewed and current_seq == review_seq`）。硬约束：

- `finish` 必须带非空 evidence（`error: finish requires non-empty evidence`）；`completed` 路线项必须有
  `evidence_note`（`completed 项 {id!r} 缺少 evidence_note（完成项需给出屏幕证据）。`）；有 open 路线项时
  `finish` fail-closed（`路线仍有未完成项：…`）。
- 被接受的 finish **立即终局**：同轮后续 sibling 工具调用收到 `status="error"` 的 skipped 回执且不再采样
  模型（`v2/agent.py` 的 execution-admission 监听器 + 终局谓词）。
- `PHONE_AGENT_FINISH_VERIFY=off` 退化为单段落定（向后兼容），此时验收器不参与。

**独立上下文验收器**（`v2/verify.py`）只看三样东西：权威目标（`goal_base` + amendments）、证据路线
（completed + `evidence_note` / blocked + reason）、尾部截图（K=1 当前帧）。它构造的上下文只有两条消息
（system + human），**绝不看 actor transcript**——docstring 原文：the actor's transcript / self-defence is
deliberately **excluded**（防「说服式通过」）。目标与证据字符串出境前先经 `config/redact` 脱敏。

**故障语义与安全门相反**：安全门 fail-closed，验收器 **fail-open**——setup/调用故障时放行该 finish、审计记
`skipped`、绝不记 `pass`（docstring：It must never wedge a correct completion behind a flaky model）。
触发策略 `auto`（默认）：高风险目标或硬矛盾下坚持 confirm 才跑验收器；`always` 每次 confirm 都跑。
连续 2 次驳回（`DISPUTE_TAKEOVER_THRESHOLD`）升级 `take_over`
（`finish 反复被验收驳回，需人工确认`）。TaskDoc 关闭时 run 原始目标仍是权威（`v2/verify.py::_goal_texts`
回落 `session.run_goal`）。

（理由为回溯性重建：`v2/verify.py` docstring 与 `.env.example` 记下了 fail-open 的理由与各档语义，但
「为什么必须两段式」没有当时的书面论证。）

## Alternatives considered

**单段 finish（模型一次调用即定稿）**

- **最强的理由**：少一次模型往返，链路最短；终局本来就是模型的判断。
- **为何被否**：没有「最后看一眼世界事实」的机会，模型的自述直接变成终局。两段式把复核包（目标 + 路线 +
  疑点）先摆到模型面前，`confirm=true` 要求它明确重申。`off` 档保留单段只是为了向后兼容，不是推荐值。

**验收器 fail-closed（故障则拒绝 finish）**

- **最强的理由**：绝不让未验证的完成落定；与安全门语义一致，最保守。
- **为何被否**：L1 的两段确认本身已是有效兜底，再叠一层硬门会让不稳定模型卡住正确完成；用户约束是「不要
  过度强调安全」。因此故障放行但**记录在案**：审计状态 `skipped` 与 `pass` 严格区分，事后可追。

**把 actor transcript 一起喂给验收器**

- **最强的理由**：信息更多，模型能解释它为什么认为完成了。
- **为何被否**：会被「说服式通过」（docstring 原话）；验收器只认世界/路线事实与截图，不看模型辩解。

**每次 finish 都过验收器（把 always 做成默认）**

- **最强的理由**：没有漏检；策略简单，不需要风险判定。
- **为何被否**：成本与收益不匹配——验收器是一次额外模型调用。默认 `auto` 只在高风险目标（goal 命中支付/
  删除/凭据词表）或「硬矛盾下坚持 confirm」时触发，`always` 留给需要的部署。

**让验收器输出操作指导（「还差什么、下一步怎么做」）**

- **最强的理由**：驳回信息越具体，模型越容易一次补上缺口，减少往返。
- **为何被否**：验收器是终局裁判，不是第二个规划者。其系统提示明确要求「陈述缺了什么，不要给出操作指导」：
  指导会与 actor 的 TaskDoc 规划冲突，也会给「说服式通过」提供新的措辞入口。驳回只陈述缺口，怎么补由模型
  自己决定。

## Consequences

- **收益**：终局有世界事实锚点（目标 + 证据 + 截图）；证据强制（completed 项必须 `evidence_note`，空证据
  finish 被拒）；驳回可升级人工；故障可审计（`skipped` ≠ `pass`）。
- **代价**：`auto` 命中时多一次模型调用；fail-open 意味着验收器故障时未验证的完成也会落定（只留审计）；
  当前只喂 K=1 当前帧（`PHONE_AGENT_FINISH_VERIFY_K`），`K>1` 需要历史帧保留能力，属路线图延期项。
- **边界**：验收器只判「目标是否达成」，不给操作指导；TaskDoc 的路线是给验收器看的证据，TaskDoc 关闭时
  不影响原始目标的权威性。被接受的 finish 是终局：同轮 sibling 调用不再执行，也不再有第二次模型采样——
  唯一例外是 token 边界上一次已发复核包的续办额度（P0 #13）。
