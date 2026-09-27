"""Report builder tests (§7): deterministic rows, typed-error cells recorded, never hidden."""

import pytest

from scripts.metric_report import build_rows


def test_report_rows_aapl_fixture(aapl_facts, aapl_entry):
    rows = build_rows(aapl_entry, aapl_facts)
    assert len(rows) == 18  # 3 fiscal years x 6 metrics
    assert rows == sorted(rows, key=lambda r: (r["fiscal_year"], r["metric"]))
    assert all(row["ticker"] == "AAPL" for row in rows)
    errors = {(r["fiscal_year"], r["metric"]): r["error"]["error_code"] for r in rows if r["error"]}
    # The oldest year has no prior-year comparative and no balance-sheet instant in the pinned filing.
    assert errors == {(2023, "current_ratio"): "fiscal_year_not_in_filing",
                      (2023, "revenue_growth_yoy"): "fiscal_year_not_in_filing"}
    ok_rows = [r for r in rows if r["error"] is None]
    assert len(ok_rows) == 16
    sample = next(r for r in ok_rows if r["metric"] == "revenue" and r["fiscal_year"] == 2025)
    result = sample["result"]
    assert set(result) == {"metric", "formula", "inputs", "result", "result_unit",
                          "display_value", "period_start", "period_end", "evidence_ids"}
    assert result["display_value"] == "$416,161 million"
    assert result["result"] == "416161000000"  # exact Decimal serialized as string


def test_report_rows_msft_fixture(msft_facts, msft_entry):
    rows = build_rows(msft_entry, msft_facts)
    assert len(rows) == 18
    errors = {(r["fiscal_year"], r["metric"]): r["error"]["error_code"] for r in rows if r["error"]}
    assert errors == {(2024, "current_ratio"): "fiscal_year_not_in_filing",
                      (2024, "revenue_growth_yoy"): "fiscal_year_not_in_filing"}
    revenue_2026 = next(r for r in rows if r["metric"] == "revenue" and r["fiscal_year"] == 2026)
    assert revenue_2026["result"]["result"] == "331839000000"
    assert revenue_2026["result"]["display_value"] == "$331,839 million"


def test_report_rows_error_cells_carry_messages(aapl_facts, aapl_entry):
    rows = build_rows(aapl_entry, aapl_facts)
    error_row = next(r for r in rows if r["error"])
    assert set(error_row["error"]) == {"error_code", "message"}
    assert "available" in error_row["error"]["message"]
