#!/usr/bin/env python3
"""Offline verifier demonstration against pinned cache and canonical filing text."""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citations.ledger import (CanonicalChunkSource, CompanyFactsFactSource,
                                  EvidenceLedger, MetricRecomputeSource)
from app.citations.semantic import JudgeResult, ScriptedJudge
from app.citations.verifier import verify_claims
from app.compute.metrics import Metric
from app.config import Settings
from app.domain.claims import Claim, MetadataAssertion, NumericAssertion
from app.ingestion.chunking import Chunk
from app.ingestion.manifest import latest_manifest_path, load_manifest
from app.providers.sec import SecProvider
from app.tools.calculate_financial_metric import CalculateFinancialMetricInput, calculate_financial_metric

NOTE = "Narrative verdicts come from a scripted fake judge; this report demonstrates the deterministic layer and is not a measurement."


def main() -> int:
    manifest = load_manifest(latest_manifest_path(ROOT / "data/manifests"))
    by_ticker = {entry.ticker: entry for entry in manifest.entries}
    ledger = EvidenceLedger({"chunk": CanonicalChunkSource(ROOT / "data/normalized"),
                             "fact": CompanyFactsFactSource(manifest, ROOT / "data"),
                             "metric": MetricRecomputeSource(manifest, ROOT / "data")}, manifest)
    chunk_ids = ["ev_426b275bb4fd862c5317", "ev_e777a0715850dc81ab53",
                 "ev_fffbd37ac73dbe0a73f2"]
    for evidence_id in chunk_ids:
        accession = (by_ticker["MSFT"] if evidence_id == chunk_ids[2] else by_ticker["AAPL"]).accession_number
        with (ROOT / "data/normalized" / accession / "chunks.jsonl").open(encoding="utf-8") as handle:
            row = next(json.loads(line) for line in handle if json.loads(line)["evidence_id"] == evidence_id)
        ledger.register_chunk(Chunk.model_validate(row).to_evidence(None))

    with SecProvider(Settings(_env_file=None, data_dir=ROOT / "data")) as provider:
        def calculate(ticker, fiscal_year, metric):
            return calculate_financial_metric(CalculateFinancialMetricInput(
                metric=metric, company=ticker, fiscal_year=fiscal_year,
                accession_number=by_ticker[ticker].accession_number), provider=provider, manifest=manifest)

        msft_revenue = calculate("MSFT", 2026, Metric.REVENUE)
        aapl_revenue = calculate("AAPL", 2025, Metric.REVENUE)
        operating_margin = calculate("MSFT", 2026, Metric.OPERATING_MARGIN)
        free_cash_flow = calculate("AAPL", 2025, Metric.FREE_CASH_FLOW)
    msft_fact = ledger.register_fact(msft_revenue.result.inputs[0])
    aapl_fact = ledger.register_fact(aapl_revenue.result.inputs[0])
    margin_id = ledger.register_metric(operating_margin)
    fcf_id = ledger.register_metric(free_cash_flow)

    def numeric(claim_id, text, ticker, evidence_ids, value, scale, unit, period_end):
        return Claim(claim_id=claim_id, claim_text=text, claim_type="numeric", ticker=ticker,
                     evidence_ids=evidence_ids,
                     numeric=NumericAssertion(value=Decimal(value), scale=scale, unit=unit,
                                              period_end=date.fromisoformat(period_end)))

    def metadata(claim_id, text, field, value):
        return Claim(claim_id=claim_id, claim_text=text, claim_type="metadata", ticker="AAPL",
                     evidence_ids=[aapl_fact], metadata=MetadataAssertion(field=field, value=value))

    def narrative(claim_id, text, ticker, evidence_id):
        return Claim(claim_id=claim_id, claim_text=text, claim_type="narrative", ticker=ticker,
                     evidence_ids=[evidence_id])

    rev_text = "Microsoft's revenue for fiscal 2026 was $331.8 billion."
    iphone_text = "Apple's iPhone net sales increased in fiscal 2025 due to higher net sales of Pro models."
    claims = [
        numeric("c01", rev_text, "MSFT", [msft_fact], "331.8", "billions", "USD", "2026-06-30"),
        numeric("c02", "Microsoft's revenue for fiscal 2025 was $331.8 billion.", "MSFT",
                [msft_fact], "331.8", "billions", "USD", "2025-06-30"),
        numeric("c03", "Microsoft's fiscal 2026 operating margin was $46.8 million.", "MSFT",
                [margin_id], "46.8", "millions", "USD", "2026-06-30"),
        numeric("c04", "Microsoft's fiscal 2026 operating margin was 46.8%.", "MSFT",
                [margin_id], "46.8", "units", "percent", "2026-06-30"),
        metadata("c05", "Apple's fiscal 2025 10-K has accession number 0000320193-25-000079.",
                 "accession_number", "0000320193-25-000079"),
        metadata("c06", "Apple's fiscal 2025 10-K was filed on 2025-10-30.",
                 "filing_date", "2025-10-30"),
        narrative("c07", iphone_text, "AAPL", chunk_ids[0]),
        narrative("c08", iphone_text, "AAPL", chunk_ids[1]),
        narrative("c09", "Apple's iPhone net sales increased in fiscal 2025 due to Pro models and strong growth in Greater China.",
                  "AAPL", chunk_ids[0]),
        narrative("c10", "Microsoft's Intelligent Cloud revenue increased, driven by Azure.", "MSFT", chunk_ids[2]),
        numeric("c11", "Apple's fiscal 2025 free cash flow was $98.8 billion.", "AAPL",
                [fcf_id], "98.8", "billions", "USD", "2025-09-27"),
        numeric("c12", rev_text, "MSFT", [chunk_ids[2]], "331.8", "billions", "USD", "2026-06-30"),
        numeric("c13", rev_text, "MSFT", ["ev_doesnotexist000000"], "331.8", "billions", "USD", "2026-06-30"),
    ]
    def scripted(label, score):
        return JudgeResult(label=label, score=score, rationale=f"scripted_{label}")
    judge = ScriptedJudge({
        (iphone_text, (chunk_ids[0],)): scripted("entailed", 1),
        (iphone_text, (chunk_ids[1],)): scripted("not_entailed", 0),
        (claims[8].claim_text, (chunk_ids[0],)): scripted("partial", 0.5),
        (claims[9].claim_text, (chunk_ids[2],)): scripted("entailed", 1),
    })
    report = verify_claims(claims, ledger, judge)
    expected = [
        ("supported", "deterministic_match"), ("unsupported", "period_mismatch"),
        ("unsupported", "unit_mismatch"), ("supported", "deterministic_match"),
        ("supported", "deterministic_match"), ("unsupported", "metadata_mismatch"),
        ("supported", "scripted_entailed"), ("unsupported", "scripted_not_entailed"),
        ("partially_supported", "scripted_partial"), ("supported", "scripted_entailed"),
        ("supported", "deterministic_match"),
        ("unsupported", "numeric_claim_without_structured_evidence"),
        ("unsupported", "evidence_not_found"),
    ]
    assert [(v.verdict, v.reason) for v in report.verifications] == expected
    artifact = {"note": NOTE, **report.model_dump(mode="json")}
    target = ROOT / "artifacts/verifier_demo/verification_report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"{target}: {report.supported} supported, {report.partially_supported} partial, {report.unsupported} unsupported")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
