import copy
import json
import shutil
from pathlib import Path

from evals.graders import GRADER_VERSION, grade_item


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/eval/baseline_rows"


def _dataset():
    return {item["question_id"]: item for item in
            (json.loads(line) for line in (ROOT / "evals/datasets/questions.v1.jsonl").read_text().splitlines())}


def _fixture(question_id):
    return json.loads((FIXTURES / f"{question_id}.jsonl").read_text())


def _cp7_fixture(question_id):
    return json.loads((FIXTURES / f"cp7-render-v2-dev-{question_id}.jsonl").read_text())


def _append_supported_metric_claim(record, *, evidence_id, metric, year, ticker="AAPL",
                                   value="0.028", unit="ratio", period_end="2023-09-30",
                                   claim_text=None):
    if claim_text is None:
        claim_text = f"Fiscal {year} {metric} was {float(value) * (100 if unit == 'ratio' else 1)}%."
    summary = {"ticker": ticker, "fiscal_year": year, "metric_evidence_id": evidence_id,
               "result": {"metric": metric, "period_end": period_end, "result": value,
                          "result_unit": unit}}
    record["tool_calls"].append({"name": "calculate_financial_metric", "ok": True,
                                 "result_summary": summary})
    verify_call = next((call for call in record["tool_calls"] if call.get("name") == "verify_citations"), None)
    if not record.get("verify_outputs"):
        record["verify_outputs"] = [{"report": {"verifications": []}}]
    claim_id = f"claim-{len(record['verify_outputs'][-1]['report']['verifications']) + 1}"
    assertion = {"value": value, "unit": unit, "period_end": period_end}
    claim = {"claim_id": claim_id, "claim_text": claim_text, "claim_type": "numeric",
             "verdict": "supported", "evidence_ids": [evidence_id]}
    if verify_call is None:
        verify_call = {"name": "verify_citations", "ok": True, "args": {"claims": []}}
        record["tool_calls"].append(verify_call)
    verify_call.setdefault("args", {}).setdefault("claims", []).append(
        {"claim_id": claim_id, "claim_text": claim_text, "claim_type": "numeric",
         "evidence_ids": [evidence_id], "numeric": assertion})
    record["verify_outputs"][-1]["report"]["verifications"].append(claim)


def test_v3_g1_g3_g4_and_baseline_refusals_preserve_existing_behaviour():
    assert GRADER_VERSION == "v3"
    items = _dataset()
    revenue = _fixture("aapl-num-revenue-2024")
    assert grade_item(items[revenue["question_id"]], revenue)["numeric_correct"] is True
    refusals = ("fail-unavailable-year", "msft-num-revenue_growth_yoy-2024",
                "aapl-num-revenue_growth_yoy-2023", "fail-unpinned-company", "fail-unsupported-metric")
    for question_id in refusals:
        assert grade_item(items[question_id], _fixture(question_id))["refusal_correct"] is True
    narrative = _fixture("msft-nar-ai-risk")
    assert "runtime_error" in grade_item(items[narrative["question_id"]], narrative)["error_categories"]
    only_tool_failure = {"question_type": "narrative", "expected_answer": {"kind": "narrative"},
                         "expected_tools": []}
    tool_categories = grade_item(only_tool_failure, {"tool_calls": [
        {"name": "verify_citations", "ok": False, "error_code": "backend_not_running"}],
        "runtime_error": None})["error_categories"]
    assert "tool_error" in tool_categories and "runtime_error" not in tool_categories


def test_v3_accepts_b_dev_refusals_with_verified_adjacent_figures():
    items = _dataset()
    for question_id in ("aapl-num-revenue_growth_yoy-2023", "fail-unavailable-year"):
        record = _cp7_fixture(question_id)
        assert grade_item(items[question_id], record)["refusal_correct"] is True
        assert grade_item(items[question_id], record)["tool_selection_correct"] is True


def test_v3_rules_reject_requested_metric_and_unverified_or_fabricated_figures():
    items = _dataset()
    base = _cp7_fixture("aapl-num-revenue_growth_yoy-2023")

    # Rule 1: a supported met_ citation whose calculator result is the requested FY2023 growth.
    requested_claim = copy.deepcopy(base)
    _append_supported_metric_claim(requested_claim, evidence_id="met_requested",
                                   metric="revenue_growth_yoy", year=2023,
                                   value="0.028", period_end="2023-09-30")
    assert grade_item(items[requested_claim["question_id"]], requested_claim)["refusal_correct"] is False

    # Rule 2: no supported claim backs the requested-family growth figure in this record.
    unsupported_target = copy.deepcopy(base)
    unsupported_target["final_assistant_text"] += "\nFY2023 revenue growth was 2.0%."
    assert grade_item(items[unsupported_target["question_id"]], unsupported_target)["refusal_correct"] is False

    # Rule 3: an unsupported currency figure is fabricated even though the adjacent revenue is verified.
    fabricated = copy.deepcopy(base)
    fabricated["final_assistant_text"] += "\nRevenue was $999 million."
    assert grade_item(items[fabricated["question_id"]], fabricated)["refusal_correct"] is False


def test_v3_allows_supported_other_period_and_other_metric_figures():
    items = _dataset()
    adjacent = _cp7_fixture("fail-unavailable-year")
    assert "FY2024 revenue growth" in adjacent["final_assistant_text"]
    assert grade_item(items["fail-unavailable-year"], adjacent)["refusal_correct"] is True

    gross_margin = _fixture("fail-unsupported-metric")
    _append_supported_metric_claim(gross_margin, evidence_id="met_operating_margin",
                                   metric="operating_margin", year=2026, ticker="MSFT",
                                   value="0.447", period_end="2026-06-30",
                                   claim_text="Microsoft FY2026 operating margin was 44.7%.")
    gross_margin["final_assistant_text"] = (
        "I cannot provide gross margin. The verified FY2026 operating margin was 44.7%."
    )
    assert grade_item(items["fail-unsupported-metric"], gross_margin)["refusal_correct"] is True


def test_scaled_assertion_text_matching_obeys_stated_rounding():
    item = {"question_type": "numeric_lookup", "expected_tools": [],
            "expected_answer": {"kind": "numeric", "value": "391035000000", "unit": "USD",
                                "metric": "revenue", "ground_truth_key": "AAPL|2024|revenue",
                                "tolerance": {"relative": "0.0005"}},
            "expected_evidence": {"period_end": "2024-09-28"}}

    def record(text, value, scale):
        return {"tool_calls": [{"name": "verify_citations", "ok": True, "args": {"claims": [
            {"claim_id": "c", "numeric": {"value": value, "scale": scale, "unit": "USD",
                                            "period_end": "2024-09-28"}}]}}],
            "verify_outputs": [{"report": {"verifications": [{"claim_id": "c", "claim_type": "numeric",
                "claim_text": text, "verdict": "supported"}]}}]}

    assert grade_item(item, record("Revenue was $391,035 million.", "391035", "millions"))["numeric_correct"] is True
    assert grade_item(item, record("Revenue was $391,035 million.", "391", "billions"))["numeric_correct"] is False
    assert grade_item(item, record("Revenue was $391.0 billion.", "391", "billions"))["numeric_correct"] is True
    assert grade_item(item, record("Revenue was $390 billion.", "390", "billions"))["numeric_correct"] is False


def test_regrade_v2_to_v3_preserves_v1_and_records_append_only_history(tmp_path):
    from scripts.regrade_eval import regrade_run

    run_dir = tmp_path / "artifacts/evals/regrade-test"
    run_dir.mkdir(parents=True)
    source_run = ROOT / "artifacts/evals/cp6b-baseline-v1"
    shutil.copyfile(source_run / "dataset_manifest.json", run_dir / "dataset_manifest.json")
    (tmp_path / "evals/datasets").mkdir(parents=True)
    shutil.copyfile(ROOT / "evals/datasets/questions.v1.jsonl", tmp_path / "evals/datasets/questions.v1.jsonl")
    (run_dir / "config.json").write_text('{"agent":"pi"}\n')
    (run_dir / "README.md").write_text("# Temporary run\n\nExisting grader v2 history.\n")

    row = _cp7_fixture("fail-unavailable-year")
    row["grades"] = {"previous": "v2"}
    current_results = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    old_v1 = copy.deepcopy(row)
    old_v1["grades"] = {"preserved": "v1"}
    v1_results = (json.dumps(old_v1, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    v1_summary = b'{"grader_version":"v1","saved":"unchanged"}\n'
    v2_summary = b'{"agent":"pi","grader_version":"v2"}\n'
    (run_dir / "results.grader_v1.jsonl").write_bytes(v1_results)
    (run_dir / "summary.grader_v1.json").write_bytes(v1_summary)
    (run_dir / "results.jsonl").write_bytes(current_results)
    (run_dir / "summary.json").write_bytes(v2_summary)
    # The extant v1 -> v2 marker was an object; migrate it without losing its entry.
    (run_dir / "regrade.json").write_text(json.dumps({
        "from": "v1", "grader_version": "v2", "grader_git_sha": "old-sha",
        "changes": ["previous changes"],
        "per_item_changes": [{"question_id": row["question_id"], "field": "old", "v1": 0, "v2": 1}],
    }))
    raw_before = [{key: value for key, value in row.items() if key != "grades"}]

    regrade_run("regrade-test", root=tmp_path)
    assert (run_dir / "results.grader_v1.jsonl").read_bytes() == v1_results
    assert (run_dir / "summary.grader_v1.json").read_bytes() == v1_summary
    assert (run_dir / "results.grader_v2.jsonl").read_bytes() == current_results
    assert (run_dir / "summary.grader_v2.json").read_bytes() == v2_summary
    updated = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    assert [{key: value for key, value in value.items() if key != "grades"} for value in updated] == raw_before
    assert json.loads((run_dir / "summary.json").read_text())["grader_version"] == GRADER_VERSION
    history = json.loads((run_dir / "regrade.json").read_text())
    assert len(history) == 2 and [(entry["from"], entry["to"]) for entry in history] == [
        ("v1", "v2"), ("v2", "v3")]
    assert history[0]["sha"] == "old-sha"
    assert history[-1]["sha"] and history[-1]["per_item_changes"]
    snapshot = {name: (run_dir / name).read_bytes() for name in
                ("results.jsonl", "summary.json", "regrade.json", "README.md")}

    regrade_run("regrade-test", root=tmp_path)
    assert {name: (run_dir / name).read_bytes() for name in snapshot} == snapshot
