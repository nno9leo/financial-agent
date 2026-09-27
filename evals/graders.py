"""Pure graders and aggregation for normalized eval run records."""
from __future__ import annotations

import re
import statistics
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\$?\s*\d[\d,]*(?:\.\d+)?\s*%?x?(?![A-Za-z])", re.I)
ERROR_CATEGORIES = ("wrong_tool", "no_calculator", "fact_selection", "retrieval_miss", "unsupported_claim",
                    "refusal_missing", "runtime_error", "tool_error", "schema_error")
GRADER_VERSION = "v3"

_MANAGER_REFUSAL_TARGETS = {
    "fail-unpinned-company": ("NVDA", 2025, "revenue"),
    "fail-unavailable-year": ("AAPL", 2023, "revenue_growth_yoy"),
    "fail-unsupported-metric": ("MSFT", 2026, "gross_margin"),
}


def _dec(x: Any) -> Decimal | None:
    try:
        return Decimal(str(x).replace(",", "").replace("$", "").replace("%", "").replace("x", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _tools(record: dict) -> list[str]:
    return [str(t.get("name", "")) for t in record.get("tool_calls", [])]


def _final_report(record: dict) -> dict | None:
    reports = record.get("verify_outputs", [])
    return reports[-1] if reports else None


def _claims(report: dict | None) -> list[dict]:
    if not report:
        return []
    return report.get("report", {}).get("verifications", report.get("verifications", []))


def _numeric_in_answer(record: dict, item: dict) -> bool:
    expected = item.get("expected_answer", {})
    # The record may carry calculator/compare results as structured summaries.
    if expected.get("kind") == "mixed":
        expected = expected.get("numeric", {})
    if expected.get("kind") == "comparison":
        expected_values = expected.get("values", [])
        compared = []
        for call in record.get("tool_calls", []):
            if call.get("name") != "compare_periods" or not call.get("ok"):
                continue
            for row in call.get("result_summary", {}).get("rows", []):
                for side in ("value_a", "value_b"):
                    result = row.get(side)
                    if result and result.get("result") is not None:
                        compared.append(_dec(result["result"]))
        if len(expected_values) < 2 or len(compared) < 2:
            return False
        text = record.get("final_assistant_text", "")
        text_nums = [v for v, _ in _parsed_values(text, {"unit": expected_values[0]["unit"]}, skip_years=True)]
        for target_record in expected_values:
            target = Decimal(target_record["value"])
            tolerance = Decimal("0.0005") if target_record["unit"] == "ratio" else abs(target) * Decimal("0.0005")
            if not any(actual is not None and abs(actual - target) <= tolerance for actual in compared): return False
            if not any(abs(actual - target) <= tolerance for actual in text_nums): return False
        return True
    target = _dec(expected.get("value"))
    expected_period = item.get("expected_evidence", {}).get("period_end") or _gold_period(item, expected)
    for claim in _claims(_final_report(record)):
        if claim.get("verdict") != "supported" or claim.get("claim_type") != "numeric":
            continue
        assertion = _assertion_for(record, claim.get("claim_id"))
        if assertion and _assertion_matches(assertion, claim.get("claim_text", ""), expected, target, expected_period): return True
    if not _final_report(record):
        text = record.get("final_assistant_text", "")
        for val, token in _parsed_values(text, expected, skip_years=True):
            if target is not None and _units_match(token, expected) and _within(val, target, expected, rounding_tolerance=_rounding_tolerance(token, expected)):
                if not expected_period or str(int(expected_period[:4])) in text: return True
    return False


def _assertion_for(record: dict, claim_id: str | None) -> dict | None:
    if not claim_id: return None
    for call in reversed(record.get("tool_calls", [])):
        if call.get("name") == "verify_citations":
            for claim in call.get("args", {}).get("claims", []):
                if claim.get("claim_id") == claim_id: return claim.get("numeric")
    return None


def _gold_period(item: dict, expected: dict) -> str | None:
    key = expected.get("ground_truth_key")
    if not key: return None
    parts = key.split("|")
    if len(parts) != 3: return None
    ticker, year = parts[0], int(parts[1])
    if ticker == "MSFT": return f"{year:04d}-06-30"
    if ticker == "AAPL":
        from datetime import date, timedelta
        day = date(year, 9, 30)
        while day.weekday() != 5: day -= timedelta(days=1)
        return day.isoformat()
    return None


def _parsed_values(text: str, expected: dict, *, skip_years: bool = False):
    for match in NUMBER.finditer(text):
        token = match.group()
        value = _dec(token)
        if value is None: continue
        if skip_years and re.fullmatch(r"\s*\d{4}\s*", token) and 1900 <= int(value) <= 2100:
            following = text[match.end():match.end() + 24].casefold()
            if not re.match(r"\s*(?:thousand|million|billion)", following): continue
        low = text.casefold()
        if expected.get("unit") == "USD":
            if "billion" in low: value *= Decimal(1_000_000_000)
            elif "million" in low: value *= Decimal(1_000_000)
            elif "thousand" in low: value *= Decimal(1000)
        elif expected.get("unit") == "ratio" and ("%" in token or "percent" in low or "percentage" in low): value /= 100
        yield value, token


def _assertion_matches(assertion: dict, claim_text: str, expected: dict, target: Decimal | None, expected_period: str | None) -> bool:
    written = _dec(assertion.get("value"))
    if written is None or target is None: return False
    scale = {"units": Decimal(1), "thousands": Decimal(1000), "millions": Decimal(1_000_000), "billions": Decimal(1_000_000_000)}.get(assertion.get("scale"), Decimal(1))
    value = written * scale
    unit = assertion.get("unit")
    if expected.get("unit") == "ratio" and unit == "percent": value /= 100
    if expected.get("unit") == "USD" and unit != "USD": return False
    if expected.get("unit") == "ratio" and unit not in {"ratio", "percent"}: return False
    if expected_period and assertion.get("period_end") != expected_period: return False
    if not _within(value, target, expected, rounding_tolerance=_rounding_tolerance(claim_text, expected)):
        return False
    for match in NUMBER.finditer(claim_text):
        number = _dec(match.group())
        if number is None:
            continue
        following = claim_text[match.end():match.end() + 24]
        if re.fullmatch(r"\s*\d{4}\s*", match.group()) and 1900 <= int(number) <= 2100 and not re.match(r"\s*(?:thousand|million|billion)", following, re.I):
            continue
        if expected.get("unit") == "USD":
            text_scale_match = re.match(r"\s*(thousand|million|billion)", following, re.I)
            text_scale = {"thousand": Decimal(1000), "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000)}.get(
                text_scale_match.group(1).casefold(), Decimal(1)) if text_scale_match else Decimal(1)
            normalized = number * text_scale
            stated_precision = _rounding_tolerance(match.group() + following, expected)
            if abs(normalized - value) <= stated_precision:
                return True
        else:
            is_percent_text = "%" in match.group() or re.match(r"\s*percent(?:age)?\b", following, re.I)
            normalized = number / 100 if is_percent_text else number
            if abs(normalized - value) <= Decimal(str(expected.get("tolerance", {}).get("absolute", "0.0005"))):
                return True
    return False


def _rounding_tolerance(text: str, expected: dict) -> Decimal:
    if expected.get("unit") != "USD":
        return Decimal(0)
    matches = [m for m in NUMBER.finditer(text)
               if not (re.fullmatch(r"\s*\d{4}\s*", m.group()) and 1900 <= int(_dec(m.group()) or 0) <= 2100
                       and not re.match(r"\s*(?:thousand|million|billion)", text[m.end():m.end() + 24], re.I))]
    match = matches[0] if matches else None
    if not match:
        return Decimal(0)
    decimals = re.search(r"\.(\d+)", match.group())
    places = len(decimals.group(1) if decimals else "")
    following = text[match.end():match.end() + 24].casefold()
    scale = Decimal(1_000_000_000) if "billion" in following else (Decimal(1_000_000) if "million" in following else (Decimal(1000) if "thousand" in following else Decimal(1)))
    return scale * Decimal(1).scaleb(-places) / 2


def _tolerance_rule(item: dict, record: dict) -> str | None:
    expected = item.get("expected_answer", {})
    if expected.get("kind") == "mixed":
        expected = expected.get("numeric", {})
    if expected.get("kind") == "comparison":
        unit = (expected.get("values") or [{}])[0].get("unit")
        return "comparison_ratio_absolute_0.0005" if unit == "ratio" else "comparison_usd_relative_0.05_percent"
    if expected.get("kind") != "numeric":
        return None
    if expected.get("unit") == "ratio":
        return "ratio_absolute_0.0005"
    text = record.get("final_assistant_text", "")
    report = _final_report(record)
    for claim in _claims(report):
        if claim.get("verdict") == "supported" and claim.get("claim_type") == "numeric":
            text = claim.get("claim_text", text)
            break
    target = _dec(expected.get("value")) or Decimal(0)
    return "usd_agent_stated_rounding" if _rounding_tolerance(text, expected) > abs(target) * Decimal("0.0005") else "usd_relative_0.05_percent"


def _units_match(observed: str, expected: dict) -> bool:
    text = str(observed).casefold()
    if expected.get("unit") == "USD":
        return "$" in text or "usd" in text or "dollar" in text or "million" in text or "billion" in text
    metric = expected.get("metric")
    if metric == "current_ratio":
        return "x" in text or "ratio" in text or "multiple" in text
    return "%" in text or "percent" in text or "percentage" in text or "ratio" in text


def _forbidden_figures(text: str) -> bool:
    patterns = (r"(?:\$\s*\d[\d,]*(?:\.\d+)?|\bUSD\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:USD|dollars?|thousand|million|billion)\b)",
                r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent(?:age)?)", r"\d[\d,]*(?:\.\d+)?\s*x\b")
    return any(re.search(pattern, text, re.I) for pattern in patterns)


_CURRENCY_OR_SCALE = re.compile(r"\$\s*$|\bUSD\s*$", re.I)


def _stated_figures(text: str) -> list[tuple[str, Decimal, Decimal]]:
    figures = []
    for match in NUMBER.finditer(text):
        value = _dec(match.group())
        if value is None:
            continue
        if re.fullmatch(r"\s*\d{4}\s*", match.group()) and 1900 <= int(value) <= 2100:
            continue
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 30]
        scale_match = re.match(r"\s*(thousand|million|billion)", after, re.I)
        if (_CURRENCY_OR_SCALE.search(before) or scale_match or
                re.match(r"\s*(?:USD|US dollars?|dollars?)\b", after, re.I)):
            scale = {"thousand": Decimal(1000), "million": Decimal(1_000_000), "billion": Decimal(1_000_000_000)}.get(
                scale_match.group(1).casefold(), Decimal(1)) if scale_match else Decimal(1)
            decimals = len(re.search(r"\.(\d+)", match.group()).group(1)) if re.search(r"\.(\d+)", match.group()) else 0
            figures.append(("currency", value * scale, scale * Decimal(1).scaleb(-decimals) / 2))
            continue
        if "%" in match.group() or re.match(r"\s*percent(?:age)?\b", after, re.I):
            decimals_match = re.search(r"\.(\d+)", match.group())
            decimals = len(decimals_match.group(1)) if decimals_match else 0
            figures.append(("ratio", value / 100, Decimal(1).scaleb(-decimals) / 200))
            continue
        if re.search(r"x\s*$", match.group(), re.I) or re.match(r"\s*(?:ratio|multiple)\b", after, re.I):
            decimals_match = re.search(r"\.(\d+)", match.group())
            decimals = len(decimals_match.group(1)) if decimals_match else 0
            figures.append(("ratio", value, Decimal(1).scaleb(-decimals) / 2))
            continue
        if re.search(r"(?:growth|margin|ratio|multiple)\s+(?:(?:was|is|of|at)\s+)?$", before, re.I):
            decimals_match = re.search(r"\.(\d+)", match.group())
            decimals = len(decimals_match.group(1)) if decimals_match else 0
            figures.append(("ratio", value, Decimal(1).scaleb(-decimals) / 2))
    return figures


def _successful_output_figures(calls: list[dict]) -> list[tuple[str, Decimal, Decimal]]:
    figures = []

    def visit(value, inherited_unit=None):
        if isinstance(value, dict):
            unit = value.get("result_unit", value.get("unit", inherited_unit))
            display = value.get("display_value")
            if isinstance(display, str):
                figures.extend(_stated_figures(display))
            result = value.get("result")
            if result is not None and unit in {"USD", "ratio", "percent"}:
                number = _dec(result)
                if number is not None:
                    figures.append(("currency" if unit == "USD" else "ratio",
                                    number / 100 if unit == "percent" else number, Decimal(0)))
            for key, child in value.items():
                child_unit = unit if key in {"value_a", "value_b"} else None
                visit(child, child_unit)
        elif isinstance(value, list):
            for child in value:
                visit(child, inherited_unit)

    for call in calls:
        if call.get("ok") and call.get("name") in {"calculate_financial_metric", "compare_periods"}:
            visit(call.get("result_summary", {}))
    return figures


def _requested_target(item: dict, expected: dict) -> tuple[str, int, str] | None:
    key = expected.get("ground_truth_key")
    if not key and expected.get("kind") == "mixed":
        key = expected.get("numeric", {}).get("ground_truth_key")
    if isinstance(key, str):
        parts = key.split("|")
        if len(parts) == 3:
            try:
                return parts[0].upper(), int(parts[1]), parts[2]
            except ValueError:
                pass
    if item.get("question_id") in _MANAGER_REFUSAL_TARGETS:
        return _MANAGER_REFUSAL_TARGETS[item["question_id"]]
    metric, year, ticker = expected.get("metric"), expected.get("fiscal_year"), item.get("ticker")
    if metric is not None and year is not None and ticker:
        try:
            return str(ticker).upper(), int(year), str(metric)
        except (TypeError, ValueError):
            return None
    return None


def _metric_family(metric: str | None) -> str | None:
    if metric in {"revenue", "free_cash_flow"}:
        return "currency"
    if metric in {"revenue_growth_yoy", "operating_margin", "net_margin", "gross_margin", "current_ratio"}:
        return "ratio"
    return None


def _target_period_end(item: dict, expected: dict, target: tuple[str, int, str] | None) -> str | None:
    period_end = item.get("expected_evidence", {}).get("period_end")
    if period_end:
        return str(period_end)
    if target is None:
        return None
    ticker, year, metric = target
    period = _gold_period(item, {"ground_truth_key": f"{ticker}|{year}|{metric}"})
    if period:
        return period
    if ticker == "NVDA" and year == 2025:
        return "2025-01-26"
    return None


def _metric_outputs(calls: list[dict]) -> dict[str, list[dict]]:
    by_id: dict[str, list[dict]] = {}
    for call in calls:
        if not call.get("ok"):
            continue
        summary = call.get("result_summary", {})
        if call.get("name") == "calculate_financial_metric":
            metric = summary.get("result", {})
            evidence_id = summary.get("metric_evidence_id")
            if isinstance(evidence_id, str) and evidence_id.startswith("met_"):
                by_id.setdefault(evidence_id, []).append({
                    "ticker": str(summary.get("ticker", "")).upper(), "metric": metric.get("metric"),
                    "year": summary.get("fiscal_year"),
                })
        elif call.get("name") == "compare_periods":
            ticker = str(summary.get("ticker") or summary.get("company") or "").upper()
            for row in summary.get("rows", []):
                for side, year_key in (("a", "fiscal_year_a"), ("b", "fiscal_year_b")):
                    evidence_id = row.get(f"metric_evidence_id_{side}")
                    if isinstance(evidence_id, str) and evidence_id.startswith("met_"):
                        by_id.setdefault(evidence_id, []).append({
                            "ticker": ticker, "metric": row.get("metric"), "year": summary.get(year_key),
                        })
    return by_id


def _supported_numeric_claims(record: dict) -> list[tuple[dict, dict | None]]:
    result = []
    verify_claims = {}
    for call in record.get("tool_calls", []):
        if call.get("name") == "verify_citations":
            for submitted in call.get("args", {}).get("claims", []):
                if submitted.get("claim_id"):
                    verify_claims[submitted["claim_id"]] = submitted
    for claim in _claims(_final_report(record)):
        if claim.get("verdict") == "supported" and claim.get("claim_type") == "numeric":
            submitted = verify_claims.get(claim.get("claim_id"), {})
            merged = dict(claim)
            merged["_grader_citation_ids"] = list(dict.fromkeys(
                [*claim.get("evidence_ids", []), *submitted.get("evidence_ids", [])]))
            result.append((merged, _assertion_for(record, claim.get("claim_id"))))
    return result


def _claim_targets_requested(claim: dict, assertion: dict | None, outputs: dict[str, list[dict]],
                             target: tuple[str, int, str] | None, target_period_end: str | None) -> bool:
    if target is None:
        return False
    ticker, year, metric = target
    cited_ids = [value for value in claim.get("_grader_citation_ids", claim.get("evidence_ids", []))
                 if isinstance(value, str) and value.startswith("met_")]
    for evidence_id in cited_ids:
        if any(output.get("ticker") == ticker and output.get("metric") == metric
               and output.get("year") == year for output in outputs.get(evidence_id, [])):
            return True
    if cited_ids or assertion is None or target_period_end is None:
        return False
    unit_family = ("currency" if assertion.get("unit") == "USD" else
                   "ratio" if assertion.get("unit") in {"ratio", "percent"} else None)
    return unit_family == _metric_family(metric) and assertion.get("period_end") == target_period_end


def _assertion_figure(assertion: dict | None) -> tuple[str, Decimal] | None:
    if not assertion:
        return None
    value = _dec(assertion.get("value"))
    if value is None:
        return None
    scale = {"units": Decimal(1), "thousands": Decimal(1000), "millions": Decimal(1_000_000),
             "billions": Decimal(1_000_000_000)}.get(assertion.get("scale"), Decimal(1))
    value *= scale
    if assertion.get("unit") == "USD":
        return "currency", value
    if assertion.get("unit") == "percent":
        return "ratio", value / 100
    if assertion.get("unit") == "ratio":
        return "ratio", value
    return None


def _currency_percent_figures(text: str) -> list[tuple[str, Decimal, Decimal]]:
    figures = []
    for match in NUMBER.finditer(text):
        token, value = match.group(), _dec(match.group())
        if value is None or (re.fullmatch(r"\s*\d{4}\s*", token) and 1900 <= int(value) <= 2100):
            continue
        before = text[max(0, match.start() - 40):match.start()]
        after = text[match.end():match.end() + 30]
        scale_match = re.match(r"\s*(thousand|million|billion)", after, re.I)
        decimals_match = re.search(r"\.(\d+)", token)
        decimals = len(decimals_match.group(1)) if decimals_match else 0
        if (_CURRENCY_OR_SCALE.search(before) or scale_match or
                re.match(r"\s*(?:USD|US dollars?|dollars?)\b", after, re.I)):
            scale = {"thousand": Decimal(1000), "million": Decimal(1_000_000),
                     "billion": Decimal(1_000_000_000)}.get(scale_match.group(1).casefold(), Decimal(1)) if scale_match else Decimal(1)
            figures.append(("currency", value * scale, scale * Decimal(1).scaleb(-decimals) / 2))
        elif "%" in token or re.match(r"\s*percent(?:age)?\b", after, re.I):
            figures.append(("ratio", value / 100, Decimal(1).scaleb(-decimals) / 200))
    return figures


def _figure_matches_claim(figure: tuple[str, Decimal, Decimal],
                          claims: list[tuple[dict, dict | None]]) -> bool:
    family, value, tolerance = figure
    for _claim, assertion in claims:
        asserted = _assertion_figure(assertion)
        if asserted and asserted[0] == family and abs(value - asserted[1]) <= tolerance:
            return True
    return False


def _refusal_has_forbidden_figure(text: str, item: dict, expected: dict,
                                  calls: list[dict], record: dict) -> bool:
    target = _requested_target(item, expected)
    target_period_end = _target_period_end(item, expected, target)
    requested_family = _metric_family(target[2]) if target else None
    claims = _supported_numeric_claims(record)
    outputs = _metric_outputs(calls)

    # Rule 1: no supported numeric claim may point at the requested metric and year.
    if any(_claim_targets_requested(claim, assertion, outputs, target, target_period_end)
           for claim, assertion in claims):
        return True

    # Rule 2: requested-family figures must be supported by a complementary verified claim.
    complementary = [(claim, assertion) for claim, assertion in claims
                     if not _claim_targets_requested(claim, assertion, outputs, target, target_period_end)]
    if requested_family and any(figure[0] == requested_family and
                                not _figure_matches_claim(figure, complementary)
                                for figure in _stated_figures(text)):
        return True

    # Rule 3: every currency or percentage figure must come from an output or a supported claim.
    permitted = _successful_output_figures(calls)
    for figure in _currency_percent_figures(text):
        family, value, tolerance = figure
        output_match = any(out_family == family and abs(value - out_value) <= max(tolerance, out_tolerance)
                           for out_family, out_value, out_tolerance in permitted)
        if not output_match and not _figure_matches_claim(figure, claims):
            return True
    return False


def _accepted_refusal_codes(expected: dict) -> set[str]:
    evidence = expected.get("refusal_evidence")
    if evidence is not None:
        return set(evidence.get("any_error_codes", []))
    code = expected.get("expected_error_code")
    return {code} if code else set()


def _within(value: Decimal, target: Decimal, expected: dict, precision: int | None = None,
            rounding_tolerance: Decimal = Decimal(0)) -> bool:
    tol = expected.get("tolerance", {})
    if "absolute" in tol:
        return abs(value - target) <= Decimal(str(tol["absolute"]))
    relative = Decimal(str(tol.get("relative", "0.0005")))
    rounding = Decimal(1).scaleb(-precision) / 2 if precision is not None else rounding_tolerance
    return abs(value - target) <= max(abs(target) * relative, rounding)


def grade_item(item: dict, record: dict) -> dict:
    calls = record.get("tool_calls", [])
    names = _tools(record)
    expected_tools = item.get("expected_tools", [])
    tools_ok = set(expected_tools).issubset(names)
    order_ok = True
    for first, second in item.get("expected_tool_order", []):
        if first not in names or second not in names or names.index(first) >= names.index(second):
            order_ok = False
    expected = item.get("expected_answer", {})
    if item.get("question_type") in {"numeric_lookup", "numeric_failure", "mixed", "comparison"}:
        if expected.get("kind") != "refusal":
            successful_calc = any(c.get("name") in {"calculate_financial_metric", "compare_periods"} and c.get("ok") for c in calls)
            states_number = bool(NUMBER.search(record.get("final_assistant_text", "")))
            if states_number and not successful_calc:
                tools_ok = False
    if expected.get("kind") == "refusal" and _refusal_has_forbidden_figure(
            record.get("final_assistant_text", ""), item, expected, calls, record):
        tools_ok = False
    tools_ok &= order_ok
    report = _final_report(record)
    numeric_correct = _numeric_in_answer(record, item) if expected.get("kind") in {"numeric", "comparison"} or item.get("question_type") == "mixed" else None
    refusal_correct = None
    if item.get("question_type") in {"failure", "numeric_failure"}:
        evidence = expected.get("refusal_evidence", {})
        accepted_codes = _accepted_refusal_codes(expected)
        error_seen = any(not c.get("ok", True) and c.get("error_code") in accepted_codes for c in calls)
        unpinned_seen = evidence.get("or_search_companies_unpinned", False) and any(
            c.get("name") == "search_companies" and any(company.get("ticker", "").upper() == item.get("ticker", "").upper() and company.get("pinned") is False
                for company in c.get("result_summary", {}).get("candidates", [])) for c in calls)
        optional = evidence.get("tool_optional", False)
        final_text = record.get("final_assistant_text", "")
        refusal_correct = ((error_seen or unpinned_seen or optional) and
                           not _refusal_has_forbidden_figure(final_text, item, expected, calls, record))
    fields_ok = True
    for name, value in expected.get("fields", {}).items():
        fields_ok &= value == record.get("metadata", {}).get(name)
    executed_evidence = record.get("search_evidence", [])
    gold = set(item.get("expected_evidence", {}).get("evidence_ids", []))
    retrieval_rank = next((x.get("rank", i + 1) for i, x in enumerate(executed_evidence)
                           if (x.get("evidence_id") if isinstance(x, dict) else x) in gold), None)
    top_k = record.get("top_k")
    retrieval_hit = bool(gold and retrieval_rank is not None and (top_k is None or retrieval_rank <= top_k)) if gold else None
    verifications = _claims(report)
    needing = [v for v in verifications if v.get("evidence_ids")]
    def coverage_passes(v):
        checks = v.get("deterministic_checks", {})
        return bool(v.get("evidence_ids")) and all(checks.get(k) is True for k in
            ("evidence_exists", "hash_matches", "provenance_consistent", "ticker_matches", "single_accession"))
    valid = [v for v in needing if coverage_passes(v)]
    unsupported = sum(v.get("verdict") == "unsupported" for v in verifications)
    support_den = len(verifications)
    keypoints = expected.get("key_points", [])
    supported_text = " ".join(v.get("claim_text", "") for v in verifications
                               if v.get("verdict") in {"supported", "partially_supported"}).casefold()
    keypoint_score = sum(k.casefold() in supported_text for k in keypoints) / len(keypoints) if keypoints else None
    categories = []
    if record.get("runtime_error"): categories.append("runtime_error")
    if record.get("schema_error") or any(c.get("error_code") in {"invalid_input", "schema_error"} for c in calls):
        categories.append("schema_error")
    accepted_codes = _accepted_refusal_codes(expected)
    if any(not c.get("ok", True) and c.get("error_code") not in accepted_codes for c in calls):
        categories.append("tool_error")
    if not tools_ok:
        categories.append("no_calculator" if "calculate_financial_metric" in expected_tools and "calculate_financial_metric" not in names else "wrong_tool")
    if numeric_correct is False: categories.append("fact_selection")
    if retrieval_hit is False: categories.append("retrieval_miss")
    if unsupported: categories.append("unsupported_claim")
    if refusal_correct is False: categories.append("refusal_missing")
    return {
        "numeric_correct": numeric_correct, "numeric_path": "verify_report" if report else "assistant_text",
        "tolerance_rule_applied": _tolerance_rule(item, record),
        "refusal_correct": refusal_correct, "metadata_correct": bool(fields_ok), "tool_selection_correct": bool(tools_ok),
        "retrieval_hit_at_k": retrieval_hit, "retrieval_rank": retrieval_rank,
        "citation_coverage": len(valid) / len(needing) if needing else None,
        "citation_coverage_numerator": len(valid), "citation_coverage_n_claims": len(needing),
        "unsupported_claim_rate": unsupported / support_den if support_den else None,
        "unsupported_claim_count": unsupported, "unsupported_claim_n_claims": support_den,
        "partial_claim_rate": sum(v.get("verdict") == "partially_supported" for v in verifications) / support_den if support_den else None,
        "narrative_key_points_proxy": keypoint_score, "latency_s": record.get("latency_s"),
        "tokens_in": record.get("tokens_in"), "tokens_out": record.get("tokens_out"),
        "cost_pi_usd": record.get("cost_pi_usd"), "cost_judge_usd": record.get("cost_judge_usd"),
        "retry_used": len(record.get("verify_outputs", [])) > 1, "runtime_error": record.get("runtime_error"),
        "error_categories": sorted(set(categories)),
    }


def _mean(values: list[Any]) -> dict:
    numbers = [float(v) for v in values if isinstance(v, (int, float))]
    return {"mean": statistics.fmean(numbers) if numbers else None, "n": len(numbers)}


def summarize(rows: list[dict], agent: str = "pi") -> dict:
    def group(rows_: list[dict]) -> dict:
        metric_names = ("numeric_correct", "refusal_correct", "metadata_correct", "tool_selection_correct",
                       "retrieval_hit_at_k", "citation_coverage", "unsupported_claim_rate", "partial_claim_rate",
                       "narrative_key_points_proxy", "latency_s", "tokens_in", "tokens_out", "cost_pi_usd", "cost_judge_usd")
        out = {k: _mean([r.get("grades", {}).get(k) for r in rows_ if r.get("grades", {}).get(k) is not None]) for k in metric_names}
        for metric, numerator_key, denominator_key in (("citation_coverage", "citation_coverage_numerator", "citation_coverage_n_claims"),
                                                       ("unsupported_claim_rate", "unsupported_claim_count", "unsupported_claim_n_claims")):
            numerator = sum(int(r.get("grades", {}).get(numerator_key, 0) or 0) for r in rows_)
            n_claims = sum(int(r.get("grades", {}).get(denominator_key, 0) or 0) for r in rows_)
            out[f"{metric}_item_mean"] = out[metric]
            out[metric] = {"rate": numerator / n_claims if n_claims else None, "n_claims": n_claims}
        lat = sorted(float(r["grades"]["latency_s"]) for r in rows_ if r.get("grades", {}).get("latency_s") is not None)
        out["latency_p50_s"] = _percentile(lat, .5)
        out["latency_p95_s"] = _percentile(lat, .95)
        out["total_cost_usd"] = sum(float(r.get("grades", {}).get("cost_pi_usd") or 0) + float(r.get("grades", {}).get("cost_judge_usd") or 0) for r in rows_)
        out["cost_unknown_items"] = sum(bool(r.get("runtime_error") and r.get("cost_pi_usd") is None) for r in rows_)
        cats = Counter(c for r in rows_ for c in r.get("grades", {}).get("error_categories", []))
        out["error_taxonomy"] = {c: cats.get(c, 0) for c in ERROR_CATEGORIES}
        out["items_with_provider_retries"] = sum(
            any(event.get("type") == "auto_retry_start" for event in r.get("runtime_events", []))
            for r in rows_
        )
        out["items_with_stall_retry"] = sum(int(r.get("stall_retries_used", 0) or 0) > 0 for r in rows_)
        out["items_failed_by_stall"] = sum(str(r.get("runtime_error", "")).startswith("PiStallError") for r in rows_)
        out["items"] = len(rows_)
        return out
    result = {"agent": agent, "overall": group(rows), "by_split": {}, "by_question_type": {}}
    if agent == "fake":
        result["banner"] = "PIPELINE TEST — NOT A MEASUREMENT"
    for split in sorted({r.get("split", "unknown") for r in rows}): result["by_split"][split] = group([r for r in rows if r.get("split", "unknown") == split])
    for typ in sorted({r.get("question_type", "unknown") for r in rows}): result["by_question_type"][typ] = group([r for r in rows if r.get("question_type", "unknown") == typ])
    return result


def _percentile(values: list[float], p: float) -> float | None:
    if not values: return None
    return values[round((len(values) - 1) * p)]
