# ADR 0006 — Eval dataset composition and leakage control

- **Status:** Accepted
- **Date:** 2026-09-25
- **Handoff refs:** §1 (严禁目标数字), §9.0–9.4, §7 坑4, 坑7, 坑8, Q11, Q12

## Context
A one-week MVP cannot hand-label 80–150 items. The dataset must still be versioned, reproducible and free of leakage, and must measure tool paths and citations, not just answers.

## Decision
- Target ≈36 items (allowed 24–40): 12–20 numeric items auto-generated from pinned `companyfacts` by code (ground truth never produced by the model under test); 8–12 converted external items (FinanceBench/FinQA/TAT-QA) with dataset ID, license and conversion script recorded; 8–10 project-specific MD&A / tool-path / citation / failure-case items written or reviewed by hand.
- Every item records: question type, ticker, accession, cutoff date, expected answer (+unit, tolerance), expected tools/sequence, expected evidence.
- Only facts filed on/before the item's cutoff are eligible; retrieval is restricted to the pinned accession.
- Dev vs holdout split is declared in the dataset manifest before tuning.
- All reported metrics come from `artifacts/evals/<run_id>/summary.json`; design targets (e.g. 90%/10%) are thresholds, never reported as results.

## Consequences
- (+) Reproducible, auditable numbers; tool-selection and citation metrics exist alongside accuracy.
- (−) Small N → wide confidence intervals; must be stated in README limitations.

## Evidence (fill when implemented)
- Dataset + manifest: `evals/datasets/questions.v1.jsonl`, `evals/datasets/dataset_manifest.json`
- Baseline / A-B runs: `artifacts/evals/cp6b-baseline-v1`, `artifacts/evals/cp7-render-v2-dev`, `artifacts/evals/cp7-render-v2-holdout`
