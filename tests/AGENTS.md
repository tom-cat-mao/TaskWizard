# tests/ — 测试编辑规矩

> 全仓军规与 P0 索引在[根 `AGENTS.md`](../AGENTS.md)，这里只写**写测试时特有**的就近约束，不复述根文件。
> 真机验收的流程在 `.agents/skills/phone-agent-live-diagnosis/SKILL.md`：测试树内的绿灯不等于真机通过。

## 硬规矩

1. **改了哪层只跑哪层**。`tests/v2` 对应 v2 代码，`tests/web` 对应 Web 前端与 runner，`tests/docs` 对应
   文档与 `AGENTS.md`；本地不默认跑全套，全量矩阵交给 CI（根文件 Working Agreements）。新增测试放进它
   守护的那层目录，不要堆在 `tests/` 根下。
2. **只用 `.venv/bin/python -m pytest`**，绝不用系统 Python（根文件 Environment Gotchas）。解释器不对，
   依赖解析与 `sys.path` 行为都不可复现。
3. **离线是硬约束**：不碰真机、不连网关、不触发下载。设备与观测用 `tests/v2/_doubles.py` 的 fake，
   provider 用假 transport，记忆与插件落到 `tmp_path`；[`conftest.py`](conftest.py) 已守护真实 `memory/`
   目录与用户插件清单。
4. **`tests/docs` 保持标准库 + pytest**：CI 的 `docs` job 只装 pytest 与 mkdocs-material、不装
   `requirements.txt`，这是它便宜的原因。新增文档门禁测试不得引入第三方依赖，也不得 import `phone_agent`
   ——否则门禁本身成为最贵的 job。
5. **文档登记在 `tests/docs`**。受管文档要在 `tests/docs/doc-budgets.json` 立预算、在
   `tests/docs/test_doc_references.py` 的 `_DOCUMENTS` 清单里登记；文档提到的仓库路径必须真实存在。
6. **断言要指向约束**。失败信息说清违反了什么，别断言实现细节；只测当前行为，不为尚未存在的分支预留占位。

## 局部约定

- `tests/docs/`：每个文件一道门禁——词数预算 `tests/docs/test_doc_budgets.py`、路径引用
  `tests/docs/test_doc_references.py`、变迁措辞 `tests/docs/test_docs_freshness.py`、笔记格式
  `tests/docs/test_notes_format.py`、配置键一致 `tests/docs/test_config_keys_sync.py`、bash 块解析
  `tests/docs/test_doc_bash_blocks.py`。改门禁连带改这条清单。
- `tests/web/`：守护 Web 投影契约——bridge 快照与 run 目录协议、状态推进、App-KB 只读表、流式观察者。
  改 `phone_agent/web/` 时同批改这里的断言，别只改实现。
- `tests/v2/`：会话、事件、安全、预算、经验与配置的契约测试；跨模块行为在这里集成验证。
- `tests/skill/`：真机诊断 skill 的离线冒烟（20 个文件、自带 `conftest.py` 把 skill 的 scripts 目录放进
  `sys.path`），随 CI 的全量 `pytest tests` 跑，没有自己的分层命令。
- `tests/` 根：保留库的测试——`phone_agent/adb/`（test_adb_app_labels.py、test_adb_device_signals.py、
  test_adb_execution_contract.py）与 `phone_agent/config/` 的注册表（test_app_registry.py、
  test_policy_registry.py）；改动这些代码时跑全量或点名文件。

## 改完跑什么

```bash
.venv/bin/python -m pytest tests/v2 -q      # 改了 v2 代码
.venv/bin/python -m pytest tests/web -q     # 改了 Web 前端 / runner
.venv/bin/python -m pytest tests/docs -q    # 改了文档 / AGENTS.md
.venv/bin/ruff check .
```

全量矩阵（`lint` / `docs` / `test`）交给 CI，本地不默认跑全套（对应根文件 Development Commands）。
