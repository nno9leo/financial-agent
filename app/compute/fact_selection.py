"""Deterministic fact selection: explicit, period-aware, and fail-closed on ambiguity.

Pipeline:
1. resolve the target period for (ticker, fiscal_year) from the pinned filing
   only — never from `fy`/`fp`/`frame`, never "closest/latest/largest/first";
2. walk the alias tags in priority order and record a decision for EVERY
   candidate fact, with a machine-readable rejection reason;
3. the first alias tag with survivors wins; identical-value duplicates are
   recorded as `duplicate_of_selected`; any value disagreement (same tag or
   lower-priority alias) raises `AmbiguousFactError` instead of guessing.
"""

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from app.compute.tag_aliases import CONCEPTS, FISCAL_YEAR_END_MONTH, Concept, ConceptSpec
from app.domain.errors import (AmbiguousFactError, FiscalCalendarMismatchError, FiscalYearNotInFilingError,
                               MissingFactError, UnknownTickerError)
from app.domain.financial_facts import FinancialFact, fact_id
from app.ingestion.manifest import ManifestEntry

ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380


class CandidateDecision(BaseModel):
    fact_id: str
    tag: str
    unit: str
    period_start: date | None
    period_end: date
    value: Decimal
    accession_number: str
    form: str
    filed_at: date
    decision: Literal["selected", "duplicate_of_selected", "rejected"]
    reason: str | None                   # machine-readable rejection code, e.g. "other_accession"


class FactSelection(BaseModel):
    concept: Concept
    ticker: str
    fiscal_year: int
    period_start: date | None
    period_end: date
    selected: FinancialFact
    candidates: list[CandidateDecision]  # every fact of every alias tag considered, with its decision


def is_annual_duration(period_start: date | None, period_end: date) -> bool:
    """Annual check for duration facts: 350-380 days (52/53-week years included)."""
    if period_start is None:
        return False
    return ANNUAL_MIN_DAYS <= (period_end - period_start).days <= ANNUAL_MAX_DAYS


def _fiscal_year_end_month(ticker: str) -> int:
    try:
        return FISCAL_YEAR_END_MONTH[ticker]
    except KeyError:
        raise UnknownTickerError(ticker) from None


def _check_month_window(ticker: str, period_end: date, fye_month: int) -> None:
    if (period_end.month - fye_month) % 12 not in (0, 1, 11):
        raise FiscalCalendarMismatchError(ticker, period_end, fye_month)


def _annual_periods(pinned_facts: list[FinancialFact]) -> dict[int, set[tuple[date, date]]]:
    """Distinct annual periods in the pinned filing, keyed by end-year label."""
    periods: dict[int, set[tuple[date, date]]] = {}
    for fact in pinned_facts:
        if is_annual_duration(fact.period_start, fact.period_end):
            periods.setdefault(fact.period_end.year, set()).add((fact.period_start, fact.period_end))
    return periods


def _annual_periods_in_fiscal_window(pinned_facts: list[FinancialFact], fye_month: int) -> dict[int, set[tuple[date, date]]]:
    periods: dict[int, set[tuple[date, date]]] = {}
    for fact in pinned_facts:
        if is_annual_duration(fact.period_start, fact.period_end):
            if (fact.period_end.month - fye_month) % 12 in (0, 1, 11):
                periods.setdefault(fact.period_end.year, set()).add((fact.period_start, fact.period_end))
    return periods


def resolve_fiscal_period(ticker: str, fiscal_year: int, *, period_type: Literal["duration", "instant"],
                          facts: list[FinancialFact], pinned: ManifestEntry) -> tuple[date | None, date]:
    """Resolve the target period of (ticker, fiscal_year) inside the pinned filing.

    - Duration: the unique annual period whose end date falls in that calendar year.
    - Instant: the instant date equal to the end of that year's annual period,
      and only if an instant fact exists there in the pinned filing.
    The fiscal-year label is validated against the ticker's fiscal year-end month.
    """
    fye_month = _fiscal_year_end_month(ticker)
    pinned_facts = [f for f in facts if f.accession_number == pinned.accession_number]
    all_periods = _annual_periods(pinned_facts)
    periods = _annual_periods_in_fiscal_window(pinned_facts, fye_month)

    matching = periods.get(fiscal_year, set())
    if not matching:
        out_of_window = all_periods.get(fiscal_year, set())
        if out_of_window:
            ordered = sorted(out_of_window)
            raise FiscalCalendarMismatchError(ticker, ordered[0][1], fye_month)
        raise FiscalYearNotInFilingError(ticker, fiscal_year, sorted(periods), period_type)
    if len(matching) > 1:
        ordered = sorted(matching)
        raise AmbiguousFactError(
            f"{len(matching)} distinct annual periods end in {fiscal_year} in the pinned filing "
            f"for {ticker}: {[f'{s} -> {e}' for s, e in ordered]}",
            candidates=[{"period_start": s, "period_end": e} for s, e in ordered])
    period_start, period_end = next(iter(matching))
    _check_month_window(ticker, period_end, fye_month)

    if period_type == "duration":
        return period_start, period_end

    instant_dates = {f.period_end for f in pinned_facts if f.period_start is None}
    if period_end not in instant_dates:
        available = sorted({end.year for year_periods in periods.values() for _, end in year_periods
                            if end in instant_dates})
        raise FiscalYearNotInFilingError(ticker, fiscal_year, available, "instant")
    return None, period_end


def _rejection_reason(fact: FinancialFact, spec: ConceptSpec, pinned: ManifestEntry, cutoff: date,
                      target_start: date | None, target_end: date) -> str | None:
    if fact.accession_number != pinned.accession_number:
        return "other_accession"
    if fact.form != "10-K":
        return "amendment_or_wrong_form"
    if fact.filed_at > cutoff:
        return "after_cutoff"
    if fact.unit != spec.unit:
        return "unit_mismatch"
    if spec.period_type == "duration" and fact.period_start is None:
        return "wrong_period_type"
    if spec.period_type == "instant" and fact.period_start is not None:
        return "wrong_period_type"
    if spec.period_type == "duration" and not is_annual_duration(fact.period_start, fact.period_end):
        return "non_annual_duration"
    if spec.period_type == "duration":
        if fact.period_start != target_start or fact.period_end != target_end:
            return "period_mismatch"
    elif fact.period_end != target_end:
        return "period_mismatch"
    return None


def _fact_sort_key(fact: FinancialFact) -> tuple:
    return (fact.unit, fact.period_start or fact.period_end, fact.period_end,
            fact.accession_number, fact.filed_at, fact.value, fact.form)


def select_fact(facts: list[FinancialFact], concept: Concept, *, ticker: str, fiscal_year: int,
                pinned: ManifestEntry, cutoff_date: date | None = None) -> FactSelection:
    """Select the fact for a concept and fiscal year, pinned to one accession.

    Considers every fact of every alias tag (recorded in `candidates`); the first
    alias tag with survivors wins; any remaining ambiguity is a typed error.
    """
    try:
        spec = CONCEPTS[concept]
    except KeyError:
        raise ValueError(f"No ConceptSpec registered for concept {concept!r}") from None
    cutoff = cutoff_date or date.fromisoformat(pinned.filing_date)
    target_start, target_end = resolve_fiscal_period(ticker, fiscal_year, period_type=spec.period_type,
                                                     facts=facts, pinned=pinned)

    entries: list[tuple[FinancialFact, str | None]] = []
    rejection_counts: Counter[str] = Counter()
    survivors: dict[str, list[FinancialFact]] = {}

    for tag in spec.tags:
        tag_survivors: list[FinancialFact] = []
        for fact in sorted((f for f in facts if f.tag == tag), key=_fact_sort_key):
            reason = _rejection_reason(fact, spec, pinned, cutoff, target_start, target_end)
            if reason is None:
                tag_survivors.append(fact)
            else:
                rejection_counts[reason] += 1
            entries.append((fact, reason))
        survivors[tag] = tag_survivors

    winning_tag = next((tag for tag in spec.tags if survivors[tag]), None)
    if winning_tag is None:
        raise MissingFactError(concept.value, ticker, fiscal_year, dict(rejection_counts))

    winners = survivors[winning_tag]
    distinct_values = {f.value for f in winners}
    if len(distinct_values) > 1:
        raise AmbiguousFactError(
            f"Concept {concept.value} ({ticker}, FY{fiscal_year}): {len(winners)} facts of tag "
            f"{winning_tag!r} in the pinned filing have {len(distinct_values)} different values "
            f"for period {target_start} -> {target_end}.",
            candidates=winners)

    selected = winners[0]
    for tag in spec.tags:
        if tag == winning_tag:
            continue
        differing = [f for f in survivors[tag] if f.value != selected.value]
        if differing:
            raise AmbiguousFactError(
                f"Concept {concept.value} ({ticker}, FY{fiscal_year}): alias tag {tag!r} reports a different "
                f"value ({differing[0].value}) than selected tag {winning_tag!r} ({selected.value}) "
                f"for period {target_start} -> {target_end}.",
                candidates=[selected, *differing])

    candidates = []
    for fact, reason in entries:
        if reason is not None:
            decision = "rejected"
        elif fact is selected:
            decision = "selected"
        else:
            decision = "duplicate_of_selected"
        candidates.append(CandidateDecision(
            fact_id=fact_id(fact), tag=fact.tag, unit=fact.unit,
            period_start=fact.period_start, period_end=fact.period_end,
            value=fact.value, accession_number=fact.accession_number,
            form=fact.form, filed_at=fact.filed_at,
            decision=decision, reason=reason))

    return FactSelection(concept=concept, ticker=ticker, fiscal_year=fiscal_year,
                         period_start=target_start, period_end=target_end,
                         selected=selected, candidates=candidates)
