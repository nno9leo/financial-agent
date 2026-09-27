# ADR 0003 — Single tool-calling agent with an explicit query plan

- **Status:** Accepted
- **Date:** 2026-09-25
- **Handoff refs:** §3.6, §4.2/4.3 "不应该照搬", §5.4, §14, Q10

## Context
The hard problems are period/unit/accession selection and citation correctness, not orchestration. Multi-agent setups add message passing, debugging surface and eval variables.

## Decision
- One Pi agent calls 8 typed tools following a small query plan: numbers from the calculator, explanations from retrieval, then verification.
- Business logic lives in Python services behind FastAPI; the Pi extension is a thin typed client. The runtime is replaceable without touching domain code.
- At most one evidence retry after a verifier `unsupported` verdict.

## Consequences
- (+) Fewer moving parts; tool-selection accuracy is directly measurable per question.
- (−) No planner/reviewer specialization; revisit only if failure analysis shows tool choice/planning is the dominant error.

## Evidence (fill when implemented)
- Tool registry / query plan: `app/tools/registry.py`, `.pi/extensions/financial-research/index.ts`
- Tool-selection accuracy: 35/35 on the baseline (`artifacts/evals/cp6b-baseline-v1`).
