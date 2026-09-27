"""Small checked-in snapshots for tests that exercise SEC-backed code offline."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.ingestion.manifest import FilingManifest, load_manifest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "data/manifests/filings.v1.json"


def companyfacts_fixture() -> tuple[FilingManifest, dict[str, bytes]]:
    """Return a manifest pinned to the hashes of the trimmed test payloads."""
    manifest = load_manifest(MANIFEST_PATH)
    bodies = {}
    hashes = {}
    for entry in manifest.entries:
        path = ROOT / "tests/fixtures/companyfacts" / f"{entry.ticker.lower()}_offline_subset.json"
        body = path.read_bytes()
        bodies[entry.companyfacts_url] = body
        hashes[entry.ticker] = hashlib.sha256(body).hexdigest()
    entries = [entry.model_copy(update={"companyfacts_sha256": hashes[entry.ticker]})
               for entry in manifest.entries]
    return manifest.model_copy(update={"entries": entries}), bodies


def write_fixture_manifest(directory: Path, manifest: FilingManifest) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "filings.v1.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return directory


def seed_msft_normalized_excerpt(directory: Path, accession: str) -> None:
    """Create valid normalized-file metadata around a committed text excerpt."""
    text = (ROOT / "tests/fixtures/normalized/msft_financial_statements_excerpt.txt").read_text(encoding="utf-8")
    section_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    target = directory / accession
    target.mkdir(parents=True, exist_ok=True)
    (target / "canonical.txt").write_text(text, encoding="utf-8")
    sections = {"canonical_sha256": section_hash, "financial_statements": {
        "section": "financial_statements", "heading_text": "ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA",
        "start_offset": 0, "end_offset": len(text), "section_sha256": section_hash,
        "canonical_sha256": section_hash,
    }}
    (target / "sections.json").write_text(json.dumps(sections, sort_keys=True) + "\n", encoding="utf-8")


class OfflineCompanyFactsProvider:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies

    def get_cached(self, url: str):
        body = self.bodies.get(url)
        return None if body is None else (body, {"source_url": url, "cache": "fixture"})

    def resolve_company(self, value):
        raise AssertionError("company resolution is not part of this fixture")

    def list_filings(self, *args, **kwargs):
        from app.domain.filings import FilingListResult
        return FilingListResult(filings=[], excluded=[], coverage_start=None, older_pages_not_loaded=False)

    def close(self):
        pass
