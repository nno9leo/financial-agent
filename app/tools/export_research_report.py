"""Preview and exclusively create verified research reports."""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import (ReportAlreadyExistsError, ReportPathError, ReportRenderMismatchError,
                               UnsupportedClaimsRequireAcknowledgementError)
from app.reports.render import load_draft, render_report


class ExportResearchReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_draft_id: str = Field(min_length=1)
    format: Literal["markdown", "html"]
    output_path: str
    acknowledge_unsupported: bool = False


class ExportResearchReportWriteInput(ExportResearchReportInput):
    preview_content_sha256: str


class ExportResearchReportPreview(BaseModel):
    report_draft_id: str
    format: Literal["markdown", "html"]
    output_path: str
    content_sha256: str
    supported_claim_count: int
    partially_supported_claim_count: int
    unsupported_claim_count: int
    evidence_count: int
    preview_text: str
    requires_unsupported_acknowledgement: bool


class ExportResearchReportWrite(BaseModel):
    path: str
    content_sha256: str
    supported_claim_count: int
    partially_supported_claim_count: int
    unsupported_claim_count: int
    generated_at: str
    report_draft_id: str


def _validate_target(reports_dir: Path, output_path: str, format: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.(md|html)", output_path):
        raise ReportPathError()
    if Path(output_path).name != output_path or "/" in output_path or "\\" in output_path or ".." in output_path:
        raise ReportPathError()
    expected = "md" if format == "markdown" else "html"
    if not output_path.endswith("." + expected):
        raise ReportPathError("Report filename extension must match the selected format.")
    root = Path(reports_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / output_path
    try:
        resolved = target.resolve(strict=False)
        if resolved.parent != root:
            raise ReportPathError()
    except OSError as exc:
        raise ReportPathError() from exc
    if target.is_symlink():
        raise ReportPathError("Symlink report targets are not allowed.")
    if target.exists():
        raise ReportAlreadyExistsError(output_path)
    return target, output_path


def preview_export(request: ExportResearchReportInput | dict, *, reports_dir: Path) -> ExportResearchReportPreview:
    args = request if isinstance(request, ExportResearchReportInput) else ExportResearchReportInput.model_validate(request)
    _, normalized = _validate_target(reports_dir, args.output_path, args.format)
    draft = load_draft(reports_dir, args.report_draft_id)
    content, content_hash = render_report(draft, args.format, args.acknowledge_unsupported)
    counts = _counts(draft)
    return ExportResearchReportPreview(report_draft_id=args.report_draft_id, format=args.format,
        output_path=normalized, content_sha256=content_hash, supported_claim_count=counts[0],
        partially_supported_claim_count=counts[1], unsupported_claim_count=counts[2],
        evidence_count=len(draft["evidence"]), preview_text=content[:1500],
        requires_unsupported_acknowledgement=counts[2] > 0)


def write_export(request: ExportResearchReportInput | dict, *, reports_dir: Path,
                 expected_content_sha256: str) -> ExportResearchReportWrite:
    args = request if isinstance(request, ExportResearchReportInput) else ExportResearchReportInput.model_validate(request)
    target, normalized = _validate_target(reports_dir, args.output_path, args.format)
    draft = load_draft(reports_dir, args.report_draft_id)
    counts = _counts(draft)
    if counts[2] and not args.acknowledge_unsupported:
        raise UnsupportedClaimsRequireAcknowledgementError()
    content, content_hash = render_report(draft, args.format, args.acknowledge_unsupported)
    if content_hash != expected_content_sha256:
        raise ReportRenderMismatchError()
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, target)
        except FileExistsError as exc:
            raise ReportAlreadyExistsError(normalized) from exc
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return ExportResearchReportWrite(path=normalized, content_sha256=content_hash,
        supported_claim_count=counts[0], partially_supported_claim_count=counts[1],
        unsupported_claim_count=counts[2], generated_at=datetime.now(timezone.utc).isoformat(),
        report_draft_id=args.report_draft_id)


def _counts(draft: dict) -> tuple[int, int, int]:
    values = [v["verdict"] for v in draft["verifications"]]
    return values.count("supported"), values.count("partially_supported"), values.count("unsupported")
