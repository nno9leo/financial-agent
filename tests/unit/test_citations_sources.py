"""Fixture-backed tests for the three authoritative evidence sources."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.citations.ledger import (CanonicalChunkSource, CompanyFactsFactSource, EvidenceLedger,
                                  MetricRecomputeSource, fact_content_hash, metric_content_hash)
from app.compute.metrics import Metric
from app.domain.errors import (CompanyFactsSnapshotMismatchError, EvidenceHashMismatchError,
                               EvidenceNotFoundError)
from app.domain.financial_facts import normalize_companyfacts
from app.ingestion.chunking import Chunk
from app.ingestion.manifest import FilingManifest
from app.tools.calculate_financial_metric import (CalculateFinancialMetricInput,
                                                  calculate_financial_metric)


ROOT = Path(__file__).resolve().parents[2]
CHUNK_ACCESSION = "0000320193-25-000079"
CHUNK_FIXTURE = ROOT / "tests/fixtures/citations" / CHUNK_ACCESSION
MSFT_FIXTURE = ROOT / "tests/fixtures/companyfacts/msft_subset.json"


class FixtureProvider:
    def __init__(self, body):
        self.body = body

    def get_cached(self, _url):
        return self.body, {}


def _manifest_for_body(manifest, entry, body):
    updated = entry.model_copy(update={"companyfacts_sha256": hashlib.sha256(body).hexdigest()})
    return FilingManifest(created_at=manifest.created_at,
                          sec_user_agent_set=manifest.sec_user_agent_set,
                          entries=[updated if item.accession_number == entry.accession_number else item
                                   for item in manifest.entries])


def _fixture_facts(body, entry):
    return normalize_companyfacts(body, ticker=entry.ticker, cik=entry.cik, pinned=entry)


def _changed_fact_bytes(body, fact, increment=1):
    payload = json.loads(body)
    units = payload["facts"][fact.taxonomy][fact.tag]["units"][fact.unit]
    for row in units:
        if (row["accn"] == fact.accession_number and row["end"] == fact.period_end.isoformat()
                and row.get("start") == (fact.period_start.isoformat() if fact.period_start else None)):
            row["val"] += increment
            break
    else:
        raise AssertionError("fixture row for registered fact was not found")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def test_canonical_chunk_source_reloads_real_offset_slice_and_hash(manifest):
    rows = [json.loads(line) for line in (CHUNK_FIXTURE / "chunks.jsonl").read_text().splitlines()]
    ledger = EvidenceLedger({"chunk": CanonicalChunkSource(CHUNK_FIXTURE.parent)}, manifest)
    for row in rows:
        ledger.register_chunk(Chunk.model_validate(row).to_evidence(retrieval_score=None))
        resolved = ledger.resolve(row["evidence_id"])
        canonical = (CHUNK_FIXTURE / "canonical.txt").read_text(encoding="utf-8")
        expected = canonical[row["start_offset"]:row["end_offset"]]
        assert resolved.chunk.text == expected == row["text"]
        assert resolved.observed_hash == hashlib.sha256(expected.encode()).hexdigest()
        assert resolved.observed_hash == row["content_hash"]


def test_canonical_chunk_source_changed_byte_is_hash_mismatch(manifest, tmp_path):
    rows = [json.loads(line) for line in (CHUNK_FIXTURE / "chunks.jsonl").read_text().splitlines()]
    row = rows[0]
    normalized = tmp_path / "normalized"
    target = normalized / CHUNK_ACCESSION
    shutil.copytree(CHUNK_FIXTURE, target)
    canonical_path = target / "canonical.txt"
    canonical = canonical_path.read_text(encoding="utf-8")
    index = row["start_offset"]
    canonical_path.write_text(canonical[:index] + "X" + canonical[index + 1:], encoding="utf-8")
    ledger = EvidenceLedger({"chunk": CanonicalChunkSource(normalized)}, manifest)
    ledger.register_chunk(Chunk.model_validate(row).to_evidence(retrieval_score=None))
    with pytest.raises(EvidenceHashMismatchError) as exc:
        ledger.resolve(row["evidence_id"])
    assert exc.value.code == "hash_mismatch"


def test_canonical_chunk_source_missing_row_is_evidence_not_found(manifest, tmp_path):
    rows = [json.loads(line) for line in (CHUNK_FIXTURE / "chunks.jsonl").read_text().splitlines()]
    row = rows[0]
    normalized = tmp_path / "normalized"
    target = normalized / CHUNK_ACCESSION
    shutil.copytree(CHUNK_FIXTURE, target)
    (target / "chunks.jsonl").write_text("", encoding="utf-8")
    ledger = EvidenceLedger({"chunk": CanonicalChunkSource(normalized)}, manifest)
    ledger.register_chunk(Chunk.model_validate(row).to_evidence(retrieval_score=None))
    with pytest.raises(EvidenceNotFoundError) as exc:
        ledger.resolve(row["evidence_id"])
    assert exc.value.code == "evidence_not_found"


def test_companyfacts_source_reloads_fixture_fact_by_fact_id(manifest, msft_entry):
    body = MSFT_FIXTURE.read_bytes()
    local_manifest = _manifest_for_body(manifest, msft_entry, body)
    pinned = next(item for item in local_manifest.entries if item.accession_number == msft_entry.accession_number)
    fact = next(item for item in _fixture_facts(body, pinned)
                if item.tag == "RevenueFromContractWithCustomerExcludingAssessedTax" and item.accession_number == pinned.accession_number
                and item.period_end.isoformat() == "2026-06-30")
    source = CompanyFactsFactSource(local_manifest, provider=FixtureProvider(body))
    ledger = EvidenceLedger({"fact": source}, local_manifest)
    evidence_id = ledger.register_fact(fact)
    resolved = ledger.resolve(evidence_id)
    assert resolved.fact == fact
    assert evidence_id.startswith("fact_")
    assert resolved.observed_hash == fact_content_hash(fact)


def test_companyfacts_changed_pinned_value_is_hash_mismatch(manifest, msft_entry):
    original_body = MSFT_FIXTURE.read_bytes()
    original_manifest = _manifest_for_body(manifest, msft_entry, original_body)
    pinned = next(item for item in original_manifest.entries if item.accession_number == msft_entry.accession_number)
    fact = next(item for item in _fixture_facts(original_body, pinned)
                if item.tag == "RevenueFromContractWithCustomerExcludingAssessedTax" and item.accession_number == pinned.accession_number
                and item.period_end.isoformat() == "2026-06-30")
    changed_body = _changed_fact_bytes(original_body, fact, increment=1)
    changed_manifest = _manifest_for_body(manifest, msft_entry, changed_body)
    source = CompanyFactsFactSource(changed_manifest, provider=FixtureProvider(changed_body))
    ledger = EvidenceLedger({"fact": source}, changed_manifest)
    evidence_id = ledger.register_fact(fact)
    with pytest.raises(EvidenceHashMismatchError) as exc:
        ledger.resolve(evidence_id)
    assert exc.value.code == "hash_mismatch"


def test_companyfacts_manifest_hash_mismatch_is_typed(manifest, msft_entry):
    body = MSFT_FIXTURE.read_bytes()
    source = CompanyFactsFactSource(manifest, provider=FixtureProvider(body))
    local_manifest = _manifest_for_body(manifest, msft_entry, body)
    pinned = next(item for item in local_manifest.entries if item.accession_number == msft_entry.accession_number)
    fact = next(item for item in _fixture_facts(body, pinned)
                if item.tag == "RevenueFromContractWithCustomerExcludingAssessedTax" and item.accession_number == pinned.accession_number
                and item.period_end.isoformat() == "2026-06-30")
    ledger = EvidenceLedger({"fact": source}, local_manifest)
    evidence_id = ledger.register_fact(fact)
    with pytest.raises(CompanyFactsSnapshotMismatchError) as exc:
        source.load(ledger.entry(evidence_id))
    assert exc.value.code == "companyfacts_snapshot_mismatch"


def test_metric_source_recomputes_fixture_to_registered_hash(manifest, msft_entry):
    body = MSFT_FIXTURE.read_bytes()
    local_manifest = _manifest_for_body(manifest, msft_entry, body)
    provider = FixtureProvider(body)
    request = CalculateFinancialMetricInput(metric=Metric.OPERATING_MARGIN, company="MSFT", fiscal_year=2026,
                                            accession_number=msft_entry.accession_number)
    output = calculate_financial_metric(request, provider=provider, manifest=local_manifest)
    source = MetricRecomputeSource(local_manifest, provider=provider)
    ledger = EvidenceLedger({"metric": source}, local_manifest)
    evidence_id = ledger.register_metric(output)
    resolved = ledger.resolve(evidence_id)
    assert resolved.metric.result == output.result.result
    assert resolved.observed_hash == metric_content_hash(output.result)


def test_metric_source_changed_input_fact_is_hash_mismatch(manifest, msft_entry):
    original_body = MSFT_FIXTURE.read_bytes()
    local_manifest = _manifest_for_body(manifest, msft_entry, original_body)
    request = CalculateFinancialMetricInput(metric=Metric.OPERATING_MARGIN, company="MSFT", fiscal_year=2026,
                                            accession_number=msft_entry.accession_number)
    output = calculate_financial_metric(request, provider=FixtureProvider(original_body), manifest=local_manifest)
    fact = next(item for item in output.result.inputs
                if item.tag == "RevenueFromContractWithCustomerExcludingAssessedTax")
    changed_body = _changed_fact_bytes(original_body, fact, increment=1_000_000)
    changed_manifest = _manifest_for_body(manifest, msft_entry, changed_body)
    source = MetricRecomputeSource(changed_manifest, provider=FixtureProvider(changed_body))
    ledger = EvidenceLedger({"metric": source}, changed_manifest)
    evidence_id = ledger.register_metric(output)
    with pytest.raises(EvidenceHashMismatchError) as exc:
        ledger.resolve(evidence_id)
    assert exc.value.code == "hash_mismatch"
