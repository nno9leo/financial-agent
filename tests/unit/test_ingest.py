import hashlib
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.domain.errors import (CacheIntegrityError, CompanyFactsSnapshotMismatchError,
                               CompanyFactsSnapshotUnchangedError, ManifestNotFoundError)
from app.domain.filings import FilingListResult, FilingMetadata
from app.ingestion.manifest import load_manifest
from scripts.ingest import run


class FakeProvider:
    document = b"fixed filing"
    facts = b"fixed facts"

    def __init__(self, *, changed=False, newer=False, facts_changed=False):
        self.changed, self.newer, self.facts_changed = changed, newer, facts_changed
        self.calls = 0

    def close(self): pass
    def resolve_company(self, ticker):
        cik = "0000789019" if ticker == "MSFT" else "0000320193"
        return ticker, cik, f"{ticker} Corporation"
    def list_filings(self, cik, forms):
        day = date(2026 if self.newer else 2025, 2, 1)
        filing = FilingMetadata(ticker="X", cik=cik, company_name="X Corporation", form="10-K",
            accession_number=f"{cik}-25-000001", filing_date=day, report_date=date(day.year - 1, 12, 31),
            primary_document="annual.htm", source_url=f"https://www.sec.gov/{cik}/annual.htm")
        return FilingListResult(filings=[filing], excluded=[], coverage_start=day, older_pages_not_loaded=False)
    def get_filing_document(self, filing):
        self.calls += 1
        body = b"changed filing" if self.changed else self.document
        return body, {"sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": "2025-02-03T00:00:00Z"}
    def get_company_facts(self, cik):
        body = b"new snapshot facts" if self.facts_changed else self.facts
        return body, {"sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": "2025-02-03T00:00:00Z"}
    def get_cached_or_fetch(self, url):
        self.calls += 1
        body = b"changed filing" if self.changed and "sec.gov/" in url else self.document
        return body, {"sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": "2025-02-03T00:00:00Z"}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, sec_user_agent="Test Suite test@example.com", data_dir=tmp_path / "raw")


def test_ingest_twice_uses_pinned_manifest_and_preserves_bytes(tmp_path, settings):
    directory = tmp_path / "manifests"
    first = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    original = first.read_bytes()
    second = run(manifests_dir=directory, provider=FakeProvider(newer=True), settings=settings)
    assert second == first and second.read_bytes() == original
    assert load_manifest(second).manifest_version == "v1"


def test_pinned_changed_upstream_fails_and_file_is_unchanged(tmp_path, settings):
    directory = tmp_path / "manifests"
    manifest = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    original = manifest.read_bytes()
    with pytest.raises(CacheIntegrityError):
        run(manifests_dir=directory, provider=FakeProvider(changed=True), settings=settings)
    assert manifest.read_bytes() == original


def test_repin_twice_writes_v2_then_v3_without_overwrite(tmp_path, settings):
    directory = tmp_path / "manifests"
    v1 = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    v1_bytes = v1.read_bytes()
    v2 = run(repin=True, manifests_dir=directory, provider=FakeProvider(newer=True), settings=settings)
    v2_bytes = v2.read_bytes()
    assert run(manifests_dir=directory, provider=FakeProvider(), settings=settings) == v2
    v3 = run(repin=True, manifests_dir=directory, provider=FakeProvider(newer=True), settings=settings)
    assert [v1.name, v2.name, v3.name] == ["filings.v1.json", "filings.v2.json", "filings.v3.json"]
    assert load_manifest(v2).manifest_version == "v2" and load_manifest(v3).manifest_version == "v3"
    assert v1.read_bytes() == v1_bytes and v2.read_bytes() == v2_bytes
    latest = run(manifests_dir=directory, provider=FakeProvider(newer=False), settings=settings)
    assert latest == v3


def test_companyfacts_mismatch_default_fails_and_refresh_writes_new_version(tmp_path, settings):
    directory = tmp_path / "manifests"
    v1 = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    before = v1.read_bytes()
    with pytest.raises(CompanyFactsSnapshotMismatchError) as error:
        run(manifests_dir=directory, provider=FakeProvider(facts_changed=True), settings=settings)
    assert "AAPL" in error.value.message
    pinned_hash = load_manifest(v1).entries[0].companyfacts_sha256
    observed_hash = hashlib.sha256(b"new snapshot facts").hexdigest()
    assert pinned_hash in error.value.message and observed_hash in error.value.message
    assert "--accept-new-companyfacts-snapshot" in error.value.message
    assert v1.read_bytes() == before

    v2 = run(manifests_dir=directory, provider=FakeProvider(facts_changed=True), settings=settings,
             accept_new_companyfacts_snapshot=True)
    refreshed = load_manifest(v2)
    assert v2.name == "filings.v2.json"
    assert refreshed.manifest_version == "v2"
    assert refreshed.supersedes == "v1" and refreshed.change_reason == "companyfacts_snapshot_refresh"
    assert refreshed.entries[0].accession_number == load_manifest(v1).entries[0].accession_number
    assert refreshed.entries[0].companyfacts_sha256 != load_manifest(v1).entries[0].companyfacts_sha256
    assert v1.read_bytes() == before


def test_snapshot_refresh_still_rejects_changed_document(tmp_path, settings):
    directory = tmp_path / "manifests"
    v1 = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    before = v1.read_bytes()
    with pytest.raises(CacheIntegrityError):
        run(manifests_dir=directory, provider=FakeProvider(changed=True, facts_changed=True), settings=settings,
            accept_new_companyfacts_snapshot=True)
    assert v1.read_bytes() == before
    assert not (directory / "filings.v2.json").exists()


def test_repin_and_snapshot_refresh_flags_are_mutually_exclusive(tmp_path, settings):
    with pytest.raises(ValueError, match="mutually exclusive"):
        run(repin=True, accept_new_companyfacts_snapshot=True, manifests_dir=tmp_path,
            provider=FakeProvider(), settings=settings)


def test_snapshot_refresh_without_manifest_fails_without_writing(tmp_path, settings):
    directory = tmp_path / "manifests"
    with pytest.raises(ManifestNotFoundError) as error:
        run(manifests_dir=directory, provider=FakeProvider(), settings=settings,
            accept_new_companyfacts_snapshot=True)
    assert error.value.code == "manifest_not_found"
    assert list(directory.glob("filings.v*.json")) == []


def test_unchanged_snapshot_refresh_fails_without_writing(tmp_path, settings):
    directory = tmp_path / "manifests"
    v1 = run(manifests_dir=directory, provider=FakeProvider(), settings=settings)
    original = v1.read_bytes()
    with pytest.raises(CompanyFactsSnapshotUnchangedError) as error:
        run(manifests_dir=directory, provider=FakeProvider(), settings=settings,
            accept_new_companyfacts_snapshot=True)
    assert error.value.code == "companyfacts_snapshot_unchanged"
    assert v1.read_bytes() == original
    assert [p.name for p in directory.glob("filings.v*.json")] == ["filings.v1.json"]
