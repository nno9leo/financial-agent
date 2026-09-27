# ADR 0008 — Agent runtime model: Claude Sonnet 5 (thinking: medium) via Pi, pinned explicitly per run

- **Status:** Accepted (2026-09-25)
- **Date:** 2026-09-25
- **Handoff refs:** §1 rules 5–6 (no fabricated metrics; every metric traceable to model/config/commit), §2.1, §5.0 / §5.0.1 (Pi), §7 坑8 (one variable at a time), §9.4 (`environment.json` must record the model ID), §13.3 (resume evidence ledger)

## Context

The Pi agent (the tool-calling runtime over our 8 tools) needs a model. Pi 0.87.1 is logged in to Anthropic, and the chosen model is **Claude Sonnet 5 with thinking level "medium"**.

Checking Pi's own state showed:
- Pi's model catalog (`~/.pi/agent/models-store.json`) lists this model as provider `anthropic`, id **`claude-sonnet-5`**, name "Claude Sonnet 5".
- Pi's persisted default (`~/.pi/agent/settings.json`) is still `defaultModel: "claude-opus-4-8"`. A model chosen in-session **is not persisted**.

So a Pi process launched without explicit flags would silently run a *different* model. *(Update 2026-09-25: `defaultModel` was set to `claude-sonnet-5` in `~/.pi/agent/settings.json`. No documented settings key for a default thinking level was found, so it was not set. Rules 3–4 below still apply unchanged: the persisted default is a convenience for interactive use, not something eval runs rely on.)* Any eval numbers from such a run would be attributed to the wrong model, which is exactly the traceability failure this project must avoid.

## Decision

1. **Runtime model:** provider `anthropic`, model id **`claude-sonnet-5`**, thinking level **`medium`**, runtime Pi `0.87.1`.
2. **Single source of truth:** `config/agent_runtime.json` (committed). The eval runner and any scripted Pi launch read this file. No other place hard-codes the model.
3. **Always pinned explicitly at launch:** `pi --model anthropic/claude-sonnet-5 --thinking medium …` (the `pi_cli_args` in the config). Never rely on Pi's `defaultModel`.
4. **Recorded as observed, not as configured:** the runner captures the model id (and thinking level, if exposed) that Pi **actually reports** for the run, e.g. from `--mode json` output, and writes both *configured* and *observed* values to `artifacts/evals/<run_id>/environment.json`. If observed ≠ configured, the run fails and produces no summary.
5. **Changing the model is an experiment variable** (坑8). A different model or thinking level means a new `config/agent_runtime.json` value, a new run ID, and results reported as a separate run, never mixed with this one.
6. **Semantic judge is a separate decision.** It is not this model by default. It is decided separately (ADR 0009: `openai/gpt-6-sol` via OpenRouter); offline tests use `scripted-fake/1`. A separate vendor avoids the answering model grading its own citations.

## Revision 2026-09-26 — third-party extra-usage failure; mandatory tool isolation

**Incident.** The first scripted Pi run (RPC discovery smoke) failed on its first model call:
`400 invalid_request_error: "Third-party apps now draw from your extra usage, not your plan limits. Add more at claude.ai/settings/usage and keep going."` (request_id `req_011CfRbHzqiouEaNTNSJY3WD`, 0 tokens).

**Root cause and fix.** Pi uses a Claude subscription through OAuth. Anthropic bills third-party-app calls to the account's **extra usage**, and the balance was insufficient. After the balance was topped up, the **unchanged** smoke passed: observed `anthropic/claude-sonnet-5` (medium) = configured, 8/8 tools, 0 tool calls. So the cause was not RPC mode, the explicit extension, or `-na`.

For future debugging:
- if this 400 reappears, check the extra-usage balance at claude.ai/settings/usage first;
- Pi's `$… (sub)` status-bar figure is a locally computed cost estimate, not proof of a charge.

**Option kept: A (Anthropic direct via subscription + extra usage), not B (Sonnet 5 via OpenRouter).** Owner decision, 2026-09-26:
- same price per token;
- A keeps the OpenRouter key out of the Pi process;
- smallest change.

Decisions 1–6 above are unchanged.

**New mandatory isolation for every scripted Pi launch.** It applies under A or B. Pi extensions run in-process with the same OS permissions, and Pi's built-in `bash`/`read` tools would let the model read `.env` or `~/.pi/agent/auth.json`:
- `--tools` allowlist = exactly our 8 tools, so no built-in file or shell tools;
- `--no-extensions` + explicit `--extension .pi/extensions/financial-research/index.ts`, so only the reviewed extension loads;
- `-na`;
- `--no-skills`, `--no-prompt-templates`.

These live in `config/agent_runtime.json` → `pi_isolation_args`. Launchers (the smoke script and the eval runner) must refuse to start Pi without them. A test asserts the extension source reads no environment variable except `FRA_API_URL`.

## Consequences

- (+) Every eval metric can be traced to an exact model id and thinking level, so every reported number is traceable to its runtime configuration.
- (+) Pi's changeable default can't silently alter results.
- (−) The runner must parse Pi's JSON output for the observed model. If Pi doesn't expose it, the runner must say so explicitly in `environment.json` (`"observed_model": "not_reported_by_runtime"`) rather than copying the configured value.
- (−) Resume/README wording must name the model as "Claude Sonnet 5 (claude-sonnet-5, thinking: medium) via Pi 0.87.1".

## Evidence (fill when implemented)

- Config: `config/agent_runtime.json` (2026-09-25).
- Runner reads the config and records configured + observed model: `evals/runner.py`, `evals/pi_runner.py` (each run's `environment.json`)
- Mismatch → run fails: `tests/unit/test_eval_harness.py`, `tests/unit/test_pi_runner.py`
