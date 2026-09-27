"""Real-derived citation evidence; chunks use offset/text rows from real canonical files.

tests/fixtures/citations/real_chunks.json copies three real chunk records, including
their real offsets and exact canonical slices. In-memory sources reload those slices
to keep the default suite independent of the shared normalized cache.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.citations.ledger import (EvidenceLedger, ResolvedEvidence, fact_content_hash,
                                  metric_content_hash)
from app.citations.semantic import JudgeResult, ScriptedJudge
from app.citations.verifier import verify_claims
from app.compute.metrics import Metric, calculate_metric
from app.domain.claims import Claim, MetadataAssertion, NumericAssertion
from app.domain.errors import (EmptyClaimSetError, EvidenceConflictError,
                               EvidenceHashMismatchError, InvalidClaimError,
                               JudgeNotScriptedError)
from app.domain.financial_facts import normalize_companyfacts
from app.ingestion.chunking import Chunk
from app.ingestion.manifest import load_manifest
from app.tools.calculate_financial_metric import CalculateFinancialMetricOutput

ROOT = Path(__file__).resolve().parents[2]


class MemorySource:
    def __init__(self, kind):
        self.kind = kind
        self.objects = {}

    def load(self, entry):
        obj = self.objects[entry.evidence_id]
        if self.kind == "chunk":
            from hashlib import sha256
            return ResolvedEvidence(entry=entry, observed_hash=sha256(obj.text.encode()).hexdigest(), chunk=obj)
        if self.kind == "fact":
            return ResolvedEvidence(entry=entry, observed_hash=fact_content_hash(obj), fact=obj)
        return ResolvedEvidence(entry=entry, observed_hash=metric_content_hash(obj), metric=obj)

    def remember(self, evidence_id, output):
        if self.kind == "metric":
            self.objects[evidence_id] = output.result
        elif self.kind == "fact":
            self.objects[evidence_id] = output


@pytest.fixture
def context():
    manifest = load_manifest(ROOT / "data/manifests/filings.v1.json")
    sources = {kind: MemorySource(kind) for kind in ("chunk", "fact", "metric")}
    ledger = EvidenceLedger(sources, manifest)
    rows = json.loads((ROOT / "tests/fixtures/citations/real_chunks.json").read_text())
    chunks = {}
    for row in rows:
        evidence = Chunk.model_validate(row).to_evidence(None)
        ledger.register_chunk(evidence)
        sources["chunk"].objects[evidence.evidence_id] = evidence
        chunks[evidence.evidence_id] = evidence
    facts = {}
    for ticker in ("AAPL", "MSFT"):
        pinned = next(e for e in manifest.entries if e.ticker == ticker)
        payload = (ROOT / f"tests/fixtures/companyfacts/{ticker.lower()}_subset.json").read_bytes()
        facts[ticker] = normalize_companyfacts(payload, ticker=ticker, cik=pinned.cik, pinned=pinned)
    msft = next(e for e in manifest.entries if e.ticker == "MSFT")
    aapl = next(e for e in manifest.entries if e.ticker == "AAPL")
    revenue = calculate_metric(Metric.REVENUE, ticker="MSFT", fiscal_year=2026,
                               facts=facts["MSFT"], pinned=msft)
    margin = calculate_metric(Metric.OPERATING_MARGIN, ticker="MSFT", fiscal_year=2026,
                              facts=facts["MSFT"], pinned=msft)
    ratio = calculate_metric(Metric.CURRENT_RATIO, ticker="MSFT", fiscal_year=2026,
                             facts=facts["MSFT"], pinned=msft)
    aapl_revenue = calculate_metric(Metric.REVENUE, ticker="AAPL", fiscal_year=2025,
                                    facts=facts["AAPL"], pinned=aapl)
    msft_id = ledger.register_fact(revenue.inputs[0])
    sources["fact"].objects[msft_id] = revenue.inputs[0]
    aapl_id = ledger.register_fact(aapl_revenue.inputs[0])
    sources["fact"].objects[aapl_id] = aapl_revenue.inputs[0]

    def register_metric(result, pinned, year):
        output = CalculateFinancialMetricOutput(result=result, ticker=pinned.ticker, cik=pinned.cik,
            accession_number=pinned.accession_number, accession_resolved_from_manifest=False,
            fiscal_year=year, source_url=pinned.source_url,
            companyfacts_sha256=pinned.companyfacts_sha256)
        return ledger.register_metric(output)

    return ledger, sources, chunks, msft_id, aapl_id, register_metric(margin, msft, 2026), register_metric(ratio, msft, 2026)


def num(evidence_ids, *, value="331.8", unit="USD", scale="billions", end="2026-06-30", ticker="MSFT"):
    return Claim(claim_id="n", claim_text="numeric", claim_type="numeric", ticker=ticker,
        evidence_ids=evidence_ids, numeric=NumericAssertion(value=Decimal(value), unit=unit,
                                           scale=scale, period_end=date.fromisoformat(end)))


def narrative(evidence_ids, *, ticker="AAPL", text="narrative"):
    return Claim(claim_id="q", claim_text=text, claim_type="narrative", ticker=ticker,
                 evidence_ids=evidence_ids)


def verdict(claim, ledger, script=None):
    return verify_claims([claim], ledger, ScriptedJudge(script or {})).verifications[0]


def test_day5_correct_citation_is_supported(context):
    ledger, _, _, msft_id, _, margin_id, _ = context
    assert verdict(num([msft_id]), ledger).verdict == "supported"


def test_day5_wrong_year_is_period_mismatch(context):
    ledger, _, _, msft_id, _, _, _ = context
    assert verdict(num([msft_id], end="2025-06-30"), ledger).reason == "period_mismatch"


def test_day5_wrong_unit_is_unit_mismatch(context):
    ledger, _, _, _, _, margin_id, _ = context
    assert verdict(num([margin_id], value="46.8", scale="millions"), ledger).reason == "unit_mismatch"


def test_day5_irrelevant_chunk_is_unsupported(context):
    ledger, _, _, _, _, _, _ = context
    irrelevant = "ev_e777a0715850dc81ab53"
    script = {("narrative", (irrelevant,)): JudgeResult(label="not_entailed", score=0, rationale="irrelevant")}
    assert verdict(narrative([irrelevant]), ledger, script).verdict == "unsupported"


def test_day5_partial_support_is_partial(context):
    ledger, _, _, _, _, _, _ = context
    good = "ev_426b275bb4fd862c5317"
    script = {("narrative", (good,)): JudgeResult(label="partial", score=0.5, rationale="partial")}
    assert verdict(narrative([good]), ledger, script).verdict == "partially_supported"


@pytest.mark.parametrize("kind", ["chunk", "fact", "metric"])
def test_hash_tamper(context, kind):
    ledger, sources, chunks, fact_id, _, metric_id, _ = context
    evidence_id = {"chunk": "ev_426b275bb4fd862c5317", "fact": fact_id,
                   "metric": metric_id}[kind]
    original = sources[kind].objects[evidence_id]
    if kind == "chunk":
        sources[kind].objects[evidence_id] = original.model_copy(update={"text": original.text + " changed"})
        claim = narrative([evidence_id])
    elif kind == "fact":
        sources[kind].objects[evidence_id] = original.model_copy(update={"value": original.value + 1})
        claim = num([evidence_id])
    else:
        sources[kind].objects[evidence_id] = original.model_copy(update={"result": original.result + 1})
        claim = num([evidence_id], value="46.8", unit="percent", scale="units")
    with pytest.raises(EvidenceHashMismatchError):
        ledger.resolve(evidence_id)
    class Spy:
        judge_id = "spy"
        def judge(self, *_):
            raise AssertionError("judge reached after hash mismatch")
    checked = verify_claims([claim], ledger, Spy()).verifications[0]
    assert checked.reason == "hash_mismatch"
    assert checked.semantic_score is None


def test_evidence_and_provenance_failures(context):
    ledger, _, _, msft_id, aapl_id, _, _ = context
    assert verdict(num([]), ledger).reason == "no_evidence"
    assert verdict(num(["ev_missing"]), ledger).reason == "evidence_not_found"
    assert verdict(num([aapl_id]), ledger).reason == "ticker_mismatch"
    assert verdict(num([msft_id, aapl_id], ticker="MSFT"), ledger).reason == "ticker_mismatch"
    # Same ticker on a different accession exposes the single-filing invariant.
    other_fact = ledger.sources["fact"].objects[msft_id].model_copy(update={"accession_number": "other"})
    other_id = ledger.register_fact(other_fact)
    assert verdict(num([msft_id, other_id]), ledger).reason == "mixed_accessions"


def test_evidence_type_rules(context):
    ledger, _, _, msft_id, _, _, _ = context
    assert verdict(num(["ev_fffbd37ac73dbe0a73f2"]), ledger).reason == "numeric_claim_without_structured_evidence"
    assert verdict(narrative([msft_id], ticker="MSFT"), ledger).reason == "narrative_claim_without_text_evidence"


def test_tolerance_exponent_and_ratio_forms(context):
    ledger, _, _, msft_id, _, margin_id, ratio_id = context
    # Actual revenue is 331.839 billion: both formats below include it.
    assert verdict(num([msft_id], value="331.8"), ledger).verdict == "supported"
    assert verdict(num([msft_id], value="331.80"), ledger).reason == "value_mismatch"
    for value, unit in [("46.8", "percent"), ("0.468", "ratio"), ("0.47", "ratio")]:
        assert verdict(num([margin_id], value=value, unit=unit, scale="units"), ledger).verdict == "supported"
    assert verdict(num([ratio_id], value="1.23", unit="ratio", scale="units"), ledger).verdict == "supported"
    # Synthetic boundary values are derived from the real revenue fact.
    source = ledger.sources["fact"]
    real_fact = source.objects[msft_id]
    boundary = real_fact.model_copy(update={"value": Decimal("331850000000")})
    boundary_id = ledger.register_fact(boundary)
    source.objects[boundary_id] = boundary
    assert verdict(num([boundary_id], value="331.8"), ledger).verdict == "supported"
    beyond = real_fact.model_copy(update={"value": Decimal("331850000001")})
    beyond_id = ledger.register_fact(beyond)
    source.objects[beyond_id] = beyond
    assert verdict(num([beyond_id], value="331.8"), ledger).reason == "value_mismatch"


def test_metadata_and_registration(context):
    ledger, sources, chunks, msft_id, aapl_id, _, _ = context
    claim = Claim(claim_id="m", claim_text="metadata", claim_type="metadata", ticker="AAPL",
                  evidence_ids=[aapl_id], metadata=MetadataAssertion(field="filing_date", value="2025-10-31 "))
    assert verdict(claim, ledger).verdict == "supported"
    assert verdict(claim.model_copy(update={"metadata": MetadataAssertion(field="filing_date", value="2025-10-30")}), ledger).reason == "metadata_mismatch"
    chunk = chunks["ev_426b275bb4fd862c5317"]
    assert ledger.register_chunk(chunk) == chunk.evidence_id
    with pytest.raises(EvidenceConflictError):
        ledger.register_chunk(chunk.model_copy(update={"content_hash": "different"}))


def test_judge_retrieval_score_report_and_invalid_shapes(context):
    ledger, sources, chunks, msft_id, _, _, _ = context
    evidence_id = "ev_426b275bb4fd862c5317"
    claim = narrative([evidence_id])
    script = {("narrative", (evidence_id,)): JudgeResult(label="entailed", score=1, rationale="scripted")}
    first = verdict(claim, ledger, script)
    sources["chunk"].objects[evidence_id] = chunks[evidence_id].model_copy(update={"retrieval_score": 0.001})
    second = verdict(claim, ledger, script)
    assert (first.verdict, first.reason) == (second.verdict, second.reason)
    report = verify_claims([claim, num([msft_id]), num([])], ledger, ScriptedJudge(script))
    assert (report.supported, report.partially_supported, report.unsupported, report.unsupported_rate) == (2, 0, 1, 1/3)
    assert report.judge_id == "scripted-fake/1"
    with pytest.raises(JudgeNotScriptedError):
        verdict(narrative([evidence_id], text="unscripted"), ledger)
    with pytest.raises(EmptyClaimSetError):
        verify_claims([], ledger, ScriptedJudge({}))
    bad = Claim(claim_id="bad", claim_text="bad", claim_type="numeric", ticker="MSFT", evidence_ids=[msft_id])
    with pytest.raises(InvalidClaimError):
        verify_claims([num([msft_id]), bad], ledger, ScriptedJudge({}))
    with pytest.raises(InvalidClaimError):
        verdict(num([msft_id], unit="ratio", scale="billions"), ledger)
