import json
from pathlib import Path

import pytest

from scripts.report_results import RUN_IDS, render_report


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _row(split: str, *, correct: bool, error: str | None = None):
    return {
        "question_id": f"{split}-q", "split": split, "question_type": "numeric_lookup",
        "latency_s": 10, "cost_pi_usd": .1, "cost_judge_usd": .02, "runtime_error": error,
        "verify_outputs": [{"report": {"verifications": [
            {"verdict": "unsupported", "reason": "unit_mismatch", "claim_type": "numeric"}]}}],
        "grades": {"numeric_correct": correct, "refusal_correct": None, "tool_selection_correct": True,
                   "retrieval_hit_at_k": True, "citation_coverage_numerator": 1,
                   "citation_coverage_n_claims": 1, "unsupported_claim_count": 1,
                   "unsupported_claim_n_claims": 1},
    }


def _run(root: Path, run_id: str, rows: list[dict]):
    path = root / "artifacts/evals" / run_id
    _write(path / "summary.json", {"grader_version": "v3"})
    path.mkdir(parents=True, exist_ok=True)
    (path / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _write(path / "config.json", {"derived_metric_render": "v2"})
    _write(path / "environment.json", {"git_sha": "fixture", "dirty_tree": False,
        "agent": {"observed_model": "fake-agent"}, "judge": {"observed_models": ["fake-judge"]}})
    if run_id != "cp7-render-v2-holdout":
        _write(path / "regrade.json", [{"from": "v2", "to": "v3"}])


def test_report_results_reads_tiny_approved_fixture_runs(tmp_path):
    _run(tmp_path, RUN_IDS[0], [_row("dev", correct=False), _row("holdout", correct=True)])
    _run(tmp_path, RUN_IDS[1], [_row("dev", correct=True)])
    _run(tmp_path, RUN_IDS[2], [_row("holdout", correct=True)])
    _write(tmp_path / "evals/datasets/dataset_manifest.json", {
        "counts_by_question_type": {"numeric_lookup": 2, "narrative": 1}, "item_count": 3,
        "external_items": 0, "sha256": "fixture-hash", "split_rule": "fixture split"})

    output = render_report(tmp_path)
    assert "Baseline overall and by split" in output
    assert "0/1 (0.0%; n=1)" in output
    assert "unit_mismatch" in output
    assert "cp7-render-v2-holdout" in output
    assert "v3" in output
    assert "Generated numeric items | 2" in output


def test_report_results_rejects_non_v3_summary(tmp_path):
    _run(tmp_path, RUN_IDS[0], [_row("dev", correct=False)])
    _run(tmp_path, RUN_IDS[1], [_row("dev", correct=True)])
    _run(tmp_path, RUN_IDS[2], [_row("holdout", correct=True)])
    _write(tmp_path / "artifacts/evals" / RUN_IDS[0] / "summary.json", {"grader_version": "v2"})
    _write(tmp_path / "evals/datasets/dataset_manifest.json", {"item_count": 0, "counts_by_question_type": {}})
    with pytest.raises(ValueError, match="expected grader v3"):
        render_report(tmp_path)
