"""One fresh, isolated Pi RPC process per question. Never imported by fake runs."""
from __future__ import annotations

import json
import os
import re
import select
import subprocess
import tempfile
import time
from pathlib import Path
from app.agent.pi_args import ROOT, build_pi_argv, read_runtime_config


class PiRunError(RuntimeError):
    def __init__(self, message: str, *, partial: dict | None = None):
        super().__init__(message)
        self.partial = partial or {}


class PiModelMismatchError(PiRunError):
    pass


class PiStallError(PiRunError):
    """Raised when Pi emits no JSONL events for the configured idle window."""


class ProviderAccountError(PiRunError):
    def __init__(self, message: str, *, request_ids: list[str] | None = None, partial: dict | None = None):
        super().__init__(message, partial=partial)
        self.request_ids = request_ids or []


def _provider_account_failure(message: str, *, retries_exhausted: bool = False) -> bool:
    text = message.casefold()
    return bool(
        re.search(r"\b(?:http\s*)?(?:401|403)\b", text)
        or re.search(r"\b400\b.{0,160}\binvalid_request_error\b", text)
        or any(term in text for term in ("extra usage", "credit", "billing", "quota", "authentication"))
        or ("rate_limit" in text and retries_exhausted)
    )


def _provider_request_ids(message: str, event: dict) -> list[str]:
    ids = set(re.findall(r"\breq_[A-Za-z0-9_-]+\b", message))
    for key in ("requestId", "request_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            ids.add(value)
    return sorted(ids)


def build_pi_child_env(parent_env: dict[str, str], api_url: str) -> dict[str, str]:
    child_env = {key: parent_env[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "LANG", "LC_ALL") if key in parent_env}
    child_env["FRA_API_URL"] = api_url
    return child_env


def run_one(question: str, *, timeout_s: int = 180, max_tool_calls: int = 30,
            pi_bin: str | list[str] | tuple[str, ...] | None = None, api_url: str = "http://127.0.0.1:8765",
            stall_idle_s: float | None = None, stall_abort_wait_s: float = 15,
            stall_stats_wait_s: float = 10) -> dict:
    config = read_runtime_config()
    stall_idle_s = float(config.get("eval_stall_idle_s", 120) if stall_idle_s is None else stall_idle_s)
    with tempfile.TemporaryDirectory(prefix="eval-pi-session-") as session:
        command = ([pi_bin] if isinstance(pi_bin, str) else list(pi_bin)) if pi_bin else [os.environ.get("PI_BIN", "pi")]
        argv = [*command, *build_pi_argv(session, config)]
        child_env = build_pi_child_env(os.environ, api_url)
        stderr_path = Path(session) / "pi.stderr.log"
        stderr_file = stderr_path.open("wb", buffering=0)
        try:
            proc = subprocess.Popen(argv, cwd=ROOT, env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr_file, bufsize=0)
        except Exception:
            stderr_file.close()
            raise
        start = time.monotonic()
        deadline = start + timeout_s
        events: list[dict] = []
        event_diagnostics: list[dict] = []
        runtime_events: list[dict] = []
        tool_calls: list[dict] = []
        tool_calls_by_id: dict[str, dict] = {}
        orphan_tool_results: list[dict] = []
        verify_outputs: list[dict] = []
        search_evidence: list[dict] = []
        ui_requests: list[dict] = []
        top_k_used = 5
        metadata: dict = {}
        assistant_text = ""
        last_assistant_error = False
        model_state = None
        stats = None
        sequence = 0
        salvage_mode = False
        dialog_methods = {"confirm", "select", "input", "editor"}
        stdout_buffer = bytearray()
        prompt_active = False
        last_event_at: float | None = None
        last_event_type: str | None = None
        max_event_gap_s = 0.0

        def send(message: dict) -> None:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def next_line(read_deadline: float, label: str) -> bytes:
            while True:
                newline = stdout_buffer.find(b"\n")
                if newline >= 0:
                    line = bytes(stdout_buffer[:newline])
                    del stdout_buffer[:newline + 1]
                    return line
                if proc.poll() is not None:
                    raise PiRunError(f"Pi exited before {label} (exit={proc.returncode})")
                now = time.monotonic()
                remaining = read_deadline - now
                if remaining <= 0:
                    raise PiRunError(f"timed out waiting for {label}")
                idle_remaining = None
                if prompt_active and last_event_at is not None:
                    idle_remaining = last_event_at + stall_idle_s - now
                    if idle_remaining <= 0:
                        raise _IdleStall(elapsed_s=now - start, idle_s=now - last_event_at,
                                         last_event_type=last_event_type)
                wait_s = min(0.25, remaining, idle_remaining) if idle_remaining is not None else min(0.25, remaining)
                ready, _, _ = select.select([proc.stdout], [], [], max(.001, wait_s))
                if not ready:
                    continue
                chunk = os.read(proc.stdout.fileno(), 65536)
                if chunk:
                    stdout_buffer.extend(chunk)
            raise PiRunError(f"timed out waiting for {label}")

        def judge_observations() -> list[dict]:
            return [observation for output in verify_outputs
                    for observation in output.get("judge_observations", [])]

        def stop_process() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        def stderr_tail() -> str:
            try:
                return stderr_path.read_bytes().decode("utf-8", errors="replace")[-2000:]
            except OSError:
                return ""

        def last_events() -> list[dict]:
            return list(event_diagnostics[-30:])

        def record(stats_value=None, *, runtime_error=None) -> dict:
            observations = judge_observations()
            judge_cost = sum(float(x.get("cost_usd") or 0) for x in observations)
            judge_tokens_in = sum(int(x.get("prompt_tokens") or 0) for x in observations)
            judge_tokens_out = sum(int(x.get("completion_tokens") or 0) for x in observations)
            tokens = (stats_value or {}).get("tokens", {})
            pi_tokens_in, pi_tokens_out = tokens.get("input"), tokens.get("output")
            pi_cost = (stats_value or {}).get("cost")
            result = {"tool_calls": list(tool_calls), "orphan_tool_results": list(orphan_tool_results),
                    "verify_outputs": list(verify_outputs),
                    "final_assistant_text": assistant_text, "latency_s": time.monotonic() - start,
                    "tokens_in": (pi_tokens_in or 0) + judge_tokens_in,
                    "tokens_out": (pi_tokens_out or 0) + judge_tokens_out,
                    "tokens_pi_in": pi_tokens_in, "tokens_pi_out": pi_tokens_out,
                    "tokens_judge_in": judge_tokens_in, "tokens_judge_out": judge_tokens_out,
                    "cost_pi_usd": pi_cost, "cost_pi_status": ("available" if pi_cost is not None else
                                                                    ("unknown_run_failed" if runtime_error else "unknown")),
                    "cost_judge_usd": judge_cost, "search_evidence": list(search_evidence),
                    "top_k": top_k_used, "metadata": dict(metadata), "ui_requests": list(ui_requests),
                    "observed_agent_model": (f"{model_state.get('model', {}).get('provider')}/{model_state.get('model', {}).get('id')}"
                                              if isinstance(model_state, dict) else "not_reported_by_runtime"),
                    "observed_agent_thinking": model_state.get("thinkingLevel") if isinstance(model_state, dict) else "not_reported_by_runtime",
                    "judge_observations": observations, "runtime_error": runtime_error,
                    "runtime_events": list(runtime_events), "max_event_gap_s": round(max_event_gap_s, 6)}
            if runtime_error:
                result["last_events"] = last_events()
            return result

        def receive(predicate, label, *, wait_s: float | None = None):
            nonlocal top_k_used, assistant_text, last_assistant_error, last_event_at, last_event_type, max_event_gap_s, prompt_active
            read_deadline = min(deadline, time.monotonic() + wait_s) if wait_s is not None else deadline
            if wait_s is not None:
                read_deadline = time.monotonic() + wait_s
            while time.monotonic() < read_deadline:
                line = next_line(read_deadline, label)
                now = time.monotonic()
                if prompt_active:
                    if last_event_at is not None:
                        max_event_gap_s = max(max_event_gap_s, now - last_event_at)
                    last_event_at = now
                try:
                    event = json.loads(line.decode("utf-8"))
                except ValueError:
                    if prompt_active:
                        last_event_type = None
                    continue
                events.append(event)
                event_type = event.get("type")
                if prompt_active:
                    last_event_type = event_type
                elapsed = time.monotonic() - start
                diagnostic = {"type": event_type}
                if event.get("toolName") is not None:
                    diagnostic["toolName"] = event["toolName"]
                diagnostic["elapsed_s"] = elapsed
                event_diagnostics.append(diagnostic)
                if (isinstance(event_type, str) and
                        (event_type.startswith("auto_retry_") or event_type.startswith("compaction_") or
                         event_type.startswith("summarization_retry_"))):
                    runtime_event = {"type": event_type}
                    for key in ("attempt", "delayMs", "errorMessage", "success", "finalError", "reason"):
                        if key in event:
                            runtime_event[key] = event[key]
                    runtime_event["elapsed_s"] = elapsed
                    runtime_events.append(runtime_event)

                if event_type == "extension_ui_request":
                    method = event.get("method")
                    if method in dialog_methods:
                        args = event.get("arguments") or event.get("params") or {}
                        ui_requests.append({"method": method, "title": event.get("title") or args.get("title")})
                        response = {"type": "extension_ui_response", "id": event.get("id")}
                        response.update({"confirmed": False} if method == "confirm" else {"cancelled": True})
                        send(response)
                elif salvage_mode and event_type not in {"response", "extension_ui_request"}:
                    # Once a failure occurred, stop accumulating new work while asking
                    # Pi for final session stats; still service dialogs and the response.
                    continue
                elif event_type == "tool_execution_start":
                    if len(tool_calls) >= max_tool_calls:
                        raise PiRunError(f"max tool-call cap exceeded ({max_tool_calls})")
                    tool_call_id = event.get("toolCallId")
                    call = {"tool_call_id": tool_call_id, "name": event.get("toolName", ""),
                            "args": event.get("args", {}), "ok": None,
                            "error_code": "no_result", "result_summary": {}}
                    tool_calls.append(call)
                    if tool_call_id is not None:
                        tool_calls_by_id[str(tool_call_id)] = call
                    if event.get("toolName") == "search_filing_chunks":
                        top_k_used = event.get("args", {}).get("top_k", 5)
                elif event_type == "tool_execution_end":
                    raw = event.get("result") or {}
                    details = raw.get("details", raw)
                    if isinstance(details, str):
                        try:
                            details = json.loads(details)
                        except ValueError:
                            details = {"text": details}
                    tool_call_id = event.get("toolCallId")
                    call = tool_calls_by_id.get(str(tool_call_id)) if tool_call_id is not None else None
                    if call is None:
                        orphan_tool_results.append({"tool_call_id": tool_call_id, "event": event})
                        if predicate(event):
                            return event
                        continue
                    is_error = bool(raw.get("isError") or event.get("isError"))
                    call["ok"] = not is_error
                    detail_error = details.get("error", {}) if isinstance(details, dict) else {}
                    call["error_code"] = detail_error.get("code") if isinstance(detail_error, dict) else None
                    call["result_summary"] = details if isinstance(details, dict) else {"text": str(details)}
                    if is_error:
                        content = raw.get("content", [])
                        error_text = "\n".join(part.get("text", "") for part in content
                                               if isinstance(part, dict) and part.get("type") == "text")[:2000]
                        match = re.match(r"^([a-z][a-z0-9_]*):", error_text)
                        call["error_code"] = call["error_code"] or (match.group(1) if match else "unknown_tool_error")
                        call["error_text"] = error_text
                    if call["name"] == "verify_citations" and call["ok"]:
                        verify_outputs.append(details)
                    if call["name"] == "search_filing_chunks" and call["ok"]:
                        search_evidence.extend({"evidence_id": row.get("evidence_id"), "rank": i + 1}
                                               for i, row in enumerate(details.get("evidence", [])))
                    if call["name"] == "list_filings" and call["ok"] and details.get("filings"):
                        filing = details["filings"][0]
                        metadata.update({"accession_number": filing.get("accession_number"),
                                         "filing_date": filing.get("filing_date"), "report_date": filing.get("report_date"),
                                         "form": filing.get("form"), "ticker": details.get("company", {}).get("ticker"),
                                         "cik": details.get("company", {}).get("cik")})
                elif event_type == "agent_end":
                    assistant_messages = [m for m in event.get("messages", []) if m.get("role") == "assistant"]
                    last_message = assistant_messages[-1] if assistant_messages else {}
                    assistant_text = "\n".join(part.get("text", "") for part in last_message.get("content", [])
                                                if part.get("type") == "text")
                    last_assistant_error = last_message.get("stopReason") == "error"
                    if last_assistant_error and event.get("willRetry") is False:
                        message = last_message.get("errorMessage", "assistant runtime error")
                        retries_exhausted = any(
                            retry.get("type") == "auto_retry_end" and retry.get("success") is False
                            for retry in runtime_events
                        )
                        if _provider_account_failure(message, retries_exhausted=retries_exhausted):
                            raise ProviderAccountError(message, request_ids=_provider_request_ids(message, event))
                        raise PiRunError(message)
                elif event_type == "agent_settled" and last_assistant_error:
                    raise PiRunError("assistant runtime error after final agent_end")

                if predicate(event):
                    if event_type == "agent_settled":
                        prompt_active = False
                    return event
            raise PiRunError(f"timed out waiting for {label}")

        def request(kind: str, data: dict | None = None, *, wait_s: float | None = None) -> dict:
            nonlocal sequence
            sequence += 1
            request_id = f"eval-{kind}-{sequence}"
            send({"id": request_id, "type": kind, **(data or {})})
            return receive(lambda e: e.get("type") == "response" and e.get("id") == request_id,
                           kind, wait_s=wait_s)

        try:
            prompt_active = True
            last_event_at = time.monotonic()
            send({"id": "eval-prompt", "type": "prompt", "message": question})
            receive(lambda e: e.get("type") == "agent_settled", "agent_settled")
            model_state = request("get_state").get("data")
            stats = request("get_session_stats").get("data")
            model = model_state.get("model", {}) if isinstance(model_state, dict) else {}
            observed = f"{model.get('provider')}/{model.get('id')}"
            expected = f"{config['provider']}/{config['model_id']}"
            if observed != expected or model_state.get("thinkingLevel") != config["thinking_level"]:
                raise PiModelMismatchError(f"configured/observed agent mismatch: configured {expected} {config['thinking_level']}; observed {observed} {model_state.get('thinkingLevel')}")
            return record(stats_value=stats, runtime_error=None)
        except _IdleStall as exc:
            prompt_active = False
            salvage_mode = True
            runtime_events.append({"type": "stall", "elapsed_s": round(exc.elapsed_s, 6),
                                   "idle_s": round(exc.idle_s, 6), "last_event_type": exc.last_event_type})
            if proc.poll() is None:
                try:
                    request("abort", wait_s=stall_abort_wait_s)
                except Exception:
                    pass
                try:
                    stats = request("get_session_stats", wait_s=stall_stats_wait_s).get("data")
                except Exception:
                    stats = None
            stop_process()
            message = f"Pi idle stall after {exc.idle_s:.3f}s without an event"
            partial = record(stats_value=stats, runtime_error=f"PiStallError: {message}")
            partial["pi_stderr_tail"] = stderr_tail()
            if partial["cost_pi_usd"] is None:
                partial["cost_pi_status"] = "unknown_run_failed"
            raise PiStallError(message, partial=partial) from None
        except PiModelMismatchError as exc:
            stop_process()
            partial = record(stats_value=stats, runtime_error=f"{type(exc).__name__}: {exc}")
            partial["pi_stderr_tail"] = stderr_tail()
            exc.partial = partial
            raise
        except Exception as exc:
            prompt_active = False
            salvage_mode = True
            if proc.poll() is None:
                try:
                    stats = request("get_session_stats", wait_s=min(5, max(0.1, timeout_s))).get("data")
                except Exception:
                    stats = None
            stop_process()
            partial = record(stats_value=stats, runtime_error=f"{type(exc).__name__}: {exc}")
            partial["pi_stderr_tail"] = stderr_tail()
            if partial["cost_pi_usd"] is None:
                partial["cost_pi_status"] = "unknown_run_failed"
            if isinstance(exc, PiRunError):
                exc.partial = partial
                raise
            raise PiRunError(f"{type(exc).__name__}: {exc}", partial=partial) from exc
        finally:
            if proc.poll() is None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            stop_process()
            stderr_file.close()


class _IdleStall(Exception):
    def __init__(self, *, elapsed_s: float, idle_s: float, last_event_type: str | None):
        super().__init__("idle stall")
        self.elapsed_s, self.idle_s, self.last_event_type = elapsed_s, idle_s, last_event_type
