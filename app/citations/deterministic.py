"""Ordered, short-circuiting checks on reloaded evidence."""

from decimal import Decimal

from pydantic import BaseModel

from app.citations.ledger import EvidenceLedger, ResolvedEvidence
from app.domain.claims import Claim
from app.domain.errors import (CompanyFactsSnapshotMismatchError, EvidenceHashMismatchError,
                               EvidenceNotFoundError, InvalidClaimError)

SCALES = {"units": Decimal(1), "thousands": Decimal(1000),
          "millions": Decimal(1_000_000), "billions": Decimal(1_000_000_000)}


class DeterministicOutcome(BaseModel):
    passed: bool
    checks: dict[str, bool | str | float]
    failure_reason: str | None
    resolved: list[ResolvedEvidence]


def validate_claim_shape(claim: Claim) -> None:
    if claim.claim_type == "numeric":
        if claim.numeric is None or claim.metadata is not None:
            raise InvalidClaimError("A numeric claim requires numeric and forbids metadata.")
        if claim.numeric.unit != "USD" and claim.numeric.scale != "units":
            raise InvalidClaimError("Percent and ratio claims must use the units scale.")
        if not claim.numeric.value.is_finite():
            raise InvalidClaimError("Numeric assertion must be finite.")
    elif claim.claim_type == "metadata":
        if claim.metadata is None or claim.numeric is not None:
            raise InvalidClaimError("A metadata claim requires metadata and forbids numeric.")
    elif claim.numeric is not None or claim.metadata is not None:
        raise InvalidClaimError("A narrative claim cannot carry numeric or metadata assertions.")


def _fail(checks, reason, resolved):
    return DeterministicOutcome(passed=False, checks=checks, failure_reason=reason, resolved=resolved)


def _provenance(item: ResolvedEvidence):
    if item.chunk is not None:
        return [item.chunk]
    elif item.fact is not None:
        return [item.fact]
    return item.metric.inputs if item.metric else []


def run_deterministic_checks(claim: Claim, ledger: EvidenceLedger) -> DeterministicOutcome:
    validate_claim_shape(claim)
    checks: dict[str, bool | str | float] = {}
    resolved: list[ResolvedEvidence] = []
    checks["evidence_ids_present"] = bool(claim.evidence_ids)
    if not claim.evidence_ids:
        return _fail(checks, "no_evidence", resolved)
    for evidence_id in claim.evidence_ids:
        try:
            entry = ledger.entry(evidence_id)
        except EvidenceNotFoundError:
            checks["evidence_exists"] = False
            checks["failed_evidence_id"] = evidence_id
            return _fail(checks, "evidence_not_found", resolved)
    checks["evidence_exists"] = True
    for evidence_id in claim.evidence_ids:
        try:
            resolved.append(ledger.resolve(evidence_id))
        except EvidenceNotFoundError:
            checks["hash_matches"] = False
            checks["failed_evidence_id"] = evidence_id
            return _fail(checks, "evidence_not_found", resolved)
        except (EvidenceHashMismatchError, CompanyFactsSnapshotMismatchError):
            checks["hash_matches"] = False
            checks["failed_evidence_id"] = evidence_id
            return _fail(checks, "hash_mismatch", resolved)
    checks["hash_matches"] = True
    accessions = {r.entry.accession_number for r in resolved}
    for item in resolved:
        provenance = _provenance(item)
        pinned = next((p for p in ledger.manifest.entries
                       if p.accession_number == item.entry.accession_number), None) if ledger.manifest else None
        expected = (item.entry.ticker, item.entry.cik, item.entry.accession_number)
        if not provenance or any((value.ticker, value.cik, value.accession_number) != expected for value in provenance) or (
            pinned is not None and (expected != (pinned.ticker, pinned.cik, pinned.accession_number) or
            any((str(value.filing_date) if item.chunk else str(value.filed_at)) != pinned.filing_date or
                (value.filing_form if item.chunk else value.form) != pinned.form for value in provenance))):
            checks["provenance_consistent"] = False
            return _fail(checks, "provenance_mismatch", resolved)
    checks["provenance_consistent"] = True
    checks["ticker_matches"] = all(r.entry.ticker == claim.ticker for r in resolved)
    if not checks["ticker_matches"]:
        return _fail(checks, "ticker_mismatch", resolved)
    checks["single_accession"] = len(accessions) == 1
    if not checks["single_accession"]:
        return _fail(checks, "mixed_accessions", resolved)

    if claim.claim_type == "numeric":
        candidates = []
        for item in resolved:
            if item.fact is not None and item.fact.unit == "USD":
                candidates.append((item.entry.evidence_id, "USD", item.fact.value,
                                   item.fact.period_start, item.fact.period_end))
            if item.metric is not None:
                metric = item.metric
                if metric.result_unit == "USD":
                    candidates.append((item.entry.evidence_id, "USD", metric.result,
                                       metric.period_start, metric.period_end))
                elif metric.result_unit == "ratio":
                    candidates.extend([(item.entry.evidence_id, "percent", metric.result * 100,
                                        metric.period_start, metric.period_end),
                                       (item.entry.evidence_id, "ratio", metric.result,
                                        metric.period_start, metric.period_end)])
        checks["structured_evidence_present"] = any(r.fact is not None or r.metric is not None for r in resolved)
        if not checks["structured_evidence_present"]:
            return _fail(checks, "numeric_claim_without_structured_evidence", resolved)
        unit_candidates = [c for c in candidates if c[1] == claim.numeric.unit]
        checks["unit_matches"] = bool(unit_candidates)
        if not unit_candidates:
            return _fail(checks, "unit_mismatch", resolved)
        period_candidates = [c for c in unit_candidates if c[4] == claim.numeric.period_end
                             and (claim.numeric.period_start is None or c[3] == claim.numeric.period_start)]
        checks["period_matches"] = bool(period_candidates)
        if not period_candidates:
            return _fail(checks, "period_mismatch", resolved)
        scale = SCALES[claim.numeric.scale]
        tolerance = Decimal("0.5") * (Decimal(10) ** claim.numeric.value.as_tuple().exponent) * scale
        target = claim.numeric.value * scale
        best = min(period_candidates, key=lambda c: abs(target - c[2]))
        difference = abs(target - best[2])
        checks.update(matched_evidence_id=best[0], candidate_value=str(best[2]),
                      absolute_difference=str(difference), tolerance=str(tolerance),
                      value_matches=difference <= tolerance)
        if not checks["value_matches"]:
            return _fail(checks, "value_mismatch", resolved)
    elif claim.claim_type == "metadata":
        field, asserted = claim.metadata.field, claim.metadata.value.strip()
        for item in resolved:
            pinned = next((p for p in ledger.manifest.entries
                           if p.accession_number == item.entry.accession_number), None) if ledger.manifest else None
            if field in ("filing_date", "report_date", "form"):
                actual = str(getattr(pinned, field)).strip() if pinned else None
            else:
                actual = str(getattr(_provenance(item)[0], field)).strip()
            if actual != asserted:
                checks["metadata_matches"] = False
                return _fail(checks, "metadata_mismatch", resolved)
        checks["metadata_matches"] = True
    else:
        checks["text_evidence_present"] = any(r.chunk is not None for r in resolved)
        if not checks["text_evidence_present"]:
            return _fail(checks, "narrative_claim_without_text_evidence", resolved)
    return DeterministicOutcome(passed=True, checks=checks, failure_reason=None, resolved=resolved)
