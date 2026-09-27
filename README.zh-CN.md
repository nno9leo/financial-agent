# Financial Research Agent（金融研究 Agent）

[English](README.md) | **简体中文**

一个基于 tool calling 的研究 agent，回答关于两份固定 SEC 10-K 年报（Microsoft FY2026、Apple FY2025）的问题。**数字**来自一个确定性计算器，计算器的输入是 SEC 的结构化 `companyfacts` 数据；**解释**来自对年报正文的检索。**每一条 claim** 都要先对照存储的证据重新核验，才能作为结论呈现。另有一个带版本的评测框架，用来测量准确率、引用质量、延迟和成本。

## 问题与架构

目标是用可追溯的证据回答财务问题，并且可以重复做离线评测。数字不单靠 RAG：文本检索没法可靠地选对财年期间、单位、申报版本或计算公式。结构化的 companyfacts 进入一个按期间选取 fact 的选择器和一个确定性指标引擎；年报正文负责回答原因、风险、趋势类问题。

```text
用户问题
    -> Pi tool-calling agent（隔离的项目本地扩展）
    -> typed tool schemas -> FastAPI / Pydantic 服务
           |                                  |
           | 数值类                            | 叙述类
           v                                  v
    SEC companyfacts                 固定年报的文本块
           |                                  |
    规范化 / 选取                              检索
           |                                  |
    确定性指标计算                        Evidence Ledger
           +------------------+---------------+
                              v
                   逐条 claim 的引用核验器
                     确定性检查 -> 语义判定
                              |
                     答案 + JSONL trace
                              v
                    带版本的离线评测
```

**8 个工具：**
- `search_companies`：解析 ticker、公司名或 CIK。
- `list_filings`：返回申报元数据。
- `get_filing_section`：读取指定章节。
- `search_filing_chunks`：检索原文段落。
- `calculate_financial_metric`：计算一个指标。
- `compare_periods`：比较两个期间。
- `verify_citations`：对照证据核验 claim。
- `export_research_report`：预览报告，经用户确认后导出。

其中 `list_filings`、`search_filing_chunks`、`calculate_financial_metric`、`verify_citations` 是评测最深入的四个核心工具，覆盖申报选择、检索、确定性计算和证据正确性。

## 为什么不直接把 10-K 丢给大模型？

一份 10-K 放得进现代长上下文窗口：MSFT 10-K 的 canonical 文本有 325,486 个字符（约 6.7 万个 BERT WordPiece token）。所以区别不在大小，而在正确性和可核验性：

| | 把 10-K 粘贴给大模型 | 本项目 |
|---|---|---|
| 数字 | 模型读表格、自己心算 | 基于结构化 facts 的确定性计算器（`Decimal`；返回公式、输入、期间、单位和 accession） |
| 财年期间 | 从表格排版里猜 | 由 fact 的起止**日期**决定，从不用 SEC 的 `fy`/`fp` 标签（AAPL FY2023 的对比数字被标成 `fy=2025`）；有歧义就是 typed error |
| 版本 | 粘贴的文本，加上模型训练时的记忆 | 固定 accession 和内容哈希；来源一变就明确失败 |
| 溯源 | "根据 10-K……" | 每条 claim 都引用证据 ID，并重新加载、重新核验（哈希、ticker、accession、单位、期间、数值） |
| 没有证据支持时 | 通常照样流畅作答 | 只有被支持的 claim 才进入核验后的答案；最多补一次证据 |
| 数据缺失时 | 可能编出一个看似合理的数字 | typed error 触发拒答（baseline 拒答 5/5） |
| 可测量性 | 很难系统地测量 | 35 题的版本化评测（结果见下文） |

**取舍。**
- 直接丢给大模型的方案简单得多，还能在整份文档里综合信息；我们的检索 hit@k 是 9/11。
- 它不局限于我们的 6 个指标和 2 家公司。
- 单个问题可能更快；我们的 p50 是 21.2 秒。
- 我们**没有**在同样的题目上实测过长上下文方案的对照组，这是最自然的下一个实验。

## 固定数据与证据

[`data/manifests/filings.v1.json`](data/manifests/filings.v1.json) 固定了两份原始 10-K，并记录了文档哈希和 companyfacts 哈希：
- Microsoft：accession `0001193125-26-323660`，2026-07-29 申报；
- Apple：accession `0000320193-25-000079`，2025-10-31 申报。

accession 固定了申报版本，所以 facts、正文和引用不会悄悄混进后来的修订版或其他申报。选择规则是：每家公司最近一份原始 10-K，不含修订版。

每个可被引用的工具输出，都会在 Evidence Ledger 里得到一个稳定的证据 ID：
- 检索到的文本块（`ev_…`）；
- companyfacts 的 fact（`fact_…`）；
- 计算出的指标（`met_…`）。

核验器会从权威来源重新加载每个被引用的 ID，先做确定性检查：是否存在、哈希、来源、ticker、是否同一个 accession；数值类 claim 还要对照结构化证据检查单位、期间和数值。只有叙述类 claim、并且确定性检查全部通过之后，才交给另一家厂商的模型判官判断是否蕴含。

`verify_citations` 只用被支持的 claim 生成核验后的答案（`final_answer_markdown`）。不被支持的 claim 会被计数并单独列出，绝不当作结论陈述。agent 自己的文字里可能仍会提到这类数字，但只会标注为"未核验"。

## 评测数据与防泄漏

数据集 `v1` 已冻结，SHA-256 为 `121d669d3e86a0f23555cc7a9079c49dac74bf99147a334213bae0bcfdb8b3a4`。

**数值标准答案**是人工从 10-K 财务报表正文里读出来的（[`evals/ground_truth/statement_ground_truth.json`](evals/ground_truth/statement_ground_truth.json)），刻意不经过 companyfacts 流水线和被评测的计算器，这样计算器没法给自己打分。**叙述类题目**的 gold 证据 ID 都对照过文本块原文。

**数据划分**按题目 ID 的哈希分配，在任何运行之前就冻结。holdout 从没被用来挑选改动或设定门槛；实验组只在门槛 commit 之后在它上面跑过一次（ADR 0011）。**需要说明：** A/B 实验要修的渲染缺陷，是通过阅读 baseline 全部题目的原始记录诊断出来的，**其中包括 holdout 的 baseline 记录**。这个修复是机制性的（工具结果的文字里缺少可引用的 `met_` ID），但 holdout 严格来说不是完全没被看过的划分。

## 自研与复用

| 组件 | 选择 |
|---|---|
| SEC 数据来源 | 官方 submissions 和 companyfacts API |
| API 与 schema | FastAPI 和 Pydantic |
| 存储 | PostgreSQL + pgvector |
| Embedding | fastembed，`BAAI/bge-small-en-v1.5`（本地 ONNX，384 维） |
| Agent 运行时 | Pi 0.87.1，加上本仓库的扩展（隔离启动参数） |
| Agent 模型 | `anthropic/claude-sonnet-5`，thinking medium（通过 Pi） |
| 语义判官 | `openai/gpt-6-sol`，通过 OpenRouter，reasoning high，provider 固定为 OpenAI |
| Pi 扩展（TypeScript 工具封装） | 本仓库实现 |
| SEC 数据规范化与 fact 选择 | 本仓库实现 |
| 指标引擎、Evidence Ledger、引用核验器 | 本仓库实现 |
| 评测数据生成、评分与运行器 | 本仓库实现 |

## 结果

下面的表格直接来自 `scripts/report_results.py` 的输出（表格保持英文原样），可以用 `.venv/bin/python scripts/report_results.py` 重新生成。只包含三次正式测量运行；每个比率都附分母和划分，每份汇总都必须由 grader v3 评分。

<!-- BEGIN GENERATED REPORT: scripts/report_results.py -->

# Eval results report

Only the three approved, non-underscore measurement run IDs are included. Every rate includes its denominator; grader v3 is required.

## Baseline overall and by split

Source: `cp6b-baseline-v1`; metrics are calculated from its `results.jsonl` fields.

| Split | Items | Numeric correct | Strict P1 numeric | Refusal correct | Tool selection | Retrieval hit@k | Citation coverage | Unsupported claims | Narrative unsupported | Latency p50 / p95 | Total cost | Run errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| overall | 35 | 6/21 (28.6%; n=21) | 4/19 (21.1%; n=19) | 5/5 (100.0%; n=5) | 35/35 (100.0%; n=35) | 9/11 (81.8%; n=11) | 54/54 (100.0%; n=54) | 19/54 (35.2%; n=54) | 0/24 (0.0%; n=24) | 21.2s / 55.6s (n=35) | $1.1851 | 1/35 (n=35) |
| dev | 23 | 2/12 (16.7%; n=12) | 0/10 (0.0%; n=10) | 4/4 (100.0%; n=4) | 23/23 (100.0%; n=23) | 6/7 (85.7%; n=7) | 33/33 (100.0%; n=33) | 14/33 (42.4%; n=33) | 0/16 (0.0%; n=16) | 21.7s / 61.4s (n=23) | $0.8378 | 1/23 (n=23) |
| holdout | 12 | 4/9 (44.4%; n=9) | 4/9 (44.4%; n=9) | 1/1 (100.0%; n=1) | 12/12 (100.0%; n=12) | 3/4 (75.0%; n=4) | 21/21 (100.0%; n=21) | 5/21 (23.8%; n=21) | 0/8 (0.0%; n=8) | 18.5s / 47.5s (n=12) | $0.3474 | 0/12 (n=12) |

## Render A/B experiment

A uses the matching split rows from `cp6b-baseline-v1`; B uses the named treatment run. P1 is strict numeric accuracy over numeric_lookup + mixed items. P2 is pooled unsupported claims.

| Split | Arm | Run id | P1 strict numeric | P2 unsupported claims | Tool selection | Refusal correct | Retrieval hit@k | Narrative unsupported | Run errors | Total cost |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| dev | A | `cp6b-baseline-v1` | 0/10 (0.0%; n=10) | 14/33 (42.4%; n=33) | 23/23 (100.0%; n=23) | 4/4 (100.0%; n=4) | 6/7 (85.7%; n=7) | 0/16 (0.0%; n=16) | 1/23 (n=23) | $0.8378 |
| dev | B | `cp7-render-v2-dev` | 8/10 (80.0%; n=10) | 3/43 (7.0%; n=43) | 22/23 (95.7%; n=23) | 4/4 (100.0%; n=4) | 6/7 (85.7%; n=7) | 0/24 (0.0%; n=24) | 1/23 (n=23) | $0.8394 |
| holdout | A | `cp6b-baseline-v1` | 4/9 (44.4%; n=9) | 5/21 (23.8%; n=21) | 12/12 (100.0%; n=12) | 1/1 (100.0%; n=1) | 3/4 (75.0%; n=4) | 0/8 (0.0%; n=8) | 0/12 (n=12) | $0.3474 |
| holdout | B | `cp7-render-v2-holdout` | 7/9 (77.8%; n=9) | 0/23 (0.0%; n=23) | 11/12 (91.7%; n=12) | 1/1 (100.0%; n=1) | 4/4 (100.0%; n=4) | 0/7 (0.0%; n=7) | 1/12 (n=12) | $0.3481 |

## Unsupported-claim reasons in baseline

Counts are derived from the baseline's final verification reports; `n` is unsupported claims.

| Reason | Unsupported claims |
|---|---:|
| `numeric_claim_without_structured_evidence` | 5 (n=19) |
| `unit_mismatch` | 12 (n=19) |
| `value_mismatch` | 2 (n=19) |

## Run provenance

| Run id | Git SHA | Dirty | Grader | Agent observed | Judge observed | Render | Regrade history |
|---|---|---:|---|---|---|---|---|
| `cp6b-baseline-v1` | `9ca9f7bb9d8768ffc96015b87027f860a9ef7444` | False | v3 | anthropic/claude-sonnet-5, not_reported_by_runtime | openai/gpt-6-sol | not recorded | v1→v2→v3 |
| `cp7-render-v2-dev` | `7fb2cd2a455668cad0897798a65359c74cb7274a` | False | v3 | anthropic/claude-sonnet-5, not_reported_by_runtime | openai/gpt-6-sol | v2 | v2→v3 |
| `cp7-render-v2-holdout` | `40fc1c8e573375876b3793f10a59cbfacbce2b26` | False | v3 | anthropic/claude-sonnet-5, not_reported_by_runtime | openai/gpt-6-sol | v2 | none (summary is v3) |

## Dataset composition

| Dataset field | Value |
|---|---:|
| Total items | 35 |
| Generated numeric items | 18 |
| Hand-authored/reviewed items | 17 |
| External items | 0 |
| Frozen dataset SHA-256 | `121d669d3e86a0f23555cc7a9079c49dac74bf99147a334213bae0bcfdb8b3a4` |
| Holdout split policy | holdout iff int(sha256(question_id)[:8],16) % 3 == 0; frozen before runs |
<!-- END GENERATED REPORT -->

**怎么读 provenance 表：**
- **Git SHA** 指的是原始开发仓库里的 commit。本公开仓库以单一快照形式发布，所以这里没有那些 commit；ADR 0010/0011 记录了预注册的时间顺序。
- baseline 的 **Render "not recorded"** 表示 v1：这次运行早于渲染开关的加入，而 v1 的输出与当时运行的代码逐字节相同。
- **`not_reported_by_runtime`** 来自超时的题目（每次运行 1 道），超时发生在 Pi 报告状态之前。这些运行早于"卡住看门狗"，所以每次都包含一次 300 秒超时。

**A/B 实验。**
- 唯一的变量是 **`calculate_financial_metric` 和 `compare_periods` 结果的呈现方式**：v2 会展示可引用的 `met_` 证据 ID、精确的期间和可接受的单位写法。上表中的提升来自这项改动。
- 核验器让不被支持的 claim 变得可见，并把它们挡在核验后的答案之外；它本身并没有减少这类 claim 的数量。
- 判定规则事先预注册（ADR 0010）。第一次 dev 结果被副作用检查**否决**：拒答时开始夹带额外的已核验数字。
- 随后确定了拒答规则（ADR 0011：允许给出其他指标或其他期间的已核验数字），并实现为 grader v3。所有运行都在不改动原始记录的前提下离线重新评分，每次运行的 `regrade.json` 列出了全部改动。
- 最后，一次预注册的 holdout 运行确认了这项改动。
- **注意事项：** 每组只跑一次；holdout 很小（9 道严格数值题、1 道拒答题）；A 组在更早的 commit 上运行。这是有限的证据，不是普遍的性能保证。

## 安装与运行

使用 Python 3.11；`constraints.txt` 在 3.11.16 上生成并验证过。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[test]'
```

**真实运行的前置条件：**
- Node 24。
- Pi：`npm install -g @earendil-works/pi-coding-agent@0.87.1`。
- 扩展的依赖：在 `.pi/extensions/financial-research/` 下执行 `npm install`。
- 在 Pi 里用 `/login` 登录 Anthropic。Pi 这类第三方应用的用量从 Claude 的 **extra usage** 扣费；余额为零时，每个请求都会返回 `400 … extra usage`，评测 runner 会直接中止（写 `ABORTED.md`），而不是去打分。

**密钥。** 数据抓取需要 `SEC_USER_AGENT`，语义判官需要 `OPENROUTER_API_KEY`。后端从环境变量或本地 `.env` 读取（参见 `.env.example`）。评测 runner 启动 Pi 时使用白名单环境变量，不包含这两个密钥。`scripts/pi_smoke.mjs` 和手动启动的 `pi` 会继承当前 shell 的环境变量，所以不要把密钥 export 到你运行 Pi 的 shell 里。

**全新 clone。** 原始 SEC 缓存没有提交；公司每申报一次，SEC 的 companyfacts 文件就会变。所以在新 clone 里运行 `scripts/ingest.py` 时，companyfacts 的哈希按设计会校验失败（ADR 0002）。可以用 `--accept-new-companyfacts-snapshot` 把新快照记录为一个新的 manifest 版本。已固定的答案不会变，因为 fact 选择只采用来自固定 accession 的 fact。

先用 `docker compose up -d` 启动 PostgreSQL/pgvector，然后：

```bash
.venv/bin/python scripts/ingest.py
.venv/bin/python scripts/build_chunks.py
.venv/bin/python scripts/ingest_chunks.py
.venv/bin/python scripts/serve.py
```

Pi 启动时会读取 `config/agent_runtime.json`，并强制使用其中的隔离参数：`--tools <8 个工具> --no-extensions --extension .pi/extensions/financial-research/index.ts -na --no-skills --no-prompt-templates`。
- `node scripts/pi_smoke.mjs` 会启动 Pi，并产生付费的模型调用。
- `run_eval.py --agent pi` 会启动真实的 agent，产生模型和判官的费用。
- 离线的 fake 评测不会启动 Pi：

```bash
.venv/bin/python scripts/run_eval.py --dataset v1 --split all --agent fake --run-id local-fake --limit 3
```

**测试。** 默认的 pytest 是离线的：会排除标记为 `model`、`live`、`db`、`live_llm` 的测试，也不需要被忽略的本地 SEC 缓存。测试用的 SEC 数据来自少量已提交的片段，说明见 [`tests/fixtures/README.md`](tests/fixtures/README.md)。默认测试套件还带有防护，永远不会启动真实的 agent。

```bash
.venv/bin/python -m pytest -q
node --test --test-reporter=spec .pi/extensions/financial-research/tests/*.test.ts scripts/*.test.mjs
```

## 局限

- **范围：** 两份固定年报；评测集很小（35 题，0 道外部题）；A/B 实验每组只跑一次；LLM 的输出会有波动。
- **评测：**
  - 数值标准答案由一个人整理；
  - 判官只在 2 条叙述类 claim 上对照原文做过抽查；
  - `narrative_key_points_proxy` 只是子串匹配的近似指标，不是质量评分。
- **观察到的失败模式：**
  - agent 模型偶尔会声称 Microsoft 的 FY2026"还没结束"，这时它会拒答，而不是编数字；
  - 上游模型的流式输出偶尔会卡住；现在的"卡住看门狗"会在 120 秒无事件后中止，并重试一次。
- **覆盖范围：** 只有已定义的标准 XBRL 标签别名和支持的期间是可靠的；自定义标签、segment 维度、复杂表格和更多公司还需要进一步工作。

## 设计参考

本项目参考了：
- OpenBB 的 provider 契约和规范化金融数据模型；
- FinRobot 对 agent 编排、领域工具、确定性金融计算与溯源的分层；
- GPT Researcher 的来源追踪研究方法和引用评测方法；
- FinanceBench 以证据为基础的金融问答格式。

SEC 数据规范化流水线、工具契约、确定性指标引擎、Evidence Ledger、引用核验器、评测数据生成器和评测运行器都在本仓库中实现。

## 许可证与数据

**数据。** SEC EDGAR 的 submissions、companyfacts 和申报文件是美国政府公开记录，按 SEC 的公平访问规则抓取（声明 `User-Agent`、节流、本地缓存）。仓库只提交：
- 固定的 manifest；
- `tests/fixtures/` 下的少量裁剪片段（来源见 `tests/fixtures/README.md`）。

不提交原始缓存、完整年报，也不包含任何第三方 PDF 或数据集。

**依赖**（许可证信息读取自已安装包的元数据）：

| 许可证 | 包 |
|---|---|
| MIT | FastAPI、Pydantic、pgvector（Python）、onnxruntime、Pi（`@earendil-works/pi-coding-agent`）、typebox |
| BSD-3-Clause | httpx、uvicorn、Starlette |
| Apache-2.0 | fastembed、tokenizers |
| LGPL-3.0 | psycopg，作为未修改的库依赖使用，不再分发 |

**Embedding 模型。** 根据模型卡，`BAAI/bge-small-en-v1.5` 采用 MIT 许可证；本地 ONNX 缓存里没有许可证文件。**模型：** agent 和判官模型通过各自厂商的 API 使用，遵守相应条款。

## 架构决策

设计决策及其理由记录在 [`docs/adr/`](docs/adr/)。
