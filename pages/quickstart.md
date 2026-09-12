# 快速开始

## 前置条件

| 条件 | 验证方式 |
|---|---|
| Python ≥ 3.10 | `python3 --version` |
| ADB 可用 | `adb version` |
| 安卓设备开启 USB 调试并被识别 | `adb devices` 列出设备 |
| 设备安装 ADBKeyboard | [APK 下载](https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk) |
| OpenAI-compatible 视觉模型网关 | `curl $BASE_URL/models` |

## 安装

```bash
git clone https://github.com/tom-cat-mao/TaskWizard.git
cd TaskWizard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，按部署填入网关、模型与 key（默认值面向本地无鉴权网关）：

```bash
PHONE_AGENT_BASE_URL="https://你的网关/v1"
PHONE_AGENT_MODEL="你的模型 id"
PHONE_AGENT_API_KEY="你的 key"
```

## 运行

命令行运行（多设备时加 `--device-id <serial>`）：

```bash
.venv/bin/python main_v2.py "打开设置进入 WLAN"
```

Web 控制台（默认 `http://127.0.0.1:8080`）：

```bash
.venv/bin/python -m phone_agent.web --port 8080
```

运行结束打印 `steps=… reason=…` 与 trace 路径；退出码 `0` 表示 run 报告 `success=true`，非零只是当前 CLI 的
粗略区分，具体以 `reason` 与 trace 为准。工具回执与逐步过程在 trace 文件和控制台步骤时间线中查看，stdout
不会逐步打印；最终是否达成目标以实际任务验收为准，不要只看单个字符串。

只需要一个网关时不用额外配置；需要多提供方/多模型时可选一个单层 `models.json`，见[配置参考](configuration.md)。

## 常见问题

| 症状 | 原因 | 处理 |
|---|---|---|
| `adb devices` 无设备 | USB 调试未开 / 授权未点 | 重新插线，手机上点"允许调试" |
| 网关 403 | 网关有额外访问控制 | 检查 `PHONE_AGENT_BASE_URL` 与 key；需要自定义请求头时用 `PHONE_AGENT_HTTP_HEADERS` |
| 截图失败 | 当前页面被 FLAG_SECURE 保护（登录/支付页） | 属预期行为；agents 会收到保护提示并可能请求人工接管 |
| 步数耗尽 `loop_fuse` | 任务超出保险丝 | 调大 `--max-steps`，或缩小任务范围 |
| `unknown app` | 应用未安装或名称未收录 | 先看控制台「应用库」页确认本机应用名 |

## 数据与隐私

运行数据默认写入本机 `memory/` 与 `.traces/`；`.env`、`memory/`、`outputs/` 都不应提交或发布。生产 trace
对文本截断并脱敏、不落截图 base64；控制台默认只监听 `127.0.0.1`，浏览器收不到 API key 或认证头。

## 下一步

- [配置参考](configuration.md)：调整预算、安全模式、记忆开关与模型路由
- [Web 控制台](console.md)：界面各区说明
- [安全模式](safety.md)：四档门控的选择
- [记忆与自进化](memory.md)：App-KB、经验档案与受控回注
