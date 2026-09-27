# ADR 0001 — Numbers from structured facts, explanations from filing text

- **Status:** Accepted
- **Date:** 2026-09-25
- **Handoff refs:** §3.1, §3.2, §7 坑5, Q1, Q3

## Context
Financial questions mix two kinds of answers: exact numbers/ratios and management's narrative (causes, risks, trends). Extracting numbers from 10-K tables flattened to text loses column headers, units and periods. An LLM doing arithmetic over retrieved text is neither auditable nor reproducible.

## Decision
- Numeric lookup and formula inputs come only from normalized SEC `companyfacts` (`FinancialFact`), selected by deterministic code (`app/compute/fact_selection.py`) and computed by `app/compute/metrics.py`.
- Filing text (MD&A etc.) is chunked and retrieved only for narrative/explanatory claims.
- Both paths are aligned on the same pinned accession (see ADR 0002).
- If structured facts cannot be selected reliably → typed `insufficient_structured_data` error; the agent may fall back to retrieval, but the calculator never guesses.

## Consequences
- (+) Numbers are unit-tested, reproducible and carry formula/inputs/period/unit/source.
- (+) Fact-selection errors, formula errors and tool-choice errors are separately measurable.
- (−) Metrics that lack a reliable XBRL tag are unsupported rather than approximated.
- (−) Two ingestion paths to maintain.

## Evidence (fill when implemented)
- Code: `app/compute/fact_selection.py`, `app/compute/metrics.py`, `app/tools/calculate_financial_metric.py`
- Tests: `tests/unit/test_fact_selection.py`, `tests/unit/test_metrics.py`; measured results in `artifacts/evals/cp6b-baseline-v1`
