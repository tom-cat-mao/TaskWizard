# Agent Note: 插件平面 Round 2 —— 配置键、记账角色、画面哈希与脱敏字面量的声明面

Status: implemented

## Problem

Round 1 把工具风险、pin 前缀、mode、CLI 与服务归属开成了声明面，剩下四处仍然各自封闭：

1. **配置键没有归属**：插件能挂工具、发事件、注册服务，却无法声明“我有哪些配置键”。值只能由插件自己
   `os.getenv`，于是优先级链、`.env` 前缀规则、CLI 覆盖层都要插件作者重新实现一遍，而且一定与内建不一致。
   manifest 的 `[plugin.config]` 更尴尬：读写往返支持它，加载路径却丢掉它——`plugin.py` 拿不到清单里配的值
   （Round 1 的 `pages/plugins.md` 明确写着这条限制）。
2. **记账角色双拷贝**：`usage.py` 的 `USAGE_ROLES` frozenset 与 `experience.py` 的第二份 `_TOKEN_ROLES` 各写
   一遍；插件角色既进不了 ledger（`record` 直接 raise），也进不了经验 schema（静默丢弃）。
3. **observe 事件缺画面指纹**：`epoch`/`screen_seq` 每次成功观测都 +1，marks 失败时也照样 commit，监听器
   没有任何“画面是否实质变化”的判据——等待稳定、卡死识别这类能力只能自己再截一次图。
4. **脱敏表封闭**：`phone_agent/config/redact.py` 的 `SENSITIVE_PATTERN` 覆盖通用模式（手机号/邮箱/订单号/
   api key/JWT/long base64），插件自己的敏感子串（自有账号前缀、设备相关 token 前缀）没有声明入口。

## Decision

四个声明点，纪律与 Round 1 一致：**只影响分类/记账/脱敏，绝不影响是否被允许**，全部 fail-visible。

- **配置键（R1）**：新模块 `phone_agent/v2/settings.py` 提供 `SettingRegistry`；装配缝是
  `ctx.register_setting(key, env_var=..., default=..., description=...)`，返回解析值。解析顺序：
  harness CLI 覆盖（`setting_overrides` 服务：声明键 -> 值）> shell 环境变量 / `.env` > 本插件 manifest
  `[plugin.config]` 同名值 > 声明的默认值。校验：`env_var` 必须 `PHONE_AGENT_` 前缀（`.env` 只加载该前缀）、
  key 为小写标识符、默认值只支持 str/bool/int/float/None、描述非空、环境/清单值读不成声明类型即报错；第二个
  能力声明同一键报错，release 撤销并同时清掉 `V2Config.plugin_settings` 里的镜像。
- **manifest 透传**：`load_plugin_spec` 把 `entry.config` 挂到 `CapabilitySpec.manifest_config`（frozen
  dataclass，用 `dataclasses.replace`；条目无 config 时返回原对象，既有 identity 测试不动），
  `assemble_capabilities` 在 apply 前发布进 ctx，插件用 `ctx.plugin_config()` 读任意键；该表同时是上面链里
  的清单层。CLI 维护路径没有 config 服务，此时只跳过“镜像到 V2Config”这一步，声明校验与解析照常。
- **记账角色（R3）**：新模块 `phone_agent/v2/usage_roles.py` 承载 `UsageRoleRegistry`（harness 预注册
  actor/compact/verifier/reviewer/distill，全部 `unit="tokens"`）；`usage.py` 只做薄再导出 + ledger 消费，
  `experience.py` 从同一注册表取 token 角色（双拷贝消失）。`unit="calls"` 角色每次 `record(role)` 记一次调用、
  拒绝带 payload；只进 `calls_by_role()` / `calls_total`，**不进** `total`，因此预算裁决点仍只读 token 总额。
  `by_role()` 保持“token 角色视图”的语义，经验档案的 `tokens_by_role` 不会混入调用次数。
- **画面哈希（G7）**：`observe` payload 增加 `screen_hash`，直接复用 `Observation.screen_hash`（observe 路径
  已为 screen binding 算过的 sha256 前 16 位）——只加字段，不加图像通道（P0 #6）。
- **脱敏字面量**：新模块 `phone_agent/v2/redaction.py` 的 `RedactionRegistry`；`ctx.register_redaction(literal)`
  声明，`middleware/_redact.py::redact_text` 先按字面量（长的先替换）再走内建正则。校验：单行、≥ 4 字符；
  同名可被多个能力共同持有（release 只撤自己那份）。只覆盖 v2 出口（trace/诊断证据流/Web 事件/流式预览），
  安全分类与提示词侧 sanitize 仍只读内建 pattern。

明确不做：不给声明面任何放行语义；不改安全分类规则；不把注册的字面量带进 prompt 侧清洗；不点名 manifest 里
未声明的键（插件可经 `ctx.plugin_config()` 自由读取非声明键）；不新增插件配置 CLI flag。

## Alternatives considered

- **把三个注册表塞进 capabilities.py / usage.py / middleware 包**。最强的理由：不新增模块，Module Map 不用动。
  为何被否：`capabilities.py` 会被 `plugins.py`（`main_v2.py plugin list`、Web bridge 的懒加载路径）导入，
  而它当前不拉 langchain；`usage.py` 会经 `middleware` 包 `__init__` 拉 langchain，`_redact.py` 同理。
  三个新模块都是零重量依赖，保住了“装配/离线路径不进重依赖”的既有边界。
- **manifest `[plugin.config]` 压过环境变量**。最强的理由：清单值描述“这个插件实例”，比全局 env 更具体，
  且用户写下 `[plugin.config]` 时期待它生效。为何被否：规格明示声明的键“纳入 CLI > env > `.env` > 默认”同一条链；
  且 `.taskwizard.toml` 随仓库提交、环境变量是本机操作面，让提交的文件压过操作者的 export 是错的默认方向。
  清单值因此只替换“声明的默认值”。（此为上报名单里的歧义点，理由已写进 `pages/configuration.md`。）
- **apply-kwargs 传 config 而不是 spec 字段**。最强的理由：不往冻结 dataclass 加字段。为何被否：`apply(ctx)`
  是所有插件与 `_owned_apply` 的公共契约，加参数是破坏性 API 变更（`PLUGIN_API_VERSION` 的语义）；声明面本
  就是该放这类元数据的地方，`compare=False`/`hash=False` 让它不参与身份比较，无 config 时保持对象同一性。
- **calls 角色也进 `by_role()`、也写进 `tokens_by_role`**。最强的理由：一个映射看全所有角色。为何被否：
  `tokens_by_role` 是固定 schema 里的 token 字段，把调用次数写进去是 schema 层面的假话；多一个
  `calls_by_role()` 比改 schema 诚实。
- **calls 角色静默忽略 message / estimate_tokens**。最强的理由：插件作者少踩一次报错。为何被否：token 数被
  当成调用数就是静默错账，与仓库 fail-visible 纪律相反；报错信息直接给出正确调用形态。
- **脱敏字面量单 owner（重名即 raise，像 pin 前缀）**。最强的理由：与既有声明面一致、实现最少。为何被否：
  两个插件保护同一个 secret 是常态而非冲突；用 owner 集合，release 只撤自己那份，价值判断与身份判断分开。
- **screen_hash 改用控件树 digest（或截图+树组合哈希）**。最强的理由：能捕捉“画面没变但控件变了”。为何被否：
  截图 sha256 在 observe 路径已经算过一次（B5 单次哈希），控件树 digest 要新增一遍遍历，且等待稳定语义问的
  就是“画面是否变”；将来若需要树 digest，可作为新字段追加，不必现在改语义。

## Consequences

- **收益**：四个声明点全部 fail-visible；`[plugin.config]` 第一次真正可用且有明确优先级；记账角色只剩一份事实
  源（经验 schema 自动认得插件角色）；observe 事件可直接判画面变化（Round 4 的 `wait_for_stable` 用它）；脱敏
  可扩展而不碰分类与 prompt。
- **代价**：新增三个模块与两个**进程级**注册表（usage roles / redaction）：declaration 的 release 由 ctx 包装，
  但进程全局意味着不经过装配的代码路径（如 `plugin list` 的一次性装配）也会短暂写入——由 release 撤销兜底。
- **代价**：插件 token 角色在**未加载该插件的离线进程**里不被识别，重建 `episodes.json` 时该角色的
  `tokens_by_role` 条目会被丢弃（harness 角色不受影响）。插件要在离线侧保住角色，需把键名当成静态事实写进
  自己的文档；本批不做角色持久化。
- **代价**：CLI 覆盖层当前没有 CLI flag，只有 `setting_overrides` 服务这一个入口，因此“CLI 最高”是预留能力，
  真正可达的是 env/.env 与 manifest 两层；插件配置的第三方键名不能进 `pages/configuration.md`（配置键同步门禁
  只认仓库内源码真实读取的键），只能由插件自己的文档列出。
- **代价**：`V2Config` 多一个私有字段 `_plugin_settings` 与三个读写方法；`CapabilitySpec` 多一个
  `manifest_config` 字段（不参与 eq/hash/repr）。文档预算 `pages/plugins.md` 2600 → 3100，`README.md` 只剩
  3 词余量，其余受管文档在原预算内吸收。
