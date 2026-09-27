from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.errors import AmbiguousSectionError, SectionNotFoundError
from app.ingestion.html_text import extract_html
from app.ingestion.sections import REQUIRED_ITEMS, SectionName, detect_headings, extract_sections

FIXTURES = Path(__file__).parents[1] / "fixtures" / "filings"


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT"])
def test_only_real_item_heading_blocks_define_sections(ticker):
    extracted = extract_html((FIXTURES / f"{ticker.lower()}_excerpt.html").read_bytes(), ticker)
    headings = detect_headings(extracted.blocks)
    sections = extract_sections(extracted.canonical_text, extracted.blocks)
    for section, item in REQUIRED_ITEMS.items():
        assert sum(candidate.item == item for candidate in headings) == 1
        assert section in sections
    assert not any(candidate.heading_text.startswith("Item 7.") and extracted.blocks[candidate.block_index].kind == "table_row" for candidate in headings)


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT"])
def test_section_boundaries_end_at_the_next_item_heading(ticker):
    extracted = extract_html((FIXTURES / f"{ticker.lower()}_excerpt.html").read_bytes(), ticker)
    headings = detect_headings(extracted.blocks)
    sections = extract_sections(extracted.canonical_text, extracted.blocks)
    by_item = {heading.item: heading for heading in headings}
    assert sections[SectionName.RISK_FACTORS].end_offset == by_item["1B"].start_offset
    assert sections[SectionName.MDNA].end_offset == by_item["7A"].start_offset
    assert sections[SectionName.FINANCIAL_STATEMENTS].end_offset == by_item["9"].start_offset


def test_missing_and_duplicate_required_headings_are_typed_with_candidates():
    extracted = extract_html((FIXTURES / "aapl_excerpt.html").read_bytes(), "AAPL")
    headings = detect_headings(extracted.blocks)
    item_1a_index = next(candidate.block_index for candidate in headings if candidate.item == "1A")
    without_item_1a = [block for i, block in enumerate(extracted.blocks) if i != item_1a_index]
    with pytest.raises(SectionNotFoundError) as missing:
        extract_sections(extracted.canonical_text, without_item_1a)
    assert missing.value.section == SectionName.RISK_FACTORS.value
    assert missing.value.candidates == []

    duplicate = list(extracted.blocks)
    duplicate.insert(item_1a_index + 1, replace(extracted.blocks[item_1a_index]))
    with pytest.raises(AmbiguousSectionError) as ambiguous:
        extract_sections(extracted.canonical_text, duplicate)
    assert ambiguous.value.section == SectionName.RISK_FACTORS.value
    assert len(ambiguous.value.candidates) == 2
    assert all("block_index" in item and "start_offset" in item for item in ambiguous.value.candidates)


def test_running_headers_are_removed_from_relevant_section_text():
    for ticker in ("AAPL", "MSFT"):
        extracted = extract_html((FIXTURES / f"{ticker.lower()}_excerpt.html").read_bytes(), ticker)
        sections = extract_sections(extracted.canonical_text, extracted.blocks)
        mdna = extracted.canonical_text[sections[SectionName.MDNA].start_offset:sections[SectionName.MDNA].end_offset]
        financials = extracted.canonical_text[sections[SectionName.FINANCIAL_STATEMENTS].start_offset:sections[SectionName.FINANCIAL_STATEMENTS].end_offset]
        if ticker == "AAPL":
            assert "Apple Inc. | 2025 Form 10-K | 21" not in mdna
        else:
            assert "PART II" not in mdna
            assert "Item 8" not in financials
