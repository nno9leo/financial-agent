from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class NumericAssertion(BaseModel):
    value: Decimal
    scale: Literal["units", "thousands", "millions", "billions"] = "units"
    unit: Literal["USD", "percent", "ratio"]
    period_end: date
    period_start: date | None = None


class MetadataAssertion(BaseModel):
    field: Literal["accession_number", "filing_date", "report_date", "form", "ticker", "cik"]
    value: str


class Claim(BaseModel):
    claim_id: str
    claim_text: str
    claim_type: Literal["numeric", "metadata", "narrative"]
    ticker: str
    evidence_ids: list[str]
    numeric: NumericAssertion | None = None
    metadata: MetadataAssertion | None = None


class ClaimVerification(BaseModel):
    claim_id: str
    claim_text: str
    claim_type: Literal["numeric", "metadata", "narrative"]
    evidence_ids: list[str]
    verdict: Literal["supported", "partially_supported", "unsupported"]
    deterministic_checks: dict[str, bool | str | float]
    semantic_score: float | None
    reason: str


class VerificationReport(BaseModel):
    verifications: list[ClaimVerification]
    supported: int
    partially_supported: int
    unsupported: int
    unsupported_rate: float
    judge_id: str
