# systemone — System One 决策模型插件（一等插件）

`plugins/systemone/` 是仓库内的**一等插件**，也是插件平面声明面的完整 dogfood：它不碰任何 core 私有工厂，
只用 `apply(ctx)` 上公开的接缝与声明点。

System One 不是聊天模型：**一次调用 = 一段无结构 `state` + 一组带类型的提问**，模型并行回答每个问题，
每个答案带一个**校准概率**；没有自由文本通道，也没有图像通道。

!!! warning "协议以官方 API 为准（本插件返工过一轮）"
    插件最初按博客/二手资料实现，与官方 API reference 有四处实质差异（鉴权头、题型名、字段名、
    criteria 形状）。冒烟打真服务时全部暴露，已按官方规范重写；差异清单与代价见
    `.agents/notes/implemented/2026-09-25-plugin-plane-declaration-round4.md` 的「协议纠偏」一节。

## 四个接缝

| 接缝 | 形态 | 默认档位 |
|---|---|---|
| (a) provider | api family `systemone` + providers `jev`（云端）/ `kev`（本机 server） | 后端配置后可用 |
| (b) 安全复核 | `tool/execute` 前置监听器（`prepend=True`） | `shadow`（只写 trace） |
| (c) 画面稳定 | 工具 `wait_for_stable`（声明 `readonly`） | 始终挂载 |
| (d) 卡死调速器 | `model/pre_request` 观测监听器（**不注入提示词**） | `shadow` |

### (a) provider：`provider:model` 引用

```
PHONE_AGENT_MODEL=kev:kev-4b@qwen3          # 本机
PHONE_AGENT_MODEL=jev:jev-latest            # 云端（需 TYPESAFE_API_KEY）
PHONE_AGENT_SAFETY_REVIEWER_MODEL=kev:kev-8b
```

| provider | 默认 base_url | 模型 |
|---|---|---|
| `jev` | `https://api.typesafe.ai` | `jev-latest` |
| `kev` | `http://127.0.0.1:8787` | `kev-4b@qwen3`、`kev-8b`、`kev-9b` |

**官方 wire 格式**（`POST {base_url}/v1/systemone`）：

- 鉴权：云端 `Authorization: Bearer <TYPESAFE_API_KEY>`（**不是** `X-API-Key`，那样会 403）；
  本机 `kev` 无鉴权。
- 请求体：`{"model", "state", "questions"}`；`state` 可以是字符串、对象或数组。
- 提问条目：`{"type", "instructions", "criteria"}` ——字段名是 **`instructions`**。
- **只有三种题型**（没有 `count`）：
  - `noul`（布尔型）：`{"type":"noul","instructions":"...","criteria":{"true":"...","false":"..."}}`，criteria 可选；
  - `choice`：`{"type":"choice","instructions":"...","criteria":{"选项A":"描述或 null",...}}` —— criteria 是 **map**；
  - `score`：`{"type":"score","instructions":"...","criteria":["等级0","等级1",...]}` —— 有序数组 ≥2 级，**没有 min/max**。
- 响应：`{"model", "answers": {key: {...}}, "usage": {"input_tokens", "output_tokens"}}`，`usage` **必返**。
  - `noul` → `{"type":"noul","noul":0..1}`（概率即答案）
  - `choice` → `{"type":"choice","choice":str,"confidence":0..1,"probabilities":{...}}`
  - `score` → `{"type":"score","score":float,"confidence":0..1,"legend":{...},"probabilities":{...}}`
- 错误：HTTP 状态即类别——`401` 鉴权 / `422` 校验（body 指明字段）/ `429` 限流 / `529` 过载，
  body 形如 `{"detail":{"error_type","message"}}`。**只有 429/529 与网络错误重试**（有界指数退避 + 抖动），
  `401`/`422` 立即失败。

插件内部表示（wire 之外的自行设计）：`noul` → `value = (noul ≥ 0.5)`、`p = noul`；`choice` →
`value = choice`、`p = confidence`；`score` → `value = score`、`p = confidence`（`legend` 原样保留）。

**聊天适配约定**（不是协议的一部分，是本插件定的约定）：取**最后一条 user 消息**的文本作为 `state`；
若该消息带一行尾随标记 `Options: A | B | C`（取最后一个匹配行，至少两个非空选项），就发一个 `choice`
问题（选项即 criteria map 的键），把选中的选项原文作为回复；否则发一个 `noul` 问题
「Does the state warrant a yes?」，回复 `"yes"` / `"no"`。答复不在选项里、或布尔答复不是 yes/no——**报错**。

**构建期失败可见（P0 #8）**：`jev` 没有 `TYPESAFE_API_KEY`、`kev` 的 base_url 没有监听者、base_url 为空，
都在**构建时**直接抛错。绝不静默换端点、换网关或换成别的模型。

### (b) shadow 安全复核

状态 = 工具名 + **脱敏且有上限**的目标文本（`redact_context_text` + 截断；原始参数不外发），一次调用问两个问题：
`noul`（这个动作有风险/不可逆吗？）+ `score`（五档描述性风险等级，供审计）。

**决策信号是 `noul` 答案的概率 `p`**（`noul` 本身就是概率，不需要在数字之间做换算）；`score` 的
`confidence` 与原始 `score` 进 trace 供审计，其量纲是模型自己的（`legend` 不解释）。

- `off`：完全不挂载；
- `shadow`（默认）：每次复核写一条 trace 事件（`systemone_review`），**不动调用本身**；
- `on`：仅当**既有安全分类器本来会放行**、`noul` 为正、且 `p ≥ PHONE_AGENT_SYSTEMONE_CONFIDENCE` 时，
  **追加**一个否决（返回与内建预警同形的 `⚠️ 已拦截（未执行）` ToolMessage，模型按同样方式带
  `confirm_irreversible=true` 重发即可通过）。

它**只加不减**：内置闸门本来要拦的调用，插件只记录、不改变决定；已带 `confirm_irreversible=true` 的重发直接放行
（与内建预警一致）；端点故障、超时、答复畸形 —— 一律**不否决**并写一条 error trace。追加的闸门不该把一次
端点故障变成一次跑不动。已知交互：`PHONE_AGENT_SAFETY_MODE=off` 时插件是唯一闸门（`on` 是操作者的显式选择）。

### (c) `wait_for_stable`

轮询 `session.observe()`（唯一观测生产者，P0 #15），比较已提交帧的 `screen_hash`（G7），
连续 N 帧相同即返回：

```
wait_for_stable: stable=yes consecutive=2 polls=2 elapsed_ms=520 screen#14 app=com.x.y
本工具已提交新的观测批次（此前 mark id 失效）；需要 target_mark_id 寻址时先 read_screen。
```

超时是 `stable=no reason=timeout`；观测失败是 `reason=observe_failed: <异常>`；session 的帧不带哈希是
`reason=no_screen_hash` —— 三种都不冒充「稳定」。内建的 `wait`（睡固定秒数）仍在：这是一个**新增名字**，
不替换任何既有工具，装配前后内建行为逐字节不变。

### (d) 卡死调速器（本轮 shadow-only）

从 transcript 的 `tool_calls` 派生最近流线（`tool|intent` 签名，`intent` 缺失时退回目标描述），
最近 K 步签名完全相同就发一条 advisory trace 事件（同一签名只发一次）；后端可用时**额外**问一个
`noul`（「这些重复步骤是不是卡在循环里？」）并把校准 `p` 记在同一条事件里。

**为什么不注入提示词**：注入必须活过 auto-compact，而任何要免折叠的块都需要 core 侧的 pin 白名单行
（`v2/pins.py`）。插件认领不了这样的行，于是注入会在第一次折叠时消失——调速器就会**静默失效**，比不注入更糟。
所以本轮只发布诚实的观察面；注入留给开放白名单的那一轮。

## 启用

```toml
# .taskwizard.toml（仓库里已登记，默认 enabled = false）
[[plugin]]
name = "systemone"
enabled = true
path = "plugins/systemone"

[plugin.config]          # 可选：覆盖下面这些键的默认值
systemone_stable_polls = "2"
```

```bash
export PHONE_AGENT_SYSTEMONE_BACKEND=kev      # jev（云端）| kev（本机）；留空=只挂 wait_for_stable 与调速器
export PHONE_AGENT_SYSTEMONE_BASE_URL=        # 可选，覆盖所选后端的 base_url
export PHONE_AGENT_SYSTEMONE_MODEL=           # 可选，复核/调速器用的模型（缺省取目录首个）
export PHONE_AGENT_SYSTEMONE_REVIEW=shadow    # off | shadow | on
export PHONE_AGENT_SYSTEMONE_CONFIDENCE=0.9   # on 档追加否决所需的 p
export PHONE_AGENT_SYSTEMONE_TIMEOUT=10       # 协议超时，必须在 5-15s
export PHONE_AGENT_SYSTEMONE_RETRIES=2        # 429/529/网络的重试上限（401/422 永不重试）
export PHONE_AGENT_SYSTEMONE_STABLE_WAIT=6    # wait_for_stable 默认等待预算（0.5-60s）
export PHONE_AGENT_SYSTEMONE_STABLE_INTERVAL=0.5   # 轮询间隔（≥0.05s）
export PHONE_AGENT_SYSTEMONE_STABLE_POLLS=2   # 连续相同 screen_hash 次数（≥2）
export PHONE_AGENT_SYSTEMONE_GOVERNOR=shadow  # off | shadow（本轮不支持 on）
export PHONE_AGENT_SYSTEMONE_GOVERNOR_STEPS=3 # 判定重复的最近步数（≥2）
export TYPESAFE_API_KEY=...                   # 仅 jev 需要；本机 kev 不需要
```

链与内建键一致：CLI 覆盖 > shell env/`.env` > 本插件 `[plugin.config]` > 声明的默认值；
解析结果进 `V2Config.plugin_settings`，release 时随能力撤销。**任一值越界都在装配期报错**（不静默回落）。
键名一律 `PHONE_AGENT_` 前缀——只有该前缀会从 `.env` 加载；`TYPESAFE_API_KEY` 是后端原生变量名，
只从 shell 环境读，且一旦存在就注册为脱敏字面量（≥4 字符），出站状态、trace 与错误文本三处都不带它。

真机冒烟（coordinator 手工跑，需要真 key）：

```bash
TYPESAFE_API_KEY=... .venv/bin/python plugins/systemone/smoke.py             # 云端 noul（默认）
.venv/bin/python plugins/systemone/smoke.py --kind choice --backend kev      # 另两种题型
.venv/bin/python plugins/systemone/smoke.py --kind score  --backend kev
```

输出是一行 JSON：`{"ok": true, "backend", "model", "answers": {key: {kind, value, p, ...}}, "usage", "tokens", "latency_ms"}`。

## 诚实的限制

- **无自由文本、无图像**：协议只回带类型的答案 + 校准概率。想让模型写一段话、看一张截图，用别的接缝。
- **不能当 actor**：它发不出 tool call，所以 `bind_tools` 直接 `NotImplementedError`（早失败 > 空转的 run）。
  把它接到 `safety_reviewer` / `verifier` / `memory` 这类纯文本角色上。
- **校准概率 ≈ 2/3 量级的准确率**：`p` 是模型的自我校准，不是真值概率；按 `p` 设门槛能换来精度，
  但换不来确定性。默认 `shadow` 就是为了先量它、再信它。
- **选项顺序敏感**：`choice` 的 criteria 是有序 map，同一组选项换个顺序可能换答案；跨 run 比对时请固定顺序。
- **`score` 的量纲由模型自定**：协议不给 min/max，回的是 `score` + `legend`；插件只把 `noul` 概率当决策信号，
  `score` 仅入 trace，不做跨 run 的数值比较。
- **本地 server 单通道**：`kev` 一次只处理一个请求，客户端用进程内锁串行化（同一 base_url 共享一把锁），
  所以并发复核不会把单槽 server 变成超时队列——代价是延迟线性叠加。
- **进程级 transport**：`register_api_builder` 是进程级表（与内建三个 api 同一张），release 时注销；
  同进程两个 agent 共享最后一次注册。这是平面既有约定，不是本插件引入的。
- **文本状态**：外发的是「工具名 + 脱敏目标」，不是完整 transcript（隐私与延迟都更小）。

## 文件

| 文件 | 职责 |
|---|---|
| `plugin.py` | `CAPABILITY`、`apply`/`release`、声明点、兄弟模块加载器（`load_sibling`） |
| `client.py` | 官方协议客户端：三种题型、Bearer 鉴权、错误码/重试、`kev` 串行化、token usage、key 永不外泄 |
| `provider.py` | LangChain 适配 + api builder + `jev`/`kev` 两个 `ProviderSpec` |
| `reviewer.py` | `tool/execute` 复核监听器（noul 概率 + score 审计，shadow / 追加否决） |
| `stability.py` | `wait_for_stable` 工具 |
| `governor.py` | `model/pre_request` 卡死调速器（advisory） |
| `smoke.py` | 真服务冒烟（`--kind noul/choice/score`） |
| `../../tests/plugins/` | 离线契约测试（假协议服务器实现官方形状与 401/422/429/529） |
