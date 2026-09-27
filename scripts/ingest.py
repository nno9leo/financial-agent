#!/usr/bin/env python3
import argparse
import hashlib
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.domain.errors import (CacheIntegrityError, CompanyFactsSnapshotMismatchError,
                               CompanyFactsSnapshotUnchangedError, ConfigurationError,
                               FilingNotFoundError, FilingRangeUnavailableError, ManifestNotFoundError)
from app.ingestion.manifest import (build_manifest, load_manifest, manifest_versions,
                                    next_manifest_version, write_manifest)
from app.providers.sec import BASE_DATA, SecProvider

RULE = "Most recent original 10-K by filing_date; amendments excluded."


def run(*, repin: bool = False, provider: SecProvider | None = None,
        provider_factory: Callable[[Settings], SecProvider] | None = None,
        manifests_dir: Path | None = None, settings: Settings | None = None,
        accept_new_companyfacts_snapshot: bool = False) -> Path:
    if repin and accept_new_companyfacts_snapshot:
        raise ValueError("--repin and --accept-new-companyfacts-snapshot are mutually exclusive")
    settings = settings or Settings()
    if not settings.sec_user_agent:
        raise ConfigurationError("Set SEC_USER_AGENT in .env or the environment before live ingestion.")
    manifests_dir = Path(manifests_dir or (ROOT / "data/manifests"))
    manifests_dir.mkdir(parents=True, exist_ok=True)
    versions = manifest_versions(manifests_dir)
    latest_path = versions[-1][1] if versions else None
    if accept_new_companyfacts_snapshot and latest_path is None:
        raise ManifestNotFoundError()

    if latest_path is not None and not repin:
        pinned = load_manifest(latest_path)
        sec = provider or (provider_factory or SecProvider)(settings)
        try:
            if accept_new_companyfacts_snapshot:
                version = next_manifest_version(manifests_dir)
                target = manifests_dir / f"filings.{version}.json"
                rows = []
                changed_any = False
                for entry in pinned.entries:
                    document, document_meta = sec.get_cached_or_fetch(entry.source_url)
                    document_hash = hashlib.sha256(document).hexdigest()
                    if document_hash != entry.document_sha256:
                        raise CacheIntegrityError(entry.source_url)
                    facts, facts_meta = sec.get_company_facts(entry.cik)
                    row = entry.model_dump()
                    refreshed_hash = hashlib.sha256(facts).hexdigest()
                    changed_any = changed_any or refreshed_hash != entry.companyfacts_sha256
                    row["companyfacts_sha256"] = refreshed_hash
                    row["retrieved_at"] = max(document_meta["retrieved_at"], facts_meta["retrieved_at"])
                    rows.append(row)
                if not changed_any:
                    raise CompanyFactsSnapshotUnchangedError()
                refreshed = build_manifest(rows, version=version,
                    created_at=max(row["retrieved_at"] for row in rows), sec_user_agent_set=True,
                    supersedes=pinned.manifest_version, change_reason="companyfacts_snapshot_refresh")
                write_manifest(refreshed, target, exclusive=True)
                return target

            for entry in pinned.entries:
                document, document_meta = sec.get_cached_or_fetch(entry.source_url)
                document_hash = hashlib.sha256(document).hexdigest()
                if document_hash != entry.document_sha256:
                    raise CacheIntegrityError(entry.source_url)
                facts, facts_meta = sec.get_company_facts(entry.cik)
                facts_hash = hashlib.sha256(facts).hexdigest()
                if facts_hash != entry.companyfacts_sha256:
                    raise CompanyFactsSnapshotMismatchError(entry.ticker, entry.companyfacts_sha256, facts_hash)
            return latest_path
        finally:
            if provider is None:
                sec.close()

    sec = provider or (provider_factory or SecProvider)(settings)
    try:
        version = next_manifest_version(manifests_dir) if repin else "v1"
        target = manifests_dir / f"filings.{version}.json"
        rows = []
        for ticker in ("MSFT", "AAPL"):
            normalized_ticker, cik, name = sec.resolve_company(ticker)
            listing = sec.list_filings(cik, {"10-K"})
            originals = [filing for filing in listing.filings if filing.form == "10-K"]
            if not originals:
                if listing.older_pages_not_loaded:
                    raise FilingRangeUnavailableError(f"No original 10-K is present in recent submissions for {ticker}; older pages are available but were not loaded.")
                raise FilingNotFoundError(f"No original 10-K available for {ticker}; excluded={listing.excluded}")
            # Recent submissions are newest first, so any original 10-K there is newer than every older-page filing.
            filing = max(originals, key=lambda item: item.filing_date)
            document, document_meta = sec.get_filing_document(filing)
            _, facts_meta = sec.get_company_facts(cik)
            facts_url = f"{BASE_DATA}/api/xbrl/companyfacts/CIK{cik}.json"
            rows.append({"ticker": normalized_ticker, "cik": cik, "company_name": name, "form": filing.form,
                         "accession_number": filing.accession_number, "filing_date": filing.filing_date.isoformat(),
                         "report_date": filing.report_date.isoformat(), "primary_document": filing.primary_document,
                         "source_url": str(filing.source_url), "document_sha256": document_meta["sha256"],
                         "companyfacts_url": facts_url, "companyfacts_sha256": facts_meta["sha256"],
                         "retrieved_at": max(document_meta["retrieved_at"], facts_meta["retrieved_at"]), "selection_rule": RULE})
        supersedes = load_manifest(latest_path).manifest_version if repin and latest_path else None
        manifest = build_manifest(rows, version=version, created_at=max(row["retrieved_at"] for row in rows),
                                  sec_user_agent_set=True, supersedes=supersedes)
        write_manifest(manifest, target, exclusive=True)
        return target
    finally:
        if provider is None:
            sec.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repin", action="store_true")
    group.add_argument("--accept-new-companyfacts-snapshot", action="store_true")
    args = parser.parse_args()
    print(run(repin=args.repin, accept_new_companyfacts_snapshot=args.accept_new_companyfacts_snapshot))
