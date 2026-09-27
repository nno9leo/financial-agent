"""Build and validate the frozen, accession-pinned v1 evaluation dataset."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from evals.generate_numeric import generate

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/questions.v1.jsonl"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate_ground_truth_keys(item: dict, gt: dict) -> None:
    answer = item.get("expected_answer", {})
    keys = list(answer.get("ground_truth_keys", []))
    if answer.get("ground_truth_key"):
        keys.append(answer["ground_truth_key"])
    if answer.get("numeric", {}).get("ground_truth_key"):
        keys.append(answer["numeric"]["ground_truth_key"])
    for key in keys:
        if key not in gt:
            raise ValueError(f"{item.get('question_id')} has unknown ground truth key {key}")


def validate_gold_evidence(item: dict, chunks: dict) -> None:
    for eid in item.get("expected_evidence", {}).get("evidence_ids", []):
        row = chunks.get(eid)
        if row is None:
            raise ValueError(f"{item.get('question_id')} references missing gold evidence ID {eid}")
        accession = item.get("accession_number")
        if accession and row.get("accession_number") != accession:
            raise ValueError(f"{item.get('question_id')} gold evidence crosses accession")


def build(*, chunks_dir: Path | None = None) -> tuple[list[dict], dict]:
    frozen_splits = {x["question_id"]: x["split"] for x in read_jsonl(DATASET)} if DATASET.exists() else {}
    gt_path = ROOT / "evals/ground_truth/statement_ground_truth.json"
    manifest_path = ROOT / "data/manifests/filings.v1.json"
    data_manifest_path = ROOT / "data/manifests/filings.v1.json"
    manifest = json.loads(manifest_path.read_text())
    gt = json.loads(gt_path.read_text())
    generated = generate(gt, manifest)
    handwritten = read_jsonl(ROOT / "evals/datasets/handwritten_items.v1.jsonl")
    source_by_id = {x["question_id"]: "Manager (Claude Code), 2026-09-26" for x in handwritten}
    source_by_id.update({x["question_id"]: "generator:cp3-groundtruth.json" for x in generated})
    items = generated + handwritten
    seen = set()
    pins = {e["ticker"]: e for e in manifest["entries"]}
    chunks = {}
    source_chunks_dir = Path(chunks_dir or ROOT / "data/normalized")
    for path in source_chunks_dir.glob("*/chunks.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            chunks[row.get("evidence_id")] = row
    for item in items:
        qid = item.get("question_id")
        if not qid or qid in seen:
            raise ValueError(f"missing or duplicate question_id: {qid!r}")
        seen.add(qid)
        for field in ("question", "question_type", "ticker", "accession_number", "cutoff_date",
                      "expected_answer", "expected_tools", "expected_evidence", "authored_by"):
            if field not in item:
                raise ValueError(f"{qid} missing §9.1 field {field}")
        if item["ticker"] in pins:
            pin = pins[item["ticker"]]
            if item["accession_number"] != pin["accession_number"] or item["cutoff_date"] != pin["filing_date"]:
                raise ValueError(f"{qid} does not match pinned accession/cutoff")
        if item["question_type"] in {"numeric_lookup", "numeric_failure", "mixed", "comparison"}:
            answer = item["expected_answer"]
            validate_ground_truth_keys(item, gt)
            if answer.get("kind") == "mixed" and "numeric" in answer:
                numeric = answer["numeric"]
                value = gt[numeric["ground_truth_key"]]
                if value == "ERROR":
                    raise ValueError(f"{qid} numeric gold points to ERROR")
                numeric.update({"value": value, "tolerance": {"relative": "0.0005", "rounding": "agent-stated precision"}
                                if numeric["unit"] == "USD" else {"absolute": "0.0005"},
                                "display_unit": "USD millions" if numeric["unit"] == "USD" else "percent"})
            if answer.get("kind") == "comparison":
                metric = answer["metric"]
                answer["values"] = [{"key": key, "value": gt[key],
                                     "unit": "USD" if metric in ("revenue", "free_cash_flow") else "ratio"}
                                    for key in answer["ground_truth_keys"]]
        validate_gold_evidence(item, chunks)
        item["authored_by"] = source_by_id[qid]
        item["split"] = "holdout" if int(hashlib.sha256(qid.encode()).hexdigest()[:8], 16) % 3 == 0 else "dev"
        if qid in frozen_splits and frozen_splits[qid] != item["split"]:
            raise ValueError(f"frozen split changed for {qid}: {frozen_splits[qid]} -> {item['split']}")
    items.sort(key=lambda x: x["question_id"])
    serialized = "".join(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for x in items)
    counts_type = dict(sorted(Counter(x["question_type"] for x in items).items()))
    split_counts = {split: dict(sorted(Counter(x["question_type"] for x in items if x["split"] == split).items()))
                    for split in ("dev", "holdout")}
    manifest_out = {
        "dataset_version": "v1", "sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "item_count": len(items), "counts_by_question_type": counts_type,
        "counts_by_split": {s: sum(x["split"] == s for x in items) for s in ("dev", "holdout")},
        "counts_by_type_and_split": split_counts,
        "split_rule": "holdout iff int(sha256(question_id)[:8],16) % 3 == 0; frozen before runs",
        "holdout_policy": "Never used for tuning.",
        "ground_truth_source": {"path": "evals/ground_truth/statement_ground_truth.json", "sha256": digest(gt_path),
                                "method": "read from 10-K statement text"},
        "data_manifest": {"version": manifest["manifest_version"], "path": "data/manifests/filings.v1.json",
                           "sha256": digest(data_manifest_path)},
        "cutoff_policy": "facts/evidence must come from the pinned accession filed on/before cutoff_date",
        "external_items": 0,
        "external_items_note": "External benchmarks (e.g. FinanceBench/FinQA) are deferred: their filings are outside the pinned corpus.",
        "authored_by": {"handwritten_items.v1.jsonl": "Hand-authored; source entries preserved per item",
                        "numeric": "deterministic generator from cp3-groundtruth.json"},
    }
    return items, manifest_out


def main() -> None:
    items, manifest = build()
    DATASET.write_text("".join(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for x in items))
    (DATASET.parent / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"items": len(items), "manifest": manifest}, sort_keys=True))


if __name__ == "__main__":
    main()
