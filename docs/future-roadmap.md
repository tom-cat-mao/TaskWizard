# Future Roadmap（内部状态）

> 维护者视角的当前状态与延期项。硬性编码约束见根 `AGENTS.md`（P0 表）；模块契约以 `phone_agent/v2/`
> docstring 为准；**公开**能力状态在 `pages/roadmap.md`。特定批次的授权、设计与验收记录在
> `docs/execution/`——本文件不复制实施流水账。

**状态时间：2026-09-10。主修改分支 `fix/harness-contracts-20260909`，更新现有 PR #23；尚未合入 `main`，
线上只反映已合并内容。**

## 本分支已落地

- **Harness 契约修复 S1–S5**：`model/pre_request` 瀑布纯全列表变换 + 桥接唯一 `RemoveMessage`；token
  计量计工具参数/CJK/无 usage 双向；蒸馏游标 `(ts_end, run_id)` 失败重试；任务板结构预算；S3/S4 的
  provider-plugin 实链、Web 失败分类与 core 事件边界；失败构造清理。
- **U1/U2/U4 与窗口化 marks**：原子观测（单生产者、epoch 批次徽章、freshness gate、locate 新批）、
  安全预警制（`wary` 默认；`hard`/`reviewer`/`off`）、grounding 死代码删除、`MARKS_WINDOWED` 纯展示分组。
- **记忆与经验线**：经验数据面（WP-I/I1）、RAG shadow 回想（I2）、隐式纠正（I3）、经验提炼/晋升/
  受控回注（L/A3）、workflow 记忆与注入多源化（WF1–4）、蒸馏全自动（DISTILL-AUTO）、上下文与记忆
  加固（MEMFIX）。
- **装配与扩展**：能力挂载层（C2）、插件系统全量（PLUGIN A–E，`PLUGIN_API_VERSION=1` provisional）、
  模型提供方插件（PROVIDER：单层 models.json、可注册传输、五角色路由、`roles` 段、thinking 翻译）。
- **可用性恢复 B（参考图）**：观测失败但本次取得有效截图时返回标注过的未验证参考图，不提交
  epoch/`screen_seq`/几何、不铸 mark。merge `cf33cf4`，主审 + 整合测试通过。
- **可用性恢复 C（记忆诊断）**：`snapshot_missing`/`snapshot_corrupt`、撤销/版本/缺失独立 reason、
  rule 与 card 抑制计数独立、全 fail-open；主审缺陷（非 UTF-8 快照）已修并有真实 bytes 回归。
  merge `a8e7aca`。
- **IME 空参数修复**：base64 `--es msg` 负载 shell-quote，空 warm-up 参数不再被设备端 shell 吞掉；
  真实 ADB 回环 + 隔离 CWD 测试通过，尚未真机复测。
- **可用性恢复 A（fallback）**：可选 `PHONE_AGENT_FALLBACK_MODEL`（缺省/same-target/不可构建即惰性，不猜端点/密钥）；
  actor 构建失败与调用失败各降级一次、同目标不重复，备用按自身 metadata + 全局构建、不复制首选 role 参数；
  辅助角色仅构建期降一跳。运行时坏 models 声明逐项跳过 + 结构化 `declaration_warnings`（env gateway 保留，
  严格解析保留）；降级写 `model_fallback` 审计；compact 窗口按实际构建的 actor 推断。merge `557cb79`。
- **D 文档与 Pages**：README/AGENTS/内部 roadmap/Pages 手册/Pages workflow 整理；无生产源码改动。
  merge `1c1087f`。
- **F 控制台改版**：固定顶栏 + 视口高度两栏工作台（336px 设备栏、右侧紧凑任务输入与步骤/任务板/应用库/
  记忆/产出页签，HITL 置顶）；等待模型/工具/人工与「已请求停止」按真实事件区分，actual 模型只取
  provider 元数据；参考帧保留独立身份；观察型事件字段 + 桥接投影，无设备/agent/工作流改动。
  merge `811c39e`；界面由用户手动看 fake 预览后接受（不采用浏览器自动化截图验收），未在真机运行。

- **控制台启动修复**：App-KB 表刷新改为只读，快照按内容标识、漂移只审计；设备输入留空为显式自动。针对性验证 109 项通过。
- **模型 Streaming**：全局/模型/角色可配置，默认 off；SDK 聚合完整消息后才执行工具，Web 增量投影独立尝试并支持跟随/钉选。OpenAI Chat Completions/Responses、Anthropic、Google 离线协议验证通过；最终定点回归 **197 passed**（一条合成 Anthropic tool block 警告），ruff 与 MkDocs strict 通过。未进行真实网关流式或新 UI 浏览器视觉验证。

## 本轮整合状态（A/B/C/D/F 已合入本分支）

| 包 | 状态 |
|---|---|
| A 可用性 fallback | 已合入 `557cb79`，验收记录见 `docs/execution/availability-restoration-20260910.md` |
| B 参考图 / C 记忆诊断 | 已合入 `cf33cf4` / `a8e7aca`；IME 空参数修复有真实 ADB 回环证据，尚未真机复测 |
| D 文档与 Pages 手册 | 已合入 `1c1087f` |
| F 控制台改版 | 已合入 `811c39e`（含合成预览脚本 `scripts/fake_console_preview.py`）；界面为用户手动视觉验收 |
| 整合验证 | 清洁 archive（HEAD `a137891`，临时目录、共享 venv 只软链、`PYTHONPATH` 指向 archive）跑完整 tracked suite + ruff + `mkdocs build --strict`：**1393 passed / ruff 通过 / 站点构建成功**，2 条为既有 SWIG 警告。不自动合 `main` |

延期项（不在本轮范围）：全面初始化失败清理、`LessonStore` 读性能、插件 API 第二实现方验证。

## 延期项（明确不做，不无限新增阻塞）

- 全面初始化失败清理、`LessonStore` 读性能、插件 API 的真实第二实现方验证（S3 provisional 解除）。
- prefix-cache 优化（任务板版本化 + 图片批量折叠）；按任务复杂度的动态模型路由。
- marks `op=blocked` **不计划**直接用于执行门控（当前纯展示）；若未来要启用属于独立决策，需先完成真机验证
  与授权。成功先例回注（exemplar 三闸达标前不开工）。
- verifier 多帧输入（`K>1` 需历史帧保留能力）；compact 异步水位线；产出物进语义索引。
- 真机回归与短输入任务测试：手机保持停止，等用户明确授权后再启动。

## 证据入口

- `docs/execution/availability-restoration-20260910.md`：A/B/C 的范围、验收与合并记录（最新决定覆盖旧
  「provider 不允许 fallback」「观测失败只返回文字」要求）。
- `docs/execution/console-and-docs-refresh-20260910.md`：控制台与文档的本轮授权、职责与验收（控制台验收
  方式已由用户从「浏览器自动化截图」改为手动看图，最终结论见该文件末尾）。
- `docs/execution/harness-contract-repair.md`：S1–S5 的契约修复记录。
- `docs/archive/`：v1 时代历史日志，仅作证据，不当作现行规范。
