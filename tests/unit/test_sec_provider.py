import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.domain.errors import (CacheIntegrityError, CompanyNotFoundError, ConfigurationError,
                               FilingRangeUnavailableError, SecHttpError, SecPayloadError, SecRateLimitedError, SecTimeoutError)
from app.providers.sec import SecProvider, normalize_cik

FIX = Path(__file__).parents[1] / "fixtures" / "sec"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, sec_user_agent="Test Suite test@example.com", data_dir=tmp_path,
                    max_retries=3, min_request_interval=0)


def test_clean_and_warm_cache_provenance_and_atomic_files(settings):
    calls = []
    payload = (FIX / "company_tickers.json").read_bytes()
    transport = httpx.MockTransport(lambda req: (calls.append(req.url) or httpx.Response(200, content=payload)))
    p = SecProvider(settings, transport=transport, sleep=lambda _: None)
    url = "https://www.sec.gov/files/company_tickers.json"
    body, meta = p._fetch(url)
    assert body == payload and meta["cache"] == "miss"
    assert meta["sha256"] == hashlib.sha256(payload).hexdigest() and meta["retrieved_at"].endswith("Z")
    assert not list((settings.data_dir / "raw").glob("*.tmp"))
    p.client.close()
    def forbidden(_):
        raise AssertionError("warm cache must avoid transport")
    warm = SecProvider(settings, transport=httpx.MockTransport(forbidden))
    _, cached = warm._fetch(url)
    assert cached["cache"] == "hit" and len(calls) == 1


def test_atomic_failure_leaves_no_final_or_temp(settings, monkeypatch):
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok")), sleep=lambda _: None)
    def fail(*_): raise OSError("simulated atomic rename failure")
    monkeypatch.setattr("app.providers.sec.os.replace", fail)
    with pytest.raises(OSError): p._fetch("https://data.sec.gov/example")
    assert not list((settings.data_dir / "raw").glob("*.body"))
    assert not list((settings.data_dir / "raw").glob("*.tmp"))


def test_cache_tamper_raises(settings):
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"original")), sleep=lambda _: None)
    url = "https://data.sec.gov/example"
    p._fetch(url)
    body, _ = p._paths(url)
    body.write_bytes(b"tampered")
    with pytest.raises(CacheIntegrityError): p._fetch(url)


def test_orphan_body_without_metadata_is_cache_miss(settings):
    calls = []
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: (calls.append(1) or httpx.Response(200, content=b"fresh"))), sleep=lambda _: None)
    url = "https://data.sec.gov/orphan"
    body, meta = p._paths(url)
    body.write_bytes(b"partial prior response")
    result, provenance = p._fetch(url)
    assert result == b"fresh" and provenance["cache"] == "miss" and len(calls) == 1


def test_public_get_cached_never_fetches_on_cache_miss(settings):
    calls = []
    provider = SecProvider(settings, transport=httpx.MockTransport(
        lambda req: (calls.append(req.url) or httpx.Response(200, content=b"network"))))
    assert provider.get_cached("https://data.sec.gov/cache-only") is None
    assert calls == []
    url = "https://data.sec.gov/warm-cache"
    body, meta = provider._paths(url)
    payload = b"cached"
    body.write_bytes(payload)
    meta.write_text(json.dumps({"source_url": url, "sha256": hashlib.sha256(payload).hexdigest()}), encoding="utf-8")
    result = provider.get_cached(url)
    assert result[0] == payload and result[1]["cache"] == "hit"
    assert calls == []


def test_invalid_json_payload_is_typed(settings):
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"not json")), sleep=lambda _: None)
    with pytest.raises(SecPayloadError) as error:
        p._json("https://data.sec.gov/bad-json")
    assert error.value.code == "sec_invalid_payload"


def test_list_filings_tool_returns_typed_filings_and_exclusions(settings):
    from app.tools.list_filings import ListFilingsInput, ListFilingsOutput, list_filings
    submissions = json.loads((FIX / "submissions.json").read_text())
    submissions["filings"]["files"] = []
    ticker_payload = json.dumps({"0": {"cik_str": 1, "ticker": "TEST", "title": "Example Corp"}}).encode()
    def handler(req):
        if "company_tickers" in str(req.url): return httpx.Response(200, content=ticker_payload)
        return httpx.Response(200, json=submissions)
    p = SecProvider(settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    output = list_filings(ListFilingsInput(company="TEST", form="8-K"), p)
    assert isinstance(output, ListFilingsOutput)
    assert [filing.accession_number for filing in output.filings] == ["0000000001-25-000004"]
    assert [(x.accession_number, x.form, x.filing_date.isoformat(), x.reason) for x in output.excluded] == [
        ("0000000001-25-000005", "8-K", "2025-06-02", "missing_report_date"),
    ]
    assert output.older_pages_not_loaded is False and output.coverage_start == date(2024, 2, 1)


def test_amendment_exclusions_obey_requested_form_and_date_range(settings):
    payload = json.loads((FIX / "submissions.json").read_text())
    payload["filings"]["files"] = []
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    result = p.list_filings(1, {"8-K"}, date_from=date(2025, 1, 1), date_to=date(2025, 6, 2))
    assert all(item.reason != "amendment" for item in result.excluded)
    p = SecProvider(Settings(_env_file=None, sec_user_agent=settings.sec_user_agent, data_dir=settings.data_dir / "amendment-range", min_request_interval=0),
                    transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    result = p.list_filings(1, {"10-K"}, date_from=date(2025, 3, 1), date_to=date(2025, 6, 2))
    assert all(item.reason != "amendment" for item in result.excluded)


def test_cik_resolution_missing_company_is_typed(settings):
    payloads = {"404": httpx.Response(404), "empty": httpx.Response(200, json={"tickers": [], "name": ""})}
    def handler(req):
        if "0000000002" in str(req.url): return payloads["404"]
        return payloads["empty"]
    p = SecProvider(settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    with pytest.raises(CompanyNotFoundError): p.resolve_company(2)
    with pytest.raises(CompanyNotFoundError): p.resolve_company(3)


def test_http_retry_and_404_no_retry(settings):
    calls = []
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: (calls.append(1) or httpx.Response(500))), sleep=lambda _: None)
    with pytest.raises(SecHttpError): p._fetch("https://data.sec.gov/server")
    assert len(calls) == 3
    calls.clear()
    p.client.close()
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: (calls.append(1) or httpx.Response(404))), sleep=lambda _: None)
    with pytest.raises(SecHttpError): p._fetch("https://data.sec.gov/missing")
    assert len(calls) == 1


def test_timeout_and_rate_limit(settings):
    def timeout(_): raise httpx.ReadTimeout("timeout")
    p = SecProvider(settings, transport=httpx.MockTransport(timeout), sleep=lambda _: None)
    with pytest.raises(SecTimeoutError): p._fetch("https://data.sec.gov/timeout")
    p.client.close()
    calls = []
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: (calls.append(1) or httpx.Response(429))), sleep=lambda _: None)
    with pytest.raises(SecRateLimitedError): p._fetch("https://data.sec.gov/limited")
    assert len(calls) == 3


def test_missing_user_agent_has_no_network(settings, tmp_path):
    calls = []
    p = SecProvider(Settings(_env_file=None, sec_user_agent=None, data_dir=tmp_path),
                    transport=httpx.MockTransport(lambda req: (calls.append(req) or httpx.Response(200))))
    with pytest.raises(ConfigurationError): p._fetch("https://data.sec.gov/example")
    assert calls == []


def test_invalid_ticker_and_company_resolution(settings):
    calls = []
    payload = (FIX / "company_tickers.json").read_bytes()
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: (calls.append(str(req.url)) or httpx.Response(200, content=payload))), sleep=lambda _: None)
    assert p.resolve_company("MSFT") == ("MSFT", "0000789019", "Microsoft Corporation")
    with pytest.raises(CompanyNotFoundError): p.resolve_company("NOPE")


@pytest.mark.parametrize("value", [123, "123", "0000000123"])
def test_normalize_cik(value):
    assert normalize_cik(value) == "0000000123"


def test_list_filings_filter_amendments_dates_missing_report_and_old_range(settings):
    payload = json.loads((FIX / "submissions.json").read_text())
    payload["filings"]["files"] = [{"name": "older.json"}]
    p = SecProvider(settings, transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    ranged = p.list_filings(1, {"10-K", "10-Q", "8-K"}, date_from=date(2025, 1, 1), date_to=date(2025, 6, 2))
    assert ranged.older_pages_not_loaded is False and ranged.coverage_start == date(2024, 2, 1)
    assert [(x.form, x.reason) for x in ranged.excluded] == [("10-K/A", "amendment"), ("8-K", "missing_report_date")]
    p = SecProvider(Settings(_env_file=None, sec_user_agent=settings.sec_user_agent, data_dir=settings.data_dir / "all", min_request_interval=0), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    all_recent = p.list_filings(1, {"10-K", "10-Q", "8-K"})
    assert all_recent.older_pages_not_loaded is True and all_recent.coverage_start == date(2024, 2, 1)
    p = SecProvider(Settings(_env_file=None, sec_user_agent=settings.sec_user_agent, data_dir=settings.data_dir / "too-old", min_request_interval=0), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    with pytest.raises(FilingRangeUnavailableError): p.list_filings(1, {"10-K"}, date_from=date(2022, 1, 1))
    payload["filings"]["files"] = []
    p = SecProvider(Settings(_env_file=None, sec_user_agent=settings.sec_user_agent, data_dir=settings.data_dir / "filter", min_request_interval=0), transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)), sleep=lambda _: None)
    listing = p.list_filings(1, {"10-K", "10-Q", "8-K"}, date_from=date(2025, 1, 1), date_to=date(2025, 6, 2))
    filings, excluded = listing.filings, listing.excluded
    assert [f.form for f in filings] == ["10-K", "10-Q", "8-K"]
    assert all(not f.accession_number.endswith("000002") for f in filings)
    assert all(f.report_date != f.filing_date for f in filings)
    assert [(item.form, item.reason) for item in excluded] == [("10-K/A", "amendment"), ("8-K", "missing_report_date")]
    assert all(f.filing_date <= date(2025, 6, 2) for f in filings)
