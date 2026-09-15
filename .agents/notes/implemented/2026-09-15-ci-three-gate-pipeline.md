# Agent Note: 主流水线立 lint / docs / test 三道独立机器裁决，检查与部署分离

Status: implemented

## Problem

仓库的产出模式已经变了：代码由多个 agent 并行写、并行提，人只做裁决。但**验证还停在单机上**——
`pytest tests` 只在提 PR 的那个人的机器上跑过，云端零复核。三个可观测的后果：

- **claim 与验证之间没有机器**：PR 描述里的「1815 passed」是自报，没有第二个进程独立复现过它。agent 越
  多、并行分支越多，这句话的可信度越低——它不是不诚实，而是**不可核验**。
- **复核成本随并行度线性上升**：每个分支都要人（或另一个 agent）重新 checkout、装依赖、跑一遍，才能
  判断能不能合。这条路径既慢，又把裁决者的注意力消耗在重复劳动上。
- **既有门禁只覆盖了文档的一小半**：`.github/workflows/pages.yml` 同时做检查和部署，PR 触发时跑
  `pytest tests/docs` + `mkdocs build --strict`，但**只有**碰到 `pages/**`、`AGENTS.md`、`mkdocs.yml` 等
  路径才触发。改 `phone_agent/**` 的 PR 完全不经过任何 CI。

## Decision

- **三道并行门禁，全绿才能合 main**：`.github/workflows/ci.yml` 新增三个 job——`lint`（`ruff check .`
  + 拼写与格式）、`docs`（`pytest tests/docs` + `mkdocs build --strict` + 产物密钥扫描）、`test`
  （`pytest tests`，Python 3.12 + 3.13 矩阵，ubuntu-only）。三者互不 `needs`，并行跑完由分支保护统一卡。
- **触发面按「检查」定义，不按「路径」定义**：`pull_request` 不限路径 + `push` 到 main。上一个门禁的
  路径白名单正是它漏检的原因——白名单需要预知改动会影响什么，而门禁的职责恰恰是不假设这一点。
- **检查与部署分离**：`pages.yml` 只保留 push 到 main（`pages/**`、`mkdocs.yml`、自身）与
  `workflow_dispatch`；PR 触发、`pytest tests/docs`、产物密钥扫描全部移出，部署 job 原样不动。检查归
  `ci.yml`，部署归 `pages.yml`。
- **文档检查刻意不装 `requirements.txt`**：`tests/docs/` 只依赖标准库 + pytest，这条约束必须保持——它是
  docs job 能一直开着、且比 test job 快一个数量级的原因。
- **测试矩阵只需要解释器**：全套 1815 个测试是离线单测，不需要安卓设备、不需要 API key、不需要网络。
- **真机验收不进 CI**：真机诊断留在 `.agents/skills/phone-agent-live-diagnosis/SKILL.md` 描述的本地路径。
- **`requirements.txt` 用环境 marker，不删依赖**：`mlx-embeddings` 依赖 `mlx`，而 `mlx` 没有 Linux wheel，
  `pip install -r requirements.txt` 在 ubuntu 上必失败。加上 `; sys_platform == "darwin" and
  platform_machine == "arm64"` 后，macOS 本地装法不变、ubuntu 可干净安装；MLX 的导入本来就是懒的
  （`v2/recall.py` 在 `_ensure_loaded` 里才 import），所以测试路径不需要它。
- **格式与拼写暂时是 advisory，不是 gate**：`ruff format --check .` 在 161 个文件上失败、`typos` 在 15 处
  失败，两者都是**本次改动之前就存在**的状态（树从未被任何格式化工具跑过：black 在 88 与 120 两个行长
  下同样报 183/198 个文件；15 处拼写全是误报——`aafter_model`/`abefore_model` 是 LangChain 中间件覆写名、
  `com.sdu.didi.psnger` 是包名、`iy`/`ix` 是坐标变量、`(mis)named` 是断词）。清账分别要一次 161 文件的
  机械重排与一个 `.typos.toml` 白名单，两者都不该随 CI PR 混进来，所以这两步挂 `continue-on-error: true`
  先可见、后升级。**唯一真正阻塞的样式信号是 `ruff check .`，它当前全绿。**

## Alternatives considered

- **信任 agent 自报结果，不上 CI**
  - **最强的理由**：零维护成本。跑在本机的 `pytest tests` 已经覆盖了同一套断言，云端再跑一遍是纯重复；
    workflow 本身也是要维护的代码，YAML 会腐、action 会过期、runner 会变。
  - **为何被否**：claim 不等于验证。自报结果由**产出方**签，而 PR 描述里的数字没有任何机制保证它对应
    被合入的那个 commit；并行 agent 越多，一个 agent 复述另一个 agent 结论的概率越高。裁决的前提是
    有一份不由被裁决方书写的证据，这正是 CI 提供的东西——它买的不是「多跑一遍测试」，是**独立性**。
- **把真机测试也搬进 CI**
  - **最强的理由**：真机行为才是这个项目的验收标准，`dry-run` 不等于真机验收（skill 自己这么写）。
    如果 CI 能跑真机，三道门禁就能覆盖到终局，而不只是单测。
  - **为何被否**：云端 runner 没有安卓设备，也没有 ADB 通道；要造这条链路得引入设备农场或自托管
    runner，那是运维成本与安全面（真机画面、设备序列号、私有数据都不该进公开 CI）。真机诊断按设计是
    本地 skill 的职责，与 CI 的边界不需要重叠。
- **检查与部署留在同一个 workflow（只删 `pull_request` 触发）**
  - **最强的理由**：改动最小，一个 workflow 里既构建又发布，产物不用传两次，依赖关系天然可见。
  - **为何被否**：合并后语义会互相污染——样式或测试变红会挡住站点发布，而发布失败又容易被误读成
    「检查没过」。两者的触发面本来就不同（检查要对每个 PR 负责，部署只对 main 负责），拆开后
    `concurrency` 分组、`permissions`（`contents: read` vs `pages: write` + `id-token: write`）也能各按
    最小权限声明。

## Consequences

- **收益**：每个 PR 有一份不由产出方书写的证据，人从「复现一遍」退回「读结论 + 裁决」；文档检查从
  「只有改文档才触发」变成「每次改动都触发」；`pages.yml` 的权限面收窄到部署，键值扫描进了必然执行的
  docs job 而不是可能被路径白名单跳过的岗位；`requirements.txt` 第一次在 Linux 上可干净安装。
- **代价**：多了一个要维护的 workflow 与两个矩阵腿（3.12 / 3.13），Python 或依赖升级时要同步；矩阵是
  双份 runner 时长。**`requirements.txt` 从此背了一条新契约：必须保持可干净安装**——任何引入
  平台专属依赖的改动都要带环境 marker，否则 test job 在两个版本上一起红。`crate-ci/typos@master` 是
  浮动 ref，上游发版会直接落到 CI 上，字典变更可能让 advisory 步骤的报错数漂移。
- **仍然脆弱**：三道门禁全绿**只**证明「单测 + 文档 + 样式」，不证明行为正确——真机验收仍是本地 skill
  的职责，CI 绿永远不等于任务成功。格式与拼写还是 advisory，所以「借 CI PR 顺手引入未格式化代码」目前
  不会变红；把这两步升级为阻塞需要先落下 161 文件重排与 `.typos.toml`，在那之前它们是可见的欠账，不是
  门禁。分支保护的「required status checks」是仓库设置而非代码，改不到仓库设置时这三个 job 只是**报告**，
  挡不住合并——本条笔记不负责也无法保证它已被配置。
