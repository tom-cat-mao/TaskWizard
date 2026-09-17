# 操作手册

本页只给操作序列：编号步骤、可直接粘贴的命令、每步的验证方式。语义归宿——配置键与默认值见[配置参考](configuration.md)，
App-KB、经验档案与回想见[记忆](memory.md)，蒸馏、分级与注入见[自进化](evolution.md)，插件接缝与授权见
[插件开发](plugins.md)，提供方与角色路由见[模型提供方与路由](providers.md)，观察层见[Web 控制台](console.md)。

以下命令都在仓库根目录用共享虚拟环境执行；安装与前置条件见[快速开始](quickstart.md)。多设备时给 run 类命令补
`--device-id` 与 `adb devices` 列出的 serial；`plugin` 子命令走独立 parser，不接受该参数。

## 真机接入与首跑验证

1. 确认设备在线：

    ```bash
    adb devices
    ```

    serial 出现在输出列表里才继续。

2. 在 `.env` 配三个键（下列默认值面向本地无鉴权网关）：

    ```bash
    PHONE_AGENT_BASE_URL="http://localhost:8000/v1"
    PHONE_AGENT_MODEL="autoglm-phone-9b"
    PHONE_AGENT_API_KEY="EMPTY"
    ```

    远端网关换成实际地址、模型 id 与 key；完整键表见[配置参考](configuration.md)。

3. 用小步数保险丝跑一次真任务：

    ```bash
    .venv/bin/python main_v2.py "把闹钟设到 7 点" --max-steps 5
    ```

4. 验证：stdout 打印 `steps=… reason=…` 与本次 trace 路径；退出码 `0` 表示 run 报告成功，`2` 是人工接管，
   `3` 是预算或保险丝耗尽，其余非零为运行错误。trace 默认落在 `.traces/`（`PHONE_AGENT_TRACE_DIR` 可改），
   每个 run 一个 JSONL 文件：

    ```bash
    ls -lt .traces | head
    ```

    首跑全流程与常见问题见[快速开始](quickstart.md)。

## 记忆与自进化维护

以下命令都在 run 之间执行，按需选择顺序。

1. `--dream` 整理 App-KB、归档经验档案、对账 lesson：

    ```bash
    .venv/bin/python main_v2.py --dream
    ```

    摘要以一行 `dream: {...}` 打印；别名纠正与条目淘汰规则见[记忆](memory.md#app-kb)。

2. `--distill` 把新档案蒸馏成 lesson 与过程卡：

    ```bash
    .venv/bin/python main_v2.py --distill
    ```

    `PHONE_AGENT_EVOLUTION=off` 时该命令打印错误并以退出码 1 结束；分级与游标见[自进化](evolution.md#distill-promote)。

3. `--review-lessons` 交互式审批 proposed / needs_review 候选，逐条选 `a`（批准）/ `r`（撤销）/ `s`（跳过）：

    ```bash
    .venv/bin/python main_v2.py --review-lessons
    ```

    人工通道的写入边界见[自进化](evolution.md#lesson-evolution)。

4. `--rebuild-vec` 全量重建语义召回索引：

    ```bash
    .venv/bin/python main_v2.py --rebuild-vec
    ```

5. 别名维护：

    ```bash
    .venv/bin/python main_v2.py --learn-alias "闹钟=com.android.deskclock"
    .venv/bin/python main_v2.py --forget-alias "闹钟"
    ```

    `--learn-alias` 写全局 `user` 别名（最高信任，包未安装时警告后仍保存）；`--forget-alias` 只删该名称的全局
    `user` / `learned` 条目。

6. `PHONE_AGENT_MEMORY_RAG=off` 时 recall 能力不挂载，第 2 至第 4 步报 capability 不可用并以退出码 1 结束
   （三档边界见[自进化](evolution.md#lesson-injection)）。

查看入口（用编辑器打开即可，字段口径见[记忆](memory.md#experience-plane)与[自进化](evolution.md#distill-promote)）：

```bash
ls memory/lessons/lessons.json memory/experience/recall_stats.json
```

## 插件从授权到验证

`plugin add` 即代码执行授权，信任级与 `pip install` 相同——只添加你审过源码的目录或包。

1. 登记本地插件目录，或安装 pip 包：

    ```bash
    .venv/bin/python main_v2.py plugin add ./my-plugin
    ```

2. 确认条目已启用、加载状态与 API 兼容性：

    ```bash
    .venv/bin/python main_v2.py plugin list
    ```

    每行是一个 JSON 回执，固定含 `name`、`version`、`enabled`、`path` 与 `state`；`cap_id` 只出现在已启用且加载
    成功的行，未启用的行 `state` 为 `disabled`，加载失败或 `REQUIRES_API` 不匹配的行 `state` 为 `error` 并带
    `reason`。

3. 跑一次任务（或开 Web 控制台），在 trace 的 `capability_snapshot` 事件里确认该 cap 的 `state` 为 `active`；
   控制台「记忆」页也列能力状态。

4. 加载期 fail-visible：已启用条目加载失败时启动即报 `error: plugin load failed: …` 并以退出码 1 结束，不静默降级。

接缝与授权契约见[插件开发](plugins.md#plugin-authorization)，能力挂载见[插件开发](plugins.md#capability-mount)。

## 接入新模型网关

1. 写一份单层 `models.json`（最小条目）：

    ```json
    {
      "providers": {
        "secondgw": {
          "api": "openai-completions",
          "baseUrl": "https://gateway.example/v1",
          "apiKey": "$SECOND_GW_KEY",
          "models": [
            {"id": "vision-model-a", "inputModalities": ["text", "image"]}
          ]
        }
      }
    }
    ```

    条目字段、`$ENV` 解析与合并语义见[模型提供方与路由](providers.md#models-file)；键表见
    [配置参考](configuration.md#models-json)。

2. 把指向写进 `.env`（只想单次生效时也可在命令前内联，见第 3 步）：

    ```bash
    PHONE_AGENT_MODELS_FILE="models.json"
    ```

3. 验证解析与角色路由（`--list-models` 打印生效注册表，默认 provider 带 `*` 前缀，模型逐行缩进列出；内联变量
   只对本次命令生效）：

    ```bash
    PHONE_AGENT_MODELS_FILE=models.json .venv/bin/python main_v2.py --list-models
    ```

4. 需要单角色替换模型时加 `roles` 段（角色名与回落链见[模型提供方与路由](providers.md#role-routing)）：

    ```json
    {"roles": {"memory": {"model": "secondgw:vision-model-a"}}}
    ```

    `roles.<role>.model` 只在该角色专属 env 未设置时生效。

5. 需要 actor 备用目标时，同样写进 `.env`：

    ```bash
    PHONE_AGENT_FALLBACK_MODEL="secondgw:vision-model-a"
    ```

    启用条件与降级审计见[模型提供方与路由](providers.md#fallback)。

## 诊断证据包

1. 打开证据流再跑任务：

    ```bash
    PHONE_AGENT_DIAG_EVIDENCE=on .venv/bin/python main_v2.py "把闹钟设到 7 点"
    ```

2. 产物落在证据目录（默认 `outputs/live-diagnosis/.evidence`，`PHONE_AGENT_DIAG_EVIDENCE_DIR` 可改）：

    ```text
    <evidence_dir>/<run_id>.evidence.jsonl   每个事件一行
    <evidence_dir>/screenshots/screen-<seq>.png
    ```

3. 查看：

    ```bash
    ls -R outputs/live-diagnosis/.evidence | head -30
    ```

    观察层职责见 [Web 控制台](console.md#web-projection)，报告产物与事件契约见仓内
    `.agents/skills/phone-agent-live-diagnosis/SKILL.md`。

4. 隐私：证据流里的截图是真实设备画面，`PHONE_AGENT_DIAG_UNREDACTED=on` 档还保留未脱敏文本与未截断正文；
   这些是本机私有数据，不提交、不发布。
