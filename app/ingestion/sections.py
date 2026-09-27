from __future__ import annotations

import hashlib
import re
from datetime import date
from enum import Enum

from pydantic import BaseModel

from app.domain.errors import AmbiguousSectionError, SectionNotFoundError
from app.ingestion.html_text import ExtractedBlock


class SectionName(str, Enum):
    RISK_FACTORS = "risk_factors"
    MDNA = "mdna"
    FINANCIAL_STATEMENTS = "financial_statements"


REQUIRED_ITEMS = {
    SectionName.RISK_FACTORS: "1A",
    SectionName.MDNA: "7",
    SectionName.FINANCIAL_STATEMENTS: "8",
}
ITEM_PATTERN = re.compile(r"^ITEM\s+(1A|1B|1C|1|2|3|4|5|6|7A|7|8|9A|9B|9C|9|1[0-6])\.\s+(.+)$", re.I)


class HeadingCandidate(BaseModel):
    block_index: int
    item: str
    heading_text: str
    start_offset: int
    end_offset: int


class SectionRecord(BaseModel):
    section: SectionName
    heading_text: str
    start_offset: int
    end_offset: int
    section_sha256: str


def detect_headings(blocks: list[ExtractedBlock]) -> list[HeadingCandidate]:
    found = []
    for index, block in enumerate(blocks):
        if block.kind != "text":
            continue
        match = ITEM_PATTERN.fullmatch(block.text.strip())
        if match:
            item = match.group(1).upper()
            found.append(HeadingCandidate(block_index=index, item=item, heading_text=block.text,
                                          start_offset=block.start_offset, end_offset=block.end_offset))
    return found


def extract_sections(canonical_text: str, blocks: list[ExtractedBlock]) -> dict[SectionName, SectionRecord]:
    headings = detect_headings(blocks)
    by_item: dict[str, list[HeadingCandidate]] = {}
    for heading in headings:
        by_item.setdefault(heading.item, []).append(heading)
    output = {}
    for section, item in REQUIRED_ITEMS.items():
        candidates = by_item.get(item, [])
        serialized = [candidate.model_dump(mode="json") for candidate in candidates]
        if not candidates:
            raise SectionNotFoundError(section.value, serialized)
        if len(candidates) > 1:
            raise AmbiguousSectionError(section.value, serialized)
        heading = candidates[0]
        next_headings = [candidate for candidate in headings if candidate.start_offset > heading.start_offset]
        end = next_headings[0].start_offset if next_headings else len(canonical_text)
        section_text = canonical_text[heading.start_offset:end]
        output[section] = SectionRecord(section=section, heading_text=heading.heading_text,
                                        start_offset=heading.start_offset, end_offset=end,
                                        section_sha256=hashlib.sha256(section_text.encode("utf-8")).hexdigest())
    return output
