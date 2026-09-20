# Agent Note: 描述式寻址的短原文规则 + 匹配落空提示 + 验收裁决落盘

Status: implemented

## Problem

真机样本（合成描述、非真实设备画面）暴露三处相互独立的缺口，它们让同一次 run 的三个环节都失去了纠错能力：

1. **prompt 把模型带偏**。模型给 `tap` 的 `target_description` 写位置式长句（形如「搜索结果第一行
   『沈阳市』（带蓝色『城市』标签）」），而 `v2/resolver.py` 的描述解析只做精确 / 包含 / 归一化三级
   **文本**匹配：查询串必须整体出现在 mark 的文本或角色里，长句结构性必然零命中。同一份 system prompt
   对 `locate` 详述「外观 + 可见文字 + 相对位置」，对 `target_description` 只写「用自然语言描述目标」——
   两条寻址路径的说明粒度不对称，等于教模型把长句写进文本匹配。
2. **落空不可见**。零命中的描述会委派 `session.locate`（`v2/resolver.py` 的视觉兜底，实测单次约 40 秒
   级）。样本 run 的 5 次描述式 tap 全部穿透兜底并「成功」，回执只有 `OK. …` 一行，模型没有任何信号知道
   自己的写法一直在走最慢的路径，也就没有机会改。
3. **裁决不落盘**。验收器的 `Verdict.reason`（`v2/verify.py::_parse_verdict` 截 200 字）只回传给
   `v2/tools/control.py` 存内存，`run.json` 不含任何裁决字段：诊断分析只能对已结束的 run 报
   `verifier_status: "unknown"`，事后无法回答「验收器到底判了什么、为什么」。

## Decision

三处改动，边界如下：

1. **短原文规则写进两处模型可见的契约**（`v2/prompts.py` 的 cn/en system prompt、`v2/tools/actuation.py`
   的 `tap` / `type_text` 工具 schema）：`target_description` 只写控件上可见的短原文（如「沈阳市」），
   系统按文本包含匹配；位置 / 外观 / 颜色长句只属于 `locate`。两侧同时说明落空的后果（退回视觉定位，
   慢且可能失败）。
2. **落空在回执里可执行**（`v2/tools/actuation.py`）：判据是「解析出的 mark id 不在本次调用前的 mark
   集合里」——resolver 的文本三级只能返回当前屏上既有的 mark，零命中才委派 `locate`，而 `locate` 会开启
   新批次、铸入新 id，所以这条判据与「文本零命中」等价。命中该判据时成功回执追加一行 `TEXT_MISS_HINT`，
   描述类失败（视觉定位也没命中）的错误回执追加同一句；服务故障与瞬时失败两条分支不追加（那是基础设施
   或重试语义，不是写法问题）。动作照常执行——这是提示，不是拒绝，fail-closed 语义不变。
3. **裁决落盘**（`v2/verify.py` + `v2/tools/control.py` + `v2/runner.py`）：`Verdict` 增加
   `latency_ms`（验收器自身墙钟耗时）与 `usage`（记账到 `verifier` 角色的 token 数）；`reason` 在产出处
   统一脱敏 + 塌白 + 截 200 字，使回执、trace 与产物同一份文本。control 把裁决镜像到 session
   （`finish_verifier` 状态 + `finish_verifier_verdict` 记录），runner 在终局把它写进 `run.json`：
   未产出裁决时 `finish_verifier_verdict` 为 `null`，run 没有 session 可读时两个键都不写。

明确不做：不改 resolver 的匹配算法、不给长句加语义兜底、不为验收裁决新增 trace 正文、不要求诊断 skill 的
analyze 层渲染 reason。

## Alternatives considered

**放宽 resolver：长句 / 语义匹配**

- **最强的理由**：模型想怎么写就怎么写，最贴近「用自然语言描述目标」的字面承诺，也不用改 prompt。
- **为何被否**：语义匹配是在 marks 里「猜」，与 marks-first 的 fail-closed 相反；它还要引入新的模型调用，
  成本与延迟都高于视觉兜底。目标本来就完整地以短原文存在于 marks 中，规范化描述比放宽匹配便宜得多。

**文本落空直接 fail-closed 拒绝执行**

- **最强的理由**：最诚实——模型立刻收到错误，没有任何静默路径，也不会浪费 40 秒。
- **为何被否**：视觉兜底是目标确实没有文本（图标、图片按钮）时唯一可行的寻址方式，关掉它会让一批本来能
  完成的任务失败。保留能力、在回执里说清代价，比直接砍掉路径更符合「工具失败不回退成不可用」。

**只改 prompt，不动回执**

- **最强的理由**：改动最小，零风险，不碰执行路径与产物格式。
- **为何被否**：真机上同一条规则写进 prompt 后模型仍写长句——prompt 是事前约束，运行期还缺一个模型能
  看到、能据此立即改写的反馈信号。

**裁决写进 trace / evidence 流，而不是 `run.json`**

- **最强的理由**：trace 与 evidence 已有成熟的脱敏与滚动写入机制，复用即可。
- **为何被否**：裁决是**终局事实**（这次 finish 是被判通过还是驳回），与 `result` / `usage` 同层；trace 是
  逐步流水，诊断分析要拿到「权威裁决」还得再解析一遍。终局事实落在终局产物上。

## Consequences

- **收益**：模型有一条可自纠的路径（回执提示 + prompt 规则 + 工具 schema 三处同一条）；验收裁决首次成为
  可审计的 run 级事实（`approve` / `status` / `reason` / `latency_ms` / `usage`），诊断不再默认
  `unknown`；reason 的脱敏与截断收敛到产出一处，三个落点自动一致。
- **代价**：成功回执多一行文本，transcript 略长；「文本零命中」的判据依赖 resolver 的现行契约（文本级只
  返回既有 mark、视觉兜底必铸新 id），resolver 若改变返回形态，这条判据要跟着改；`finish_verifier_verdict`
  由 control 在产出裁决时写到 session 上，`session.py` 不在本工作包的文件范围内，因此它没有进 `Session.__init__`
  的属性清单——读契约要看 control / runner 两处；`run.json` 是本机私有运行产物，不进 Web 投影也不进 Pages，
  `finish_verifier_verdict` 里的 reason 仍只对本机可见；同一 run 多次裁决只留最后一次，需要「哪一次驳回」
  得看 evidence 流的驳回回执；诊断 skill 的 analyze 目前只消费状态字符串，`reason` / `latency_ms` 需人工读
  `run.json`。
