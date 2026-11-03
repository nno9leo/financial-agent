#!/usr/bin/env python3
"""Export the three measurement runs as one JSON bundle for the replay console in web/.

Read-only over committed artifacts: every evidence record is rebuilt from the tool results the
agent actually saw during the run, so the console never shows text the agent didn't retrieve.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from report_results import (RUN_IDS, _claims, _cost, _load_run, _p1_rows,  # noqa: E402
                            _pctl, _regrade_history)

RUN_LABELS = {
    "cp6b-baseline-v1": {"arm": "A", "label": "Baseline (render v1)", "split": "all"},
    "cp7-render-v2-dev": {"arm": "B", "label": "Render v2 (dev)", "split": "dev"},
    "cp7-render-v2-holdout": {"arm": "B", "label": "Render v2 (holdout)", "split": "holdout"},
}
GRADE_FIELDS = ("numeric_correct", "refusal_correct", "tool_selection_correct", "retrieval_hit_at_k",
                "retrieval_rank", "citation_coverage", "unsupported_claim_count", "unsupported_claim_n_claims",
                "partial_claim_rate", "metadata_correct", "narrative_key_points_proxy", "retry_used",
                "error_categories", "numeric_path")
FACT_FIELDS = ("label", "tag", "taxonomy", "value", "unit", "period_start", "period_end", "fiscal_year",
               "fiscal_period", "frame", "form", "filed_at", "accession_number", "ticker", "source_url")


def _questions() -> dict[str, dict]:
    path = ROOT / "evals/datasets/questions.v1.jsonl"
    return {row["question_id"]: row for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())}


def _record_metric(evidence: dict, metric_id: str | None, value: dict, ticker: str | None, accession: str | None) -> None:
    """Register a metric (met_) and its input facts (fact_) from one calculator result."""
    fact_ids = list(value.get("evidence_ids") or [])
    inputs = list(value.get("inputs") or [])
    if len(fact_ids) == len(inputs):
        for fact_id, fact in zip(fact_ids, inputs):
            evidence.setdefault(fact_id, {"kind": "fact", **{k: fact.get(k) for k in FACT_FIELDS}})
    if metric_id:
        evidence.setdefault(metric_id, {
            "kind": "metric", "metric": value.get("metric"), "formula": value.get("formula"),
            "value": value.get("result"), "display_value": value.get("display_value"),
            "unit": value.get("result_unit"), "period_start": value.get("period_start"),
            "period_end": value.get("period_end"), "inputs": fact_ids, "ticker": ticker,
            "accession_number": accession})


def _collect_evidence(evidence: dict, call: dict) -> list[str]:
    """Add every citable record in one tool result to the evidence map; return the IDs it produced."""
    result = call.get("result_summary")
    if not isinstance(result, dict):
        return []
    produced: list[str] = []
    ticker, accession = result.get("ticker") or result.get("company"), result.get("accession_number")
    for chunk in result.get("evidence") or []:
        if not chunk.get("text"):
            continue
        produced.append(chunk["evidence_id"])
        evidence.setdefault(chunk["evidence_id"], {
            "kind": "chunk", "text": chunk["text"], "section": chunk.get("section"),
            "chunk_id": chunk.get("chunk_id"), "ticker": chunk.get("ticker") or ticker,
            "accession_number": chunk.get("accession_number"), "source_url": chunk.get("source_url"),
            "start_offset": chunk.get("start_offset"), "end_offset": chunk.get("end_offset"),
            "content_hash": chunk.get("content_hash"), "filing_date": chunk.get("filing_date")})
    if call["name"] == "calculate_financial_metric" and isinstance(result.get("result"), dict):
        _record_metric(evidence, result.get("metric_evidence_id"), result["result"], ticker, accession)
        produced.append(result.get("metric_evidence_id"))
    for row in result.get("rows") or []:
        for side in ("a", "b"):
            value = row.get(f"value_{side}")
            if isinstance(value, dict):
                _record_metric(evidence, row.get(f"metric_evidence_id_{side}"), value, ticker, accession)
                produced.append(row.get(f"metric_evidence_id_{side}"))
    return [eid for eid in produced if eid]


def _step_summary(call: dict) -> str:
    """One human-readable line per tool call for the timeline."""
    if not call.get("ok"):
        return f"error: {call.get('error_code') or 'tool_error'}"
    result = call.get("result_summary") or {}
    name = call["name"]
    if name == "search_companies":
        pinned = [c for c in result.get("candidates") or [] if c.get("pinned")]
        if pinned:
            return f"resolved {pinned[0]['ticker']} · pinned {pinned[0]['pinned_accession']}"
        return f"{len(result.get('candidates') or [])} candidates, none pinned"
    if name == "list_filings":
        filings = result.get("filings") or []
        return f"{len(filings)} filing(s)" + (f" · {filings[0].get('accession_number')}" if filings else "")
    if name == "search_filing_chunks":
        hits = result.get("evidence") or []
        if not hits:
            return "no passages"
        return f"{len(hits)} passages · top: {hits[0].get('section')} (score {hits[0].get('retrieval_score', 0):.2f})"
    if name == "calculate_financial_metric":
        value = result.get("result") or {}
        return f"{value.get('metric')} = {value.get('display_value')} · {value.get('formula')}"
    if name == "compare_periods":
        rows = result.get("rows") or []
        return "; ".join(f"{r.get('metric')} {(r.get('value_a') or {}).get('display_value')} → "
                         f"{(r.get('value_b') or {}).get('display_value')} ({r.get('change_display')})" for r in rows)
    if name == "get_filing_section":
        return f"section {result.get('section')} · {len(result.get('text') or '')} chars"
    if name == "verify_citations":
        report = result.get("report") or {}
        return (f"attempt {(result.get('_financialResearch') or {}).get('attempt', 1)} · "
                f"{report.get('supported', 0)} supported, {report.get('partially_supported', 0)} partial, "
                f"{report.get('unsupported', 0)} unsupported" + (" · final" if result.get("is_final") else ""))
    if name == "export_research_report":
        return "report preview"
    return "ok"


def _strip_internal(args: Any) -> Any:
    if isinstance(args, dict):
        return {k: _strip_internal(v) for k, v in args.items() if not k.startswith("_")}
    if isinstance(args, list):
        return [_strip_internal(v) for v in args]
    return args


def _item(row: dict, question: dict, evidence: dict) -> dict:
    steps = []
    for call in row.get("tool_calls") or []:
        steps.append({"name": call["name"], "args": _strip_internal(call.get("args") or {}), "ok": call.get("ok"),
                      "error_code": call.get("error_code"), "summary": _step_summary(call),
                      "evidence_ids": _collect_evidence(evidence, call)})
    attempts = []
    for output in row.get("verify_outputs") or []:
        report = output.get("report") or {}
        attempts.append({
            "attempt": (output.get("_financialResearch") or {}).get("attempt", len(attempts) + 1),
            "is_final": bool(output.get("is_final")),
            "final_answer_markdown": output.get("final_answer_markdown"),
            "counts": {k: report.get(k, 0) for k in ("supported", "partially_supported", "unsupported")},
            "verifications": [{k: v.get(k) for k in ("claim_id", "claim_text", "claim_type", "evidence_ids",
                                                      "verdict", "reason", "semantic_score", "deterministic_checks")}
                              for v in report.get("verifications") or []]})
    grades = row.get("grades") or {}
    return {
        "question_id": row["question_id"], "question": question.get("question"),
        "question_type": row.get("question_type"), "split": row.get("split"), "ticker": question.get("ticker"),
        "accession_number": question.get("accession_number"), "expected": question.get("expected_answer"),
        "expected_tools": question.get("expected_tools"),
        "final_text": row.get("final_assistant_text"), "latency_s": row.get("latency_s"),
        "cost_usd": round(float(row.get("cost_pi_usd") or 0) + float(row.get("cost_judge_usd") or 0), 6),
        "tokens_in": row.get("tokens_in"), "tokens_out": row.get("tokens_out"),
        "runtime_error": row.get("runtime_error"),
        "grades": {k: grades.get(k) for k in GRADE_FIELDS},
        "steps": steps, "attempts": attempts}


def _frac(numerator: int, denominator: int) -> dict:
    return {"num": numerator, "den": denominator}


def _bool_frac(rows: list[dict], field: str) -> dict:
    values = [r["grades"].get(field) for r in rows if r.get("grades", {}).get(field) is not None]
    return _frac(sum(v is True for v in values), len(values))


def _metrics(rows: list[dict]) -> dict:
    latencies = [float(r["latency_s"]) for r in rows if r.get("latency_s") is not None]
    numeric = [r for r in rows if r.get("grades", {}).get("numeric_correct") is not None]
    return {"items": len(rows), "p1_strict_numeric": _frac(*_p1_rows(rows)),
            "numeric_correct": _frac(sum(r["grades"]["numeric_correct"] is True for r in numeric), len(numeric)),
            "unsupported_claims": _frac(*_claims(rows)),
            "refusal_correct": _bool_frac(rows, "refusal_correct"),
            "tool_selection": _bool_frac(rows, "tool_selection_correct"),
            "retrieval_hit": _bool_frac(rows, "retrieval_hit_at_k"),
            "latency_p50_s": _pctl(latencies, .5), "latency_p95_s": _pctl(latencies, .95),
            "cost_usd": round(_cost(rows), 4), "run_errors": _frac(sum(bool(r.get("runtime_error")) for r in rows), len(rows))}


def build_bundle(root: Path = ROOT) -> dict:
    questions = _questions()
    evidence: dict[str, dict] = {}
    runs = []
    for run_id in RUN_IDS:
        run = _load_run(root, run_id)
        rows = run["rows"]
        env = run["environment"]
        by_split = {split: _metrics([r for r in rows if r.get("split") == split]) for split in ("dev", "holdout")}
        runs.append({
            "run_id": run_id, **RUN_LABELS[run_id],
            "render": run["config"].get("derived_metric_render") or "v1",
            "agent_model": (env.get("agent") or {}).get("configured_model"),
            "agent_thinking": (env.get("agent") or {}).get("configured_thinking"),
            "judge_model": (env.get("judge") or {}).get("configured_model"),
            "git_sha": env.get("git_sha"),
            "regrade_history": _regrade_history(run["regrades"]),
            "metrics": _metrics(rows), "metrics_by_split": by_split,
            "items": [_item(r, questions.get(r["question_id"], {}), evidence) for r in rows]})
    return {"generated_from": [f"artifacts/evals/{r}/results.jsonl" for r in RUN_IDS],
            "dataset_sha256": json.loads((root / "evals/datasets/dataset_manifest.json").read_text(encoding="utf-8")).get("sha256"),
            "runs": runs, "evidence": evidence}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "web/public/data/console.json")
    args = parser.parse_args(argv)
    bundle = build_bundle()
    cited = {eid for run in bundle["runs"] for item in run["items"] for a in item["attempts"]
             for v in a["verifications"] for eid in v["evidence_ids"] or []}
    missing = sorted(cited - bundle["evidence"].keys())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bundle, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KiB): {sum(len(r['items']) for r in bundle['runs'])} items, "
          f"{len(bundle['evidence'])} evidence records, {len(missing)} cited IDs unresolved {missing[:5]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
