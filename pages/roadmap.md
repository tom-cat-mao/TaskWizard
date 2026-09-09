# 路线图

## 已完成

| 模块 | 内容 |
|---|---|
| thin-loop v2 | 工具环替代图编排；v1 LangGraph 架构已删除 |
| 原子观测 | 单生产者 + epoch 批次徽章 + mark 新鲜度闸门；dump 失败触发重试并在观测文本标注 |
| 安全 | 预警制（wary 默认）、finish 两段式、独立验收器 |
| 成本 | token 预算上限、两级 auto-compact（摘要携带记忆/能力状态）、cached_tokens 与 first-diff 计量 |
| App 名解析 | 四层解析（归一化→多路候选→先验排序→证据分型三态决策）；`exact_package_segment` 设备事实证据；拼音/嵌入/模糊永不单独自动执行；typed 默认 + legacy 可回退 |
| App-KB | 设备事实 + learned 别名（验证启动写回 + 隐式纠正）+ dream 整理与错误别名覆盖（秒退+自述签名）+ 用户纠正入口（`--learn-alias`/`--forget-alias`，最高信任级） |
| 窗口化 marks | `uiautomator dump --windows` 双模式采集（auto 回退）；窗口分组渲染 + 可操作性四档（confirmed/likely/blocked/unknown）；名额按窗口配额，顶层弹窗保底 |
| locate | 原图输入、hint-first、可选 scope 区域裁剪；连击 id 不碰撞；命中开启新观测批次 |
| 产出物 | `write_document`/`update_document` 产出单页 HTML（攻略/计划/报告）；路径由 run id 派生，控制台「产出」页预览/删除 |
| 经验档案 | episode 记录（隐私白名单）+ UsageLedger 持久化 + dream 归档 |
| 语义回想 | sqlite-vec + 本地 MLX 嵌入（全量精度）；run 结束自动增量索引；别名/档案分榜召回；Hit@1 等新口径统计 |
| 经验提炼与晋升 | `--distill` 离线蒸馏（水位线批次、语义字段输出 + harness 客观校验与簿记、无失败锚定/复现硬门槛）、两类候选统一自判分级（auto_approved / needs_review）、人工 CLI 纠正通道、版本链撤销 |
| 经验回注 | `MEMORY_RAG=on` 时注入 approved / auto_approved 经验（参考提示身份、上限、scope 过滤、可审计） |
| 过程卡注入 | 三时机：run 开局通用卡、goal mention 预取（≤2 个 resolved App）、进场送卡 + app 规则（launch_app 成功或前台包检测，系统包过滤名单可叠加）；每包每 run 去重；`replay --channel procedure` 离线评估 |
| 成功先例离线评估 | `python -m phone_agent.v2.replay`：时间序内存重放 episode 日志，coverage/relevance/steps-delta 三闸为"是否注入成功先例"提供离线证据 |
| 能力体系 | 注册表 + apply/release 挂载层（装配 reconcile、依赖可见、run 快照审计、紧急撤销通道） |
| 运行隔离 | runner 子进程执行、控制台重启不中断任务、断线重连回放 |
| 观测加固 | FLAG_SECURE 黑屏检测、观测静置（全局 + 按动作可选 settle_ms） |
| 控制台 | 步骤时间线、钉帧回看、任务板/应用库/记忆/产出页、软停止、每轮配置覆盖 |
| 实机诊断 | 诊断 skill：证据流 + 截图落盘 + 逐步回放 HTML 报告 + 源码归因 + 录屏；解析路/召回对照/别名生命周期维度 |
| 模型提供方插件 | 单层 models.json 注册表（项目 `.taskwizard.models.json` / `PHONE_AGENT_MODELS_FILE`），`openai-completions`/`anthropic-messages`/`google-generative-ai` 三种 API 构建器；五角色 `provider:model` 路由（actor/memory/verifier/safety_reviewer/distill 回落链不变）；roles 段每角色调用配置、DSH 式传输注册表；`PHONE_AGENT_THINKING` 按 `thinkingLevelMap` 翻译各家思考配置；`--list-models`；零配置与旧版逐字段一致；第三方插件可注册 provider |
| 上下文与记忆加固 | model/pre_request 瀑布改为纯全列表变换（桥接器统一铸唯一 RemoveMessage，杜绝监听器 RemoveMessage 吃掉摘要/任务板）；token 计量算工具调用参数 + CJK 感知 + 无 usage 时 input+output 全计；compact 窗口计入 schema/output 预留；蒸馏游标 `(ts_end, run_id)` 失败可重试（3 次放弃留痕）；撤销后同 id 重提自动降格；规则按渲染行成本装箱；过程卡投递前对权威 lesson 视图复检（撤销/版本不一致即抑制）；replay 口径正名（app_grounded_hit_rate、重叠 run 不前视、calibrate 不自动改常数）；任务板结构预算（字段长度上限 + 渲染截断） |

## 进行中

- 经验数据积累与回想命中率观测（控制台「记忆」页，新口径 Hit@1 / 污染率）

## 下一步

| 方向 | 状态 |
|---|---|
| 成功先例回注（run 开局注入同类成功 episode） | 离线三闸达标前不开工；等 replay 指标提供证据 |
| prefix-cache 优化（任务板版本化 + 图片批量折叠） | 评审文档已备，待批准后开工 |
| marks 可操作性进执行（op=blocked 执行前 fail-closed） | 待窗口分组真机验证后开工 |
| 产出物进语义索引（攻略可复用召回） | 待产出物积累 |
| 强弱模型路由 | 按任务复杂度分流，降成本。角色级路由已由模型提供方插件交付（每角色 `provider:model`）；按任务复杂度动态分流仍是远期 |

演进原则：先记录、再影子验证、蒸馏自判分级 + 人工 CLI 纠正；注入有上限、可撤销；记忆不旁路安全与验收。
