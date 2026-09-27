import hashlib

import pytest

from app.domain.errors import CacheIntegrityError
from app.ingestion.manifest import build_manifest, manifest_bytes, verify_entry_hashes


def row(ticker):
    doc, facts = b"doc", b"facts"
    return {"ticker": ticker, "cik": "0000000001", "company_name": "Example", "form": "10-K",
            "accession_number": "0000000001-25-000001", "filing_date": "2025-02-01", "report_date": "2024-12-31",
            "primary_document": "annual.htm", "source_url": "https://www.sec.gov/doc",
            "document_sha256": hashlib.sha256(doc).hexdigest(), "companyfacts_url": "https://data.sec.gov/facts",
            "companyfacts_sha256": hashlib.sha256(facts).hexdigest(), "retrieved_at": "2025-02-03T00:00:00Z",
            "selection_rule": "latest original 10-K"}


def test_manifest_deterministic_and_sorted():
    first = manifest_bytes(build_manifest([row("MSFT"), row("AAPL")], created_at="2025-02-03T00:00:00Z"))
    second = manifest_bytes(build_manifest([row("AAPL"), row("MSFT")], created_at="2025-02-03T00:00:00Z"))
    assert first == second and first.endswith(b"\n")


def test_pinned_hash_change_is_hard_failure():
    entry = build_manifest([row("MSFT")], created_at="2025-02-03T00:00:00Z").entries[0]
    with pytest.raises(CacheIntegrityError): verify_entry_hashes(entry, b"changed", b"facts")
