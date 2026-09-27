from app.citations.deterministic import run_deterministic_checks, validate_claim_shape
from app.citations.ledger import EvidenceLedger
from app.citations.semantic import SemanticJudge
from app.domain.claims import Claim, ClaimVerification, VerificationReport
from app.domain.errors import EmptyClaimSetError


def verify_claims(claims: list[Claim], ledger: EvidenceLedger, judge: SemanticJudge) -> VerificationReport:
    if not claims:
        raise EmptyClaimSetError()
    for claim in claims:
        validate_claim_shape(claim)
    verifications = []
    for claim in claims:
        outcome = run_deterministic_checks(claim, ledger)
        if not outcome.passed:
            verdict, score, reason = "unsupported", None, outcome.failure_reason
        elif claim.claim_type == "narrative":
            texts = [item.chunk.text for item in outcome.resolved if item.chunk is not None]
            if hasattr(judge, "bind_evidence_ids"):
                judge.bind_evidence_ids(claim.evidence_ids)
            judged = judge.judge(claim.claim_text, texts)
            verdict = {"entailed": "supported", "partial": "partially_supported",
                       "not_entailed": "unsupported"}[judged.label]
            score, reason = judged.score, judged.rationale
        else:
            verdict, score, reason = "supported", None, "deterministic_match"
        verifications.append(ClaimVerification(claim_id=claim.claim_id, claim_text=claim.claim_text,
            claim_type=claim.claim_type, evidence_ids=claim.evidence_ids, verdict=verdict,
            deterministic_checks=outcome.checks, semantic_score=score, reason=reason))
    supported = sum(v.verdict == "supported" for v in verifications)
    partial = sum(v.verdict == "partially_supported" for v in verifications)
    unsupported = len(verifications) - supported - partial
    return VerificationReport(verifications=verifications, supported=supported,
                              partially_supported=partial, unsupported=unsupported,
                              unsupported_rate=unsupported / len(verifications), judge_id=judge.judge_id)
