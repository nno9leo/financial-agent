import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import Settings
from app.domain.errors import (CacheIntegrityError, CompanyNotFoundError, ConfigurationError,
                               FilingRangeUnavailableError, SecHttpError, SecPayloadError, SecRateLimitedError, SecTimeoutError)
from app.domain.filings import ExcludedFiling, FilingListResult, FilingMetadata
from app.providers.base import SecProviderBase

BASE_SEC = "https://www.sec.gov"
BASE_DATA = "https://data.sec.gov"
SUPPORTED_FORMS = {"10-K", "10-Q", "8-K"}
AMENDMENT_FORMS = {"10-K/A", "10-Q/A", "8-K/A"}
EMPTY_REPORT_DATE_POLICY = "exclude_with_reason"


def normalize_cik(value: str | int) -> str:
    raw = str(value).strip()
    if raw.isdigit():
        number = int(raw)
        if 0 <= number <= 9_999_999_999:
            return f"{number:010d}"
    raise ValueError(f"Invalid CIK: {value!r}")


def accession_no_dashes(accession: str) -> str:
    compact = accession.replace("-", "")
    if not compact.isdigit() or len(compact) != 18:
        raise ValueError(f"Invalid accession number: {accession!r}")
    return compact


class SecProvider(SecProviderBase):
    def __init__(self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings or Settings()
        self._clock, self._sleep, self._last_request = clock, sleep, None
        self.cache_dir = Path(self.settings.data_dir) / "raw"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(timeout=httpx.Timeout(self.settings.request_timeout), transport=transport)

    def close(self) -> None:
        self.client.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def _headers(self) -> dict[str, str]:
        ua = self.settings.sec_user_agent
        if not ua or not ua.strip():
            raise ConfigurationError("SEC_USER_AGENT must be configured before a network request.")
        return {"User-Agent": ua.strip(), "Accept-Encoding": "gzip, deflate"}

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"

    def _cached(self, url: str) -> tuple[bytes, dict[str, Any]] | None:
        body_path, meta_path = self._paths(url)
        # Metadata is the commit marker; an interrupted body-only write is a miss.
        if not meta_path.exists():
            return None
        try:
            body, meta = body_path.read_bytes(), json.loads(meta_path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise CacheIntegrityError(url) from exc
        if meta.get("source_url") != url or hashlib.sha256(body).hexdigest() != meta.get("sha256"):
            raise CacheIntegrityError(url)
        meta["cache"] = "hit"
        return body, meta

    def _atomic_write(self, path: Path, data: bytes) -> None:
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as f:
                tmp_name = f.name
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if tmp_name and os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _fetch(self, url: str) -> tuple[bytes, dict[str, Any]]:
        cached = self._cached(url)
        if cached is not None:
            return cached
        headers = self._headers()  # Fail before any I/O, including throttle activity.
        for attempt in range(self.settings.max_retries):
            if self._last_request is not None:
                delay = self.settings.min_request_interval - (self._clock() - self._last_request)
                if delay > 0:
                    self._sleep(delay)
            self._last_request = self._clock()
            try:
                response = self.client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                if attempt + 1 == self.settings.max_retries:
                    raise SecTimeoutError(url) from exc
                self._sleep(min(0.25 * (2 ** attempt), 2.0))
                continue
            if response.status_code in (403, 429):
                if attempt + 1 == self.settings.max_retries:
                    raise SecRateLimitedError(response.status_code, url)
                self._sleep(min(0.25 * (2 ** attempt), 2.0))
                continue
            if response.status_code >= 500:
                if attempt + 1 == self.settings.max_retries:
                    raise SecHttpError(response.status_code, url)
                self._sleep(min(0.25 * (2 ** attempt), 2.0))
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise SecHttpError(response.status_code, url)
            body = response.content
            meta = {"source_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "sha256": hashlib.sha256(body).hexdigest(), "cache": "miss"}
            body_path, meta_path = self._paths(url)
            self._atomic_write(body_path, body)
            self._atomic_write(meta_path, (json.dumps({k: v for k, v in meta.items() if k != "cache"}, sort_keys=True, indent=2) + "\n").encode())
            return body, meta
        raise AssertionError("retry loop must return or raise")

    def _json(self, url: str) -> tuple[dict[str, Any], dict[str, Any]]:
        body, provenance = self._fetch(url)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise SecPayloadError(url, "response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SecPayloadError(url, "expected a JSON object")
        return payload, provenance

    def resolve_company(self, ticker_or_cik: str | int) -> tuple[str, str, str]:
        raw = str(ticker_or_cik).strip()
        if raw.isdigit():
            cik = normalize_cik(raw)
            url = f"{BASE_DATA}/submissions/CIK{cik}.json"
            try:
                payload, _ = self._json(url)
            except SecHttpError as exc:
                if exc.status_code == 404:
                    raise CompanyNotFoundError(raw) from exc
                raise
            tickers = payload.get("tickers")
            name = payload.get("name")
            if not isinstance(tickers, list) or not tickers or not isinstance(tickers[0], str) or not tickers[0].strip() or not isinstance(name, str) or not name.strip():
                raise CompanyNotFoundError(raw)
            return tickers[0].upper(), cik, name
        wanted = raw.upper()
        payload, _ = self._json(f"{BASE_SEC}/files/company_tickers.json")
        for row in payload.values():
            if str(row.get("ticker", "")).upper() == wanted:
                cik = normalize_cik(row["cik_str"])
                return wanted, cik, str(row["title"])
        raise CompanyNotFoundError(raw)

    def list_filings(self, cik: str | int, forms: set[str] | list[str], date_from: date | None = None,
                     date_to: date | None = None) -> FilingListResult:
        normalized_cik = normalize_cik(cik)
        requested = set(forms)
        if not requested or not requested <= SUPPORTED_FORMS:
            raise ValueError(f"forms must be a non-empty subset of {sorted(SUPPORTED_FORMS)}")
        url = f"{BASE_DATA}/submissions/CIK{normalized_cik}.json"
        data, _ = self._json(url)
        company = str(data.get("name", ""))
        tickers = data.get("tickers") or []
        if not tickers or not isinstance(tickers[0], str) or not tickers[0].strip():
            raise CompanyNotFoundError(normalized_cik)
        ticker = tickers[0].upper()
        recent = data.get("filings", {}).get("recent", {})
        files = data.get("filings", {}).get("files", [])
        forms_list = recent.get("form", [])
        dates = recent.get("filingDate", [])
        oldest_recent = min((_parse_date(d) for d in dates if d), default=None)
        if date_from and oldest_recent and date_from < oldest_recent and files:
            raise FilingRangeUnavailableError("Requested range may extend beyond submissions.recent; older filing pages are not followed.")
        result = []
        excluded: list[ExcludedFiling] = []
        for i, form in enumerate(forms_list):
            accession = recent.get("accessionNumber", [""] * len(forms_list))[i]
            filing_date = _parse_date(dates[i])
            base_form = form[:-2] if form.endswith("/A") else form
            if form in AMENDMENT_FORMS:
                if base_form in requested and (date_from is None or filing_date >= date_from) and (date_to is None or filing_date <= date_to):
                    excluded.append(ExcludedFiling(accession_number=accession, form=form, filing_date=filing_date, reason="amendment"))
                continue
            if form not in requested:
                continue  # Explicitly exclude amendments and all unsupported form spellings.
            if date_from and filing_date < date_from or date_to and filing_date > date_to:
                continue
            report = (recent.get("reportDate", [""] * len(forms_list))[i] or "").strip()
            if not report:
                excluded.append(ExcludedFiling(accession_number=accession, form=form, filing_date=filing_date, reason="missing_report_date"))
                continue  # EMPTY_REPORT_DATE_POLICY: exclude with reason; never invent filing_date.
            primary = recent["primaryDocument"][i]
            accession_path = accession_no_dashes(accession)
            source = f"{BASE_SEC}/Archives/edgar/data/{int(normalized_cik)}/{accession_path}/{primary}"
            result.append(FilingMetadata(ticker=ticker, cik=normalized_cik, company_name=company, form=form,
                                         accession_number=accession, filing_date=filing_date,
                                         report_date=_parse_date(report), primary_document=primary, source_url=source))
        return FilingListResult(filings=result, excluded=excluded, coverage_start=oldest_recent,
                                older_pages_not_loaded=bool(files) and date_from is None)

    def get_cached_or_fetch(self, url: str) -> tuple[bytes, dict[str, Any]]:
        """Fetch a URL through the provider cache; used for pinned SEC resources."""
        return self._fetch(url)

    def get_cached(self, url: str) -> tuple[bytes, dict[str, Any]] | None:
        """Return a verified cache hit only; this method never performs network I/O."""
        return self._cached(url)

    def get_company_facts(self, cik: str | int) -> tuple[bytes, dict[str, Any]]:
        normalized = normalize_cik(cik)
        return self._fetch(f"{BASE_DATA}/api/xbrl/companyfacts/CIK{normalized}.json")

    def get_filing_document(self, filing: FilingMetadata) -> tuple[bytes, dict[str, Any]]:
        return self._fetch(str(filing.source_url))


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)
