#!/usr/bin/env python3
"""Calculate one financial metric from a cache-only pinned companyfacts snapshot."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.compute.metrics import Metric
from app.domain.errors import ResearchError
from app.tools.calculate_financial_metric import calculate_financial_metric


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metric", choices=[metric.value for metric in Metric])
    parser.add_argument("--company", required=True)
    parser.add_argument("--fiscal-year", required=True, type=int)
    parser.add_argument("--accession-number")
    args = parser.parse_args(argv)
    try:
        output = calculate_financial_metric({
            "metric": args.metric, "company": args.company, "fiscal_year": args.fiscal_year,
            "accession_number": args.accession_number,
        })
    except ResearchError as exc:
        print(f"{exc.code}: {exc.message} ({exc.fallback or 'no fallback available'})", file=sys.stderr)
        return 1
    result = output.result
    print(result.display_value)
    print(f"Formula: {result.formula}")
    print("Inputs:")
    for fact in result.inputs:
        period = f"{fact.period_start}..{fact.period_end}" if fact.period_start else f"{fact.period_end} (instant)"
        print(f"- {fact.tag} | {period} | {fact.value} | {fact.accession_number}")
    print(f"Evidence IDs: {', '.join(result.evidence_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
