from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest

from app.config import Settings
from app.domain.errors import (EmbeddingModelMismatchError, IngestCountMismatchError,
                               NotIngestedError, AccessionNotInManifestError)
from app.ingestion.chunking import CHUNKER_VERSION, Chunk
from app.ingestion.html_text import EXTRACTOR_VERSION
from app.ingestion.manifest import FilingManifest, ManifestEntry
from app.ingestion.sections import SectionName
from app.retrieval.embeddings import FakeEmbedder
from app.retrieval.repository import Repository
from app.retrieval.search import SearchFilingChunksInput, search_filing_chunks


MODEL = FakeEmbedder()
MODEL_SHA = MODEL.model_sha256


@pytest.fixture
def repository():
    database_url = Settings(_env_file=None).database_url
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')

    def connect(url, **kwargs):
        return psycopg.connect(url, options=f"-c search_path={schema},public", **kwargs)

    repo = Repository(database_url, connection_factory=connect)
    try:
        repo.ensure_schema()
        yield repo
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def chunk(accession, evidence_id, *, section=SectionName.MDNA, text="income grew", offset=0,
          chunker_version=CHUNKER_VERSION):
    return Chunk(
        chunk_id=f"{accession}:{section.value}:{offset:04d}", evidence_id=evidence_id,
        ticker="MSFT" if accession.endswith("000001") else "AAPL", cik="0000000001",
        accession_number=accession, filing_form="10-K", filing_date=date(2026, 1, 1),
        section=section, heading_path=[section.value], chunk_type="text", text=text,
        table_context=None, start_offset=offset, end_offset=offset + len(text), page_number=None,
        content_hash=("a" * 64), token_count=MODEL.count_tokens(text),
        source_url="https://www.sec.gov/Archives/filing.htm", canonical_sha256="c" * 64,
        extractor_version=EXTRACTOR_VERSION, chunker_version=chunker_version,
    )


def _ingest(repo, chunks, vectors=None, *, model_hash=MODEL_SHA, chunker_version=CHUNKER_VERSION):
    vectors = vectors or MODEL.embed_documents([item.text for item in chunks])
    return repo.ingest_filing(
        chunks, vectors, embedding_model=MODEL.model_id, embedding_model_sha256=model_hash,
        allowed_accessions={item.accession_number for item in chunks},
        chunker_version=chunker_version,
    )


def _manifest():
    entries = []
    for ticker, cik, accession in [
        ("MSFT", "0000000001", "0000000001-26-000001"),
        ("AAPL", "0000000002", "0000000002-26-000002"),
    ]:
        entries.append(ManifestEntry(
            ticker=ticker, cik=cik, company_name=ticker, form="10-K", accession_number=accession,
            filing_date="2026-01-01", report_date="2025-12-31", primary_document="filing.htm",
            source_url="https://www.sec.gov/Archives/filing.htm", document_sha256="d" * 64,
            companyfacts_url="https://data.sec.gov/facts.json", companyfacts_sha256="f" * 64,
            retrieved_at="2026-01-02T00:00:00Z", selection_rule="test",
        ))
    return FilingManifest(created_at="2026-01-02T00:00:00Z", sec_user_agent_set=False, entries=entries)


pytestmark = pytest.mark.db


def test_reingest_is_idempotent(repository):
    repository.ensure_schema()
    chunks = [chunk("0000000001-26-000001", "ev_idempotent")]
    vectors = MODEL.embed_documents([chunks[0].text])
    assert _ingest(repository, chunks, vectors) == (1, 0)
    before = repository.count_rows(chunks[0].accession_number)
    assert _ingest(repository, chunks, vectors) == (0, 1)
    assert repository.count_rows(chunks[0].accession_number) == before == 1


def test_ingestion_refuses_accessions_outside_latest_manifest(repository):
    chunks = [chunk("not-pinned", "ev_unpinned")]
    with pytest.raises(AccessionNotInManifestError):
        repository.ingest_filing(
            chunks, MODEL.embed_documents([chunks[0].text]), embedding_model=MODEL.model_id,
            embedding_model_sha256=MODEL_SHA, allowed_accessions={"0000000001-26-000001"},
        )


def test_search_isolates_accession_even_when_other_accession_is_exact_query_match(repository):
    msft_acc, aapl_acc = "0000000001-26-000001", "0000000002-26-000002"
    query_text = "needle query"
    foreign = chunk(msft_acc, "ev_foreign", text=query_text)
    requested = chunk(aapl_acc, "ev_requested", text="unrelated wording")
    _ingest(repository, [foreign], [MODEL.embed_query(query_text)])
    _ingest(repository, [requested], [MODEL.embed_query("unrelated wording")])
    result = search_filing_chunks(
        SearchFilingChunksInput(question=query_text, company="AAPL", accession_number=aapl_acc),
        repository=repository, embedder=MODEL, manifest=_manifest(),
    )
    assert result.evidence
    assert {item.accession_number for item in result.evidence} == {aapl_acc}


def test_section_filter_and_legitimate_empty_filter(repository):
    accession = "0000000001-26-000001"
    chunks = [
        chunk(accession, "ev_mdna", section=SectionName.MDNA, text="management discussion"),
        chunk(accession, "ev_risk", section=SectionName.RISK_FACTORS, text="risk details", offset=30),
    ]
    _ingest(repository, chunks)
    mdna = search_filing_chunks(
        SearchFilingChunksInput(question="discussion", company="MSFT", sections=[SectionName.MDNA]),
        repository=repository, embedder=MODEL, manifest=_manifest(),
    )
    assert mdna.evidence and {hit.section for hit in mdna.evidence} == {"mdna"}
    empty = search_filing_chunks(
        SearchFilingChunksInput(question="discussion", company="MSFT", sections=[SectionName.FINANCIAL_STATEMENTS]),
        repository=repository, embedder=MODEL, manifest=_manifest(),
    )
    assert empty.evidence == []
    assert empty.sections == [SectionName.FINANCIAL_STATEMENTS]


def test_missing_and_other_model_rows_have_distinct_errors(repository):
    request = SearchFilingChunksInput(question="q", company="MSFT")
    with pytest.raises(NotIngestedError):
        search_filing_chunks(request, repository=repository, embedder=MODEL, manifest=_manifest())
    accession = "0000000001-26-000001"
    _ingest(repository, [chunk(accession, "ev_old_weights")], model_hash="other-model-weights")
    with pytest.raises(EmbeddingModelMismatchError) as error:
        search_filing_chunks(request, repository=repository, embedder=MODEL, manifest=_manifest())
    assert MODEL_SHA in str(error.value) and "other-model-weights" in str(error.value)


def test_rows_from_old_chunker_version_are_ignored(repository):
    accession = "0000000001-26-000001"
    _ingest(repository, [chunk(accession, "ev_old_chunker", chunker_version="chunker/0")],
            chunker_version="chunker/0")
    with pytest.raises(NotIngestedError):
        search_filing_chunks(SearchFilingChunksInput(question="q", company="MSFT"),
                             repository=repository, embedder=MODEL, manifest=_manifest())


def test_distance_ties_are_broken_by_evidence_id(repository):
    accession = "0000000001-26-000001"
    chunks = [chunk(accession, "ev_z", text="same", offset=0),
              chunk(accession, "ev_a", text="same", offset=20)]
    same_vector = MODEL.embed_query("same")
    _ingest(repository, chunks, [same_vector, same_vector])
    result = search_filing_chunks(
        SearchFilingChunksInput(question="same", company="MSFT", top_k=2),
        repository=repository, embedder=MODEL, manifest=_manifest(),
    )
    assert [hit.evidence_id for hit in result.evidence] == ["ev_a", "ev_z"]


def test_post_ingest_row_count_mismatch_is_a_typed_failure(repository):
    accession = "0000000001-26-000001"
    first, stray = chunk(accession, "ev_expected"), chunk(accession, "ev_stray", offset=30)
    _ingest(repository, [first, stray])
    with pytest.raises(IngestCountMismatchError):
        _ingest(repository, [first])
    assert repository.count_rows(accession) == 2
