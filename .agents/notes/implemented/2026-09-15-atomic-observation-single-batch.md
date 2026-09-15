# Agent Note: session.observe() 是唯一观测生产者，marks 带批次徽章且工具单批串行

Status: implemented

## Problem

thin loop 的每一步都建立在「模型看到的画面 = 它引用的 mark」这个等式上，而设备侧有四类信号要取
（截图、accessibility 树、前台组件 before/after）。朴素实现有三个坑：

- **不同帧**：截图与 dump 各采一次，采样之间前台可能漂移 → marks 与图像对不上，模型按错位的 marks 点击；
- **同 id 不同屏**：mark id 只是 `ax_1`，换屏后同一个 `ax_1` 指向别的控件，过期引用会静默点错目标；
- **交织/并行**：多个工具调用同时动设备，或 provider 端并行下发多个 tool call，会让「第一个动作已经作废
  的 marks」继续被第二个动作使用——成功观测会让整批 mark 过期。

而需要观测的地方不止一处：run 开局（`v2/agent.py`）、执行类工具成功后的自动观测块（`v2/tools/_obs.py`）、
finish 复核包（`v2/review.py`）、独立验收器（`v2/verify.py`）都要拿「当前屏幕」。如果每个调用点各写一套
截图/dump 逻辑，"哪一帧算数"就会随调用点漂移。

（本篇 2026-09-15 回填，日期为补写日。）

## Decision

- **唯一观测生产者**：`v2/session.py::observe()`（docstring "Atomic single-producer observation (U1)"）。
  流程：静置（`PHONE_AGENT_OBSERVE_SETTLE_MS`，默认 300ms，显式值钳 [0,5000]）→ 取前台 before → 截图 →
  用**同一张截图**做 accessibility dump → 再取前台 after。前后台不一致（"Screen moved mid-capture"）或瞬时
  dump 失败（timeout/parse/provider/`on` 不支持）在同层重试**一次**；只有末次可提交。
- **提交与徽章**：`_commit_observation` 递增 `screen_seq` 与 `epoch`，并把该帧的 marks 铸成新批次
  `<provider_id>@e<epoch>`（如 `ax_1@e12`）。外部 mark id 一律带后缀，`resolve_mark` 先比 epoch 再查表，
  跨批引用返回 `is from batch e…, current batch is e…; re-observe (read_screen) and use a fresh mark id`。
- **失败整批作废**：观测失败时 `_invalidate_batch()` 清空 marks、epoch 冻结（绝不留下旧批的寻址权威）。两个
  例外被显式定义：最终截图有效但 dump 仍失败 → 提交**标注过的零 marks 帧**（真正空屏不重试）；本次取得过
  有效截图但整窗失败 → 返回**未验证参考图**（docstring：`screen_seq`/`epoch` 冻结、不铸 mark、几何不变、
  `secure_screenshot_blocked` 不保留）。
- **黑屏防线**：`PHONE_AGENT_BLACK_SCREEN_DETECT=on`（默认）时 RGB ≤ 4 的均匀黑图经
  `secure_screenshot_blocked` fail-closed，走既有保护页通道，不假装成「空屏无控件」。
- **locate 开新批**：成功 `locate` 递增 `screen_seq`/`epoch`、清空 marks、只铸入命中的那个 mark（重复 locate
  用单调的 `#<seq>` infix 防 id 冲突）。
- **单批执行**：`v2/agent.py` 的 execution-admission 监听器串行化工具调用、保持声明顺序；终局转换后 sibling
  返回 `自动化已终止；该后续工具调用已跳过。`（`status="error"`）。`PHONE_AGENT_PARALLEL_TOOL_CALLS` 只是
  provider hint：设 true 只是不再下发 `parallel_tool_calls=false`（`v2/model.py` 注释：The thin loop is
  strictly one-observation-one-action; a parallel batch would address marks the first action already
  invalidated），串行仍由 admission 保证。

（理由为回溯性重建：以上动机散见于 `v2/session.py` 的 U1/B1/B4 注释与 `v2/model.py` 注释，没有当时的独立
决策记录。）

## Alternatives considered

**截图与 dump 各自采样（解耦取信号）**

- **最强的理由**：解耦更灵活——dump 可以复用缓存、失败可以独立重试，不必绑定某一次截图。
- **为何被否**：前后台漂移会让 marks 与图像不同帧，寻址立即失真；解耦还让「哪一帧算数」没有唯一答案。
  原子窗口把重试放在同一层（要么整窗成功，要么失败），只有末次可提交。

**mark id 不带批次徽章**

- **最强的理由**：id 更短更好引用，提示词与回执都更干净。
- **为何被否**：批次不可见时过期引用无法结构性检测。代码注释记录了真实故障形态：同一 epoch 的第二次 locate
  会覆盖同名的第一条目，之后对第一个 id 的 tap 会静默操作第二个目标（B1）；B4 之前 locate 还会把新帧命中
  打上旧批次的 epoch。徽章让 `resolve_mark` 能先比批次再查表。

**允许并行/交织工具调用**

- **最强的理由**：provider 原生支持并行 tool call，理论上更快，能一次做完多个动作。
- **为何被否**：单批语义下，第一个动作会让当前 marks 批次失效，并行批的第二个动作就会攻击已作废的 mark
  （`v2/model.py` 注释）；设备也只有一只手，交织采样（截图/dump/前台）会互相污染。串行 + 声明顺序是可复现
  的前提。

**观测失败只返回纯文字**

- **最强的理由**：干净——不给模型任何可能被误当作「当前屏幕」的图像，避免它按旧图操作。
- **为何被否**：完全失去世界锚点会让模型只能盲试。v2 用「未验证参考图」折中：返回最近一张有效图，但明确
  标注较早采样/未验证/非当前可操作批次，不提交 epoch/`screen_seq`/几何、不铸 mark；`secure` 保护页不保留
  （隐私边界不因失败而放宽）。

**观测失败就中止 run（fail-fast）**

- **最强的理由**：状态未知时继续动作风险最大；立刻停下并交人工最保守。
- **为何被否**：观测失败不等于任务失败——模型可能只需要再读一次屏幕、换一条路径，或者明确报告失败
  （`finish` / `ask_user` / `take_over`）；harness 替他结束会剥夺这些选择。因此失败只返回事实文本或标注过的
  参考图，终局决定仍归模型（P0 #5：失败返回错误文本，绝不伪装成功）。

## Consequences

- **收益**：marks 与图像同帧；过期寻址结构性拒绝（测试断言 stale mark 返回错误且无设备动作）；模型有稳定的
  引用规则（只用最近一次观测的 `ax_*@eN`）；终局后 sibling 不会偷偷执行；失败时仍有一张标注过的参考图可用，
  动作成功回执不因随后的观测失败而降级。
- **代价**：每次观测最多 4 次设备往返 + 一次整窗重试，且默认 300ms 静置直接加在每步延迟上；成功 `locate`
  强制开新批（旧 marks 全失效，模型必须重新观测）；零 marks 帧要靠诊断标注区分「空屏」与「dump 失败」。
- **边界**：参考图永远不能寻址（旧 `ax_*@eN` 仍 fail-closed）；`op=blocked` 只展示不拦截；`K>1` 历史帧保留
  能力尚未实现（验收器目前只看当前帧）。`PHONE_AGENT_MARKS_WINDOWED` 只是展示层（分组/标注/渲染），不改变
  寻址、执行与折叠语义——批次徽章才是寻址权威。
