# WP-PLUGIN-E 全量迁移计划（工作稿，不入库）

> 目标：中间件栈里所有策略件迁到事件总线，栈里只剩桥接器。依据 traex-11 研究：顺序保证按性质落点（数据依赖→方法内调用；洋葱深度→注册顺序嵌套；不变量→测试护栏），`on()` 不加数字 order。

## 现状家底（代码核实）

| order | 件 | hooks | 迁移难点 |
|---|---|---|---|
| 0 | control_hitl | wrap_tool_call（中断 ask_user/take_over、hard 档 approve/reject） | 中断语义 |
| 20 | compact | before_model（can_jump_to 无；T1/T2 折叠） | 必须先于 images（C1） |
| 30 | images | before_model | 与 C1 绑定 |
| 40 | budget | before_model（`can_jump_to=["end"]`）、after_model、wrap_model_call | jump_to 语义 |
| 45 | model/pre_request 桥 | before_model | 已有 |
| 50 | trace | wrap_model_call、wrap_tool_call、after_agent | 需要"包住真实调用"的事件面 |
| 60 | model_limit | before_model（exit_behavior=end） | jump 语义 |
| 70 | diagnostic | before_agent、before_model、wrap_model_call、wrap_tool_call、after_agent | 面最广 |
| 80+ | extra_middleware | 任意 | **不迁**（外部 API） |
| 90 | safety | wrap_tool_call | 已委托 bus，剩桥位搬家 |

## 事件面缺口（E1 补齐）

现有：`run/start|end`、`observe`、`tool/pre_execute`（洋葱，terminal=handler）、`model/pre_request`（洋葱，桥在 45）。

新增：
1. **`model/post_request`**（emit）：after_model 面——budget 累积。
2. **`model/request`**（洋葱，terminal=真实模型调用）：wrap_model_call 面——trace/budget/diagnostic 包住模型调用。新桥接 middleware（wrap_model_call hook）。
3. **`tool/execute`**（洋葱，terminal=真实工具执行）：把总线调用点从 safety(90) 搬到 core 桥（order 0 位）。监听器嵌套=注册顺序：trace（最外）→ diagnostic → hitl → safety（最内，最后注册）。REJECT→warning ToolMessage 的转换逻辑从 safety 移到桥。
4. **`agent/after`**（emit）：after_agent 面——trace/diagnostic 收尾。
5. **JUMP_END 哨兵**（events.py 导出）：监听器返回它 → 桥转成 `{"jump_to": "end"}`。承接 budget/model_limit 的熔断语义。

桥接器终态（LangChain 栈只剩这些 + extra_middleware）：
`tool-bridge(0)` → `pre-request-bridge(10)` → `wrap-model-bridge(20)` → `after-bridge(30)`。位置本身不再承载策略顺序。

## 迁移表

| 件 | 迁为 | 顺序保证怎么活 |
|---|---|---|
| compact | `model/pre_request` 监听器，**方法体内先调 `ctx.service("context_pruner")` 再折叠**（D5 模式落地） | C1 从排位搬进方法体；images 中间件删除 |
| images | 删除（逻辑已被 D5 服务化） | — |
| taskdoc | 已是监听器（D6） | pins 契约 |
| budget | `model/pre_request`（熔断→JUMP_END）+ `model/post_request`（累积）+ `model/request`（包调用） | D2 已证顺序无关 |
| model_limit | `model/pre_request` 监听器（→JUMP_END），core 注册 | 保险丝，早晚触发等价 |
| trace | `tool/execute` 最外监听器 + `model/request` 最外 + `agent/after` emit | "外层看到内层 return"=洋葱天然语义；被拦调用=next() 返回 REJECT/warning，照样记录 |
| diagnostic | 同 trace（次外层） | 同上 |
| control_hitl | `tool/execute` 监听器（core 注册，居 trace/diag 之内、safety 之外） | 嵌套由注册顺序决定（core 先于能力装配） |
| safety | 已是监听器；调用点从 safety 中间件搬到 tool-bridge，SafetyWarningMiddleware 删除，hard 档 HITL 同样迁为监听器 | 最内=最后注册（装配顺序保证）；REJECT 短路单调 |

## 执行阶段（kimi 细分，逐阶段验收）

- **E1（地基，1 个 agent）**：新事件面（model/post_request、model/request、tool/execute、agent/after、JUMP_END）+ 四个桥接器，**纯新增不迁移**——现有中间件全不动，桥全 no-op，940 测试原样过。
- **E2（工具域，1 个）**：trace/diagnostic/hitl 迁 tool/execute 监听器；safety 调用点搬桥；SafetyWarningMiddleware 删除；四档行为逐字节等价。
- **E3（模型域，与 E2 并行，agent.py 改动区域划开）**：compact 监听器化+方法内调 pruner、images 中间件删除、budget 三件套迁移、model_limit 迁移。C1 等价测试（摘要器拿到真实历史）必须过。
- **E4（收尾，等 E2）**：trace/diagnostic 的 model/request、agent/after 面迁移；删空壳中间件；`_MIDDLEWARE_ORDER` 表删除；全量回归。

## 验收标准（每阶段）

- 全量 tests 过（当前 940 + 各阶段新增）、ruff clean
- 行为等价：safety 四档、compact T1/T2、钉板免疫、预算熔断、loop 保险丝、trace 不变量（D2 护栏）原样绿
- 终态断言测试：`create_agent` 的 middleware 列表只剩桥接器 + extra
