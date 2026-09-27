import subprocess
import sys

import pytest

from app.agent.pi_args import ROOT, read_runtime_config
from evals.pi_runner import PiModelMismatchError, PiRunError, PiStallError, ProviderAccountError, run_one


STUB = ROOT / "tests/fixtures/eval/fake_pi_rpc.py"
STUB_CMD = (sys.executable, str(STUB))


def test_run_one_stub_happy_path_uses_last_agent_end_and_keeps_accounting():
    result = run_one("happy", pi_bin=STUB_CMD, timeout_s=5)
    assert result["final_assistant_text"] == "Final answer only."
    assert [call["name"] for call in result["tool_calls"]] == ["calculate_financial_metric", "verify_citations"]
    assert all(call["ok"] for call in result["tool_calls"])
    assert result["verify_outputs"][0]["judge_observations"][0]["cost_usd"] == 0.02
    assert result["cost_judge_usd"] == pytest.approx(0.02)
    assert result["cost_pi_usd"] == pytest.approx(0.123)
    assert result["tokens_pi_in"] == 100 and result["tokens_pi_out"] == 50
    assert result["observed_agent_model"] == "anthropic/claude-sonnet-5"
    assert result["observed_agent_thinking"] == "medium"


def test_run_one_records_provider_retry_events_and_summary_count():
    result = run_one("auto-retry", pi_bin=STUB_CMD, timeout_s=5)
    assert result["final_assistant_text"] == "Final answer only."
    assert [event["type"] for event in result["runtime_events"]] == ["auto_retry_start", "auto_retry_end"]
    assert result["runtime_events"][0]["attempt"] == 1
    assert result["runtime_events"][0]["errorMessage"] == "529 overloaded"
    assert result["runtime_events"][1]["success"] is True
    assert all("elapsed_s" in event for event in result["runtime_events"])

    from evals.graders import summarize
    summary = summarize([
        {"split": "dev", "question_type": "narrative", "runtime_events": result["runtime_events"], "grades": {}},
        {"split": "dev", "question_type": "narrative", "runtime_events": [], "grades": {}},
    ], agent="pi")
    assert summary["overall"]["items_with_provider_retries"] == 1


def test_run_one_preserves_typed_tool_error_and_grades_refusal():
    result = run_one("typed-error", pi_bin=STUB_CMD, timeout_s=5)
    call = result["tool_calls"][0]
    assert call["ok"] is False
    assert call["error_code"] == "fiscal_year_not_in_filing"
    assert call["error_text"].startswith("fiscal_year_not_in_filing:")
    assert len(call["error_text"]) <= 2000

    import json
    from evals.graders import grade_item
    dataset = [json.loads(line) for line in (ROOT / "evals/datasets/questions.v1.jsonl").read_text().splitlines()]
    item = next(item for item in dataset if item["question_id"] == "fail-unavailable-year")
    assert grade_item(item, result)["refusal_correct"] is True


def test_run_one_matches_parallel_metric_results_by_tool_call_id():
    result = run_one("parallel-metric", pi_bin=STUB_CMD, timeout_s=5)
    calls = result["tool_calls"]
    assert [call["tool_call_id"] for call in calls] == ["metric-2024", "metric-2025"]
    assert [call["args"]["fiscal_year"] for call in calls] == [2024, 2025]
    assert calls[0]["ok"] is True and calls[0]["result_summary"] == {"result": "0.31"}
    assert calls[1]["ok"] is False and calls[1]["error_code"] == "fiscal_year_not_in_filing"


def test_run_one_routes_parallel_search_and_verification_outputs_by_id():
    result = run_one("parallel-search-verify", pi_bin=STUB_CMD, timeout_s=5)
    assert [call["tool_call_id"] for call in result["tool_calls"]] == ["search-1", "verify-1"]
    assert result["search_evidence"] == [{"evidence_id": "ev-search", "rank": 1}]
    assert result["verify_outputs"][0]["judge_observations"][0]["cost_usd"] == 0.02
    assert result["tool_calls"][0]["ok"] is True and result["tool_calls"][1]["ok"] is True


def test_run_one_marks_unended_tool_and_preserves_orphan_results():
    result = run_one("missing-tool-end", pi_bin=STUB_CMD, timeout_s=5)
    call = result["tool_calls"][0]
    assert call["tool_call_id"] == "never-ended"
    assert call["ok"] is None and call["error_code"] == "no_result"
    assert result["orphan_tool_results"][0]["tool_call_id"] == "unmatched-result"


def test_run_one_stderr_over_200kb_does_not_block_and_failure_tail_is_separate():
    result = run_one("stderr-flood", pi_bin=STUB_CMD, timeout_s=5)
    assert result["final_assistant_text"] == "Final answer only."
    with pytest.raises(PiRunError, match="stub final failure") as caught:
        run_one("stderr-failure", pi_bin=STUB_CMD, timeout_s=5)
    partial = caught.value.partial
    assert len(partial["pi_stderr_tail"]) <= 2000
    assert "STDERR-END-MARKER" in partial["pi_stderr_tail"]
    assert partial["runtime_error"] == "PiRunError: stub final failure"
    assert "STDERR-END-MARKER" not in partial["runtime_error"]


def test_run_one_stub_answers_extension_confirm_and_records_dialog():
    result = run_one("dialog", pi_bin=STUB_CMD, timeout_s=5)
    assert result["ui_requests"] == [{"method": method, "title": f"{method} dialog"}
                                      for method in ("confirm", "select", "input", "editor")]


def test_run_one_final_error_has_partial_tool_verify_and_cost_data():
    with pytest.raises(PiRunError, match="stub final failure") as caught:
        run_one("final-error", pi_bin=STUB_CMD, timeout_s=5)
    partial = caught.value.partial
    assert [call["name"] for call in partial["tool_calls"]] == ["calculate_financial_metric", "verify_citations"]
    assert partial["verify_outputs"]
    assert partial["cost_judge_usd"] == pytest.approx(0.02)
    assert partial["cost_pi_usd"] == pytest.approx(0.123)
    assert partial["latency_s"] is not None


def test_run_one_stub_model_mismatch_still_aborts():
    with pytest.raises(PiModelMismatchError, match="configured/observed agent mismatch"):
        run_one("mismatch", pi_bin=STUB_CMD, timeout_s=5)


def _batch_test_item(question_id, question):
    return {"question_id": question_id, "question_type": "failure", "split": "dev", "question": question,
            "expected_tools": [], "expected_answer": {"kind": "refusal", "refusal_evidence": {"tool_optional": True}}}


def test_provider_account_error_aborts_batch_and_writes_not_measurement(tmp_path):
    from evals.runner import _run_pi_item_batch

    item = _batch_test_item("provider-failure", "provider-error")
    with pytest.raises(ProviderAccountError, match="extra usage") as caught:
        _run_pi_item_batch([item, _batch_test_item("never-started", "happy")], read_runtime_config(),
                           "http://127.0.0.1:8765", tmp_path, pi_bin=STUB_CMD)
    aborted = (tmp_path / "ABORTED.md").read_text()
    assert "NOT A MEASUREMENT" in aborted
    assert "Reason: provider_account_failure" in aborted
    assert "400 invalid_request_error" in aborted
    assert "req_stub_123" in aborted
    assert "Items attempted (1): provider-failure" in aborted
    assert caught.value.request_ids == ["req_stub_123"]
    assert not (tmp_path / "summary.json").exists()


def test_zero_tool_runtime_errors_trip_circuit_breaker(tmp_path):
    from evals.runner import _run_pi_item_batch

    items = [_batch_test_item(f"zero-{index}", "zero-tool-error") for index in range(3)]
    with pytest.raises(RuntimeError, match="circuit_breaker"):
        _run_pi_item_batch(items, read_runtime_config(), "http://127.0.0.1:8765", tmp_path, pi_bin=STUB_CMD)
    aborted = (tmp_path / "ABORTED.md").read_text()
    assert "Reason: circuit_breaker" in aborted
    assert "Items attempted (3): zero-0, zero-1, zero-2" in aborted
    assert "NOT A MEASUREMENT" in aborted
    assert not (tmp_path / "summary.json").exists()


def test_ordinary_typed_tool_failure_is_graded_without_aborting(tmp_path):
    from evals.runner import _run_pi_item_batch

    item = _batch_test_item("tool-failure", "typed-error")
    rows = _run_pi_item_batch([item], read_runtime_config(), "http://127.0.0.1:8765", tmp_path, pi_bin=STUB_CMD)
    assert len(rows) == 1 and rows[0]["runtime_error"] is None
    assert rows[0]["tool_calls"][0]["error_code"] == "fiscal_year_not_in_filing"
    assert rows[0]["grades"]["refusal_correct"] is True
    assert not (tmp_path / "ABORTED.md").exists()


def test_run_one_timeout_preserves_data_collects_stats_and_terminates_stub(monkeypatch):
    spawned = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr("evals.pi_runner.subprocess.Popen", capture)
    with pytest.raises(PiRunError, match="timed out") as caught:
        run_one("hang", pi_bin=STUB_CMD, timeout_s=0.5)
    partial = caught.value.partial
    assert partial["tool_calls"] and partial["cost_pi_usd"] == pytest.approx(0.123)
    assert partial["cost_pi_status"] == "available"
    assert spawned and spawned[0].poll() is not None
    assert partial["last_events"]
    assert len(partial["last_events"]) <= 30
    assert all(set(event).issubset({"type", "toolName", "elapsed_s"}) for event in partial["last_events"])
    assert all("elapsed_s" in event for event in partial["last_events"])


def test_run_one_idle_stall_aborts_collects_stats_and_terminates_stub(monkeypatch):
    spawned = []
    real_popen = subprocess.Popen
    def capture(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc
    monkeypatch.setattr("evals.pi_runner.subprocess.Popen", capture)
    with pytest.raises(PiStallError) as caught:
        run_one("stall", pi_bin=STUB_CMD, timeout_s=5, stall_idle_s=.15,
                stall_abort_wait_s=.5, stall_stats_wait_s=.5)
    partial = caught.value.partial
    assert partial["cost_pi_usd"] == pytest.approx(.123)
    assert partial["runtime_events"][-1]["type"] == "stall"
    assert partial["runtime_events"][-1]["last_event_type"] == "message_start"
    assert "ABORT-RECEIVED" in partial["pi_stderr_tail"]
    assert "STATS-REQUESTED" in partial["pi_stderr_tail"]
    assert spawned and spawned[0].poll() is not None


def test_run_one_ignores_abort_but_still_terminates_within_bounded_waits():
    started = __import__("time").monotonic()
    with pytest.raises(PiStallError):
        run_one("ignore-abort", pi_bin=STUB_CMD, timeout_s=5, stall_idle_s=.1,
                stall_abort_wait_s=.15, stall_stats_wait_s=.5)
    assert __import__("time").monotonic() - started < 2


def test_normal_stub_records_event_gap_below_stall_threshold():
    result = run_one("happy", pi_bin=STUB_CMD, timeout_s=5, stall_idle_s=1)
    assert isinstance(result["max_event_gap_s"], float)
    assert 0 <= result["max_event_gap_s"] < 1


def test_runner_retries_stall_once_and_sums_attempt_costs(monkeypatch):
    from evals import runner
    partial = {"tool_calls": [], "verify_outputs": [], "final_assistant_text": "", "latency_s": 1.0,
               "cost_pi_usd": .12, "cost_judge_usd": .03, "max_event_gap_s": 1.0,
               "runtime_events": [{"type": "stall"}], "observed_agent_model": "test"}
    success = {"tool_calls": [], "verify_outputs": [], "final_assistant_text": "Revenue margin 100%.",
               "latency_s": 2.0, "cost_pi_usd": .2, "cost_judge_usd": .04, "max_event_gap_s": .2,
               "runtime_events": [], "observed_agent_model": "test"}
    calls = iter([PiStallError("first stall", partial=partial), success])
    monkeypatch.setattr("evals.pi_runner.run_one", lambda *a, **k: (_raise(next(calls))))
    item = {"question_id": "retry", "question": "numeric", "question_type": "numeric_lookup", "split": "dev",
            "expected_tools": [], "expected_answer": {"kind": "numeric", "value": "1", "unit": "ratio",
            "tolerance": {"absolute": "0.0005"}}}
    row = runner._run_pi_item(item, {"eval_timeout_s": 5, "eval_max_tool_calls": 30,
                                    "eval_stall_idle_s": .1, "eval_stall_retries": 1}, "http://localhost")
    assert row["grades"]["numeric_correct"] is True
    assert row["stall_retries_used"] == 1
    assert len(row["attempts"]) == 2 and [a["outcome"] for a in row["attempts"]] == ["stall", "success"]
    assert row["cost_pi_usd"] == pytest.approx(.32)
    assert row["cost_judge_usd"] == pytest.approx(.07)
    assert row["runtime_error"] is None


def test_runner_reports_both_stalls_with_summed_costs(monkeypatch):
    from evals import runner
    partials = [
        {"tool_calls": [], "verify_outputs": [], "final_assistant_text": "", "latency_s": 1,
         "cost_pi_usd": .1, "cost_judge_usd": .01, "max_event_gap_s": 1, "runtime_events": [{"type": "stall"}]},
        {"tool_calls": [], "verify_outputs": [], "final_assistant_text": "", "latency_s": 2,
         "cost_pi_usd": .2, "cost_judge_usd": .02, "max_event_gap_s": 2, "runtime_events": [{"type": "stall"}]},
    ]
    calls = iter(partials)
    def stall(*args, **kwargs):
        raise PiStallError("stall", partial=next(calls))
    monkeypatch.setattr("evals.pi_runner.run_one", stall)
    item = {"question_id": "double-stall", "question": "q", "question_type": "narrative", "split": "dev",
            "expected_tools": [], "expected_answer": {"kind": "narrative"}}
    row = runner._run_pi_item(item, {"eval_timeout_s": 5, "eval_max_tool_calls": 30,
                                    "eval_stall_idle_s": .1, "eval_stall_retries": 1}, "http://localhost")
    assert row["runtime_error"] == "PiStallError after retry"
    assert row["stall_retries_used"] == 1 and len(row["attempts"]) == 2
    assert row["cost_pi_usd"] == pytest.approx(.3) and row["cost_judge_usd"] == pytest.approx(.03)
    summary = __import__("evals.graders", fromlist=["summarize"]).summarize([row], "pi")
    assert summary["overall"]["items_with_stall_retry"] == 1
    assert summary["overall"]["items_failed_by_stall"] == 1


def _raise(value):
    if isinstance(value, BaseException):
        raise value
    return value


def test_run_one_tool_cap_keeps_partial_cost_and_unknown_cost_is_marked():
    with pytest.raises(PiRunError, match="max tool-call cap") as caught:
        run_one("happy", pi_bin=STUB_CMD, timeout_s=5, max_tool_calls=1)
    partial = caught.value.partial
    assert len(partial["tool_calls"]) == 1
    assert partial["cost_pi_usd"] == pytest.approx(0.123)

    with pytest.raises(PiRunError, match="timed out") as unknown:
        run_one("no-stats", pi_bin=STUB_CMD, timeout_s=0.25)
    assert unknown.value.partial["cost_pi_usd"] is None
    assert unknown.value.partial["cost_pi_status"] == "unknown_run_failed"


def test_runner_item_path_merges_stub_partial_failure_data():
    from evals.runner import _run_pi_item

    item = {"question_id": "stub", "question_type": "narrative", "split": "dev", "question": "final-error",
            "expected_tools": [], "expected_answer": {"kind": "narrative"}}
    row = _run_pi_item(item, read_runtime_config(), "http://127.0.0.1:8765", pi_bin=STUB_CMD)
    assert row["runtime_error"] == "PiRunError: stub final failure"
    assert row["tool_calls"] and row["verify_outputs"]
    assert row["cost_pi_usd"] == pytest.approx(0.123)
    assert row["grades"]["runtime_error"] == row["runtime_error"]
    from evals.graders import summarize
    summary = summarize([row], agent="pi")
    assert summary["overall"]["total_cost_usd"] == pytest.approx(0.143)
