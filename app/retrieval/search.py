from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.errors import (EmbeddingModelMismatchError,
                               NotIngestedError)
from app.domain.evidence import Evidence
from app.ingestion.chunking import CHUNKER_VERSION
from app.ingestion.html_text import EXTRACTOR_VERSION
from app.ingestion.manifest import (FilingManifest, latest_manifest_path, load_manifest,
                                    resolve_manifest_entry)
from app.ingestion.sections import SectionName
from app.retrieval.embeddings import Embedder


class SearchFilingChunksInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    company: str = Field(min_length=1)
    accession_number: str | None = None
    sections: list[SectionName] | None = None
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("question", "company")
    @classmethod
    def non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace")
        return value.strip()

    @field_validator("sections")
    @classmethod
    def sections_must_not_be_empty(cls, value):
        if value is not None and not value:
            raise ValueError("sections must contain at least one section; use None for all sections")
        return value


class SearchFilingChunksOutput(BaseModel):
    evidence: list[Evidence]
    accession_number: str
    accession_resolved_from_manifest: bool
    sections: list[SectionName] | None
    top_k: int
    embedding_model: str
    embedding_model_sha256: str


class SearchRepository(Protocol):
    def model_hashes_for_versions(self, accession_number: str, *, extractor_version: str,
                                  chunker_version: str) -> list[str]: ...

    def search_chunks(self, *, accession_number: str, query_vector: list[float],
                      embedding_model_sha256: str, sections: list[SectionName] | None,
                      top_k: int, extractor_version: str,
                      chunker_version: str) -> list[dict]: ...


def search_filing_chunks(
    request: SearchFilingChunksInput | dict,
    *,
    repository: SearchRepository,
    embedder: Embedder,
    manifest: FilingManifest | None = None,
    manifests_dir: Path | None = None,
    extractor_version: str = EXTRACTOR_VERSION,
    chunker_version: str = CHUNKER_VERSION,
) -> SearchFilingChunksOutput:
    parsed = request if isinstance(request, SearchFilingChunksInput) else SearchFilingChunksInput.model_validate(request)
    if manifest is None:
        if manifests_dir is None:
            manifests_dir = Path(__file__).resolve().parents[2] / "data" / "manifests"
        manifest = load_manifest(latest_manifest_path(Path(manifests_dir)))
    entry, resolved = resolve_manifest_entry(manifest, parsed.company, parsed.accession_number)

    stored_hashes = repository.model_hashes_for_versions(
        entry.accession_number, extractor_version=extractor_version, chunker_version=chunker_version,
    )
    if embedder.model_sha256 not in stored_hashes:
        if stored_hashes:
            raise EmbeddingModelMismatchError(embedder.model_sha256, stored_hashes)
        raise NotIngestedError(entry.accession_number)

    rows = repository.search_chunks(
        accession_number=entry.accession_number,
        query_vector=embedder.embed_query(parsed.question),
        embedding_model_sha256=embedder.model_sha256,
        sections=parsed.sections,
        top_k=parsed.top_k,
        extractor_version=extractor_version,
        chunker_version=chunker_version,
    )
    evidence = [Evidence(
        evidence_id=row["evidence_id"], ticker=row["ticker"], cik=row["cik"],
        accession_number=row["accession_number"], filing_form=row["filing_form"],
        filing_date=row["filing_date"], section=row["section"], chunk_id=row["chunk_id"],
        text=row["text"], source_url=row["source_url"], start_offset=row["start_offset"],
        end_offset=row["end_offset"], page_number=row["page_number"],
        content_hash=row["content_hash"], retrieval_score=1.0 - float(row["distance"]),
    ) for row in rows]
    return SearchFilingChunksOutput(
        evidence=evidence, accession_number=entry.accession_number,
        accession_resolved_from_manifest=resolved, sections=parsed.sections, top_k=parsed.top_k,
        embedding_model=embedder.model_id, embedding_model_sha256=embedder.model_sha256,
    )
