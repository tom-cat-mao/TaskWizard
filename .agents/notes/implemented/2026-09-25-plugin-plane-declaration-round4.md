# Agent Note: 插件平面 Round 4 —— 一等插件 systemone（四接缝 dogfood）

Status: implemented

## Problem

Round 1-3 把声明面铺齐了（工具风险 / mode / CLI / 服务归属 / pin 前缀 / 配置键 / 记账角色 / 脱敏字面量 /
运行身份 / 挂载顺序），但整个平面上**没有一个真实插件**：每条声明都只有单测证明"写进去会被分类/记账/脱敏"，
没有一条证据证明"按这些声明真的能写一个完整插件"。规格单把 systemone 定为这个证据——它必须同时用上
provider、安全复核、工具、监听器四条行为缝，且是一个**决策模型**（结构化提问进、带校准概率的类型化答案出，
无自由文本、无图像），不是又一个 OpenAI 兼容网关。四个具体缺口：

1. **没有路径插件能用的模块组织方式**。`plugins.py::_spec_from_path` 用
   `spec_from_file_location` + `exec_module` 加载 `plugin.py`，**不注册进 `sys.modules`**；于是
   `from . import x` 不可用（不是包），而 Python 3.10+ 的 `dataclasses` 又要通过
   `sys.modules[cls.__module__]` 解析注解——一个多文件的路径插件连 dataclass 都定义不了。
   这条限制此前没人踩过，因为之前没有多文件的路径插件。
2. **安全复核只有内建一个实现**，而且是"第二模型判可逆性"的聊天式复核（`build_safety_reviewer`）。
   决策模型的价值恰恰在**校准概率**上：它能给"有风险"一个 `p`，而聊天复核只能给一个词。
   需要一个能把 `p` 变成闸门的接缝，且必须只加不减。
3. **画面稳定只能靠猜等待时长**。内建 `wait` 睡固定秒数再观测一次；G7 已经把
   `screen_hash` 放进 observe 事件，但没有任何消费端。
4. **卡死只有被动信号**（TaskDoc 流程线）。没有任何"最近 K 步在原地打转"的可见事实。

## Decision

按规格单写仓库内一等插件 `plugins/systemone/`（path 插件，默认关闭），四个接缝全部走公开声明点，
不加任何 core 钩子。

- **插件形态**：`plugin.py` 只做装配，行为拆在 `client/provider/reviewer/stability/governor` 五个兄弟模块。
  `plugin.py::load_sibling(name)` 用 `spec_from_file_location` + **先写 `sys.modules` 再 exec** 的方式加载，
  名字是稳定的私有名（重复加载复用同一模块对象，`plugin list` 会对每个条目加载两次）。这一条同时解决了
  缺口 1 的两半：模块有身份，dataclass 注解能解析。
- **provider**：`register_api_builder("systemone", builder)` + `register_provider(ctx, ProviderSpec(...))`
  两个后端（`jev` 云端 `https://api.typesafe.ai`、`kev` 本机 `http://127.0.0.1:8787`，免 key），
  provider bootstrap 靠 `deps=("providers",)` 提前挂载，所以它能当 actor/role 模型用。适配约定写在
  provider.py 模块 docstring、插件 README 与 pages/plugins.md：**最后一条 user 消息 = `state`**；
  带尾随 `Options: A | B | C` 标记行 → 一个 `choice` 问题（选项即官方 criteria map 的键），选中项原文
  即回复；否则一个 `noul` 问题（"Does the state warrant a yes?"）→ `"yes"`/`"no"`。选中的选项不在选项
  表里、答复不是 yes/no，都**抛错**。回复里的 `usage`（官方必返）翻译成 `AIMessage.usage_metadata`，
  于是 trace/budget 的角色记账零额外代码。`bind_tools` 直接 `NotImplementedError`（决策模型发不出 tool call，早失败 > 空转的 run）。
- **失败可见（P0 #8）**：`jev` 缺 `TYPESAFE_API_KEY`、`kev` 无监听者、`base_url` 为空、超时越出协议
  5-15s 带、mode/数值越界——装配期或构建期直接抛。同时把"**未配置**"与"**配错了**"分开：
  `PHONE_AGENT_SYSTEMONE_BACKEND` 留空是合法状态（只挂 `wait_for_stable` 与确定性调速器，stderr 一行说明），
  但显式要求 `REVIEW=on` 而没有后端 = 报错，不静默降档。
- **复核（真正的交付物）**：`tool/execute` **prepend** 监听器。状态 = 工具名 + 脱敏且有上限的目标文本，
  一次调用问 `noul`（有风险/不可逆吗）+ `score`（五档描述性风险等级，供审计）。
  - `off` 不挂载；`shadow`（默认）每次复核写 trace 事件 `systemone_review`，调用本身不动；
  - `on` 只在**三条件同时成立**时**追加**否决：既有分类器本来放行、布尔答案为真、其 `p ≥
    PHONE_AGENT_SYSTEMONE_CONFIDENCE`（默认 0.9）。否决文本与内建预警同形，模型带
    `confirm_irreversible=true` 重发即可通过。
  - **只加不减**靠四条实现约束锁住：既有闸门要拦的调用只记录、不改决定；已带 confirm 的重发直接放行；
    只读工具（未声明工具按 `actuation` fail-closed）之外不碰控制类工具；端点故障/超时/答复畸形/没有 `p`
    一律**不否决**并写 `action="error"`。阈值取 `noul` 答案的 `p`（`noul` 本身即校准概率，不需要在
    数字之间换算），`score` 与它的 `confidence` 只进 trace 供审计（官方 score 没有量纲定义）。
- **`wait_for_stable`**：`register_tool(..., risk="readonly")` 显式声明只读（dogfood Round 1）。轮询
  `session.observe()`（P0 #15 唯一观测生产者），连续 N 帧 `screen_hash` 相同即返回；三种失败各有其文本：
  `reason=timeout` / `reason=observe_failed: <异常>` / `reason=no_screen_hash`。每次轮询都推进观测批次，
  回执里点名"此前 mark id 已失效"。`max_wait_s` 可被模型按次覆盖但 clamp 在 0.5-60s。
- **调速器（shadow-only）**：`model/pre_request` 监听器，从 `AIMessage.tool_calls` 派生 `tool|intent` 签名，
  最近 K 步全同即发一条 advisory trace 事件（同一签名只发一次）；后端可用时额外问一个 `noul` 并把 `p`
  记进同一事件。**不注入提示词**的理由是硬约束而不是本轮取舍：注入要活过 auto-compact，而免折叠需要
  core 侧的 pin 白名单行（`v2/pins.py`），插件认领不了，于是注入会在第一次折叠时消失——调速器会静默失效。
- **dogfood 清单**：`register_setting` ×12（键名只在插件文档里列，不进 `pages/configuration.md`）、
  `register_usage_role("systemone", unit="tokens")`（官方必返 `usage`，按 P0 #13 的 token 单位记账）、
  `register_redaction(TYPESAFE_API_KEY)`、`register_tool(risk="readonly")`、`ctx.plugin_config()` 覆盖层、
  `after=("providers",)` 顺序声明、`ctx.on_dispose` 注销进程级 transport。release 后零残留（新测试断言
  声明/监听器/工具/transport 全部撤销）。
- **测试与配置**：`tests/plugins/`（自带的 `systemone_harness.py` 起一个**真 HTTP** 的假协议服务器，
  在 127.0.0.1 上实现官方 wire 形状与 401/422/429/529、延迟、部分失败、并发计数、usage 注入、
  `Authorization: Bearer` 校验；全部离线）；`plugins/index.json`
  登记该插件；`.taskwizard.toml` 新增条目但 `enabled = false`（随仓库提交的配置默认不启用、不执行插件代码）。
- **文档**：插件自带 README（四接缝、键表、启停步骤、诚实限制），`pages/plugins.md` 增一等插件段并把
  "index.json 暂未上架任何包"改成事实；预算 `pages/plugins.md` 3400 → 3600；`tests/AGENTS.md` 加一行
  `tests/plugins/` 并由收紧措辞吸收（745/750）。

## Alternatives considered

- **单文件 plugin.py**。最强的理由：完全不碰导入机制，任何 Python 版本都能跑。为何被否：四接缝 + 协议
  客户端接近千行，且单文件会把"协议 / 适配 / 策略"三层揉在一起，测试只能整体加载；兄弟模块 + 显式加载器
  的代价只有 12 行。
- **把兄弟模块塞进 `sys.path` 再普通 import**。最强的理由：`import client` 最直观。为何被否：插件往全局
  `sys.path` 注入目录，且 `client`/`provider` 这类顶层名与宿主进程的模块名冲突风险实打实；私有前缀名 +
  `sys.modules` 注册没有这两个问题。
- **做成真正的包（`plugins/__init__.py` + 相对导入）**。最强的理由：测试里 `from plugins.systemone import x`
  最干净。为何被否：路径插件的规范加载方式不会把仓库根放进 `sys.path`，包导入只在"恰好从仓库根跑"时成立，
  等于把"能不能加载"押在调用方式上；兄弟加载器不依赖 cwd。
- **transport 注册失败就整体报错（不做幂等覆盖）**。最强的理由：任何已存在的 `systemone` 注册都是可疑的。
  为何被否：`plugin list` 对每个条目加载两次、同进程可能装配多个 agent、release 后又 apply——自己的重复注册
  是常态。改成"标记自己的 builder、只覆盖自己的"，对**别人的**同 family 注册仍然报错。
- **`bind_tools` 返回 self、让 run 自己跑空**。最强的理由：不挡任何用法。为何被否：一个永远不产出 tool call
  的 actor 会跑到步数/预算上限才结束，比装配期一句 `NotImplementedError`（含正确用法）贵得多。
- **后端未配置时整体报错**。最强的理由：最省心，绝无"看起来挂了其实没干活"。为何被否：`wait_for_stable`
  与确定性调速器不需要模型，把它们绑在一个云端 key 上等于让最便宜的功能付最贵的门槛。改成"未配置 = 合法
  且可见（stderr 一行）"，但**显式**要 `REVIEW=on` 就必须有后端。
- **门槛用 score 而不是 `noul` 的 `p`**。最强的理由：分数信息量更大。为何被否：官方 score 没有 min/max，
  回的是模型自定标度的 `score` + `legend`，阈值语义要能一句话说清——"有风险的校准概率 ≥ X"能，
  "风险分 ≥ Y"不能；score 仍进 trace 供审计。
- **复核失败时 fail-closed（否决）**。最强的理由：与"未声明工具按 actuation"同一条纪律。为何被否：
  纪律管的是"沉默不能绕过既有闸门"；这是**追加**闸门，端点宕机就拦下所有动作是把一次网络故障升级成一次
  不可用。既有闸门不受影响，所以 fail-open + error trace 才是诚实方向。
- **调速器注入一条"你在打转"的 system 消息**。最强的理由：这是它对模型的唯一实际价值。为何被否：
  见 Decision——没有 pin 白名单，注入活不过第一次 auto-compact，插件会**静默**变哑巴；宁可先只发 advisory。
- **`wait_for_stable` 返回带截图的观测块**（像 `read_screen`）。最强的理由：模型能直接看到稳定后的画面。
  为何被否：它就把"我再等一会儿"变成了又一个视觉步骤，且与"把多轮等待折进一次工具调用"的初衷相反；
  回执写明批次已推进，需要画面时模型自己 `read_screen`。
- **插件键同步进 `pages/configuration.md`**。最强的理由：操作者只需读一页。为何被否：Round 2 已定——配置键
  同步门禁只认仓库内源码真实读取的键，第三方/插件键由插件自己的文档列出；本插件把 12 个键、取值域与
  越界行为都写在 README 里，并由装配期校验兜底。

## Consequences

- **收益**：平面第一次有完整消费端。四条行为缝、六个声明点（配置键/记账角色/脱敏/工具风险/顺序/进程级
  transport 归属）都有真实插件在用；`tests/plugins/` 122 个离线用例覆盖协议错误码、重试策略、kev 串行化、
  key 不外泄、构建期失败可见、阈值否决、只加不减、残留清理与 CLI/无 run 上下文降级。
  `.taskwizard.toml` + `plugins/index.json` 让"启用一个一等插件"成为一条可复制的路径。
- **代价**：`load_sibling` 是一条 harness 之外的第二加载路径（虽然遵循规范加载器的语义），harness 侧若要
  支持多文件路径插件应把它上收成 `plugins.py` 的能力——本轮不动 core。`register_api_builder` 是进程级表，
  同进程两个 agent 共享最后注册的 transport（平面既有约定，README 已写）。`plugin list` 会把插件加载两次，
  兄弟模块的 `sys.modules` 复用避免了重复类，但 `plugin.py` 本身仍被 exec 两次（既有行为）。
- **代价**：`on` 档的否决与内建预警共用 `confirm_irreversible` 这一放行键，所以"模型已确认"会让复核层
  一同放行；这是刻意的（不能让复核变成无法解除的僵局），但意味着复核不能用于"即使确认也要拦"的场景。
  另外 `safety_mode=off` 时插件是唯一闸门——`on` 是操作者的显式选择，README 已点名这层交互。
- **代价**：未配置后端的可见性只有一行 stderr（装配期 trace recorder 还没挂上）。Round 3 的
  `capability/tools_undeclared` 事件给了先例，但本轮没有为此新增事件；真机诊断时看 stderr 或
  `WaitState` 的 trace 里没有 `systemone_review` 事件即可判断。
- **代价**：`pages/plugins.md` 3400 → 3600（一等插件段 + index 事实修正）；`tests/AGENTS.md` 靠收紧措辞
  吸收新目录行；`tests/v2` 无改动（新增测试独立成 `tests/plugins/`），与 `tests/v2/harness/test_suite_layout.py`
  的命名纪律也无关。
- **收益（测试抓到的真 bug）**：`base_urls={backend: base_url} | dict(DEFAULT_BASE_URLS)` 里右操作数会覆盖
  左操作数，导致自定义 `PHONE_AGENT_SYSTEMONE_BASE_URL` 被默认值吃掉——只有"注册表里的 kev ProviderSpec
  指向假服务器"的端到端用例能抓到这个（单元级一直绿）。已修，并保留该用例。
- **遗留**：`README.md` / `AGENTS.md` 未改（前者"插件系统"行本就标注"含…"非穷举，只剩 3 词余量；后者在
  2190/2200）。规格单的 Round 5（recall 插件化评估 + 全量矩阵 + 两段 PR）不在本批范围；coordinator 需要
  在主线 README 里提一句"仓库内已有一等插件"时再决定削减他处还是上调预算。

## 协议纠偏（博客资料 ≠ 官方 API）

本插件第一版是按博客/二手资料实现的。coordinator 用真 key 打冒烟时，**官方服务 200 通过、我们的请求 403**，
四处差异一次性暴露；随后以官方 API reference 为准重写了传输层、题型模型、答案解析与错误处理。逐条对照：

| 维度 | 博客/二手资料（第一版实现） | 官方 API（实测，返工后） | 后果 |
|---|---|---|---|
| 鉴权 | `X-API-Key: <key>` | `Authorization: Bearer <key>` | 云端每次 403 "Must supply an API key"，功能完全不可用 |
| 题型名 | `boolean` | `noul` | 422 校验失败：未知题型 |
| 字段名 | `description` | `instructions` | 422：字段缺失 |
| `choice` 的 criteria | `options: [...]` 数组 | `criteria: {选项: 描述或 null}` map | 422；且答案无法与选项对齐 |
| `score` 的 criteria | `min`/`max` + 自由描述 | 有序等级数组（≥2 级），**无 min/max** | 422 |
| 题型数 | 四种（含 `count`） | **三种**（无 `count`） | 声明了服务端不认的题型 |
| 答案形状 | `{value, p}` 统一 | `noul` / `choice`(+`confidence`,`probabilities`) / `score`(+`legend`) | 解析器读不到任何答案 |
| `usage` | 可选 | **必返** `input_tokens`/`output_tokens` | 记账单位该是 token 而不是调用次数 |
| 错误体 | `{"error":{"code"}}` | `{"detail":{"error_type","message"}}`，状态码 401/422/429/529 | 取不到错误类别；重试集合也错（旧写死 503，官方是 529） |

**返工范围**：`client.py`（Bearer、三种题型 + criteria 形状、按类型的答案解析、`detail.error_type`、
重试集合 {429,529}∪NETWORK、`usage_tokens`、state 允许 string/object/array）、`provider.py`（marker → `choice`
的 criteria map；无 marker → `noul`）、`reviewer.py`（`noul` 概率作决策信号 + 五档 `score` 作审计）、
`governor.py`（`noul`）、`plugin.py`（`register_usage_role("systemone", unit="tokens")`，去掉双角色）、
`smoke.py`（三种题型可选、打印真实 answers/usage/tokens）、`README.md`、以及**测试的假服务器与全部断言**。

**为什么离线套件没抓到**：假服务器是照着同一份错误资料写的——**用同一份规格造的 double 无法证伪那份规格**。
它只保证"我们与服务端的假定一致"，而这对一致恰好是错的。抓出它的是"真服务 200 / 我们 403"这一条外部事实，
所以本轮把冒烟脚本升成一等公民（`--kind noul/choice/score`，可逐个验证三种形状），并在 README 顶部标注
"协议以官方 API 为准"。

**分工的一个副产品**：为了不重复 `usage → token 数` 的规则，`usage_tokens` 只写在 `client.py` 一处，
再经 `tokens_of=` 注入两个监听器（兄弟模块是扁平模块、没有包上下文可做相对导入）——注入缝顺带把
"双拷贝" 挡在门外（Round 2 记账角色那次的教训）。

**仍未验证的**：`kev` 本机 server 的实际鉴权/响应是否与云端逐字节一致（本轮只按"无鉴权、同 wire"实现）；
`score` 的量纲与 `legend` 的语义（官方未定义，插件只把 `noul` 的 `p` 当决策信号）；`choice` 的 255 选项上限
是**客户端**自设的护栏，不是官方约束（错误信息里写明了）。
