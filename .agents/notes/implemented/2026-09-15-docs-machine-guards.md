# Agent Note: 文档四道机器门禁——手册只写现状，笔记格式、配置键与 bash 块由机器裁决

Status: implemented

## Problem

`pages/`、`README.md`、`AGENTS.md` 与 `.agents/notes/` 是入库并发布的读物（站点由 `mkdocs build --strict`
构建），但机器裁决此前只覆盖其中一半：`tests/docs/` 原有两道门禁——词预算（`test_doc_budgets.py`）与
引用可解析（`test_doc_references.py`）。剩下四类腐化没有任何进程读得出来，且都不是"写错一次"，而是
**没有所有者**：

- **手册会攒下历史叙述**：`pages/*.md` 里的"以前 / 不再 / 改名为 / 现已 / 旧版 / 新版 / 原本 / 曾经"把
  页面钉在某个改动日期上；读者从没见过旧状态，比较句对他零信息，却会随每次改动留下。落地实扫（基线
  `main`）：5 页共 12 处命中（`architecture.md` 2、`configuration.md` 3、`memory.md` 2、`plugins.md` 1、
  `safety.md` 4），最终 9 处加行级豁免、4 处改写为现状陈述，其中 1 处改写后同行仍有命中、同时带豁免。
  它们未必都该删——"耗尽后不再付摘要调用"是规则——问题在于**读到时已无从判断是规则还是历史**。
- **笔记格式只有文字约束**：`.agents/notes/AGENTS.md` 规定了文件名 `YYYY-MM-DD-slug.md`、`Status:` 与
  所在文件夹一致、四格顺序（Problem / Decision / Alternatives considered / Consequences）、严禁全局
  `INDEX.md`。既有的 `notes-check.yml` 软门只提醒"该写笔记"，不检查"写对没有"。
- **配置键三处靠人同步**：`phone_agent/v2/config.py` 加 `phone_agent/v2/providers/` 是真相源，
  `pages/configuration.md` 是手册，`.env.example` 是模板。任一处漂移都不报错：手册写了不存在的键、新键
  没进手册、模板列了没人读的键。
- **bash 围栏块是"复制即可执行"的承诺，却没有进程解析过它**：占位符与残缺引号在 review 里读不出来，
  读者粘贴才发现。落地时实测：`README.md` 与 `pages/console.md` 共 4 个 bash 块含
  `<serial>` / `<run_dir>`（分别落地于 2026-09-12 与 2026-09-13 的改动）。

## Decision

四道门禁全部作为新文件放进 `tests/docs/`，只 import 标准库与 pytest，因此随 `ci.yml` 的 docs job
（`pytest tests/docs -q`）在**每个 PR** 上执行：不需要新 workflow、不需要新 job，也不破坏"docs job 不装
`requirements.txt`"这条约束（它是 docs job 便宜且总在跑的根据，见
`.agents/notes/implemented/2026-09-15-ci-three-gate-pipeline.md`）。

- **`tests/docs/test_docs_freshness.py`**：`pages/*.md` 拒 8 个变迁词。语义词混写的行为规则用
  `<!-- allow:词 -->` 行级豁免：注释与命中词同行（或紧邻上一行）才生效，源码里可见、渲染不可见、不计词
  预算，因此它是一句"这行是规则"的显式声明，而不是让整个页面失声。豁免只清它列出的词。
- **`tests/docs/test_notes_format.py`**：文件名（真实 ISO 日期 + 小写 kebab）、恰好一行 `Status:` 且与
  文件夹一致（`git mv` 即状态迁移）、四格齐全且有序、`implemented/` 笔记不得出现
  Proposal / 计划 / TODO / 路线图 一类承诺未来工作的标题、`.agents/notes/` 下不得存在 `INDEX.md`。
- **`tests/docs/test_config_keys_sync.py`**：三方一致性。页面只能写全树 `*.py` 里真实出现的键字面量
  （注释与 docstring 不算读取）；config plane 读的键必须进手册，例外只能进 `UNDOCUMENTED_OK` 且每条要
  一行理由、并验证它仍然既未文档化又真的被读取；`.env.example` 列的键必须有读取方。**反向不强制**：
  保留的 no-op 兼容键刻意不进模板，这条边界写在配置页的"保留但无读取方的字段"注里。
- **`tests/docs/test_doc_bash_blocks.py`**：扫 `README.md`、`AGENTS.md`、`pages/*.md`、
  `.agents/notes/**/*.md` 的 bash 围栏块，逐块 `bash -n`（只解析不执行），并拒绝块内出现
  `<占位符>`——要展示形状就降到 text 围栏，bash 围栏只留给能直接粘贴的命令。

边界（明确不做）：不扫 `docs/` 与 `phone_agent/` 的模块 docstring（那是维护者内部读物，不是发布手册）；
不做文档自动改写（门禁只报告，修复归作者）；不新增第三方依赖；不动 `.github/workflows/`（新门禁复用既有
docs job）；不把"这句是规则还是历史"的语义判断交给机器——机器只拒**没有豁免声明**的变迁词。

## Alternatives considered

- **只靠人工 review 文档，不加机器门禁**
  - **最强的理由**：判断"这是历史叙述还是现行规则"、"这个块该不该可执行"、"这个键算不算对外契约"只有
    读得懂上下文的人（或 review agent）能做；门禁用词表与正则近似语义，必然有误报，而误报的代价是作者
    写一条豁免注释把问题盖过去——门禁反而可能生产"为了过门禁"的粉饰。人工 review 没有这一类失真。
  - **为何被否**：本仓 PR 主要由 agent 产出、人只做裁决，review 注意力是稀缺资源，而流程项恰恰是机器最
    擅长、人最容易疲劳的地方。更要紧的是这四类腐化的失败模式不是"判断错一次"而是"没人再读"：本次实测
    的占位符已经躺在 README 里 2–3 天、6 处变迁词分散在 3 个页面，说明人工 review 事实上没有抓住它们，
    而机器判据能覆盖的那一半（词表、键集合、`bash -n`）不需要消耗裁决带宽。语义判断仍然留给人：词表只
    拒未声明豁免的命中，行级豁免就是"人已判断过"的落点。
- **直接自动改写：门禁发现变迁词就替作者删句或换词**
  - **最强的理由**：既然结论是"这些句子不该以这种形态留在页面里"，一步到位改掉最省人力，也避免作者
    随手写豁免糊过去；门禁变绿的同时文档已经被修正。
  - **为何被否**：改写需要语义判断，机器改会改坏句子（"耗尽后不再付摘要调用"是规则，删掉"不"字就反了
    语义），而这种错误由机器写出、人反而更容易放过。更重要的是自动改写把"文档为什么变成这样"从 diff 里
    抹掉了——本仓的取舍是修复归作者、门禁只把事实摆出来。
- **引入现成的 markdown / 中文文本 linter（markdownlint、vale、textlint）**
  - **最强的理由**：成熟工具、规则面广、社区维护，不必自己写正则；markdownlint 还能顺带检查围栏块语言
    标注、标题层级这类通用卫生，长期维护成本低于自研门禁。
  - **为何被否**：本仓真正的三条契约是**跨文件**的（配置键三方一致、笔记 `Status:` 与文件夹一致、bash
    块可解析），现成 linter 的规则集表达不了它们，仍要写自定义规则——那就只剩依赖成本。而 docs job 的
    零安装（pytest + mkdocs-material）是它保持便宜、每个 PR 都跑得起的根据；引入 Node 或另一个二进制的
    代价大于四道几十行的 pytest 门禁。
- **把门禁挂到 `pages.yml`（部署前）而不是 `tests/docs/`**
  - **最强的理由**：`pages.yml` 已经在跑 `pytest tests/docs` 与 `mkdocs build --strict`，"检查再发布"的
    顺序天然成立，不必依赖 ci.yml 的 docs job 长期存在。
  - **为何被否**：`pages.yml` 按设计只在 `pages/**` 与 `mkdocs.yml` 改动时触发（检查与部署分离的结论，
    见 ci-three-gate 笔记），而配置键门禁最该在"有人改了 `phone_agent/v2/config.py` 却忘了改手册"的那个
    PR 上生效——那类 PR 不会碰到 `pages/**`。挂进 `tests/docs` 还顺带让门禁在本机 `pytest tests -q` 里
    可见，作者不必等 CI。

## Consequences

- **收益**：四类腐化从"靠自觉"变成每个 PR 的机器事实，且不需要新增 workflow、job 或依赖；`tests/docs`
  从 40 项涨到 155 项。落地当次即抓到真实问题：4 个不可执行的 bash 块（`<serial>` / `<run_dir>` 占位符）
  被改成可粘贴命令，5 个页面 12 处变迁词被逐个甄别（9 处确认为行为规则、加行级豁免，4 处改写为现状陈述，
  其中 1 处改写后同行仍有命中、同时带豁免）；笔记门禁对既有 8 篇笔记零告警（格式已一致，门禁是防回流），
  配置键门禁对三处现况零告警（说明当时没有漂移）。每道门禁自带正向自测（例如
  `test_change_narration_is_detected`、`test_future_heading_detector_matches_planned_sections`、`_env`
  模板解析样例、`bash -n` 真的拒绝坏块），并有"扫描面非空"护栏，避免文件被挪走后静默变绿。
- **代价**：`pages/*.md` 的作者从此多一条机械约束——写"不再支持"这类规则句要顺手加 `<!-- allow:词 -->`，
  忘加就是红灯。豁免注释是**人类判断的落点，不是绕过**：它可见、行级、渲染后不可见，因此页面不会出现
  读者看得见的注释垃圾，但源码会多出一些标记。词表本身也是欠账：`不再` 这类高频行为词决定了误报率，词表
  越窄越少误报、越宽越能防历史叙述，本次选择保留宽词表 + 行级豁免。
- **仍然脆弱（只证明便宜的那一半）**：`bash -n` 只证明语法，不证明语义——改名的 flag、被删的路径、变化
  的默认值仍然只能由真跑命令的读者发现；配置键门禁只核对"键名出现在源码与文档里"，不核对文档里写的默认
  值、类型与行为是否与代码一致（落地时人工发现 `PHONE_AGENT_PARALLEL_TOOL_CALLS` 的默认语义在
  `architecture.md` 与 `configuration.md` 里写反了：默认是**下发** `parallel_tool_calls=false`，设 `true`
  才不发送——机器门禁当时全绿，这处是清变迁词时顺手修正的）。变迁词门禁同理：它拒的是词汇，不是叙事，
  一句不含禁词的"与旧实现相比……"仍能通过。
- **仍然脆弱（覆盖面按目录划）**：新页面/新目录不会自动进扫描面——`pages/*.md` 用 glob 覆盖，但新增
  `pages/sub/` 或把手册搬到别处就会漏检；每道门禁的"扫描面非空"护栏只能在既有根目录整体消失时报警。
  根 `AGENTS.md` 的 P0 表与本文件未同步新增条目：这四道门禁是随 docs job 走的测试工具，不是运行时代码
  契约，因此本次不改 P0 表，也不新增入口文档。
