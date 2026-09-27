"""Metric-engine tests: by-hand math on real values, guards, display rules, provenance.

Expected results are computed by hand inside this file from the literal fixture
values (an independent `Context(prec=28, ROUND_HALF_EVEN)`), never by calling the
implementation a second time.
"""

from datetime import date
from decimal import Context, Decimal, ROUND_HALF_EVEN

import pytest

import app.compute.metrics as metrics_module
from app.compute.fact_selection import FactSelection
from app.compute.metrics import (METRIC_FORMULAS, Metric, calculate_metric,
                                  _display_multiple, _display_percent, _display_usd)
from app.compute.tag_aliases import CONCEPTS, Concept
from app.domain.errors import (FiscalYearNotInFilingError, NegativeBaseError, PeriodMismatchError,
                               UnitMismatchError, UnknownTickerError, UnsupportedMetricError,
                               ZeroDenominatorError)
from app.domain.financial_facts import FinancialFact, fact_id, normalize_companyfacts
from tests.unit.conftest import add_fact, modified

HAND = Context(prec=28, rounding=ROUND_HALF_EVEN)

# Literal pinned values (from the real fixtures; see tests/fixtures/companyfacts/*_subset.json).
AAPL = {
    "revenue": {2023: 383285000000, 2024: 391035000000, 2025: 416161000000},
    "operating_income": {2023: 114301000000, 2024: 123216000000, 2025: 133050000000},
    "net_income": {2023: 96995000000, 2024: 93736000000, 2025: 112010000000},
    "operating_cash_flow": {2023: 110543000000, 2024: 118254000000, 2025: 111482000000},
    "capex": {2023: 10959000000, 2024: 9447000000, 2025: 12715000000},
    "current_assets": {2024: 152987000000, 2025: 147957000000},
    "current_liabilities": {2024: 176392000000, 2025: 165631000000},
}
MSFT = {
    "revenue": {2024: 245122000000, 2025: 281724000000, 2026: 331839000000},
    "operating_income": {2024: 109433000000, 2025: 128528000000, 2026: 155237000000},
    "net_income": {2024: 88136000000, 2025: 101832000000, 2026: 133749000000},
    "operating_cash_flow": {2024: 118548000000, 2025: 136162000000, 2026: 182935000000},
    "capex": {2024: 44477000000, 2025: 64551000000, 2026: 115948000000},
    "current_assets": {2025: 191131000000, 2026: 207710000000},
    "current_liabilities": {2025: 141218000000, 2026: 168825000000},
}


def compute(facts, entry, metric, year):
    return calculate_metric(metric, ticker=entry.ticker, fiscal_year=year, facts=facts, pinned=entry)


# ---------------------------------------------------------------- §6.11 metric math on real values, by hand


def test_aapl_all_metrics_all_years_by_hand(aapl_facts, aapl_entry):
    rev = AAPL["revenue"]
    fcf = {y: HAND.subtract(Decimal(AAPL["operating_cash_flow"][y]), Decimal(AAPL["capex"][y]))
           for y in (2023, 2024, 2025)}

    r = compute(aapl_facts, aapl_entry, Metric.REVENUE, 2025)
    assert r.result == Decimal(rev[2025]) and r.display_value == "$416,161 million"
    assert (r.period_start, r.period_end) == (date(2024, 9, 29), date(2025, 9, 27))
    r = compute(aapl_facts, aapl_entry, Metric.REVENUE, 2024)
    assert r.result == Decimal(rev[2024]) and r.display_value == "$391,035 million"
    r = compute(aapl_facts, aapl_entry, Metric.REVENUE, 2023)
    assert r.result == Decimal(rev[2023]) and r.display_value == "$383,285 million"

    r = compute(aapl_facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2025)
    assert r.result == HAND.divide(HAND.subtract(Decimal(rev[2025]), Decimal(rev[2024])), Decimal(rev[2024]))
    assert r.display_value == "6.4%"
    r = compute(aapl_facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2024)
    assert r.result == HAND.divide(HAND.subtract(Decimal(rev[2024]), Decimal(rev[2023])), Decimal(rev[2023]))
    assert r.display_value == "2.0%"

    r = compute(aapl_facts, aapl_entry, Metric.OPERATING_MARGIN, 2025)
    assert r.result == HAND.divide(Decimal(AAPL["operating_income"][2025]), Decimal(rev[2025]))
    assert r.display_value == "32.0%"
    r = compute(aapl_facts, aapl_entry, Metric.OPERATING_MARGIN, 2024)
    assert r.result == HAND.divide(Decimal(AAPL["operating_income"][2024]), Decimal(rev[2024]))
    assert r.display_value == "31.5%"
    r = compute(aapl_facts, aapl_entry, Metric.OPERATING_MARGIN, 2023)
    assert r.result == HAND.divide(Decimal(AAPL["operating_income"][2023]), Decimal(rev[2023]))
    assert r.display_value == "29.8%"

    r = compute(aapl_facts, aapl_entry, Metric.NET_MARGIN, 2025)
    assert r.result == HAND.divide(Decimal(AAPL["net_income"][2025]), Decimal(rev[2025]))
    assert r.display_value == "26.9%"
    r = compute(aapl_facts, aapl_entry, Metric.NET_MARGIN, 2024)
    assert r.result == HAND.divide(Decimal(AAPL["net_income"][2024]), Decimal(rev[2024]))
    assert r.display_value == "24.0%"
    r = compute(aapl_facts, aapl_entry, Metric.NET_MARGIN, 2023)
    assert r.result == HAND.divide(Decimal(AAPL["net_income"][2023]), Decimal(rev[2023]))
    assert r.display_value == "25.3%"

    for year, display in ((2025, "$98,767 million"), (2024, "$108,807 million"), (2023, "$99,584 million")):
        r = compute(aapl_facts, aapl_entry, Metric.FREE_CASH_FLOW, year)
        assert r.result == fcf[year] and r.display_value == display and r.result_unit == "USD"

    r = compute(aapl_facts, aapl_entry, Metric.CURRENT_RATIO, 2025)
    assert r.result == HAND.divide(Decimal(AAPL["current_assets"][2025]), Decimal(AAPL["current_liabilities"][2025]))
    assert r.display_value == "0.89x" and (r.period_start, r.period_end) == (None, date(2025, 9, 27))
    r = compute(aapl_facts, aapl_entry, Metric.CURRENT_RATIO, 2024)
    assert r.result == HAND.divide(Decimal(AAPL["current_assets"][2024]), Decimal(AAPL["current_liabilities"][2024]))
    assert r.display_value == "0.87x"


def test_msft_all_metrics_all_years_by_hand(msft_facts, msft_entry):
    rev = MSFT["revenue"]
    fcf = {y: HAND.subtract(Decimal(MSFT["operating_cash_flow"][y]), Decimal(MSFT["capex"][y]))
           for y in (2024, 2025, 2026)}

    assert compute(msft_facts, msft_entry, Metric.REVENUE, 2026).result == Decimal(rev[2026])
    assert compute(msft_facts, msft_entry, Metric.REVENUE, 2026).display_value == "$331,839 million"
    assert compute(msft_facts, msft_entry, Metric.REVENUE, 2025).result == Decimal(rev[2025])
    assert compute(msft_facts, msft_entry, Metric.REVENUE, 2024).result == Decimal(rev[2024])

    r = compute(msft_facts, msft_entry, Metric.REVENUE_GROWTH_YOY, 2026)
    assert r.result == HAND.divide(HAND.subtract(Decimal(rev[2026]), Decimal(rev[2025])), Decimal(rev[2025]))
    assert r.display_value == "17.8%"
    r = compute(msft_facts, msft_entry, Metric.REVENUE_GROWTH_YOY, 2025)
    assert r.result == HAND.divide(HAND.subtract(Decimal(rev[2025]), Decimal(rev[2024])), Decimal(rev[2024]))
    assert r.display_value == "14.9%"

    r = compute(msft_facts, msft_entry, Metric.OPERATING_MARGIN, 2026)
    assert r.result == HAND.divide(Decimal(MSFT["operating_income"][2026]), Decimal(rev[2026]))
    assert r.display_value == "46.8%"
    r = compute(msft_facts, msft_entry, Metric.OPERATING_MARGIN, 2025)
    assert r.result == HAND.divide(Decimal(MSFT["operating_income"][2025]), Decimal(rev[2025]))
    assert r.display_value == "45.6%"
    r = compute(msft_facts, msft_entry, Metric.OPERATING_MARGIN, 2024)
    assert r.result == HAND.divide(Decimal(MSFT["operating_income"][2024]), Decimal(rev[2024]))
    assert r.display_value == "44.6%"

    r = compute(msft_facts, msft_entry, Metric.NET_MARGIN, 2026)
    assert r.result == HAND.divide(Decimal(MSFT["net_income"][2026]), Decimal(rev[2026]))
    assert r.display_value == "40.3%"
    r = compute(msft_facts, msft_entry, Metric.NET_MARGIN, 2025)
    assert r.result == HAND.divide(Decimal(MSFT["net_income"][2025]), Decimal(rev[2025]))
    assert r.display_value == "36.1%"
    r = compute(msft_facts, msft_entry, Metric.NET_MARGIN, 2024)
    assert r.result == HAND.divide(Decimal(MSFT["net_income"][2024]), Decimal(rev[2024]))
    assert r.display_value == "36.0%"

    for year, display in ((2026, "$66,987 million"), (2025, "$71,611 million"), (2024, "$74,071 million")):
        r = compute(msft_facts, msft_entry, Metric.FREE_CASH_FLOW, year)
        assert r.result == fcf[year] and r.display_value == display

    r = compute(msft_facts, msft_entry, Metric.CURRENT_RATIO, 2026)
    assert r.result == HAND.divide(Decimal(MSFT["current_assets"][2026]), Decimal(MSFT["current_liabilities"][2026]))
    assert r.display_value == "1.23x"
    r = compute(msft_facts, msft_entry, Metric.CURRENT_RATIO, 2025)
    assert r.result == HAND.divide(Decimal(MSFT["current_assets"][2025]), Decimal(MSFT["current_liabilities"][2025]))
    assert r.display_value == "1.35x"


def test_growth_result_is_unrounded_full_precision(aapl_facts, aapl_entry):
    r = compute(aapl_facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2025)
    assert r.result == Decimal("0.06425511782832738757400232716")  # 28 significant digits, not rounded


# ---------------------------------------------------------------- §6.9 unavailable years (metric level)


def test_growth_and_current_ratio_fail_for_oldest_year(aapl_facts, aapl_entry):
    with pytest.raises(FiscalYearNotInFilingError) as growth:
        compute(aapl_facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2023)
    assert growth.value.code == "fiscal_year_not_in_filing"
    assert growth.value.available_years == [2023, 2024, 2025]  # FY2022 is outside the pinned filing
    with pytest.raises(FiscalYearNotInFilingError) as ratio:
        compute(aapl_facts, aapl_entry, Metric.CURRENT_RATIO, 2023)
    assert ratio.value.available_years == [2024, 2025]  # only two balance-sheet instants in the filing


def test_msft_oldest_year_growth_and_current_ratio_fail(msft_facts, msft_entry):
    with pytest.raises(FiscalYearNotInFilingError):
        compute(msft_facts, msft_entry, Metric.REVENUE_GROWTH_YOY, 2024)
    with pytest.raises(FiscalYearNotInFilingError):
        compute(msft_facts, msft_entry, Metric.CURRENT_RATIO, 2024)


# ---------------------------------------------------------------- §6.12 zero / negative guards


def _edit_pinned(raw, tag, *, accn, start=None, end, val):
    """Edit the value of the one pinned-accession fact matching the period (modified copy)."""
    units = raw["facts"]["us-gaap"][tag]["units"]["USD"]
    matches = [f for f in units if f.get("start") == start and f["end"] == end and f["accn"] == accn]
    assert len(matches) == 1, f"expected exactly one pinned fact for {tag} {start}->{end}"
    matches[0]["val"] = val


def test_zero_revenue_denominator_raises(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    _edit_pinned(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
                 start="2024-09-29", end="2025-09-27", val=0)
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(ZeroDenominatorError) as excinfo:
        compute(facts, aapl_entry, Metric.OPERATING_MARGIN, 2025)
    assert excinfo.value.code == "zero_denominator" and excinfo.value.denominator == "revenue"
    with pytest.raises(ZeroDenominatorError):
        compute(facts, aapl_entry, Metric.NET_MARGIN, 2025)


def test_zero_growth_base_raises(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    _edit_pinned(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
                 start="2023-10-01", end="2024-09-28", val=0)
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(ZeroDenominatorError):
        compute(facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2025)


def test_negative_growth_base_raises(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    _edit_pinned(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
                 start="2023-10-01", end="2024-09-28", val=-391035000000)
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(NegativeBaseError) as excinfo:
        compute(facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2025)
    assert excinfo.value.code == "negative_base" and excinfo.value.base == "revenue[FY-1]"


def test_zero_and_negative_current_liabilities_raise(aapl_raw, aapl_entry):
    for value, error in ((0, ZeroDenominatorError), (-165631000000, NegativeBaseError)):
        raw = modified(aapl_raw)
        _edit_pinned(raw, "LiabilitiesCurrent", accn=aapl_entry.accession_number,
                     end="2025-09-27", val=value)
        facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
        with pytest.raises(error):
            compute(facts, aapl_entry, Metric.CURRENT_RATIO, 2025)


# ---------------------------------------------------------------- §6.7 mixed units (engine guard)


def test_calculate_metric_mixed_units_raises(monkeypatch, aapl_raw, aapl_entry):
    # Simulate a registry whose capex concept is denominated in USD/shares (a non-USD concept
    # would be the only way two inputs could carry different units into the engine).
    spec = CONCEPTS[Concept.CAPEX].model_copy(update={"unit": "USD/shares"})
    monkeypatch.setitem(CONCEPTS, Concept.CAPEX, spec)
    raw = modified(aapl_raw)
    add_fact(raw, "PaymentsToAcquirePropertyPlantAndEquipment", unit="USD/shares",
             accn=aapl_entry.accession_number, start="2024-09-29", end="2025-09-27", val=12.75,
             form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(UnitMismatchError) as excinfo:
        compute(facts, aapl_entry, Metric.FREE_CASH_FLOW, 2025)
    assert sorted(excinfo.value.units) == ["USD", "USD/shares"]


def test_period_mismatch_guard_raises(monkeypatch, aapl_entry):
    # Defensive guard: selection always resolves one period per year, so this is only
    # reachable if select_fact ever returns inputs from different periods.
    def synthetic_fact(tag, start, end):
        return FinancialFact(ticker="AAPL", cik="0000320193", taxonomy="us-gaap", tag=tag, label=None,
                             value=Decimal(1), unit="USD", period_start=start, period_end=end,
                             fiscal_year=None, fiscal_period=None, form="10-K", filed_at=date(2025, 10, 31),
                             accession_number=aapl_entry.accession_number, frame=None,
                             source_url=str(aapl_entry.source_url))

    def selection(fact, start, end):
        return FactSelection(concept=Concept.OPERATING_INCOME, ticker="AAPL", fiscal_year=2025,
                             period_start=start, period_end=end, selected=fact, candidates=[])

    selections = iter([
        selection(synthetic_fact("OperatingIncomeLoss", date(2024, 9, 29), date(2025, 9, 27)),
                   date(2024, 9, 29), date(2025, 9, 27)),
        selection(synthetic_fact("RevenueFromContractWithCustomerExcludingAssessedTax", None, date(2025, 9, 27)),
                   None, date(2025, 9, 27)),
    ])
    monkeypatch.setattr(metrics_module, "select_fact", lambda *args, **kwargs: next(selections))
    with pytest.raises(PeriodMismatchError):
        calculate_metric(Metric.OPERATING_MARGIN, ticker="AAPL", fiscal_year=2025, facts=[], pinned=aapl_entry)


# ---------------------------------------------------------------- §6.13 determinism / provenance


def test_result_provenance_fields(aapl_facts, aapl_entry):
    r = compute(aapl_facts, aapl_entry, Metric.OPERATING_MARGIN, 2025)
    assert r.formula == METRIC_FORMULAS[Metric.OPERATING_MARGIN] == "operating_income / revenue"
    assert [f.tag for f in r.inputs] == ["OperatingIncomeLoss", "RevenueFromContractWithCustomerExcludingAssessedTax"]
    assert all(f.accession_number == aapl_entry.accession_number for f in r.inputs)
    assert r.evidence_ids == [fact_id(f) for f in r.inputs]
    assert (r.period_start, r.period_end) == (date(2024, 9, 29), date(2025, 9, 27))
    assert r.metric == "operating_margin" and r.result_unit == "ratio"


def test_growth_inputs_are_current_then_prior(aapl_facts, aapl_entry):
    r = compute(aapl_facts, aapl_entry, Metric.REVENUE_GROWTH_YOY, 2025)
    assert [f.period_end for f in r.inputs] == [date(2025, 9, 27), date(2024, 9, 28)]
    assert all(f.accession_number == aapl_entry.accession_number for f in r.inputs)
    assert (r.period_start, r.period_end) == (date(2024, 9, 29), date(2025, 9, 27))


def test_metric_results_are_deterministic(aapl_facts, aapl_entry):
    for metric in Metric:
        first = compute(aapl_facts, aapl_entry, metric, 2025)
        second = compute(aapl_facts, aapl_entry, metric, 2025)
        assert first == second


def test_snapshot_noise_does_not_change_metric_results(aapl_raw, aapl_entry):
    noisy = modified(aapl_raw)
    add_fact(noisy, "RevenueFromContractWithCustomerExcludingAssessedTax", accn="0000320193-26-000001",
             start="2023-10-01", end="2024-09-28", val=1, form="10-K", filed="2026-11-01", fy=2026, fp="FY")
    base = normalize_companyfacts(aapl_raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    snapshot = normalize_companyfacts(noisy, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    for metric in (Metric.REVENUE, Metric.REVENUE_GROWTH_YOY, Metric.CURRENT_RATIO):
        assert compute(base, aapl_entry, metric, 2025) == compute(snapshot, aapl_entry, metric, 2025)


# ---------------------------------------------------------------- §6.14 unknown metric / unknown ticker


def test_unknown_metric_string_raises(aapl_facts, aapl_entry):
    with pytest.raises(UnsupportedMetricError):
        calculate_metric("bogus_metric", ticker="AAPL", fiscal_year=2025, facts=aapl_facts, pinned=aapl_entry)
    with pytest.raises(UnsupportedMetricError):
        calculate_metric("revenue", ticker="AAPL", fiscal_year=2025, facts=aapl_facts, pinned=aapl_entry)


def test_unknown_ticker_raises(aapl_facts, aapl_entry):
    with pytest.raises(UnknownTickerError):
        calculate_metric(Metric.REVENUE, ticker="TSLA", fiscal_year=2025, facts=aapl_facts, pinned=aapl_entry)


# ---------------------------------------------------------------- §6.15 no float in the value path


def test_results_are_decimal_never_float(aapl_facts, aapl_entry):
    for metric in Metric:
        r = compute(aapl_facts, aapl_entry, metric, 2025)
        assert type(r.result) is Decimal and not isinstance(r.result, float)
        assert all(type(f.value) is Decimal for f in r.inputs)


# ---------------------------------------------------------------- display rules (§5)


def test_display_usd_rules():
    assert _display_usd(Decimal(331839000000)) == "$331,839 million"
    assert _display_usd(Decimal(9447000000)) == "$9,447 million"
    assert _display_usd(Decimal("12345678901")) == "$12,345.7 million"  # not a whole million: one decimal
    assert _display_usd(Decimal("-1234000000")) == "-$1,234 million"


def test_display_percent_rules():
    assert _display_percent(Decimal("0.345")) == "34.5%"
    assert _display_percent(Decimal("0.1235")) == "12.4%"  # 12.35 -> half-even -> 12.4
    assert _display_percent(Decimal("0.1234")) == "12.3%"
    assert _display_percent(Decimal("1")) == "100.0%"
    assert _display_percent(Decimal("-0.0000001")) == "0.0%"  # no negative zero
    assert _display_percent(Decimal("-0.1255")) == "-12.6%"


def test_display_multiple_rules():
    assert _display_multiple(Decimal("1.3534464")) == "1.35x"
    assert _display_multiple(Decimal("0.8932929")) == "0.89x"
    assert _display_multiple(Decimal("1.345")) == "1.34x"  # tie -> half-even -> 1.34
    assert _display_multiple(Decimal("1.355")) == "1.36x"  # tie -> half-even -> 1.36


def test_formulas_are_fixed_strings():
    assert METRIC_FORMULAS[Metric.REVENUE] == "revenue"
    assert METRIC_FORMULAS[Metric.REVENUE_GROWTH_YOY] == "(revenue[FY] - revenue[FY-1]) / revenue[FY-1]"
    assert METRIC_FORMULAS[Metric.FREE_CASH_FLOW] == "operating_cash_flow - capex"
    assert METRIC_FORMULAS[Metric.CURRENT_RATIO] == "current_assets / current_liabilities"
