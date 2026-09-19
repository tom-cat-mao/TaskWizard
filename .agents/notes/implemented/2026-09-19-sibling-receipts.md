# Agent Note: 同轮中间步骤证据回执——只改呈现，不动采样

Status: implemented

## Problem

thin-loop 每步一次模型调用，但一次调用可以带多个工具调用（同一轮的 sibling），由 execution-admission 监听器
串行执行。每个观测类工具成功都会回一张新截图 + 完整 marks digest（`tools/_obs.py::auto_observation`），而
transcript 侧只保留最新 `PHONE_AGENT_IMAGE_KEEP` 条含图消息（默认 2）。于是：

- 3 动作轮里，中间步骤的图必被后续观测挤出 keep 窗口，模型从未看到——设备截图、编码与上传全部白花；
- 更硬的一条：每次 `session.observe()` 都会重铸批次（epoch 前进，mark id 带 `@e<epoch>`），所以中间帧的
  mark id 在**下一次模型调用之前就已经失效**，它本来就不可能被用于寻址——那张图既看不到、也不能用；
- 同时，中间帧还会把动作前的那一帧（模型真正据以寻址的画面）挤出证据窗口，反而更糟。

可用的省法只有"少传"：P0 #15 规定观测只有 `session.observe()` 一个生产者、执行动作的观测是执行语义的一部分
（前台包名、失败判定、flow line、experience 都建立在它之上）；P0 #5 又要求失败与未知如实呈现。所以既不能
跳过观测，也不能把失败包装成成功。

## Decision

新增 `PHONE_AGENT_SIBLING_RECEIPTS`（`on`/`off`，默认 `on`）与一个**纯呈现层**策略：同一轮里非最后的观测类
工具不附截图与 marks 摘要，改回一行紧凑文本回执（`app=`、`screen#N`、`marks (K)`、一行结构差分），并在回执里
说明"批次已推进、旧 mark id 失效、要寻址先 `read_screen`"。

- **寻位缝（exact seam）**：`tool/execute` 瀑布上最内层的监听器（`v2/agent.py::_SiblingReceiptListener`）从
  LangGraph 状态里取最后一条带 `tool_calls` 的 AI 消息，得到本轮 batch 与当前调用在 batch 中的下标；若当前
  工具属于观测族（`v2/tools/_obs.py::OBSERVATION_TOOLS`，10 个：`tap`/`long_press`/`type_text`/`scroll`/
  `swipe`/`back`/`home`/`wait`/`launch_app`/`read_screen`）且其后还有观测族 sibling，就在工具体执行期间设
  `session.<SIBLING_RECEIPT_HINT>`；`auto_observation` 的成功分支读到提示即改渲染，`finally` 里清除。
  注册为最内层是有意的：被安全门拒绝或因终局跳过的调用根本走不到它。
- **采样不动**：`observe()` 照常完整执行（epoch、marks、重试、失败语义全部不变），只是这张图与摘要不进
  transcript。off 时监听器短路、hint 不设，transcript 与改动前一致。
- **末步与单步不变**：该轮最后一个观测 sibling、单动作轮、后面只跟 TaskDoc/finish/deliverable 的轮次照旧附
  完整截图与摘要——寻址基准必须完整出现在下一次模型调用前。
- **失败不降级**：观测失败仍走既有的失败文本（可带 unverified 参考帧），永不变成回执；末步失败时，中间回执
  仍指向它提交的 `screen#N` 并提示 `read_screen`，不发明新的观测路径。
- 边界：`locate` 不在观测族（它回的是视觉定位所用的同一帧，不是新批次）；插件挂载的未知工具一律按旧口径
  （保守失败）。结构差分的基线是 session 上一个私有 presentation 指纹（只记计数与前台包名，无额外设备 IO）。

## Alternatives considered

- **调大 `PHONE_AGENT_IMAGE_KEEP`**：最省事，一行 env 就能让中间帧活到模型看到。
  - **最强的理由**：不改代码路径，不动任何语义，窗口调大即可。
  - **为何被否**：token 与延迟随图片数线性上涨，买到的却是一张**无法寻址**的图（批次已失效）；窗口越大历史
    图越多，压缩与缓存也一起变贵。回执只花几十 token，且把"这一步提交了什么"讲清楚。
- **中间 sibling 直接跳过观测**：省掉真机上最贵的资源（截图 + a11y dump）。
  - **最强的理由**：既然图看不到、批次也要重铸，这一步采样看起来纯浪费。
  - **为何被否**：P0 #15 明确观测只有一个生产者，且执行动作的观测承载失败判定与 run 级事实；跳过会让"动作
    到底有没有生效"不可知，等于用不可观测换省钱。本方案只省上传，不省采样。
- **整轮只留一张图（含中间步不给摘要）**：把 keep 窗口收益吃满。
  - **最强的理由**：最多只新增一张图 + 动作前帧，token 最省。
  - **为何被否**：marks-first 要求模型看到**最新批次**才能继续寻址，末帧必须完整；末步失败后的兜底路径是
    `read_screen`，不能靠少给证据来"引导"。
- **中间帧只附 marks 摘要、不附图**：保留 mark id 供后续动作使用。
  - **最强的理由**：摘要便宜，寻址信息还在。
  - **为何被否**：这些 id 在下一次模型调用前已随批次重铸失效，贴出来只会诱导模型用一个注定 stale 的 id；
    如实说"批次已推进、先 read_screen"更诚实也更省。

## Consequences

- **收益**：3+ 动作轮不再产出与上传中间截图（每步省一张图的上传与 token）；动作前帧与末帧能同时留在窗口里，
  模型看到的是"操作前 / 操作后"两张真正有用的图。
- **收益**：回执带一行结构差分（marks/窗口/前台计数变化），给模型一个低成本的"这步有没有效果"信号。
- **代价**：中间步骤不再展示 marks digest，同轮后续动作要用 mark 寻址时必须先 `read_screen`（本来也必须，
  因为 id 已 stale）；诊断证据流里中间步骤只剩计数，没有摘要。
- **代价**：每次成功观测多写一个私有 presentation 指纹（几个整数）；`off` 时它照写，但模型不可见。
- **脆弱点**：观测族名单是手工枚举——新增观测工具若没登记，只会退化为完整观测（保守方向，不是错误）；
  batch 位置依赖"最后一条带 `tool_calls` 的消息"，读不到就按末步处理（fail-open 到完整观测）。
- **脆弱点**：判断只看声明的 batch。若后面那个观测 sibling 因安全预警或预算拒绝而未执行，该轮回执可能成为
  唯一证据；此时回执已经写明"本步观测已提交、`screen#N`、要寻址先 `read_screen`"，模型能沿既有路径取回证据，
  但看不到那张图本身。这是契约允许的代价，不是静默丢失。
