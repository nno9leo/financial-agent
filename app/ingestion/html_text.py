from __future__ import annotations

import re
import warnings
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

EXTRACTOR_VERSION = "html_text/1"
BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
RUNNING_HEADER_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "MSFT": (
        ("msft_part_header", re.compile(r"^PART\s+(?:I|II|III|IV)$", re.I)),
        ("msft_item_header", re.compile(r"^ITEM\s+\d{1,2}[A-C]?$", re.I)),
        ("page_number", re.compile(r"^\d{1,3}$")),
    ),
    "AAPL": (
        ("aapl_company_footer", re.compile(r"^Apple Inc\.$", re.I)),
        ("aapl_form_footer", re.compile(r"^\d{4} Form 10-K$", re.I)),
        ("aapl_full_page_footer", re.compile(r"^Apple Inc\.\s*\|\s*\d{4} Form 10-K\s*\|\s*\d{1,3}$", re.I)),
        ("page_number", re.compile(r"^\d{1,3}$")),
    ),
}


@dataclass(frozen=True)
class ExtractedBlock:
    kind: Literal["text", "table_row"]
    text: str
    start_offset: int
    end_offset: int
    bold: bool = False
    table_id: int | None = None
    table_row: int | None = None
    table_header: bool = False


@dataclass(frozen=True)
class ExtractedDocument:
    canonical_text: str
    blocks: list[ExtractedBlock]
    removed_patterns: dict[str, int]


def _hidden(tag: Tag) -> bool:
    if not isinstance(tag.attrs, dict):
        return True
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    style = re.sub(r"\s+", "", str(tag.get("style", "")).lower())
    return "display:none" in style or "visibility:hidden" in style


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ").replace("\u2009", " ")).strip()


def _inline_text(node: Tag) -> str:
    return _normalize(node.get_text(separator="", strip=False))


def _has_structural_descendant(tag: Tag) -> bool:
    return tag.find(list(BLOCK_TAGS | {"table", "br"})) is not None


def _cell_text(node: Tag) -> str:
    """Join inline descendants tightly; separate cell-internal blocks and breaks."""
    parts: list[str] = []

    def visit(current: Tag) -> None:
        for child in current.children:
            if isinstance(child, NavigableString):
                parts.append(str(child))
                continue
            if not isinstance(child, Tag) or _hidden(child):
                continue
            name = (child.name or "").lower()
            if name == "br":
                parts.append(" ")
            elif name == "table":
                continue
            elif name in BLOCK_TAGS or _has_structural_descendant(child):
                before = len(parts)
                visit(child)
                if len(parts) > before:
                    parts.insert(before, " ")
                    parts.append(" ")
            else:
                visit(child)

    visit(node)
    return _normalize("".join(parts))


def _is_bold(tag: Tag) -> bool:
    for child in [tag, *tag.find_all(True)]:
        if child.name in {"b", "strong"}:
            return True
        style = str(child.get("style", "")).lower()
        match = re.search(r"font-weight\s*:\s*(bold|[6-9]00)", style)
        if match:
            return True
    return False


def _row_text(cells: list[str]) -> str:
    cleaned = [cell for cell in cells if cell]
    merged: list[str] = []
    i = 0
    while i < len(cleaned):
        cell = cleaned[i]
        if cell == "$" and i + 1 < len(cleaned):
            merged.append("$ " + cleaned[i + 1].lstrip())
            i += 2
            continue
        if cell == ")" and merged:
            merged[-1] = merged[-1].rstrip() + ")"
        else:
            merged.append(cell)
        i += 1
    return _normalize(" | ".join(merged))


def _append_block(raw_blocks: list[dict], kind: str, text: str, **meta) -> None:
    normalized = _normalize(text)
    if normalized:
        raw_blocks.append({"kind": kind, "text": normalized, **meta})


def extract_html(html: bytes | str, ticker: str) -> ExtractedDocument:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    for node in list(soup.find_all(True)):
        if not isinstance(node.attrs, dict):
            continue
        name = (node.name or "").lower()
        if name in {"script", "style", "ix:header"} or name.endswith(":header") or _hidden(node):
            node.decompose()

    root = soup.body or soup
    raw_blocks: list[dict] = []
    table_counter = 0

    def process_table(table: Tag) -> None:
        nonlocal table_counter
        current_id = table_counter
        table_counter += 1
        row_index = 0
        nonempty_row_index = 0
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all(["td", "th"], recursive=False)
            if not cells:
                cells = row.find_all(["td", "th"])
            text = _row_text([_cell_text(cell) for cell in cells])
            is_header = row.find("th") is not None or (nonempty_row_index <= 1 and bool(text))
            _append_block(raw_blocks, "table_row", text, table_id=current_id, table_row=row_index, table_header=is_header)
            if text:
                nonempty_row_index += 1
            row_index += 1

    def process_node(tag: Tag) -> None:
        name = (tag.name or "").lower()
        if _hidden(tag) or name in {"script", "style"}:
            return
        if name == "table":
            process_table(tag)
        elif name in BLOCK_TAGS:
            process_block(tag)
        elif not _has_structural_descendant(tag):
            _append_block(raw_blocks, "text", tag.get_text(separator="", strip=False), bold=_is_bold(tag))
        else:
            # Unknown/namespaced tags and inline wrappers containing blocks are
            # transparent. This includes ix:nonNumeric TextBlock containers.
            inline_parts: list[str] = []

            def flush_inline() -> None:
                if inline_parts:
                    _append_block(raw_blocks, "text", "".join(inline_parts), bold=_is_bold(tag))
                    inline_parts.clear()

            for nested in tag.children:
                if isinstance(nested, NavigableString):
                    inline_parts.append(str(nested))
                elif isinstance(nested, Tag):
                    nested_name = (nested.name or "").lower()
                    if nested_name in BLOCK_TAGS or nested_name == "table" or _has_structural_descendant(nested):
                        flush_inline()
                        process_node(nested)
                    elif not _hidden(nested):
                        inline_parts.append(nested.get_text(separator="", strip=False))
            flush_inline()

    def process_block(tag: Tag) -> None:
        pieces: list[str] = []

        def flush() -> None:
            if pieces:
                _append_block(raw_blocks, "text", "".join(pieces), bold=_is_bold(tag))
                pieces.clear()

        for child in tag.children:
            if isinstance(child, NavigableString):
                pieces.append(str(child))
            elif isinstance(child, Tag):
                if _hidden(child) or child.name in {"script", "style"}:
                    continue
                name = (child.name or "").lower()
                if name == "br":
                    flush()
                elif name in BLOCK_TAGS or name == "table" or _has_structural_descendant(child):
                    flush()
                    process_node(child)
                else:
                    pieces.append(child.get_text(separator="", strip=False))
        flush()

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Tag):
                process_node(child)

    walk(root)
    patterns = RUNNING_HEADER_PATTERNS.get(ticker.upper())
    if patterns is None:
        raise ValueError(f"No explicit running-header patterns are defined for {ticker!r}.")
    removed = Counter({name: 0 for name, _ in patterns})
    kept = []
    for block in raw_blocks:
        if block["kind"] == "text":
            for pattern_name, pattern in patterns:
                if pattern.fullmatch(block["text"]):
                    removed[pattern_name] += 1
                    break
            else:
                kept.append(block)
        else:
            kept.append(block)

    pieces: list[str] = []
    blocks: list[ExtractedBlock] = []
    offset = 0
    for item in kept:
        text = item["text"]
        start = offset
        end = start + len(text)
        blocks.append(ExtractedBlock(start_offset=start, end_offset=end, **item))
        pieces.append(text)
        offset = end + 1
    return ExtractedDocument("\n".join(pieces), blocks, dict(removed))
