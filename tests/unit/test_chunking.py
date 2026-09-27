import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.ingestion.chunking import build_chunks
from app.ingestion.html_text import ExtractedBlock, extract_html
from app.ingestion.pipeline import build_filing_pipeline
from app.ingestion.sections import SectionName, SectionRecord
from app.retrieval.embeddings import FakeEmbedder

FIXTURES = Path(__file__).parents[1] / "fixtures" / "filings"


def manifest_entry(ticker="AAPL"):
    return SimpleNamespace(ticker=ticker, cik="0000320193" if ticker == "AAPL" else "0000789019",
        accession_number="0000320193-25-000079" if ticker == "AAPL" else "0001193125-26-323660",
        form="10-K", filing_date=date(2025, 10, 31) if ticker == "AAPL" else date(2026, 7, 29),
        source_url="https://www.sec.gov/Archives/edgar/data/example.htm")


def pipeline_for(ticker):
    html = (FIXTURES / f"{ticker.lower()}_excerpt.html").read_bytes()
    return build_filing_pipeline(manifest_entry(ticker), html, FakeEmbedder(), target_tokens=120, max_tokens=512)


def chunks_jsonl(chunks):
    return "".join(json.dumps(chunk.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n" for chunk in chunks)


def test_real_fixture_chunks_preserve_exact_spans_provenance_and_budget():
    embedder = FakeEmbedder()
    for ticker in ("AAPL", "MSFT"):
        result = pipeline_for(ticker)
        entry = manifest_entry(ticker)
        for chunk in result.chunks:
            assert chunk.text == result.canonical_text[chunk.start_offset:chunk.end_offset]
            assert chunk.content_hash == hashlib.sha256(chunk.text.encode()).hexdigest()
            assert chunk.canonical_sha256 == result.canonical_sha256
            assert chunk.token_count == embedder.count_tokens((chunk.table_context + "\n" if chunk.table_context else "") + chunk.text)
            assert chunk.token_count <= 512
            section = result.sections[chunk.section]
            assert section.start_offset <= chunk.start_offset < chunk.end_offset <= section.end_offset
            matching_blocks = [block for block in result.blocks if chunk.start_offset <= block.start_offset < chunk.end_offset]
            expected_type = "table" if matching_blocks[0].kind == "table_row" else "text"
            assert chunk.chunk_type == expected_type
            assert all((block.kind == "table_row") == (chunk.chunk_type == "table") for block in matching_blocks)
            evidence = chunk.to_evidence(retrieval_score=0.5)
            assert evidence.evidence_id == chunk.evidence_id and evidence.accession_number == entry.accession_number
            assert evidence.text == chunk.text and evidence.retrieval_score == 0.5


def test_table_chunks_include_caption_and_header_context():
    for ticker in ("AAPL", "MSFT"):
        result = pipeline_for(ticker)
        table_chunks = [chunk for chunk in result.chunks if chunk.chunk_type == "table"]
        assert table_chunks
        assert all(chunk.table_context and chunk.table_context.strip() for chunk in table_chunks)
        contexts = "\n".join(chunk.table_context or "" for chunk in table_chunks)
        assert "year" in contexts.lower() and "ended" in contexts.lower()
        assert "2025" in contexts or "2026" in contexts


def test_single_oversized_block_splits_without_losing_characters():
    text = "Annual revenue increased in the reporting period. " * 150
    canonical = text
    block = ExtractedBlock(kind="text", text=text, start_offset=0, end_offset=len(text))
    section = SectionRecord(section=SectionName.MDNA, heading_text="ITEM 7. MANAGEMENT DISCUSSION",
        start_offset=0, end_offset=len(canonical), section_sha256=hashlib.sha256(canonical.encode()).hexdigest())
    chunks = build_chunks(canonical_text=canonical, blocks=[block], sections={SectionName.MDNA: section},
        manifest_entry=manifest_entry(), count_tokens=FakeEmbedder().count_tokens,
        target_tokens=350, max_tokens=512, token_boundaries=FakeEmbedder().token_boundaries)
    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == text
    assert all(chunk.token_count <= 512 for chunk in chunks)
    assert all(chunks[i].end_offset == chunks[i + 1].start_offset for i in range(len(chunks) - 1))


def test_ids_jsonl_and_changed_character_are_deterministic_local():
    first = pipeline_for("AAPL")
    second = pipeline_for("AAPL")
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]
    assert [c.evidence_id for c in first.chunks] == [c.evidence_id for c in second.chunks]
    assert chunks_jsonl(first.chunks).encode() == chunks_jsonl(second.chunks).encode()

    parts = ["first block", "second block", "third block"]
    canonical = "\n".join(parts)
    blocks = []
    offset = 0
    for value in parts:
        blocks.append(ExtractedBlock(kind="text", text=value, start_offset=offset, end_offset=offset + len(value)))
        offset += len(value) + 1
    record = SectionRecord(section=SectionName.MDNA, heading_text="Section", start_offset=0, end_offset=len(canonical), section_sha256="0" * 64)
    fake = FakeEmbedder()
    args = dict(blocks=blocks, sections={SectionName.MDNA: record}, manifest_entry=manifest_entry(),
                count_tokens=fake.count_tokens, target_tokens=4, max_tokens=20)
    original = build_chunks(canonical_text=canonical, **args)
    changed_text = canonical.replace("third block", "third blOck")
    changed_blocks = [*blocks[:-1], ExtractedBlock(kind="text", text="third blOck", start_offset=blocks[-1].start_offset, end_offset=blocks[-1].end_offset)]
    changed = build_chunks(canonical_text=changed_text, **{**args, "blocks": changed_blocks})
    assert len(original) == len(changed) == 3
    assert [c.evidence_id for c in original[:2]] == [c.evidence_id for c in changed[:2]]
    assert original[2].evidence_id != changed[2].evidence_id
