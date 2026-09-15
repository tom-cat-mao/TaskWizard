# phone_agent/v2 — 子树编辑规矩

> 全仓军规与 P0 索引在[根 `AGENTS.md`](../../AGENTS.md)，这里只写**本子树特有**的就近约束，不复述根文件。
> `v2/…` 指本目录；`middleware/…`、`tools/…`、`providers/…` 是其下同名子目录。

## 硬规矩

1. **docstring 是契约真相源**。模块 / 类 / 函数的 docstring 写当下契约：入参出参、失败分支、env 键、
   事件 payload。改行为同一批改 docstring；对用户可见的行为再同步 `pages/` 对应锚点章节
   （[P0 #23](../../AGENTS.md)，同步时机见根文件的 Version Management）。设计理由写 `.agents/notes/`，
   两种载体不互抄。
2. **策略一律是事件总线监听器**。新增或修改策略挂在 [`events.py`](events.py) 的接缝上，由
   [`capabilities.py`](capabilities.py) 装配；编译后的 LangChain 中间件栈只放桥接器与可选
   `extra_middleware` 观察者，不能成为策略的家（[P0 #18](../../AGENTS.md)）。
3. **坐标换算只在 [`coords.py`](coords.py)**。模型只见 0-1000 相对值，工具不得自行乘屏宽屏高，也不得让绝对
   像素出现在模型可见文本里（[P0 #1](../../AGENTS.md)）。
4. **观测只有一个生产者**。[`session.py`](session.py) 的 `observe()` 负责静置、截图、marks 与 epoch；
   工具只消费结果并渲染回执，不自己截图、不自己 dump accessibility、不自己铸 mark
   （[P0 #15](../../AGENTS.md)）。
5. **工具失败返回错误文本**。错误 / 未命中 / 歧义一律 fail-closed 回文本，绝不伪装成功，不确定就只说未知，
   不声称未执行（[P0 #5](../../AGENTS.md)）。回执带 `intent` 与 `note`（[P0 #11b](../../AGENTS.md)）。
6. **设备操作经 `DeviceFactory` → `adb/`**，本子树内没有直接 ADB 调用（[P0 #7](../../AGENTS.md)）。

## 局部约定

- `tools/`：操作、感知、控制（finish / ask_user / take_over）、TaskDoc、deliverable 各自成文件；工具 schema
  就是对模型的契约，改参数名或回执语义等于改 P0，须与 `pages/` 同批同步。
- `middleware/`：`budget.py`、`compact.py`、`_tokens.py` 的预算与压缩表述有独立正文锚点，改前先读锚点。
- `providers/`：网关差异（认证头、采样上限、流式协议、能力旗标）只在 builders 里翻译成协议参数，
  调用点不做分支（[P0 #22](../../AGENTS.md)）。
- `runner.py`、`run_ipc.py`、`run_events.py` 是 Web 观察面：Web 进程不拥有设备访问、工具执行或工作流路由，
  只有终局事件推进状态（[P0 #21](../../AGENTS.md)）。
- 日志 / trace / 经验写入遵守脱敏与固定 schema（[P0 #6](../../AGENTS.md)、[P0 #16](../../AGENTS.md)）；
  新增落盘字段前先确认它在 schema 之内。

## 改完跑什么

```bash
.venv/bin/python -m pytest tests/v2 -q     # 改了本子树代码
.venv/bin/python -m pytest tests/docs -q   # 改了 AGENTS.md / README.md / docs / pages
.venv/bin/ruff check .                     # 改动前后都值得跑
```

全量矩阵（`lint` / `docs` / `test`）交给 CI，本地不默认跑全套。
