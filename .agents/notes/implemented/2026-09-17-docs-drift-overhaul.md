# Agent Note: 文档语义漂移大修——五路并行修复，锚点与 Module Map 转机器门禁

Status: implemented

## Problem

三路并行审计（机器门禁之外逐页通读）在入库文档里数出约 60 条语义层漂移，按读者损失分级：

- **高危 2**：`pages/roadmap.md` 正文把详细验收证据指向仓库 `docs/execution/`，该目录在磁盘上不存在；
  根 `AGENTS.md` 的 Module Map 漏掉 `recall.py`、`coords.py`、`usage.py`、`locate_scope.py`、
  `native_content.py` 与三个 middleware 模块——读索引的人找不到它们。
- **中危约 15**：读者按页面口径会走偏的契约描述——同一事实两页说法不一致、与源码行为不符的用法句一类。
- **低危约 45**：口径与措辞级偏差，单条不致命，但读者每页都会撞见。

与此同时，**机器门禁覆盖的那一层零漂移**：配置键三方一致、词预算、变迁词、bash 块可解析、引用路径存在性
全绿——基线 `main`（`bd454c1`）上 `pytest tests/docs -q` 实测 189 项全通过。结论很明确：漂移全部集中在机器
还看不见的语义层，而其中两类本可被机器看见——锚点存在性与 Module Map 覆盖：

- P0 表的 30 个锚点链接与页面内的互链（`#coords`、`architecture.md#app-name-resolution`、
  `../../pages/console.md#web-projection`）没有任何进程核对；`test_doc_references.py` 明确跳过含 `#` 的
  token，锚点 id 只靠"改标题时自己回头核对"这条口头约定。改一次标题、链一页悬空，站点深链与历史链接同时失效。
- Module Map 是人工维护的索引，新增模块没有任何提示会漏（本次 5 个顶层模块 + 3 个 middleware 模块就是证据）。

## Decision

一次大修覆盖**全部**三档漂移，不做"只止血"的部分修复：

- **五 worktree 并行**：按页与主题切分（`pages/` 契约页、配置与控制台、记忆与自进化、模型提供方与新页、
  门禁与索引），各自修复本路漂移，由协调者合并。
- **新增两页**：`pages/evolution.md`（自进化：经验平面、蒸馏、晋升、回注的现状契约）与
  `pages/providers.md`（模型提供方的声明、角色路由与故障降级），承接从 `memory.md` 与
  `configuration.md` 迁出的正文。
- **新增两道机器门禁**（`tests/docs/`，只依赖标准库 + pytest，随 docs job 跑）：
  - `test_doc_anchors.py`：核对文档里的 `target#anchor` 引用在目标文件确有 `{#anchor}` 显式声明；
    只认 attr_list 显式锚点，不猜 mkdocs 自动 slug；仓外目标只能进带理由的白名单。
  - `test_module_map.py`：`phone_agent/v2/*.py` 与 `middleware/*.py` 的每个公开模块必须被根 `AGENTS.md`
    点名（`v2/name.py` token、花括号简写，或 middleware 名册里的裸名）；`_` 前缀私有模块按规则豁免。
- **`docs/` 改为入库**：`.gitignore` 放开该目录。`docs/future-roadmap.md` 一直是入库的"当前实现状态"，
  同目录却因整目录规则不可见；漂移审计必须能引用仓库内的当前状态文档，口径写进版本管理。
- **历史批次不溯及**：决策笔记制度建立（2026-09-15）之前的批次不补写笔记，取舍与代价以制度建立后的笔记为准
  （这条规矩同时写进 `.agents/notes/AGENTS.md`）。

边界（明确不做）：不改 P0 表的条目与语义；不把整个 Module Map 改成生成物（简写与分组是给人读的）；
不引入第三方 markdown linter；两道新门禁只报告不自动改写。

## Alternatives considered

- **单分支顺序修复**
  - **最强的理由**：零合并冲突、每一步都能独立验收，review 与回滚都以一个 diff 为单位；文档改动号称"改字
    就行"，看起来不需要并行。
  - **为何被否**：漂移量跨 10+ 文件、约 60 条，顺序修复的路径长度与协调成本更高；这次改动天然按页切分
    （契约页 / 配置页 / 记忆页 / 新页 / 门禁），并行五路几乎不争同一文件，收益大于冲突成本。代价是共享文件
    （`mkdocs.yml`、`doc-budgets.json`、`configuration.md`）的冲突要由协调者统一消解。
- **只修高危与中危（约 17 条），低危留档**
  - **最强的理由**：低危约 45 条多是措辞与口径，逐条改的 reader-value 低，先止血最省钱；留档可以下次批量处理。
  - **为何被否**：这次约 60 条里 45 条低危正是历次"留档"的堆积——低危口径会随每次改动复利增长，下一次
    大修只会更贵；而且低危漂移的读者损失不是"小"，是"每页都看得见"。
- **开三个新页，含「完成与验收」页**
  - **最强的理由**：`verify` / `review` / finish 复核包的事实量够独立成页，一页一主题与"一事实一归宿"一致，
    也顺手解决 architecture.md 的体量。
  - **为何被否**：那部分事实与 `architecture.md`、新的 `evolution.md` 会三处重复，在收敛完成前开页等于把
    重复固化；本次先开两页（`evolution.md`、`providers.md`），验收页等重复收敛后再评估。

## Consequences

- **收益**：三类漂移清零；锚点存在性与 Module Map 覆盖从"人工约定 + 口头核对"变成每个 PR 的机器事实，
  悬空深链与漏记模块会在改动的同一个 PR 里变红；`memory.md` 与 `configuration.md` 迁出正文后预算解压，
  两页回到各自主题；`docs/` 入库后"当前实现状态"与仓库同版本可见，审计引用不再指向幽灵路径。
- **代价**：两个新页是长期维护责任——进 nav、进词预算清单、进引用与锚点门禁，新增事实要先决定落哪页；
  `#lesson-injection`、`#lesson-evolution`、`#distill-promote` 三个锚点随正文迁页，是对外契约变更，根
  `AGENTS.md` 的 P0 表链接必须同批改向，否则老深链失效；五路并行的合并冲突集中在
  `mkdocs.yml` / `doc-budgets.json` / `configuration.md`，需要协调者按"后合并者让位"的次序消解。
- **仍然脆弱**：门禁只覆盖锚点与模块名两类，中危里的"跨页事实互斥"、低危里的口径漂移仍只能靠人读出；
  `test_module_map.py` 只要求模块被点名，不要求点名在正确的 Map 行（点名了但归错行的模块仍然通过）；
  锚点门禁只认显式 `{#id}`，页面若改用 mkdocs 自动 slug 深链，门禁会拒绝而不是跟随——这是刻意的：
  自动 slug 会随标题翻译漂移，不适合当对外契约。
