from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.errors import (AccessionCompanyMismatchError, AccessionNotInManifestError,
                               AmbiguousManifestCompanyError,
                               CompanyNotInManifestError, EmbeddingModelMismatchError,
                               ManifestNotFoundError, NotIngestedError)
from app.config import LOCAL_DATABASE_URL, Settings
from app.ingestion.manifest import FilingManifest, ManifestEntry, latest_manifest_path, resolve_manifest_entry
from app.ingestion.sections import SectionName
from app.retrieval.embeddings import FakeEmbedder
from app.retrieval.repository import mask_database_url
from app.retrieval.search import SearchFilingChunksInput, SearchFilingChunksOutput, search_filing_chunks


def _entry(ticker="MSFT", accession="0000000001-26-000001", cik="0000000001"):
    return ManifestEntry(
        ticker=ticker, cik=cik, company_name=ticker, form="10-K", accession_number=accession,
        filing_date="2026-01-01", report_date="2025-12-31", primary_document="filing.htm",
        source_url=f"https://www.sec.gov/{accession}/filing.htm", document_sha256="d" * 64,
        companyfacts_url="https://data.sec.gov/facts.json", companyfacts_sha256="f" * 64,
        retrieved_at="2026-01-02T00:00:00Z", selection_rule="test",
    )


def _manifest():
    return FilingManifest(created_at="2026-01-02T00:00:00Z", sec_user_agent_set=False, entries=[
        _entry(), _entry("AAPL", "0000000002-26-000002", "0000000002"),
    ])


class FakeRepository:
    def __init__(self, hashes=None, rows=None):
        self.hashes = hashes if hashes is not None else [FakeEmbedder.model_sha256]
        self.rows = rows if rows is not None else []
        self.last_accession = None

    def model_hashes_for_versions(self, accession_number, **kwargs):
        self.last_accession = accession_number
        return self.hashes

    def search_chunks(self, **kwargs):
        assert kwargs["accession_number"] == self.last_accession
        return self.rows


def _row(accession="0000000001-26-000001"):
    return {
        "evidence_id": "ev_123", "ticker": "MSFT", "cik": "0000000001",
        "accession_number": accession, "filing_form": "10-K", "filing_date": date(2026, 1, 1),
        "section": "mdna", "chunk_id": "chunk-1", "text": "Operating income increased.",
        "source_url": "https://www.sec.gov/filing.htm", "start_offset": 0, "end_offset": 28,
        "page_number": None, "content_hash": "a" * 64, "distance": 0.125,
    }


def test_search_input_validation_and_output_shape():
    with pytest.raises(ValidationError):
        SearchFilingChunksInput(question="  ", company="MSFT")
    with pytest.raises(ValidationError):
        SearchFilingChunksInput(question="q", company="MSFT", top_k=21)
    repo = FakeRepository(rows=[_row()])
    result = search_filing_chunks(
        {"question": "How did income change?", "company": "MSFT", "top_k": 1},
        repository=repo, embedder=FakeEmbedder(), manifest=_manifest(),
    )
    assert isinstance(result, SearchFilingChunksOutput)
    assert result.accession_number == "0000000001-26-000001"
    assert result.accession_resolved_from_manifest is True
    assert result.top_k == 1
    assert result.embedding_model == FakeEmbedder.model_id
    assert result.evidence[0].retrieval_score == pytest.approx(0.875)


def test_latest_manifest_is_selected_by_version_number(tmp_path):
    (tmp_path / "filings.v2.json").write_text(_manifest().model_dump_json(), encoding="utf-8")
    (tmp_path / "filings.v10.json").write_text(_manifest().model_dump_json(), encoding="utf-8")
    assert latest_manifest_path(tmp_path).name == "filings.v10.json"
    with pytest.raises(ManifestNotFoundError):
        latest_manifest_path(tmp_path / "empty")


@pytest.mark.parametrize("company,accession,error", [
    ("NOPE", None, CompanyNotInManifestError),
    ("MSFT", "missing-accession", AccessionNotInManifestError),
    ("MSFT", "0000000002-26-000002", AccessionCompanyMismatchError),
])
def test_manifest_identity_errors_are_typed(company, accession, error):
    with pytest.raises(error):
        search_filing_chunks(
            SearchFilingChunksInput(question="q", company=company, accession_number=accession),
            repository=FakeRepository(), embedder=FakeEmbedder(), manifest=_manifest(),
        )


def test_omitted_accession_rejects_ambiguous_company_manifest():
    manifest = _manifest()
    msft = manifest.entries[0]
    manifest = manifest.model_copy(update={"entries": [
        msft, msft.model_copy(update={"accession_number": "0000000001-25-000009"}),
        manifest.entries[1],
    ]})
    with pytest.raises(AmbiguousManifestCompanyError) as error:
        search_filing_chunks(
            SearchFilingChunksInput(question="q", company="MSFT"),
            repository=FakeRepository(), embedder=FakeEmbedder(), manifest=manifest,
        )
    assert error.value.accessions == ["0000000001-26-000001", "0000000001-25-000009"]


def test_not_ingested_and_model_mismatch_are_distinct_typed_errors():
    request = SearchFilingChunksInput(question="q", company="MSFT")
    with pytest.raises(NotIngestedError) as error:
        search_filing_chunks(request, repository=FakeRepository(hashes=[]), embedder=FakeEmbedder(), manifest=_manifest())
    assert "scripts/ingest_chunks.py" in error.value.fallback
    with pytest.raises(EmbeddingModelMismatchError) as error:
        search_filing_chunks(request, repository=FakeRepository(hashes=["other-weight-hash"]),
                             embedder=FakeEmbedder(), manifest=_manifest())
    assert FakeEmbedder.model_sha256 in str(error.value)
    assert "other-weight-hash" in str(error.value)


def test_empty_section_filter_is_returned_as_a_valid_empty_result():
    request = SearchFilingChunksInput(question="q", company="MSFT", sections=[SectionName.FINANCIAL_STATEMENTS])
    result = search_filing_chunks(request, repository=FakeRepository(rows=[]), embedder=FakeEmbedder(), manifest=_manifest())
    assert result.evidence == []
    assert result.sections == [SectionName.FINANCIAL_STATEMENTS]
    with pytest.raises(ValidationError, match="sections must contain at least one"):
        SearchFilingChunksInput(question="q", company="MSFT", sections=[])


def test_database_url_password_is_masked():
    masked = mask_database_url("postgresql://user:secret@localhost:5432/db")
    assert masked == "postgresql://user:***@localhost:5432/db"
    assert "secret" not in masked
    query_masked = mask_database_url("postgresql://localhost/db?password=secret")
    assert "secret" not in query_masked and "password=%2A%2A%2A" in query_masked


def test_empty_database_url_uses_local_dev_default_and_scripts_load_runtime_settings():
    assert Settings(_env_file=None, database_url="").database_url == LOCAL_DATABASE_URL
    assert latest_manifest_path.__module__ == "app.ingestion.manifest"
    assert resolve_manifest_entry.__module__ == "app.ingestion.manifest"
    for script in ("scripts/ingest_chunks.py", "scripts/search.py", "scripts/smoke_search.py"):
        source = (Path(__file__).parents[2] / script).read_text("utf-8")
        assert "Settings()" in source
        assert "_env_file=None" not in source
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text("utf-8")
    for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        assert f"{key}: ${{{key}:-" in compose
