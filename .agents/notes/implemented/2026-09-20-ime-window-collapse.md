# Agent Note: 输入法窗口折叠——键盘键位不占 mark 配额，节点标签以 text 为准

Status: implemented

## Problem

真机 dump 暴露两个各自独立的展示层缺陷，都由同一次排查牵出来：

- **配额饥饿**：软键盘窗口（`TYPE_INPUT_METHOD`）的每个键位节点都是 clickable，于是作为普通候选进入
  `accessibility.py` 的候选收集。键盘一开，80 条 retained marks 里 68 条被键位图标吃掉，前台 App 只剩 8
  条——目标控件明明就在 dump 里，模型却看不到，只能退回约 43s 的视觉定位。逐键 tap 不是本 harness 的动作
  面：文本输入走 `type_text` 工具，键盘键位对寻址没有价值。
- **标签被挤占**：`_node_text` 把 `text` 与 `content-desc` 用 `" | "` 合并。有 App（携程）把 resource-id
  状的长 `content-desc` 写进节点，合并后这串东西跟着进入 32 字的摘要展示预算，正文标签被挤掉。

两条都属纯展示层，但影响执行路径：模型看不见 mark 就必须换更贵的感知手段（P0 #2 的 marks-first 前提被削弱）。

## Decision

**折叠点选在候选收集层**（`phone_agent/grounding/accessibility.py::_parse_uiautomator_xml` Phase 1），
不是配额层、也不是摘要层：

- 窗口 `type` 为 `TYPE_INPUT_METHOD` 时，只有**可能的动作键**仍然是候选：`text` / `content-desc` 命中动作
  词表（搜索 / 完成 / 发送 / 确定 / 前往 / 下一步 / GO / Search / Done / Enter / Next，大小写不敏感），或
  节点带真实 IME action（`ime-action` 类属性，值不是 `none` / `unspecified` / `0` / `1`）。其余节点在进入
  配额前整批丢弃。
- **丢弃就是丢弃**：绝不合成替身 mark（P0 #2 —— mark 必须映射真实节点）。动作键留下的 mark 仍带它自己
  节点的真实 bounds / window / 包名。
- **判定只看真窗口 type**：弱窗口（legacy `<hierarchy>` 推断窗）没有真 `type`，折叠永不触发，legacy 单根
  路径逐字节不变。
- `parse_summary` 新增两个纯计数字段：`ime_window_count`（本帧识别出的键盘窗口数）与
  `ime_collapsed_key_count`（被折叠掉、且本来会成为 mark 的键位数）；`total_candidates` /
  `per_window_counts` 都是折叠后的口径，模型看到的 `marks (K/total)` 不承诺不可寻址的键位。
- **呈现**：`phone_agent/v2/tools/_obs.py::_keyboard_collapse_note` 在 marks 摘要上一行渲染
  `keyboard: open (N keys collapsed)`。它只是计数注解，不含任何 mark id，不产生可寻址对象；没有折叠时这一
  行不存在，`[OBS]` 文本与改动前逐字节一致。
- **标签优先级**：`_node_text` 有 `text` 就用 `text`，`content-desc` 只作为无 text 节点的标签；节点级上限仍
  是 `MAX_TEXT_SUMMARY_CHARS`（120），摘要行的 32 字预算由真实标签独占。

## Alternatives considered

- **只调大 `PHONE_AGENT_ACCESSIBILITY_MAX_MARKS`（80 → 160+）**：最省事，一行配置。
  - **最强的理由**：不动解析逻辑，且键盘键位若真有用也一并保留。
  - **为何被否**：这是容量思路，而 80 条配额的设计意图是"留给当前可操作的控件"；调大后多出来的仍是模型
    永远用不上的图标，token 与延迟却按比例上涨。用户在两条路线里已拍板折叠（路线 C）。
- **只在配额层限流（给 IME 窗口一个上限，如最多 6 条）**：改动面最小，只动
  `_select_by_window_quota`，其余逻辑不碰。
  - **最强的理由**：保留键盘动作键，同时不再让键盘吃掉整块预算。
  - **为何被否**："最多 6 条"没有语义依据，字母键仍会占住候选与诊断口径；而且候选收集层的
    `total_candidates` / `per_window_counts` 仍会把 68 个键位算进去，模型读到的是一个虚高的总数。折叠到
    "只剩动作键"才是这条边界的真实语义。
- **按 IME 包名折叠（包名含 `inputmethod` 等）**：某些 dump 可能缺 `type`，包名是第二条信号。
  - **最强的理由**：真机上缺失属性时仍能识别键盘，鲁棒性更好。
  - **为何被否**：折叠是"整窗丢弃"的高影响动作，包名启发式会误伤名字里带 input 的普通 App；只用真窗口
    type（强证据）与可操作性四档的"强/弱证据"口径一致。缺 type 时退化为旧行为（保守方向），不是错误。
- **在摘要/呈现层过滤键位 mark**（如 `select_marks_for_digest` 里剔除）：
  - **最强的理由**：完全不碰解析器，只改呈现，回归面最小。
  - **为何被否**：饥饿发生在解析器的 `max_marks` 截断处——到摘要层时前台 App 的候选已经被挤掉了，在这里过
    滤等于"少显示几个键位"，配额仍然回不来，缺陷原样保留。
- **`text` 与短 `content-desc` 合并、超 32 字再丢弃**：折中方案，保住 desc 可能提供的额外线索。
  - **最强的理由**：图标按钮的 `text` 常常为空或很短，desc 有时是更准确的说明。
  - **为何被否**：这正是本次缺陷的形态——"合并"没有客观裁断标准，而模型读到的就是裁断后的字符串。规则越
    简单越可预测（有 text 用 text），无 text 节点仍回退到 desc，信息没有净损失。

## Consequences

- **收益**：前台 App 的控件回到配额内（离线用例：12 个控件从"只剩配额保底的 8 条"变为全保留），模型不再为
  dump 里已有的控件走视觉定位。
- **收益**：`keyboard: open (N keys collapsed)` 让"marks 变少"有解释——否则模型会把折叠读成"控件消失了"；
  诊断与回放也能直接看出这一帧有键盘。
- **代价**：模型看不到键盘键位 mark。逐键点击本就不在本 harness 的动作面（输入走 `type_text`），需要按特定
  键时只能靠 `type_text` 或视觉定位。
- **代价**：动作键是白名单。词表之外的新式按键（换行、emoji、剪贴板）一并折叠，扩白名单要动常量。
- **代价**：折叠发生在 bounds 解析与 dedup 之前，键盘窗口的节点不再计入 `bounds_parse_fail_count` /
  `filtered_zero_area_count` 等诊断计数——这些计数描述的是"进入候选的节点"。
- **代价**：`pages/architecture.md` 的词数预算从 6300 提到 6500（`tests/docs/doc-budgets.json`）。该页原本只剩
  8 词余量，而本节要写清折叠边界与注解行的存在；选择提预算而不是删既有契约正文，因为后者会与并行工作包
  抢同一段文字。
- **代价**：历史帧被 OBS marks 折叠处理后，占位符会保留 keyboard 注解行（两行，而不是一行）；注解不参与
  幂等判定，重跑不会二次改写。
- **脆弱点**：判定完全依赖窗口 `type`。厂商 dump 若不给 `type`，折叠不生效，退化为改动前的口径（保守方向，
  不是错误）；`PHONE_AGENT_MARKS_WINDOWED=off` 的 legacy 路径同样不受影响。
