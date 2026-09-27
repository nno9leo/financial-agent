import hashlib
import json
from decimal import Decimal
from pathlib import Path

from evals.build_dataset import build
from evals.generate_numeric import generate
from evals.graders import grade_item, summarize

ROOT = Path(__file__).resolve().parents[2]
from evals.runner import _run_pi_items as _RUN_PI_ITEMS_IMPL


def test_numeric_generator_has_18_text_gold_items_and_is_deterministic():
    gt = json.loads((ROOT / "evals/ground_truth/statement_ground_truth.json").read_text())
    manifest = json.loads((ROOT / "data/manifests/filings.v1.json").read_text())
    one = generate(gt, manifest)
    assert len(one) == 18
    assert one == generate(gt, manifest)
    assert sum(x["question_type"] == "numeric_failure" for x in one) == 2
    for item in one:
        value = gt[item["expected_answer"]["ground_truth_key"]]
        assert item["expected_answer"].get("value") == (None if value == "ERROR" else value)


def test_calculator_crosscheck_agrees_with_all_16_generated_non_error_gold_values():
    from app.compute.metrics import Metric
    from app.ingestion.manifest import load_manifest
    from app.tools.calculate_financial_metric import calculate_financial_metric, clear_normalized_companyfacts_cache
    from tests.unit.offline_fixture_data import companyfacts_fixture, OfflineCompanyFactsProvider
    gt = json.loads((ROOT / "evals/ground_truth/statement_ground_truth.json").read_text())
    manifest, bodies = companyfacts_fixture()
    items = generate(gt, json.loads((ROOT / "data/manifests/filings.v1.json").read_text()))
    provider = OfflineCompanyFactsProvider(bodies)
    clear_normalized_companyfacts_cache()
    try:
        checked = 0
        for item in items:
            answer = item["expected_answer"]
            if answer["kind"] != "numeric":
                continue
            output = calculate_financial_metric({"metric": answer["metric"], "company": item["ticker"],
                "fiscal_year": int(answer["ground_truth_key"].split("|")[1])}, provider=provider, manifest=manifest)
            assert Decimal(str(output.result.result)) == Decimal(answer["value"])
            checked += 1
        assert checked == 16
    finally:
        provider.close()
        clear_normalized_companyfacts_cache()


def test_dataset_build_is_35_frozen_and_all_gold_evidence_resolves():
    items, manifest = build(chunks_dir=ROOT / "tests/fixtures/eval/gold_chunks")
    assert len(items) == 35
    assert manifest["external_items"] == 0
    assert sum(manifest["counts_by_split"].values()) == 35
    assert manifest["holdout_policy"] == "Never used for tuning."
    expected = "".join(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for x in items)
    assert hashlib.sha256(expected.encode()).hexdigest() == manifest["sha256"]


def test_dataset_validation_rejects_bad_groundtruth_and_missing_evidence_ids():
    from evals.build_dataset import validate_gold_evidence, validate_ground_truth_keys
    gt = {"MSFT|2026|revenue": "331839000000"}
    with __import__("pytest").raises(ValueError, match="unknown ground truth key"):
        validate_ground_truth_keys({"question_id": "bad", "expected_answer": {"numeric": {
            "ground_truth_key": "MSFT|1900|revenue"}}}, gt)
    with __import__("pytest").raises(ValueError, match="missing gold evidence ID"):
        validate_gold_evidence({"question_id": "bad", "accession_number": "a1",
            "expected_evidence": {"evidence_ids": ["ev_missing"]}}, {})


def test_numeric_grader_requires_supported_claim_and_tolerance_edges():
    item = {"question_type": "numeric_lookup", "expected_tools": ["calculate_financial_metric"],
            "expected_answer": {"kind": "numeric", "value": "100000000", "unit": "USD",
                                "tolerance": {"relative": "0.0005"}}}
    unsupported = {"tool_calls": [{"name": "calculate_financial_metric", "ok": True}],
                   "verify_outputs": [{"report": {"verifications": [{"claim_text": "$100 million", "verdict": "unsupported"}]}}],
                   "final_assistant_text": "$100 million"}
    assert grade_item(item, unsupported)["numeric_correct"] is False
    supported = {**unsupported, "tool_calls": [{"name": "calculate_financial_metric", "ok": True},
        {"name": "verify_citations", "ok": True, "args": {"claims": [{"claim_id": "c", "numeric": {"value": "100.05", "scale": "millions", "unit": "USD"}}]}}],
        "verify_outputs": [{"report": {"verifications": [
        {"claim_id": "c", "claim_type": "numeric", "claim_text": "$100.05 million", "verdict": "supported", "evidence_ids": ["ev1"]}]}}]}
    assert grade_item(item, supported)["numeric_correct"] is True
    assert grade_item(item, supported)["tolerance_rule_applied"] == "usd_relative_0.05_percent"
    supported["tool_calls"][1]["args"]["claims"][0]["numeric"]["value"] = "100.051"
    supported["verify_outputs"][0]["report"]["verifications"][0]["claim_text"] = "$100.051 million"
    assert grade_item(item, supported)["numeric_correct"] is False
    current = {**item, "expected_evidence": {"period_end": "2026-06-30"}}
    wrong_period = {"tool_calls": [{"name": "calculate_financial_metric", "ok": True}],
                    "final_assistant_text": "Fiscal 2025 revenue was $100 million."}
    assert grade_item(current, wrong_period)["numeric_correct"] is False


def test_comparison_requires_both_calculator_periods_and_final_answer_values():
    item = {"question_type": "comparison", "expected_tools": ["compare_periods"],
            "expected_answer": {"kind": "comparison", "metric": "operating_margin", "values": [
                {"key": "AAPL|2024|operating_margin", "value": "0.315102", "unit": "ratio"},
                {"key": "AAPL|2025|operating_margin", "value": "0.319708", "unit": "ratio"}]}}
    call = {"name": "compare_periods", "ok": True, "result_summary": {"rows": [{
        "value_a": {"result": "0.315102"}, "value_b": {"result": "0.319708"}}]}}
    rec = {"tool_calls": [call], "final_assistant_text": "Fiscal 2024: 31.5%; fiscal 2025: 32.0%."}
    assert grade_item(item, rec)["numeric_correct"] is True
    rec["final_assistant_text"] = "Fiscal 2024: 31.5%."
    assert grade_item(item, rec)["numeric_correct"] is False


def test_refusal_numbers_tool_order_retrieval_and_final_retry_report():
    failure = {"question_type": "failure", "expected_tools": ["calculate_financial_metric"],
               "expected_answer": {"kind": "refusal", "refusal_evidence": {"any_error_codes": ["missing_fact"], "or_search_companies_unpinned": False, "tool_optional": False}}}
    record = {"tool_calls": [{"name": "calculate_financial_metric", "ok": False, "error_code": "missing_fact"}],
              "final_assistant_text": "I cannot provide the figure."}
    assert grade_item(failure, record)["refusal_correct"] is True
    record["final_assistant_text"] = "The answer is $5 million."
    assert grade_item(failure, record)["refusal_correct"] is False
    order_item = {"question_type": "mixed", "expected_tools": ["calculate_financial_metric", "verify_citations"],
                  "expected_tool_order": [["calculate_financial_metric", "verify_citations"]],
                  "expected_answer": {"kind": "mixed", "numeric": {"kind": "numeric", "value": "1", "unit": "ratio"}}}
    wrong_order = {"tool_calls": [{"name": "verify_citations", "ok": True}, {"name": "calculate_financial_metric", "ok": True}]}
    assert grade_item(order_item, wrong_order)["tool_selection_correct"] is False
    evidence_item = {"question_type": "narrative", "expected_tools": [], "expected_evidence": {"evidence_ids": ["e2"]},
                     "expected_answer": {"kind": "narrative", "key_points": ["azure"]}}
    retry = {"tool_calls": [{"name": "search_filing_chunks", "ok": True}], "search_evidence": [{"evidence_id": "e2", "rank": 2}],
             "top_k": 3, "verify_outputs": [{"report": {"verifications": [{"claim_text": "Azure", "verdict": "unsupported", "evidence_ids": ["e2"]}]}},
                                              {"report": {"verifications": [{"claim_text": "Azure", "verdict": "partially_supported", "evidence_ids": ["e2"]}]}}]}
    grade = grade_item(evidence_item, retry)
    assert grade["retry_used"] is True
    assert grade["unsupported_claim_rate"] == 0
    assert grade["retrieval_rank"] == 2
    assert grade["narrative_key_points_proxy"] == 1
    assert grade["citation_coverage"] == 0
    retry["search_evidence"] = [{"evidence_id": "wrong", "rank": 1}]
    assert grade_item(evidence_item, retry)["retrieval_hit_at_k"] is False
    retry["verify_outputs"][-1]["report"]["verifications"][0]["deterministic_checks"] = {
        "evidence_exists": True, "hash_matches": True, "provenance_consistent": True,
        "ticker_matches": True, "single_accession": True, "text_evidence_present": True}
    assert grade_item(evidence_item, retry)["citation_coverage"] == 1


def test_missing_calculator_and_aggregation_preserve_n_and_failed_rows():
    item = {"question_type": "numeric_lookup", "expected_tools": ["calculate_financial_metric"],
            "expected_answer": {"kind": "numeric", "value": "2", "unit": "ratio", "metric": "current_ratio", "tolerance": {"absolute": "0.0005"}}}
    row = {"question_id": "q", "question_type": "numeric_lookup", "split": "dev", "tool_calls": [],
           "final_assistant_text": "2x", "grades": grade_item(item, {"tool_calls": [], "final_assistant_text": "2x"})}
    assert row["grades"]["tool_selection_correct"] is False
    assert "no_calculator" in row["grades"]["error_categories"]
    summary = summarize([row], agent="fake")
    assert summary["agent"] == "fake"
    assert summary["overall"]["tool_selection_correct"] == {"mean": 0.0, "n": 1}
    assert summary["by_split"]["dev"]["items"] == 1


def test_fake_runner_end_to_end_writes_fake_artifacts(tmp_path):
    from evals.runner import run_eval
    from app.config import Settings
    from app.providers.sec import SecProvider
    from fastapi.testclient import TestClient
    from app.api.app import ApiDeps, create_app
    from app.citations.semantic import ScriptedJudge

    settings = Settings(_env_file=None)
    def client_factory():
        return TestClient(create_app(ApiDeps(settings=settings,
            sec_provider_factory=lambda: SecProvider(settings), judge_factory=lambda: ScriptedJudge({}),
            reports_dir=tmp_path / "reports")))
    out = run_eval(split="all", agent="fake", run_id="test-run", limit=3,
                   output_root=tmp_path / "run", client_factory=client_factory)
    required = {"config.json", "dataset_manifest.json", "environment.json", "results.jsonl",
                "summary.json", "failures.md", "README.md"}
    assert required.issubset({x.name for x in out.iterdir()})
    summary = json.loads((out / "summary.json").read_text())
    assert summary["agent"] == "fake"
    assert summary["grader_version"] == "v3"
    assert "PIPELINE TEST — NOT A MEASUREMENT" in summary["banner"]
    assert len((out / "results.jsonl").read_text().splitlines()) == 3
    environment = json.loads((out / "environment.json").read_text())
    # Fake runs don't need the embedding model; a fresh clone hasn't downloaded it (data/models is gitignored).
    from evals.runner import ROOT
    model_present = any((ROOT / "data/models").glob("models--Qdrant--bge-small-en-v1.5-onnx-Q/snapshots/*/model_optimized.onnx"))
    weights_sha = environment["embedding"]["weights_sha256"]
    assert (len(weights_sha) == 64) if model_present else (weights_sha is None)
    assert "secrets" not in environment or environment["secrets"] == "omitted"
    assert environment["judge"]["implementation"] == "ScriptedJudge"
    assert environment["judge"]["observed_models"] == ["scripted-fake/1"]
    config = json.loads((out / "config.json").read_text())
    assert config["max_verify_attempts"] == 2 and config["agent"] == "fake"
    assert config["derived_metric_render"] == "v2"
    assert config["stall_idle_s"] == 120 and config["stall_retries"] == 1
    progress = [json.loads(line) for line in (out / "progress.jsonl").read_text().splitlines()]
    assert len(progress) == 3
    assert all({"n", "total", "question_id", "elapsed_s", "latency_s", "cost_pi_usd",
                "cost_judge_usd", "cumulative_cost_usd", "runtime_error", "stall_retries_used"} <= row.keys() for row in progress)
    assert "extension_source_sha256" in config and "guidance_text_sha256" in config
    assert "prompt_guidance_sha256" not in config


def test_pi_model_mismatch_guard_is_fail_closed():
    from evals.runner import validate_observed_models
    cfg = json.loads((ROOT / "config/agent_runtime.json").read_text())
    try:
        validate_observed_models(cfg, "anthropic/wrong-model", cfg["semantic_judge"]["model_id"])
    except RuntimeError as exc:
        assert "agent model mismatch" in str(exc)
    else:
        raise AssertionError("model mismatch must stop the run")
    try:
        validate_observed_models(cfg, "anthropic/claude-sonnet-5", "wrong/judge")
    except RuntimeError as exc:
        assert "judge model mismatch" in str(exc)
    else:
        raise AssertionError("judge mismatch must stop the run")


def test_pi_dirty_tree_guard_runs_before_launch(tmp_path, monkeypatch):
    import pytest
    import evals.runner as runner
    import evals.pi_runner as pi_runner
    monkeypatch.setattr(runner, "_git", lambda: ("simulated", True))
    monkeypatch.setattr(runner, "_run_pi_items", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    monkeypatch.setattr(pi_runner, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    monkeypatch.setenv("FRA_ALLOW_LIVE_EVAL", "1")
    with pytest.raises(RuntimeError, match="dirty tree"):
        runner.run_eval(agent="pi", run_id="must-not-launch", output_root=tmp_path / "dirty")


def test_pi_runner_pytest_hard_guard_precedes_backend(monkeypatch, tmp_path):
    import pytest
    import evals.runner as runner
    monkeypatch.delenv("FRA_ALLOW_LIVE_EVAL", raising=False)
    monkeypatch.setattr(runner, "_git", lambda: ("simulated-clean", False))
    with pytest.raises(runner.LiveEvalGuardError) as caught:
        runner.run_eval(agent="pi", run_id="guarded", output_root=tmp_path / "guarded")
    assert caught.value.code == "live_eval_blocked_in_pytest"


def test_default_suite_launch_blocker_fails_loudly(monkeypatch, tmp_path):
    import pytest
    import evals.runner as runner
    monkeypatch.setenv("FRA_ALLOW_LIVE_EVAL", "1")
    monkeypatch.setattr(runner, "_git", lambda: ("simulated-clean", False))
    with pytest.raises(AssertionError, match="live launch attempted in default test suite"):
        runner.run_eval(agent="pi", run_id="blocked-launch", output_root=tmp_path / "blocked")


def test_guidance_digest_only_tracks_guideline_text():
    from evals.runner import _guidance_text_sha256
    original = "const QUERY_PLAN_GUIDELINES = [\"one\", \"two\"];\nconst unrelated = 1;"
    assert _guidance_text_sha256(original) == _guidance_text_sha256(original.replace("unrelated = 1", "unrelated = 2"))
    assert _guidance_text_sha256(original) != _guidance_text_sha256(original.replace("two", "three"))


def test_pi_backend_signal_handler_terminates_child(monkeypatch, tmp_path):
    import signal
    import pytest
    import evals.runner as runner

    handlers, children = {}, []
    class Backend:
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def terminate(self): self.stopped = True
        def wait(self, timeout=None): return 0
        def kill(self): self.stopped = True
    class Sock:
        def bind(self, address): pass
        def getsockname(self): return ("127.0.0.1", 54321)
        def close(self): pass
    child = Backend()
    def popen(*args, **kwargs):
        children.append(args[0])
        return child
    monkeypatch.setattr(runner.socket, "socket", Sock)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner, "_run_pi_items", _RUN_PI_ITEMS_IMPL)
    monkeypatch.setattr(runner.signal, "signal", lambda sig, handler: handlers.setdefault(sig, handler))

    def trigger(url, timeout=None):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(runner.urllib.request, "urlopen", trigger)
    with pytest.raises(SystemExit):
        runner._run_pi_items([], {}, tmp_path)
    assert children and child.stopped


def test_pi_child_env_drops_openrouter_key_from_parent(monkeypatch):
    from evals.pi_runner import build_pi_child_env
    parent = {"PATH": "/bin", "HOME": "/home/test", "OPENROUTER_API_KEY": "secret", "LANG": "C"}
    child = build_pi_child_env(parent, "http://127.0.0.1:9000")
    assert "OPENROUTER_API_KEY" not in child
    assert child["FRA_API_URL"] == "http://127.0.0.1:9000"


def test_numeric_structured_assertion_and_year_phrasings():
    item = {"question_type": "numeric_lookup", "expected_answer": {"kind": "numeric", "value": "331800000000", "unit": "USD", "tolerance": {"relative": "0.0005"}},
            "expected_evidence": {"period_end": "2026-06-30"}}
    record = {"tool_calls": [{"name": "verify_citations", "ok": True, "args": {"claims": [
        {"claim_id": "c1", "numeric": {"value": "331.8", "scale": "billions", "unit": "USD", "period_end": "2026-06-30"}}]}}],
        "verify_outputs": [{"report": {"verifications": [{"claim_id": "c1", "claim_type": "numeric", "claim_text": "Revenue for fiscal year 2026 was $331.8 billion", "verdict": "supported"}]}}]}
    assert grade_item(item, record)["numeric_correct"] is True
    item2 = {"question_type": "numeric_lookup", "expected_answer": {"kind": "numeric", "value": "0.468", "unit": "ratio", "tolerance": {"absolute": "0.0005"}},
             "expected_evidence": {"period_end": "2026-06-30"}}
    assert grade_item(item2, {"final_assistant_text": "Revenue growth in 2026 was 46.8%"})["numeric_correct"] is True
    assert grade_item(item2, {"final_assistant_text": "Revenue growth during 2025–2026 was 46.8%"})["numeric_correct"] is True
    mixed = {"question_type": "mixed", "expected_answer": {"kind": "mixed", "numeric": {
        "kind": "numeric", "ground_truth_key": "AAPL|2025|revenue_growth_yoy", "value": "0.064255",
        "unit": "ratio", "tolerance": {"absolute": "0.0005"}}}}
    claim = {"claim_id": "m", "claim_type": "numeric", "claim_text": "In 2025 revenue growth was 6.4%", "verdict": "supported"}
    call = {"name": "verify_citations", "ok": True, "args": {"claims": [{"claim_id": "m", "numeric": {
        "value": "6.4", "scale": "units", "unit": "percent", "period_end": "2025-09-27"}}]}}
    assert grade_item(mixed, {"tool_calls": [call], "verify_outputs": [{"report": {"verifications": [claim]}}]})["numeric_correct"] is True
    call["args"]["claims"][0]["numeric"]["period_end"] = "2025-09-28"
    assert grade_item(mixed, {"tool_calls": [call], "verify_outputs": [{"report": {"verifications": [claim]}}]})["numeric_correct"] is False


def test_refusal_evidence_branches_and_forbidden_figure_precision():
    from evals.graders import _forbidden_figures
    assert not any(_forbidden_figures(x) for x in ["See the 10-K", "accession 0001193125-26-323660", "8 tools were called"])
    assert all(_forbidden_figures(x) for x in ["$120 billion", "46.8%", "1.2x"])
    base = {"question_type": "failure", "ticker": "NVDA", "expected_tools": [],
            "expected_answer": {"kind": "refusal", "refusal_evidence": {"any_error_codes": ["not_pinned"], "or_search_companies_unpinned": True, "tool_optional": False}}}
    rec = {"tool_calls": [{"name": "search_companies", "ok": True, "result_summary": {"candidates": [{"ticker": "NVDA", "pinned": False}]}}], "final_assistant_text": "I cannot answer from pinned data."}
    assert grade_item(base, rec)["refusal_correct"] is True
    base["expected_answer"]["refusal_evidence"]["tool_optional"] = True
    assert grade_item(base, {"tool_calls": [], "final_assistant_text": "I cannot answer."})["refusal_correct"] is True
    base["expected_answer"]["refusal_evidence"]["tool_optional"] = False
    assert grade_item(base, {"tool_calls": [{"name": "x", "ok": False, "error_code": "not_pinned"}], "final_assistant_text": "Revenue was $120 billion."})["refusal_correct"] is False


def test_coverage_ignores_support_value_checks_and_pools_claims():
    from evals.graders import summarize
    item = {"question_type": "narrative", "expected_tools": []}
    report = {"report": {"verifications": [
        {"claim_text": "a", "verdict": "unsupported", "evidence_ids": ["x"], "deterministic_checks": {"evidence_exists": True, "hash_matches": True, "provenance_consistent": True, "ticker_matches": True, "single_accession": True, "value_matches": False}},
        {"claim_text": "b", "verdict": "supported", "evidence_ids": ["x"], "deterministic_checks": {"evidence_exists": True, "hash_matches": True, "provenance_consistent": True, "ticker_matches": True, "single_accession": True}}]}}
    grade = grade_item(item, {"verify_outputs": [report]})
    assert grade["citation_coverage"] == 1
    summary = summarize([{"split": "dev", "question_type": "narrative", "grades": grade}], "fake")
    assert summary["overall"]["citation_coverage"] == {"rate": 1.0, "n_claims": 2}
    assert summary["overall"]["unsupported_claim_rate"] == {"rate": 0.5, "n_claims": 2}


def test_comparison_values_can_appear_in_any_order():
    item = {"question_type": "comparison", "expected_answer": {"kind": "comparison", "values": [
        {"key": "A|2024|m", "value": "0.2", "unit": "ratio"}, {"key": "A|2025|m", "value": "0.4", "unit": "ratio"}]}}
    call = {"name": "compare_periods", "ok": True, "result_summary": {"rows": [{"value_a": {"result": "0.2"}, "value_b": {"result": "0.4"}}]}}
    assert grade_item(item, {"tool_calls": [call], "final_assistant_text": "Increased from 40% to 20%."})["numeric_correct"] is True
