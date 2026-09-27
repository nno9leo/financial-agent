# ADR 0010 — Experiment: rendering of derived-metric tool results (pre-registered)

**Status:** Pre-registered 2026-09-26, **before** any arm-B run. The decision rule below is fixed; it isn't edited after results are seen.

## Hypothesis

In baseline v1 (`artifacts/evals/cp6b-baseline-v1`), all 19 unsupported claims were numeric citation-shape errors. The Pi extension's result text for `calculate_financial_metric` shows only the input `fact_…` ids (labelled "Evidence IDs"), never the `met_…` metric evidence id, and `compare_periods` shows no ids. Neither shows the exact `period_end`. **If** the rendered text shows the citable `met_` id, the exact period_end and the accepted unit forms, **then** verified numeric accuracy rises and the unsupported-claim rate falls, without hurting refusals, tool selection or narrative claims.

## Single variable

`experiments.derived_metric_render` ∈ {`v1` (current), `v2` (shows `met_` id + period + unit forms)}. It affects only the content text of those two tools. Everything else stays unchanged: the backend outputs, verifier, grader (v2), dataset (v1, sha `121d669d…`), guidance prompt, models, top_k, timeouts and retry budget.

## Arms

- **A (control):** the existing baseline `cp6b-baseline-v1` (git 9ca9f7b, render v1, regraded with grader v2). Budget reason: it's not re-run. A snapshot test proves the v1 render text byte-identical; the other code changes since 9ca9f7b are grading/diagnostics only.
- **B (treatment):** render v2, same commit/config as the render-switch change, clean tree, one run on **dev** (23 items). Only if the dev rule passes: one run on **holdout** (12 items). Nothing changes between the dev and holdout runs.

## Metrics (grader v2)

- **P1: strict numeric correct** = `numeric_correct` over `numeric_lookup` + `mixed` items (verify-report path; comparison items reported separately because their rule is the looser text-vs-`compare_periods` check). A-dev = **0/10**, A-holdout = **4/9**.
- **P2: pooled unsupported-claim rate.** A-dev = **14/33**, A-holdout = **5/21**.

**Guards (dev):**
- tool selection ≥ 22/23 (A 23/23);
- refusal correct ≥ A (4/4);
- retrieval hit ≥ A − 1 (A 6/7);
- narrative claims unsupported ≤ 1 (A 0);
- run-level errors ≤ 1.

## Decision rule

- **Adopt on dev** iff P1(B) − P1(A) ≥ **+3 items** **and** P2(B) < P2(A) **and** all guards hold.
- **Holdout confirmation** iff P1(B) ≥ P1(A) (4/9) **and** P2(B) ≤ P2(A) (5/21).
- **Outcomes:**
  - dev pass + holdout confirm → make `v2` the default;
  - dev pass + holdout not confirmed → report as "not confirmed" and keep `v1` default;
  - dev fail → stop, don't run holdout.

## Limitations stated up front

- One run per arm; LLM nondeterminism.
- A was run at an earlier commit.
- Small n (10 / 9 strict numeric items). The +3-item threshold is a practical-effect bar, not a significance test.
- The results describe these two filings only.

---

## Result (appended 2026-09-26 after the arm-B dev run; the rule above is unchanged)

Arm B: `artifacts/evals/cp7-render-v2-dev` (git 7fb2cd2, clean, render v2 served by `/config/runtime`, grader v2, $0.839). The guidance text (`QUERY_PLAN_GUIDELINES`) and tool descriptions are byte-identical to arm A. The run's `prompt_guidance_sha256` differs only because that field hashes the whole `index.ts`; it's misnamed, and a backlog item.

| Dev | A (baseline) | B (render v2) | Rule | Pass |
|---|---|---|---|---|
| P1 strict numeric correct | 0/10 | **8/10** | ≥ +3 items | ✅ |
| P2 unsupported-claim rate | 14/33 (42 %) | **3/43 (7 %)** | B < A | ✅ |
| Tool selection | 23/23 | 21/23 | ≥ 22/23 | ❌ |
| Refusal correct | 4/4 | 2/4 | ≥ 4/4 | ❌ |
| Retrieval hit | 6/7 | 6/7 | ≥ 5/7 | ✅ |
| Narrative claims unsupported | 0 | 0 | ≤ 1 | ✅ |
| Run-level errors | 1 | 1 | ≤ 1 | ✅ |

**Decision: dev FAILS the pre-registered guards, so the holdout run is not performed and `v1` stays the default.**

Why the guards failed (from reading the raw records):
- Both refusal failures, and one tool-selection failure, are a **new behaviour induced by v2**. The agent still refused the unanswerable FY2023 growth question, but now also stated *verified adjacent figures* ("FY2023 revenue $383,285 million", "FY2024 growth 2.0 %", both correct and supported). The refusal rule counts any supported numeric claim or requested-unit-family figure in a refusal as a violation.
- The other tool-selection failure is a 300 s model stall after `message_start` (1 per arm; infrastructure noise).

What this shows: the render fix addresses the dominant failure mode, but it changes refusal behaviour. Whether verified adjacent figures are acceptable in a refusal is a **policy decision**. It must be made, and pre-registered, before any further run. The holdout split is still untouched by any v2 run, so it remains usable for a clean confirmation of a follow-up.

> **Note on commit hashes.** Commit hashes cited in this ADR refer to the original development repository. This public repository was published as a single snapshot, so those commits aren't present here; each run's `environment.json` records the commit it ran at.
