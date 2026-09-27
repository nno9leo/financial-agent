# Financial Research Agent

**English** | [简体中文](README.zh-CN.md)

A tool-calling research agent that answers questions about two pinned SEC 10-K filings (Microsoft FY2026 and Apple FY2025). **Numbers** come from a deterministic calculator over SEC's structured `companyfacts` data; **explanations** come from retrieved passages of the filing text. **Every claim** is re-checked against stored evidence before it can be presented as a finding. A versioned evaluation harness measures accuracy, citation quality, latency and cost.

## Problem and architecture

The goal is to answer financial questions with traceable evidence and repeatable offline evaluation. RAG alone isn't used for numbers: text retrieval doesn't reliably select the right fiscal period, unit, filing or formula. Structured companyfacts feed a period-aware fact selector and a deterministic metric engine; filing text serves questions about causes, risks and trends.

```text
User question
    -> Pi tool-calling agent (isolated project-local extension)
    -> typed tool schemas -> FastAPI / Pydantic services
           |                                  |
           | numeric                          | narrative
           v                                  v
    SEC companyfacts                 pinned filing chunks
           |                                  |
    normalize/select                          retrieval
           |                                  |
    deterministic metric                 Evidence Ledger
           +------------------+---------------+
                              v
                 claim-level citation verifier
                  deterministic -> semantic
                              |
                     answer + JSONL trace
                              v
                versioned offline evaluation
```

**The eight tools:**
- `search_companies` resolves a ticker, name or CIK.
- `list_filings` returns filing metadata.
- `get_filing_section` reads a known section.
- `search_filing_chunks` retrieves source passages.
- `calculate_financial_metric` computes one metric.
- `compare_periods` compares two periods.
- `verify_citations` checks claims against evidence.
- `export_research_report` previews and exports a report after user confirmation.

`list_filings`, `search_filing_chunks`, `calculate_financial_metric` and `verify_citations` are the four core tools that get the deepest evaluation. They cover filing selection, retrieval, deterministic calculation and evidence correctness.

## Why not just put the 10-K into an LLM?

A 10-K fits in a modern long-context window. Our canonical text of the MSFT 10-K is 325,486 characters (≈ 67k BERT-WordPiece tokens). So the difference is correctness and verifiability, not size:

| | 10-K pasted into an LLM | This project |
|---|---|---|
| Numbers | the model reads tables and does arithmetic | deterministic calculator over structured facts (`Decimal`; formula, inputs, period, unit and accession returned) |
| Fiscal periods | inferred from table layout | from the facts' start/end **dates**, never SEC `fy`/`fp` tags (AAPL's FY2023 comparative is tagged `fy=2025`); ambiguity is a typed error |
| Versioning | whatever was pasted, plus training memory | pinned accession and content hashes; a changed source fails loudly |
| Provenance | "according to the 10-K" | every claim cites evidence IDs that are reloaded and re-checked (hash, ticker, accession, unit, period, value) |
| Unsupported answers | usually still answered fluently | only supported claims enter the verified answer; one bounded evidence retry |
| Missing data | risk of a plausible invented number | typed errors lead to a refusal (baseline refusals 5/5) |
| Measurement | hard to measure systematically | 35-question versioned eval (results below) |

**Trade-offs.**
- The simple approach is much easier to build and can synthesise across the whole document. Our retrieval hit@k was 9/11.
- It isn't limited to our 6 metrics and 2 companies.
- It can be faster for one question; our p50 is 21.2 s.
- A head-to-head long-context baseline on the same questions has **not** been measured; it's the natural next experiment.

## Pinned data and evidence

[`data/manifests/filings.v1.json`](data/manifests/filings.v1.json) pins the original 10-Ks, including document and companyfacts hashes:
- Microsoft: accession `0001193125-26-323660`, filed 2026-07-29;
- Apple: accession `0000320193-25-000079`, filed 2025-10-31.

An accession fixes the filing version, so facts, text and citations can't silently mix in later amendments or filings. The selection rule picks each company's most recent original 10-K and excludes amendments.

Every citable tool output gets a stable evidence ID in the Evidence Ledger:
- retrieved chunks (`ev_…`);
- companyfacts facts (`fact_…`);
- calculated metrics (`met_…`).

The verifier reloads each cited ID from its authoritative source and runs deterministic checks first: existence, hash, provenance, ticker and single accession, then unit, period and value against structured evidence for numeric claims. A model judge from a different vendor is used only for narrative entailment, and only after the deterministic checks pass.

`verify_citations` builds the verified answer (`final_answer_markdown`) from supported claims only. Unsupported claims are counted and listed separately, never stated as findings. The agent's own prose may still mention such a number, but only labelled as unverified.

## Evaluation data and leakage controls

Dataset `v1` is frozen with SHA-256 `121d669d3e86a0f23555cc7a9079c49dac74bf99147a334213bae0bcfdb8b3a4`.

**Numeric ground truth** was read by hand from the 10-K financial statement text ([`evals/ground_truth/statement_ground_truth.json`](evals/ground_truth/statement_ground_truth.json)). It deliberately doesn't use the companyfacts pipeline or the calculator being evaluated, so the calculator can't grade itself. **Narrative items'** gold evidence IDs were checked against the chunk text.

**Splits** are assigned by a hash of the question ID and frozen before any run. The holdout split was never used to choose a change or a threshold. The treatment arm ran on it once, after the thresholds were committed (ADR 0011). **Disclosure:** the rendering defect behind the A/B experiment was diagnosed by reading the baseline's raw records for all items, **including the holdout baseline rows**. The fix is mechanistic (the tool-result text lacked the citable `met_` ID), but the holdout is not a completely untouched split.

## Built vs. reused

| Component | Choice |
|---|---|
| SEC source | Official submissions and companyfacts APIs |
| API and schemas | FastAPI and Pydantic |
| Storage | PostgreSQL with pgvector |
| Embeddings | fastembed, `BAAI/bge-small-en-v1.5` (local ONNX, 384-d) |
| Agent runtime | Pi 0.87.1 with this repository's extension (isolated launch args) |
| Agent model | `anthropic/claude-sonnet-5`, thinking medium (via Pi) |
| Semantic judge | `openai/gpt-6-sol` via OpenRouter, reasoning high, provider pinned to OpenAI |
| Pi extension (TypeScript tool wrappers) | Implemented in this repository |
| SEC normalization and fact selection | Implemented in this repository |
| Metric engine, Evidence Ledger, citation verifier | Implemented in this repository |
| Eval generation, grading and runner | Implemented in this repository |

## Results

The tables below are the direct output of `scripts/report_results.py`; regenerate them with `.venv/bin/python scripts/report_results.py`. Only the three measurement runs are included. Every rate shows its denominator and split, and every summary must be graded by grader v3.

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

**Reading the provenance table:**
- **Git SHAs** refer to the original development repository. This public repository was published as a single snapshot, so those commits aren't present here; the pre-registration timing in ADR 0010/0011 is documented there.
- **Render "not recorded"** for the baseline means v1. The run predates the render switch, and v1 output is byte-identical to the code it ran.
- **`not_reported_by_runtime`** comes from the items that timed out (1 per run) before Pi reported its state. These runs predate the idle-stall watchdog, so each includes one 300 s timeout.

**The A/B experiment.**
- The single variable was the **rendering of `calculate_financial_metric` and `compare_periods` results**: v2 shows the citable `met_` evidence ID, the exact period and the accepted unit forms. This is the change behind the improvement above.
- The verifier made unsupported claims visible and kept them out of verified answers; it didn't reduce their count.
- The decision rule was pre-registered (ADR 0010). The first dev result was **rejected** by its side-effect guards: refusals began to include extra verified figures.
- A refusal policy was then decided (ADR 0011: verified figures for other metrics or periods are allowed) and encoded as grader v3. All runs were regraded offline without changing raw records; each run's `regrade.json` lists every change.
- A single pre-registered holdout run then confirmed the change.
- **Caveats:** one run per arm; small holdout (9 strict numeric items, 1 refusal item); arm A ran at an earlier commit. This is limited evidence, not a broad performance guarantee.

## Setup and run

Use Python 3.11; `constraints.txt` was generated and verified on 3.11.16.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -c constraints.txt -e '.[test]'
```

**Prerequisites for live runs:**
- Node 24.
- Pi: `npm install -g @earendil-works/pi-coding-agent@0.87.1`.
- The extension's dependency: `npm install` in `.pi/extensions/financial-research/`.
- Pi logged in to Anthropic via Pi's `/login`. Pi's third-party usage is billed to Claude **extra usage**. With an empty balance, every request fails with `400 … extra usage`, and the eval runner aborts (`ABORTED.md`) instead of scoring.

**Keys.** Set `SEC_USER_AGENT` for ingestion and `OPENROUTER_API_KEY` for the semantic judge. The backend reads them from the environment or a local `.env` (see `.env.example`). The eval runner starts Pi with an allow-listed environment that excludes them. `scripts/pi_smoke.mjs` and manual `pi` launches inherit your shell environment, so don't export the keys into a shell where you run Pi.

**Fresh clones.** The raw SEC cache isn't committed, and SEC's companyfacts file changes whenever the company files anything. A fresh `scripts/ingest.py` therefore fails the pinned companyfacts hash by design (ADR 0002). Use `--accept-new-companyfacts-snapshot` to record a new snapshot in a new manifest version. Pinned answers can't change, because fact selection only admits facts from the pinned accession.

Start PostgreSQL/pgvector with `docker compose up -d`, then:

```bash
.venv/bin/python scripts/ingest.py
.venv/bin/python scripts/build_chunks.py
.venv/bin/python scripts/ingest_chunks.py
.venv/bin/python scripts/serve.py
```

Pi launches read `config/agent_runtime.json` and enforce its isolation arguments: `--tools <the 8 tools> --no-extensions --extension .pi/extensions/financial-research/index.ts -na --no-skills --no-prompt-templates`.
- `node scripts/pi_smoke.mjs` launches Pi and makes paid model calls.
- `run_eval.py --agent pi` launches the real agent and incurs model and judge costs.
- The offline fake eval doesn't launch Pi:

```bash
.venv/bin/python scripts/run_eval.py --dataset v1 --split all --agent fake --run-id local-fake --limit 3
```

**Tests.** The default pytest run is offline: it excludes tests marked `model`, `live`, `db` and `live_llm`, and doesn't need the ignored local SEC caches. SEC-shaped test data comes from the small fixtures documented in [`tests/fixtures/README.md`](tests/fixtures/README.md). The default suite is also guarded so it can never launch a real agent.

```bash
.venv/bin/python -m pytest -q
node --test --test-reporter=spec .pi/extensions/financial-research/tests/*.test.ts scripts/*.test.mjs
```

## Limitations

- **Scope:** two pinned filings; a small evaluation set (35 questions, 0 external); one run per arm in the A/B experiment; LLM output can vary.
- **Evaluation:**
  - numeric ground truth was transcribed by a single reviewer;
  - the judge was spot-checked on only 2 narrative claims against the source text;
  - `narrative_key_points_proxy` is a substring proxy, not a quality score.
- **Observed failure modes:**
  - the agent model occasionally asserted that Microsoft's fiscal 2026 "has not ended". It refused rather than inventing numbers;
  - upstream model streams occasionally stalled; an idle-stall watchdog now aborts after 120 s and retries once.
- **Coverage:** only the defined standard XBRL tag aliases and supported periods are reliable. Custom tags, segment dimensions, complex tables and more companies need further work.

## Design references

This project is informed by:
- OpenBB's provider contracts and normalized financial-data models;
- FinRobot's separation of agent orchestration, domain tools, deterministic financial computation and provenance;
- GPT Researcher's source-tracked research and citation-evaluation methodology;
- FinanceBench's evidence-grounded financial QA format.

The SEC normalization pipeline, tool contracts, deterministic metric engine, Evidence Ledger, citation verifier, evaluation-data generator and evaluation runner are implemented in this repository.

## Licenses and data

**Data.** SEC EDGAR submissions, companyfacts and filing documents are public U.S. government records, fetched under SEC's fair-access rules (declared `User-Agent`, throttling, local cache). The repository commits only:
- the pinned manifest;
- small trimmed excerpts in `tests/fixtures/` (provenance in `tests/fixtures/README.md`).

It doesn't commit the raw cache, full filings, or any third-party PDFs or datasets.

**Dependencies** (licenses read from installed package metadata):

| License | Packages |
|---|---|
| MIT | FastAPI, Pydantic, pgvector (Python), onnxruntime, Pi (`@earendil-works/pi-coding-agent`), typebox |
| BSD-3-Clause | httpx, uvicorn, Starlette |
| Apache-2.0 | fastembed, tokenizers |
| LGPL-3.0 | psycopg, used unmodified as a library dependency, not redistributed |

**Embedding model.** `BAAI/bge-small-en-v1.5` is MIT according to its model card; the local ONNX cache has no license file. **Models:** the agent and judge models are used via their providers' APIs under the respective terms.

## Architecture decisions

Design decisions and their rationale are recorded in [`docs/adr/`](docs/adr/).
