# Agent Note: 安全默认走 wary 预警流，而不是默认 HITL 硬拦

Status: implemented

## Problem

不可逆动作（提交订单、转账、删除）与凭据输入（密码、验证码）需要一道门。门有两种代价完全不同的形态：

- **HITL 硬拦**：停下来等人 approve/reject。人在场时最可靠；设备挂机跑批时会把 run 卡在中断点。
- **直接放开**：零摩擦，但不可逆动作没有任何检查。

thin loop 的前提是模型是唯一决策者：harness 不该在模型背后突然召唤人类，也不该替模型判断「这个动作值不
值得」。v2 最初按重构文档 §9.1 实现的是 HITL——命中敏感词就 `HumanInTheLoopMiddleware` interrupt
approve/reject。

另一条既有约束是「模式可切」：`off` / `wary` / `hard` / `reviewer` 四档并存，工厂/集群场景要低摩擦，挂机
场景要真人把关；而 `ask_user` / `take_over` 在任何档位都必须 interrupt（模型主动要人时不能被安全档位吞掉）。

（本篇 2026-09-15 回填，日期为补写日。）

## Decision

默认 `wary`：检出风险的执行类调用**不执行、也不叫人**，工具直接返回一段预警（世界事实 + 选项），模型要带
`confirm_irreversible=true` 重发同一个调用才真正执行（commit `b31cb64`，2026-08-29）。

- **判定链**：`v2/middleware/safety.py::classify_tool_call` 把调用判成 `none | recall | reviewer | hard`：
  宽召回（policy 词表 / 密码框 / 模型自申报）→ 可选第二模型精排 → 硬信号。硬信号包括不可逆提交（承诺词 +
  不可逆宾语共现）、密码框、凭据/验证码输入、模型 `sensitive=true` 自申报、policy takeover；`launch_app`
  只是软候选，永不 hard（可逆）。
- **短路点**：`SafetyWarningListener` 挂在事件总线 `tool/execute` 最内层，返回 `ToolMessage(status="error")`
  且不调用下游——即「不执行」；`_default_notify` 只往 stdout 打一条非阻塞提示——即「不叫人」。
- **预警文本**：`⚠️ 已拦截（未执行）：<工具>` + 世界事实 + 三个选项（带 `confirm_irreversible=true` 重发 /
  放弃 / 交人工 `ask_user`/`take_over`）。
- **「召回≠预警」**：宽词表只产出候选，弱动词必须与不可逆宾语共现才升级为硬信号，所以「确认支付」
  「立即支付」预警，而「支付方式」「支付宝红包」「删除」保持软候选（默认 wary 不预警）。
- **档位**：`PHONE_AGENT_SAFETY_MODE=off|wary|hard|reviewer`，默认 `wary`。`hard` 保留旧 HITL 硬拦（挂机/
  无人值守用）；`reviewer` = wary + 第二模型精排，精排模型的三处构建入口全空时跳过精排并按 fail-closed 预警
  处理，构建失败沿角色链再试一跳一次并记 `model_fallback` 审计。
- **人工入口不变**：`ask_user` / `take_over` 在任何档位都 interrupt——需要人时由模型显式提出，而不是 harness
  暗中拦下。

（理由为回溯性重建：`v2/middleware/safety.py` docstring 与 `.env.example` 记下了各档语义与 "for unattended
runs" 的用途，但「为什么默认不是 hard」没有当时的书面论证。）

## Alternatives considered

**默认 hard：HITL 硬拦（v2 最初的实现）**

- **最强的理由**：真人把关最可靠；不可逆动作绝不越过人；中断点天然可审计、可复盘。
- **为何被否**：它把「是否值得继续」的判断从模型手里拿走，而 thin loop 的前提是模型决策；每一次风险调用都
  需要人在场，交互式跑任务时变成频繁打断。需要真人把关的场景仍然保留，只是显式切 `hard`
  （`.env.example`：「hard：旧 HITL 硬拦……（挂机/无人值守用）」），而不是默认。

**默认 off：不门控 actuation**

- **最强的理由**：零摩擦、可直接跑批；行为完全可预测（工具调用就是工具调用）。
- **为何被否**：不可逆提交、密码框、验证码没有任何检查，而且「静默放开」不可审计。`off` 保留为显式选项
  （`.env.example`：「用于长期低摩擦跑批」），风险由操作者自己承担。

**预警之后叫人（通知 / 桌面提醒 / 强制转 ask_user）**

- **最强的理由**：双保险——既不执行，又保证有人知道。
- **为何被否**：wary 的定位是把世界事实与选项交回**模型**，不新增人工决策点；`ask_user` / `take_over` 已是
  模型可用的显式人工入口，harness 再插一层既重复又无法判断何时该叫人。实现上因此只有 stdout + trace 的非
  阻塞提示，不做桌面通知。

**所有召回都预警（宽词表直接 gate）**

- **最强的理由**：安全优先，宁可误报不漏报。
- **为何被否**：误报会把「支付方式」这类普通界面词变成每步确认，拖垮 loop 并诱导模型无脑确认；期望行为是
  软候选不预警（`v2/middleware/safety.py` 的 "召回≠预警" 段）。精度交给 `reviewer` 档的第二模型精排，而不是
  默认档。

**预警文本里直接给出替代动作（harness 替模型规划）**

- **最强的理由**：模型不一定能自己想到安全替代（先切到「支付方式」再确认）；给出建议能减少一次试错往返。
- **为何被否**：harness 不做工作流路由（P0 #18），预警的定位是「世界事实 + 选项」——告诉模型它拦下了什么、
  为什么、有哪些出路，但不指定走哪条。替代方案属于模型的规划自由，hard-code 进预警等于把策略又搬回 harness。

## Consequences

- **收益**：无人值守可跑，风险动作仍有门且模型知情选择；模式可切（off/wary/hard/reviewer）覆盖从「跑批」到
  「挂机等人」的全部场景；误报率由软/硬信号分层控制。
- **代价**：预警不是阻塞保证——模型可能直接确认重发（多一次往返、多读一遍事实）；真正的「绝不执行」只剩
  `hard` 档。`reviewer` 档依赖第二模型可用性：模型不可用或异常一律 fail-closed 预警（宁可多问一次）。
- **边界**：预警只给「世界事实 + 选项」，不判断动作是否明智；`launch_app` 之类可逆动作永不 hard，避免把普通
  切换应用变成人工门。预警的可见性有上限：stdout 提示与 trace 记录（`warning_count`、reason）是审计面，
  不是阻塞面。
