#!/usr/bin/env python3
"""Live smoke for the narrative demo claims c07-c10 (paid judge calls). Do not run in offline work."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.verify_demo import main as demo_main
from app.citations.openrouter_judge import OpenRouterJudge

NOTE="Live judge smoke on 4 hand-written demo claims; not an evaluation metric."

def main():
    # Build the demo's real ledger and hand-written claims using the same fixtures and source loaders.
    # Keep construction shared with verify_demo to avoid drifting evidence definitions.
    from app.citations.ledger import CanonicalChunkSource, CompanyFactsFactSource, EvidenceLedger, MetricRecomputeSource
    from app.citations.verifier import verify_claims
    from app.ingestion.manifest import latest_manifest_path, load_manifest
    from app.config import Settings
    from app.providers.sec import SecProvider
    from app.compute.metrics import Metric
    from app.tools.calculate_financial_metric import CalculateFinancialMetricInput, calculate_financial_metric
    from app.domain.claims import Claim
    from app.ingestion.chunking import Chunk
    manifest=load_manifest(latest_manifest_path(ROOT/"data/manifests")); by={e.ticker:e for e in manifest.entries}
    ledger=EvidenceLedger({"chunk":CanonicalChunkSource(ROOT/"data/normalized"),"fact":CompanyFactsFactSource(manifest,ROOT/"data"),"metric":MetricRecomputeSource(manifest,ROOT/"data")},manifest)
    ids=["ev_426b275bb4fd862c5317","ev_e777a0715850dc81ab53","ev_fffbd37ac73dbe0a73f2"]
    for eid in ids:
        accession=(by["MSFT"] if eid==ids[2] else by["AAPL"]).accession_number
        with (ROOT/"data/normalized"/accession/"chunks.jsonl").open(encoding="utf-8") as f:
            row=next(json.loads(line) for line in f if json.loads(line)["evidence_id"]==eid)
        ledger.register_chunk(Chunk.model_validate(row).to_evidence(None))
    claims=[Claim(claim_id="c07",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to higher net sales of Pro models.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[0]]),
            Claim(claim_id="c08",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to higher net sales of Pro models.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[1]]),
            Claim(claim_id="c09",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to Pro models and strong growth in Greater China.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[0]]),
            Claim(claim_id="c10",claim_text="Microsoft's Intelligent Cloud revenue increased, driven by Azure.",claim_type="narrative",ticker="MSFT",evidence_ids=[ids[2]])]
    judge=OpenRouterJudge(settings=Settings())
    report=verify_claims(claims,ledger,judge)
    by_claim={v.claim_id:v for v in report.verifications}
    if by_claim["c07"].reason is None or by_claim["c07"].verdict != "supported":
        raise RuntimeError("c07 judge verdict did not satisfy the expected entailment")
    if by_claim["c08"].verdict != "unsupported":
        raise RuntimeError("c08 judge verdict did not satisfy the expected non-entailment")
    if any(r.observed_model != judge.config.model_id or r.provider != "OpenAI" for r in judge.calls):
        raise RuntimeError("Observed judge model or provider differs from the pinned route")
    records=[r.__dict__ for r in judge.calls]
    artifact={"note":NOTE,"judge_id":judge.judge_id,"verdicts":[v.model_dump(mode="json") for v in report.verifications],"calls":records,
              "total_cost_usd":sum(r.cost_usd or 0 for r in judge.calls),"total_tokens":sum((r.prompt_tokens or 0)+(r.completion_tokens or 0) for r in judge.calls)}
    target=ROOT/"artifacts/verifier_demo/judge_smoke.json"; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(json.dumps(artifact,indent=2)+"\n",encoding="utf-8")
    return 0
if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(f"Judge smoke failed ({getattr(exc,'code','judge_error')}).",file=sys.stderr); raise SystemExit(1)
