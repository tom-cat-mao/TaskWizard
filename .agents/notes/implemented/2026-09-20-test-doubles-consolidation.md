# Agent Note: 测试双打收敛为单一事实源 + 观测域目录化

Status: implemented

## Problem

v2 测试树里同一族假设备被逐字复制，然后各自演进，改一份不会同步其余：

- **设备假件两份形态**：动作面 `FakeDeviceFactory`（记录 tap/swipe/launch）与观测面 4 份各自长出的版本
  （静态 xml、scripted dump、scripted screenshot、scripted foreground），观测面版本决定真实 `PhoneSession`
  的观测定时是否进入测试——settle 300ms 与 backoff 2s 都是墙钟成本。
- **`_SETTINGS_XML` 5 份拷贝、3 种取值**：2 节点 1080 宽、1 节点 1080 宽、1 节点 **1000** 宽（几何错配是
  被测对象）。同名常量漂移后，谁都不是权威。
- **TaskDoc 5 份**：字段（`reason`/`evidence_note`）、渲染节（是否出 `## 关键事实`）、条目行格式（
  `- {id} [{status}] {content}` vs `- [{status}] {id}: {content}`）三处都不同。
- **其余重复**：`FakeShot`/`FakeForeground` 5 份、scripted 模型 3 份（仅 `_llm_type` 标签不同）、
  `FakeConfig` 6 份、`FakePhoneSession` 2 份。
- **路径深度敏感**：`Path(__file__).resolve().parents[2]` 把"文件在树里的深度"写进了常量，文件一搬家就
  指向别处（`.env.example` / 仓库根）。

## Decision

`tests/v2/doubles/` 七个模块成为唯一事实源：`config`（基础 `FakeConfig`，全字段带默认，观测定时钉 0）、
`device`、`session`、`models`、`taskdoc`、`marks`、`paths`。消费方只覆写差异字段。

- **动作面与观测面合成一个 `FakeDeviceFactory`，观测面显式 opt-in（`observing=True`）**。理由是实测：
  动作面假设备一旦能回答 `get_foreground_app`，真实 `PhoneSession` 就会多出 `source="foreground"` 的
  `app/launched` 事件，`test_workflow_memory_wf3` 的发布断言当场变红。开关不是装饰，它守住"不改断言语义"。
- **`FakeTaskDoc` 取空默认（`goal_base=""`）**：空板渲染空串，于是"只是需要会话里有块板子"的测试
  （`test_compact` 14 处裸构造）不会被凭空塞进预览块、不会改变 token 计量；真正需要渲染的 7 处改为显式
  `_board()`（`test_pre_request_protocol`），唯一断言目标字符串的用例语义不变。
- **XML 常量保留三个具名常量**而非一个：`SETTINGS_XML`（2 节点 1080）、`SETTINGS_XML_SINGLE`（1 节点
  1080，重试/失败码用例断言 `len(marks) == 1`）、`SETTINGS_XML_NARROW`（1 节点 1000 宽，几何错配本体）。
- **`tests/v2/_doubles.py` 退化为 re-export shim**（批 5 删除），既有 17 处 import 一行不动；新增
  `tests/v2/conftest.py::no_production_timing` autouse 双保险钉住观测定时。
- **观测域 12 个文件 `git mv` 到 `tests/v2/observation/`**（只搬不拆，拆分留批 5），
  `paths.py::REPO_ROOT` 终结 `parents[N]`。
- **边界**：不改任何断言语义、不删用例、不合参数化；`tests/docs`、`tests/web`、`tests/skill`、`tests/` 根
  不碰；不引入 `pytest.ini`/`pyproject.toml`。

（批 5 待办：删 shim、拆观测域大文件、收编仍在本地的 scripted 模型与 `test_agent_loop` 的 `FakeConfig`。）

## Alternatives considered

- **只抽公共基类，4 份观测设备各自留在原文件。**
  - 最强理由：scripted dump / scripted shot / 静态 xml 的用法确实不同，合一个类容易长成"多模式巨类"。
  - 为何被否：差异其实是**参数**不是行为（同一组方法，返回值脚本化程度不同）。统一成
    `xml` / `shots` / `dumps` 三个参数后调用点更短，且参考帧的两处硬断言（`taps`、`shot2`）原样通过。
- **观测方法不带开关，人人有份。**
  - 最强理由：少一个概念，"假设备就是全能假设备"。
  - 为何被否：wf3 实测变红（见 Decision）。动作面假设备凭空获得前台查询能力，会让真实 `PhoneSession`
    走进生产分支，等于改断言。
- **`FakeTaskDoc` 沿用一个现存副本的默认 `goal_base`。**
  - 最强理由：`打开设置并连上 WLAN` 在 3 份副本里出现，看起来就是"标准默认"。
  - 为何被否：非空默认会把 `test_compact` 的 14 处裸构造从"无预览块"变成"有预览块"，
    `test_taskdoc_integration` 又要求空板不注入——两个方向的要求不可能同时满足，只能取空默认 + 显式声明。
- **5 份 `_SETTINGS_XML` 收敛成一个常量。**
  - 最强理由：同名、内容近乎相同，收敛最干净。
  - 为何被否：2 节点版本会让 `len(marks) == 1` 的断言变成 2；1000 宽版本本身就是 `contract_repair_s2`
    的被测对象，合并等于删掉一个用例的语义。
- **引入 `pyproject.toml` / `pytest.ini` 集中配置 lint 与 pytest。**
  - 最强理由：工具配置集中一处，省去命令行长参数。
  - 为何被否：本轮红线明确禁止；且新增配置文件会与并行分支的配置改动持续冲突，收益不抵成本。

## Consequences

- **收益**：观测设备、TaskDoc、截图/前台、scripted 模型各只有一份；`observing=True` 让"这个测试要不要
  设备能力"在调用点可见；观测定时双保险钉零，测试不再为生产等待付费；`REPO_ROOT` 让测试文件可以搬家。
  净删约 890 行重复代码（`_doubles.py` 231 行 → 24 行 shim，观测域 12 文件 −477 行）。
- **代价**：`doubles/device.py` 是一个带模式开关的类，读它需要先看 `observing` 的含义；`_doubles.py`
  作为 shim 会短暂出现"两处可导入"的中间态（批 5 删除后才唯一）。同族重复没有清零：`test_agent_loop` 的
  `FakeConfig`/`ScriptedToolModel`、`test_event_chain_behavior`/`test_plugin_e4`/`test_sibling_receipts`
  的 scripted 模型仍是本地副本，留给批 5——它们不在本轮的迁移清单内，动它们会超出"批 1+2"的授权范围。
- **验证**：批 1 与批 2 各自全量 `pytest tests -q` → 2257 passed / 4 failed（仅已知环境性：anthropic
  httpx2 ×2、`tests/web/test_model_streaming` ×2，与本次改动无关）；`ruff check . --select E4,E7,E9,F`
  通过；`pytest tests/docs -q` 316 passed。
