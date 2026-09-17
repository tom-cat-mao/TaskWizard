# Agent Note: CI 产物校验跳过搜索索引，消除 JSON 转义拼接造成的 API key 误报

Status: implemented

## Problem

PR #31（`docs/overhaul`）在 `pages/cookbook.md` 新增了一个以 `PHONE_AGENT_API_KEY="EMPTY"` 收尾的
代码块。CI docs job 的 "Validate built site artifact" 步骤（`mkdocs build --strict` 之后逐文件扫描
`site/`）因此变红：

```text
secret-like api key value in site/search/search_index.json
```

实测根因：`site/search/search_index.json` 把每个页面的文本压平进一条 JSON 字符串，`\"` 与 `\n`
这类转义序列在文件字面里是**反斜杠+字符**，空白分隔随之消失——栏尾代码块在索引里变成
`PHONE_AGENT_API_KEY=\"EMPTY\"\n</code></pre>`，`=` 之后恰好是 24 个连续非空白字符，命中校验模式
`PHONE_AGENT_API_KEY=\S{24,}`。同一段文本在 `site/cookbook/index.html` 里被语法高亮 `<span>` 与
HTML 实体切开，凑不出连续 24 个非空白字符，所以只有被派生的索引文件报错。即：**索引是页面文本的
JSON 转义再拼接，扫描它没有独立覆盖，只有转义引入的假阳性**（`docs` 数组逐条对应页面，不含页面之外
的内容）。

## Decision

产物校验遍历 `site/` 时按相对路径跳过 `site/search/search_index.json`
（`path.relative_to(root).as_posix() == "search/search_index.json"`），并在旁注释两句说明原因；
其余检查原样保留：`.env` 文件、`/Users/` 本地绝对路径、`PRIVATE KEY-----`、`PHONE_AGENT_API_KEY=\S{24,}`。

理由：索引里的每条字符串都来自 `site/*/index.html`，而 HTML 页本来就是全量扫描的对象；跳过这个纯
派生文件不损失任何秘密泄露检出面，却消除了转义拼接制造的假阳性。

## Alternatives considered

- **改文档绕开误报（把 `="EMPTY"` 拆行或换成别的写法）**
  - **最强的理由**：改动最小，只碰一个文档页，不必给校验脚本加特例；示例对读者仍然等价。
  - **为何被否**：脆弱。误报由「代码块以 `=值` 收尾」这种普通排版触发，下一个页面、下一位作者自然
    写出同样形状的示例时照样踩；把 CI 的稳定性押在文档排版自觉上，是把系统性缺陷当文案问题修。
- **脚本内先对 JSON 反转义再扫描**
  - **最强的理由**：语义上更「正确」——扫索引实际承载的文本而非转义后的字节；将来若真有只在索引
    出现的内容也覆盖得到。
  - **为何被否**：为一个纯派生文件引入 JSON 解析与反转义逻辑（外加格式异常、同名非 JSON 文件的处理），
    复杂度与收益不成比例；反转义后也不会多检出一个字节——索引文本已是 HTML 全量扫描的子集。
- **收紧正则，要求真实 key 形态（引号/长度/字符集约束）**
  - **最强的理由**：直击误报机制——`="EMPTY"` 明显不是秘密，模式学会区分「占位符」与「疑似真实
    key」，校验对文档示例更友好。
  - **为何被否**：这是秘密扫描模式语义的大改。区分占位符与真 key 就得枚举 `EMPTY`/`xxxx` 这类形态
    或引入熵阈值，误杀面与绕过面都难以在 CI 时限内评估；当前模式宁可宽（能报不能漏），不该为了让
    一个派生物闭嘴而放松它。跳过派生物把问题缩回它该在的层级。

## Consequences

- **收益**：docs job 恢复可信——报错回到「真在 HTML 产物里发现了疑似秘密」的语义；
  `pages/cookbook.md` 可以自然写 `PHONE_AGENT_API_KEY="EMPTY"` 这类收尾示例，不必为校验器的转义
  假象改文案。本地已双向验证：修改后的脚本对 `site/` 输出 `artifact checks passed`；HEAD 版脚本
  原样复现上述报错；向 `site/assets/` 植入合成假 key 后修改版仍能报出并退出 1。
- **代价**：`site/search/search_index.json` 不再被扫描。理论上若某秘密只出现在搜索索引、不出现在
  任何 HTML 页面则漏检——但索引由页面文本生成，不存在这种内容，这个缺口是空的。另一个代价是脚本
  多了一个按具体路径的特例：将来引入其他 JSON 派生文本产物时，要按同一理由单独判断，而不是默认
  「扫得越多越好」。
