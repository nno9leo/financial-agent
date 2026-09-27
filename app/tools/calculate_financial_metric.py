"""Cache-only tool contract for calculating a metric from a pinned 10-K."""

from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.compute.metrics import Metric, MetricResult, calculate_metric
from app.config import Settings
from app.domain.errors import CompanyFactsNotCachedError, CompanyFactsSnapshotMismatchError
from app.domain.financial_facts import FinancialFact, normalize_companyfacts
from app.ingestion.manifest import (FilingManifest, latest_manifest_path, load_manifest,
                                    resolve_manifest_entry)
from app.providers.sec import SecProvider


class CalculateFinancialMetricInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Metric
    company: str = Field(min_length=1)
    fiscal_year: int
    accession_number: str | None = None

    @field_validator("company")
    @classmethod
    def company_not_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace")
        return value.strip()


class CalculateFinancialMetricOutput(BaseModel):
    result: MetricResult
    ticker: str
    cik: str
    accession_number: str
    accession_resolved_from_manifest: bool
    fiscal_year: int
    source_url: str
    companyfacts_sha256: str
    metric_evidence_id: str = ""


_NORMALIZED_CACHE: dict[tuple[str, str], tuple[FinancialFact, ...]] = {}


def _normalize_cached(accession_number: str, companyfacts_sha256: str, body: bytes,
                      ticker: str, cik: str, entry) -> tuple[FinancialFact, ...]:
    key = accession_number, companyfacts_sha256
    if key not in _NORMALIZED_CACHE:
        _NORMALIZED_CACHE[key] = tuple(normalize_companyfacts(body, ticker=ticker, cik=cik, pinned=entry))
    return _NORMALIZED_CACHE[key]


def clear_normalized_companyfacts_cache() -> None:
    _NORMALIZED_CACHE.clear()


def calculate_financial_metric(request: CalculateFinancialMetricInput | dict, *,
                               provider: SecProvider | None = None,
                               manifest: FilingManifest | None = None,
                               manifests_dir: Path | None = None) -> CalculateFinancialMetricOutput:
    parsed = request if isinstance(request, CalculateFinancialMetricInput) else CalculateFinancialMetricInput.model_validate(request)
    if manifest is None:
        if manifests_dir is None:
            manifests_dir = Path(__file__).resolve().parents[2] / "data" / "manifests"
        manifest = load_manifest(latest_manifest_path(Path(manifests_dir)))
    entry, resolved = resolve_manifest_entry(manifest, parsed.company, parsed.accession_number)
    owns_provider = provider is None
    provider = provider or SecProvider(Settings())
    try:
        cached = provider.get_cached(entry.companyfacts_url)
    finally:
        if owns_provider:
            provider.close()
    if cached is None:
        raise CompanyFactsNotCachedError(entry.companyfacts_url)
    body, _metadata = cached
    observed_hash = sha256(body).hexdigest()
    if observed_hash != entry.companyfacts_sha256:
        raise CompanyFactsSnapshotMismatchError(entry.ticker, entry.companyfacts_sha256, observed_hash)
    facts = _normalize_cached(entry.accession_number, entry.companyfacts_sha256, body,
                              entry.ticker, entry.cik, entry)
    result = calculate_metric(parsed.metric, ticker=entry.ticker, fiscal_year=parsed.fiscal_year,
                              facts=list(facts), pinned=entry)
    return CalculateFinancialMetricOutput(
        result=result, ticker=entry.ticker, cik=entry.cik, accession_number=entry.accession_number,
        accession_resolved_from_manifest=resolved, fiscal_year=parsed.fiscal_year,
        source_url=entry.source_url, companyfacts_sha256=entry.companyfacts_sha256,
        metric_evidence_id=metric_evidence_id(result, entry.ticker, parsed.fiscal_year, entry.accession_number),
    )


def metric_evidence_id(result: MetricResult, ticker: str, fiscal_year: int, accession_number: str) -> str:
    import json
    from hashlib import sha256
    payload = {"metric": result.metric, "ticker": ticker, "fiscal_year": fiscal_year,
               "accession_number": accession_number}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "met_" + sha256(canonical.encode()).hexdigest()[:20]


class CalculateFinancialMetricTool:
    def __init__(self, provider: SecProvider | None = None, *, manifest: FilingManifest | None = None,
                 manifests_dir: Path | None = None):
        self.provider, self.manifest, self.manifests_dir = provider, manifest, manifests_dir

    def __call__(self, request: CalculateFinancialMetricInput) -> CalculateFinancialMetricOutput:
        return calculate_financial_metric(request, provider=self.provider, manifest=self.manifest,
                                          manifests_dir=self.manifests_dir)
