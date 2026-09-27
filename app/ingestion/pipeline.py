from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.domain.errors import ResearchError
from app.ingestion.chunking import Chunk, build_chunks
from app.ingestion.html_text import EXTRACTOR_VERSION, ExtractedBlock, extract_html
from app.ingestion.sections import SectionName, SectionRecord, extract_sections
from app.retrieval.embeddings import Embedder


@dataclass(frozen=True)
class FilingPipelineResult:
    canonical_text: str
    canonical_sha256: str
    blocks: list[ExtractedBlock]
    sections: dict[SectionName, SectionRecord]
    chunks: list[Chunk]
    removed_patterns: dict[str, int]


def build_filing_pipeline(manifest_entry, html: bytes | str, embedder: Embedder,
                          *, target_tokens: int = 350, max_tokens: int = 512) -> FilingPipelineResult:
    extracted = extract_html(html, manifest_entry.ticker)
    sections = extract_sections(extracted.canonical_text, extracted.blocks)
    canonical_hash = hashlib.sha256(extracted.canonical_text.encode("utf-8")).hexdigest()
    chunks = build_chunks(canonical_text=extracted.canonical_text, blocks=extracted.blocks,
                          sections=sections, manifest_entry=manifest_entry,
                          count_tokens=embedder.count_tokens, target_tokens=target_tokens,
                          max_tokens=max_tokens, extractor_version=EXTRACTOR_VERSION,
                          token_boundaries=getattr(embedder, "token_boundaries", None))
    return FilingPipelineResult(extracted.canonical_text, canonical_hash, extracted.blocks,
                                sections, chunks, extracted.removed_patterns)
