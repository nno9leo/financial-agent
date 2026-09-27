import hashlib
import json
from pathlib import Path

import pytest

from app.domain.errors import ResearchError
from app.ingestion.manifest import load_manifest
from app.tools.get_filing_section import FilingSectionIntegrityError, get_filing_section
from app.tools.search_companies import search_companies
from app.tracing.jsonl import JsonlTraceWriter

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(ROOT / "data/manifests/filings.v1.json")


class Cache:
    def __init__(self, body): self.body=body
    def get_cached(self, url): return (self.body,{}) if self.body is not None else None


def test_company_search_match_order_normalized_cik_and_pinned():
    body=(ROOT / "tests/fixtures/sec/company_tickers.json").read_bytes()
    for query, typ in [("MSFT","ticker"),("0000789019","cik"),("789019","cik"),("microsoft","name")]:
        output=search_companies({"query":query},provider=Cache(body),manifest=MANIFEST)
        match=output.candidates[0]
        assert match.ticker == "MSFT" and match.match_type == typ and match.pinned
    multi=json.loads(body); multi["2"]={"cik_str":320193,"ticker":"APLE","title":"Apple Holdings"}
    output=search_companies({"query":"apple"},provider=Cache(json.dumps(multi).encode()),manifest=MANIFEST)
    assert output.ambiguous and [c.ticker for c in output.candidates] == ["AAPL","APLE"]
    with pytest.raises(ResearchError): search_companies({"query":"absent"},provider=Cache(body),manifest=MANIFEST)
    with pytest.raises(Exception): search_companies({"query":"mi"},provider=Cache(body),manifest=MANIFEST)


def test_filing_section_pages_concatenate_and_integrity_check(tmp_path):
    entry=next(e for e in MANIFEST.entries if e.ticker=="MSFT")
    from tests.unit.offline_fixture_data import seed_msft_normalized_excerpt
    target=tmp_path / entry.accession_number
    seed_msft_normalized_excerpt(tmp_path, entry.accession_number)
    canonical=(target/"canonical.txt").read_text(encoding="utf-8")
    (target/"chunks.jsonl").write_text(json.dumps({"canonical_sha256":hashlib.sha256(canonical.encode()).hexdigest()})+"\n")
    first=get_filing_section({"company":"MSFT","section":"financial_statements","max_chars":100},manifest=MANIFEST,normalized_dir=tmp_path)
    second=get_filing_section({"company":"MSFT","section":"financial_statements","offset":first.next_offset,"max_chars":100},manifest=MANIFEST,normalized_dir=tmp_path)
    assert first.text + second.text == canonical[first.start_offset:first.start_offset+200]
    (target/"canonical.txt").write_text(canonical+"corruption")
    with pytest.raises(FilingSectionIntegrityError): get_filing_section({"company":"MSFT","section":"financial_statements"},manifest=MANIFEST,normalized_dir=tmp_path)
    with pytest.raises(ResearchError): get_filing_section({"company":"MSFT","section":"financial_statements","offset":9999999},manifest=MANIFEST,normalized_dir=tmp_path)


def test_jsonl_writer_appends_atomic_json_lines(tmp_path):
    writer=JsonlTraceWriter(tmp_path)
    writer.write({"trace_id":"one"}); writer.write({"trace_id":"two"})
    paths=list(tmp_path.glob("*.jsonl"))
    lines=paths[0].read_text().splitlines()
    assert [json.loads(line)["trace_id"] for line in lines] == ["one","two"]
