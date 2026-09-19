# Future Roadmap（内部状态）

> 维护者视角的当前状态与延期项。硬性编码约束见根 `AGENTS.md`（P0 表）；模块契约以 `phone_agent/v2/`
> docstring 为准；**公开**能力状态在 `pages/roadmap.md`。特定批次的授权、设计与验收原始记录是维护者
> 本机的私有文档（不入库，clone 后不可见）——本文件不复制实施流水账。

**状态时间：2026-09-19。主分支 `main`，HEAD `a24f9c8`（Merge PR #31）。除「SoL 启发效率包」为在途 PR（`feat/sol-efficiency-pack`）外，本文列出的能力全部在 main 上。**

## 已落地

- **SoL 启发效率包**（feat/sol-efficiency-pack）：观测存档与只读召回（`obs_archive` 能力，默认 off；`session.observe()` 提交成功即落 `[OBS]` 全文到 `memory/obs_archive/<run_id>.jsonl` + 可重建 FTS5 索引，`recall_screen`/`search_screens` 只读工具，历史 mark id 一律渲染失效；文本面，截图不落盘不动 P0 #6）；边界感知在线压缩（`boundary_compact` 能力，默认 shadow；`update_task_doc` 的 `in_progress→completed` 发 `taskdoc/completed`，harness 机械经济门 horizon×净收益 vs 摘要成本，复用 `_commit_fold` 全部既有门，T1/T2 容量兑底逐字不动）；摘要逐字命中影子指标（`compact_summary_quote_check` trace 事件，只观测不拦截）；同轮中间 sibling 证据回执（`PHONE_AGENT_SIBLING_RECEIPTS` 默认 on，非最后观测 sibling 只回文本回执，采样/epoch/marks 全不动，纯呈现层）。决策笔记三篇同批入库。
- **Harness 契约修复 S1–S5**（PR #23）：`model/pre_request` 瀑布纯全列表变换 + 桥接唯一 `RemoveMessage`；
  token 计量计工具参数/CJK/无 usage 双向；蒸馏游标 `(ts_end, run_id)` 失败重试；任务板结构预算；S3/S4 的
  provider-plugin 实链、Web 失败分类与 core 事件边界；失败构造清理。
- **U1/U2/U4 与窗口化 marks**（PR #23）：原子观测（单生产者、epoch 批次徽章、freshness gate、locate 新批）、
  安全预警制（`wary` 默认；`hard`/`reviewer`/`off`）、grounding 死代码删除、`MARKS_WINDOWED` 纯展示分组。
- **记忆与经验线**（PR #23）：经验数据面（WP-I/I1）、RAG shadow 回想（I2）、隐式纠正（I3）、经验提炼/
  晋升/受控回注（L/A3）、workflow 记忆与注入多源化（WF1–4）、蒸馏全自动（DISTILL-AUTO）、上下文与记忆
  加固（MEMFIX）。
- **装配与扩展**（PR #23）：能力挂载层（C2）、插件系统全量（PLUGIN A–E，`PLUGIN_API_VERSION=1`
  provisional）、模型提供方插件（PROVIDER：单层 models.json、可注册传输、五角色路由、`roles` 段、
  thinking 翻译）。
- **可用性恢复 A/B/C**（PR #23）：`PHONE_AGENT_FALLBACK_MODEL`（缺省/same-target/不可构建即惰性，不猜
  端点/密钥；actor 构建失败与调用失败各降级一次、同目标不重复；辅助角色仅构建期降一跳；降级写
  `model_fallback` 审计）；观测失败但本次取得有效截图时返回标注过的未验证参考图（不提交
  epoch/`screen_seq`/几何、不铸 mark）；记忆诊断 `snapshot_missing`/`snapshot_corrupt`、撤销/版本/缺失
  独立 reason、rule 与 card 抑制计数独立、全 fail-open。
- **IME 空参数修复**（PR #23）：base64 `--es msg` 负载 shell-quote，空 warm-up 参数不会被设备端 shell
  吞掉；真实 ADB 回环 + 隔离 CWD 测试通过，尚未真机复测。
- **文档与 Pages**（PR #23）：README/AGENTS/内部 roadmap/Pages 手册/Pages workflow 整理；无生产源码改动。
- **控制台与 Streaming**（PR #23）：固定顶栏 + 视口高度两栏工作台（336px 设备栏、右侧紧凑任务输入与
  步骤/任务板/应用库/记忆/产出页签，HITL 置顶）；等待模型/工具/人工与「已请求停止」按真实事件区分；
  App-KB 表刷新改为只读，快照按内容标识、漂移只审计；`PHONE_AGENT_STREAMING` 全局/模型/角色可配置、
  默认 off，SDK 聚合完整消息后才执行工具，Web 增量投影支持跟随/钉选。
- **CI 与文档治理**（PR #27/#28/#29）：P0 表降级为索引、契约正文归 `pages/`、决策笔记机制；lint/docs/test
  三门流水线、`requirements.lock` 为 CI 真相源、决策笔记软门与 tag 发版；AGENTS.md 层级化（v2/、pages/、
  web/、tests/ 就近约束）、四道文档机器门禁、覆盖率进报告不设阈值。

## 整合验证

清洁 archive（临时目录、共享 venv 只软链、`PYTHONPATH` 指向 archive）跑完整 tracked suite + ruff +
`mkdocs build --strict`：**1393 passed / ruff 通过 / 站点构建成功**，2 条为既有 SWIG 警告。控制台界面由
用户手动看合成预览后接受（不采用浏览器自动化截图验收），未在真机运行。模型 Streaming 的最终定点回归
**197 passed**（一条合成 Anthropic tool block 警告），未进行真实网关流式或新 UI 浏览器视觉验证。

## 延期项（明确不做，不无限新增阻塞）

- 全面初始化失败清理、`LessonStore` 读性能、插件 API 的真实第二实现方验证（S3 provisional 解除）。
- prefix-cache 优化（任务板版本化 + 图片批量折叠）；按任务复杂度的动态模型路由。
- marks `op=blocked` **不计划**直接用于执行门控（当前纯展示）；若未来要启用属于独立决策，需先完成真机验证
  与授权。成功先例回注（exemplar 三闸达标前不开工）。
- verifier 多帧输入（`K>1` 需历史帧保留能力）；compact 异步水位线；产出物进语义索引。
- `setup.py` 的 `console_scripts` 入口 `phone-agent=main:main` 指向仓库里不存在的顶层 `main.py`（v1 入口
  已删除），而 `setup.py` 只声明 `find_packages()`、没有 `py_modules`，顶层单文件 `main_v2.py` 不进安装
  产物；修复属独立决策——补 `py_modules` 并把入口指到 `main_v2:main`，或删掉该 `entry_points`。
- 真机回归与短输入任务测试：手机保持停止，等用户明确授权后再启动。

## 证据入口

- 入库可查的证据是 git 历史里 PR #23 及其后各 PR 的 merge commit，与代码本身：对应模块的 docstring 与测试。
  批次级的授权、评审与验收原始记录是维护者本机的私有文档，不入库。
- 两条现行行为由代码与 `pages/` 契约定义：`PHONE_AGENT_FALLBACK_MODEL`（见[配置参考](pages/configuration.md)）
  与失败参考图（见 `pages/architecture.md`）。
- v1 时代的历史日志只在本机保留，仅作历史证据，不当作现行规范；v1 约定以根 `AGENTS.md` 的 Module Map
  与 P0 表为准。
