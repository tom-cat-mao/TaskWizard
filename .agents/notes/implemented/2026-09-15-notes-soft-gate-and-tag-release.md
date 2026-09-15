# Agent Note: 决策笔记用软评论提醒，发版用 tag 一致性校验后自动 Release

Status: implemented

## Problem

P0 契约 #23（非平凡改动必须带决策笔记）立规时只有文字约束，没有任何机器触点，两个具体缺口：

- **靠自觉会遗忘**：规则正文在 `.agents/notes/AGENTS.md`，而每次 session 常驻的是根 `AGENTS.md` 里那一行
  索引；六类命中条件（用户可见行为 / 架构切分 / 跨文件契约 / 工具链 / 测试策略 / 落盘格式）要读到索引之后
  才展开。笔记不影响任何测试或命令的成败，所以在「先把功能改完」的推进节奏里，它是最先被省掉的那一步。
- **成本不对称**：漏一篇笔记不会当场报错，代价（过一段时间后没人说得出当时为什么这样设计）要到很久以后
  才显现；而契约要求的是「与代码同批提交」，事后补写虽然允许，却已经丢了当时的理由。

发版侧是另一类问题：版本号写在 `setup.py` 的 `version="0.1.0"`，tag 由人手工打，两者没有任何机器绑定。
tag 打成 `v0.1.1` 而 `setup.py` 仍是 `0.1.0` 时，仓库进入「tag 说的版本 ≠ 代码里的版本」的状态，而
GitHub Release 本身发现不了这一点——它只认 tag。

## Decision

两条自动化，都在 `.github/workflows/`，边界写死：

- **`notes-check.yml`（软门）**：`pull_request` 的 `opened / synchronize / reopened` 触发，用
  `actions/github-script@v7` 分页拉取 PR 变更文件；若含 `phone_agent/**/*.py` 且**没有**新增
  （`status: added`）`.agents/notes/**/*.md`，就在 PR 下发一条带隐藏标记 `<!-- notes-check -->` 的中文提醒，
  列出六类命中条件并链接 `.agents/notes/AGENTS.md`。发之前先查历史评论，见到标记就不再发（幂等）。
  权限只给 `contents: read` + `pull-requests: write`。**job 永远 exit 0**（唯一例外是 GitHub API 真的故障），
  fork PR 上评论失败只发 warning，不把提醒变成红灯。
- **`release.yml`（tag 发版）**：`push` tag `v*` 触发，权限 `contents: write`。先解析 `setup.py` 的
  version 与 tag 逐字比对（tag 去掉 `v` 前缀），不一致直接 fail 并打印「tag 是什么 / setup.py 是什么 /
  该怎么改」；一致才 `gh release create <tag> --generate-notes --verify-tag`，release 名即 tag 名。

明确不做：不建 `ci.yml`（lint / docs / test 三门归另一分支）、不构建产物、不发 PyPI、不维护 changelog 文件、
不做硬门禁。release notes 由 GitHub 从 PR 标题自动生成。

## Alternatives considered

- **硬门禁：没有笔记就让检查变红，block merge**
  - **最强的理由**：只有强制力能真正改变行为。软提醒的可忽略性与漏写笔记的可忽略性是同一种失败模式——
    决定「提醒可以被无视」时就等于承认它会被无视，契约 #23 名存实亡。
  - **为何被否**：判据太粗。判断一个 PR 是否非平凡靠的是六类语义条件，workflow 只能看到路径；typo 修复、
    格式化、不改行为的依赖升级都会命中 `phone_agent/**/*.py`，硬门禁会把它们全部卡住，而唯一的绕过办法是
    补一篇没有内容可写的笔记——那会把 notes 变成模板填充场，反过来污染笔记的可信度。先用软门观察误报率，
    要升级门槛时再改（本决定在代价栏记下了这个未决项）。
- **人工在 review 时检查，不加自动化**
  - **最强的理由**：人是唯一能真正判断「这一步是否非平凡」的裁判，机器判据必然要么过宽要么过窄；人工检查
    没有误报，也不需要维护 workflow。
  - **为何被否**：本仓的 PR 主要由 agent 产出，一个分支常常「实现 + 测试 + 文档」一次成型；reviewer 的注意力
    若花在「有没有笔记」这类流程项上，就会挤掉真正的正确性检查，而流程项恰恰是机器最擅长、人最容易疲劳的
    地方。人工检查不是不做，而是只应留在机器判不了的地方：软评论把事实摆到 PR 上，人只需判断一次。
- **发 PyPI：tag 时顺带 `python -m build` + `twine upload`**
  - **最强的理由**：`setup.py` 与 `version` 字段都已存在，PyPI 是 Python 项目的默认分发路径，发布即获得版本
    归档与 `pip install` 入口。
  - **为何被否**：本仓是插真机（ADB、设备授权、本地模型权重、`.env`）运行的应用，而不是被 import 的库。
    README 的安装故事是 `git clone` + `pip install -r requirements.txt`，而 `requirements.txt` 比
    `setup.py` 的 `install_requires` 多出一批运行依赖（NiceGUI、FastAPI、LangChain 各家 provider、MLX 相关），
    装成 wheel 的体验与文档不一致。当前阶段 clone 分发够用，少一条发布链路就少一处长期密钥（PyPI token）
    与一轮不可逆操作；真要发行时，本 workflow 追加一步即可，tag 一致性门禁继续复用。

## Consequences

- **收益**：契约 #23 从「靠自觉」变成 PR 上可见的提醒——事实（本 PR 有没有笔记）由机器摆出，判断仍归人；
  发版从「人记得同步两个数字」变成「机器强制两个数字相等」，打错版本的 tag 在 Release 建立之前就被挡住。
  两条 workflow 都是几十行，没有新增依赖，也没有新增密钥（只用 `GITHUB_TOKEN`），且 soft gate 全程不阻塞
  任何合并。
- **代价（覆盖不全）**：软门只对 `phone_agent/**/*.py` 生效，漏报是已知边界：只改 `tests/`、只改 `pages/`、
  或改动完全落在 `phone_agent/` 之外的改动都不会触发提醒，命中六类仍需自觉。
- **代价（评论噪音）**：每个「改了代码没写笔记」的 PR 都会多一条评论。判定为琐碎改动的作者需要自己忽略它；
  若误报率高到让人条件反射地点掉，软门就退化成背景噪音——届时按上面的升级路径改成硬门禁或收窄路径。
- **仍然脆弱（fork PR）**：来自 fork 的 PR 拿到只读 `GITHUB_TOKEN`，评论会失败，此时只发 `core.warning`；
  即提醒在 fork PR 上可能完全不出现。这是为「不让提醒把外部贡献者的 CI 弄红」付出的代价。
- **仍然脆弱（tag）**：版本门禁只校验 tag 与 `setup.py` 相等，不校验 `README.md` / `pages/` 里是否还写着旧
  版本号；`--generate-notes` 的质量取决于 PR 标题是否写清行为变化，没有 changelog 文件可校对。
