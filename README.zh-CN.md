# Generate Token Receipt

[简体中文](README.zh-CN.md) | [English](README.md)

从本地 Codex 遥测、OpenAI API usage 对象或经确认的人工 token 计数中生成可审计的 token 用量小票。该 skill 会输出自包含 HTML 文档和 JSON 审计副本，并可选渲染为 PDF 和 PNG。

可选用黑白 80 mm 小票或克制的 A4 电子账单式版式。两种版式共用同一份标准化审计记录，因此更换纸张尺寸不会改变 token 数据、定价假设、小票编号或校验和。

> [!IMPORTANT]
> 基于 Codex 或 ChatGPT 订阅遥测生成的金额是 **API 等价 token 成本估算**。它不是 OpenAI 账单、实际订阅扣费、税务发票或付款凭证。

> [!NOTE]
> 这是一个独立的开源项目，不是 OpenAI 产品，也未获得 OpenAI 背书。OpenAI 和 Codex 是 OpenAI 的商标，使用时应遵循 [OpenAI 品牌指南](https://openai.com/brand/)。仓库使用自有的小票图形，不使用 OpenAI 官方标识；MIT License 不授予任何第三方商标权利。

## 样式参考

下面两张预览图来自同一份完全虚构的审计记录。其中的 token 数量、模型名、费率、时间、小票编号和校验和全部为演示数据；不包含任何真实 Codex 遥测或用户数据。

<table>
  <tr>
    <th>80 mm 小票</th>
    <th>A4 电子账单式文档</th>
  </tr>
  <tr>
    <td><a href="docs/images/sample-receipt-80mm.png"><img src="docs/images/sample-receipt-80mm.png" alt="完全虚构的 80 mm token 小票示例" width="380"></a></td>
    <td><a href="docs/images/sample-receipt-a4.png"><img src="docs/images/sample-receipt-a4.png" alt="完全虚构的 A4 token 账单示例" width="520"></a></td>
  </tr>
</table>

## 功能

- 统计当前 Codex 对话截至明确标注时间点的用量。
- 合并上一个或前 `N` 个已完成轮次，也可精确选取某一个历史轮次。
- 聚合某个项目文件夹及其子目录的本地 Codex 日志用量。
- 读取 Responses API 和 Chat Completions 的 usage JSON。
- 接受带精确模型 ID 的权威人工 token 计数。
- 分开展示新输入、缓存输入、缓存写入、可见输出和推理输出。
- 按每个模型调用应用精确定价快照；无精确匹配的本地 Codex 模型使用清晰标注的参考模型估算，并避免重复计费推理 token。
- 生成自包含 HTML 小票和可机读 JSON 审计记录。
- 从同一份数据渲染 80 mm 小票或 A4 电子文档。
- 重新渲染前使用 SHA-256 校验和验证审计记录。
- 默认隐去原始 session 和 turn 标识符。

## 环境要求

- 查询当前对话或已完成轮次时需要 Codex
- Python 3.9 或更高版本
- 仅在需要渲染 PDF 或 PNG 时需要 Google Chrome 或 Chromium

小票生成器只使用 Python 标准库，无需安装额外 Python 依赖。

## 安装

将仓库克隆到 Codex skills 目录：

```bash
git clone https://github.com/Beluga0105/generate-token-receipt.git \
  ~/.codex/skills/generate-token-receipt
```

安装后重启 Codex，使其发现该 skill。

可用以下命令验证命令行入口：

```bash
python3 ~/.codex/skills/generate-token-receipt/scripts/generate_receipt.py --help
```

## 在 Codex 中使用

可直接用自然语言描述，也可指定 skill 名称：

```text
使用 $generate-token-receipt 为当前 Codex 任务生成一张 80 mm token 小票，
包含子代理用量，并导出 PDF 和 PNG。
```

其他示例：

```text
为我上两个已完成轮次生成一份 A4 token 账单。

统计这个项目文件夹在本机记录的所有 Codex token 用量并生成小票。

把这份 OpenAI Responses API usage JSON 制作成可审计的 token 小票。
```

Codex 会自动选择合适的数据源，在主要工作完成后采集实时遥测，校验用户指定的范围，并返回所需文件。

## 命令行示例

运行以下命令前，先将 `SKILL_DIR` 设为 skill 安装目录：

```bash
SKILL_DIR="$HOME/.codex/skills/generate-token-receipt"
```

### 当前 Codex 对话

该模式会使用 Codex 提供的 `CODEX_THREAD_ID`，因此应在当前 Codex 任务内运行：

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-current \
  --include-subagents \
  --output "/absolute/path/token-receipt.html"
```

### 已完成轮次

合并上两个已完成轮次：

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-last-turns 2 \
  --include-subagents \
  --output "/absolute/path/previous-two-turns.html"
```

只选取往前数第 2 个已完成轮次：

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-turn 2 \
  --output "/absolute/path/turn-two-back.html"
```

已完成轮次范围不包含当前轮次。后代任务会从本地保留的全部 session 和 archived-session 日志中发现。包含子代理时，系统会根据选定根轮次时间窗内的 token 事件时间戳归属用量。

### 项目文件夹总量

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --codex-project "/absolute/path/to/project" \
  --output "/absolute/path/project-token-receipt.html"
```

项目总量只覆盖当前设备上仍然存在的匹配本地日志。远程、已删除、已过期、损坏或未记录的活动无法纳入。

### OpenAI API usage JSON

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --usage-json "/absolute/path/response.json" \
  --output "/absolute/path/api-token-receipt.html"
```

解析器支持 Responses API 和 Chat Completions 的 usage 结构，也支持常见的外层 response body 封装。API request ID 和输入文件名默认隐去；只有在本地取证确实需要时，才应显式加上 `--include-source-metadata`。

### 精确人工计数

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --model "exact-model-id" \
  --input-tokens 125000 \
  --cached-input-tokens 80000 \
  --output-tokens 4200 \
  --reasoning-tokens 1800 \
  --manual-exact \
  --input-rate 5 \
  --cached-input-rate 0.5 \
  --cache-write-input-rate 6.25 \
  --output-rate 30 \
  --pricing-as-of "YYYY-MM-DD" \
  --pricing-source "https://example.com/exact-model-rate-card" \
  --output "/absolute/path/manual-token-receipt.html"
```

只有当 token 数据直接复制自权威 usage 记录时，才应使用 `--manual-exact`。四项显式费率必须同时提供，并附带 `--pricing-as-of` 和 HTTP(S) `--pricing-source`。

## 纸张尺寸与重新渲染

默认纸张规格为 `80mm`。使用 `--paper a4` 生成 A4 版式：

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --usage-json "/absolute/path/response.json" \
  --paper a4 \
  --output "/absolute/path/token-receipt-a4.html"
```

如果要在不重新采集用量、不重新计价的前提下生成另一种版式，请从现有 JSON 副本渲染：

```bash
python3 "$SKILL_DIR/scripts/generate_receipt.py" \
  --receipt-json "/absolute/path/token-receipt.json" \
  --paper a4 \
  --output "/absolute/path/token-receipt-a4.html"
```

现有审计记录会在渲染前验证校验和。

## 导出 PDF 和 PNG

```bash
python3 "$SKILL_DIR/scripts/render_receipt.py" \
  "/absolute/path/token-receipt.html" \
  --pdf "/absolute/path/token-receipt.pdf" \
  --png "/absolute/path/token-receipt-preview.png"
```

渲染器会在 macOS、Linux 和 Windows 上查找 Chrome 或 Chromium。如果自动检测失败，可通过 `--chrome` 传入浏览器可执行文件的绝对路径。

生成小票不会向实体打印机发送任务。

## 定价规则

Skill 会先按每次模型调用计价，再汇总整张小票。

- 当前源码内置了于 2026-08-13 核验的 [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[`gpt-5.6-terra`](https://developers.openai.com/api/docs/models/gpt-5.6-terra) 和 [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol) 官方定价快照。
- 多模型小票在分类表格中显示有效混合费率，JSON 保留每个模型的精确费率卡。
- 本地 Codex 模型没有精确快照时，会明确标注使用 `gpt-5.6-terra` 作为参考估价，不再只返回不可用的 USD 小计。
- 未知 API 响应和手动模型仍需提供四项显式费率：新输入、缓存输入、缓存写入和输出。
- 费率日期和来源 URL 会与小票一同保存和展示。
- 推理 token 作为输出 token 的子集展示，不会被二次计费。
- 工具调用费、税费、抵扣额、订阅分摊和未知收费维度始终排除。
- 展示的费率、USD 金额和百分比统一保留两位小数，JSON 保留完整计算精度。

提供外部费率前，应始终根据官方的精确模型页面核对当前定价。

## 隐私与数据边界

- 不会将 prompt、回复内容、API 密钥或项目文件复制到小票中。
- 默认隐去原始 Codex session 和 turn UUID。
- 项目小票只保存文件夹名和简短路径指纹，不保存绝对项目路径。该指纹是稳定的，因此可以关联同一本地路径生成的多份小票。
- HTML 使用 data URI 内嵌仓库自有的小票图形，不加载远程字体、脚本或图像。
- `--include-session-ids` 是为本地取证关联提供的显式选项，可能暴露敏感标识符。
- API request ID 和输入 usage 文件名默认隐去；`--include-source-metadata` 是显式选项。
- HTML 和 JSON 都包含完整的标准化审计记录，其中含有详细的单次调用元数据。公开分享前应同时审查两者。

所有文件均在本地生成，该 skill 不会上传小票或用量数据。

## 输出文件

默认情况下，生成器会创建：

```text
token-receipt.html   自包含、人类可读的小票
token-receipt.json   标准化审计记录与 SHA-256 校验和
```

可选渲染还会生成：

```text
token-receipt.pdf
token-receipt-preview.png
```

保存或分享可审计小票时，建议将 HTML 和 JSON 副本放在一起。

## 仓库结构

```text
generate-token-receipt/
├── .github/
│   └── workflows/
│       └── test.yml
├── .gitignore
├── .gitattributes
├── LICENSE
├── README.md
├── README.zh-CN.md
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── receipt-mark.svg
├── docs/
│   └── images/
│       ├── sample-receipt-80mm.png
│       └── sample-receipt-a4.png
├── references/
│   └── receipt-contract.md
├── scripts/
│   ├── generate_receipt.py
│   └── render_receipt.py
└── tests/
    └── test_generate_receipt.py
```

`references/receipt-contract.md` 定义了标准化记录、数据映射、定价规则以及未来打印机集成的边界。

## 局限

- Codex 总量来自本地遥测，不是组织级账单记录。
- 当前对话的用量截止点可能不包含生成小票的调用和最后的交付消息。
- 未知本地 Codex 模型使用已标注的参考估价；未知 API 或手动模型需要显式费率。
- 导出 PDF 和 PNG 需要本地安装 Chrome 或 Chromium。
- 本项目不生成官方发票，也不读取实际订阅扣费。

## 开发与验证

使用以下命令运行无第三方依赖的发布测试：

```bash
python3 -m unittest discover -s tests -v
```

仓库内置的 GitHub Actions 工作流会在 push 和 pull request 时运行同样的检查。

## 许可证

本项目以 [MIT License](LICENSE) 发布。
