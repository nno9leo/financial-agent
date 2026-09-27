#!/usr/bin/env python3
"""Render measurement tables only from the three valid measurement run artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUN_IDS = ("cp6b-baseline-v1", "cp7-render-v2-dev", "cp7-render-v2-holdout")
P1_TYPES = {"numeric_lookup", "mixed"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_run(root: Path, run_id: str) -> dict:
    if run_id.startswith("_") or run_id not in RUN_IDS:
        raise ValueError(f"run is not an approved measurement: {run_id}")
    path = root / "artifacts/evals" / run_id
    summary = _json(path / "summary.json")
    if summary.get("grader_version") != "v3":
        raise ValueError(f"{run_id}: expected grader v3, got {summary.get('grader_version')!r}")
    rows = [json.loads(line) for line in (path / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    config = _json(path / "config.json")
    environment = _json(path / "environment.json")
    regrade_path = path / "regrade.json"
    regrades = _json(regrade_path) if regrade_path.is_file() else []
    if not isinstance(regrades, list):
        raise ValueError(f"{run_id}: regrade.json must contain an array")
    return {"run_id": run_id, "path": path, "summary": summary, "rows": rows,
            "config": config, "environment": environment, "regrades": regrades}


def _rate(numerator: int, denominator: int, *, percent: bool = True) -> str:
    if denominator == 0:
        return f"— (n=0)"
    suffix = f"; n={denominator}"
    if percent:
        suffix = f" ({100 * numerator / denominator:.1f}%; n={denominator})"
    else:
        suffix = f" (n={denominator})"
    return f"{numerator}/{denominator}{suffix}"


def _mean_rate(rows: list[dict], field: str) -> str:
    selected = [row["grades"].get(field) for row in rows if row.get("grades", {}).get(field) is not None]
    return _rate(sum(value is True for value in selected), len(selected))


def _claims(rows: list[dict], *, narrative_only: bool = False) -> tuple[int, int]:
    selected = [row for row in rows if not narrative_only or row.get("question_type") == "narrative"]
    bad = sum(int(row.get("grades", {}).get("unsupported_claim_count", 0) or 0) for row in selected)
    count = sum(int(row.get("grades", {}).get("unsupported_claim_n_claims", 0) or 0) for row in selected)
    return bad, count


def _coverage(rows: list[dict]) -> str:
    selected = [row for row in rows if row.get("grades", {}).get("citation_coverage_n_claims") is not None]
    numerator = sum(int(row["grades"].get("citation_coverage_numerator", 0) or 0) for row in selected)
    denominator = sum(int(row["grades"].get("citation_coverage_n_claims", 0) or 0) for row in selected)
    return _rate(numerator, denominator)


def _numeric(rows: list[dict], *, strict: bool = False) -> str:
    selected = [row for row in rows if row.get("grades", {}).get("numeric_correct") is not None
                and (not strict or row.get("question_type") in P1_TYPES)]
    return _rate(sum(row["grades"]["numeric_correct"] is True for row in selected), len(selected))


def _refusal(rows: list[dict]) -> str:
    return _mean_rate(rows, "refusal_correct")


def _retrieval(rows: list[dict]) -> str:
    return _mean_rate(rows, "retrieval_hit_at_k")


def _pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * p)]


def _latency(rows: list[dict]) -> str:
    values = [float(row["latency_s"]) for row in rows if row.get("latency_s") is not None]
    if not values:
        return "— (n=0)"
    return f"{_pctl(values, .5):.1f}s / {_pctl(values, .95):.1f}s (n={len(values)})"


def _cost(rows: list[dict]) -> float:
    return sum(float(row.get("cost_pi_usd") or 0) + float(row.get("cost_judge_usd") or 0) for row in rows)


def _run_errors(rows: list[dict]) -> int:
    return sum(bool(row.get("runtime_error")) for row in rows)


def _baseline_line(label: str, rows: list[dict]) -> list[str]:
    unsupported, claims = _claims(rows)
    narrative_bad, narrative_claims = _claims(rows, narrative_only=True)
    all_numeric = _numeric(rows)
    strict_numeric = _numeric(rows, strict=True)
    p50_p95 = _latency(rows)
    return [f"| {label} | {len(rows)} | {all_numeric} | {strict_numeric} | {_refusal(rows)} | "
            f"{_mean_rate(rows, 'tool_selection_correct')} | {_retrieval(rows)} | "
            f"{_coverage(rows)} | {_rate(unsupported, claims)} | "
            f"{_rate(narrative_bad, narrative_claims)} | {p50_p95} | ${_cost(rows):.4f} | "
            f"{_run_errors(rows)}/{len(rows)} (n={len(rows)}) |"]


def _p1_rows(rows: list[dict]) -> tuple[int, int]:
    selected = [r for r in rows if r.get("question_type") in P1_TYPES and r.get("grades", {}).get("numeric_correct") is not None]
    return sum(r["grades"]["numeric_correct"] is True for r in selected), len(selected)


def _p2_rows(rows: list[dict]) -> tuple[int, int]:
    return _claims(rows)


def _regrade_history(regrades: list[dict]) -> str:
    if not regrades:
        return "none (summary is v3)"
    versions = []
    for row in regrades:
        source, target = row.get("from"), row.get("to")
        if source and (not versions or versions[-1] != source):
            versions.append(source)
        if target:
            versions.append(target)
    return "→".join(versions) if versions else "metadata present"


def _observed(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(x) for x in value)
    return str(value or "not reported")


def render_report(root: Path = ROOT) -> str:
    root = Path(root)
    runs = {run_id: _load_run(root, run_id) for run_id in RUN_IDS}
    baseline = runs[RUN_IDS[0]]["rows"]
    lines = ["# Eval results report", "",
             "Only the three approved, non-underscore measurement run IDs are included. Every rate includes its denominator; grader v3 is required.", "",
             "## Baseline overall and by split", "",
             "Source: `cp6b-baseline-v1`; metrics are calculated from its `results.jsonl` fields.", "",
             "| Split | Items | Numeric correct | Strict P1 numeric | Refusal correct | Tool selection | Retrieval hit@k | Citation coverage | Unsupported claims | Narrative unsupported | Latency p50 / p95 | Total cost | Run errors |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    lines.extend(_baseline_line("overall", baseline))
    for split in ("dev", "holdout"):
        lines.extend(_baseline_line(split, [row for row in baseline if row.get("split") == split]))
    lines.extend(["", "## Render A/B experiment", "",
                  "A uses the matching split rows from `cp6b-baseline-v1`; B uses the named treatment run. P1 is strict numeric accuracy over numeric_lookup + mixed items. P2 is pooled unsupported claims.", "",
                  "| Split | Arm | Run id | P1 strict numeric | P2 unsupported claims | Tool selection | Refusal correct | Retrieval hit@k | Narrative unsupported | Run errors | Total cost |",
                  "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for split, treatment_id in (("dev", "cp7-render-v2-dev"), ("holdout", "cp7-render-v2-holdout")):
        for arm, run_id, source_rows in (("A", RUN_IDS[0], [r for r in baseline if r.get("split") == split]),
                                         ("B", treatment_id, runs[treatment_id]["rows"])):
            p1_ok, p1_n = _p1_rows(source_rows)
            p2_bad, p2_n = _p2_rows(source_rows)
            narrative_bad, narrative_n = _claims(source_rows, narrative_only=True)
            lines.append(f"| {split} | {arm} | `{run_id}` | {_rate(p1_ok, p1_n)} | {_rate(p2_bad, p2_n)} | "
                         f"{_mean_rate(source_rows, 'tool_selection_correct')} | {_refusal(source_rows)} | "
                         f"{_retrieval(source_rows)} | {_rate(narrative_bad, narrative_n)} | "
                         f"{_run_errors(source_rows)}/{len(source_rows)} (n={len(source_rows)}) | ${_cost(source_rows):.4f} |")
    lines.extend(["", "## Unsupported-claim reasons in baseline", "",
                  "Counts are derived from the baseline's final verification reports; `n` is unsupported claims.", "",
                  "| Reason | Unsupported claims |", "|---|---:|"])
    reasons: dict[str, int] = {}
    for row in baseline:
        final_output = (row.get("verify_outputs") or [None])[-1]
        for verification in (final_output or {}).get("report", {}).get("verifications", []):
            if verification.get("verdict") == "unsupported":
                reason = str(verification.get("reason") or "unspecified")
                reasons[reason] = reasons.get(reason, 0) + 1
    for reason in sorted(reasons):
        lines.append(f"| `{reason}` | {reasons[reason]} (n={sum(reasons.values())}) |")
    if not reasons:
        lines.append("| none recorded | 0 (n=0) |")
    lines.extend(["", "## Run provenance", "",
                  "| Run id | Git SHA | Dirty | Grader | Agent observed | Judge observed | Render | Regrade history |",
                  "|---|---|---:|---|---|---|---|---|"])
    for run_id in RUN_IDS:
        run = runs[run_id]
        env = run["environment"]
        lines.append(f"| `{run_id}` | `{env.get('git_sha', 'unknown')}` | {env.get('dirty_tree', 'unknown')} | "
                     f"{run['summary']['grader_version']} | {_observed(env.get('agent', {}).get('observed_model'))} | "
                     f"{_observed(env.get('judge', {}).get('observed_models'))} | "
                     f"{run['config'].get('derived_metric_render', 'not recorded')} | {_regrade_history(run['regrades'])} |")
    dataset_manifest = _json(root / "evals/datasets/dataset_manifest.json")
    counts = dataset_manifest.get("counts_by_question_type", {})
    generated_numeric = int(counts.get("numeric_lookup", 0)) + int(counts.get("numeric_failure", 0))
    total = int(dataset_manifest.get("item_count", sum(counts.values())))
    authored = total - generated_numeric - int(dataset_manifest.get("external_items", 0))
    lines.extend(["", "## Dataset composition", "",
                  "| Dataset field | Value |", "|---|---:|"])
    lines.append(f"| Total items | {total} |")
    lines.append(f"| Generated numeric items | {generated_numeric} |")
    lines.append(f"| Hand-authored/reviewed items | {authored} |")
    lines.append(f"| External items | {dataset_manifest.get('external_items', 0)} |")
    lines.append(f"| Frozen dataset SHA-256 | `{dataset_manifest.get('sha256', 'missing')}` |")
    lines.append(f"| Holdout split policy | {dataset_manifest.get('split_rule', 'not recorded')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root or tiny fixture root")
    args = parser.parse_args(argv)
    print(render_report(args.root), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
