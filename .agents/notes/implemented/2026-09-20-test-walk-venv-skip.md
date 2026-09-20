# Agent Note: 测试基建的两类假等待——全仓扫描跳过任意深度的 venv、观测时序不在测试里真睡

Status: implemented

## Problem

主 worktree 上 `.venv/bin/python -m pytest tests` 从 ~89s 恶化到 ~13min，而 CI 三个 job 一直全绿：慢的是
**本机私有状态**，不是代码。两个独立成因，均有实测。

**一、全仓扫描走进嵌套 venv（主因）**。`tests/docs/test_config_keys_sync.py::repository_keys()` 用
`rglob("*.py")` 遍历仓库，跳过判定只比较**顶层**目录名（`parts[0] in _WALK_SKIP_DIRS`）。主 worktree 的
`.external-agent/worktrees/<worktree>/` 是一整个隔离 worktree，自带 `.venv`——该子树共 15,867 个 `.py`（其中
14,673 个是 venv 里的第三方包源码，1,194 个是那个 worktree 自身源码），全部被当成 shipped sources 逐文件
`ast.parse`。该函数在 docs 门禁里被调用 **15 次**（13 个页面参数各一次 + 2 个仓库级断言），于是：

- 修前 `pytest tests/docs -q` = **674.08s（310 passed）**，即单次扫描 ≈44s；基线输出里 **15 条**
  `SyntaxWarning` 全部来自那个 venv 的 `MakeSingleHeader.py`——扫描确实走了进去的直接证据。修后单次扫描只剩
  **105 个** `.py`（仓库自身源码），同一套件 4.62s。
- 同一缺陷在 `tests/docs/test_doc_references.py::_bare_files()` 里复制了一份（同一套顶层跳过集，`rglob("*")`
  范围更大、含全部文件），只是它有 `lru_cache` 且只走一次，代价被掩盖——属于同一批修掉的同类问题。

**二、观测失败/退避路径付真实 sleep（次因）**。`observe_settle_ms`（默认 300ms）与
`observe_retry_backoff_s`（默认 2.0s）是运行期契约，但凡是驱动真实 `PhoneSession` 而 config double 没显式置零
的用例，每轮观测先睡 0.3s，marks dump 判为不稳定再睡 2.0s。用临时 pytest 插件包住 `time.sleep` 统计调用点，
修前 **87.14s** 的非 docs 套件里纯睡眠 ≈42s（占近一半）：2.0s 退避组按 durations 反推 ~34s
（`test_observation_reference_frame` ~22s、`test_observation_failure_codes` ~8s、`test_contract_repair_s2` ~4s，
审计表表头被 `tail` 截断，这部分按各用例耗时反推），审计表可见的 0.3s settle 组 7.8s（26 次：
`test_observation_lifecycle` 15、`test_appkb_writeback` 9、`test_workflow_memory_wf3` 2）。这些等待不改变任何
被测逻辑，只把墙钟转嫁给了每一次本地运行与 CI。

## Decision

**1. 全仓扫描的跳过集分两类，两个门禁同一形状。** `tests/docs/test_config_keys_sync.py` 与
`tests/docs/test_doc_references.py` 各自声明：

- `_ROOT_SKIP_DIRS`（只匹配顶层）：`.external-agent`、`.qoder`、`.wt`、`.pi`、`.git`、`memory`、`outputs`、
  `site`；`test_config_keys_sync` 另有 `tests`（只被测试读的键不是 shipped key）。这一类的共性是「工作副本
  会长、新 clone 不会有」的本地状态，而 `.external-agent` / `.wt` 是 agent worktree 容器，每个 worktree 自带
  一套 venv。
- `_ANY_DEPTH_SKIP_DIRS`（任意深度）：`.venv`、`venv`、`__pycache__`、`node_modules`、`.git`。venv 可以出现在
  任意一层，只挡顶层正是这次的病根。

判定收敛到一个 `_skipped(path)`：`parts[0]` 查顶层表，任一段查任意深度表。**同时给 `repository_keys()` 加
`lru_cache(maxsize=1)`** 并把返回值改为 `frozenset`：它是纯函数却被逐页重复调用，而兄弟门禁
`test_doc_references._bare_files()` 早就是这个形状，这里补齐。

**2. 观测时序在测试里显式归零，生产默认值一律不动。** 在 6 个驱动真实 `PhoneSession` 的 config double / fixture
里显式写 `observe_settle_ms = 0` / `observe_retry_backoff_s = 0.0`：`test_observation_lifecycle.py`（原来两者都
缺，每轮付 300ms 默认）、`test_observation_failure_codes.py`、`test_observation_reference_frame.py`、
`test_contract_repair_s2.py`（原来只缺 backoff）、`test_appkb_writeback.py`、`test_workflow_memory_wf3.py`
（原来只缺 settle）。原则是**只改等待、不改路径**：重试轮数、整轮重采（截图含在内）、注解提交、失败码全部照旧
执行，只是不等墙钟。时序值本身的断言留在 `test_observation_hardening.py` 与 `test_observe_retry_backoff.py`
——它们 monkeypatch `phone_agent.v2.session.time.sleep` 并断言被调用值（`[0.3]`、`[0.3, 0.3]`、`[2.0]`、
`[0.5, 0.5]`、`[]`），口径没有丢。

明确不做：不动生产默认（300ms / 2.0s 是部署契约，`PHONE_AGENT_OBSERVE_*` 可覆盖）；不在 `conftest.py` 里加
autouse 的 `time.sleep` 补丁（那会同时掩蔽「测试真的在等什么」这一信号）；不删任何门禁，也不把它们只留给 CI。

## Alternatives considered

- **只往顶层跳过集补 `.external-agent` 与 `.qoder`**：改动最小，本次症状立刻消失。
  - **最强的理由**：这两个名字就是本机仅有的两个「非 clone 目录」，够用即止，不为将来的形状写规则。
  - **为何被否**：病根是**匹配方式**（只看 `parts[0]`）而不是名单，嵌套 venv 会随下一个隔离 worktree、
    vendored clone 或 conda 布局重现；更糟的是失效形态**静默变慢**——不红、不报错，只在十几分钟后被注意到。
    名字表分两类写死的成本只是多一个常量。
- **用 `git ls-files` 枚举 shipped sources，替掉文件系统遍历**：语义上最准——「shipped sources」就是 tracked
  文件，天然看不见任何本地状态，本问题不可能复现。
  - **最强的理由**：跳过表无需维护，规则由版本控制本身给出；两道门禁要管的正是 tracked 内容。
  - **为何被否**：`tests/docs` 现在不 import 任何外部依赖、也不假设自己在 git worktree 里（CI 只是恰好有
    git），这是它「便宜到可以每次都跑」的前提；换成 `git ls-files` 会把文档门禁绑上外部命令与工作副本形态，
    而问题本可以用两行判断解决。另外 `_bare_files()` 的语义是「裸文件名 → 仓库里第一个匹配」，含未 tracked
    的文件，换 git 会顺带改掉它的解析面。
- **保留 sleep 但缩小（例如 backoff 0.05s）**：保留「确实睡了一下」的形状。
  - **最强的理由**：中间值更接近生产节奏，对时序敏感的行为（事件顺序等）或许仍需要一点错位。
  - **为何被否**：这条路径上没有任何行为依赖等待时长——重试是显式循环，不靠时间探测；而任何非零等待都可以
    在机器负载高时变成 flake。要么用 monkeypatch 断言它，要么别等。
- **把慢门禁挪到 CI，本地不跑 docs**：本地立刻变快。
  - **最强的理由**：CI 才是验收口径，本地重复同样的门禁本来就冗余。
  - **为何被否**：门禁的价值在于**推之前**发现文档漂移（一条错路径就是一次红 CI 往返）；修完之后整个 docs
    门禁只要 4.62s，成本已低于一次往返的沟通成本。
- **在 `conftest.py` 里 autouse 地把 `time.sleep` 换成 no-op**：一处治全身，不会漏掉下一个忘配置的双件。
  - **最强的理由**：彻底；理论上任何等待在测试里都不该真实发生。
  - **为何被否**：它会让「以等待为被测对象」的用例失去意义（`tests/skill/test_worker_subprocess.py` 的
    subprocess 超时、`tests/web/` 的轮询退出都要真等一小会儿），也会让真正卡住的代码表现为「跑得飞快」。
    归零必须写在**该测试自己的双件**上，才看得见谁在等什么。
- **给 `repository_keys()` 结果做跨运行落盘缓存**：连那 1.7s 也省掉。
  - **最强的理由**：单次扫描仍有成本，落盘后趋近于 0。
  - **为何被否**：跨运行缓存引入失效问题（改了代码而缓存未刷，门禁会撒谎），而这门禁的全部意义就是「以当前
    文件为准」。进程内 memoize 已把 15 次降到 1 次，收益足够。

## Consequences

- **收益（实测）**：`pytest tests/docs -q` **674.08s → 4.62s**（310 passed，来自嵌套 venv 的 15 条
  `SyntaxWarning` 一并消失）；全量 `pytest tests -q` **≈761s（docs 674.08s + 其余 87.14s，分两次测得）→
  45.19s**（2251 passed；4 条既有环境性失败：`test_provider_context` 的 anthropic httpx2 ×2、
  `tests/web/test_model_streaming` ×2）。睡眠审计从 **~43s → 2.27s**，残留全部落在「以等待为被测对象」的用例里
  （subprocess 超时 1.5s、web bridge 0.6s、轮询 ~0.2s），**观测路径为 0**。另一层收益是**本地与 CI 同答案**：
  扫描不再看 clone 里不存在的目录，本机结果与干净 checkout 一致，而这一点原先只是巧合。
- **代价**：跳过表是名字表，新增一类本地工具目录（又一个 agent 的 worktree 容器、新的缓存目录）时不会自动
  生效——好在它现在的失效方式仍是「变慢」而非「变假绿」，且最常见的嵌套 venv 形状已由
  `_ANY_DEPTH_SKIP_DIRS` 兜住。`_bare_files()` 的解析面变窄：`.pi` / `.qoder` / `.external-agent` 内的文件
  不再参与裸文件名解析（改后 `tests/docs` 全绿，说明没有文档引用依赖它们）。`repository_keys()` 现在是进程内
  memoize 的纯函数且返回 `frozenset`——将来若有测试想「先改仓库文件再断言扫描结果」，必须自己清缓存。最后，
  **生产时序在测试里的可见度降低了**：6 个双件把 0 写死之后，「忘配置就白等 2s」这个提示信号没有了；默认值
  与 env 解析仍由 `test_config.py` / `test_observation_hardening.py` 守着（各自断言 300 与 0），但想验证真实
  节拍的测试必须自己显式设置时序值——这是刻意的：时序是**被测对象**，不该是测试的默认成本。
