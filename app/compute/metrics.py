"""Deterministic metric engine (the core of `calculate_financial_metric`).

Fixed formulas keyed by a `Metric` enum — no expression strings, no `eval`. All
arithmetic uses a local `decimal.Context(prec=28, rounding=ROUND_HALF_EVEN)`;
the global Decimal context is never mutated, and the value path never touches
`float`. `result` is unrounded; rounding exists only in `display_value`.
"""

from datetime import date
from decimal import Context, Decimal, ROUND_HALF_EVEN
from enum import Enum

from pydantic import BaseModel

from app.compute.fact_selection import FactSelection, select_fact
from app.compute.tag_aliases import Concept
from app.domain.errors import (NegativeBaseError, PeriodMismatchError, UnsupportedMetricError,
                               UnitMismatchError, ZeroDenominatorError)
from app.domain.financial_facts import FinancialFact, fact_id
from app.ingestion.manifest import ManifestEntry

_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN)
_MILLION = Decimal(1_000_000)


class Metric(str, Enum):
    REVENUE = "revenue"
    REVENUE_GROWTH_YOY = "revenue_growth_yoy"
    OPERATING_MARGIN = "operating_margin"
    NET_MARGIN = "net_margin"
    FREE_CASH_FLOW = "free_cash_flow"
    CURRENT_RATIO = "current_ratio"


class MetricResult(BaseModel):
    metric: str
    formula: str
    inputs: list[FinancialFact]
    result: Decimal
    result_unit: str
    display_value: str
    period_start: date | None
    period_end: date
    evidence_ids: list[str]              # fact_id(...) of each input, same order as inputs


METRIC_FORMULAS: dict[Metric, str] = {
    Metric.REVENUE: "revenue",
    Metric.REVENUE_GROWTH_YOY: "(revenue[FY] - revenue[FY-1]) / revenue[FY-1]",
    Metric.OPERATING_MARGIN: "operating_income / revenue",
    Metric.NET_MARGIN: "net_income / revenue",
    Metric.FREE_CASH_FLOW: "operating_cash_flow - capex",
    Metric.CURRENT_RATIO: "current_assets / current_liabilities",
}


def _display_usd(value: Decimal) -> str:
    """USD in millions, half-even: `"$331,839 million"`; non-whole millions get one decimal."""
    millions = _CTX.divide(value, _MILLION)
    if millions == millions.to_integral_value():
        shown = millions.quantize(Decimal(1), rounding=ROUND_HALF_EVEN, context=_CTX)
    else:
        shown = millions.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN, context=_CTX)
    sign = "-" if shown < 0 else ""
    return f"{sign}${abs(shown):,} million"


def _display_percent(value: Decimal) -> str:
    """Ratios as percent with one decimal, half-even: `"34.5%"`."""
    shown = _CTX.multiply(value, Decimal(100)).quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN, context=_CTX)
    if shown == 0:
        shown = shown.copy_abs()
    sign = "-" if shown < 0 else ""
    return f"{sign}{abs(shown):,}%"


def _display_multiple(value: Decimal) -> str:
    """Current ratio with two decimals, half-even: `"1.35x"`."""
    shown = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN, context=_CTX)
    if shown == 0:
        shown = shown.copy_abs()
    sign = "-" if shown < 0 else ""
    return f"{sign}{abs(shown):,}x"


def _check_same_units(metric: Metric, inputs: list[FinancialFact]) -> None:
    units = [f.unit for f in inputs]
    if len(set(units)) > 1:
        raise UnitMismatchError(units)


def _check_same_period(metric: Metric, inputs: list[FinancialFact]) -> None:
    periods = [(f.period_start, f.period_end) for f in inputs]
    if len(set(periods)) > 1:
        raise PeriodMismatchError([f"{s} -> {e}" if s else f"{e} (instant)" for s, e in periods])


def _check_positive(metric: Metric, base: str, value: Decimal) -> None:
    if value == 0:
        raise ZeroDenominatorError(metric.value, base)
    if value < 0:
        raise NegativeBaseError(metric.value, base, value)


def _result(metric: Metric, inputs: list[FinancialFact], selection: FactSelection,
            result: Decimal, result_unit: str) -> MetricResult:
    display = _display_usd(result) if result_unit == "USD" else (
        _display_multiple(result) if metric is Metric.CURRENT_RATIO else _display_percent(result))
    return MetricResult(metric=metric.value, formula=METRIC_FORMULAS[metric], inputs=inputs,
                        result=result, result_unit=result_unit, display_value=display,
                        period_start=selection.period_start, period_end=selection.period_end,
                        evidence_ids=[fact_id(f) for f in inputs])


def calculate_metric(metric: Metric, *, ticker: str, fiscal_year: int, facts: list[FinancialFact],
                     pinned: ManifestEntry, cutoff_date: date | None = None) -> MetricResult:
    """Compute a metric for (ticker, fiscal_year) from facts of the pinned accession only."""
    if not isinstance(metric, Metric):
        raise UnsupportedMetricError(metric)

    def _select(concept: Concept, year: int = fiscal_year) -> FactSelection:
        return select_fact(facts, concept, ticker=ticker, fiscal_year=year, pinned=pinned, cutoff_date=cutoff_date)

    if metric is Metric.REVENUE:
        selection = _select(Concept.REVENUE)
        return _result(metric, [selection.selected], selection, selection.selected.value, "USD")

    if metric is Metric.REVENUE_GROWTH_YOY:
        current = _select(Concept.REVENUE)
        prior = _select(Concept.REVENUE, fiscal_year - 1)
        inputs = [current.selected, prior.selected]
        _check_same_units(metric, inputs)
        _check_positive(metric, "revenue[FY-1]", prior.selected.value)
        growth = _CTX.divide(_CTX.subtract(current.selected.value, prior.selected.value), prior.selected.value)
        return _result(metric, inputs, current, growth, "ratio")

    if metric is Metric.OPERATING_MARGIN:
        return _ratio_metric(metric, _select(Concept.OPERATING_INCOME), _select(Concept.REVENUE), "revenue")

    if metric is Metric.NET_MARGIN:
        return _ratio_metric(metric, _select(Concept.NET_INCOME), _select(Concept.REVENUE), "revenue")

    if metric is Metric.FREE_CASH_FLOW:
        operating_cash_flow = _select(Concept.OPERATING_CASH_FLOW)
        capex = _select(Concept.CAPEX)
        inputs = [operating_cash_flow.selected, capex.selected]
        _check_same_units(metric, inputs)
        _check_same_period(metric, inputs)
        free_cash_flow = _CTX.subtract(operating_cash_flow.selected.value, capex.selected.value)
        return _result(metric, inputs, operating_cash_flow, free_cash_flow, "USD")

    if metric is Metric.CURRENT_RATIO:
        assets = _select(Concept.CURRENT_ASSETS)
        liabilities = _select(Concept.CURRENT_LIABILITIES)
        inputs = [assets.selected, liabilities.selected]
        _check_same_units(metric, inputs)
        _check_same_period(metric, inputs)
        _check_positive(metric, "current_liabilities", liabilities.selected.value)
        ratio = _CTX.divide(assets.selected.value, liabilities.selected.value)
        return _result(metric, inputs, assets, ratio, "ratio")

    raise UnsupportedMetricError(metric)


def _ratio_metric(metric: Metric, numerator: FactSelection, denominator: FactSelection, base: str) -> MetricResult:
    inputs = [numerator.selected, denominator.selected]
    _check_same_units(metric, inputs)
    _check_same_period(metric, inputs)
    _check_positive(metric, base, denominator.selected.value)
    ratio = _CTX.divide(numerator.selected.value, denominator.selected.value)
    return _result(metric, inputs, numerator, ratio, "ratio")
