# Agent Note: v2 测试全域目录化 + 命名门禁

Status: implemented

## Problem

批 1+2（见 `2026-09-20-test-doubles-consolidation.md`）只解决了"双打唯一化"和观测域一隅：其余用例文件
仍平铺在 `tests/v2/` 根下，86 个文件里既看不出归属、也没有分组，找"谁在守 safety"只能靠文件名猜。

- **文件名带批次/事故编号**是常态：`_wp1`、`_g2c`、`_s2`、`_s3_s4`、`_e1`、`_e4`、`_wf1`…`_wf4c`、
  `_wp_r2`、`_a`。编号先于代码被遗忘，一个红灯文件名告诉后来者的是"哪个批次做的"，不是"什么坏了"。
- **深度敏感路径**：`Path(__file__).parent.parent.parent` 把"文件在树里的深度"焊进 skill scripts 的定位，
  文件一搬家就指向别处（`test_diagnostic` 两处）。
- **共享辅助跨文件**：`test_experience._install_mini_agent_modules` 被 8 个文件 import，搬家时它的路径也
  必须跟着改，否则整片用例 import 失败。
- **`.gitignore` 的 `memory/` 未锚定**：新域目录 `tests/v2/memory/` 会被一并忽略，`__init__.py` 进不了
  库——不是设计讨论，是实测踩到的（`git check-ignore` 命中 `.gitignore:98`）。
- **P0 门禁文件不能动语义**：`test_safety_warning.py` / `test_safety_layers.py` / `test_trace_invariant.py`
  在搬家时必须逐字不变，否则本轮"只搬不改"的验收就没有证据。

## Decision

`tests/v2/` 按被测对象分成 12 个域目录：`locate/`、`actuation/`、`safety/`、`taskdoc/`、`context/`、
`providers/`、`memory/`、`recall/`、`harness/`、`finish/`、`runner/`、`observation/`，加双打 `doubles/`；
94 个用例文件全部归位（含批 2 已搬的观测域）。

- **三个 P0 门禁文件只做 `git mv`**：`git diff -M --numstat` 显示 0 增 0 删，路径之外没有一行变化。
- **16 个批次编号文件按被测对象改名**（例：`test_workflow_memory_wf3` → `test_procedure_injection`，
  `test_marks_ime_collapse_wp1` → `test_marks_ime_collapse`，`test_plugin_e4` →
  `test_plugin_middleware_retirement`），同域保留同前缀以便列表归组。
- **新增命名门禁** `tests/v2/harness/test_suite_layout.py`：`tests/v2/` 下任何文件名命中
  `_(wp|wf|s|g|e|p|r)\d+` 即失败——批次编号从此只能出现在 commit message 里。
- **两处拆分是"搬"不是"重写"**：`test_middleware.py` 拆成 `safety/test_middleware_safety.py`（安全谓词 +
  trace 脱敏）与 `context/test_middleware_context.py`（图像剪枝 + 预算）；`test_contract_repair_s2.py` 的
  两条 opening-observation 用例移进 `runner/test_opening_observation_failure.py`。拆完用 AST 对名，27 个
  与 8 个用例名逐一不变。
- **删 `tests/v2/_doubles.py` shim**：12 个引用文件的 import 机械改写成
  `tests.v2.doubles.<family>`，双打只剩一处可导入。
- **`.gitignore` 的 `memory/` 锚成 `/memory/`**：仓库根 `memory/`（私有运行态）仍被忽略，
  `tests/v2/memory/`（测试代码）恢复可入库——顺带修掉一个会静默吞掉测试的坑。
- **边界**：不改断言语义、不删用例；`tests/docs`、`tests/web`、`tests/skill`、`tests/` 根不碰；不引入
  `pytest.ini`/`pyproject.toml`。
- **一处例外**：`tests/v2/test_agent_loop.py` 留在根目录。`tests/web/test_bridge.py`（本批冻结）从它
  import `ScriptedToolModel`，搬走就得改 web 树——宁可留一个根下文件，也不越红线。

## Alternatives considered

- **全局 `tests/` 重排（把 `tests/docs`、`tests/web`、`tests/skill` 与 `tests/` 根的保留库测试一起收编成
  同一套域目录）。**
  - 最强理由：一次做完，"测试树长什么样"只有一种答案；`tests/` 根的 adb/config 注册表测试也确实可以按
    同一套域重排。
  - 为何被否：这三棵树各有自己的门禁与读者——docs 门禁只装 pytest 与 mkdocs、web 守护投影契约、skill 是
    真机诊断的离线冒烟——重排等于同时改 CI 三条 job 的语义与本批的验收面；且批 1+2 与并行工作流都以它们
    的现状为基准，红线明确不碰。
- **双打改用根 `conftest.py` 的 fixture 共享，不再有 `doubles/` 包。**
  - 最强理由：pytest 原生机制，用例只声明参数、不写 import，依赖在签名里显式可见。
  - 为何被否：双打不止是实例——还有共享常量（`SETTINGS_XML*`）、构造辅助（`make_mark`）和模块级注入
    （`_install_mini_agent_modules`），且大量用例在**编辑期**就引用它们（子类化 `ScriptedModel`、按
    `FakeConfig` 类属性钉定时），fixture 只覆盖其中一部分，结果是"一半 fixture、一半 import"的双重来源。
    批 1+2 已把 `doubles/` 定为唯一事实源，本批只做搬家与清 shim。
- **不做门禁，靠评审守命名。**
  - 最强理由：少一个测试文件、少一套正则要维护；"文件该叫什么"本来就是人的判断。
  - 为何被否：命名漂移属于"没人会主动反对"的变更——16 个编号名正是这样一个个长出来的，且每个都通过了
    评审。机器门禁是唯一在事后仍拦得住的机制，代价只有一行正则。

## Consequences

- **收益**：94 个文件按域归位，`ls tests/v2/<domain>/` 就是一份契约清单；失败文件名直接说明被测对象；
  `REPO_ROOT` 让 skill scripts 的定位不再随目录深度漂移；shim 删除后双打只有一处可导入；`.gitignore`
  锚定后新域目录不会再被静默吞掉。
- **代价**：测试 ID 与 import 路径全变（如 `tests/v2/context/test_compact.py::…`），任何按旧路径点名的
  外部脚本、CI 过滤或记忆都得跟着改；`tests/v2/test_agent_loop.py` 是根下的已知例外；跨文件注释里的路径
  引用要人维护（本批扫过一遍）。同族重复没有清零：scripted 模型仍有本地副本（`test_agent_loop`、
  `test_event_chain_behavior`、`test_sibling_receipts`），吸收进 `doubles/models.py` 留待后续批次。
- **验证**：批 3 / 批 4 / 批 5a / 批 5b 各自全量 `pytest tests -q` → 2263 / 2263 / 2263 / 2264 passed + 4
  failed（仅已知环境性：anthropic httpx2 ×2、`tests/web/test_model_streaming` ×2，与本次改动无关）；
  `ruff check . --select E4,E7,E9,F` 通过；`pytest tests/docs -q` 322 passed；`tests/AGENTS.md` 734/750 词。
