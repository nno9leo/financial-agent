from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from app.domain.filings import FilingListResult, FilingMetadata


class SecProviderBase(ABC):
    @abstractmethod
    def resolve_company(self, ticker_or_cik: str | int) -> tuple[str, str, str]: ...

    @abstractmethod
    def list_filings(self, cik: str | int, forms: set[str] | list[str], date_from: date | None = None, date_to: date | None = None) -> FilingListResult: ...

    @abstractmethod
    def get_company_facts(self, cik: str | int) -> tuple[bytes, dict[str, Any]]: ...

    @abstractmethod
    def get_filing_document(self, filing: FilingMetadata) -> tuple[bytes, dict[str, Any]]: ...
