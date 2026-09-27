from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import date
from typing import Literal

from pydantic import BaseModel, HttpUrl

from app.domain.evidence import Evidence
from app.domain.errors import ChunkTooLongError
from app.ingestion.html_text import EXTRACTOR_VERSION, ExtractedBlock
from app.ingestion.sections import SectionName, SectionRecord

CHUNKER_VERSION = "chunker/1"


class Chunk(BaseModel):
    chunk_id: str
    evidence_id: str
    ticker: str
    cik: str
    accession_number: str
    filing_form: str
    filing_date: date
    section: SectionName
    heading_path: list[str]
    chunk_type: Literal["text", "table"]
    text: str
    table_context: str | None
    start_offset: int
    end_offset: int
    page_number: int | None
    content_hash: str
    token_count: int
    source_url: HttpUrl
    canonical_sha256: str
    extractor_version: str
    chunker_version: str

    def to_evidence(self, retrieval_score: float | None) -> Evidence:
        return Evidence(evidence_id=self.evidence_id, ticker=self.ticker, cik=self.cik,
                        accession_number=self.accession_number, filing_form=self.filing_form,
                        filing_date=self.filing_date, section=self.section.value, chunk_id=self.chunk_id,
                        text=self.text, source_url=self.source_url, start_offset=self.start_offset,
                        end_offset=self.end_offset, page_number=self.page_number,
                        content_hash=self.content_hash, retrieval_score=retrieval_score)


def _embedding_input(text: str, context: str | None) -> str:
    return f"{context}\n{text}" if context else text


def embedding_input_for_chunk(chunk: Chunk) -> str:
    """Return the exact text used for token counting and embedding."""
    return _embedding_input(chunk.text, chunk.table_context)


def _split_oversized(text: str, absolute_start: int, context: str | None,
                     count_tokens: Callable[[str], int], max_tokens: int,
                     token_boundaries: Callable[[str], list[int]] | None = None) -> list[tuple[int, int]]:
    result = []
    local_start = 0
    while local_start < len(text):
        remaining = text[local_start:]
        if count_tokens(_embedding_input(remaining, context)) <= max_tokens:
            result.append((absolute_start + local_start, absolute_start + len(text)))
            break
        low, high = local_start + 1, len(text)
        fit = local_start
        while low <= high:
            mid = (low + high) // 2
            candidate = text[local_start:mid]
            if count_tokens(_embedding_input(candidate, context)) <= max_tokens:
                fit = mid
                low = mid + 1
            else:
                high = mid - 1
        if fit == local_start:
            actual = count_tokens(_embedding_input(text[local_start:local_start + 1], context))
            raise ChunkTooLongError(actual, max_tokens)
        # Prefer the last sentence boundary in the latter half of the fitting span.
        boundary_floor = local_start + max(1, (fit - local_start) // 2)
        boundary = None
        for match in re.finditer(r"[.!?](?:[\"’')\]]*)\s+", text[local_start:fit]):
            candidate_boundary = local_start + match.end()
            if candidate_boundary >= boundary_floor:
                boundary = candidate_boundary
        if boundary is None:
            token_ends = token_boundaries(text[local_start:fit]) if token_boundaries else []
            eligible = [local_start + endpoint for endpoint in token_ends if endpoint > 0]
            boundary = max(eligible, default=fit)
        result.append((absolute_start + local_start, absolute_start + boundary))
        local_start = boundary
    return result


def build_chunks(*, canonical_text: str, blocks: list[ExtractedBlock],
                 sections: dict[SectionName, SectionRecord], manifest_entry,
                 count_tokens: Callable[[str], int], target_tokens: int = 350,
                 max_tokens: int = 512, extractor_version: str = EXTRACTOR_VERSION,
                 token_boundaries: Callable[[str], list[int]] | None = None) -> list[Chunk]:
    if target_tokens <= 0 or max_tokens <= 0 or target_tokens > max_tokens:
        raise ValueError("token budgets must satisfy 0 < target_tokens <= max_tokens")
    canonical_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    output: list[Chunk] = []
    for section, section_record in sections.items():
        section_blocks = [block for block in blocks if section_record.start_offset <= block.start_offset < section_record.end_offset]
        table_contexts: dict[int, str] = {}
        preceding_text: list[str] = []
        for block in section_blocks:
            if block.kind == "text":
                preceding_text.append(block.text)
            elif block.table_id is not None and block.table_id not in table_contexts:
                headers = [candidate.text for candidate in section_blocks
                           if candidate.kind == "table_row" and candidate.table_id == block.table_id and candidate.table_header]
                caption = preceding_text[-2:]
                parts = caption + headers[:2]
                table_contexts[block.table_id] = "\n".join(parts)

        # Greedily pack complete adjacent blocks of a single type up to target_tokens.
        pending: list[tuple[ExtractedBlock, int, int, str | None, list[str]]] = []
        current_type: str | None = None
        current_start: int | None = None
        current_end: int | None = None
        current_context: str | None = None
        current_path: list[str] = [section_record.heading_text]
        latest_subheading: str | None = None

        def emit(start: int, end: int, chunk_type: str, context: str | None, path: list[str]) -> None:
            text = canonical_text[start:end]
            token_count = count_tokens(_embedding_input(text, context))
            if token_count > max_tokens:
                raise ChunkTooLongError(token_count, max_tokens)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            ordinal = sum(1 for prior in output if prior.section == section)
            chunk_id = f"{manifest_entry.accession_number}:{section.value}:{ordinal:04d}"
            id_input = "|".join([manifest_entry.accession_number, section.value, str(start), str(end),
                                 content_hash, extractor_version, CHUNKER_VERSION])
            evidence_id = "ev_" + hashlib.sha256(id_input.encode("utf-8")).hexdigest()[:20]
            output.append(Chunk(chunk_id=chunk_id, evidence_id=evidence_id, ticker=manifest_entry.ticker,
                cik=manifest_entry.cik, accession_number=manifest_entry.accession_number,
                filing_form=manifest_entry.form, filing_date=manifest_entry.filing_date,
                section=section, heading_path=path, chunk_type=chunk_type, text=text,
                table_context=context, start_offset=start, end_offset=end, page_number=None,
                content_hash=content_hash, token_count=token_count, source_url=manifest_entry.source_url,
                canonical_sha256=canonical_hash, extractor_version=extractor_version,
                chunker_version=CHUNKER_VERSION))

        def flush() -> None:
            nonlocal pending, current_type, current_start, current_end, current_context
            if pending and current_start is not None and current_end is not None and current_type is not None:
                emit(current_start, current_end, current_type, current_context, current_path.copy())
            pending = []
            current_type = current_start = current_end = current_context = None

        for block in section_blocks:
            if block.kind == "text" and block.bold and len(block.text) <= 120 and not block.text.endswith("."):
                latest_subheading = block.text
            path = [section_record.heading_text] + ([latest_subheading] if latest_subheading and latest_subheading != section_record.heading_text else [])
            kind = "table" if block.kind == "table_row" else "text"
            context = table_contexts.get(block.table_id) if kind == "table" and block.table_id is not None else None
            block_text = canonical_text[block.start_offset:block.end_offset]
            block_tokens = count_tokens(_embedding_input(block_text, context))
            if block_tokens > max_tokens:
                flush()
                for start, end in _split_oversized(block_text, block.start_offset, context, count_tokens,
                                                    max_tokens, token_boundaries):
                    emit(start, end, kind, context, path)
                continue
            if current_type is not None and (current_type != kind or current_context != context):
                flush()
            proposed_start = block.start_offset if current_start is None else current_start
            proposed_end = block.end_offset
            proposed_text = canonical_text[proposed_start:proposed_end]
            proposed_tokens = count_tokens(_embedding_input(proposed_text, context))
            if pending and proposed_tokens > target_tokens:
                flush()
                proposed_start, proposed_end = block.start_offset, block.end_offset
            current_type = kind
            current_start = proposed_start if current_start is None else current_start
            current_end = proposed_end
            current_context = context
            current_path = path
            pending.append((block, block.start_offset, block.end_offset, context, path))
        flush()
    return output
