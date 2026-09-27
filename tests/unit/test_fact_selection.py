"""Fact-selection tests: period resolution, pinning, aliases, ambiguity, rejections (§6.1-§6.10, §6.13, §6.14)."""

from datetime import date
from decimal import Decimal

import pytest

from app.compute.fact_selection import (FactSelection, is_annual_duration, resolve_fiscal_period,
                                         select_fact)
from app.compute.tag_aliases import CONCEPTS, Concept
from app.domain.errors import (AmbiguousFactError, FiscalCalendarMismatchError, FiscalYearNotInFilingError,
                               MissingFactError, UnknownTickerError)
from app.domain.financial_facts import normalize_companyfacts
from tests.unit.conftest import add_fact, modified, pinned_facts_of

AAPL_FY2023 = (date(2022, 9, 25), date(2023, 9, 30))
AAPL_FY2024 = (date(2023, 10, 1), date(2024, 9, 28))
AAPL_FY2025 = (date(2024, 9, 29), date(2025, 9, 27))


def select(aapl_facts, concept, year, entry, **kwargs):
    return select_fact(aapl_facts, concept, ticker=entry.ticker, fiscal_year=year, pinned=entry, **kwargs)


# ---------------------------------------------------------------- §6.1 the `fy` trap


def test_fy_trap_selects_by_period_not_by_fy(aapl_facts, aapl_entry):
    selection = select(aapl_facts, Concept.REVENUE, 2023, aapl_entry)
    assert (selection.period_start, selection.period_end) == AAPL_FY2023
    assert selection.selected.value == Decimal(383285000000)
    # The selected fact's own `fy` is the FILING's label (2025), not the period's year: proof `fy` was ignored.
    assert selection.selected.fiscal_year == 2025 and selection.selected.fiscal_period == "FY"
    current = select(aapl_facts, Concept.REVENUE, 2025, aapl_entry)
    assert current.selected.value == Decimal(416161000000)


def test_frame_is_never_used_for_selection(aapl_facts, aapl_entry):
    # AAPL FY2023 carries frame CY2023 while ending 2023-09-30; MSFT FY2026 ends 2026-06-30 with frame CY2026.
    selection = select(aapl_facts, Concept.REVENUE, 2023, aapl_entry)
    assert selection.selected.frame == "CY2023"  # present but irrelevant: period dates decided the match


# ---------------------------------------------------------------- §6.2 accession pinning


def test_other_accession_facts_rejected_for_same_period(aapl_facts, aapl_entry):
    selection = select(aapl_facts, Concept.REVENUE, 2023, aapl_entry)
    assert selection.selected.accession_number == aapl_entry.accession_number
    same_period_candidates = [c for c in selection.candidates
                               if c.period_start == AAPL_FY2023[0] and c.period_end == AAPL_FY2023[1]
                               and c.accession_number != aapl_entry.accession_number]
    assert same_period_candidates  # the comparative is re-reported by later filings
    assert all(c.decision == "rejected" and c.reason == "other_accession" for c in same_period_candidates)
    forms = {c.form for c in selection.candidates if c.reason == "other_accession"}
    assert {"10-K", "10-Q", "8-K", "10-K/A"} <= forms  # real data: every form family is present and rejected


def test_conflicting_restated_value_from_other_accession_is_rejected(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn="0000320193-26-000001",
             start=AAPL_FY2025[0].isoformat(), end=AAPL_FY2025[1].isoformat(), val=999999999999,
             form="10-K", filed="2026-10-31", fy=2026, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    assert selection.selected.value == Decimal(416161000000)
    restated = [c for c in selection.candidates if c.accession_number == "0000320193-26-000001"]
    assert restated and all(c.reason == "other_accession" for c in restated)


def test_pinned_amendment_form_rejected(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
             start=AAPL_FY2025[0].isoformat(), end=AAPL_FY2025[1].isoformat(), val=416161000000,
             form="10-K/A", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    amendment = [c for c in selection.candidates if c.form == "10-K/A"]
    assert amendment and amendment[0].decision == "rejected"
    assert amendment[0].reason == "amendment_or_wrong_form"
    assert selection.selected.form == "10-K"


# ---------------------------------------------------------------- §6.3 alias fallback


def test_older_alias_tags_have_no_pinned_facts(aapl_facts, aapl_entry):
    selection = select(aapl_facts, Concept.REVENUE, 2025, aapl_entry)
    revenues = [c for c in selection.candidates if c.tag == "Revenues"]
    sales = [c for c in selection.candidates if c.tag == "SalesRevenueNet"]
    assert revenues and sales  # considered as alias candidates
    assert all(c.reason == "other_accession" for c in revenues + sales)
    assert selection.selected.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_alias_fallback_when_primary_tag_absent(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    del raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]
    add_fact(raw, "Revenues", accn=aapl_entry.accession_number, start=AAPL_FY2025[0].isoformat(),
             end=AAPL_FY2025[1].isoformat(), val=416161000000, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    assert selection.selected.tag == "Revenues"
    assert selection.selected.value == Decimal(416161000000)


# ---------------------------------------------------------------- §6.4 alias agreement/disagreement


def test_alias_disagreement_raises(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "Revenues", accn=aapl_entry.accession_number, start=AAPL_FY2025[0].isoformat(),
             end=AAPL_FY2025[1].isoformat(), val=42, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(AmbiguousFactError) as excinfo:
        select(facts, Concept.REVENUE, 2025, aapl_entry)
    assert excinfo.value.code == "ambiguous_fact"
    assert any(getattr(f, "tag", None) == "Revenues" for f in excinfo.value.candidates)


def test_alias_agreement_with_equal_value_recorded_not_raised(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "Revenues", accn=aapl_entry.accession_number, start=AAPL_FY2025[0].isoformat(),
             end=AAPL_FY2025[1].isoformat(), val=416161000000, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    assert selection.selected.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    alias = [c for c in selection.candidates if c.tag == "Revenues" and c.accession_number == aapl_entry.accession_number]
    assert alias and alias[0].decision == "duplicate_of_selected" and alias[0].reason is None


# ---------------------------------------------------------------- §6.5 same-tag ambiguity


def test_same_tag_two_values_same_period_raises(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
             start=AAPL_FY2025[0].isoformat(), end=AAPL_FY2025[1].isoformat(), val=1,
             form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(AmbiguousFactError) as excinfo:
        select(facts, Concept.REVENUE, 2025, aapl_entry)
    conflicting = excinfo.value.candidates
    assert {f.value for f in conflicting} == {Decimal(416161000000), Decimal(1)}


def test_identical_duplicate_fact_is_recorded_not_ambiguous(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
             start=AAPL_FY2025[0].isoformat(), end=AAPL_FY2025[1].isoformat(), val=416161000000,
             form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    winners = [c for c in selection.candidates if c.decision in ("selected", "duplicate_of_selected")]
    assert len(winners) == 2
    assert sum(c.decision == "selected" for c in winners) == 1
    assert all(c.reason is None for c in winners)


# ---------------------------------------------------------------- §6.6 non-annual durations


def test_real_msft_quarterly_dividend_facts_are_not_annual(msft_facts, msft_entry):
    dividends = [f for f in msft_facts if f.tag == "CommonStockDividendsPerShareDeclared"
                 and f.accession_number == msft_entry.accession_number]
    quarters = [f for f in dividends if f.period_start == date(2024, 7, 1) and f.period_end == date(2024, 9, 30)]
    annual = [f for f in dividends if f.period_start == date(2024, 7, 1) and f.period_end == date(2025, 6, 30)]
    assert quarters and annual
    assert not is_annual_duration(quarters[0].period_start, quarters[0].period_end)  # 91 days, real data
    assert is_annual_duration(annual[0].period_start, annual[0].period_end)  # 364 days


def test_quarter_length_pinned_fact_rejected_non_annual(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
             start="2025-06-28", end="2025-09-27", val=999999999999, form="10-K", filed="2025-10-31",
             fy=2025, fp="Q4")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    quarter = [c for c in selection.candidates if c.period_start == date(2025, 6, 28)]
    assert quarter and quarter[0].decision == "rejected"
    assert quarter[0].reason == "non_annual_duration"
    assert selection.selected.value == Decimal(416161000000)


def test_annual_window_boundaries():
    from datetime import timedelta
    base = date(2024, 1, 1)
    assert is_annual_duration(base, base + timedelta(days=350))
    assert is_annual_duration(base, base + timedelta(days=380))
    assert not is_annual_duration(base, base + timedelta(days=349))
    assert not is_annual_duration(base, base + timedelta(days=381))
    assert is_annual_duration(date(2024, 9, 29), date(2025, 9, 27))   # 363 days
    assert is_annual_duration(date(2022, 9, 25), date(2023, 9, 30))  # 370 days (53-week year)
    assert not is_annual_duration(None, date(2025, 9, 27))           # instants are never annual durations


# ---------------------------------------------------------------- §6.7 unit mismatch (selection half)


def test_pinned_fact_with_wrong_unit_rejected(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", unit="USD/shares",
             accn=aapl_entry.accession_number, start=AAPL_FY2025[0].isoformat(),
             end=AAPL_FY2025[1].isoformat(), val=416161, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    wrong_unit = [c for c in selection.candidates if c.unit == "USD/shares"]
    assert wrong_unit and wrong_unit[0].decision == "rejected"
    assert wrong_unit[0].reason == "unit_mismatch"


# ---------------------------------------------------------------- §6.8 instant vs duration


def test_balance_sheet_concepts_pick_the_fy_end_instant(aapl_facts, aapl_entry):
    for concept, tag in ((Concept.CURRENT_ASSETS, "AssetsCurrent"), (Concept.CURRENT_LIABILITIES, "LiabilitiesCurrent")):
        selection = select(aapl_facts, concept, 2025, aapl_entry)
        assert selection.period_start is None
        assert selection.period_end == date(2025, 9, 27)
        assert selection.selected.tag == tag and selection.selected.period_start is None


def test_duration_fact_rejected_for_instant_concept(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "AssetsCurrent", accn=aapl_entry.accession_number, start=AAPL_FY2025[0].isoformat(),
             end=AAPL_FY2025[1].isoformat(), val=1, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.CURRENT_ASSETS, 2025, aapl_entry)
    duration = [c for c in selection.candidates if c.tag == "AssetsCurrent" and c.period_start is not None]
    assert duration and duration[0].reason == "wrong_period_type"


def test_instant_fact_rejected_for_duration_concept(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    add_fact(raw, "RevenueFromContractWithCustomerExcludingAssessedTax", accn=aapl_entry.accession_number,
             end="2025-09-27", val=1, form="10-K", filed="2025-10-31", fy=2025, fp="FY")
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    selection = select(facts, Concept.REVENUE, 2025, aapl_entry)
    instant = [c for c in selection.candidates
               if c.tag == "RevenueFromContractWithCustomerExcludingAssessedTax" and c.period_start is None]
    assert instant and instant[0].reason == "wrong_period_type"


# ---------------------------------------------------------------- §6.9 unavailable years


def test_fiscal_year_absent_from_pinned_filing_raises(aapl_facts, aapl_entry):
    with pytest.raises(FiscalYearNotInFilingError) as excinfo:
        select(aapl_facts, Concept.REVENUE, 2021, aapl_entry)
    assert excinfo.value.code == "fiscal_year_not_in_filing"
    assert excinfo.value.available_years == [2023, 2024, 2025]


def test_current_ratio_year_without_instant_raises(aapl_facts, aapl_entry):
    # FY2023's annual period exists in the filing, but the fixture's pinned instants only cover 2024/2025.
    with pytest.raises(FiscalYearNotInFilingError) as excinfo:
        select(aapl_facts, Concept.CURRENT_ASSETS, 2023, aapl_entry)
    assert excinfo.value.available_years == [2024, 2025]


# ---------------------------------------------------------------- §6.10 cutoff


def test_fact_filed_after_cutoff_rejected(aapl_raw, aapl_entry):
    raw = modified(aapl_raw)
    units = raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    for fact in units:
        if fact.get("start") == "2024-09-29" and fact["end"] == "2025-09-27":
            fact["filed"] = "2025-11-15"  # after the pinned filing date (modified copy)
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(MissingFactError) as excinfo:
        select(facts, Concept.REVENUE, 2025, aapl_entry)
    counts = excinfo.value.rejection_counts
    assert counts["after_cutoff"] == 1
    assert counts["period_mismatch"] == 2  # the FY2023/FY2024 comparatives of the pinned filing
    assert sum(counts.values()) == len([f for f in facts if f.tag in CONCEPTS[Concept.REVENUE].tags])
    assert excinfo.value.fallback and "search_filing_chunks" in excinfo.value.fallback


def test_explicit_cutoff_before_filing_rejects_every_pinned_fact(aapl_facts, aapl_entry):
    with pytest.raises(MissingFactError) as excinfo:
        select(aapl_facts, Concept.REVENUE, 2025, aapl_entry, cutoff_date=date(2025, 10, 30))
    assert excinfo.value.rejection_counts["after_cutoff"] == 3


def test_cutoff_equal_to_filing_date_accepts_pinned_facts(aapl_facts, aapl_entry):
    selection = select(aapl_facts, Concept.REVENUE, 2025, aapl_entry, cutoff_date=date(2025, 10, 31))
    assert selection.selected.value == Decimal(416161000000)


# ---------------------------------------------------------------- period resolution edges (§3)


def _duration_fact(ticker, accn, start, end, val=1):
    from app.domain.financial_facts import FinancialFact
    return FinancialFact(ticker=ticker, cik="0000000001", taxonomy="us-gaap", tag="Revenues", label=None,
                         value=Decimal(val), unit="USD", period_start=start, period_end=end,
                         fiscal_year=None, fiscal_period=None, form="10-K", filed_at=date(2026, 1, 1),
                         accession_number=accn, frame=None,
                         source_url="https://www.sec.gov/Archives/edgar/data/1/x/")


def test_resolve_duration_period_real_windows(aapl_facts, msft_facts, aapl_entry, msft_entry):
    assert resolve_fiscal_period("AAPL", 2023, period_type="duration", facts=aapl_facts, pinned=aapl_entry) == AAPL_FY2023
    assert resolve_fiscal_period("MSFT", 2026, period_type="duration", facts=msft_facts, pinned=msft_entry) == \
        (date(2025, 7, 1), date(2026, 6, 30))
    assert resolve_fiscal_period("AAPL", 2025, period_type="instant", facts=aapl_facts, pinned=aapl_entry) == \
        (None, date(2025, 9, 27))


def test_unknown_ticker_raises(msft_entry, msft_facts):
    with pytest.raises(UnknownTickerError):
        resolve_fiscal_period("TSLA", 2025, period_type="duration", facts=msft_facts, pinned=msft_entry)


def test_resolved_period_outside_fiscal_calendar_raises(msft_entry):
    facts = [_duration_fact("MSFT", msft_entry.accession_number, date(2025, 1, 1), date(2025, 12, 31))]
    with pytest.raises(FiscalCalendarMismatchError):
        resolve_fiscal_period("MSFT", 2025, period_type="duration", facts=facts, pinned=msft_entry)


def test_out_of_window_same_year_period_is_ignored_when_fiscal_period_exists(msft_entry, msft_facts):
    facts = list(msft_facts) + [
        _duration_fact("MSFT", msft_entry.accession_number, date(2024, 1, 1), date(2024, 12, 31), val=99),
    ]
    assert resolve_fiscal_period("MSFT", 2024, period_type="duration", facts=facts, pinned=msft_entry) == \
        (date(2023, 7, 1), date(2024, 6, 30))


def test_only_out_of_window_annual_period_raises_calendar_mismatch(msft_entry):
    facts = [_duration_fact("MSFT", msft_entry.accession_number, date(2024, 1, 1), date(2024, 12, 31))]
    with pytest.raises(FiscalCalendarMismatchError):
        resolve_fiscal_period("MSFT", 2024, period_type="duration", facts=facts, pinned=msft_entry)


def test_two_distinct_annual_periods_same_year_raise(aapl_entry):
    facts = [
        _duration_fact("AAPL", aapl_entry.accession_number, date(2024, 9, 29), date(2025, 9, 27), val=1),
        _duration_fact("AAPL", aapl_entry.accession_number, date(2024, 10, 1), date(2025, 10, 4), val=2),
    ]
    with pytest.raises(AmbiguousFactError) as excinfo:
        resolve_fiscal_period("AAPL", 2025, period_type="duration", facts=facts, pinned=aapl_entry)
    assert {c["period_end"] for c in excinfo.value.candidates} == {date(2025, 9, 27), date(2025, 10, 4)}


def test_instant_resolution_requires_an_instant_at_the_annual_end(aapl_entry):
    annual_only = [_duration_fact("AAPL", aapl_entry.accession_number, date(2024, 9, 29), date(2025, 9, 27))]
    with pytest.raises(FiscalYearNotInFilingError):
        resolve_fiscal_period("AAPL", 2025, period_type="instant", facts=annual_only, pinned=aapl_entry)


def test_month_window_covers_52_53_week_years(aapl_entry):
    facts = [_duration_fact("AAPL", aapl_entry.accession_number, date(2024, 9, 29), date(2025, 10, 4))]
    start, end = resolve_fiscal_period("AAPL", 2025, period_type="duration", facts=facts, pinned=aapl_entry)
    assert (start, end) == (date(2024, 9, 29), date(2025, 10, 4))  # Sept +1 month: accepted


# ---------------------------------------------------------------- §6.13 provenance / candidates coverage


def test_candidates_cover_every_fact_of_every_alias_tag(aapl_facts, aapl_entry):
    selection = select(aapl_facts, Concept.REVENUE, 2025, aapl_entry)
    alias_tags = CONCEPTS[Concept.REVENUE].tags
    considered = [f for f in aapl_facts if f.tag in alias_tags]
    assert len(selection.candidates) == len(considered)
    from app.domain.financial_facts import fact_id
    assert {c.fact_id for c in selection.candidates} == {fact_id(f) for f in considered}
    assert all(c.decision in ("selected", "duplicate_of_selected", "rejected") for c in selection.candidates)
    assert all((c.reason is None) == (c.decision != "rejected") for c in selection.candidates)


def test_selection_is_deterministic(aapl_facts, aapl_entry):
    first = select(aapl_facts, Concept.REVENUE, 2025, aapl_entry)
    second = select(list(reversed(aapl_facts)), Concept.REVENUE, 2025, aapl_entry)
    assert first.selected == second.selected
    assert first.candidates == second.candidates
    assert isinstance(first, FactSelection)


# ---------------------------------------------------------------- ADR 0002 rule 7: snapshot invariance


def test_same_pinned_facts_same_selection_across_snapshots(aapl_raw, aapl_entry):
    noisy = modified(aapl_raw)
    # A later snapshot adds a restated 10-K value, a new 10-Q fact, and drops nothing.
    add_fact(noisy, "RevenueFromContractWithCustomerExcludingAssessedTax", accn="0000320193-26-000001",
             start=AAPL_FY2024[0].isoformat(), end=AAPL_FY2024[1].isoformat(), val=123456789,
             form="10-K", filed="2026-11-01", fy=2026, fp="FY")
    add_fact(noisy, "RevenueFromContractWithCustomerExcludingAssessedTax", accn="0000320193-26-000002",
             start="2026-01-01", end="2026-03-31", val=1, form="10-Q", filed="2026-04-30", fy=2026, fp="Q2")
    base = normalize_companyfacts(aapl_raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    snapshot = normalize_companyfacts(noisy, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    a = select(base, Concept.REVENUE, 2025, aapl_entry)
    b = select(snapshot, Concept.REVENUE, 2025, aapl_entry)
    assert a.selected == b.selected  # same fact, same value, same id: pinned answers ignore snapshot noise


def test_pinned_fact_value_changed_between_snapshots_is_a_typed_error(aapl_raw, aapl_entry):
    drifted = modified(aapl_raw)
    units = drifted["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    for fact in units:
        if fact.get("start") == "2024-09-29" and fact["end"] == "2025-09-27":
            fact["val"] = 416161000001  # same accession, same period, different value in the new snapshot
    base = normalize_companyfacts(aapl_raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    snapshot = normalize_companyfacts(drifted, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(AmbiguousFactError):
        select(base + snapshot, Concept.REVENUE, 2025, aapl_entry)  # never a silent choice


def test_pinned_facts_only_come_from_the_pinned_accession(aapl_facts, aapl_entry):
    pinned = pinned_facts_of(aapl_facts, aapl_entry.accession_number)
    assert pinned
    assert all(f.accession_number == aapl_entry.accession_number for f in pinned)
    assert all(str(f.source_url) == str(aapl_entry.source_url) for f in pinned)
