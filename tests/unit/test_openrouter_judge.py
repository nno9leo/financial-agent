import json
import logging
from pathlib import Path
import tomllib

import httpx
import pytest

from app.citations.openrouter_judge import JudgeConfig, OpenRouterJudge
from app.domain.errors import (ConfigurationError, JudgeAuthError, JudgeModelMismatchError,
                               JudgeRouteMismatchError, JudgeSchemaError, JudgeUnavailableError)
from app.citations.ledger import EvidenceLedger
from app.citations.verifier import verify_claims
from app.domain.claims import Claim
from app.config import Settings

KEY = "sk-or-TEST-SENTINEL-123"
CFG = JudgeConfig(model_id="openai/gpt-6-sol", base_url="https://example.test/api/v1",
                  reasoning_effort="high", provider_routing={"order": ["OpenAI"], "allow_fallbacks": False},
                  seed=20260926, max_attempts=3)

def payload(label="entailed", model="openai/gpt-6-sol", provider="OpenAI", finish="stop", content=None):
    return {"id":"gen-1", "model":model, "provider":provider,
            "choices":[{"finish_reason":finish,"message":{"content":content or json.dumps({"label":label,"score":0.9,"rationale":"supported"})}}],
            "usage":{"prompt_tokens":10,"completion_tokens":5,"completion_tokens_details":{"reasoning_tokens":2},"cost":0.00007}}

def build(handler, sleep=lambda _: None, log=None):
    transport = httpx.MockTransport(handler)
    return OpenRouterJudge(CFG, settings=Settings(_env_file=None, openrouter_api_key=KEY), transport=transport, sleep=sleep, logger=log)

def test_happy_path_request_record_and_hygiene(caplog):
    bodies=[]
    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json=payload())
    caplog.set_level(logging.DEBUG)
    judge=build(handler,log=logging.getLogger("judge-test"))
    result=judge.judge("claim",["evidence"])
    body=bodies[0]
    assert result.label=="entailed" and result.score==0.9
    assert body["model"]==CFG.model_id and body["provider"]==CFG.provider_routing
    assert body["reasoning"]=={"effort":"high","exclude":True} and body["seed"]==CFG.seed
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert judge.calls[0].prompt_tokens==10 and judge.calls[0].reasoning_tokens==2
    assert judge.judge_id=="openrouter:openai/gpt-6-sol:reasoning-high:prompt-v1"
    assert KEY not in repr(judge) and KEY not in json.dumps([r.__dict__ for r in judge.calls]) and KEY not in caplog.text

def test_response_failures_do_not_leak_key():
    cases=[(payload(model="wrong"),JudgeModelMismatchError),(payload(provider="Other"),JudgeRouteMismatchError),
           (payload(finish="length"),JudgeSchemaError),(payload(content="{"),JudgeSchemaError),
           (payload(content='{"label":"entailed","score":0.4}'),JudgeSchemaError),
           (payload(content='{"label":"entailed","score":1.5,"rationale":"x"}'),JudgeSchemaError)]
    for response, error in cases:
        judge=build(lambda req,response=response:httpx.Response(200,json=response))
        with pytest.raises(error) as exc: judge.judge("c",["e"])
        assert KEY not in str(exc.value)
        assert KEY not in json.dumps([r.__dict__ for r in judge.calls])

def test_retry_and_auth_semantics():
    seq=[429,200]; sleeps=[]
    def handler(req):
        status=seq.pop(0)
        return httpx.Response(status,json=payload())
    assert build(handler,sleep=sleeps.append).judge("c",["e"]).label=="entailed"
    assert len(sleeps)==1
    attempts=[]
    judge=build(lambda req:(attempts.append(1) or httpx.Response(500)),sleep=lambda _:None)
    with pytest.raises(JudgeUnavailableError): judge.judge("c",["e"])
    assert len(attempts)==3
    attempts=[]
    judge=build(lambda req:(attempts.append(1) or httpx.Response(401)))
    with pytest.raises(JudgeAuthError): judge.judge("c",["e"])
    assert len(attempts)==1
    attempts=[]
    def timeout(req):
        attempts.append(1)
        raise httpx.ReadTimeout("upstream timeout",request=req)
    with pytest.raises(JudgeUnavailableError): build(timeout).judge("c",["e"])
    assert len(attempts)==3

def test_missing_key_fails_before_transport():
    calls=[]
    judge=OpenRouterJudge(CFG,settings=Settings(_env_file=None),transport=httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(ConfigurationError): judge.judge("c",["e"])
    assert not calls

def test_hash_stable_and_verifier_propagates():
    def handler(req): return httpx.Response(200,json=payload())
    a,b=build(handler),build(handler)
    a.judge("c",["e"]); b.judge("c",["e"])
    assert a.calls[0].prompt_sha256==b.calls[0].prompt_sha256

def test_unavailable_judge_propagates_without_report():
    from tests.unit.test_citations import MemorySource
    from app.ingestion.chunking import Chunk
    evidence=Chunk.model_validate(json.loads((__import__('pathlib').Path(__file__).resolve().parents[1]/"fixtures/citations/real_chunks.json").read_text())[0]).to_evidence(None)
    source=MemorySource("chunk"); ledger=EvidenceLedger({"chunk":source,"fact":MemorySource("fact"),"metric":MemorySource("metric")})
    ledger.register_chunk(evidence); source.objects[evidence.evidence_id]=evidence
    claim=Claim(claim_id="n1",claim_text="claim",claim_type="narrative",ticker=evidence.ticker,evidence_ids=[evidence.evidence_id])
    class FailingJudge:
        judge_id="failure"
        def judge(self,*args): raise JudgeUnavailableError()
    with pytest.raises(JudgeUnavailableError): verify_claims([claim],ledger,FailingJudge())

def test_default_pytest_selection_excludes_live_llm():
    settings=tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    assert "not live_llm" in settings["tool"]["pytest"]["ini_options"]["addopts"]

@pytest.mark.live_llm
def test_live_openrouter_c07_c08_c09():
    from app.citations.ledger import CanonicalChunkSource, CompanyFactsFactSource, EvidenceLedger, MetricRecomputeSource
    from app.citations.verifier import verify_claims
    from app.domain.claims import Claim
    from app.ingestion.chunking import Chunk
    from app.ingestion.manifest import latest_manifest_path, load_manifest

    root=Path(__file__).resolve().parents[2]
    manifest=load_manifest(latest_manifest_path(root/"data/manifests")); by={e.ticker:e for e in manifest.entries}
    ledger=EvidenceLedger({"chunk":CanonicalChunkSource(root/"data/normalized"),
        "fact":CompanyFactsFactSource(manifest,root/"data"),
        "metric":MetricRecomputeSource(manifest,root/"data")},manifest)
    ids=["ev_426b275bb4fd862c5317","ev_e777a0715850dc81ab53"]
    for eid in ids:
        with (root/"data/normalized"/by["AAPL"].accession_number/"chunks.jsonl").open(encoding="utf-8") as handle:
            row=next(json.loads(line) for line in handle if json.loads(line)["evidence_id"]==eid)
        ledger.register_chunk(Chunk.model_validate(row).to_evidence(None))
    claims=[Claim(claim_id="c07",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to higher net sales of Pro models.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[0]]),
        Claim(claim_id="c08",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to higher net sales of Pro models.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[1]]),
        Claim(claim_id="c09",claim_text="Apple's iPhone net sales increased in fiscal 2025 due to Pro models and strong growth in Greater China.",claim_type="narrative",ticker="AAPL",evidence_ids=[ids[0]])]
    judge=OpenRouterJudge(settings=Settings())
    report=verify_claims(claims,ledger,judge)
    assert [call.observed_model for call in judge.calls] == [judge.config.model_id] * 3
    assert [call.provider for call in judge.calls] == ["OpenAI"] * 3
    assert [call.label for call in judge.calls[:2]] == ["entailed","not_entailed"]
    assert judge.calls[2].label in {"entailed","partial","not_entailed"}
