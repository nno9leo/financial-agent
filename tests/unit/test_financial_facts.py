"""Normalization tests: payload shape, Decimal exactness, provenance, fact_id stability (§2, §6.15)."""

import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.domain.financial_facts import FinancialFact, fact_id, filing_index_url, normalize_companyfacts
from tests.unit.conftest import modified

AAPL_PINNED_REVENUE_FY2023 = 383285000000


def test_us_gaap_only_and_fixture_note_ignored(aapl_entry):
    raw = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "_fixture_note": {"source": "anything"},
        "facts": {
            "dei": {"EntityPublicFloat": {"label": "Public Float", "units": {"USD": [
                {"end": "2025-03-31", "val": 3234000000000, "accn": "0000320193-25-000079",
                 "fy": 2025, "fp": "Q1", "form": "10-K", "filed": "2025-10-31"}]}}},
            "us-gaap": {"Revenues": {"label": "Revenues", "units": {"USD": [
                {"start": "2024-09-29", "end": "2025-09-27", "val": 1, "accn": "0000320193-25-000079",
                 "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31"}]}}},
        },
    }
    facts = normalize_companyfacts(raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    assert len(facts) == 1
    fact = facts[0]
    assert (fact.taxonomy, fact.tag) == ("us-gaap", "Revenues")
    assert fact.label == "Revenues"
    assert fact.ticker == "AAPL" and fact.cik == "0000320193"
    assert fact.period_start == date(2024, 9, 29) and fact.period_end == date(2025, 9, 27)
    assert fact.fiscal_year == 2025 and fact.fiscal_period == "FY"
    assert fact.frame is None
    assert isinstance(fact, FinancialFact)


def test_frame_preserved_when_present(aapl_facts, aapl_entry):
    framed = [f for f in aapl_facts if f.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
              and f.accession_number == aapl_entry.accession_number]
    assert {f.frame for f in framed} == {"CY2023", "CY2024", "CY2025"}


def test_instant_facts_have_no_period_start(aapl_facts, aapl_entry):
    instants = [f for f in aapl_facts if f.tag == "AssetsCurrent" and f.accession_number == aapl_entry.accession_number]
    assert {f.period_end for f in instants} == {date(2024, 9, 28), date(2025, 9, 27)}
    assert all(f.period_start is None for f in instants)


def test_values_are_exact_decimals_never_float(msft_facts, msft_entry):
    dividends = [f for f in msft_facts if f.tag == "CommonStockDividendsPerShareDeclared"
                 and f.accession_number == msft_entry.accession_number]
    per_share = {f.value: f for f in dividends}
    # 0.83 is a float literal in the raw JSON; it must arrive as an exact Decimal.
    assert Decimal("0.83") in per_share and Decimal("3.32") in per_share and Decimal("3") in per_share
    assert all(isinstance(f.value, Decimal) and not isinstance(f.value, float) for f in msft_facts)
    revenue = next(f for f in msft_facts if f.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
                   and f.period_end == date(2026, 6, 30) and f.accession_number == msft_entry.accession_number)
    assert revenue.value == Decimal(331839000000)
    assert type(revenue.value) is Decimal


def test_integer_value_round_trips_exactly(aapl_entry):
    raw = {"facts": {"us-gaap": {"Revenues": {"label": None, "units": {"USD": [
        {"start": "2024-09-29", "end": "2025-09-27", "val": 331839000000, "accn": aapl_entry.accession_number,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31"}]}}}}}
    (fact,) = normalize_companyfacts(raw, ticker="MSFT", cik="0000789019", pinned=aapl_entry)
    assert fact.value == Decimal("331839000000")
    assert str(fact.value) == "331839000000"


def test_bytes_and_dict_inputs_agree(aapl_raw, aapl_entry):
    from_bytes = normalize_companyfacts(json_bytes_of(aapl_raw), ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    from_dict = normalize_companyfacts(aapl_raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    assert [fact_id(f) for f in from_bytes] == [fact_id(f) for f in from_dict]
    expected_count = sum(len(unit_facts) for tag in aapl_raw["facts"]["us-gaap"].values()
                         for unit_facts in tag["units"].values())
    assert len(from_bytes) == expected_count == 1743  # every fact of every subset tag, all accessions


def json_bytes_of(raw):
    import json
    return json.dumps(raw, separators=(",", ":")).encode()


def test_source_url_pinned_document_vs_other_accession(aapl_facts, aapl_entry):
    pinned = [f for f in aapl_facts if f.accession_number == aapl_entry.accession_number]
    others = [f for f in aapl_facts if f.accession_number != aapl_entry.accession_number]
    assert pinned and others
    assert all(str(f.source_url) == str(aapl_entry.source_url) for f in pinned)
    for fact in others:
        compact = fact.accession_number.replace("-", "")
        assert str(fact.source_url) == f"https://www.sec.gov/Archives/edgar/data/320193/{compact}/"
    assert filing_index_url("0000320193", "0000320193-25-000079") == \
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/"


def test_facts_sorted_deterministically(aapl_raw, aapl_entry):
    first = normalize_companyfacts(aapl_raw, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    reordered = {k: aapl_raw[k] for k in reversed(list(aapl_raw.keys()))}
    reordered["facts"]["us-gaap"] = dict(reversed(list(aapl_raw["facts"]["us-gaap"].items())))
    second = normalize_companyfacts(reordered, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    assert [fact_id(f) for f in first] == [fact_id(f) for f in second]
    keys = [(f.taxonomy, f.tag, f.unit, f.period_start or f.period_end, f.period_end) for f in first]
    assert keys == sorted(keys)


def test_fact_id_spec_shape_and_stability(aapl_facts):
    fact = next(f for f in aapl_facts if f.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
                and f.period_end == date(2025, 9, 27) and f.value == Decimal(416161000000))
    digest = "|".join([fact.accession_number, fact.taxonomy, fact.tag, fact.unit,
                       str(fact.period_start), str(fact.period_end), str(fact.value)])
    expected = "fact_" + hashlib.sha256(digest.encode("utf-8")).hexdigest()[:20]
    assert fact_id(fact) == expected
    assert fact_id(fact).startswith("fact_") and len(fact_id(fact)) == 25
    # instant facts hash their None start distinctly
    twin = fact.model_copy(update={"period_start": None})
    assert fact_id(twin) != fact_id(fact)
    changed_value = fact.model_copy(update={"value": Decimal(1)})
    assert fact_id(changed_value) != fact_id(fact)


def test_malformed_facts_raise(aapl_entry, aapl_raw):
    broken = modified(aapl_raw)
    broken["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        {"start": "2024-09-29", "end": "2025-09-27", "accn": aapl_entry.accession_number,
         "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31"})  # missing val
    with pytest.raises(ValueError, match="Malformed companyfacts"):
        normalize_companyfacts(broken, ticker="AAPL", cik="0000320193", pinned=aapl_entry)
    with pytest.raises(ValueError, match="Malformed companyfacts"):
        normalize_companyfacts({"facts": []}, ticker="AAPL", cik="0000320193", pinned=aapl_entry)


def test_fy_fp_are_the_filings_labels_not_the_periods(aapl_facts, aapl_entry):
    revenue = [f for f in aapl_facts if f.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
               and f.accession_number == aapl_entry.accession_number]
    by_end = {f.period_end: f for f in revenue}
    assert by_end[date(2023, 9, 30)].value == Decimal(AAPL_PINNED_REVENUE_FY2023)
    assert by_end[date(2023, 9, 30)].fiscal_year == 2025 and by_end[date(2023, 9, 30)].fiscal_period == "FY"
    assert all(f.fiscal_year == 2025 for f in revenue)
