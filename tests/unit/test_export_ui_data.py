import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("export_ui_data", ROOT / "scripts/export_ui_data.py")
export_ui_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_ui_data)


def test_bundle_resolves_every_cited_evidence_id():
    bundle = export_ui_data.build_bundle(ROOT)
    cited = {eid for run in bundle["runs"] for item in run["items"] for attempt in item["attempts"]
             for verification in attempt["verifications"] for eid in verification["evidence_ids"] or []}
    assert cited
    assert cited <= bundle["evidence"].keys()


def test_bundle_metrics_match_published_report():
    runs = {run["run_id"]: run for run in export_ui_data.build_bundle(ROOT)["runs"]}
    baseline = runs["cp6b-baseline-v1"]
    assert baseline["metrics_by_split"]["dev"]["p1_strict_numeric"] == {"num": 0, "den": 10}
    assert baseline["metrics_by_split"]["holdout"]["p1_strict_numeric"] == {"num": 4, "den": 9}
    assert baseline["metrics"]["unsupported_claims"] == {"num": 19, "den": 54}
    assert runs["cp7-render-v2-dev"]["metrics"]["p1_strict_numeric"] == {"num": 8, "den": 10}
    assert runs["cp7-render-v2-dev"]["metrics"]["unsupported_claims"] == {"num": 3, "den": 43}
    assert runs["cp7-render-v2-holdout"]["metrics"]["p1_strict_numeric"] == {"num": 7, "den": 9}
    assert runs["cp7-render-v2-holdout"]["metrics"]["unsupported_claims"] == {"num": 0, "den": 23}


def test_metric_evidence_links_to_its_input_facts():
    evidence = export_ui_data.build_bundle(ROOT)["evidence"]
    metrics = [ev for ev in evidence.values() if ev["kind"] == "metric"]
    assert metrics
    for metric in metrics:
        for fact_id in metric["inputs"]:
            assert evidence[fact_id]["kind"] == "fact"
