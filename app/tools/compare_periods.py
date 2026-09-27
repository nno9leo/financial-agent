from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.compute.metrics import Metric, MetricResult
from app.domain.errors import ResearchError
from app.ingestion.manifest import FilingManifest
from app.tools.calculate_financial_metric import calculate_financial_metric
from app.tools.calculate_financial_metric import CalculateFinancialMetricOutput

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)


class ComparePeriodsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1)
    fiscal_year_a: int
    fiscal_year_b: int
    metrics: list[Metric] = Field(min_length=1, max_length=6)
    accession_number: str | None = None

    @model_validator(mode="after")
    def validate_years_and_metrics(self):
        if self.fiscal_year_a == self.fiscal_year_b: raise ValueError("fiscal years must differ")
        if len(self.metrics) != len(set(self.metrics)): raise ValueError("metrics must be unique")
        return self


class CompareError(BaseModel):
    code: str
    message: str


class CompareRow(BaseModel):
    metric: str
    formula: str
    result_unit: str
    value_a: MetricResult | None
    value_b: MetricResult | None
    absolute_change: Decimal | None
    percentage_change: Decimal | None
    percentage_change_reason: str | None = None
    percentage_change_error: CompareError | None = None
    change_display: str | None
    error: CompareError | None
    metric_evidence_id_a: str | None = None
    metric_evidence_id_b: str | None = None


class ComparePeriodsOutput(BaseModel):
    company: str
    accession_number: str
    fiscal_year_a: int
    fiscal_year_b: int
    rows: list[CompareRow]


def compare_periods(request: ComparePeriodsInput | dict, *, provider=None, manifest: FilingManifest | None = None,
                    manifests_dir: Path | None = None, return_outputs: bool = False):
    args = request if isinstance(request, ComparePeriodsInput) else ComparePeriodsInput.model_validate(request)
    rows = []
    successful_outputs: list[CalculateFinancialMetricOutput] = []
    accession = args.accession_number
    for metric in args.metrics:
        values, outputs, errors = [], [], []
        for year in (args.fiscal_year_a, args.fiscal_year_b):
            try:
                output = calculate_financial_metric({"metric": metric.value, "company": args.company,
                    "fiscal_year": year, "accession_number": accession}, provider=provider,
                    manifest=manifest, manifests_dir=manifests_dir)
                if accession is None: accession = output.accession_number
                elif accession != output.accession_number:
                    raise ResearchError("accession_mismatch", "Metric results resolved to different pinned accessions.")
                values.append(output.result); errors.append(None)
                outputs.append(output)
                successful_outputs.append(output)
            except ResearchError as exc:
                values.append(None); outputs.append(None); errors.append(CompareError(code=exc.code, message=exc.message))
        formula = values[0].formula if values[0] else values[1].formula if values[1] else metric.value
        unit = values[0].result_unit if values[0] else values[1].result_unit if values[1] else ("USD" if metric in (Metric.REVENUE, Metric.FREE_CASH_FLOW) else "ratio")
        if errors[0] or errors[1]:
            error = errors[0] or errors[1]
            rows.append(CompareRow(metric=metric.value, formula=formula, result_unit=unit, value_a=values[0], value_b=values[1],
                absolute_change=None, percentage_change=None, percentage_change_reason=None, change_display=None, error=error,
                metric_evidence_id_a=outputs[0].metric_evidence_id if outputs[0] else None,
                metric_evidence_id_b=outputs[1].metric_evidence_id if outputs[1] else None))
            continue
        a, b = values
        with localcontext(_CTX): absolute = b.result - a.result
        pct, reason, pct_error = None, None, None
        if unit == "USD":
            if a.result > 0:
                with localcontext(_CTX): pct = absolute / abs(a.result)
            else:
                reason = "percentage change requires a positive year A value"
                pct_error = CompareError(code="non_positive_percentage_base", message=reason)
        display = _format_change(absolute, pct, unit, metric)
        rows.append(CompareRow(metric=metric.value, formula=formula, result_unit=unit, value_a=a, value_b=b,
            absolute_change=absolute, percentage_change=pct, percentage_change_reason=reason, percentage_change_error=pct_error,
            change_display=display, error=None, metric_evidence_id_a=outputs[0].metric_evidence_id,
            metric_evidence_id_b=outputs[1].metric_evidence_id))
    result = ComparePeriodsOutput(company=args.company, accession_number=accession or "", fiscal_year_a=args.fiscal_year_a,
                                  fiscal_year_b=args.fiscal_year_b, rows=rows)
    return (result, successful_outputs) if return_outputs else result


def _format_change(change: Decimal, pct: Decimal | None, unit: str, metric: Metric) -> str:
    sign = "+" if change >= 0 else "-"
    magnitude = abs(change)
    if unit == "USD":
        millions = magnitude / Decimal(1_000_000)
        shown = millions.quantize(Decimal(1), rounding=ROUND_HALF_EVEN) if millions == millions.to_integral_value() else millions.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
        pct_text = f" ({'+' if pct is not None and pct >= 0 else ''}{(pct * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_EVEN)}%)" if pct is not None else ""
        return f"{sign}${shown:,} million{pct_text}"
    if metric is Metric.CURRENT_RATIO:
        return f"{sign}{magnitude.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)}x"
    pp = (magnitude * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
    return f"{sign}{pp} pp"
