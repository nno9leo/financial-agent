from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.domain.filings import ExcludedFiling, FilingMetadata
from app.providers.sec import SecProvider


class ListFilingsInput(BaseModel):
    company: str
    form: Literal["10-K", "10-Q", "8-K"]
    date_from: date | None = None
    date_to: date | None = None


class CompanyMetadata(BaseModel):
    ticker: str
    cik: str
    company_name: str


class ListFilingsOutput(BaseModel):
    filings: list[FilingMetadata]
    excluded: list[ExcludedFiling]
    coverage_start: date | None
    older_pages_not_loaded: bool
    company: CompanyMetadata


def list_filings(query: ListFilingsInput | dict, provider: SecProvider | None = None) -> ListFilingsOutput:
    args = query if isinstance(query, ListFilingsInput) else ListFilingsInput.model_validate(query)
    sec = provider or SecProvider()
    try:
        ticker, cik, company_name = sec.resolve_company(args.company)
        result = sec.list_filings(cik, {args.form}, args.date_from, args.date_to)
        return ListFilingsOutput(filings=result.filings, excluded=result.excluded,
                                 coverage_start=result.coverage_start, older_pages_not_loaded=result.older_pages_not_loaded,
                                 company=CompanyMetadata(ticker=ticker, cik=cik, company_name=company_name))
    finally:
        if provider is None:
            sec.close()
