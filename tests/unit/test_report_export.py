import json
import hashlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.domain.errors import (ReportAlreadyExistsError, ReportPathError,
                               UnsupportedClaimsRequireAcknowledgementError,
                               RetryBudgetExhaustedError, RetryParentDraftError)
from app.reports.render import render_report
from app.tools.export_research_report import preview_export, write_export
from app.reports.render import render_report
from app.tools.verify_citations import VerifyCitationsInput, verify_citations
from app.citations.ledger import EvidenceLedger
from app.citations.semantic import JudgeResult, ScriptedJudge
from app.domain.claims import Claim, NumericAssertion
from app.domain.evidence import Evidence


def test_export_api_preview_write_and_direct_route(tmp_path):
    from app.api.app import ApiDeps, create_app
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir()
    (draft_dir / "draft_api.json").write_text(json.dumps(sample_draft()), encoding="utf-8")
    app = create_app(ApiDeps(reports_dir=tmp_path, evidence_ledger=EvidenceLedger({}), judge_factory=lambda: ScriptedJudge({})))
    client = TestClient(app, raise_server_exceptions=False)
    args = {"report_draft_id":"draft_api", "format":"markdown", "output_path":"api.md"}
    assert client.post("/tools/export_research_report", json=args).status_code == 409
    preview = client.post("/tools/export_research_report/preview", json=args)
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert not (tmp_path / "api.md").exists()
    assert result["supported_claim_count"] == 1 and result["partially_supported_claim_count"] == 1
    assert result["unsupported_claim_count"] == 1 and result["requires_unsupported_acknowledgement"]
    denied = client.post("/tools/export_research_report/write", json={**args, "preview_content_sha256":result["content_sha256"]})
    assert denied.status_code == 422 and not (tmp_path / "api.md").exists()
    allowed_args = {**args, "output_path":"allowed.md", "acknowledge_unsupported":True}
    allowed_preview = client.post("/tools/export_research_report/preview", json=allowed_args).json()
    allowed = client.post("/tools/export_research_report/write", json={**allowed_args,
        "preview_content_sha256":allowed_preview["content_sha256"]})
    assert allowed.status_code == 200, allowed.text
    assert hashlib.sha256((tmp_path / "allowed.md").read_bytes()).hexdigest() == allowed_preview["content_sha256"]
    second = client.post("/tools/export_research_report/write", json={**allowed_args,
        "preview_content_sha256":allowed_preview["content_sha256"]})
    assert second.status_code == 422 and second.json()["error"]["code"] == "report_already_exists"


def sample_draft():
    return {"ticker": "MSFT", "judge_id": "scripted-fake/1", "evidence": [{
        "evidence_id": "ev_1", "kind": "chunk", "ticker": "MSFT", "cik": "0000789019",
        "accession_number": "0000000000-00-000001", "section": "mdna",
        "source_url": "https://example.test/source", "text": "Revenue grew."}],
        "verifications": [
            {"claim_id": "s", "claim_text": "Revenue grew.", "evidence_ids": ["ev_1"], "verdict": "supported", "reason": "entailed"},
            {"claim_id": "p", "claim_text": "Revenue grew rapidly.", "evidence_ids": ["ev_1"], "verdict": "partially_supported", "reason": "rapidly is not stated"},
            {"claim_id": "u", "claim_text": "<script>alert(1)</script>", "evidence_ids": ["ev_1"], "verdict": "unsupported", "reason": "not stated"}]}


def test_render_supported_partial_removed_html_escape_and_deterministic():
    draft = sample_draft()
    md, md_hash = render_report(draft, "markdown")
    html, html_hash = render_report(draft, "html")
    assert "Revenue grew." in md and "Revenue grew rapidly." in md
    assert "<script>" not in md
    assert "<script>" not in html and "&lt;script&gt;" not in html
    assert "not stated" in html  # partial reason is visible; unsupported reason/text is omitted
    assert "<script>alert(1)</script>" not in html
    acknowledged, _ = render_report(draft, "markdown", True)
    assert "Removed — not supported by evidence" in acknowledged and "<script>" in acknowledged
    assert render_report(draft, "markdown")[1] == md_hash
    assert render_report(draft, "html")[1] == html_hash
    assert "generated_at" not in md and "2026-" not in md


@pytest.fixture
def draft_directory(tmp_path):
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "draft_x.json").write_text(json.dumps(sample_draft()), encoding="utf-8")
    return tmp_path


def test_preview_is_read_only_and_write_hash_matches(draft_directory):
    args = {"report_draft_id": "draft_x", "format": "markdown", "output_path": "report.md"}
    preview = preview_export(args, reports_dir=draft_directory)
    assert preview.unsupported_claim_count == 1
    assert list(draft_directory.iterdir()) == [draft_directory / "drafts"]
    with pytest.raises(UnsupportedClaimsRequireAcknowledgementError):
        write_export(args, reports_dir=draft_directory, expected_content_sha256=preview.content_sha256)
    args["acknowledge_unsupported"] = True
    preview = preview_export(args, reports_dir=draft_directory)
    written = write_export(args, reports_dir=draft_directory, expected_content_sha256=preview.content_sha256)
    assert written.content_sha256 == preview.content_sha256
    expected, expected_hash = render_report(sample_draft(), "markdown", True)
    assert (draft_directory / "report.md").read_bytes() == expected.encode()
    assert expected_hash == preview.content_sha256
    with pytest.raises(ReportAlreadyExistsError):
        preview_export(args, reports_dir=draft_directory)


@pytest.mark.parametrize("name,fmt", [("../x.md", "markdown"), ("/abs.md", "markdown"),
    ("a/b.md", "markdown"), ("bad.html", "markdown"), ("..x.md", "markdown")])
def test_export_rejects_bad_paths(draft_directory, name, fmt):
    with pytest.raises(ReportPathError):
        preview_export({"report_draft_id": "draft_x", "format": fmt, "output_path": name}, reports_dir=draft_directory)


def test_export_rejects_existing_and_symlink_targets(draft_directory):
    (draft_directory / "exists.md").write_text("keep", encoding="utf-8")
    with pytest.raises(ReportAlreadyExistsError):
        preview_export({"report_draft_id": "draft_x", "format": "markdown", "output_path": "exists.md"}, reports_dir=draft_directory)
    try:
        (draft_directory / "link.md").symlink_to(draft_directory / "target.md")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ReportPathError):
        preview_export({"report_draft_id": "draft_x", "format": "markdown", "output_path": "link.md"}, reports_dir=draft_directory)


class MemorySource:
    kind = "chunk"
    def __init__(self, evidence): self.evidence = evidence
    def load(self, entry):
        from app.citations.ledger import ResolvedEvidence
        return ResolvedEvidence(entry=entry, observed_hash=self.evidence.content_hash, chunk=self.evidence)


class FactSource:
    kind = "fact"
    def __init__(self, fact): self.fact = fact; self.registered = {}
    def remember(self, evidence_id, fact): self.registered[evidence_id] = fact
    def load(self, entry):
        from app.citations.ledger import ResolvedEvidence, fact_content_hash
        return ResolvedEvidence(entry=entry, observed_hash=fact_content_hash(self.fact), fact=self.fact)


def test_verify_claims_draft_is_deterministic(tmp_path):
    from app.citations.ledger import canonical_hash
    text = "Revenue increased."
    import hashlib
    evidence = Evidence(evidence_id="ev_1", ticker="MSFT", cik="0000789019", accession_number="acc",
        filing_form="10-K", filing_date="2026-01-01", section="mdna", chunk_id="c1", text=text,
        source_url="https://example.test", start_offset=0, end_offset=len(text), page_number=None,
        content_hash=hashlib.sha256(text.encode()).hexdigest(), retrieval_score=0.5)
    ledger = EvidenceLedger({"chunk": MemorySource(evidence)})
    ledger.register_chunk(evidence)
    claim = Claim(claim_id="n1", claim_text=text, claim_type="narrative", ticker="MSFT", evidence_ids=["ev_1"])
    judge = ScriptedJudge({(text, ("ev_1",)): JudgeResult(label="entailed", score=1, rationale="exact")})
    args = VerifyCitationsInput(claims=[claim], ticker="MSFT")
    one = verify_citations(args, ledger=ledger, judge=judge, reports_dir=tmp_path)
    data_one = (tmp_path / "drafts" / f"{one.report_draft_id}.json").read_bytes()
    two = verify_citations(args, ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert one.report_draft_id == two.report_draft_id
    assert data_one == (tmp_path / "drafts" / f"{two.report_draft_id}.json").read_bytes()


def _retry_fixture_ledger():
    from app.domain.financial_facts import FinancialFact
    text = "Operating income increased."
    fact = FinancialFact(ticker="MSFT", cik="0000789019", taxonomy="us-gaap", tag="Revenue",
        label="Revenue", value=Decimal("100"), unit="USD", period_start=date(2024,1,1),
        period_end=date(2024,12,31), fiscal_year=2025, fiscal_period="FY", form="10-K",
        filed_at=date(2025,2,1), accession_number="acc", frame=None, source_url="https://example.test/acc")
    chunk = Evidence(evidence_id="ev_retry", ticker="MSFT", cik="0000789019", accession_number="acc",
        filing_form="10-K", filing_date=date(2025,2,1), section="mdna", chunk_id="chunk-retry", text=text,
        source_url="https://example.test/acc", start_offset=0, end_offset=len(text), page_number=None,
        content_hash=hashlib.sha256(text.encode()).hexdigest(), retrieval_score=None)
    ledger = EvidenceLedger({"fact": FactSource(fact), "chunk": MemorySource(chunk)})
    fact_id = ledger.register_fact(fact)
    ledger.register_chunk(chunk)
    numeric = Claim(claim_id="supported", claim_text="Revenue was $100.", claim_type="numeric", ticker="MSFT",
        evidence_ids=[fact_id], numeric=NumericAssertion(value=100, unit="USD", period_start=date(2024,1,1), period_end=date(2024,12,31)))
    wrong_year = Claim(claim_id="unsupported", claim_text="Revenue was $100 in 2023.", claim_type="numeric", ticker="MSFT",
        evidence_ids=[fact_id], numeric=NumericAssertion(value=100, unit="USD", period_end=date(2023,12,31)))
    partial = Claim(claim_id="partial", claim_text="Operating income increased substantially.", claim_type="narrative", ticker="MSFT", evidence_ids=[chunk.evidence_id])
    judge = ScriptedJudge({(partial.claim_text, (chunk.evidence_id,)): JudgeResult(label="partial", score=0.5, rationale="The increase is supported, but its magnitude is not stated.")})
    return ledger, judge, fact_id, numeric, wrong_year, partial


def test_retry_attempt_chain_and_exhaustion_are_authoritative(tmp_path):
    ledger, judge, _fact_id, _numeric, unsupported, _partial = _retry_fixture_ledger()
    first = verify_citations({"ticker":"MSFT", "claims":[unsupported]}, ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert first.retry_allowed and not first.is_final and first.retry_guidance
    second = verify_citations({"ticker":"MSFT", "claims":[unsupported], "attempt":2,
        "parent_draft_id":first.report_draft_id}, ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert second.report.unsupported == 1 and second.is_final and not second.retry_allowed
    unsupported_heading = "## Not supported by the filing evidence"
    statements = second.final_answer_markdown.split(unsupported_heading, 1)[0]
    assert unsupported.claim_text not in statements
    assert "1 claim was removed because the evidence did not support them." in statements
    assert unsupported.claim_text in second.final_answer_markdown.split(unsupported_heading, 1)[1]
    drafts = sorted((tmp_path / "drafts").glob("*.json"))
    assert len(drafts) == 2
    saved = json.loads((tmp_path / "drafts" / f"{second.report_draft_id}.json").read_text())
    assert saved["attempt"] == 2 and saved["parent_draft_id"] == first.report_draft_id
    with pytest.raises(RetryBudgetExhaustedError):
        verify_citations({"ticker":"MSFT", "claims":[unsupported], "attempt":3,
            "parent_draft_id":second.report_draft_id}, ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert len(list((tmp_path / "drafts").glob("*.json"))) == 2


@pytest.mark.parametrize("parent,reason", [(None, "required"), ("draft_0000000000000000", "exist"),
    ("draft_1111111111111111", "ticker")])
def test_retry_parent_must_exist_and_match_ticker(tmp_path, parent, reason):
    ledger, judge, _fact_id, _numeric, unsupported, _partial = _retry_fixture_ledger()
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    if parent == "draft_1111111111111111":
        (drafts / f"{parent}.json").write_text(json.dumps({"ticker":"AAPL"}), encoding="utf-8")
    request = {"ticker":"MSFT", "claims":[unsupported], "attempt":2}
    if parent is not None:
        request["parent_draft_id"] = parent
    with pytest.raises(RetryParentDraftError, match=reason):
        verify_citations(request, ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert len(list(drafts.glob("*.json"))) == int(parent == "draft_1111111111111111")


def test_final_answer_is_verified_only_cited_deterministic_and_does_not_state_unsupported(tmp_path):
    ledger, judge, fact_id, numeric, unsupported, partial = _retry_fixture_ledger()
    result = verify_citations({"ticker":"MSFT", "claims":[numeric, partial, unsupported]},
        ledger=ledger, judge=judge, reports_dir=tmp_path)
    answer = result.final_answer_markdown
    assert "Revenue was $100." in answer and f"[{fact_id}]" in answer
    assert "Value: 100 USD" in answer and "2024-01-01 to 2024-12-31" in answer
    assert "Partially supported: Operating income increased substantially." in answer
    heading = "## Not supported by the filing evidence"
    assert heading in answer and "1 claim was removed" in answer
    assert "Revenue was $100 in 2023." not in answer.split(heading)[0]
    assert "Revenue was $100 in 2023." in answer.split(heading)[1]
    repeat = verify_citations({"ticker":"MSFT", "claims":[numeric, partial, unsupported]},
        ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert result.final_answer_markdown == repeat.final_answer_markdown
    assert result.report_draft_id == repeat.report_draft_id
    all_unsupported = verify_citations({"ticker":"MSFT", "claims":[unsupported]},
        ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert "The pinned filing evidence did not support an answer to this question." in all_unsupported.final_answer_markdown


@pytest.mark.parametrize("attempt,verdicts,expected_retry", [
    (1, ["supported"], False), (1, ["unsupported"], True),
    (1, ["partially_supported"], True), (2, ["unsupported"], False),
])
def test_retry_allowed_truth_table(tmp_path, attempt, verdicts, expected_retry):
    ledger, judge, _fact_id, numeric, unsupported, partial = _retry_fixture_ledger()
    claim_by_verdict = {"supported": numeric, "unsupported": unsupported, "partially_supported": partial}
    parent = None
    if attempt == 2:
        parent = "draft_2222222222222222"
        (tmp_path / "drafts").mkdir(exist_ok=True)
        (tmp_path / "drafts" / f"{parent}.json").write_text(json.dumps({"ticker":"MSFT"}), encoding="utf-8")
    result = verify_citations({"ticker":"MSFT", "claims":[claim_by_verdict[item] for item in verdicts],
        "attempt":attempt, **({"parent_draft_id":parent} if parent else {})},
        ledger=ledger, judge=judge, reports_dir=tmp_path)
    assert result.retry_allowed is expected_retry
    assert result.is_final is (not expected_retry)


def test_judge_error_leaves_no_draft(tmp_path):
    from app.domain.errors import JudgeUnavailableError
    text = "Revenue increased."
    evidence = Evidence(evidence_id="ev_1", ticker="MSFT", cik="0000789019", accession_number="acc",
        filing_form="10-K", filing_date="2026-01-01", section="mdna", chunk_id="c1", text=text,
        source_url="https://example.test", start_offset=0, end_offset=len(text), page_number=None,
        content_hash=hashlib.sha256(text.encode()).hexdigest(), retrieval_score=0.5)
    ledger = EvidenceLedger({"chunk": MemorySource(evidence)})
    ledger.register_chunk(evidence)
    class BrokenJudge:
        judge_id = "broken/1"
        def judge(self, *_): raise JudgeUnavailableError()
    claim = Claim(claim_id="n1", claim_text=text, claim_type="narrative", ticker="MSFT", evidence_ids=["ev_1"])
    with pytest.raises(JudgeUnavailableError):
        verify_citations({"ticker":"MSFT", "claims":[claim]}, ledger=ledger, judge=BrokenJudge(), reports_dir=tmp_path)
    assert not (tmp_path / "drafts").exists()


def test_unknown_evidence_id_fails_without_draft(tmp_path):
    from app.domain.errors import EvidenceNotFoundError
    claim = Claim(claim_id="old", claim_text="Old claim", claim_type="narrative", ticker="MSFT", evidence_ids=["ev_old"])
    with pytest.raises(EvidenceNotFoundError):
        verify_citations({"ticker":"MSFT", "claims":[claim]}, ledger=EvidenceLedger({}),
                         judge=ScriptedJudge({}), reports_dir=tmp_path)
    assert not (tmp_path / "drafts").exists()


def test_deterministic_numeric_claim_and_fresh_ledger_rejects_old_id():
    from app.citations.ledger import EvidenceLedger, fact_content_hash
    from app.domain.financial_facts import FinancialFact, fact_id
    fact = FinancialFact(ticker="MSFT", cik="0000789019", taxonomy="us-gaap", tag="Revenue",
        label="Revenue", value=Decimal("100"), unit="USD", period_start=date(2024,1,1),
        period_end=date(2024,12,31), fiscal_year=2025, fiscal_period="FY", form="10-K",
        filed_at=date(2025,2,1), accession_number="acc", frame=None, source_url="https://example.test/acc")
    source = FactSource(fact)
    ledger = EvidenceLedger({"fact": source})
    fact_eid = ledger.register_fact(fact)
    claim = Claim(claim_id="n", claim_text="Revenue was 100 USD", claim_type="numeric", ticker="MSFT",
        evidence_ids=[fact_eid], numeric=NumericAssertion(value=100, unit="USD", period_end=date(2024,12,31), period_start=date(2024,1,1)))
    result = __import__("app.citations.verifier", fromlist=["verify_claims"]).verify_claims([claim], ledger, ScriptedJudge({}))
    assert result.verifications[0].verdict == "supported"
    fresh = EvidenceLedger({"fact": source})
    rejected = __import__("app.citations.verifier", fromlist=["verify_claims"]).verify_claims([claim], fresh, ScriptedJudge({}))
    assert rejected.verifications[0].verdict == "unsupported"
    assert rejected.verifications[0].reason == "evidence_not_found"
