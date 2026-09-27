"""Offline-safe eval orchestration and artifact writer. Pi mode is intentionally opt-in."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import os
import re
import ast
import signal
import socket
import urllib.request
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from app.api.app import ApiDeps, create_app
from app.config import Settings
from app.providers.sec import SecProvider
from evals.build_dataset import ROOT, read_jsonl
from evals.graders import GRADER_VERSION, grade_item, summarize


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _embedding_sha256() -> str | None:
    candidates = sorted((ROOT / "data/models").glob("models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/*/model_optimized.onnx"))
    if not candidates:
        return None
    digest = hashlib.sha256()
    with candidates[0].open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git() -> tuple[str, bool]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ("pytest-forced-clean", False) if os.environ.get("FRA_TEST_FORCE_CLEAN_GIT") == "1" else ("pytest-dirty", True)
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        return sha, dirty
    except Exception:
        return "unavailable", True


def validate_observed_models(config: dict, observed_agent: str, observed_judge: str) -> None:
    expected_agent = f"{config['provider']}/{config['model_id']}"
    expected_judge = config["semantic_judge"]["model_id"]
    if observed_agent != expected_agent:
        raise RuntimeError(f"configured/observed agent model mismatch: {expected_agent} != {observed_agent}")
    if observed_judge != expected_judge:
        raise RuntimeError(f"configured/observed judge model mismatch: {expected_judge} != {observed_judge}")


class LiveEvalGuardError(RuntimeError):
    code = "live_eval_blocked_in_pytest"


def _guidance_lines(source: str) -> list[str]:
    match = re.search(r"const\s+QUERY_PLAN_GUIDELINES\s*=\s*\[(.*?)\]\s*;", source, re.S)
    if not match:
        raise ValueError("QUERY_PLAN_GUIDELINES not found in extension source")
    return ast.literal_eval("[" + match.group(1) + "]")


def _guidance_text_sha256(source: str | None = None) -> str:
    if source is None:
        source = (ROOT / ".pi/extensions/financial-research/index.ts").read_text()
    return hashlib.sha256("\n".join(_guidance_lines(source)).encode("utf-8")).hexdigest()


def _progress_line(out: Path, *, n: int, total: int, item: dict, row: dict,
                   started: float, cumulative_cost: float) -> float:
    def cost(key):
        value = row.get(key)
        return float(value) if isinstance(value, (int, float)) else None
    pi_cost, judge_cost = cost("cost_pi_usd"), cost("cost_judge_usd")
    cumulative_cost += sum(value for value in (pi_cost, judge_cost) if value is not None)
    record = {"n": n, "total": total, "question_id": item["question_id"],
              "elapsed_s": round(time.perf_counter() - started, 6), "latency_s": row.get("latency_s"),
              "cost_pi_usd": pi_cost, "cost_judge_usd": judge_cost,
              "cumulative_cost_usd": round(cumulative_cost, 8), "runtime_error": bool(row.get("runtime_error"))}
    for key in ("numeric_correct", "refusal_correct"):
        if key in (row.get("grades") or {}):
            record[key] = row["grades"][key]
    record["stall_retries_used"] = int(row.get("stall_retries_used", 0) or 0)
    line = json.dumps(record, separators=(",", ":"), default=str)
    with (out / "progress.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(line, flush=True)
    return cumulative_cost


def run_eval(*, split="all", agent="fake", run_id="local", limit=None, allow_dirty=False,
             output_root: Path | None = None, client_factory: Callable | None = None,
             derived_metric_render: str | None = None) -> Path:
    dataset_path = ROOT / "evals/datasets/questions.v1.jsonl"
    items = read_jsonl(dataset_path)
    if split != "all": items = [x for x in items if x["split"] == split]
    if limit is not None: items = items[:limit]
    cfg = json.loads((ROOT / "config/agent_runtime.json").read_text())
    configured_render = cfg.get("experiments", {}).get("derived_metric_render", "v2")
    if derived_metric_render not in {None, "v1", "v2"}:
        raise ValueError("derived_metric_render must be v1 or v2")
    judge_cfg = cfg["semantic_judge"]
    if agent not in {"fake", "pi"}: raise ValueError("agent must be pi or fake")
    if agent == "pi" and os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("FRA_ALLOW_LIVE_EVAL") != "1":
        raise LiveEvalGuardError("Pi eval is blocked under pytest unless FRA_ALLOW_LIVE_EVAL=1")
    sha, dirty = _git()
    if agent == "pi" and dirty and not allow_dirty:
        raise RuntimeError("Pi eval refuses a dirty tree; pass --allow-dirty to record it")
    out = output_root or (ROOT / ("artifacts/evals/_fake" if agent == "fake" else "artifacts/evals") / run_id)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps({"dataset_version": "v1", "dataset_sha256": _sha(dataset_path),
        "split": split, "top_k": 5, "max_verify_attempts": cfg["max_verify_attempts"],
        "derived_metric_render": configured_render,
        "per_item_timeout_s": cfg["eval_timeout_s"], "max_tool_calls_per_item": cfg["eval_max_tool_calls"],
        "stall_idle_s": cfg.get("eval_stall_idle_s", 120), "stall_retries": cfg.get("eval_stall_retries", 1),
        "extension_source_sha256": _sha(ROOT / ".pi/extensions/financial-research/index.ts"),
        "guidance_text_sha256": _guidance_text_sha256(), "agent": agent,
        "allow_dirty": allow_dirty}, indent=2) + "\n")
    (out / "dataset_manifest.json").write_bytes((ROOT / "evals/datasets/dataset_manifest.json").read_bytes())
    versions = {"git_sha": sha, "dirty_tree": dirty, "python": platform.python_version(), "node": _version("node"),
                "pi": cfg.get("pi_version"), "agent": {"configured_model": f"{cfg['provider']}/{cfg['model_id']}",
                "configured_thinking": cfg["thinking_level"], "observed_model": "not_applicable_fake" if agent == "fake" else "pending_runtime",
                "observed_thinking": "not_applicable_fake" if agent == "fake" else "pending_runtime"},
                "judge": {"implementation": "ScriptedJudge" if agent == "fake" else "configured_semantic_judge",
                          "configured_model": "scripted-fake/1" if agent == "fake" else judge_cfg["model_id"],
                          "configured_provider": "offline" if agent == "fake" else (judge_cfg.get("provider_routing", {}).get("order") or [None])[0],
                          "configured_gateway": "offline" if agent == "fake" else judge_cfg["gateway"],
                          "configured_effort": "not_applicable_fake" if agent == "fake" else judge_cfg["reasoning_effort"],
                          "observed_models": ["scripted-fake/1"] if agent == "fake" else [],
                          "observed_providers": ["offline"] if agent == "fake" else [],
                          "observed_efforts": ["not_applicable_fake"] if agent == "fake" else [],
                          "provider": "offline" if agent == "fake" else (judge_cfg.get("provider_routing", {}).get("order") or [None])[0],
                          "effort": "not_applicable_fake" if agent == "fake" else judge_cfg["reasoning_effort"],
                          "gateway": "offline" if agent == "fake" else judge_cfg["gateway"]},
                "embedding": {"model_id": "BAAI/bge-small-en-v1.5", "weights_sha256": _embedding_sha256()},
                "data_manifest_sha256": _sha(ROOT / "data/manifests/filings.v1.json"), "secrets": "omitted"}
    (out / "environment.json").write_text(json.dumps(versions, indent=2) + "\n")
    fixture_dir = ROOT / "tests/fixtures/eval/fake_agent"
    plans = {p.stem: json.loads(p.read_text()) for p in fixture_dir.glob("*.json")}
    (out / "progress.jsonl").write_text("", encoding="utf-8")
    progress_started = time.perf_counter()
    if agent == "pi":
        rows = _run_pi_items(items, cfg, out, derived_metric_render=derived_metric_render,
            progress_callback=lambda n, item, row, cumulative: _progress_line(out, n=n, total=len(items), item=item,
                row=row, started=progress_started, cumulative_cost=cumulative))
        summary_agent = "pi"
    else:
        summary_agent = "fake"
        from app.citations.semantic import ScriptedJudge
        settings = Settings(_env_file=None)
        scripted_judge_factory = lambda: ScriptedJudge({})
        client = client_factory() if client_factory else TestClient(create_app(ApiDeps(
            settings=settings, sec_provider_factory=lambda: SecProvider(settings),
            judge_factory=scripted_judge_factory,
            reports_dir=ROOT / "artifacts/reports",
            derived_metric_render_override=derived_metric_render)))
        rows = []
        with client as c:
            runtime_response = c.get("/config/runtime")
            runtime_response.raise_for_status()
            served_mode = runtime_response.json().get("derived_metric_render")
            _record_render_mode(out, served_mode)
            for item in items:
                started = time.perf_counter()
                plan = plans.get(item["question_id"], {"tool_calls": []})
                calls, evidence, verify_outputs, metadata = [], [], [], {}
                for call in plan.get("tool_calls", []):
                    response = c.post(f"/tools/{call['name']}", json=call["args"])
                    try: body = response.json()
                    except Exception: body = {}
                    calls.append({"name": call["name"], "args": call["args"], "ok": response.is_success,
                                  "error_code": body.get("error", {}).get("code"),
                                  "result_summary": body if response.is_success else {"status": response.status_code}})
                    if call["name"] == "search_filing_chunks" and response.is_success:
                        evidence.extend({"evidence_id": x["evidence_id"], "rank": i + 1} for i, x in enumerate(body.get("evidence", [])))
                    if call["name"] == "verify_citations" and response.is_success: verify_outputs.append(body)
                    if call["name"] == "list_filings" and response.is_success and body.get("filings"):
                        filing = body["filings"][0]
                        metadata = {"accession_number": filing.get("accession_number"), "filing_date": filing.get("filing_date"),
                                    "report_date": filing.get("report_date"), "form": filing.get("form"),
                                    "ticker": body.get("company", {}).get("ticker"), "cik": body.get("company", {}).get("cik")}
                record = {"question_id": item["question_id"], "tool_calls": calls, "verify_outputs": verify_outputs,
                          "final_assistant_text": plan.get("final_assistant_text", ""), "search_evidence": evidence,
                          "metadata": metadata, "top_k": plan.get("top_k", 5), "latency_s": round(time.perf_counter() - started, 6),
                          "tokens_in": None, "tokens_out": None, "cost_pi_usd": 0, "cost_judge_usd": 0,
                          "runtime_error": None}
                rows.append({**record, "question_id": item["question_id"], "question_type": item["question_type"],
                             "split": item["split"], "grades": grade_item(item, record)})
                _progress_line(out, n=len(rows), total=len(items), item=item, row=rows[-1], started=progress_started,
                               cumulative_cost=sum(float(r.get("cost_pi_usd") or 0) + float(r.get("cost_judge_usd") or 0) for r in rows[:-1]))
    (out / "results.jsonl").write_text("".join(json.dumps(x, sort_keys=True, default=str) + "\n" for x in rows))
    summary = summarize(rows, agent=summary_agent)
    summary["grader_version"] = GRADER_VERSION
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    (out / "failures.md").write_text(_failures(items, rows, agent))
    (out / "README.md").write_text("# Eval run\n\n" + ("PIPELINE TEST — NOT A MEASUREMENT.\n\nProduced offline by replaying scripted fake tool-call plans against backend handlers. This is not a target or baseline. Headline `unsupported_claim_rate` and `citation_coverage` are pooled over claims and include `n_claims`; `*_item_mean` fields are secondary per-item means.\n" if agent == "fake" else "Real Pi run; measurements belong only to this run artifact. Headline `unsupported_claim_rate` and `citation_coverage` are pooled over claims and include `n_claims`; `*_item_mean` fields are secondary per-item means.\n"))
    return out


def _version(cmd: str) -> str:
    try: return subprocess.check_output([cmd, "--version"], text=True, stderr=subprocess.STDOUT).strip()
    except Exception: return "unavailable"


def _record_render_mode(out: Path, value: object) -> None:
    if value not in {"v1", "v2"}:
        raise RuntimeError(f"backend served invalid derived_metric_render: {value!r}")
    config_path = out / "config.json"
    run_config = json.loads(config_path.read_text())
    run_config["derived_metric_render"] = value
    config_path.write_text(json.dumps(run_config, indent=2) + "\n")


def _run_pi_items(items: list[dict], config: dict, out: Path, *,
                  derived_metric_render: str | None = None, progress_callback=None) -> list[dict]:
    """Run a loopback backend and one isolated Pi RPC process per question."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    env = {"PATH": os.environ.get("PATH", ""), "FRA_API_PORT": str(port), "PYTHONPATH": str(ROOT)}
    if derived_metric_render is not None:
        env["FRA_EXPERIMENT_DERIVED_METRIC_RENDER"] = derived_metric_render
    backend = subprocess.Popen([sys.executable, str(ROOT / "scripts/serve.py"), "--host", "127.0.0.1", "--port", str(port)],
                               cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    previous_handlers = {}
    def stop_on_signal(signum, frame):
        _terminate_backend(backend)
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[sig] = signal.signal(sig, stop_on_signal)
        except ValueError:
            pass
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if backend.poll() is not None: raise RuntimeError(f"backend exited: {backend.stderr.read()}")
            try:
                with urllib.request.urlopen(url + "/health", timeout=1): break
            except Exception: time.sleep(.2)
        else: raise RuntimeError("backend startup timed out")
        with urllib.request.urlopen(url + "/config/runtime", timeout=3) as response:
            runtime_config = json.loads(response.read())
        _record_render_mode(out, runtime_config.get("derived_metric_render"))
        rows = _run_pi_item_batch(items, config, url, out, progress_callback=progress_callback)
        env_path = out / "environment.json"
        observed_models = sorted({r["observed_agent_model"] for r in rows})
        observed_thinking = sorted({r["observed_agent_thinking"] for r in rows})
        all_observations = [j for r in rows for j in r.get("judge_observations", [])]
        observed_judge_models = sorted({j.get("observed_model") for j in all_observations if j.get("observed_model")})
        if observed_judge_models and any(model != config["semantic_judge"]["model_id"] for model in observed_judge_models):
            raise RuntimeError(f"configured/observed judge model mismatch: {config['semantic_judge']['model_id']} != {observed_judge_models}")
        env_data = json.loads(env_path.read_text())
        env_data["agent"].update({"observed_model": observed_models[0] if len(observed_models) == 1 else observed_models,
                                  "observed_thinking": observed_thinking[0] if len(observed_thinking) == 1 else observed_thinking})
        env_data["judge"]["observed_models"] = observed_judge_models or ["not_invoked"]
        env_data["judge"]["observed_providers"] = sorted({j.get("provider") for j in all_observations if j.get("provider")}) or ["not_invoked"]
        env_data["judge"]["observed_efforts"] = sorted({j.get("reasoning_effort") for j in all_observations if j.get("reasoning_effort")}) or ["not_invoked"]
        env_data["judge"]["calls"] = [{k: j.get(k) for k in ("observed_model", "provider", "generation_id", "reasoning_effort",
                              "prompt_tokens", "completion_tokens", "reasoning_tokens", "cost_usd", "latency_s")}
                              for j in all_observations]
        env_path.write_text(json.dumps(env_data, indent=2) + "\n")
        return rows
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        _terminate_backend(backend)


def _terminate_backend(backend) -> None:
    if backend.poll() is not None:
        return
    backend.terminate()
    try:
        backend.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend.kill()
        backend.wait(timeout=5)


def _write_aborted(out: Path, *, reason: str, first_error: str,
                   request_ids: list[str], attempted: list[str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").unlink(missing_ok=True)
    request_text = ", ".join(request_ids) if request_ids else "none reported"
    attempted_text = ", ".join(attempted) if attempted else "none"
    body = ("# ABORTED — NOT A MEASUREMENT\n\n"
            f"Reason: {reason}\n\n"
            f"First error text: {first_error}\n\n"
            f"Request IDs: {request_text}\n\n"
            f"Items attempted ({len(attempted)}): {attempted_text}\n")
    (out / "ABORTED.md").write_text(body, encoding="utf-8")


def _run_pi_item_batch(items: list[dict], config: dict, api_url: str, out: Path,
                       *, pi_bin: str | list[str] | tuple[str, ...] | None = None, progress_callback=None) -> list[dict]:
    """Run items and stop before grading a provider/account failure or systemic startup failure."""
    from evals.pi_runner import ProviderAccountError

    rows, attempted = [], []
    cumulative_cost = 0.0
    for item_n, item in enumerate(items, 1):
        attempted.append(item["question_id"])
        try:
            row = _run_pi_item(item, config, api_url, pi_bin=pi_bin)
        except ProviderAccountError as exc:
            _write_aborted(out, reason="provider_account_failure", first_error=str(exc),
                           request_ids=exc.request_ids, attempted=attempted)
            raise
        if any(c.get("error_code") in {"judge_model_mismatch", "judge_route_mismatch"} for c in row["tool_calls"]):
            raise RuntimeError("observed judge model/provider mismatch; refusing run")
        for observation in row.get("judge_observations", []):
            if observation.get("observed_model") != config["semantic_judge"]["model_id"]:
                raise RuntimeError(f"configured/observed judge model mismatch: {config['semantic_judge']['model_id']} != {observation.get('observed_model')}")
            if observation.get("provider") != (config["semantic_judge"].get("provider_routing", {}).get("order") or [None])[0]:
                raise RuntimeError(f"configured/observed judge provider mismatch: {observation.get('provider')}")
            if observation.get("reasoning_effort") != config["semantic_judge"]["reasoning_effort"]:
                raise RuntimeError(f"configured/observed judge effort mismatch: {observation.get('reasoning_effort')}")
        rows.append(row)
        if progress_callback:
            cumulative_cost = progress_callback(item_n, item, row, cumulative_cost)
        if len(rows) == 3 and all(r.get("runtime_error") and not r.get("tool_calls") for r in rows):
            first_error = next((r["runtime_error"] for r in rows if r.get("runtime_error")), "runtime failures")
            _write_aborted(out, reason="circuit_breaker", first_error=first_error,
                           request_ids=[], attempted=attempted)
            raise RuntimeError("circuit_breaker: first 3 items failed with runtime errors and zero tool calls")
    return rows


def _run_pi_item(item: dict, config: dict, api_url: str, *, pi_bin: str | list[str] | tuple[str, ...] | None = None) -> dict:
    """Normalize a Pi result, preserving partial observations on ordinary item failures."""
    from evals.pi_runner import PiModelMismatchError, PiStallError, ProviderAccountError, run_one
    retry_limit = max(0, int(config.get("eval_stall_retries", 1)))
    attempts: list[dict] = []
    final_data: dict | None = None
    stalled_count = 0
    for attempt_index in range(retry_limit + 1):
        try:
            data = run_one(item["question"], timeout_s=int(config.get("eval_timeout_s", 180)),
                           max_tool_calls=int(config.get("eval_max_tool_calls", 30)), api_url=api_url,
                           pi_bin=pi_bin, stall_idle_s=float(config.get("eval_stall_idle_s", 120)))
            outcome = "success" if not data.get("runtime_error") else "runtime_error"
        except PiStallError as exc:
            data = dict(exc.partial)
            data.setdefault("runtime_error", f"PiStallError: {exc}")
            outcome = "stall"
            stalled_count += 1
        except (PiModelMismatchError, ProviderAccountError):
            raise
        except Exception as exc:
            data = dict(getattr(exc, "partial", {}) or {})
            data.setdefault("tool_calls", [])
            data.setdefault("verify_outputs", [])
            data.setdefault("final_assistant_text", "")
            data.setdefault("latency_s", None)
            data.setdefault("tokens_in", None)
            data.setdefault("tokens_out", None)
            if data.get("cost_pi_usd") is None:
                data["cost_pi_usd"] = None
                data["cost_pi_status"] = "unknown_run_failed"
            data.setdefault("cost_judge_usd", 0)
            data.setdefault("judge_observations", [])
            data.setdefault("observed_agent_model", "not_reported_by_runtime")
            data.setdefault("observed_agent_thinking", "not_reported_by_runtime")
            data["runtime_error"] = f"{type(exc).__name__}: {exc}"
            outcome = "runtime_error"
        attempts.append({"outcome": outcome, "cost_pi_usd": data.get("cost_pi_usd"),
                         "cost_judge_usd": data.get("cost_judge_usd"), "latency_s": data.get("latency_s"),
                         "max_event_gap_s": data.get("max_event_gap_s"),
                         "runtime_events": list(data.get("runtime_events", []))})
        final_data = data
        if outcome != "stall" or attempt_index >= retry_limit:
            break
    data = dict(final_data or {})
    if stalled_count:
        data["attempts"] = attempts
        data["stall_retries_used"] = min(stalled_count, retry_limit)
        data["runtime_events"] = [event for attempt in attempts for event in attempt["runtime_events"]]
        gaps = [attempt["max_event_gap_s"] for attempt in attempts
                if isinstance(attempt.get("max_event_gap_s"), (int, float))]
        data["max_event_gap_s"] = max(gaps) if gaps else None
        for key in ("cost_pi_usd", "cost_judge_usd"):
            costs = [attempt[key] for attempt in attempts]
            data[key] = None if any(value is None for value in costs) else sum(float(value) for value in costs)
        latencies = [attempt.get("latency_s") for attempt in attempts]
        if all(isinstance(value, (int, float)) for value in latencies):
            data["latency_s"] = sum(latencies)
        if stalled_count == len(attempts) and stalled_count > 1:
            data["runtime_error"] = "PiStallError after retry"
        elif stalled_count == len(attempts):
            data["runtime_error"] = "PiStallError"
        else:
            data["runtime_error"] = final_data.get("runtime_error")
        if data.get("cost_pi_usd") is None:
            data["cost_pi_status"] = "unknown_run_failed" if data.get("runtime_error") else "unknown"
    data.setdefault("max_event_gap_s", None)
    data.setdefault("stall_retries_used", 0)
    row = {**data, "question_id": item["question_id"], "split": item["split"],
           "question_type": item["question_type"]}
    row["grades"] = grade_item(item, row)
    return row


def _failures(items: list[dict], rows: list[dict], agent: str = "fake") -> str:
    by_id = {r["question_id"]: r for r in rows}
    lines = ["# Failed items and manual spot-check hooks", "",
             "Headline `unsupported_claim_rate` and `citation_coverage` are pooled over claims (`n_claims`); `*_item_mean` reports secondary per-item means.", ""]
    for item in items:
        row = by_id[item["question_id"]]
        grades = row["grades"]
        if all(v in (True, None) for k, v in grades.items() if k in {"numeric_correct", "refusal_correct", "metadata_correct", "tool_selection_correct", "retrieval_hit_at_k"}) and not grades["error_categories"]:
            continue
        lines.extend([f"## {item['question_id']}", "", f"- Question: {item['question']}",
                      f"- Expected: `{json.dumps(item['expected_answer'], sort_keys=True)}`",
                      f"- Observed: `{json.dumps(row['tool_calls'], sort_keys=True)}`",
                      f"- Category: {', '.join(grades['error_categories']) or 'metric mismatch'}",
                      f"- Trace: {agent} run record; see results.jsonl", "- [ ] Manual spot-check", ""])
    return "\n".join(lines)
