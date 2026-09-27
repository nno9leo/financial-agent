#!/usr/bin/env python3
"""Recompute eval grades offline while preserving raw records and prior grades."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.graders import GRADER_VERSION, grade_item, summarize


CHANGES = [
    "G1: scaled numeric assertion matching",
    "G2: target-aware refusal figures under ADR 0011 rules 1-3",
    "G3: expected_error_code fallback for refusal evidence",
    "G4: separate run-level runtime errors from tool errors",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unavailable"


def _jsonl(data: bytes, label: str) -> list[dict]:
    rows = []
    for index, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"invalid JSON in {label} line {index}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"expected object in {label} line {index}")
        rows.append(value)
    return rows


def _raw(rows: list[dict]) -> list[dict]:
    return [{key: value for key, value in row.items() if key != "grades"} for row in rows]


def _history(marker_path: Path) -> list[dict]:
    if not marker_path.exists():
        return []
    data = json.loads(marker_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        if not all(isinstance(entry, dict) for entry in data):
            raise ValueError("regrade.json history must contain objects")
        return data
    if not isinstance(data, dict):
        raise ValueError("regrade.json must be a history list")
    # Migrate the existing v1 -> v2 marker to the append-only history shape.
    return [{
        "from": data.get("from", "v1"),
        "to": data.get("to", data.get("grader_version", "v2")),
        "sha": data.get("sha", data.get("grader_git_sha", "unavailable")),
        "changes": data.get("changes", []),
        "per_item_changes": data.get("per_item_changes", []),
        **({"regraded_at": data["regraded_at"]} if data.get("regraded_at") else {}),
    }]


def _version_backup(run_dir: Path, version: str, current_results: bytes,
                    current_summary: bytes) -> tuple[bytes, bytes]:
    results_backup = run_dir / f"results.grader_{version}.jsonl"
    summary_backup = run_dir / f"summary.grader_{version}.json"
    for path, current in ((results_backup, current_results), (summary_backup, current_summary)):
        if path.exists():
            if path.read_bytes() != current:
                raise FileExistsError(f"existing {path.name} differs from current grader-{version} records")
        else:
            path.write_bytes(current)
    result_bytes, summary_bytes = results_backup.read_bytes(), summary_backup.read_bytes()
    if _raw(_jsonl(result_bytes, str(results_backup))) != _raw(_jsonl(current_results, "current results")):
        raise ValueError(f"raw result fields differ from preserved grader-{version} records")
    return result_bytes, summary_bytes


def regrade_run(run_id: str, *, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("run-id must be a simple artifact directory name")
    run_dir = root / "artifacts" / "evals" / run_id
    results_path, summary_path, manifest_path = (run_dir / "results.jsonl",
                                                 run_dir / "summary.json",
                                                 run_dir / "dataset_manifest.json")
    if not all(path.is_file() for path in (results_path, summary_path, manifest_path)):
        raise FileNotFoundError("run must contain results.jsonl, summary.json, and dataset_manifest.json")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("dataset_version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", version):
        raise ValueError("dataset manifest has an invalid dataset_version")
    dataset_bytes = (root / "evals" / "datasets" / f"questions.{version}.jsonl").read_bytes()
    if _sha256(dataset_bytes) != manifest.get("sha256"):
        raise ValueError("dataset sha256 does not match the run's dataset_manifest.json")

    current_results = results_path.read_bytes()
    current_summary = summary_path.read_bytes()
    summary_data = json.loads(current_summary)
    current_version = summary_data.get("grader_version", "v1")
    marker_path = run_dir / "regrade.json"
    history = _history(marker_path)

    if current_version == GRADER_VERSION:
        if not history or history[-1].get("to") != GRADER_VERSION:
            raise ValueError("summary is already at the current grader but regrade history is missing")
        previous_version = history[-1].get("from")
        previous_results = run_dir / f"results.grader_{previous_version}.jsonl"
        if not previous_results.is_file():
            raise FileNotFoundError("current regrade history points to a missing previous-version backup")
        if _raw(_jsonl(previous_results.read_bytes(), str(previous_results))) != _raw(
                _jsonl(current_results, str(results_path))):
            raise ValueError("current raw result fields differ from preserved records")
        return run_dir

    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(current_version)):
        raise ValueError("current summary has an invalid grader_version")
    previous_results_bytes, _ = _version_backup(run_dir, str(current_version),
                                                  current_results, current_summary)
    previous_rows = _jsonl(previous_results_bytes, str(run_dir / f"results.grader_{current_version}.jsonl"))
    rows = _jsonl(current_results, str(results_path))
    if _raw(previous_rows) != _raw(rows):
        raise ValueError("raw result fields changed before regrading")
    previous_by_id = {row.get("question_id"): row for row in previous_rows}
    dataset = {row["question_id"]: row for row in _jsonl(dataset_bytes, str(root / "evals" / "datasets" / f"questions.{version}.jsonl"))}

    per_item_changes = []
    for row in rows:
        question_id = row.get("question_id")
        if question_id not in dataset:
            raise ValueError(f"run contains question_id absent from dataset: {question_id}")
        updated = grade_item(dataset[question_id], row)
        old_grades = previous_by_id.get(question_id, {}).get("grades", {})
        for field in sorted(set(old_grades) | set(updated)):
            old, new = old_grades.get(field), updated.get(field)
            if old != new:
                per_item_changes.append({"question_id": question_id, "field": field,
                                         "from": old, "to": new})
        row["grades"] = updated

    if _raw(rows) != _raw(previous_rows):
        raise AssertionError("regrade modified raw result fields")
    if set(row.get("question_id") for row in rows) != set(previous_by_id):
        raise ValueError("current and preserved results contain different question IDs")

    config_path = run_dir / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    new_summary = summarize(rows, agent=config.get("agent", "pi"))
    new_summary["grader_version"] = GRADER_VERSION
    serialized_results = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                                  for row in rows).encode("utf-8")
    if _raw(_jsonl(serialized_results, "regraded results")) != _raw(previous_rows):
        raise AssertionError("non-grade fields changed after results.jsonl JSON round-trip")

    entry = {
        "from": str(current_version),
        "to": GRADER_VERSION,
        "sha": _git_sha(root),
        "changes": CHANGES,
        "per_item_changes": per_item_changes,
        "regraded_at": datetime.now(timezone.utc).isoformat(),
    }
    history.append(entry)
    results_path.write_bytes(serialized_results)
    summary_path.write_text(json.dumps(new_summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    marker_path.write_text(json.dumps(history, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    readme_path = run_dir / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else "# Eval run\n"
    line = (f"Regraded offline with grader {GRADER_VERSION}; previous grader {current_version} "
            f"grades and summary are preserved in results.grader_{current_version}.jsonl and "
            f"summary.grader_{current_version}.json.")
    if line not in readme:
        readme_path.write_text(readme.rstrip() + "\n\n" + line + "\n", encoding="utf-8")
    return run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    run_dir = regrade_run(args.run_id)
    print(f"Regraded {run_dir} with grader {GRADER_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
