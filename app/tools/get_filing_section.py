import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.errors import CacheIntegrityError, ResearchError
from app.ingestion.manifest import FilingManifest, latest_manifest_path, load_manifest, resolve_manifest_entry
from app.ingestion.sections import SectionName, SectionNotFoundError


class FilingSectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1)
    section: SectionName
    accession_number: str | None = None
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=12000, ge=1, le=40000)

    @field_validator("company")
    @classmethod
    def company_nonblank(cls, value):
        if not value.strip(): raise ValueError("must not be empty")
        return value.strip()


class FilingSectionOutput(BaseModel):
    text: str
    total_chars: int
    offset: int
    returned_chars: int
    next_offset: int | None
    truncated: bool
    heading_text: str
    section_sha256: str
    start_offset: int
    end_offset: int
    ticker: str
    cik: str
    accession_number: str
    form: str
    filing_date: str
    report_date: str
    source_url: str


class FilingBuildOutputError(ResearchError):
    def __init__(self, accession):
        super().__init__("filing_build_output_missing", f"Normalized filing outputs are missing for {accession}.",
                         "Run scripts/build_chunks.py.")


class FilingSectionIntegrityError(ResearchError):
    def __init__(self, accession):
        super().__init__("filing_section_integrity_error", f"Canonical text hash does not match sections metadata for {accession}.",
                         "Run scripts/build_chunks.py and inspect normalized filing outputs.")


def get_filing_section(request: FilingSectionInput | dict, *, manifest: FilingManifest | None = None,
                       manifests_dir: Path | None = None, normalized_dir: Path | None = None):
    args = request if isinstance(request, FilingSectionInput) else FilingSectionInput.model_validate(request)
    root = Path(__file__).resolve().parents[2]
    if manifest is None:
        manifest = load_manifest(latest_manifest_path(Path(manifests_dir or root / "data/manifests")))
    entry, _ = resolve_manifest_entry(manifest, args.company, args.accession_number)
    directory = Path(normalized_dir or root / "data/normalized") / entry.accession_number
    sections_path, canonical_path = directory / "sections.json", directory / "canonical.txt"
    if not sections_path.exists() or not canonical_path.exists():
        raise FilingBuildOutputError(entry.accession_number)
    canonical = canonical_path.read_text("utf-8")
    data = json.loads(sections_path.read_text("utf-8"))
    expected = data.get("canonical_sha256") or data.get("canonical_text_sha256")
    chunk_path = directory / "chunks.jsonl"
    if not expected and chunk_path.exists():
        for line in chunk_path.read_text("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                expected = row.get("canonical_sha256")
                if expected: break
    if not expected or hashlib.sha256(canonical.encode()).hexdigest() != expected:
        raise FilingSectionIntegrityError(entry.accession_number)
    records = data.get("sections", data)
    key = args.section.value
    record = records.get(key) or records.get(key.upper()) if isinstance(records, dict) else None
    if record is None:
        candidates = data.get("heading_candidates", data.get("candidates", [])) if isinstance(data, dict) else []
        raise SectionNotFoundError(args.section.value, candidates)
    start, end = int(record["start_offset"]), int(record["end_offset"])
    section_text = canonical[start:end]
    if hashlib.sha256(section_text.encode("utf-8")).hexdigest() != record.get("section_sha256"):
        raise FilingSectionIntegrityError(entry.accession_number)
    if args.offset > len(section_text):
        raise ResearchError("offset_out_of_range", f"Offset {args.offset} exceeds section length {len(section_text)}.", "Use an offset within the section.")
    page = section_text[args.offset:args.offset + args.max_chars]
    next_offset = args.offset + len(page) if args.offset + len(page) < len(section_text) else None
    return FilingSectionOutput(text=page, total_chars=len(section_text), offset=args.offset,
        returned_chars=len(page), next_offset=next_offset, truncated=next_offset is not None,
        heading_text=record["heading_text"], section_sha256=record["section_sha256"],
        start_offset=start, end_offset=end, ticker=entry.ticker, cik=entry.cik,
        accession_number=entry.accession_number, form=entry.form, filing_date=entry.filing_date,
        report_date=entry.report_date, source_url=entry.source_url)
