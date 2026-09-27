class ResearchError(Exception):
    def __init__(self, code: str, message: str, fallback: str | None = None):
        self.code, self.message, self.fallback = code, message, fallback
        super().__init__(message)


class EvidenceNotFoundError(ResearchError):
    def __init__(self, evidence_id: str):
        super().__init__("evidence_not_found", f"Evidence {evidence_id!r} is not registered or absent from its source.",
                         "Retrieve and register evidence from the pinned source again.")


class EvidenceHashMismatchError(ResearchError):
    def __init__(self, evidence_id: str):
        super().__init__("hash_mismatch", f"Evidence {evidence_id!r} changed since registration.",
                         "Discard the old citation and register the current pinned content.")


class EvidenceConflictError(ResearchError):
    def __init__(self, evidence_id: str):
        super().__init__("evidence_conflict", f"Evidence {evidence_id!r} was registered with a different hash.",
                         "Resolve the conflicting source before registering it.")


class JudgeNotScriptedError(ResearchError):
    def __init__(self, claim_text: str):
        super().__init__("judge_not_scripted", f"No fake-judge response is scripted for {claim_text!r}.",
                         "Add an explicit scripted result for this claim and evidence set.")


class InvalidClaimError(ResearchError):
    def __init__(self, message: str):
        super().__init__("invalid_claim", message, "Supply the required assertion with a valid unit and scale.")


class EmptyClaimSetError(ResearchError):
    def __init__(self):
        super().__init__("empty_claim_set", "Citation verification requires at least one claim.",
                         "Provide one or more structured claims.")


class RetryBudgetExhaustedError(ResearchError):
    def __init__(self, attempt: int, max_attempts: int):
        super().__init__("retry_budget_exhausted",
                         f"Verification attempt {attempt} exceeds the configured maximum of {max_attempts}.",
                         "Present the latest final_answer_markdown without another evidence retry.")


class RetryParentDraftError(ResearchError):
    def __init__(self, draft_id: str | None, reason: str):
        super().__init__("retry_parent_invalid",
                         f"Retry parent draft {draft_id!r} is invalid: {reason}",
                         "Use the report_draft_id from the preceding verification for the same ticker.")


class ReportDraftNotFoundError(ResearchError):
    def __init__(self, draft_id: str):
        super().__init__("report_draft_not_found", f"Report draft {draft_id!r} does not exist.", "Verify the draft ID returned by verify_citations.")


class ReportPathError(ResearchError):
    def __init__(self, message: str = "Report output path must be a safe bare filename with an extension matching the format."):
        super().__init__("invalid_report_path", message, "Choose a new .md or .html filename without directories.")


class ReportAlreadyExistsError(ResearchError):
    def __init__(self, path: str):
        super().__init__("report_already_exists", f"Report output already exists: {path}.", "Choose a different output filename; reports are never overwritten.")


class UnsupportedClaimsRequireAcknowledgementError(ResearchError):
    def __init__(self):
        super().__init__("unsupported_claims_require_acknowledgement", "Unsupported claims must be acknowledged before export.", "Preview the report and confirm the removed-claims appendix before writing.")


class ReportRenderMismatchError(ResearchError):
    def __init__(self):
        super().__init__("report_render_mismatch", "Rendered report differs from its preview.", "Regenerate the preview and confirm before writing.")


class ReportExportRequiresPreviewError(ResearchError):
    def __init__(self):
        self.status = 409
        super().__init__("report_export_requires_preview", "Direct export is disabled; use preview then write.", "Call /tools/export_research_report/preview, obtain user confirmation, then call /write.")


class EvidenceLedgerUnavailableError(ResearchError):
    def __init__(self):
        super().__init__("evidence_ledger_unavailable", "Evidence ledger is not configured.", "Configure the pinned evidence sources and restart the API.")


class CompanyNotFoundError(ResearchError):
    def __init__(self, company: str):
        super().__init__("company_not_found", f"No SEC company matches {company!r}.", "Check the ticker or CIK.")


class SecHttpError(ResearchError):
    def __init__(self, status_code: int, url: str):
        self.status_code, self.url = status_code, url
        super().__init__("sec_http_error", f"SEC returned HTTP {status_code} for {url}.", "Retry later or inspect the SEC endpoint.")


class SecTimeoutError(ResearchError):
    def __init__(self, url: str):
        self.url = url
        super().__init__("sec_timeout", f"SEC request timed out for {url}.", "Retry later.")


class SecRateLimitedError(ResearchError):
    def __init__(self, status_code: int, url: str):
        self.status_code, self.url = status_code, url
        super().__init__("sec_rate_limited", f"SEC rate limited request ({status_code}) for {url}.", "Wait before retrying.")


class FilingNotFoundError(ResearchError):
    def __init__(self, message: str):
        super().__init__("filing_not_found", message, "Adjust the filing form or date range.")


class CacheIntegrityError(ResearchError):
    def __init__(self, url: str):
        self.url = url
        super().__init__("cache_integrity_error", f"Cached bytes failed SHA-256 verification for {url}.")


class ConfigurationError(ResearchError):
    def __init__(self, message: str):
        super().__init__("configuration_error", message)


class JudgeError(ResearchError):
    def __init__(self, code: str, message: str, fallback: str):
        super().__init__(code, message, fallback)


class JudgeAuthError(JudgeError):
    def __init__(self): super().__init__("judge_auth_error", "OpenRouter rejected the judge credentials.", "Check OPENROUTER_API_KEY configuration.")


class JudgeUnavailableError(JudgeError):
    def __init__(self): super().__init__("judge_unavailable", "The semantic judge is temporarily unavailable.", "Retry the verification later.")


class JudgeModelMismatchError(JudgeError):
    def __init__(self): super().__init__("judge_model_mismatch", "The semantic judge returned an unexpected model.", "Check the configured judge model and route.")


class JudgeRouteMismatchError(JudgeError):
    def __init__(self): super().__init__("judge_route_mismatch", "The semantic judge used an unexpected provider.", "Check the pinned provider route.")


class JudgeSchemaError(JudgeError):
    def __init__(self): super().__init__("judge_schema_error", "The semantic judge returned an invalid or incomplete result.", "Retry or inspect the judge response format.")


class JudgePromptTooLargeError(JudgeError):
    def __init__(self): super().__init__("judge_prompt_too_large", "The semantic judge prompt exceeds the 20000 character limit.", "Reduce the claim or evidence size.")


class FilingRangeUnavailableError(ResearchError):
    def __init__(self, message: str):
        super().__init__("filing_range_unavailable", message, "Use a date range within submissions.recent.")


class SecPayloadError(ResearchError):
    def __init__(self, url: str, reason: str):
        self.url = url
        super().__init__("sec_invalid_payload", f"SEC returned an invalid payload for {url}: {reason}", "Retry later or inspect the SEC response format.")


class CompanyFactsSnapshotMismatchError(ResearchError):
    def __init__(self, ticker: str, pinned_hash: str, observed_hash: str):
        self.ticker = ticker
        self.pinned_hash = pinned_hash
        self.observed_hash = observed_hash
        super().__init__("companyfacts_snapshot_mismatch",
                         f"companyfacts snapshot mismatch for {ticker}: pinned sha256={pinned_hash}, observed sha256={observed_hash}. Use --accept-new-companyfacts-snapshot to record a new version.")


class CompanyFactsNotCachedError(ResearchError):
    def __init__(self, url: str):
        self.url = url
        super().__init__("companyfacts_not_cached", f"Pinned companyfacts are not cached: {url}.",
                         "Populate the SEC cache before calculating metrics; this tool never fetches companyfacts.")


class ManifestNotFoundError(ResearchError):
    def __init__(self):
        super().__init__("manifest_not_found", "Cannot refresh companyfacts because no versioned manifest exists.", "Create an initial manifest before refreshing its snapshot.")


class CompanyFactsSnapshotUnchangedError(ResearchError):
    def __init__(self):
        super().__init__("companyfacts_snapshot_unchanged", "The companyfacts snapshot hashes match the latest manifest; no new version was written.")


class SectionNotFoundError(ResearchError):
    def __init__(self, section: str, candidates: list[dict]):
        self.section, self.candidates = section, candidates
        super().__init__("section_not_found", f"Required section {section!r} was not found; candidates: {candidates!r}.")


class AmbiguousSectionError(ResearchError):
    def __init__(self, section: str, candidates: list[dict]):
        self.section, self.candidates = section, candidates
        super().__init__("ambiguous_section", f"Required section {section!r} has multiple heading candidates: {candidates!r}.")


class ChunkTooLongError(ResearchError):
    def __init__(self, token_count: int, max_tokens: int):
        self.token_count, self.max_tokens = token_count, max_tokens
        super().__init__("chunk_too_long", f"Embedding input has {token_count} tokens, exceeding the {max_tokens}-token maximum.")


class EmbeddingModelError(ResearchError):
    def __init__(self, message: str):
        super().__init__("embedding_model_error", message)


class AccessionNotInManifestError(ResearchError):
    def __init__(self, accession_number: str):
        self.accession_number = accession_number
        super().__init__("accession_not_in_manifest", f"Accession {accession_number!r} is not present in the latest pinned manifest.")


class AccessionCompanyMismatchError(ResearchError):
    def __init__(self, accession_number: str, company: str):
        self.accession_number, self.company = accession_number, company
        super().__init__("accession_company_mismatch", f"Accession {accession_number!r} does not belong to company {company!r} in the latest manifest.")


class CompanyNotInManifestError(ResearchError):
    def __init__(self, company: str):
        self.company = company
        super().__init__("company_not_in_manifest", f"Company {company!r} is not present in the latest pinned manifest.", "Check the ticker or CIK against the latest pinned manifest.")


class AmbiguousManifestCompanyError(ResearchError):
    def __init__(self, company: str, accessions: list[str]):
        self.company, self.accessions = company, accessions
        super().__init__("ambiguous_manifest_company", f"Company {company!r} has multiple pinned accessions: {accessions!r}. Specify accession_number explicitly.")


class NotIngestedError(ResearchError):
    def __init__(self, accession_number: str):
        self.accession_number = accession_number
        super().__init__("not_ingested", f"Accession {accession_number!r} has no rows for the current extractor/chunker and model weights.", "Run scripts/ingest_chunks.py.")


class EmbeddingModelMismatchError(ResearchError):
    def __init__(self, expected_hash: str, found_hashes: list[str]):
        self.expected_hash, self.found_hashes = expected_hash, found_hashes
        super().__init__("embedding_model_mismatch", f"No rows match current embedding_model_sha256={expected_hash}; stored hashes for current versions: {found_hashes!r}.", "Run scripts/ingest_chunks.py with the current embedding model.")


class IngestCountMismatchError(ResearchError):
    def __init__(self, accession_number: str, expected: int, observed: int):
        self.accession_number, self.expected, self.observed = accession_number, expected, observed
        super().__init__("ingest_count_mismatch", f"Post-ingest row count for {accession_number} was {observed}; expected exactly {expected}.")


class FiscalYearNotInFilingError(ResearchError):
    def __init__(self, ticker: str, fiscal_year: int, available_years: list[int], period_type: str):
        self.ticker, self.fiscal_year, self.available_years, self.period_type = ticker, fiscal_year, available_years, period_type
        super().__init__("fiscal_year_not_in_filing",
                         f"Fiscal year {fiscal_year} has no {period_type} period in the pinned filing for {ticker}. "
                         f"Fiscal years available in the pinned filing: {available_years}.",
                         "Use a fiscal year listed as available, or pin a filing that contains the requested period.")


class MissingFactError(ResearchError):
    def __init__(self, concept: str, ticker: str, fiscal_year: int, rejection_counts: dict[str, int]):
        self.concept, self.ticker, self.fiscal_year, self.rejection_counts = concept, ticker, fiscal_year, rejection_counts
        super().__init__("missing_fact",
                         f"No reliable fact for concept {concept!r} ({ticker}, fiscal year {fiscal_year}) "
                         f"after filtering all alias tags; rejection counts by reason: {dict(sorted(rejection_counts.items()))}.",
                         "No structured fact is available; use search_filing_chunks over the pinned filing text instead.")


class AmbiguousFactError(ResearchError):
    def __init__(self, message: str, candidates: list):
        self.candidates = candidates
        super().__init__("ambiguous_fact", message,
                         "Disambiguate manually (e.g. by form, period or amendment) before computing; the calculator never guesses.")


class UnitMismatchError(ResearchError):
    def __init__(self, units: list[str]):
        self.units = units
        super().__init__("unit_mismatch", f"Metric inputs have mixed units: {units}.",
                         "Check the selected facts' units; only like units may be combined.")


class PeriodMismatchError(ResearchError):
    def __init__(self, periods: list[str]):
        self.periods = periods
        super().__init__("period_mismatch", f"Metric inputs cover different periods: {periods}.",
                         "Check the selected facts' periods; only facts of the same period may be combined.")


class NonAnnualPeriodError(ResearchError):
    def __init__(self, period_start, period_end, days: int):
        self.period_start, self.period_end, self.days = period_start, period_end, days
        super().__init__("non_annual_period",
                         f"Period {period_start} -> {period_end} spans {days} days, not an annual duration (350-380 days).",
                         "Annual metrics only accept annual durations; quarter facts are recorded as rejections.")


class ZeroDenominatorError(ResearchError):
    def __init__(self, metric: str, denominator: str):
        self.metric, self.denominator = metric, denominator
        super().__init__("zero_denominator", f"Denominator {denominator!r} for metric {metric!r} is zero.",
                         "The metric is undefined; report the missing/zero input explicitly.")


class NegativeBaseError(ResearchError):
    def __init__(self, metric: str, base: str, value):
        self.metric, self.base, self.value = metric, base, value
        super().__init__("negative_base", f"Base {base!r} for metric {metric!r} is negative ({value}); the sign of the result would be meaningless.",
                         "Report the negative base explicitly instead of computing the metric.")


class UnsupportedMetricError(ResearchError):
    def __init__(self, metric):
        from app.compute.metrics import Metric
        self.metric = metric
        valid = ", ".join(m.value for m in Metric)
        super().__init__("unsupported_metric", f"Unknown metric {metric!r}; supported metrics: {valid}.",
                         "Pick a metric from the Metric enum.")


class UnknownTickerError(ResearchError):
    def __init__(self, ticker: str):
        self.ticker = ticker
        super().__init__("unknown_ticker", f"No fiscal-calendar rule is registered for ticker {ticker!r}.",
                         "Add the ticker to FISCAL_YEAR_END_MONTH or use a supported ticker.")


class FiscalCalendarMismatchError(ResearchError):
    def __init__(self, ticker: str, period_end, fiscal_year_end_month: int):
        self.ticker, self.period_end, self.fiscal_year_end_month = ticker, period_end, fiscal_year_end_month
        super().__init__("fiscal_calendar_mismatch",
                         f"Resolved period end {period_end} does not fall within one month of {ticker}'s fiscal year-end month {fiscal_year_end_month}.",
                         "Check the ticker's fiscal calendar in FISCAL_YEAR_END_MONTH.")


class CompanyFactsNotCachedError(ResearchError):
    def __init__(self, url: str):
        self.url = url
        super().__init__("companyfacts_not_cached", f"Pinned companyfacts payload is not in the raw cache: {url}.",
                         "Run scripts/ingest.py on the machine that holds the shared cache; this task never fetches.")
