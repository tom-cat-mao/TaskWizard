# Agent Note: 执行动作必须绑定 mark，0-1000 坐标换算只在工具内

Status: implemented

## Problem

模型要在一个它看不见像素的屏幕上点击。三条现实约束：

- 屏幕分辨率、密度、旋转各异，模型没有可靠的绝对像素先验；
- 截图与 accessibility 树是两个采样面，任何「看一次、记坐标、后点」的链路都可能点到已经变化的界面；
- 模型会引用它上一次看到的元素，而屏幕早就换页了。

v1 已经确立了 0-1000 相对坐标语义（`phone_agent/v2/coords.py` 自述
"Ported from the v1 ``graph/tools/coords.py`` (P0 #1 semantics preserved)"），但「动作绑 mark」与批次有效期是
v2 补齐的。重构文档 §1 把 marks-first 列为已锁定约束：

> **marks-first grounding**：执行类动作必须绑定 mark；原始坐标仅 fallback（swipe 例外）。

两条约束如今写进了 P0 表：坐标「0-1000 相对坐标 → 绝对像素的换算**只在工具内**；模型永远看不到绝对像素」
（P0 #1）；marks「执行动作必须绑定 mark：`tap` 用 `target_mark_id` 或 `target_description`（唯一解，歧义/
未命中 fail-closed），裸坐标只留给 `swipe` 兜底」（P0 #2）。

（本篇 2026-09-15 回填，日期为补写日。）

## Decision

- **坐标换算的唯一边界**：模型只产出 0-1000 相对坐标；`v2/coords.py::convert_relative_to_absolute()` 是唯一
  换算点，只在工具内被调用（`v2/session.py` 的 `relative_to_abs` / `mark_center_abs`）。模型侧文本里只有
  归一化坐标与 marks 摘要——`[OBS]` 块渲染 `mark_id | role | text | center`，`center` 是 0-1000
  （`grounding/provider.py` 注明 "normalized 0-1000 coordinates"），没有任何 width/height/绝对像素。
- **动作必须绑 mark**：`tap` / `long_press` 走双寻址 `target_mark_id`（直达）或 `target_description`（经
  `v2/resolver.py` 解析为唯一 mark）。两者都不给、同时给、歧义、未命中、过期，全部 fail-closed 返回错误
  字符串且不执行（`v2/tools/actuation.py::_resolve_target`）；模块 docstring：
  "Neither raw-coordinate tap nor a black-image path exists."
- **裸坐标的唯一去处**：`swipe`（唯一坐标兜底，坏入参返回
  `start must be [x, y] in 0-1000 relative coords`）与 `scroll`（方向语义，工具内部取中点）；`tap` /
  `long_press` / `type_text` 没有坐标参数。
- **批次有效期**：mark id 对外一律带批次徽章 `<provider_id>@e<epoch>`（如 `ax_1@e12`）；成功观测 epoch+1、
  旧批全部过期；`session.resolve_mark` 先比 epoch 再查表，跨批引用直接拒绝并提示重新 `read_screen`；成功
  `locate` 开启新批次（只铸入命中 mark）。
- **回执语言是 mark**：成功回执写成「已点击「上海」(ax_3)」，而不是像素。

（理由为回溯性重建：约束本身与实现可见于重构文档 §1 与源码 docstring，但「为什么不能让模型直接给像素」的
论证没有当时的书面记录。）

## Alternatives considered

**模型直接输出绝对像素坐标**

- **最强的理由**：链路最短，跳过 grounding 与 marks 整条管线；解析无歧义（一个点就是那个点）。
- **为何被否**：同一模型在不同分辨率/密度/旋转下给不出稳定的像素先验；把设备几何写进模型输入等于把
  「跨设备可移植」外包给提示词。0-1000 + 工具内换算让同一条决策在任意设备上含义一致，且换算点唯一
  （P0 #1）。

**保留裸坐标 tap 作为兜底**

- **最强的理由**：UI 树缺失、dump 失败、无文本图标等场景下，裸坐标是唯一还能动的手段。
- **为何被否**：裸坐标无法表达「我点的是当前屏幕上的哪个元素」，过期即静默点错目标；而失败必须可见
  （P0 #5）。v2 的选择是让失败显式：歧义返回候选列表并给 `target_mark_id` 的出路，未命中走 `locate` 视觉
  兜底再回到 marks，而不是放开坐标。

**只认 mark_id，砍掉 `target_description`**

- **最强的理由**：少一条解析路径，行为更容易预测与测试。
- **为何被否**：marks 文本是 UI 树摘要，图标/无文本控件可能选不出来；自然语言描述是模型天然的表达方式。
  `v2/resolver.py` 用精确 → 子串 → 规范化模糊三档，多个命中就 `ResolveAmbiguousError` 返回候选，零命中才
  触发 `session.locate` 深度视觉兜底——兜底路径同样落到唯一 mark，仍然 fail-closed。

**让 `op=blocked`（被弹窗覆盖）直接拦截执行**

- **最强的理由**：被覆盖的控件点不到，拦下来能省一次失败往返。
- **为何被否**：layer 证据不足以证明控件不可点（弹窗可能已消失、覆盖判定可能过期）。窗口化 marks 因此只作
  展示；`pages/roadmap.md` 明确「不计划直接用于执行门控」，未来启用属于独立决策，需先真机验证与授权。

**把 v1 grounding 的输出坐标直接当动作目标（不过 marks 表）**

- **最强的理由**：链路短——模型给出坐标就直接下发，省掉 marks 注册、批次管理与描述解析；v1 就带着一套独立
  的 marks/objects 管线（`grounding/marks.py`、`grounding/objects.py`，合计 1622 行）。
- **为何被否**：坐标一旦离开「属于哪一帧」就没有权威，模型随后引用它时无法判断屏幕是否已变；那套 v1 管线在
  v2 里也成了死代码并被整体删除（commit `9968f8d`）。v2 让 grounding 只产出 `MarkCandidate`（0-1000 归一化
  坐标），由 session 在提交观测时铸成带批次的 mark——坐标永远不脱离它的批次。

## Consequences

- **收益**：跨设备一致（同一决策在任意分辨率下同义）；过期寻址结构性可检测，`resolve_mark` 直接拒绝，
  测试断言 stale mark 返回错误且设备调用数为 0；回执与 trace 用 mark 标签可读可复盘。
- **代价**：动作前必须先有观测（截图 + dump，多一次设备往返）；描述寻址需要解析器，必要时还要一次视觉
  locate（慢、依赖模型）；`swipe` 仍是裸坐标，是仅存的坐标风险面；`locate` 成功后旧 marks 全部失效，模型
  必须重新观测才能继续动作。
- **边界**：mark 只是「当前批次」的寻址凭据，不代表动作一定生效——观测之后屏幕仍可能变化，是否生效以设备
  回执为准（`pages/architecture.md`）。同理，`locate` 是兜底而不是常规路径：它的候选必须唯一才铸入新批次，
  零/多候选一律返回候选摘要而不执行。
