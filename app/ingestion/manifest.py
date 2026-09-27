import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import (AccessionCompanyMismatchError, AccessionNotInManifestError,
                               AmbiguousManifestCompanyError, CacheIntegrityError,
                               CompanyNotInManifestError, ManifestNotFoundError)


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    cik: str
    company_name: str
    form: str
    accession_number: str
    filing_date: str
    report_date: str
    primary_document: str
    source_url: str
    document_sha256: str
    companyfacts_url: str
    companyfacts_sha256: str
    retrieved_at: str
    selection_rule: str


class FilingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_version: str = "v1"
    created_at: str
    sec_user_agent_set: bool
    entries: list[ManifestEntry] = Field(default_factory=list)
    supersedes: str | None = None
    change_reason: str | None = None


def resolve_manifest_entry(manifest: FilingManifest, company: str, accession: str | None):
    """Resolve a ticker/CIK and optional accession within the pinned manifest."""
    normalized = company.strip()
    if normalized.isdigit():
        cik = normalized.lstrip("0") or "0"
        entries = [entry for entry in manifest.entries if (entry.cik.lstrip("0") or "0") == cik]
    else:
        entries = [entry for entry in manifest.entries if entry.ticker.upper() == normalized.upper()]
    if not entries:
        raise CompanyNotInManifestError(company)
    if accession is None:
        if len(entries) > 1:
            raise AmbiguousManifestCompanyError(company, [entry.accession_number for entry in entries])
        return entries[0], True
    pinned = next((entry for entry in manifest.entries if entry.accession_number == accession), None)
    if pinned is None:
        raise AccessionNotInManifestError(accession)
    if pinned not in entries:
        raise AccessionCompanyMismatchError(accession, company)
    return pinned, False


def manifest_versions(manifests_dir: Path) -> list[tuple[int, Path]]:
    versions = []
    for candidate in Path(manifests_dir).glob("filings.v*.json"):
        match = re.fullmatch(r"filings\.v(\d+)\.json", candidate.name)
        if match:
            versions.append((int(match.group(1)), candidate))
    return sorted(versions)


def next_manifest_version(manifests_dir: Path) -> str:
    versions = manifest_versions(manifests_dir)
    return f"v{versions[-1][0] + 1 if versions else 1}"


def latest_manifest_path(manifests_dir: Path) -> Path:
    versions = manifest_versions(manifests_dir)
    if not versions:
        raise ManifestNotFoundError()
    return versions[-1][1]


def build_manifest(entries: list[dict[str, Any]], *, version: str = "v1", created_at: str | None = None,
                   sec_user_agent_set: bool = True, supersedes: str | None = None,
                   change_reason: str | None = None) -> FilingManifest:
    if not version.startswith("v") or not version[1:].isdigit():
        raise ValueError(f"Invalid manifest version: {version}")
    return FilingManifest(manifest_version=version, created_at=created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                          sec_user_agent_set=sec_user_agent_set,
                          entries=[ManifestEntry.model_validate(row) for row in sorted(entries, key=lambda x: x["ticker"])],
                          supersedes=supersedes, change_reason=change_reason)


def manifest_bytes(manifest: FilingManifest) -> bytes:
    return (json.dumps(manifest.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode()


def write_manifest(manifest: FilingManifest, path: Path, *, exclusive: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise FileExistsError(f"Manifest already exists and is immutable: {path}")
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temp_path = handle.name
            handle.write(manifest_bytes(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            # Hard-link publication is atomic and fails if another writer created path.
            os.link(temp_path, path)
            os.unlink(temp_path)
            temp_path = None
        else:
            os.replace(temp_path, path)
            temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def load_manifest(path: Path) -> FilingManifest:
    return FilingManifest.model_validate_json(path.read_text("utf-8"))


def verify_entry_hashes(entry: ManifestEntry, document: bytes, companyfacts: bytes) -> None:
    if hashlib.sha256(document).hexdigest() != entry.document_sha256:
        raise CacheIntegrityError(entry.source_url)
    if hashlib.sha256(companyfacts).hexdigest() != entry.companyfacts_sha256:
        raise CacheIntegrityError(entry.companyfacts_url)
