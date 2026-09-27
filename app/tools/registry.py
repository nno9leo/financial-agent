from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel

from app.compute.metrics import MetricResult
from app.domain.filings import FilingMetadata
from app.ingestion.sections import SectionName
from app.retrieval.search import SearchFilingChunksInput, SearchFilingChunksOutput
from app.tools.calculate_financial_metric import CalculateFinancialMetricInput, CalculateFinancialMetricOutput
from app.tools.compare_periods import ComparePeriodsInput, ComparePeriodsOutput
from app.tools.get_filing_section import FilingSectionInput, FilingSectionOutput
from app.tools.list_filings import ListFilingsInput, ListFilingsOutput
from app.tools.search_companies import CompanySearchInput, CompanySearchOutput
from app.tools.verify_citations import VerifyCitationsInput, VerifyCitationsOutput
from app.tools.export_research_report import ExportResearchReportInput, ExportResearchReportPreview


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel] | None
    output_model: type[BaseModel] | None
    status: Literal["implemented", "pending"]
    handler: Callable[..., Any] | None


TOOL_REGISTRY = [
    ToolSpec("search_companies", "Find SEC companies by ticker, CIK, or name using the raw cache; it does not fetch or authorize unpinned companies, and unmatched queries return an explicit error.", CompanySearchInput, CompanySearchOutput, "implemented", None),
    ToolSpec("list_filings", "List SEC filings and exclusions for a company and date range; this does not pin or ingest filings, and unavailable ranges return a typed error.", ListFilingsInput, ListFilingsOutput, "implemented", None),
    ToolSpec("get_filing_section", "Read a paginated section from the pinned filing's verified normalized text; it does not summarize or silently truncate, and missing build output points to scripts/build_chunks.py.", FilingSectionInput, FilingSectionOutput, "implemented", None),
    ToolSpec("search_filing_chunks", "Retrieve ranked evidence from the pinned filing; results are evidence, not answers, and all numbers must come from calculate_financial_metric or compare_periods.", SearchFilingChunksInput, SearchFilingChunksOutput, "implemented", None),
    ToolSpec("calculate_financial_metric", "Compute a supported structured metric from verified companyfacts for the pinned accession; numbers must come from this tool rather than retrieved text, and unavailable facts return typed errors.", CalculateFinancialMetricInput, CalculateFinancialMetricOutput, "implemented", None),
    ToolSpec("compare_periods", "Compare structured metrics across fiscal years within one pinned accession; values are calculated by calculate_financial_metric, never retrieved text, and missing years return typed errors without partial values.", ComparePeriodsInput, ComparePeriodsOutput, "implemented", None),
    ToolSpec("verify_citations", "Checks evidence IDs, hashes, metadata, and numeric assertions deterministically first; narrative claims are judged by the configured judge. Relevance is not support. Unsupported claims must be removed or reworded, never presented as fact. Evidence IDs are process-local and resolve as evidence_not_found after a server restart.", VerifyCitationsInput, VerifyCitationsOutput, "implemented", None),
    ToolSpec("export_research_report", "Preview and write a report containing supported claims and labelled partial claims. Unsupported claims are omitted unless explicitly acknowledged in the Removed appendix. The Pi extension must obtain ctx.ui.confirm() between preview and write; direct calls cannot bypass preview/confirmation.", ExportResearchReportInput, ExportResearchReportPreview, "implemented", None),
]


def get_tool(name: str) -> ToolSpec | None:
    return next((tool for tool in TOOL_REGISTRY if tool.name == name), None)
