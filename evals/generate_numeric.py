"""Deterministically create numeric eval items from hand-read statement-text ground truth."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = "cp6a-numeric-selection-v1"
SELECT = {
    "revenue": (0, -1), "revenue_growth_yoy": (0,), "operating_margin": (0,),
    "net_margin": (0, -2), "free_cash_flow": (0,), "current_ratio": (0,),
}
PROMPTS = {
    "revenue": "What was {company}'s revenue in fiscal {fy}?",
    "revenue_growth_yoy": "What was {company}'s year-over-year revenue growth in fiscal {fy}?",
    "operating_margin": "What was {company}'s operating margin in fiscal {fy}?",
    "net_margin": "What was {company}'s net margin in fiscal {fy}?",
    "free_cash_flow": "What was {company}'s free cash flow in fiscal {fy}?",
    "current_ratio": "What was {company}'s current ratio in fiscal {fy}?",
}
COMPANY = {"MSFT": "Microsoft", "AAPL": "Apple"}
METRIC_TAGS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax"],
    "revenue_growth_yoy": ["RevenueFromContractWithCustomerExcludingAssessedTax"],
    "operating_margin": ["OperatingIncomeLoss", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "net_margin": ["NetIncomeLoss", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "free_cash_flow": ["NetCashProvidedByUsedInOperatingActivities", "PaymentsToAcquirePropertyPlantAndEquipment"],
    "current_ratio": ["AssetsCurrent", "LiabilitiesCurrent"],
}


def period_end(ticker: str, year: int) -> str:
    if ticker == "MSFT":
        return date(year, 6, 30).isoformat()
    day = date(year, 9, 30)
    while day.weekday() != 5:
        day -= timedelta(days=1)
    return day.isoformat()


def generate(groundtruth: dict, manifest: dict) -> list[dict]:
    entries = {e["ticker"]: e for e in manifest["entries"]}
    result = []
    for ticker in sorted(COMPANY):
        entry = entries[ticker]
        current = int(entry["report_date"][:4])
        years = sorted(int(k.split("|")[1]) for k, v in groundtruth.items()
                       if k.startswith(ticker + "|") and k.endswith("|revenue") and v != "ERROR")
        oldest = years[0]
        specs = [(metric, current + offset, False) for metric, offsets in SELECT.items() for offset in offsets]
        specs.append(("revenue_growth_yoy", oldest, True))
        for metric, fy, failure in specs:
            key = f"{ticker}|{fy}|{metric}"
            if key not in groundtruth:
                raise ValueError(f"missing ground-truth key: {key}")
            raw = groundtruth[key]
            if failure != (raw == "ERROR"):
                raise ValueError(f"unexpected ground truth for {key}: {raw!r}")
            unit = "USD" if metric in {"revenue", "free_cash_flow"} else "ratio"
            item = {
                "question_id": f"{ticker.lower()}-num-{metric}-{fy}",
                "question": (f"What was {COMPANY[ticker]}'s revenue growth in fiscal {fy} "
                             "according to the pinned filing?" if failure else
                             PROMPTS[metric].format(company=COMPANY[ticker], fy=fy)),
                "question_type": "numeric_failure" if failure else "numeric_lookup",
                "ticker": ticker, "accession_number": entry["accession_number"], "cutoff_date": entry["filing_date"],
                "expected_tools": ["calculate_financial_metric"],
                "recommended_tools": ["verify_citations"],
                "expected_answer": ({"kind": "refusal", "ground_truth_key": key,
                                     "expected_error_code": "fiscal_year_not_in_filing",
                                     "forbidden": f"any stated FY{fy} growth figure"} if failure else
                                    {"kind": "numeric", "ground_truth_key": key, "metric": metric,
                                     "value": raw, "unit": unit,
                                     "display_unit": "USD millions" if unit == "USD" else
                                                    ("multiple" if metric == "current_ratio" else "percent"),
                                     "tolerance": {"relative": "0.0005", "rounding": "agent-stated precision"}
                                                  if unit == "USD" else {"absolute": "0.0005"}}),
                "expected_evidence": {"tags": [] if failure else METRIC_TAGS[metric],
                                      "period_end": None if failure else period_end(ticker, fy)},
                "authored_by": "generator:cp3-groundtruth.json; " + RULE_VERSION,
                "generation_rule": RULE_VERSION,
            }
            result.append(item)
    return sorted(result, key=lambda x: x["question_id"])


def main() -> None:
    gt_path = ROOT / "evals/ground_truth/statement_ground_truth.json"
    manifest_path = ROOT / "data/manifests/filings.v1.json"
    items = generate(json.loads(gt_path.read_text()), json.loads(manifest_path.read_text()))
    for item in items:
        print(json.dumps(item, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
