"""Register evidence IDs, then reload content from pinned authoritative sources."""

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from app.compute.metrics import MetricResult
from app.config import Settings
from app.domain.evidence import Evidence
from app.domain.errors import (CompanyFactsNotCachedError, CompanyFactsSnapshotMismatchError,
                               EvidenceConflictError, EvidenceHashMismatchError, EvidenceNotFoundError)
from app.domain.financial_facts import FinancialFact, fact_id
from app.ingestion.chunking import Chunk
from app.ingestion.manifest import FilingManifest, ManifestEntry
from app.providers.sec import SecProvider
from app.tools.calculate_financial_metric import (CalculateFinancialMetricInput,
                                                  CalculateFinancialMetricOutput,
                                                  _normalize_cached, calculate_financial_metric,
                                                  metric_evidence_id)


def canonical_hash(data: dict) -> str:
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def fact_content_hash(fact: FinancialFact) -> str:
    return canonical_hash({"accession_number": fact.accession_number, "taxonomy": fact.taxonomy,
                           "tag": fact.tag, "unit": fact.unit,
                           "period_start": fact.period_start.isoformat() if fact.period_start else None,
                           "period_end": fact.period_end.isoformat(), "value": str(fact.value)})


def metric_id(output: CalculateFinancialMetricOutput) -> str:
    return metric_evidence_id(output.result, output.ticker, output.fiscal_year, output.accession_number)


def metric_content_hash(metric: MetricResult) -> str:
    return canonical_hash({"metric": metric.metric, "formula": metric.formula, "result": str(metric.result),
                           "result_unit": metric.result_unit,
                           "period_start": metric.period_start.isoformat() if metric.period_start else None,
                           "period_end": metric.period_end.isoformat(), "evidence_ids": metric.evidence_ids})


class LedgerEntry(BaseModel):
    evidence_id: str
    kind: Literal["chunk", "fact", "metric"]
    content_hash: str
    ticker: str
    cik: str
    accession_number: str


class ResolvedEvidence(BaseModel):
    entry: LedgerEntry
    observed_hash: str
    chunk: Evidence | None = None
    fact: FinancialFact | None = None
    metric: MetricResult | None = None


class EvidenceSource(Protocol):
    kind: Literal["chunk", "fact", "metric"]

    def load(self, entry: LedgerEntry) -> ResolvedEvidence: ...


class CanonicalChunkSource:
    kind: Literal["chunk"] = "chunk"

    def __init__(self, normalized_dir: Path):
        self.normalized_dir = Path(normalized_dir)

    def load(self, entry: LedgerEntry) -> ResolvedEvidence:
        folder = self.normalized_dir / entry.accession_number
        try:
            with (folder / "chunks.jsonl").open(encoding="utf-8") as handle:
                row = None
                for line in handle:
                    candidate = json.loads(line)
                    if candidate.get("evidence_id") == entry.evidence_id:
                        row = candidate
                        break
            if row is None:
                raise EvidenceNotFoundError(entry.evidence_id)
            chunk = Chunk.model_validate(row)
            canonical = (folder / "canonical.txt").read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise EvidenceNotFoundError(entry.evidence_id) from exc
        text = canonical[chunk.start_offset:chunk.end_offset]
        evidence = chunk.to_evidence(retrieval_score=None).model_copy(update={"text": text})
        return ResolvedEvidence(entry=entry, observed_hash=sha256(text.encode()).hexdigest(), chunk=evidence)


class CompanyFactsFactSource:
    kind: Literal["fact"] = "fact"

    def __init__(self, manifest: FilingManifest, data_dir: Path = Path("data"), provider=None):
        self.manifest, self.data_dir, self.provider = manifest, Path(data_dir), provider
        self._registered: dict[str, FinancialFact] = {}

    def remember(self, evidence_id: str, fact: FinancialFact) -> None:
        self._registered[evidence_id] = fact

    def load(self, entry: LedgerEntry) -> ResolvedEvidence:
        pinned = next((item for item in self.manifest.entries
                       if item.accession_number == entry.accession_number), None)
        if pinned is None:
            raise EvidenceNotFoundError(entry.evidence_id)
        if self.provider is not None:
            cached = self.provider.get_cached(pinned.companyfacts_url)
        else:
            with SecProvider(Settings(_env_file=None, data_dir=self.data_dir)) as provider:
                cached = provider.get_cached(pinned.companyfacts_url)
        if cached is None:
            raise CompanyFactsNotCachedError(pinned.companyfacts_url)
        body, _ = cached
        observed = sha256(body).hexdigest()
        if observed != pinned.companyfacts_sha256:
            raise CompanyFactsSnapshotMismatchError(pinned.ticker, pinned.companyfacts_sha256, observed)
        facts = _normalize_cached(pinned.accession_number, pinned.companyfacts_sha256, body,
                                  pinned.ticker, pinned.cik, pinned)
        registered = self._registered.get(entry.evidence_id)
        fact = next((item for item in facts if fact_id(item) == entry.evidence_id
                     and item.accession_number == entry.accession_number), None)
        if fact is None and registered is not None:
            # A changed value changes fact_id; locate the same source coordinate so
            # the verifier can report hash_mismatch instead of evidence_not_found.
            fact = next((item for item in facts if
                         (item.accession_number, item.taxonomy, item.tag, item.unit,
                          item.period_start, item.period_end) ==
                         (registered.accession_number, registered.taxonomy, registered.tag,
                          registered.unit, registered.period_start, registered.period_end)), None)
        if fact is None:
            raise EvidenceNotFoundError(entry.evidence_id)
        if registered is None:
            self._registered[entry.evidence_id] = fact
        return ResolvedEvidence(entry=entry, observed_hash=fact_content_hash(fact), fact=fact)


class MetricRecomputeSource:
    kind: Literal["metric"] = "metric"

    def __init__(self, manifest: FilingManifest, data_dir: Path = Path("data"), provider=None):
        self.manifest, self.data_dir, self.provider = manifest, Path(data_dir), provider
        self._requests: dict[str, CalculateFinancialMetricInput] = {}
        self._outputs: dict[str, CalculateFinancialMetricOutput] = {}

    def remember(self, evidence_id: str, output: CalculateFinancialMetricOutput) -> None:
        self._requests[evidence_id] = CalculateFinancialMetricInput(
            metric=output.result.metric, company=output.ticker, fiscal_year=output.fiscal_year,
            accession_number=output.accession_number)
        self._outputs[evidence_id] = output

    def load(self, entry: LedgerEntry) -> ResolvedEvidence:
        request = self._requests.get(entry.evidence_id)
        if request is None:
            raise EvidenceNotFoundError(entry.evidence_id)
        output = self._outputs.get(entry.evidence_id)
        if output is not None and hasattr(self.provider, "get_cached"):
            # Validate the source snapshot even though the computation itself is cached
            # as an explicit registered result; never accept changed companyfacts bytes.
            pinned = next((item for item in self.manifest.entries if item.accession_number == entry.accession_number), None)
            cached = self.provider.get_cached(pinned.companyfacts_url) if pinned else None
            if cached is None:
                raise CompanyFactsNotCachedError(pinned.companyfacts_url if pinned else entry.accession_number)
            body, _ = cached
            if sha256(body).hexdigest() != pinned.companyfacts_sha256:
                raise CompanyFactsSnapshotMismatchError(pinned.ticker, pinned.companyfacts_sha256, sha256(body).hexdigest())
        if self.provider is not None:
            output = calculate_financial_metric(request, provider=self.provider, manifest=self.manifest)
        else:
            with SecProvider(Settings(_env_file=None, data_dir=self.data_dir)) as provider:
                output = calculate_financial_metric(request, provider=provider, manifest=self.manifest)
        return ResolvedEvidence(entry=entry, observed_hash=metric_content_hash(output.result), metric=output.result)


class EvidenceLedger:
    def __init__(self, sources: Mapping[str, EvidenceSource], manifest: FilingManifest | None = None):
        self.sources = dict(sources)
        self.manifest = manifest or next((source.manifest for source in self.sources.values()
                                          if hasattr(source, "manifest")), None)
        self._entries: dict[str, LedgerEntry] = {}

    def _register(self, entry: LedgerEntry) -> str:
        old = self._entries.get(entry.evidence_id)
        if old is not None and old.content_hash != entry.content_hash:
            raise EvidenceConflictError(entry.evidence_id)
        if old is None:
            self._entries[entry.evidence_id] = entry
        return entry.evidence_id

    def register_chunk(self, evidence: Evidence) -> str:
        return self._register(LedgerEntry(evidence_id=evidence.evidence_id, kind="chunk",
                                          content_hash=evidence.content_hash, ticker=evidence.ticker,
                                          cik=evidence.cik, accession_number=evidence.accession_number))

    def register_fact(self, fact: FinancialFact) -> str:
        candidate_id = fact_id(fact)
        already_registered = candidate_id in self._entries
        evidence_id = self._register(LedgerEntry(evidence_id=candidate_id, kind="fact",
                                          content_hash=fact_content_hash(fact), ticker=fact.ticker,
                                          cik=fact.cik, accession_number=fact.accession_number))
        source = self.sources["fact"]
        if not already_registered and hasattr(source, "remember"):
            source.remember(evidence_id, fact)
        return evidence_id

    def register_metric(self, output: CalculateFinancialMetricOutput) -> str:
        evidence_id = metric_id(output)
        if output.metric_evidence_id and output.metric_evidence_id != evidence_id:
            raise EvidenceConflictError(output.metric_evidence_id)
        already_registered = evidence_id in self._entries
        self._register(LedgerEntry(evidence_id=evidence_id, kind="metric",
                                   content_hash=metric_content_hash(output.result), ticker=output.ticker,
                                   cik=output.cik, accession_number=output.accession_number))
        source = self.sources["metric"]
        if not already_registered:
            source.remember(evidence_id, output)
        return evidence_id

    def register_output_facts(self, output: CalculateFinancialMetricOutput) -> list[str]:
        """Register every input fact for a successfully computed metric."""
        return [self.register_fact(fact) for fact in output.result.inputs]

    def entry(self, evidence_id: str) -> LedgerEntry:
        try:
            return self._entries[evidence_id]
        except KeyError as exc:
            raise EvidenceNotFoundError(evidence_id) from exc

    def resolve(self, evidence_id: str) -> ResolvedEvidence:
        entry = self.entry(evidence_id)
        try:
            resolved = self.sources[entry.kind].load(entry)
        except CompanyFactsSnapshotMismatchError as exc:
            raise EvidenceHashMismatchError(evidence_id) from exc
        if resolved.observed_hash != entry.content_hash:
            raise EvidenceHashMismatchError(evidence_id)
        return resolved

    def __contains__(self, evidence_id: str) -> bool:
        return evidence_id in self._entries


def build_repository_ledger(manifest: FilingManifest, *, normalized_dir: Path, data_dir: Path,
                            provider=None) -> EvidenceLedger:
    """Build the process-local ledger against the pinned manifest and repo caches."""
    sources: dict[str, EvidenceSource] = {
        "chunk": CanonicalChunkSource(normalized_dir),
        "fact": CompanyFactsFactSource(manifest, data_dir=data_dir, provider=provider),
        "metric": MetricRecomputeSource(manifest, data_dir=data_dir, provider=provider),
    }
    return EvidenceLedger(sources, manifest=manifest)
