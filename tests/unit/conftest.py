"""Shared fixtures for metric tests: real companyfacts subsets and the pinned manifest."""

import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "companyfacts"
MANIFEST_PATH = ROOT / "data/manifests" / "filings.v1.json"


@pytest.fixture(scope="session")
def manifest():
    from app.ingestion.manifest import load_manifest
    return load_manifest(MANIFEST_PATH)


@pytest.fixture()
def aapl_entry(manifest):
    return next(e for e in manifest.entries if e.ticker == "AAPL")


@pytest.fixture()
def msft_entry(manifest):
    return next(e for e in manifest.entries if e.ticker == "MSFT")


def load_raw_fixture(name):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture()
def aapl_raw():
    return load_raw_fixture("aapl_subset.json")


@pytest.fixture()
def msft_raw():
    return load_raw_fixture("msft_subset.json")


def _normalize(raw, entry):
    from app.domain.financial_facts import normalize_companyfacts
    return normalize_companyfacts(raw, ticker=entry.ticker, cik=entry.cik, pinned=entry)


@pytest.fixture()
def aapl_facts(aapl_raw, aapl_entry):
    return _normalize(aapl_raw, aapl_entry)


@pytest.fixture()
def msft_facts(msft_raw, msft_entry):
    return _normalize(msft_raw, msft_entry)


def modified(raw):
    """Deep copy of a loaded fixture dict, ready to be edited for a modified case."""
    return copy.deepcopy(raw)


def tag_units(raw, tag):
    return raw["facts"]["us-gaap"][tag]["units"]


def add_fact(raw, tag, *, unit="USD", accn, start=None, end, val, form="10-K", filed, fy=None, fp=None, frame=None):
    """Append one raw fact to a fixture copy; mirrors the real payload shape."""
    fact = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start is not None:
        fact["start"] = start
    if frame is not None:
        fact["frame"] = frame
    tag_units(raw, tag).setdefault(unit, []).append(fact)
    return fact


def pinned_facts_of(facts, accession_number):
    return [f for f in facts if f.accession_number == accession_number]
