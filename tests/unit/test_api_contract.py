import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import ApiDeps, create_app
from app.domain.errors import (CompanyNotFoundError, CompanyFactsNotCachedError,
    MissingFactError, ConfigurationError, ResearchError, EvidenceNotFoundError,
    EvidenceHashMismatchError, EvidenceConflictError, InvalidClaimError,
    EmptyClaimSetError, JudgeNotScriptedError, RetryBudgetExhaustedError, RetryParentDraftError)
from app.api.app import _DATA_ERRORS, _INFRA_ERRORS
from app.ingestion.manifest import load_manifest
from app.providers.sec import BASE_SEC
from app.tools.registry import TOOL_REGISTRY
from app.tracing.jsonl import sanitize
from scripts.serve import LoopbackHostError, main

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(ROOT / "data/manifests/filings.v1.json")


def _research_error_subclasses(base=ResearchError):
    found = set()
    for subclass in base.__subclasses__():
        found.add(subclass)
        found.update(_research_error_subclasses(subclass))
    return found


def test_every_research_error_subclass_is_explicitly_classified_once():
    subclasses = _research_error_subclasses()
    data, infra = set(_DATA_ERRORS), set(_INFRA_ERRORS)
    from app.api.app import _CONFLICT_ERRORS
    conflicts = set(_CONFLICT_ERRORS)
    assert subclasses == data | infra | conflicts
    assert not data & infra and not data & conflicts and not infra & conflicts


@pytest.mark.parametrize("error,status,code", [
    (EvidenceNotFoundError("ev_missing"), 422, "evidence_not_found"),
    (EvidenceHashMismatchError("ev_changed"), 422, "hash_mismatch"),
    (EvidenceConflictError("ev_conflict"), 422, "evidence_conflict"),
    (InvalidClaimError("invalid claim"), 422, "invalid_claim"),
    (EmptyClaimSetError(), 422, "empty_claim_set"),
    (RetryBudgetExhaustedError(3, 2), 422, "retry_budget_exhausted"),
    (RetryParentDraftError("draft_x", "ticker mismatch"), 422, "retry_parent_invalid"),
    (JudgeNotScriptedError("claim"), 503, "judge_not_scripted"),
])
def test_citation_research_errors_have_api_mapping_representatives(error, status, code):
    api = create_app(ApiDeps(trace_writer=MemoryTrace()))
    api.add_api_route("/_citation_error", lambda: (_ for _ in ()).throw(error), methods=["GET"])
    response = TestClient(api, raise_server_exceptions=False).get("/_citation_error")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


class Provider:
    def __init__(self, company_body=None, facts=None, facts_by_url=None): self.company_body, self.facts, self.facts_by_url = company_body, facts, facts_by_url or {}
    def get_cached(self, url):
        if url == f"{BASE_SEC}/files/company_tickers.json":
            return (self.company_body, {}) if self.company_body is not None else None
        if "companyfacts" in url:
            value = self.facts_by_url.get(url, self.facts)
            return (value, {}) if value is not None else None
        return (b"{}", {})
    def resolve_company(self, value): return ("MSFT", "0000789019", "Microsoft Corporation")
    def list_filings(self, *args):
        from app.domain.filings import FilingListResult
        return FilingListResult(filings=[], excluded=[], coverage_start=None, older_pages_not_loaded=False)
    def close(self): pass


class Embedder:
    model_id, model_sha256 = "fake", "fake-hash"
    def embed_query(self, question): return [0.0]


class Repo:
    def model_hashes_for_versions(self, *args, **kwargs): return ["fake-hash"]
    def search_chunks(self, **kwargs): return []


class MemoryTrace:
    def __init__(self): self.rows=[]
    def write(self, row): self.rows.append(row)


@pytest.fixture
def client(tmp_path):
    companies = (ROOT / "tests/fixtures/sec/company_tickers.json").read_bytes()
    from tests.unit.offline_fixture_data import companyfacts_fixture, seed_msft_normalized_excerpt, write_fixture_manifest
    fixture_manifest, facts_by_url = companyfacts_fixture()
    manifest_dir = write_fixture_manifest(tmp_path / "manifest", fixture_manifest)
    normalized_dir = tmp_path / "normalized"
    seed_msft_normalized_excerpt(normalized_dir, next(e.accession_number for e in fixture_manifest.entries if e.ticker == "MSFT"))
    provider = Provider(companies, facts_by_url=facts_by_url)
    trace = MemoryTrace()
    deps = ApiDeps(sec_provider_factory=lambda: provider, search_repository=Repo(), embedder_factory=Embedder,
                   trace_writer=trace, manifest_dir=manifest_dir, normalized_dir=normalized_dir)
    return TestClient(create_app(deps), raise_server_exceptions=False), trace


def test_runtime_renderer_switch_uses_config_default_and_backend_environment_override(monkeypatch):
    client = TestClient(create_app(ApiDeps(trace_writer=MemoryTrace())))
    monkeypatch.delenv("FRA_EXPERIMENT_DERIVED_METRIC_RENDER", raising=False)
    assert client.get("/config/runtime").json()["derived_metric_render"] == "v2"
    monkeypatch.setenv("FRA_EXPERIMENT_DERIVED_METRIC_RENDER", "v2")
    assert client.get("/config/runtime").json()["derived_metric_render"] == "v2"


def test_registry_contract_and_schemas(client):
    response = client[0].get("/tools")
    assert response.status_code == 200
    tools = response.json()["tools"]
    assert [t["name"] for t in tools] == ["search_companies", "list_filings", "get_filing_section", "search_filing_chunks", "calculate_financial_metric", "compare_periods", "verify_citations", "export_research_report"]
    assert [t["status"] for t in tools] == ["implemented"] * 8
    verify_spec = next(t for t in tools if t["name"] == "verify_citations")
    export_spec = next(t for t in tools if t["name"] == "export_research_report")
    assert "deterministically first" in verify_spec["description"] and "relevance" in verify_spec["description"].lower()
    assert "ctx.ui.confirm()" in export_spec["description"]
    verify_schema = verify_spec["input_schema"]
    assert verify_schema["properties"]["attempt"]["default"] == 1
    assert "parent_draft_id" in verify_schema["properties"]
    runtime = client[0].get("/config/runtime").json()
    assert runtime["max_verify_attempts"] == 2
    assert runtime["derived_metric_render"] == "v2"
    for tool in tools[:6]:
        assert tool["input_schema"]["type"] == "object"
        assert tool["output_schema"]["type"] == "object"


@pytest.mark.parametrize("name,payload", [
    ("search_companies", {"query":"MSFT"}),
    ("list_filings", {"company":"MSFT", "form":"10-K"}),
    ("get_filing_section", {"company":"MSFT", "section":"financial_statements", "max_chars":50}),
    ("search_filing_chunks", {"company":"MSFT", "question":"revenue"}),
    ("calculate_financial_metric", {"company":"MSFT", "metric":"operating_margin", "fiscal_year":2026}),
    ("compare_periods", {"company":"MSFT", "metrics":["revenue"], "fiscal_year_a":2025, "fiscal_year_b":2026}),
])
def test_each_implemented_tool_happy_path(client, name, payload):
    response = client[0].post(f"/tools/{name}", json=payload)
    assert response.status_code == 200, response.text


def test_search_api_default_repository_uses_configured_url_and_caches_embedder(monkeypatch):
    from app.config import Settings
    from app.retrieval.repository import Repository as RealRepository
    from app.citations.ledger import EvidenceLedger

    database_url = "postgresql://configured.example:5432/financial"
    settings = Settings(_env_file=None, database_url=database_url)
    connections, repositories, embedder_builds = [], [], []

    class Cursor:
        def __init__(self, rows=()): self.rows = rows
        def fetchall(self): return self.rows

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, *args): return Cursor([("fake-hash",)] if "SELECT DISTINCT embedding_model_sha256" in sql else [])

    def connection_factory(url, **kwargs):
        connections.append((url, kwargs))
        return Connection()

    def real_repository_factory(url):
        repo = RealRepository(url, connection_factory=connection_factory)
        repositories.append(repo)
        return repo

    monkeypatch.setattr("app.api.app.Repository", real_repository_factory)
    monkeypatch.setattr("app.retrieval.repository.register_vector", lambda connection: None)

    def build_embedder():
        embedder_builds.append(True)
        return Embedder()

    deps = ApiDeps(settings=settings, sec_provider_factory=Provider,
                   embedder_factory=build_embedder, evidence_ledger=EvidenceLedger({}, MANIFEST))
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    for _ in range(2):
        response = client.post("/tools/search_filing_chunks", json={"company": "AAPL", "question": "revenue"})
        assert response.status_code == 200, response.text

    assert repositories and all(type(repo) is RealRepository for repo in repositories)
    assert not any(hasattr(repo, "close") for repo in repositories)
    assert len(connections) == 4  # model hash lookup and search, once for each request
    assert all(url == database_url for url, _ in connections)
    assert len(embedder_builds) == 1


def test_error_envelope_mapping_pending_validation_unknown_and_trace(client):
    c, trace = client
    bad = c.post("/tools/calculate_financial_metric", json={"company":"MSFT", "metric":"invalid", "fiscal_year":2026})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"
    assert c.post("/tools/verify_citations", json={}).status_code == 422
    direct_export = c.post("/tools/export_research_report", json={"report_draft_id":"draft_x", "format":"markdown", "output_path":"x.md"})
    assert direct_export.status_code == 409 and direct_export.json()["error"]["code"] == "report_export_requires_preview"
    assert c.post("/tools/no_such_tool", json={}).status_code == 404
    assert c.post("/tools/search_companies", json={"query":"xx"}).status_code == 422
    assert trace.rows[-1]["status"] == "invalid_input"


def test_default_testclient_trace_writer_is_isolated_from_repository(tmp_path):
    import hashlib
    trace_dir = ROOT / "artifacts/traces"
    def snapshot():
        if not trace_dir.exists():
            return None
        return {path.relative_to(trace_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in trace_dir.rglob("*") if path.is_file()}
    before = snapshot()
    client = TestClient(create_app(), raise_server_exceptions=False)
    assert client.post("/tools/no_such_tool", json={}).status_code == 404
    assert snapshot() == before
    # The autouse conftest fixture routes the default JsonlTraceWriter here.
    # Exercise the default constructor and assert it points outside the repo.
    from app.api import app as api_module
    assert api_module.JsonlTraceWriter().directory != trace_dir


def test_typed_error_mapping_rows(client, monkeypatch):
    from app.api import app as module
    c, _ = client
    monkeypatch.setattr(module, "search_companies", lambda *a, **k: (_ for _ in ()).throw(CompanyNotFoundError("absent")))
    assert c.post("/tools/search_companies", json={"query":"absent"}).status_code == 422
    monkeypatch.setattr(module, "search_companies", lambda *a, **k: (_ for _ in ()).throw(ConfigurationError("broken config")))
    assert c.post("/tools/search_companies", json={"query":"MSFT"}).status_code == 503
    monkeypatch.setattr(module, "search_companies", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private details")))
    response = c.post("/tools/search_companies", json={"query":"MSFT"})
    assert response.status_code == 500 and "private details" not in response.text


def test_trace_id_summary_redaction_and_no_section_text(client):
    c, trace = client
    response = c.post("/tools/get_filing_section", json={"company":"MSFT", "section":"financial_statements", "max_chars":40}, headers={"X-Trace-Id":"roundtrip-7"})
    assert response.headers["X-Trace-Id"] == "roundtrip-7"
    line = trace.rows[-1]
    assert line["trace_id"] == "roundtrip-7"
    assert "text" not in json.dumps(line["result_summary"])
    assert response.json()["text"] not in json.dumps(line)
    assert line["token_usage"] is None and line["cost"] is None
    assert sanitize({"api_key":"sentinel", "nested":{"access_token":"sentinel2"}}) == {"api_key":"[redacted]", "nested":{"access_token":"[redacted]"}}


def test_api_auto_registers_search_and_metric_ids_for_verification(tmp_path, monkeypatch):
    from app.citations.ledger import EvidenceLedger, CanonicalChunkSource, CompanyFactsFactSource, MetricRecomputeSource
    from app.citations.semantic import ScriptedJudge
    from app.domain.claims import Claim, NumericAssertion
    from app.domain.evidence import Evidence
    from app.tools.search_filing_chunks import SearchFilingChunksOutput
    from app.tools.calculate_financial_metric import calculate_financial_metric
    from tests.unit.offline_fixture_data import (companyfacts_fixture, seed_msft_normalized_excerpt,
                                                 write_fixture_manifest)
    fixture_manifest, pinned_bodies = companyfacts_fixture()
    manifest_dir = write_fixture_manifest(tmp_path / "manifest", fixture_manifest)
    entry = next(e for e in fixture_manifest.entries if e.ticker == "AAPL")
    provider = Provider(facts_by_url=pinned_bodies)
    ledger_provider = Provider(facts_by_url=pinned_bodies)
    normalized_fixture = Path("tests/fixtures/citations")
    fixture_dir = normalized_fixture / "0000320193-25-000079"
    normalized_temp = tmp_path / "normalized"
    normalized_temp.mkdir()
    import shutil
    shutil.copytree(fixture_dir, normalized_temp / entry.accession_number)
    msft_entry = next(e for e in fixture_manifest.entries if e.ticker == "MSFT")
    seed_msft_normalized_excerpt(normalized_temp, msft_entry.accession_number)
    sources = {"chunk": CanonicalChunkSource(normalized_temp),
               "fact": CompanyFactsFactSource(fixture_manifest, provider=ledger_provider),
               "metric": MetricRecomputeSource(fixture_manifest, provider=ledger_provider)}
    ledger = EvidenceLedger(sources, fixture_manifest)
    from app.ingestion.chunking import Chunk
    # Find an actual mdna row and its canonical text; the checked-in fixture uses AAPL.
    rows = [json.loads(line) for line in (fixture_dir / "chunks.jsonl").read_text().splitlines() if json.loads(line).get("evidence_id")]
    row = next(item for item in rows if item.get("section") == "mdna")
    chunk_model = Chunk.model_validate(row)
    canonical = (fixture_dir / "canonical.txt").read_text()
    text = canonical[chunk_model.start_offset:chunk_model.end_offset]
    chunk = chunk_model.to_evidence(retrieval_score=0.9).model_copy(update={"text": text})
    class RepoWithEvidence(Repo):
        def search_chunks(self, **kwargs):
            return [{"evidence_id":chunk.evidence_id,"ticker":chunk.ticker,"cik":chunk.cik,
                "accession_number":chunk.accession_number,"filing_form":chunk.filing_form,"filing_date":chunk.filing_date,
                "section":chunk.section,"chunk_id":chunk.chunk_id,"text":chunk.text,"source_url":str(chunk.source_url),
                "start_offset":0,"end_offset":len(text),"page_number":None,"content_hash":chunk.content_hash,"distance":0.1}]
    judge = ScriptedJudge({(text, (chunk.evidence_id,)): __import__("app.citations.semantic", fromlist=["JudgeResult"]).JudgeResult(label="entailed", score=1, rationale="supported")})
    deps = ApiDeps(sec_provider_factory=lambda: provider, search_repository=RepoWithEvidence(), embedder_factory=Embedder,
        trace_writer=MemoryTrace(), evidence_ledger=ledger, judge_factory=lambda: judge, reports_dir=tmp_path,
        manifest_dir=manifest_dir, normalized_dir=normalized_temp)
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    found = client.post("/tools/search_filing_chunks", json={"company":"AAPL", "question":"revenue"})
    assert found.status_code == 200
    metric_resp = client.post("/tools/calculate_financial_metric", json={"company":"MSFT", "metric":"revenue", "fiscal_year":2026})
    assert metric_resp.status_code == 200, metric_resp.text
    metric = metric_resp.json()
    assert metric["metric_evidence_id"].startswith("met_")
    assert metric["metric_evidence_id"] in ledger
    assert all(evidence_id in ledger for evidence_id in metric["result"]["evidence_ids"])
    claim = {"claim_id":"c1","claim_text":text,"claim_type":"narrative","ticker":"AAPL","evidence_ids":[chunk.evidence_id]}
    verified = client.post("/tools/verify_citations", json={"ticker":"AAPL", "claims":[claim]})
    assert verified.status_code == 200, verified.text
    assert verified.json()["report"]["verifications"][0]["verdict"] == "supported"
    stale = dict(claim, evidence_ids=["ev_from_previous_server"])
    previous = client.post("/tools/verify_citations", json={"ticker":"AAPL", "claims":[dict(stale, ticker="AAPL")]})
    assert previous.status_code == 422
    assert previous.json()["error"]["code"] == "evidence_not_found"


def test_compare_periods_exposes_and_registers_successful_side_metric_ids(client):
    c, _ = client
    response = c.post("/tools/compare_periods", json={"company":"MSFT", "metrics":["revenue"],
        "fiscal_year_a":2025, "fiscal_year_b":2026})
    assert response.status_code == 200, response.text
    row = response.json()["rows"][0]
    assert row["metric_evidence_id_a"].startswith("met_")
    assert row["metric_evidence_id_b"].startswith("met_")
    assert row["value_a"] is not None and row["value_b"] is not None
    ledger = c.app.state.api_deps.evidence_ledger
    assert row["metric_evidence_id_a"] in ledger and row["metric_evidence_id_b"] in ledger


def test_company_cache_miss_is_503(client):
    c, _ = client
    c.app.state.api_deps.sec_provider_factory = lambda: Provider(None, None)
    response = c.post("/tools/search_companies", json={"query":"MSFT"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "company_tickers_not_cached"


def test_serve_refuses_non_loopback_host():
    with pytest.raises(LoopbackHostError): main(["--host", "0.0.0.0"])
