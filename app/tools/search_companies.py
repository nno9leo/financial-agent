import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.errors import CompanyNotFoundError, ResearchError
from app.ingestion.manifest import FilingManifest, latest_manifest_path, load_manifest
from app.providers.sec import BASE_SEC, normalize_cik


class CompanySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("must not be empty or whitespace")
        if not value.isdigit() and len(value) < 3:
            raise ValueError("company-name queries must contain at least 3 characters")
        return value


class CompanyCandidate(BaseModel):
    ticker: str
    cik: str
    company_name: str
    match_type: Literal["ticker", "cik", "name"]
    pinned: bool
    pinned_accession: str | None


class CompanySearchOutput(BaseModel):
    candidates: list[CompanyCandidate]
    ambiguous: bool


class CachedProvider(Protocol):
    def get_cached(self, url: str): ...


class CompanyTickersCacheError(ResearchError):
    def __init__(self):
        super().__init__("company_tickers_not_cached", "SEC company_tickers.json is not in the raw cache.",
                         "Populate the SEC raw cache before searching companies; this tool never fetches.")


class CompanyTickersPayloadError(ResearchError):
    def __init__(self):
        super().__init__("company_tickers_invalid_payload", "Cached company_tickers.json is not a valid company map.",
                         "Repair or refresh the SEC raw cache before searching companies.")


def search_companies(request: CompanySearchInput | dict, *, provider: CachedProvider,
                     manifest: FilingManifest | None = None, manifests_dir=None) -> CompanySearchOutput:
    parsed = request if isinstance(request, CompanySearchInput) else CompanySearchInput.model_validate(request)
    cached = provider.get_cached(f"{BASE_SEC}/files/company_tickers.json")
    if cached is None:
        raise CompanyTickersCacheError()
    body = cached[0]
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict): raise ValueError("expected object")
    except (ValueError, TypeError) as exc:
        raise CompanyTickersPayloadError() from exc
    if manifest is None:
        if manifests_dir is None:
            from pathlib import Path
            manifests_dir = Path(__file__).resolve().parents[2] / "data" / "manifests"
        manifest = load_manifest(latest_manifest_path(manifests_dir))
    query = parsed.query
    wanted_ticker = query.upper()
    wanted_cik = (query.lstrip("0") or "0") if query.isdigit() else None
    rows = []
    for item in payload.values():
        ticker = str(item.get("ticker", "")).upper()
        cik = normalize_cik(item["cik_str"])
        name = str(item.get("title", ""))
        typ = "ticker" if ticker == wanted_ticker else ("cik" if wanted_cik is not None and (cik.lstrip("0") or "0") == wanted_cik else ("name" if query.casefold() in name.casefold() else None))
        if typ:
            pinned = next((e for e in manifest.entries if e.ticker.upper() == ticker), None)
            rows.append(CompanyCandidate(ticker=ticker, cik=cik, company_name=name, match_type=typ,
                                         pinned=pinned is not None, pinned_accession=pinned.accession_number if pinned else None))
    rank = {"ticker": 0, "cik": 1, "name": 2}
    rows.sort(key=lambda row: (rank[row.match_type], row.ticker))
    if not rows:
        raise CompanyNotFoundError(query)
    return CompanySearchOutput(candidates=rows, ambiguous=len(rows) > 1)
