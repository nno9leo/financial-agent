import hashlib
import json

import pytest
from pydantic import ValidationError

from app.compute.metrics import Metric, calculate_metric
from app.domain.errors import (AccessionCompanyMismatchError, CompanyFactsNotCachedError,
                               CompanyFactsSnapshotMismatchError, CompanyNotInManifestError)
from app.domain.financial_facts import normalize_companyfacts
from app.tools import calculate_financial_metric as module
from app.tools.calculate_financial_metric import (CalculateFinancialMetricInput,
                                                  calculate_financial_metric)
from tests.unit.conftest import load_raw_fixture


class FakeProvider:
    def __init__(self, body):
        self.body = body
        self.calls = 0

    def get_cached(self, url):
        self.calls += 1
        return None if self.body is None else (self.body, {"source_url": url})


def test_calculate_tool_matches_engine_and_returns_provenance(msft_entry, aapl_entry):
    for entry, raw, metric, year in (
        (msft_entry, load_raw_fixture("msft_subset.json"), Metric.OPERATING_MARGIN, 2026),
        (aapl_entry, load_raw_fixture("aapl_subset.json"), Metric.FREE_CASH_FLOW, 2025),
    ):
        body = json.dumps(raw, separators=(",", ":")).encode()
        # Fixture metadata includes the hash of the checked-in raw bytes, so
        # use a matching entry for this injected cache snapshot.
        pinned = entry.model_copy(update={"companyfacts_sha256": hashlib.sha256(body).hexdigest()})
        manifest = module.FilingManifest(created_at="2026-01-01T00:00:00Z", sec_user_agent_set=False,
                                        entries=[pinned])
        facts = normalize_companyfacts(body, ticker=pinned.ticker, cik=pinned.cik, pinned=pinned)
        expected = calculate_metric(metric, ticker=pinned.ticker, fiscal_year=year, facts=facts, pinned=pinned)
        output = calculate_financial_metric({"metric": metric.value, "company": pinned.ticker,
                                              "fiscal_year": year}, provider=FakeProvider(body), manifest=manifest)
        assert output.result == expected
        assert output.accession_number == pinned.accession_number
        assert output.source_url == pinned.source_url
        assert output.companyfacts_sha256 == pinned.companyfacts_sha256
        assert output.accession_resolved_from_manifest is True


def test_cache_miss_and_snapshot_mismatch_are_typed(msft_entry, manifest):
    with pytest.raises(CompanyFactsNotCachedError):
        calculate_financial_metric({"metric": "revenue", "company": "MSFT", "fiscal_year": 2026},
                                   provider=FakeProvider(None), manifest=manifest)
    with pytest.raises(CompanyFactsSnapshotMismatchError):
        calculate_financial_metric({"metric": "revenue", "company": "MSFT", "fiscal_year": 2026},
                                   provider=FakeProvider(b"{}"), manifest=manifest)


def test_company_resolution_errors_and_invalid_metric_are_typed(manifest):
    provider = FakeProvider(b"{}")
    with pytest.raises(CompanyNotInManifestError):
        calculate_financial_metric({"metric": "revenue", "company": "NOPE", "fiscal_year": 2026},
                                   provider=provider, manifest=manifest)
    with pytest.raises(AccessionCompanyMismatchError):
        calculate_financial_metric({"metric": "revenue", "company": "MSFT", "fiscal_year": 2026,
                                    "accession_number": next(e.accession_number for e in manifest.entries if e.ticker == "AAPL")},
                                   provider=provider, manifest=manifest)
    with pytest.raises(ValidationError):
        CalculateFinancialMetricInput(metric="made_up", company="MSFT", fiscal_year=2026)
    with pytest.raises(ValidationError):
        CalculateFinancialMetricInput(metric="revenue", company="MSFT", fiscal_year=2026, extra=True)


def test_normalized_fact_cache_uses_accession_and_snapshot_hash(monkeypatch, msft_entry):
    module.clear_normalized_companyfacts_cache()
    calls = 0
    original = module.normalize_companyfacts

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "normalize_companyfacts", counting)
    body = json.dumps(load_raw_fixture("msft_subset.json"), separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    entry = msft_entry.model_copy(update={"companyfacts_sha256": digest})
    manifest = module.FilingManifest(created_at="2026-01-01T00:00:00Z", sec_user_agent_set=False, entries=[entry])
    req = {"metric": "revenue", "company": "MSFT", "fiscal_year": 2026}
    for _ in range(2):
        calculate_financial_metric(req, provider=FakeProvider(body), manifest=manifest)
    assert calls == 1
    changed = entry.model_copy(update={"companyfacts_sha256": "0" * 64})
    # Directly exercise the cache key with a different snapshot identity; the
    # normal request path correctly rejects bytes that do not match that hash.
    module._normalize_cached(entry.accession_number, "0" * 64, body, entry.ticker, entry.cik, changed)
    assert calls == 2
    module.clear_normalized_companyfacts_cache()
