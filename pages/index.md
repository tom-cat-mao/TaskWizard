# TaskWizard

TaskWizard 是一个 LLM 驱动的安卓手机操作 Agent。模型通过工具逐步观察并操作真实设备；运行壳（harness）
提供工具、安全边界、上下文管理与运行记录，不替模型规划流程。

## 能力总览

| 能力 | 机制 |
|---|---|
| 界面元素定位 | 执行动作必须绑定当前屏幕的 mark；目标过期、歧义、未命中时工具拒绝执行并返回原因 |
| 窗口化 marks | 屏幕按窗口分组呈现（弹窗/背景分层），标注可操作性（confirmed/likely/blocked/unknown）；默认 `auto` 采集 `uiautomator dump --windows`，设备不支持自动回退 |
| App 名解析 | 四层解析：归一化 → 多路候选（精确/词汇/拼音/嵌入）→ 先验排序 → 证据分型三态；弱证据只给候选不执行 |
| 视觉定位 `locate` | 本地视觉模型，默认原分辨率输入；支持可选的容器/锚点区域裁剪；命中开启新的观测批次 |
| 观测与参考图 | 原子观测（前后台一致 + 单次采样）；观测失败但已取到有效截图时返回标注过的未验证参考图 |
| 安全门控 | 风险动作先返回预警，模型显式确认后才执行；支持四档模式（默认 `wary`，不是人工付款门） |
| 完成验收 | finish 两段式确认；高风险目标由独立上下文的验收器复核，验收器故障 fail-open 并记 `skipped` |
| 成本控制 | token 预算硬上限；上下文接近窗口时两级 auto-compact |
| 应用记忆 | App-KB 记录本机应用与别名，按设备隔离；隐式纠正自动学习、dream 覆盖错别名、用户纠正入口最高优先 |
| 经验记录 | 每次运行落盘结构化档案（episode），固定 schema + 隐私白名单过滤 |
| 经验蒸馏与回注 | 离线 `--distill` 自判分级（auto_approved / needs_review）；`MEMORY_RAG=on` 时受控注入 approved / auto_approved 经验，有上限、可撤销 |
| 经验回想 | 本地向量索引（sqlite-vec + MLX 嵌入）默认 shadow 模式召回历史经验，自动统计命中率 |
| 模型提供方 | 默认零配置走 `.env` 网关；多提供方用单层 `models.json` + 每角色 `provider:model` 路由 |
| 能力注册表 | 内建能力经统一装配层挂载、按依赖应用、每次运行快照可审计 |
| 插件系统 | 策略层全事件化；插件经 entry points 或 `plugin add` 挂入同一装配层，API provisional v1 |
| 控制台 | 本地 Web 界面：实时画面、步骤时间线、任务板、应用库、记忆页、产出页 |

## 入口

- [快速开始](quickstart.md)：安装到跑通第一个任务
- [配置参考](configuration.md)：全部 `PHONE_AGENT_*` 配置项（唯一详细手册）
- [架构](architecture.md)：thin-loop 循环、原子观测与约束
- [Web 控制台](console.md)：界面说明与本轮改版方向
- [安全模式](safety.md)：四档门控与 finish 验收
- [记忆与自进化](memory.md)：App-KB、episode、RAG、蒸馏与回注
- [插件开发](plugins.md)：事件目录、payload 契约、manifest 与安全模型
- [路线图](roadmap.md)：能力状态与后续方向

## License

Apache License 2.0
