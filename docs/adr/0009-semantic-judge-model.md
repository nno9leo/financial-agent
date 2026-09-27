# ADR 0009 — Semantic judge: GPT-6 Sol (reasoning high) via OpenRouter, separate from the answering model

- **Status:** Accepted (2026-09-26). Implemented; live smoke passed.
- **Handoff refs:** §3.3, §4.3, §7 坑6 (the same model must not grade itself), §9.2 (narrative correctness with a controlled judge; keep judge outputs), §9.4 (`environment.json` model IDs), Q7; ADR 0004 (the judge only handles narrative entailment, after deterministic checks), ADR 0008 (the answering model is Claude Sonnet 5)

## Context

ADR 0004 puts an LLM judge only at the last step: narrative-claim entailment, after every deterministic check has passed. A scripted fake judge (`scripted-fake/1`) serves offline tests. A real judge is needed for the evaluations.

**Option rejected:** reusing Pi's ChatGPT login. That path reuses ChatGPT OAuth credentials inside a third-party tool, OpenAI has no explicit statement allowing a ChatGPT subscription to be used this way, and it carries account risk.

**Option chosen:** OpenRouter with a dedicated API key. The model was verified on OpenRouter's public model list on 2026-09-26:
- id **`openai/gpt-6-sol`**, "OpenAI: GPT-6 Sol";
- supported parameters include `reasoning`, `structured_outputs`, `response_format`, `seed`; **no `temperature`**;
- price $2 / M prompt tokens, $10 / M completion tokens.

## Decision

1. **Judge model:** `openai/gpt-6-sol` through `https://openrouter.ai/api/v1` (OpenAI-compatible chat completions), **`reasoning.effort = "high"`**.
   - Why high rather than medium: the judge's labels directly drive the headline *unsupported-claim rate*, so accuracy matters more than latency. The volume is small: ~100 judge calls per eval run. *(Corrected by measurement 2026-09-26: ≈ $0.0017 per call at high, so ≈ $0.2 per run, not the earlier $4–5 estimate.)*
   - Effort is recorded per run; changing it is a single-variable experiment (坑8).
2. **Separate from the answering model:** the answering agent is Claude Sonnet 5 (ADR 0008), a different vendor and model family, so the judge isn't grading its own output (坑6).
3. **Pin the route:** OpenRouter `provider: {"order": ["OpenAI"], "allow_fallbacks": false}`, so requests aren't silently routed to a different upstream. Fixed `seed` (best effort; reasoning models aren't guaranteed deterministic).
4. **Structured output:** `response_format` with a JSON schema `{label: entailed|partial|not_entailed, score: 0..1, rationale: string}`. Output that fails the schema is a typed judge error. The claim is then reported as a **judge failure**, never silently counted as supported or unsupported.
5. **Record as observed, not just as configured.** For every call, keep:
   - the configured model id and reasoning effort;
   - the response's `model` and `provider` fields and the OpenRouter generation `id`;
   - token usage.

   At run level, `artifacts/evals/<run_id>/environment.json` records `judge.configured_model`, `judge.observed_models` (the set seen), `judge.reasoning_effort`, and `judge.gateway`. An observed model other than the configured one fails the run, mirroring ADR 0008. Judge rationales are kept in `results.jsonl` for the manual spot check.
6. **Credentials:** `OPENROUTER_API_KEY` lives only in the gitignored `.env`. It is written there directly, never pasted into chat or logs. Code reads it via `Settings()`. It is never written to traces, artifacts or error messages. A missing key is a typed configuration error **before** any request. Recommended: a credit limit on the key in the OpenRouter dashboard.
7. **Offline tests stay offline:** unit tests keep `scripted-fake/1`. A live judge smoke test is marked (e.g. `@pytest.mark.live_llm`) and deselected by default.

## Live verification (2026-09-26, 3 calls, total ≈ $0.004)

Performed with the key read from `.env` without printing it. The key was present with the `sk-or` prefix.

| Check | Result |
|---|---|
| Response `model` field | `openai/gpt-6-sol`, **identical** to the configured id, so the mismatch rule can be strict string equality |
| Response `provider` field | `OpenAI`, so the pinned route (`order: [OpenAI]`, `allow_fallbacks: false`) holds |
| `response_format` json_schema (strict) | respected: `{"label", "score", "rationale"}` only, `finish_reason: stop` |
| `reasoning.effort` applied? | yes. On a partial case: high → 33 reasoning tokens with reasoning text returned; minimal → 12. A trivial case used 0 at high, because the model is adaptive and that is not a failure |
| Labels on demo-like claims | c07-style (fully supported) → `entailed` 1.0; c09-style (Pro models + unstated Greater China growth) → `partial` at both high (0.67) and minimal (0.5) |
| Latency / cost | 1.5–3.3 s per call; $0.0008–0.0017 per call |

## Consequences

- (+) Narrative verdicts have a named, versioned, externally hosted judge distinct from the answering model; every verdict is traceable to model, route and effort.
- (+) No account-policy risk from reusing subscription OAuth.
- (−) Costs money per run (small), and adds a network dependency to live evals only.
- (−) Judge verdicts are model opinions. Keep rationales and spot-check a sample by hand before quoting any unsupported-claim rate.

## Evidence (fill when implemented)

- Config: `config/agent_runtime.json` → `semantic_judge` (2026-09-26).
- `OpenRouterJudge` implementing `SemanticJudge`: `app/citations/openrouter_judge.py`; offline tests in `tests/unit/test_openrouter_judge.py`.
- Live smoke 2026-09-26: `artifacts/verifier_demo/judge_smoke.json`. 4/4 demo narrative claims got the expected verdict (c07 supported, c08 unsupported, c09 partially_supported, c10 supported); observed model/provider/effort = configured; $0.00717. A smoke, not a metric.
- `environment.json` judge fields + mismatch → fail: `evals/runner.py`; test in `tests/unit/test_eval_harness.py`
- Manual spot check of judge verdicts: 2 narrative claims checked against the source chunk text; both verdicts agreed (a small sample, not a calibration).

> **Note on commit hashes.** Commit hashes cited in this ADR refer to the original development repository. This public repository was published as a single snapshot, so those commits aren't present here; each run's `environment.json` records the commit it ran at.
