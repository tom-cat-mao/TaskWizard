# Agent Note: WP2 ADB 稳健性与往返削减——观测重试退避、exec-out 截图、动作延迟降档

Status: implemented

## Problem

一次真机运行里，原子观测窗口的 dump 步在页面跳转后连续失败，整轮观测被拖垮；同一批还暴露出若干与 ADB 往返
和配置解析有关的小缺陷。逐项事实：

- **dump 超时太短**：`PHONE_AGENT_ACCESSIBILITY_TIMEOUT` 默认 `3.0s`，而 `uiautomator dump` 内部有
  `waitForIdle` 类硬编码等待；页面刚跳转、界面还在忙时，dump 会在这段等待里被我们的 subprocess 超时打断，
  provider 侧报 `provider_error`（`session.refresh_marks_sample` 的降级码之一）。
- **重试没有退避**：`PhoneSession.observe()` 对不稳定 marks dump 会整轮重采一次，但两次尝试背靠背——第一次
  撞上的忙乱窗口在第二次原样存在，重试成功率低。
- **`.env` 行内注释被吃进值里**：`load_project_env()` 用 `line.split("=", 1)` 取 value 后只 `strip()`，
  于是 `PHONE_AGENT_GROUNDING_PROVIDER="hybrid"   # hybrid | accessibility | locateanything` 会整行尾部
  一起写进 `os.environ`（引号判定随之落空）——`.env.example` 自己就有 3 行这种活跃写法，操作者照着抄就会
  踩到。
- **截图三趟往返**：截图走「设备端写文件 → `adb pull` → `rm`」三次 ADB 往返，观测每次都要付这份固定成本。
- **前台窗口查询整份回传**：`get_focused_window_or_app()` 把整份 `dumpsys window` 拉回本机，只为在本地
  grep 出 `mCurrentFocus` / `mFocusedApp` 两行。
- **动作延迟过度**：`phone_agent/config/timing.py::DeviceTimingConfig` 的 7 个被消费旋钮默认 `1.0s`，而
  tap/swipe/back 这类 ADB 动作本身是几十毫秒级。

本批由维护者授权（"8 个旋钮全降"），代码先落地、评审与测试后补；本笔记在评审当天写就，**决策理由中属于事后
重建的部分已标注**。

## Decision

六项改动，边界写清（含**不做**什么）：

1. **`accessibility_timeout` 默认 3.0 → 12.0**（`phone_agent/v2/config.py`）。取值落在 V2Config，env
   `PHONE_AGENT_ACCESSIBILITY_TIMEOUT` 可覆盖；不改 dump 实现，也不为单次失败引入无限等待。
2. **观测重试退避 + 轮数可配**（`phone_agent/v2/session.py::observe`）：新增
   `observe_retry_max_loops`（默认 `1`）与 `observe_retry_backoff_s`（默认 `2.0`）。不稳定 marks dump
   （`_UNSTABLE_MARK_CODES`：`timeout` / `provider_error` / `accessibility_xml_parse_error`）在还有额度时
   **先 sleep 再整轮重采**——重采包含截图，只重采 marks 是被禁止的（P0 #15）。空屏
   （`accessibility_dump_empty` / `no_interactive_marks`）不重试；最后一轮即使仍失败也照既有 B2 契约
   **提交带注解的零 marks 观测**（截图有效就不作废批次）。默认 `1` 即历史的「最多 2 轮」；两个键为负在
   `V2Config.from_env()` 直接 `ValueError`（可见失败，不静默钳制）。
3. **`.env` 行内注释按 shell 规则剥离**（`phone_agent/v2/config.py::_strip_inline_comment`）：`#` 只在
   「值首」或「前一个字符是空白」时开始注释，引号内的 `#` 一律字面量；因此 `sk-abc#def`、`/tmp/a#b/c`
   这类无空白 `#` 保持原样。不做转义序列解释、不做变量插值——解析器只负责把 `PHONE_AGENT_*` 灌进
   `os.environ` 且永不覆盖已有 shell 值。
4. **截图默认走 exec-out**（`phone_agent/adb/screenshot.py`）：`adb exec-out screencap -p` 从 stdout 直读
   PNG，1 次往返；旧的三往返路径保留为回退，由 `PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT`（默认 `on`，只有
   `1`/`true`/`yes`/`on` 算开）或显式 `use_exec_out=False` 切换。**失败语义不变**：任何一条失败路径都返回
   `is_valid=False / is_placeholder=True` 的占位图 + 稳定 `failure_code`（`screenshot_timeout` /
   `empty_screenshot` / `adb_screencap_failed` / `screenshot_exec_out_failed` / `secure_screenshot_blocked`
   / `screenshot_pull_failed`），绝不伪装成功（P0 #5）。legacy 路径的特有错误码与 `finally` 清理保持原样。
5. **前台窗口查询改为设备侧过滤**（`phone_agent/adb/device.py::get_focused_window_or_app`）：整条
   `dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'` 作为**一个 shell 字符串**交给 `adb shell`
   （与 `get_app_labels` 的既有写法一致），设备只回两行；过滤结果里连两个关键字都没有时才回退到整份
   dump 本地过滤。本地解析顺序（先 `mCurrentFocus` 后 `mFocusedApp`、`null` 跳过）与失败返回 `None` 不变。
6. **动作延迟默认 1.0 → 0.3**（`phone_agent/config/timing.py`）：`DeviceTimingConfig` 的 7 个被消费延迟
   与 `ActionTimingConfig` 的 4 个文本输入延迟一起降档；`double_tap_interval`（0.1）与
   `ConnectionTimingConfig` 不动。env 覆盖键全部保留，慢设备可调回。

评审时修正的既有实现缺陷（同一批内）：

- **设备侧管道从未生效**：`["dumpsys", "window", "|", "grep", ...]` 把 `|` 当独立 argv 传入，`adb shell`
  收到的是拼好的命令串而不是设备 shell 表达式，查询只会静默返回空 → 前台诊断退化成 `None`。改为单字符串，
  并补「argv 形状」回归测试与整份 dump 回退。
- **`observe_retry_max_loops > 1` 是空旋钮**：不稳定分支的守卫写成 `attempt == 0`，配置成 3 也只重试一次；
  改为 `attempt < retry_max_loops`，旋钮语义（额外轮数）与其文档一致。
- **CI lint 红**：`phone_agent/v2/session.py` 多了未使用的 `import os`（F401）、`screenshot.py` 留下未使用
  的模块级 `temp_path`（F841），两者都删。
- **悬空注释**：`config.py::from_env()` 末尾留着「这两个字段暂不进 V2Config」的注释，而字段就在 V2Config 里，
  已删。
- **文档归类**：`PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT` 原本被写进 `pages/configuration.md` 的
  「界面落地（Grounding）」表（V2Config 解析链），但它是在保留库调用点直读的键，已挪到
  `## 保留库直读键` 表，与 `PHONE_AGENT_SCREENSHOT_FORMAT` / `_JPEG_QUALITY` 同处。
- **既有测试被新默认值悄悄改道**：`tests/v2/test_observation_hardening.py` 的 subprocess 假件只实现 legacy
  通道，exec-out 成为默认后 4 个用例改道失败；已在那批用例显式钉 `use_exec_out=False`（它们本是 legacy
  通道回归测试），exec-out 通道另开 `tests/v2/test_screenshot_exec_out.py`。

## Alternatives considered

- **只加大 timeout，不做退避重试**：一个旋钮解决，代码最简。
  - **最强的理由**：忙乱期的长度不可预知，把等待一次性放宽到 12s 已覆盖框架内部等待；重试会再叠一层时延与
    一次截图成本。
  - **为何被否**：单次超时是「要么等到、要么丢掉」，没有第二次机会；页面上还有前台组件核对（before/after
    括号）这类不稳定，重试+退避是两条路径共用的既有机制。二者组合（超时放宽 + 退避重试）才是本批的决定。
- **指数退避**（首回 1s、次回 2s、四次 4s…）：更贴合"忙乱期越长越该等"的直觉。
  - **最强的理由**：单次失败成本与等待时长成反比，指数策略在长忙乱期里胜出。
  - **为何被否**：需要额外的状态与参数（基数、上限、上限行为），而默认只重试 1 次——指数序列在 N=1 时与线性
    完全等价；等真有 N>1 的部署数据再谈。
- **放弃设备侧 grep，回退到本地过滤**：零 shell 依赖，永远不会因设备 `grep` 缺失而退化。
  - **最强的理由**：`dumpsys window` 的 `grep` 依赖 toybox ROM 差异，「整份回传 + 本地过滤」在任何设备上都
    正确。
  - **为何被否**：整份 dump（数百 KB 级）回传是观测路径上重复发生的固定开销，而需要的信息只有两行。改为
    「设备侧过滤为主、无结果时回退整份 dump」，正确性由回退兜住，收益留给支持 grep 的设备。
- **把 `PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT` 接进 V2Config**（字段 + `DeviceFactory` 透传 + CLI 覆盖），
  与 `black_screen_detect` 同形。
  - **最强的理由**：P0 #8 的口径是配置经 V2Config；接进去后 CLI > env > `.env` > 默认 的链条对该键同样成立，
    文档也可以留在解析链表里。
  - **为何被否**（事后重建）：它的邻居 `PHONE_AGENT_SCREENSHOT_FORMAT` / `_JPEG_QUALITY` 都是保留库直读键，
    文档里已有明确的「保留库直读键」分类；本键当前的消费方只有 `get_screenshot` 一处，接进 V2Config 要多改
    `DeviceFactory` 与 `session.screenshot()` 的透传与兼容回退，却没有 CLI 消费者。**代价是保留的**：该键
    没有 CLI 覆盖，只认 env / `.env`。若将来需要 CLI 旗标，再按 `black_screen_detect` 的形状接。
- **删掉 legacy 截图路径，只留 exec-out**：代码减半，两条分支与两套错误码一起消失。
  - **最强的理由**：老设备不支持 `exec-out` 只是历史顾虑，留一条没人走的路径就是留一份不会被发现的腐坏。
  - **为何被否**：exec-out 支持度依 ROM 而异，且这是**观测**路径——观测不可用的代价是整轮 agent 停摆。
    保留回退符合 P0 #5 的 fail-closed 精神（宁可慢，不可无）。
- **延迟维持 1.0s（或只降到 0.5s）/ 引入单一「速度系数」**：最保守，回滚最容易。
  - **最强的理由**：等待是廉价的正确性保险，砍到 0.3s 在慢设备上可能让下一步观测读到旧帧；一个全局系数比
    8 个旋钮更好记。
  - **为何被否**：动作本身是几十毫秒级，1.0s 是数量级上的过度等待，而观测前还有独立的
    `observe_settle_ms`（默认 300ms）兜底；单一系数会破坏「每个动作可单独调」的既有 env 契约，慢设备只需
    调高对应键。
- **`_strip_inline_comment` 改用成熟包（python-dotenv）**：符合「能用成熟包净删手写代码就引包」的工作约定。
  - **最强的理由**：行内注释、引号、转义、`export` 前缀都是已解决的问题，手写状态机是把已知边界再实现一遍。
  - **为何被否**：本仓库的解析契约很窄（只认 `PHONE_AGENT_*`、永不覆盖 shell env、不插值），python-dotenv
    的整份语义（插值、`override` 策略、变量展开）都更宽，为一条 3 行的规则引入新依赖不划算；`tests/docs`
    的键同步门禁还按字符串字面量扫描代码，手工实现对该门禁更透明。

## Consequences

- **收益**：观测在忙乱期的自愈能力从「背靠背重试 1 次」变成「退避后可配轮次」；截图从 3 次 ADB 往返降到
  1 次，前台窗口查询从整份 dump 降到两行；`.env.example` 的活跃行终于能按操作者预期解析；动作间等待降到
  0.3s，单步省下的等待直接变成端到端时长。回归网：`tests/v2/test_observe_retry_backoff.py`（退避、轮数、
  整轮重采、空屏不重试）、`tests/v2/test_screenshot_exec_out.py`（单往返 argv、默认通道、开关、五种
  fail-closed）、`tests/v2/test_config.py`（用**真实** `.env.example` 行做行内注释用例 + 两个重试键的
  env/override/负值拒绝）、`tests/test_timing_defaults.py`（新默认值 + env 覆盖）、
  `tests/test_adb_device_signals.py`（管道 argv 形状 + 回退路径）。
- **代价**：最坏情形的观测耗时变长——`12s` 超时 × 3 轮 + `2s` 退避 × 2 已可到 40s 量级，只有真在忙乱期才会
  走到；退避是**固定** sleep，不做「页面已经稳定就提前返回」的探测（等价于放弃了更聪明但更复杂的探测方案）。
  设备侧 grep 在无 grep 的 ROM 上会多付一次整份 dump 的往返（正确性由回退保证，延迟不在保证范围内）。
  `PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT` 无 CLI 覆盖，只认 env / `.env`。`ActionTimingConfig` 的四个文本
  输入延迟仍然**无消费方**，本次降档对行为没有影响（`pages/configuration.md` 的 no-op 注记保持有效）。
  exec-out 是单次往返但**没有设备端临时文件**，`rm` 清理只对 legacy 路径有意义——两条路径的清理责任不对
  称，改动这一层的人需要同时看住两条分支的失败码。
- **本批不承诺**：草稿笔记里的真机成功率/耗时数字（"dump 成功率 ~25% → ~95%"、"payload ~68KB → ~72 bytes"
  等）没有随批留下可复核的证据文件，本笔记一律不引用；它们既不是本批的验收口径，也不该被后来的读者当事实
  使用。真机效果需要在 `.agents/skills/phone-agent-live-diagnosis/SKILL.md` 的证据流下重新测量。
