# Agent Note: AGENTS.md 层级补全——web/ 与 tests/ 子树就近约束

Status: implemented

## Problem

上一批落地 AGENTS.md 层级时（见 `2026-09-15-agents-md-hierarchy.md`），层级只建到 `phone_agent/v2/` 与 `pages/`
两棵子树，并在边界里写明「不为每个目录造一份 AGENTS.md」。这个边界留下两处读者找不到就近约束的子树：

- `phone_agent/web/` 由 `test` job 里的 `tests/web/` 覆盖（矩阵只按 Python 版本分腿、跑全量 `pytest tests`），但它的
  纪律不在本目录：控制台只是观察层、不拥有设备 / 工具 / 路由、被动读 App-KB 表、状态只由终局事件推进，这些
  **跨文件投影纪律**目前只写在根文件与 `pages/console.md` 的 `{#web-projection}` 锚点里。改
  `phone_agent/web/app.py` 与 `bridge.py` 的人要么上去翻根文件，要么根本不知道有这层约束——而 `bridge.py` 与
  `app.py` 之间的字段 / 状态词表漂移正是这套纪律要防的事。
- `tests/` 是唯一「写测试的人都必须进」的目录，却同样没有就近文件：分层跑检（改了哪层只跑哪层）、
  「`tests/docs` 标准库 + pytest、不装 `requirements.txt`」这条**成本约束**（CI `docs` job 便宜的根据，新增文档门禁
  测试不得引入第三方依赖）、以及文档登记双入口（词数预算与引用清单），都散在根文件的 Working Agreements、
  Environment Gotchas 与各门禁文件的 docstring 里。改测试的人不会为此回头翻根文件。

两个子树的规则都是「触发时机明确」型——打开目录改代码时才需要读到，正是就近约束的适用场景。

## Decision

- **新增 `phone_agent/web/AGENTS.md`**（740 词，上限 750）：读者是改 Web 前端与 runner IPC 的人。六条硬规矩回链
  根文件 P0 锚点（⑤ 的红线回链根文件 Data & Privacy）——①单向观察不反向拥有（#21）②状态只由终局事件推进（#21）
  ③展示性读取永远只读（#21）④流式只挂观察者（#22）⑤浏览器出站只有两路（#21）⑥可选是硬约束、headless
  `ThinPhoneAgent.run(...)` 必须始终可用（#21）；「局部约定」只收录跨文件事实：`bridge.py` 拥有状态与整份
  `RunPaths`、`app.py` 只渲染并如实显示缺失、改事件字段要两侧同批。**不写**启动命令与页面布局
  （那些的正文在 `pages/console.md`）。
- **新增 `tests/AGENTS.md`**（653 词，上限 750）：读者是写 / 改测试的人。六条硬规矩——①改了哪层只跑哪层
  ②只用 `.venv/bin/python -m pytest` ③离线是硬约束（不碰真机 / 网关，用 `tests/v2/_doubles.py` 的 fake 与 `tmp_path`）
  ④`tests/docs` 保持标准库 + pytest、不得 import `phone_agent` ⑤受管文档要在 `tests/docs` 的两个登记口立预算与登记
  ⑥断言要指向约束；「局部约定」逐文件列出 `tests/docs` 的六道门禁，并说明 `tests/web` 守护 Web 投影契约、
  `tests/skill` 是 skill 的离线冒烟。
- **登记进文档门禁**：两份新文件各按 750 词上限进 `tests/docs/doc-budgets.json`，并加入
  `tests/docs/test_doc_references.py` 的 `_DOCUMENTS` 清单——与 `pages/AGENTS.md`、`phone_agent/v2/AGENTS.md` 的既有
  写法一致，引用真实性与词数从此刻起由机器核对。
- **根 `AGENTS.md` 同步**：Doc Routing 表「进子树改代码」一行改列四棵子树的 `AGENTS.md`；「What This Is」里原本
  复述的 Web 边界（追加式文件观察、web 进程不拥有设备 / 工具 / 路由）删去，改为一句「纯观察层，边界见该子树
  `AGENTS.md`」——同一事实的正文归子树文件。根文件词数 2053 → 2035，仍在 2100 硬顶内，不需要提额。
- **Data & Privacy 的 Web 条目保留**：它写的是隐私红线（不给浏览器发 API key / 认证头 / 完整配置），与 `{#web-projection}`
  的投影边界是两件事，继续留在根文件作为跨层约束。
- 本文替代 `2026-09-15-agents-md-hierarchy.md` 中「层级只建到 v2/ 与 pages/」这一条边界结论；该笔记其余部分
  （层级动机、postmortem 归宿、两条 Working Agreements）继续有效（理由为回溯性重建）。

## Alternatives considered

**继续不建（维持 hierarchy 笔记的边界结论）**

- **最强的理由**：当时的结论是「层级只建到有独立纪律的子树为止」，避免规则分散成多份真相、也避免为每个目录
  造文件的维护成本；两棵子树的纪律已经通过根文件 P0 表与 `pages/console.md` 的锚点有了归宿，不建新文件也不会
  丢事实。
- **为何被否**：所有者明确要求把这两棵子树纳入，且它们各自满足「独立纪律」的判据——`web/` 有专门的测试层
  （`tests/web/`，随 `test` job 的全量 `pytest tests` 跑）与跨文件投影纪律（`bridge.py` / `app.py` / `run_events.py`
  三方字段与状态词表），`tests/` 有分层跑检纪律与「门禁测试不得引入依赖」这条成本约束。这些内容放在根文件里
  既不生效（改测试的人不会回头翻）也挤占常驻预算；边界结论因此在本批被推翻，而不是被遗忘。

**把 web 纪律并进 `phone_agent/v2/AGENTS.md`**

- **最强的理由**：`runner.py`、`run_ipc.py`、`run_events.py` 本来就住在 v2，Web 观察面与它们的耦合最紧；一个文件
  少一处维护点。
- **为何被否**：v2 子树文件已经 609/750 词，再加一轮投影纪律与隐私条目就会顶到上限，且读者是两拨人——改 v2 策略
  监听器的人与改 NiceGUI 页面的人是不同的触发时机。两个读者共用一个文件等于让其中一方读无关内容，正是层级化
  要解决的问题。

**为两棵子树合并一份 `tests/web/AGENTS.md` 式的第三入口**

- **最强的理由**：`tests/web/` 与 `phone_agent/web/` 守护同一套投影契约，读者常同时改两边，合并一份能少一个文件。
- **为何被否**：它们约束的对象不同——一个约束测试怎么写（离线、fake、登记），一个约束 Web 代码怎么写（不拥有设备、
  只读投影、只挂观察者）。合在一处会让「我该遵循哪几条」重新变成全体列表，就近约束的触发时机也就消失了。

## Consequences

- **收益**：`phone_agent/web/` 与 `tests/` 第一次有了触发时机明确的就近约束；Web 投影边界与测试成本约束从「散在
  根文件与 docstring」变成「进目录就读到」；两份文件的引用真实性与 750 词上限纳入 `tests/docs` 门禁，漂移可被机器
  发现；根文件因为删掉复述而**变短**（2053 → 2035），常驻成本不升反降。
- **代价**：需要维护的子树 AGENTS 文件从两份变四份，跨层约定改动要同时改根文件与子树文件，说法漂移的风险面变大；
  两份新文件目前只靠人工遵守，没有「子树文件必须在 Doc Routing 里被点到」这类门禁（现有门禁只核对词数与引用
  真实性）。
- **仍然脆弱**：`_DOCUMENTS` 是手工清单，新增第五棵子树时容易被漏登记；`doc-budgets.json` 的 750 与两份新文件的
  实际词数（740 / 653）都还留有余量，下一次内容扩张会先撞预算而不是先撞门禁。
