from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, HttpUrl


class FilingMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    company_name: str
    form: Literal["10-K", "10-Q", "8-K"]
    accession_number: str
    filing_date: date
    report_date: date
    primary_document: str
    source_url: HttpUrl


class ExcludedFiling(BaseModel):
    accession_number: str
    form: str
    filing_date: date
    reason: Literal["amendment", "missing_report_date"]


class FilingListResult(BaseModel):
    filings: list[FilingMetadata]
    excluded: list[ExcludedFiling]
    coverage_start: date | None
    older_pages_not_loaded: bool
