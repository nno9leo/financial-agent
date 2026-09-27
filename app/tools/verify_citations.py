"""Verify structured claims and persist a deterministic report draft."""
from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.citations.ledger import EvidenceLedger
from app.citations.verifier import verify_claims
from app.domain.claims import Claim, VerificationReport
from app.domain.errors import (ConfigurationError, RetryBudgetExhaustedError,
                               RetryParentDraftError)


class VerifyCitationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(min_length=1, max_length=50)
    ticker: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    parent_draft_id: str | None = None

    @field_validator("ticker")
    @classmethod
    def ticker_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip().upper()


class VerifyCitationsOutput(BaseModel):
    report: VerificationReport
    report_draft_id: str
    final_answer_markdown: str
    retry_allowed: bool
    retry_guidance: str | None
    is_final: bool
    # Per-call runtime facts from the judge, required for ADR 0009 auditability.
    judge_observations: list[dict] = Field(default_factory=list)


def max_verify_attempts(config_path: Path | None = None) -> int:
    path = config_path or Path(__file__).resolve().parents[2] / "config" / "agent_runtime.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))["max_verify_attempts"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigurationError("max_verify_attempts is missing from config/agent_runtime.json") from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError("max_verify_attempts must be a positive integer")
    return value


def verify_citations(request: VerifyCitationsInput | dict, *, ledger: EvidenceLedger, judge,
                     reports_dir: Path, max_attempts: int | None = None) -> VerifyCitationsOutput:
    args = request if isinstance(request, VerifyCitationsInput) else VerifyCitationsInput.model_validate(request)
    limit = max_verify_attempts() if max_attempts is None else max_attempts
    if args.attempt > limit:
        raise RetryBudgetExhaustedError(args.attempt, limit)
    if any(claim.ticker.upper() != args.ticker for claim in args.claims):
        from app.domain.errors import InvalidClaimError
        raise InvalidClaimError("Every claim ticker must match the request ticker.")
    if args.attempt > 1 and not args.parent_draft_id:
        raise RetryParentDraftError(None, "a parent draft ID is required after the first attempt")
    if args.parent_draft_id:
        _validate_parent_draft(args.parent_draft_id, args.ticker, reports_dir)
    report = verify_claims(args.claims, ledger, judge)
    evidence = []
    for evidence_id in sorted({eid for claim in args.claims for eid in claim.evidence_ids}):
        resolved = ledger.resolve(evidence_id)
        entry = resolved.entry
        row = {"evidence_id": evidence_id, "kind": entry.kind, "ticker": entry.ticker,
               "cik": entry.cik, "accession_number": entry.accession_number,
               "section": resolved.chunk.section if resolved.chunk else None,
               "source_url": str(resolved.chunk.source_url) if resolved.chunk else None}
        manifest_entry = next((item for item in ledger.manifest.entries
                               if item.accession_number == entry.accession_number), None) if ledger.manifest else None
        if manifest_entry:
            row.update({"form": manifest_entry.form, "report_date": manifest_entry.report_date,
                        "filing_date": manifest_entry.filing_date,
                        "source_url": row["source_url"] or manifest_entry.source_url})
        if resolved.chunk:
            row["text"] = resolved.chunk.text
        elif resolved.metric:
            row["metric"] = resolved.metric.model_dump(mode="json")
        elif resolved.fact:
            row["fact"] = resolved.fact.model_dump(mode="json")
        evidence.append(row)
    unsupported = report.unsupported
    retry_allowed = unsupported + report.partially_supported > 0 and args.attempt < limit
    retry_guidance = (f"You may retrieve more evidence once (search_filing_chunks / calculate_financial_metric) and call verify_citations again with attempt={args.attempt + 1} and parent_draft_id=…; otherwise present final_answer_markdown as is."
                      if retry_allowed else None)
    is_final = not retry_allowed
    final_answer = _compose_final_answer(report, evidence, args.attempt)
    payload = {"ticker": args.ticker, "attempt": args.attempt, "parent_draft_id": args.parent_draft_id,
               "claims": [c.model_dump(mode="json") for c in args.claims],
               "verifications": [v.model_dump(mode="json") for v in report.verifications],
               "judge_id": report.judge_id, "evidence": evidence, "created_from": "verify_citations",
               "final_answer_markdown": final_answer, "retry_allowed": retry_allowed,
               "retry_guidance": retry_guidance, "is_final": is_final}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    draft_id = "draft_" + sha256(canonical).hexdigest()[:16]
    drafts_dir = Path(reports_dir) / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    target = drafts_dir / f"{draft_id}.json"
    if not target.exists():
        fd, temp_name = tempfile.mkstemp(prefix=f"{draft_id}.", suffix=".tmp", dir=drafts_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp_name, target)
            except FileExistsError:
                pass
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    judge_observations = [dict(vars(call)) for call in getattr(judge, "calls", [])]
    return VerifyCitationsOutput(report=report, report_draft_id=draft_id,
        final_answer_markdown=final_answer, retry_allowed=retry_allowed,
        retry_guidance=retry_guidance, is_final=is_final, judge_observations=judge_observations)


def _validate_parent_draft(draft_id: str, ticker: str, reports_dir: Path) -> None:
    if not re.fullmatch(r"draft_[a-f0-9]{16}", draft_id):
        raise RetryParentDraftError(draft_id, "the ID is not a valid report draft ID")
    path = Path(reports_dir) / "drafts" / f"{draft_id}.json"
    try:
        parent = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RetryParentDraftError(draft_id, "the draft does not exist or is unreadable") from exc
    if str(parent.get("ticker", "")).upper() != ticker:
        raise RetryParentDraftError(draft_id, "the draft ticker does not match the retry ticker")


def _compose_final_answer(report: VerificationReport, evidence: list[dict], attempt: int) -> str:
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    lines: list[str] = []
    supported = [item for item in report.verifications if item.verdict == "supported"]
    partial = [item for item in report.verifications if item.verdict == "partially_supported"]
    unsupported = [item for item in report.verifications if item.verdict == "unsupported"]
    if not supported:
        lines.append("The pinned filing evidence did not support an answer to this question.")
    for item in supported:
        line = item.claim_text
        if item.claim_type == "numeric":
            value_period = _numeric_evidence_display(item.evidence_ids, evidence_by_id)
            if value_period:
                line += f" — {value_period}"
        citations = " ".join(f"[{evidence_id}]" for evidence_id in item.evidence_ids)
        if citations:
            line += f" {citations}"
        lines.append(line)
    for item in partial:
        citations = " ".join(f"[{evidence_id}]" for evidence_id in item.evidence_ids)
        line = f"Partially supported: {item.claim_text} — {item.reason}"
        if citations:
            line += f" {citations}"
        lines.append(line)
    if unsupported:
        count = len(unsupported)
        noun = "claim" if count == 1 else "claims"
        verb = "was" if count == 1 else "were"
        lines.append(f"{count} {noun} {verb} removed because the evidence did not support them.")
        lines.extend(["", "## Not supported by the filing evidence"])
        lines.extend(f"- {item.claim_text} — {item.reason}" for item in unsupported)
    return "\n".join(lines)


def _numeric_evidence_display(evidence_ids: list[str], evidence_by_id: dict[str, dict]) -> str | None:
    for evidence_id in evidence_ids:
        row = evidence_by_id.get(evidence_id, {})
        metric = row.get("metric")
        if metric:
            period = _period_text(metric.get("period_start"), metric.get("period_end"))
            return f"Value: {metric.get('display_value', metric.get('result'))}; period: {period}"
        fact = row.get("fact")
        if fact:
            period = _period_text(fact.get("period_start"), fact.get("period_end"))
            return f"Value: {fact.get('value')} {fact.get('unit')}; period: {period}"
    return None


def _period_text(start: str | None, end: str | None) -> str:
    return f"{start} to {end}" if start else f"ending {end}"
