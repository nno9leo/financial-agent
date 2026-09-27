#!/usr/bin/env python3
"""Real-data report: every metric x fiscal years [current-2 .. current] for both pinned filings.

Cache-only: the pinned companyfacts payloads are read with `SecProvider.get_cached`
(a cache miss is a hard error; this script never fetches). The output
`artifacts/metrics/metric_report.json` is deterministic (sorted, no timestamps) and holds
one row per (ticker, fiscal_year, metric) with either the full `MetricResult` or
`{error_code, message}`. Typed errors are shown, never hidden or special-cased.
"""

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compute.metrics import Metric, calculate_metric
from app.config import Settings
from app.domain.errors import CompanyFactsNotCachedError, ResearchError
from app.domain.financial_facts import normalize_companyfacts
from app.ingestion.manifest import latest_manifest_path, load_manifest
from app.providers.sec import SecProvider

ARTIFACT_PATH = ROOT / "artifacts/metrics/metric_report.json"


def build_rows(entry, facts) -> list[dict]:
    """Deterministic rows for one filing: every metric for fiscal years current-2..current."""
    current = date.fromisoformat(entry.report_date).year
    rows = []
    for fiscal_year in range(current - 2, current + 1):
        for metric in sorted(Metric, key=lambda m: m.value):
            row = {"ticker": entry.ticker, "fiscal_year": fiscal_year, "metric": metric.value,
                   "result": None, "error": None}
            try:
                result = calculate_metric(metric, ticker=entry.ticker, fiscal_year=fiscal_year,
                                          facts=facts, pinned=entry)
                row["result"] = result.model_dump(mode="json")
            except ResearchError as exc:
                row["error"] = {"error_code": exc.code, "message": exc.message}
            rows.append(row)
    return rows


def print_table(rows) -> None:
    print(f"{'ticker':6} {'FY':5} {'metric':20} {'value / error'}")
    for row in rows:
        if row["error"] is not None:
            shown = f"ERROR {row['error']['error_code']}"
        else:
            shown = row["result"]["display_value"]
        print(f"{row['ticker']:6} {row['fiscal_year']:<5} {row['metric']:20} {shown}")


def main() -> int:
    settings = Settings(_env_file=None, data_dir=ROOT / "data")
    manifest = load_manifest(latest_manifest_path(ROOT / "data/manifests"))
    provider = SecProvider(settings)
    rows = []
    try:
        for entry in manifest.entries:
            cached = provider.get_cached(entry.companyfacts_url)
            if cached is None:
                raise CompanyFactsNotCachedError(entry.companyfacts_url)
            body, _meta = cached
            facts = normalize_companyfacts(body, ticker=entry.ticker, cik=entry.cik, pinned=entry)
            rows.extend(build_rows(entry, facts))
    finally:
        provider.close()
    rows.sort(key=lambda r: (r["ticker"], r["fiscal_year"], r["metric"]))

    artifact = {
        "artifact": "metric-report",
        "manifest_version": manifest.manifest_version,
        "pinned_accessions": {e.ticker: e.accession_number for e in sorted(manifest.entries, key=lambda e: e.ticker)},
        "rows": rows,
    }
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print_table(rows)
    ok = sum(1 for r in rows if r["error"] is None)
    print(f"SUMMARY {ok} result cells, {len(rows) - ok} typed-error cells -> {ARTIFACT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
