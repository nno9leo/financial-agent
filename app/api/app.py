import json
import os
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings
from app.domain.errors import (AccessionCompanyMismatchError, AccessionNotInManifestError,
    AmbiguousFactError, AmbiguousManifestCompanyError, AmbiguousSectionError,
    CacheIntegrityError, CompanyFactsNotCachedError, CompanyFactsSnapshotMismatchError,
    CompanyNotFoundError, CompanyNotInManifestError, ConfigurationError, EmbeddingModelError,
    EmbeddingModelMismatchError, FilingNotFoundError, FilingRangeUnavailableError,
    FiscalCalendarMismatchError, FiscalYearNotInFilingError, ManifestNotFoundError,
    MissingFactError, NegativeBaseError, NonAnnualPeriodError, NotIngestedError,
    PeriodMismatchError, ResearchError, SecHttpError, SecPayloadError, SecRateLimitedError,
    SecTimeoutError, SectionNotFoundError, UnitMismatchError, UnsupportedMetricError,
    ZeroDenominatorError, UnknownTickerError, CompanyFactsSnapshotUnchangedError,
    IngestCountMismatchError, ChunkTooLongError)
from app.domain.errors import (EmptyClaimSetError, EvidenceConflictError, EvidenceHashMismatchError,
    EvidenceNotFoundError, InvalidClaimError, JudgeNotScriptedError, JudgeError, JudgeAuthError,
    JudgeUnavailableError, JudgeModelMismatchError, JudgeRouteMismatchError, JudgeSchemaError,
    JudgePromptTooLargeError, RetryBudgetExhaustedError, RetryParentDraftError)
from app.domain.errors import (ReportDraftNotFoundError, ReportPathError, ReportAlreadyExistsError,
    UnsupportedClaimsRequireAcknowledgementError, ReportRenderMismatchError,
    ReportExportRequiresPreviewError, EvidenceLedgerUnavailableError)
from app.citations.ledger import EvidenceLedger, build_repository_ledger
from app.citations.openrouter_judge import OpenRouterJudge, load_judge_config
from app.ingestion.manifest import latest_manifest_path, load_manifest
from app.providers.sec import SecProvider
from app.retrieval.embeddings import FastEmbedEmbedder
from app.retrieval.repository import Repository
from app.tools.calculate_financial_metric import calculate_financial_metric
from app.tools.compare_periods import compare_periods
from app.tools.get_filing_section import get_filing_section
from app.tools.get_filing_section import FilingBuildOutputError, FilingSectionIntegrityError
from app.tools.list_filings import list_filings
from app.tools.registry import TOOL_REGISTRY, get_tool
from app.tools.verify_citations import verify_citations
from app.tools.export_research_report import (preview_export, write_export,
    ExportResearchReportWriteInput)
from app.tools.search_companies import (CompanyTickersCacheError, CompanyTickersPayloadError,
                                       search_companies)
from app.tools.search_filing_chunks import SearchFilingChunksTool
from app.tracing.jsonl import JsonlTraceWriter, sanitize


@dataclass
class ApiDeps:
    manifest_dir: Path | None = None
    sec_provider_factory: Callable[[], Any] = SecProvider
    search_repository: Any = None
    embedder_factory: Callable[[], Any] | None = None
    # FastEmbed is loaded lazily once and retained for this app's lifetime; don't load it on each search.
    search_embedder: Any = None
    trace_writer: Any = None
    normalized_dir: Path | None = None
    evidence_ledger: EvidenceLedger | None = None
    judge_factory: Callable[[], Any] | None = None
    reports_dir: Path | None = None
    ledger_cache_provider: Any = None
    settings: Settings | None = None
    derived_metric_render_override: str | None = None


_DATA_ERRORS = (CompanyNotFoundError, CompanyNotInManifestError, AccessionNotInManifestError,
    AccessionCompanyMismatchError, AmbiguousManifestCompanyError, SectionNotFoundError,
    AmbiguousSectionError, FiscalYearNotInFilingError, MissingFactError, AmbiguousFactError,
    UnitMismatchError, PeriodMismatchError, NonAnnualPeriodError, ZeroDenominatorError,
    NegativeBaseError, UnsupportedMetricError, FilingNotFoundError)
_INFRA_ERRORS = (ConfigurationError, CacheIntegrityError, CompanyFactsNotCachedError,
    CompanyFactsSnapshotMismatchError, ManifestNotFoundError, NotIngestedError,
    EmbeddingModelMismatchError, EmbeddingModelError, SecHttpError, SecPayloadError,
    SecRateLimitedError, SecTimeoutError, FilingRangeUnavailableError,
    CompanyFactsSnapshotUnchangedError, IngestCountMismatchError, ChunkTooLongError,
    CompanyTickersCacheError, CompanyTickersPayloadError, FilingBuildOutputError,
    FilingSectionIntegrityError)
_DATA_ERRORS = (*_DATA_ERRORS, FiscalCalendarMismatchError, UnknownTickerError)
_DATA_ERRORS = (*_DATA_ERRORS, EvidenceNotFoundError, EvidenceHashMismatchError, EvidenceConflictError,
    InvalidClaimError, EmptyClaimSetError, RetryBudgetExhaustedError, RetryParentDraftError)
_INFRA_ERRORS = (*_INFRA_ERRORS, JudgeNotScriptedError, JudgeError, JudgeAuthError, JudgeUnavailableError,
    JudgeModelMismatchError, JudgeRouteMismatchError, JudgeSchemaError, JudgePromptTooLargeError)
_DATA_ERRORS = (*_DATA_ERRORS, ReportDraftNotFoundError, ReportPathError, ReportAlreadyExistsError,
    UnsupportedClaimsRequireAcknowledgementError, ReportRenderMismatchError)
_INFRA_ERRORS = (*_INFRA_ERRORS, EvidenceLedgerUnavailableError)
_CONFLICT_ERRORS = (ReportExportRequiresPreviewError,)


def create_app(deps: ApiDeps | None = None) -> FastAPI:
    api_deps = deps or ApiDeps()
    if api_deps.settings is None:
        api_deps.settings = Settings(_env_file=None)
    if api_deps.embedder_factory is None:
        api_deps.embedder_factory = lambda: FastEmbedEmbedder(api_deps.settings)
    api = FastAPI(title="Financial Research API")
    if api_deps.trace_writer is None:
        api_deps.trace_writer = JsonlTraceWriter()
    if api_deps.manifest_dir is None:
        api_deps.manifest_dir = Path(__file__).resolve().parents[2] / "data/manifests"
    if api_deps.normalized_dir is None:
        api_deps.normalized_dir = Path(__file__).resolve().parents[2] / "data/normalized"
    if api_deps.reports_dir is None:
        api_deps.reports_dir = (api_deps.settings or Settings(_env_file=None)).reports_dir
    if api_deps.evidence_ledger is None:
        try:
            pinned_manifest = _manifest(api_deps)
            data_dir = (api_deps.settings or Settings(_env_file=None)).data_dir
            cache_provider = api_deps.sec_provider_factory()
            api_deps.evidence_ledger = build_repository_ledger(pinned_manifest,
                normalized_dir=api_deps.normalized_dir, data_dir=data_dir, provider=cache_provider)
            api_deps.ledger_cache_provider = cache_provider
        except ResearchError:
            api_deps.evidence_ledger = None
    elif api_deps.ledger_cache_provider is not None:
        for source in api_deps.evidence_ledger.sources.values():
            if hasattr(source, "provider"):
                source.provider = api_deps.ledger_cache_provider
    if api_deps.judge_factory is None:
        api_deps.judge_factory = lambda: OpenRouterJudge(load_judge_config(), settings=api_deps.settings or Settings(_env_file=None))

    @api.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        args = getattr(request.state, "trace_args", None)
        if args is not None:
            request.state.trace_row = _error_trace_row(request, _tool(request), args, "invalid_input",
                getattr(request.state, "tool_duration", 0), http_status=422)
        else:
            request.state.trace_row = {"ts": datetime.now(timezone.utc).isoformat(),
                "trace_id": request.state.trace_id, "tool": _tool(request),
                "args": sanitize(getattr(request.state, "raw_args", {})), "status": "invalid_input",
                "http_status": 422, "duration_ms": 0, "result_summary": {}, "token_usage": None, "cost": None}
        return _error(422, "invalid_input", "Request input is invalid.", "Check the request fields and types.", _tool(request))

    @api.exception_handler(ResearchError)
    async def research_error(request: Request, exc: ResearchError):
        if isinstance(exc, _CONFLICT_ERRORS):
            code = exc.status
        elif isinstance(exc, _DATA_ERRORS):
            code = 422
        elif isinstance(exc, _INFRA_ERRORS):
            code = 503
        else:
            request.state.internal_error = {"type": type(exc).__name__, "message": str(exc)}
            args = getattr(request.state, "trace_args", None)
            if args is not None:
                request.state.trace_row = _error_trace_row(request, _tool(request), args, "internal_error",
                    getattr(request.state, "tool_duration", 0), http_status=500)
            return _error(500, "internal_error", "An internal error occurred.", "Inspect the trace ID and server logs.", _tool(request))
        args = getattr(request.state, "trace_args", None)
        if args is not None:
            request.state.trace_row = _error_trace_row(request, _tool(request), args, exc.code,
                getattr(request.state, "tool_duration", 0), http_status=code)
        return _error(code, exc.code, exc.message, exc.fallback, _tool(request))

    @api.exception_handler(_RouteError)
    async def route_error(request: Request, exc: _RouteError):
        request.state.trace_row = {"ts": datetime.now(timezone.utc).isoformat(),
            "trace_id": request.state.trace_id, "tool": _tool(request), "args": {}, "status": exc.code,
            "http_status": exc.status, "duration_ms": 0, "result_summary": {}, "token_usage": None, "cost": None}
        return _error(exc.status, exc.code, exc.message, exc.fallback, _tool(request))

    @api.exception_handler(500)
    async def generic_error(request: Request, exc: Exception):
        request.state.internal_error = {"type": type(exc).__name__, "message": str(exc)}
        args = getattr(request.state, "trace_args", None)
        if args is not None:
            request.state.trace_row = _error_trace_row(request, _tool(request), args, "internal_error",
                getattr(request.state, "tool_duration", 0), http_status=500)
        return _error(500, "internal_error", "An internal error occurred.", "Inspect the trace ID and server logs.", _tool(request))

    @api.middleware("http")
    async def trace_post(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
        request.state.trace_id = trace_id
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        if request.method == "POST" and request.url.path.startswith("/tools/"):
            row = getattr(request.state, "trace_row", None) or {
                "ts": datetime.now(timezone.utc).isoformat(),
                "trace_id": trace_id, "tool": request.url.path.rsplit("/", 1)[-1], "args": {},
                "status": "ok" if response.status_code < 400 else "request_failed",
                "http_status": response.status_code, "duration_ms": round((time.perf_counter()-start)*1000, 3),
                "result_summary": {}, "token_usage": None, "cost": None}
            row["http_status"] = response.status_code
            row["duration_ms"] = round((time.perf_counter()-start)*1000, 3)
            internal = getattr(request.state, "internal_error", None)
            if internal: row["internal_error"] = internal
            api_deps.trace_writer.write(row)
        return response

    @api.get("/health")
    def health():
        try:
            manifest_version = _manifest(api_deps).manifest_version
        except ResearchError:
            manifest_version = None
        return {"status": "ok", "manifest_version": manifest_version}

    @api.get("/tools")
    def tools():
        return {"tools": [{"name": t.name, "description": t.description, "status": t.status,
            "input_schema": _inline_schema(t.input_model) if t.input_model else None,
            "output_schema": _inline_schema(t.output_model) if t.output_model else None} for t in TOOL_REGISTRY]}

    @api.get("/config/runtime")
    def runtime_config():
        from app.tools.verify_citations import max_verify_attempts
        config_path = Path(__file__).resolve().parents[2] / "config" / "agent_runtime.json"
        runtime = json.loads(config_path.read_text(encoding="utf-8"))
        configured = runtime.get("experiments", {}).get("derived_metric_render", "v2")
        served = (api_deps.derived_metric_render_override
                  if api_deps.derived_metric_render_override is not None
                  else os.environ.get("FRA_EXPERIMENT_DERIVED_METRIC_RENDER", configured))
        return {"max_verify_attempts": max_verify_attempts(), "derived_metric_render": served}

    @api.post("/tools/{tool_name}")
    async def run_tool(tool_name: str, request: Request):
        spec = get_tool(tool_name)
        if spec is None:
            raise _RouteError("unknown_tool", f"Unknown tool {tool_name!r}.", "Check GET /tools.", 404)
        if spec.status == "pending":
            raise _RouteError("tool_not_implemented", f"Tool {tool_name!r} is pending.", "This tool is not available in this version.", 501)
        if tool_name == "verify_citations" and api_deps.evidence_ledger is None:
            raise EvidenceLedgerUnavailableError()
        if tool_name == "export_research_report":
            raise ReportExportRequiresPreviewError()
        try:
            raw = await request.json()
            request.state.raw_args = raw
            args = spec.input_model.model_validate(raw)
            request.state.trace_args = args
        except (ValueError, TypeError) as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                raise RequestValidationError(exc.errors()) from exc
            raise
        started = time.perf_counter()
        try:
            if tool_name == "verify_citations":
                result = verify_citations(args, ledger=api_deps.evidence_ledger,
                    judge=api_deps.judge_factory(), reports_dir=api_deps.reports_dir)
            elif tool_name == "compare_periods":
                result, side_outputs = _dispatch_compare(args, api_deps)
                if api_deps.evidence_ledger is not None:
                    for side_output in side_outputs:
                        api_deps.evidence_ledger.register_metric(side_output)
                        api_deps.evidence_ledger.register_output_facts(side_output)
            else:
                result = _dispatch(tool_name, args, api_deps)
                if api_deps.evidence_ledger is not None:
                    _register_successful_output(tool_name, result, api_deps.evidence_ledger)
            output = spec.output_model.model_validate(result.model_dump(mode="python") if isinstance(result, BaseModel) else result)
        except ResearchError as exc:
            request.state.tool_duration = time.perf_counter()-started
            raise
        except Exception as exc:
            request.state.internal_error = {"type": type(exc).__name__, "message": str(exc)}
            request.state.tool_duration = time.perf_counter()-started
            raise
        request.state.trace_row = _trace_row(request, tool_name, args, output, time.perf_counter()-started)
        return output

    api.state.api_deps = api_deps
    @api.post("/tools/export_research_report/preview")
    async def export_preview(request: Request):
        try:
            raw = await request.json()
            args = get_tool("export_research_report").input_model.model_validate(raw)
            request.state.trace_args = args
            result = preview_export(args, reports_dir=api_deps.reports_dir)
            request.state.trace_row = _trace_row(request, "export_research_report_preview", args, result, 0)
            return result
        except ResearchError:
            raise
        except (ValueError, TypeError) as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError): raise RequestValidationError(exc.errors()) from exc
            raise
    @api.post("/tools/export_research_report/write")
    async def export_write(request: Request):
        raw = await request.json()
        try:
            args = ExportResearchReportWriteInput.model_validate(raw)
        except Exception as exc:
            from pydantic import ValidationError
            if isinstance(exc, ValidationError): raise RequestValidationError(exc.errors()) from exc
            raise
        request.state.trace_args = args
        result = write_export(args, reports_dir=api_deps.reports_dir,
                              expected_content_sha256=args.preview_content_sha256)
        request.state.trace_row = _trace_row(request, "export_research_report_write", args, result, 0)
        return result
    return api


class _RouteError(Exception):
    def __init__(self, code, message, fallback, status): self.code,self.message,self.fallback,self.status=code,message,fallback,status


def _error(status, code, message, fallback, tool):
    return JSONResponse(status_code=status, content=jsonable_encoder({"error":{"code":code,"message":message,"fallback":fallback,"tool":tool}}))


def _inline_schema(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    def expand(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                replacement = expand(definitions[name])
                replacement.update({k: expand(v) for k, v in value.items() if k != "$ref"})
                return replacement
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list): return [expand(item) for item in value]
        return value
    return expand(schema)


def _tool(request): return request.url.path.rsplit("/",1)[-1] if request.url.path.startswith("/tools/") else None


def _manifest(deps):
    directory = deps.manifest_dir or Path(__file__).resolve().parents[2] / "data/manifests"
    return load_manifest(latest_manifest_path(Path(directory)))


def _dispatch(name, args, deps):
    manifest = _manifest(deps)
    provider = deps.sec_provider_factory()
    owns_provider = True
    try:
        if name == "search_companies": return search_companies(args, provider=provider, manifest=manifest, manifests_dir=deps.manifest_dir)
        if name == "list_filings": return list_filings(args, provider=provider)
        if name == "get_filing_section": return get_filing_section(args, manifest=manifest, normalized_dir=deps.normalized_dir)
        if name == "search_filing_chunks":
            repo = deps.search_repository
            if repo is None:
                repo = Repository(deps.settings.database_url)
            embedder = deps.search_embedder
            if embedder is None:
                embedder = deps.embedder_factory()
                deps.search_embedder = embedder
            return SearchFilingChunksTool(repo, embedder)(args)
        if name == "calculate_financial_metric":
            return calculate_financial_metric(args, provider=provider, manifest=manifest, manifests_dir=deps.manifest_dir)
    finally:
        if owns_provider and hasattr(provider, "close"): provider.close()
    raise RuntimeError(f"No handler for {name}")


def _dispatch_compare(args, deps):
    manifest = _manifest(deps)
    provider = deps.sec_provider_factory()
    try:
        return compare_periods(args, provider=provider, manifest=manifest,
                               manifests_dir=deps.manifest_dir, return_outputs=True)
    finally:
        if hasattr(provider, "close"):
            provider.close()


def _register_successful_output(name, result, ledger):
    if name == "search_filing_chunks":
        for evidence in result.evidence:
            ledger.register_chunk(evidence)
    elif name == "calculate_financial_metric":
        ledger.register_metric(result)
        ledger.register_output_facts(result)
    elif name == "compare_periods":
        raise AssertionError("compare_periods registration is handled with its per-side outputs")


def _trace_row(request, name, args, output, duration):
    raw = output.model_dump(mode="json")
    if name == "get_filing_section": summary = {k: raw[k] for k in ("ticker", "accession_number", "section_sha256", "returned_chars", "next_offset")}
    elif name == "search_filing_chunks": summary = {"evidence_ids": [e["evidence_id"] for e in raw.get("evidence", [])]}
    elif name == "calculate_financial_metric": summary = {"metric": raw["result"]["metric"], "display_value": raw["result"]["display_value"], "evidence_ids": raw["result"]["evidence_ids"]}
    elif name == "compare_periods": summary = {"rows": [{"metric": r["metric"], "change_display": r["change_display"], "error": r["error"]} for r in raw["rows"]]}
    else: summary = {"count": len(raw.get("candidates", raw.get("filings", [])))}
    return {"ts": datetime.now(timezone.utc).isoformat(),
        "trace_id": request.state.trace_id, "tool": name, "args": sanitize(args.model_dump(mode="json")),
        "status": "ok", "http_status": 200, "duration_ms": round(duration*1000, 3),
        "result_summary": summary, "token_usage": None, "cost": None}


def _error_trace_row(request, name, args, status, duration, http_status=None):
    row = {"ts": datetime.now(timezone.utc).isoformat(),
        "trace_id": request.state.trace_id, "tool": name, "args": sanitize(args.model_dump(mode="json")),
        "status": status, "http_status": http_status if http_status is not None else (500 if status == "internal_error" else 503),
        "duration_ms": round(duration*1000, 3), "result_summary": {}, "token_usage": None, "cost": None}
    if status == "internal_error": row["internal_error"] = request.state.internal_error
    return row
