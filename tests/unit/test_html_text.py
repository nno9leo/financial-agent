from pathlib import Path

from app.ingestion.html_text import extract_html

FIXTURES = Path(__file__).parents[1] / "fixtures" / "filings"


def test_real_excerpts_strip_header_hidden_content_and_join_msft_heading():
    msft = extract_html((FIXTURES / "msft_excerpt.html").read_bytes(), "MSFT")
    assert "header-stub-secret" not in msft.canonical_text
    assert "ITEM 1A. RISK FACTORS" in msft.canonical_text
    assert not any("ITEM 1A. RIS K" in block.text for block in msft.blocks)
    aapl = extract_html((FIXTURES / "aapl_excerpt.html").read_bytes(), "AAPL")
    assert "header-stub-secret" not in aapl.canonical_text


def test_display_none_and_ix_header_content_are_removed():
    html = """<html><body><ix:header><p>hidden-header-secret</p></ix:header>
    <div style="display: none">hidden-style-secret</div>
    <p>Visible <span>inline</span> text</p></body></html>"""
    extracted = extract_html(html, "AAPL")
    assert extracted.canonical_text == "Visible inline text"


def test_transparent_namespaced_container_preserves_inline_text_and_recurses():
    html = """<html><body><ix:nonNumeric>prefix<p>paragraph</p><table>
    <tr><td>Cell A</td><td>Cell B</td></tr></table>suffix</ix:nonNumeric></body></html>"""
    extracted = extract_html(html, "MSFT")
    assert [block.text for block in extracted.blocks] == [
        "prefix", "paragraph", "Cell A | Cell B", "suffix"
    ]
    assert [block.kind for block in extracted.blocks] == ["text", "text", "table_row", "text"]


def test_named_running_patterns_remove_only_standalone_non_table_blocks():
    html = """<html><body><p>PART II</p><p>Item 8</p><p>Item 1A</p><p>Item 15</p>
    <p>ITEM 1A. RISK FACTORS</p><p>Apple Inc.</p><p>2027 Form 10-K</p>
    <table><tr><td>PART II</td><td>Item 1A</td><td>Apple Inc.</td></tr></table></body></html>"""
    msft = extract_html(html, "MSFT")
    assert msft.removed_patterns["msft_part_header"] == 1
    assert msft.removed_patterns["msft_item_header"] == 3
    assert "ITEM 1A. RISK FACTORS" in msft.canonical_text
    assert not any(block.kind == "text" and block.text in {"Item 1A", "Item 15"} for block in msft.blocks)
    assert any(block.kind == "table_row" and "PART II" in block.text and "Item 1A" in block.text for block in msft.blocks)
    aapl = extract_html(html, "AAPL")
    assert aapl.removed_patterns["aapl_company_footer"] == 1
    assert aapl.removed_patterns["aapl_form_footer"] == 1
    assert any(block.kind == "table_row" and "Apple Inc." in block.text for block in aapl.blocks)


def test_table_currency_and_negative_cells_are_merged():
    html = """<table><tr><td>Revenue</td><td>$</td><td>281,724</td><td>(1,234</td><td>)</td></tr></table>"""
    extracted = extract_html(html, "MSFT")
    row = next(block.text for block in extracted.blocks if block.kind == "table_row")
    assert row == "Revenue | $ 281,724 | (1,234)"
    assert " | $ | " not in row and " | )" not in row


def test_real_fixtures_are_trimmed_and_record_source_provenance():
    for ticker in ("aapl", "msft"):
        path = FIXTURES / f"{ticker}_excerpt.html"
        content = path.read_bytes()
        assert len(content) < 150_000
        assert content.startswith(b"<!-- Source accession:")
        assert b"source byte ranges:" in content[:1000]
        assert b"<ix:header" in content


def test_real_ix_non_numeric_note_keeps_paragraphs_and_table_rows_separate():
    extracted = extract_html((FIXTURES / "msft_excerpt.html").read_bytes(), "MSFT")
    note_at = next(i for i, block in enumerate(extracted.blocks) if block.text == "NOTE 4 — INVESTMENTS")
    note_blocks = extracted.blocks[note_at:note_at + 15]
    assert any(block.text == "Investment Components" for block in note_blocks)
    assert any(block.text == "The components of investments were as follows:" for block in note_blocks)
    rows = [block for block in note_blocks if block.kind == "table_row"]
    assert rows
    assert all(" | " in block.text for block in rows)
    assert "INVESTMENTSInvestment" not in extracted.canonical_text


def test_real_table_header_cell_breaks_are_joined_with_spaces():
    extracted = extract_html((FIXTURES / "msft_excerpt.html").read_bytes(), "MSFT")
    rows = [block.text for block in extracted.blocks if block.kind == "table_row"]
    expected = ("Fair Value Level", "Adjusted Cost Basis", "Cash and Cash Equivalents", "Percentage Change")
    for phrase in expected:
        assert any(phrase in row for row in rows), phrase
