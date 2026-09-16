# phone_agent/web — 子树编辑规矩

> 全仓军规与 P0 索引在[根 `AGENTS.md`](../../AGENTS.md)，这里只写就近约束。
> 控制台正文在 [pages/console.md](../../pages/console.md)。

## 硬规矩

1. **单向观察，不反向拥有**。控制台不拥有设备 / 工具 / 路由（[P0 #21](../../AGENTS.md)）。
   与 runner 的往来分通道，文件是 [`v2/run_ipc.py`](../v2/run_ipc.py) 的整份 `RunPaths`
   （spec / events / control / run.json / runner.pid，都在 `PHONE_AGENT_RUNS_DIR` 下）：启动前写 spec，
   唯一的进程扩散是 `subprocess.Popen` 拉起 `python -m phone_agent.runner`，启动失败或 runner 消失时由
   [`bridge.py`](bridge.py) 补写终局 `run_end`；tail events 取事件；run.json 读写 usage；runner.pid
   读写判活；control 只追加 stop / hitl。除这次启动外不新增进程调用、不直连设备、不 import 设备工厂。
2. **状态只由终局事件推进**。`run_end` 是唯一能改写终态的输入，除 runner 外还有 bridge 的三个来源
   （启动失败 / runner 死亡 / 重连修复，原因 `error: runner_start_failed`、`error: runner_died`），其余事件与
   动画不得推进终态；「已请求停止」是中间态。事件名、payload 与终局状态值归
   [`v2/run_events.py`](../v2/run_events.py)，控制台状态与活动词表归 [`bridge.py`](bridge.py)，
   [`app.py`](app.py) 只做标签映射；其中 active 集合是**待收敛的重复**，新增状态同批改两侧。
3. **展示性读取永远只读**。控制台侧只有手动 dream 构造 App-KB store 并写回快照；run 内的 KB 写入属
   runner 与核心（[P0 #16b](../../AGENTS.md)、[P0 #16c](../../AGENTS.md)），Web 从不触发
   （[被动读取](../../pages/console.md#web-projection)）。
4. **流式只挂观察者**。生效值三层：角色 > 模型条目（含 modelOverrides）> 全局 `PHONE_AGENT_STREAMING`，
   无 provider 级（[正文](../../pages/configuration.md#streaming)）。抽屉生效值由 `resolve_streaming_mode`
   只读推导，覆盖随下一轮 spec 进 runner；运行面板只消费 `model_stream_*` 投影出的 stream_attempts，
   不拼装备用尝试的半截文本（[P0 #22](../../AGENTS.md)）。
5. **浏览器出站只有两路**。一路是投影后的事件快照，另一路是产出页（`/api/deliverables` 列目录、
   `/deliverables/{run_id}` 直供原始 HTML、iframe 预览、删除路由 unlink）。红线在根 `AGENTS.md` 的
   Data & Privacy：不发密钥、认证头、完整配置，网关地址给脱敏值（[投影边界](../../pages/console.md#web-projection)）；
   配置抽屉的覆盖只随 run 启动进 spec，不写 `.env`。
6. **可选是硬约束**。CLI 与 headless 的 `ThinPhoneAgent.run(...)` 必须始终可用；不为便利新增 Web 到核心
   依赖（[P0 #21](../../AGENTS.md)）；UI 依赖只落在 [`app.py`](app.py)（包 `__init__` 不带 nicegui、`__main__`
   懒加载；`scripts/fake_console_preview.py` 是文档化例外）。

## 局部约定

- [`app.py`](app.py)：只渲染，经 `WebRunBridge` 公开快照取数。真实性文案（未上报 / 参考帧未验证 /
  已请求停止）来自事件字段：缺字段显示明示缺失文案，不可证实的不显示，不用备用名 / 动画顶替。
- [`bridge.py`](bridge.py)：拥有运行状态与 run 目录句柄（整份 `RunPaths`）；新增事件消费在这里收敛字段，
  app 只读快照。
- 改事件字段必须同批核对 [`bridge.py`](bridge.py) 与 [`app.py`](app.py)（[P0 #23](../../AGENTS.md)）。

## 改完跑什么

```bash
.venv/bin/python -m pytest tests/web -q    # 改了本子树代码
.venv/bin/python -m pytest tests/docs -q   # 改了本文件 / pages / README
.venv/bin/ruff check .
```

全量矩阵交给 CI，本地不跑全套。
