from datetime import date

from pydantic import BaseModel, HttpUrl


class Evidence(BaseModel):
    evidence_id: str
    ticker: str
    cik: str
    accession_number: str
    filing_form: str
    filing_date: date
    section: str
    chunk_id: str
    text: str
    source_url: HttpUrl
    start_offset: int | None
    end_offset: int | None
    page_number: int | None
    content_hash: str
    retrieval_score: float | None
