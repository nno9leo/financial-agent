"""Normalized companyfacts facts (`FinancialFact`) and the loader.

Values are exact `Decimal`s: the raw payload is parsed with `parse_float=Decimal`
so a value never passes through a binary float. Only `us-gaap` facts are kept
(the `dei` taxonomy and any `_fixture_note` key are ignored); facts from every
accession are kept so fact selection can explain its rejections.
"""

import json
from datetime import date
from decimal import Decimal
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, HttpUrl

from app.ingestion.manifest import ManifestEntry

TAXONOMY_US_GAAP = "us-gaap"


class FinancialFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    cik: str
    taxonomy: str
    tag: str
    label: str | None
    value: Decimal
    unit: str
    period_start: date | None
    period_end: date
    fiscal_year: int | None              # SEC `fy` as reported: fiscal year of the FILING, never used for selection
    fiscal_period: str | None            # SEC `fp` as reported, never used for selection
    form: str
    filed_at: date
    accession_number: str
    frame: str | None
    source_url: HttpUrl                 # 10-K document URL for the pinned accession; EDGAR filing-index URL for other accessions


def fact_id(fact: FinancialFact) -> str:
    digest = "|".join([
        fact.accession_number,
        fact.taxonomy,
        fact.tag,
        fact.unit,
        str(fact.period_start),
        str(fact.period_end),
        str(fact.value),
    ])
    return "fact_" + sha256(digest.encode("utf-8")).hexdigest()[:20]


def filing_index_url(cik: str, accession_number: str) -> str:
    """EDGAR filing-index URL for an accession; used for facts from non-pinned accessions."""
    compact = accession_number.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{compact}/"


def _to_decimal(value: Any, where: str) -> Decimal:
    # `bool` is an `int` subclass; JSON never produces it for `val`, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"Malformed companyfacts payload at {where}: val={value!r} is not a number")
    if isinstance(value, float):
        # A pre-parsed dict may hold floats; convert through their shortest repr, never compute on them.
        return Decimal(str(value))
    return Decimal(value)


def _to_date(value: Any, where: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Malformed companyfacts payload at {where}: date={value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Malformed companyfacts payload at {where}: date={value!r}") from exc


def normalize_companyfacts(raw: bytes | dict, *, ticker: str, cik: str,
                           pinned: ManifestEntry) -> list[FinancialFact]:
    """Normalize every us-gaap fact of a companyfacts payload into `FinancialFact`s.

    Accepts the raw bytes (parsed with `parse_float=Decimal`, so fractional values
    stay exact) or an already-parsed dict (used by tests that edit fixture copies).
    """
    if isinstance(raw, (bytes, bytearray)):
        payload = json.loads(raw, parse_float=Decimal)
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise ValueError(f"Unsupported companyfacts payload type: {type(raw)!r}")
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), dict):
        raise ValueError("Malformed companyfacts payload: expected an object with a 'facts' key")

    facts: list[FinancialFact] = []
    taxonomy_data = payload["facts"].get(TAXONOMY_US_GAAP, {})
    if not isinstance(taxonomy_data, dict):
        raise ValueError("Malformed companyfacts payload: 'facts.us-gaap' must be an object")
    for tag, tag_data in taxonomy_data.items():
        if not isinstance(tag_data, dict) or not isinstance(tag_data.get("units"), dict):
            raise ValueError(f"Malformed companyfacts payload: facts.us-gaap.{tag} must hold a 'units' object")
        label = tag_data.get("label")
        for unit, unit_facts in tag_data["units"].items():
            if not isinstance(unit_facts, list):
                raise ValueError(f"Malformed companyfacts payload: facts.us-gaap.{tag}.units.{unit} must be a list")
            for item in unit_facts:
                if not isinstance(item, dict):
                    raise ValueError(f"Malformed companyfacts payload: facts.us-gaap.{tag}.units.{unit} entries must be objects")
                where = f"us-gaap/{tag}/{unit}"
                if "end" not in item or "val" not in item or "accn" not in item or "form" not in item or "filed" not in item:
                    raise ValueError(f"Malformed companyfacts fact at {where}: missing required key(s)")
                accession_number = item["accn"]
                facts.append(FinancialFact(
                    ticker=ticker,
                    cik=cik,
                    taxonomy=TAXONOMY_US_GAAP,
                    tag=tag,
                    label=label if isinstance(label, str) else None,
                    value=_to_decimal(item["val"], where),
                    unit=unit,
                    period_start=_to_date(item["start"], where) if "start" in item else None,
                    period_end=_to_date(item["end"], where),
                    fiscal_year=item["fy"] if isinstance(item.get("fy"), int) else None,
                    fiscal_period=item["fp"] if isinstance(item.get("fp"), str) else None,
                    form=item["form"],
                    filed_at=_to_date(item["filed"], where),
                    accession_number=accession_number,
                    frame=item["frame"] if isinstance(item.get("frame"), str) else None,
                    source_url=str(pinned.source_url) if accession_number == pinned.accession_number
                    else filing_index_url(cik, accession_number),
                ))
    facts.sort(key=lambda f: (f.taxonomy, f.tag, f.unit, f.period_start or f.period_end, f.period_end,
                              f.accession_number, f.filed_at, f.value, f.form))
    return facts
