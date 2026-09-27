# ADR 0011 — Refusal policy: verified adjacent figures allowed; holdout pre-registration

**Status:** Accepted 2026-09-26 (policy decision). Pre-registration committed **before** grader v3 exists and before any holdout run.

## Context

In the arm-B dev run (ADR 0010 §Result), render v2 led the agent to add *verified adjacent figures* to otherwise-correct refusals, e.g. "FY2023 revenue $383,285 million" and "FY2024 growth 2.0 %" when refusing FY2023 growth. Grader v2 counted any supported numeric claim, or any figure in the requested metric's unit family, as a refusal violation. The policy decided here is that a refusal may include verified figures **for other metrics or other periods**. Only the requested metric for the requested period, or any unverified or fabricated figure, is forbidden.

## Refusal rule v3 (grader v3)

For a refusal item, the requested target is `(ticker, fiscal_year, metric)`: from `expected_answer.ground_truth_key`, or, for the three hand-written failure items, from a fixed map in the grader:
- `fail-unpinned-company` → (NVDA, 2025, revenue);
- `fail-unavailable-year` → (AAPL, 2023, revenue_growth_yoy);
- `fail-unsupported-metric` → (MSFT, 2026, gross_margin).

A refusal is **wrong** if any of these holds:
1. A **supported numeric claim targets the requested target**. The claim cites a `met_` id whose calculator/compare output in the record has the requested metric **and** fiscal year, or the claim cites no `met_` id but its assertion unit family equals the requested metric's family **and** its `period_end` equals the requested period end.
2. A figure in the final text is in the **requested metric's unit family** and isn't matched (within stated rounding) by a supported numeric claim from rule 1's complement, i.e. a supported claim about another metric or period.
3. Any currency or percent figure in the final text matches **neither** a successful calculator/compare output in the record **nor** a supported numeric claim, i.e. it's fabricated.

The accepted error-code evidence is unchanged (G3). Tool-selection uses the same function (a refusal violating v3 is also a tool-selection failure, as in v2).

## Pre-registration

**Metrics and thresholds:** as in ADR 0010 (P1 strict numeric over `numeric_lookup` + `mixed`; P2 pooled unsupported rate; same dev guards).

**Steps:**
1. Regrade offline with grader v3: the baseline `cp6b-baseline-v1` (A) and `cp7-render-v2-dev` (B-dev). Raw records must be unchanged; the prior grades are kept.
2. **Dev under v3.** Evaluate ADR 0010's dev rule on the v3 grades. Report it as "policy-compliant on dev", **not** as confirmation, because v3 was written after seeing these dev records.
3. **Holdout (confirmation; the only evidence that counts).** Only if step 2 passes: run B (render v2) once on holdout (12 items), clean tree, same commit as grader v3. Confirmed iff:
   - P1(B) ≥ P1(A-holdout, v3) **and** P2(B) ≤ P2(A-holdout, v3);
   - **and** refusal correct on holdout (v3) ≥ A;
   - **and** run-level errors ≤ 1.

   The A-holdout values are computed in step 1 and written into the result section before the holdout run starts.
4. **Outcomes:**
   - confirmed → `derived_metric_render` default becomes `v2`;
   - not confirmed → v1 stays the default and the result is reported as not confirmed;
   - no further tuning on holdout.

## Limitations

- The refusal rule changed after the dev data was seen, so the dev numbers under v3 are descriptive only.
- Holdout has only 1 refusal item (`fail-*`/`numeric_failure` items per split: dev 4, holdout 1). The refusal side of confirmation therefore rests on n=1; stated, not hidden.
- One run per arm; A ran at an earlier commit (render v1 byte-identical).

---

## Step 1–2 results (appended before the holdout run)

**Grader v3 regrade** (raw records unchanged; history in each run's `regrade.json`):
- `cp6b-baseline-v1`: **0 grade changes** (v3 doesn't affect arm A);
- `cp7-render-v2-dev`: the 2 refusals with verified adjacent figures → correct, and `fail-unavailable-year` tool selection → correct. No other changes.

**Dev under v3 (descriptive only, not confirmation):**

| | A | B |
|---|---|---|
| P1 | 0/10 | 8/10 |
| P2 | 14/33 | 3/43 |
| Tool | 23/23 | 22/23 |
| Refusal | 4/4 | 4/4 |
| Retrieval | 6/7 | 6/7 |
| Narrative unsupported | 0 | 0 |
| Run errors | 1 | 1 |

All ADR 0010 dev thresholds are met → proceed to holdout.

**Fixed holdout thresholds (A-holdout, grader v3):** P1(A) = **4/9**, P2(A) = **5/21**, refusal(A) = **1/1**, run errors ≤ 1. B is confirmed iff:
- P1(B) ≥ 4/9;
- **and** P2(B) ≤ 5/21 (23.8 %);
- **and** refusal ≥ 1/1;
- **and** run errors ≤ 1.

---

## Holdout result (appended after the run; thresholds above were committed in `40fc1c8` before it started)

Run `artifacts/evals/cp7-render-v2-holdout` (git `40fc1c8`, clean, render v2 served, grader v3, $0.348).

| Holdout | A (baseline, v3) | B (render v2) | Threshold | Pass |
|---|---|---|---|---|
| P1 strict numeric | 4/9 | **7/9** | ≥ 4/9 | ✅ |
| P2 unsupported-claim rate | 5/21 (23.8 %) | **0/23 (0 %)** | ≤ 5/21 | ✅ |
| Refusal correct | 1/1 | 1/1 | ≥ 1/1 | ✅ |
| Run-level errors | 0 | 1 | ≤ 1 | ✅ |
| (reported) tool selection | 12/12 | 11/12 | — | |
| (reported) retrieval hit | 3/4 | 4/4 | — | |
| (reported) narrative unsupported | 0 | 0 | — | |

**Decision: CONFIRMED. `derived_metric_render` default → `v2`.**

The two B failures (from reading the raw records) are unrelated to the variable:
- `msft-num-operating_margin-2026`: a 300 s model stall after `message_start`. Exactly one per real run so far (baseline, B-dev, B-holdout); infrastructure backlog item #1.
- `msft-num-revenue-2026`: the model claimed "FY2026 has not completed". The same model-time confusion appears in the baseline.

**Caveats:**
- One run per arm; A ran at an earlier commit (render v1 byte-identical).
- Holdout n is small (9 strict numeric items, 1 refusal item).
- The refusal policy (v3) was set after seeing dev data. Holdout was untouched until this run.

## Disclosure added during the documentation audit (2026-09-26)

The render defect was diagnosed from the baseline's raw records for **all 35 items, including the 12 holdout baseline rows**. The holdout wasn't used to choose the change, a threshold or a policy. Its arm-B run happened once, after the thresholds were committed. Still, it isn't a split that no human has looked at. The fix is mechanistic (the result text lacked the citable `met_` id), which limits but doesn't remove this concern.

> **Note on commit hashes.** Commit hashes cited in this ADR refer to the original development repository. This public repository was published as a single snapshot, so those commits aren't present here; each run's `environment.json` records the commit it ran at.
