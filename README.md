# TaskWizard

LLM 驱动的安卓手机操作 Agent：模型看一眼屏幕、想一步、动一下，通过工具逐步操作真实设备。运行壳
（harness）只提供工具、安全边界、上下文管理与运行记录，不替模型编排流程。

[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](setup.py)
[![CI](https://img.shields.io/badge/CI-GitHub_Actions-lightgrey?logo=githubactions)](https://github.com/tom-cat-mao/TaskWizard/actions)

## 快速开始

需要 Python 3.10+；一台开启 USB 调试、能被 `adb devices` 识别的 Android 7.0+ 设备；文字输入需安装
[ADBKeyboard](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk)。

```bash
git clone https://github.com/tom-cat-mao/TaskWizard.git && cd TaskWizard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

复制模板后按部署填写网关、模型与 key（默认值面向本地无鉴权网关，正式网关请显式指定）。

命令行运行一条自然语言任务（多设备时加 `--device-id <serial>`）：

```bash
.venv/bin/python main_v2.py "打开设置进入 WLAN" --device-id <serial>
```

本地 Web 控制台（默认只监听 `127.0.0.1:8080`）：

```bash
.venv/bin/python -m phone_agent.web --device-id <serial> --port 8080
```

控制台设备编号留空表示自动选择；App-KB 定时刷新只读快照，记忆变化只记审计，不阻止启动。

常用维护命令：

```bash
.venv/bin/python main_v2.py --dream
.venv/bin/python main_v2.py --distill
.venv/bin/python main_v2.py --review-lessons
.venv/bin/python main_v2.py --learn-alias "小红书=com.xingin.xhs"
.venv/bin/python main_v2.py --list-models
```

运行结束打印 `steps=… reason=…` 与 trace 路径；退出码 `0` 表示 run 报告 `success=true`，非零只是当前 CLI
对失败/接管/熔断的粗略区分，具体以 `reason` 与 trace 为准。`reason` 由终局原因决定（成功时是模型的 finish
摘要），不保证固定字符串。逐步工具回执与观测过程在 trace 文件和控制台步骤时间线中查看，stdout 不会逐步打印。

## 能做什么

| 能力 | 一句话 | 详情 |
|---|---|---|
| Marks-first 操作 | 执行动作必须绑定当前屏幕元素；过期、歧义、未命中的目标 fail-closed 拒绝执行 | [架构](https://tom-cat-mao.github.io/TaskWizard/architecture/) |
| 观测与参考图 | 原子观测（前后台一致 + 单次采样）；失败但已取到有效截图时返回标注过的未验证参考图 | [架构](https://tom-cat-mao.github.io/TaskWizard/architecture/) |
| 安全预警制 | 风险动作先预警（不执行、不叫人），模型显式确认后才执行；四档模式 | [安全模式](https://tom-cat-mao.github.io/TaskWizard/safety/) |
| 可信完成 | TaskDoc 任务板 + 流程线；finish 两段式，独立上下文验收器复核（故障 fail-open 记 `skipped`） | [架构](https://tom-cat-mao.github.io/TaskWizard/architecture/) |
| 成本控制 | token 阈值与一次有效 finish 确认续办；独立 32k 软工作目标与有界成组压缩 | [配置参考](https://tom-cat-mao.github.io/TaskWizard/configuration/) |
| 应用记忆 | App-KB 别名与启动事实；隐式纠正、dream 整理、用户纠正入口 | [记忆与自进化](https://tom-cat-mao.github.io/TaskWizard/memory/) |
| 经验与回注 | episode 档案（固定 schema、隐私白名单）+ 离线蒸馏自判分级 + `MEMORY_RAG=on` 受控注入 | [记忆与自进化](https://tom-cat-mao.github.io/TaskWizard/memory/) |
| 产出物 | `write_document` / `update_document` 写成本 run 的单页 HTML | [Web 控制台](https://tom-cat-mao.github.io/TaskWizard/console/) |
| 模型提供方 | 默认零配置走 `.env` 网关；多提供方用单层 `models.json` + 角色 `provider:model` 路由；可选 `PHONE_AGENT_FALLBACK_MODEL` 在首选构建/调用失败时降级一次并写审计；可选 `PHONE_AGENT_STREAMING` 让模型流式接收并聚合完整消息（headless/各角色/控制台一致，控制台另可增量观察正文） | [配置参考](https://tom-cat-mao.github.io/TaskWizard/configuration/) |
| 插件系统 | 策略层全事件化；插件经 entry points 或 `plugin add` 挂入同一装配层 | [插件开发](https://tom-cat-mao.github.io/TaskWizard/plugins/) |
| 上下文观测 | 每个实际模型尝试独立准备与容量检查；trace 记录有序客户端差异与可空 input/output/cache read/write，缓存命中不抵扣原 token 预算 | [架构](https://tom-cat-mao.github.io/TaskWizard/architecture/) |
| 本地控制台 | 固定顶栏 + 设备栏 + 任务工作区（步骤/任务板/应用库/记忆/产出页签，人工确认置顶）；runner 子进程执行、可重连回放；可选模型流式增量面板 | [Web 控制台](https://tom-cat-mao.github.io/TaskWizard/console/) |

完成复核后若又尝试了普通操作，即使命令结果未知，也需要重新取得复核包才能在 token 边界续办确认。

自定义模型尚未声明上下文能力时，压缩仍保留无法识别的 Provider 续接信息；只有明确的重复调用表示或 SDK 记账字段可以忽略。

## 文档

- **完整文档站：<https://tom-cat-mao.github.io/TaskWizard/>**（源文件在 [`pages/`](pages/)）
- 配置项的**唯一详细手册**：[配置参考](https://tom-cat-mao.github.io/TaskWizard/configuration/)；模板见 [`.env.example`](.env.example)
- OpenAI 兼容网关可用 `compat.requestApi=auto|chat|responses` 明确协议（默认保留 SDK auto）；支持的端点可显式启用 `cachePolicy=stable-prefix`。模型 context 支持可由插件提供，缓存准备仅允许缓存参数，输出上限按 SDK 实际序列化评估；默认不猜网关缓存能力，详见[配置参考](pages/configuration.md)与[插件开发](pages/plugins.md)。
- 编码约束、开发命令与模块地图：[AGENTS.md](AGENTS.md)
- 特定批次的执行与验收记录：[`docs/execution/`](docs/execution/)

## 安全与隐私

- 风险动作默认走预警制：工具返回世界事实与选项，**不执行也不叫人**；模型带 `confirm_irreversible=true`
  重发才执行。`ask_user` / `take_over` 在任何档位都会停下来等待人工。
- 控制台默认只监听 `127.0.0.1`；浏览器只接收投影后的事件，不发送 API key、认证头或完整配置。
- 不要把 `.env`、`memory/`、`outputs/`、trace 或真实设备截图提交、发布到 Pages 或粘贴进 Issue。
- 生产 trace 对文本截断并脱敏、不落截图 base64；真机 `FLAG_SECURE` 保护页不下发黑图。

## Contributing

欢迎提交 Issue 和 Pull Request；开始编码前请先阅读 [AGENTS.md](AGENTS.md) 的开发契约与 P0 约束。

## License

本项目基于 [Apache License 2.0](LICENSE) 开源。
