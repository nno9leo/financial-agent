# ADR 0004 — Citation verifier: deterministic checks first, semantic judge only for narrative entailment

- **Status:** Accepted
- **Date:** 2026-09-25
- **Handoff refs:** §3.3, §3.7, §5.3 `verify_citations`, §7 坑6, 坑11, Q7, Q8

## Context
Letting the answering model judge its own citations inflates support. Retrieval relevance is not claim support.

## Decision
- Order of checks per claim: evidence ID exists in the ledger → stored content hash matches reloaded content → metadata (ticker/CIK/accession/section/period) matches → numbers and units match (numeric/metadata claims) → only then, for narrative claims, a semantic entailment judgment.
- Any deterministic failure short-circuits to `unsupported` with a reason; the semantic layer never overrides it.
- The verifier reloads evidence from the Evidence Ledger by ID, never from the answer text.
- Relevance (retrieval score) and support (verdict) are stored as separate fields.

## Consequences
- (+) Numeric/metadata verdicts are fully reproducible and unit-testable.
- (−) Semantic verdicts depend on a model; keep judge outputs and sample them for manual review.

## Evidence (fill when implemented)
- Code: `app/citations/` (`ledger.py`, `deterministic.py`, `semantic.py`, `openrouter_judge.py`, `verifier.py`), `app/tools/verify_citations.py`
- Five-class fixtures (correct / wrong year / wrong unit / irrelevant chunk / partial): `tests/unit/test_citations.py`, `tests/unit/test_citations_sources.py`
