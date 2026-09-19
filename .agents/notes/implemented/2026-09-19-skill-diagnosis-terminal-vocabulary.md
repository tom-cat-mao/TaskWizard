# Agent Note: 诊断技能的终局词表与文档校正——启动器状态集补 `budget_exhausted`

Status: implemented

## Problem

一次对 `.agents/skills/phone-agent-live-diagnosis/` 的逐文件审计发现真 bug 与若干文档漂移：

- **真 bug（终局词表分叉，两处）**：生产侧 `phone_agent/v2/run_events.py::terminal_status` 早前把 IPC 终局状态从
  `token_budget_exhausted` 改名为 `budget_exhausted`，技能读取器（`scripts/events.py::TERMINAL_BUDGET`）已跟上，
  但启动器与 `analyze.py` 都没跟：
  - `scripts/run_diagnosis.py::_TERMINAL_STATES` 仍只列旧名。后果：预算耗尽的 run 其 IPC 状态
    `budget_exhausted` 不在终局集合里，`_status_from_harness` 把它标成 `incomplete`（monitor 打印出来的
    就是这个），`_exit_code_from_summary` 在 `wait`/`case` 返回 5（仍在跑）而不是 2（非成功结束）——一个
    已结束的 run 被报告成未结束。
  - `analyze.py::classify_verdict` 的 harness mapping 与 `build_budget` 的 `exhausted` 仍只认旧名：读取器
    把状态归一成新名后才交给 analyze，查表落空后撞上 `source == "runner_ipc"` 分支，真机预算耗尽 run 的
    verdict 恒为 `uncertain`（报告一边显示"不确定"一边显示 state=budget_exhausted），`budget.exhausted`
    也恒 False。
- **根因是覆盖面**：`_TERMINAL_STATES` 是这套协议里唯一没有契约测试的接缝。生产侧词表
  （`terminal_status`）、读取器常量、`run_end` schema 都有测试钉住，唯独启动器的接受集没有；改名时其他
  面都红过，只有它静默通过。
- **文档漂移（同批审计确认）**：SKILL.md 的终局词表仍写旧名并混入 `uncertain`（那是报告 verdict，不是
  `run_end` 状态）；"harness UsageLedger 未导出"的说法已过期（`run.json["usage"]` 按 role 导出，
  `scripts/events.py::run_summary_block` 已消费）；run-dir 布局漏了 `launch.json` / `runner.pid` /
  `runner.log`；流式事件（P0 #22）会落进 `events.jsonl` 却未被文档记录；`--quiet` / `--interval` /
  `--evidence` / `--output` 与 run-flag 家族未写进参考页；未说明 `python main_v2.py "<task>"` 不产生诊断
  证据、真机验收必须走技能启动器；HITL 只写了"绝不自动批准"，没写控制通道关闭时
  `ControlChannel.wait_for_hitl()` 兜底 `"reject"`；`references/source-map.md` 把
  `tests/skill/test_sourcemap.py` 说成覆盖整张映射表（实际只钉了部分 category）。

## Decision

技能侧修 bug + 校正文档，**不动任何 `phone_agent/` 生产代码**（v2 侧词表正确）：

- `_TERMINAL_STATES` 增加 `budget_exhausted`；`token_budget_exhausted` 仅作**防御性保留**——读取器
  （`events.py::harness_terminal`）已把旧 run 目录的 `run_end.status`/`reason` 归一成新名，该键当前不可达，
  删掉行为不变。
- `analyze.py` 的两处同源漂移一并修：`classify_verdict` 的 harness mapping 增加
  `"budget_exhausted": "budget_exhausted"`，`build_budget` 的 `exhausted` 接受新名；旧名键保留作防御。
  `classify_verdict` 末尾按 `outcome/terminal.reason` 判断的 fallback 路径不动——那里比对的是
  `RunResult.reason`，生产侧（`agent.py::_build_result`）从未改名，`"token_budget_exhausted"` 仍是正确拼写。
- **补上缺失的契约测试**（`tests/skill/test_runner_protocol_offline.py`）：枚举 `RunResult` 的所有 reason
  （成功 / `token_budget_exhausted` / `loop_fuse` / `error:` / `model_stopped` / `hitl_resume_exhausted` /
  会话 takeover）过**真实** `terminal_status()`，断言每个产出状态都属于从 `run_diagnosis` 导入的
  `_TERMINAL_STATES`；再把真实 `run_end` 事件喂给读取器，断言派生状态同样在集合内；最后钉住退出码 2（不是
  5）与旧拼写仍在集合里。`tests/skill/test_analyze_layers.py` 新增 budget 用例走真机数据流
  （`run_end.status="budget_exhausted"` → `verdict == "budget_exhausted"` 且 `budget.exhausted is True`），
  外加旧名 alias 的防御用例。任一侧**改名**，测试即红。
- 文档同步到事实：SKILL.md 写出真实 IPC 词表（`succeeded | takeover | budget_exhausted | loop_fuse |
  error | failed` + 读取器派生的 `stopped`；`uncertain` 只是无终局事件时的报告 verdict），补齐 run-dir
  布局与流式事件、CLI 旗标、`main_v2.py` 与启动器的诊断证据差异、HITL reject 兜底；`analyze.py` /
  `report.py` 里"未导出"的措辞改为"按 role 见 `run_summary.usage`，本块刻意不合并"（行为不变，
  `ledger_used_tokens` 保持 `None`）；`source-map.md` 把契约测试的范围收窄成实际覆盖面。

## Alternatives considered

- **只保留新名，删掉 `token_budget_exhausted`**：单一词表最干净。
  - **最强的理由**：没有两套拼写就没有再次分叉的空间，读者也少一个分支；且读取器已归一，删掉行为不变。
  - **为何被否**（原论证"旧目录会重新变成未结束"不成立，此为事后更正）：旧目录在读取器处已被归一成新名，
    该键当前不可达。保留它只是零成本防御——`_TERMINAL_STATES` 也可能被直接构造的 harness 块喂到，多一个
    别名不增加分支负担；因此定性为"防御性保留"而非"必要兼容"。
- **启动器直接 import 生产词表**（如从 `run_events` 取 `terminal_status` 的产出集合）：单一事实源，
  理论上不可能漂移。
  - **最强的理由**：词表只有一处定义，测试都不用写就永不漂移。
  - **为何被否**：接受集与 `terminal_status` 的产出并不相同——它还要覆盖读取器派生的 `stopped` 与旧拼写；
    且启动器有意只当 IPC 协议客户端（只 import 少量 helper，不碰生产内部），直接 import 会把诊断工具与
    实现细节绑死。用契约测试保证兼容，比共享常量更符合现有分层。
- **只扩读取器那条测试**（`test_terminal_status_vocabulary_matches_reader`）：已有测试最省事。
  - **最强的理由**：它已经在跑真实生产事件，扩展成本最低。
  - **为何被否**：它断言的是"读取器状态 == 期望字符串"，不涉及启动器的接受集；这次的漂移恰好完整落在
    两不管地带——读取器是对的、启动器是错的。要修的是"产出 ∈ 启动器集合"这条从未被断言的命题。

## Consequences

- **收益**：预算耗尽的 run 恢复终局语义（`monitor` 不再误标 `incomplete`、`wait`/`case` 退出 2，verdict 与
  `budget.exhausted` 同步正确）；任一侧**改名**，`tests/skill` 立刻红（枚举式契约覆盖改名，不覆盖生产侧
  新增分支——新分支仍可能静默漏网，仍需两侧同步）；参考文档与真实文件/旗标/词表一致，诊断不再对着不存在
  的文件或旧状态名找证据。
- **代价**：`_TERMINAL_STATES` 仍是手工维护的重复词表（有测试兜底，但没消除重复）；新增终局状态需要
  生产与启动器两侧同时更新（枚举式测试在缺一侧时不会自动红）；旧拼写是不可达的防御别名，可随时删；`report.py`
  预算行的文案变化只影响 HTML 呈现（`ledger_available`/`ledger_used_tokens` 行为未动）。
