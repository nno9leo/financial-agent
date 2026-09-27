"""Deterministic Markdown and self-contained HTML report rendering."""
from __future__ import annotations

import html
import json
from hashlib import sha256
from pathlib import Path

from app.domain.errors import ReportDraftNotFoundError


def load_draft(reports_dir: Path, draft_id: str) -> dict:
    path = Path(reports_dir) / "drafts" / f"{draft_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportDraftNotFoundError(draft_id) from exc


def render_report(draft: dict, format: str, acknowledge_unsupported: bool = False) -> tuple[str, str]:
    if format not in {"markdown", "html"}:
        raise ValueError("format must be markdown or html")
    verifications = draft["verifications"]
    supported = [v for v in verifications if v["verdict"] == "supported"]
    partial = [v for v in verifications if v["verdict"] == "partially_supported"]
    unsupported = [v for v in verifications if v["verdict"] == "unsupported"]
    evidence = {e["evidence_id"]: e for e in draft["evidence"]}
    accession = next((e["accession_number"] for e in draft["evidence"]), "Unavailable")
    form = next((e.get("form") for e in draft["evidence"] if e.get("form")), "10-K")
    report_date = next((e.get("report_date") for e in draft["evidence"] if e.get("report_date")), "Unavailable")
    title = f"{draft['ticker']} Research Report — {accession} ({form}, {report_date})"
    if format == "markdown":
        lines = [f"# {title}", "", "## Findings", ""]
        for item in supported:
            lines.extend([item["claim_text"], ""])
            lines.extend(_markdown_citations(item, evidence))
            lines.append("")
        lines.extend(["## Partially supported", ""])
        for item in partial:
            lines.extend([f"- {item['claim_text']} — {item['reason']}"])
        if not partial:
            lines.append("None.")
        if acknowledge_unsupported and unsupported:
            lines.extend(["", "## Removed — not supported by evidence", ""])
            lines.extend(f"- {item['claim_text']} — {item['reason']}" for item in unsupported)
        lines.extend(["", f"Judge: {draft['judge_id']}", "Narrative verdicts are model judgments; numeric/metadata verdicts are deterministic.", ""])
        content = "\n".join(lines)
    else:
        parts = ["<!doctype html><html><head><meta charset=\"utf-8\"><title>", html.escape(title),
                 "</title></head><body><h1>", html.escape(title), "</h1><section><h2>Findings</h2>"]
        for item in supported:
            parts.extend(["<article><p>", html.escape(item["claim_text"]), "</p><ul>"])
            for line in _citation_rows(item, evidence):
                parts.extend(["<li>", html.escape(line), "</li>"])
            parts.append("</ul></article>")
        parts.append("</section><section><h2>Partially supported</h2><ul>")
        if partial:
            for item in partial:
                parts.extend(["<li>", html.escape(item["claim_text"]), " — ", html.escape(item["reason"]), "</li>"])
        else:
            parts.append("<li>None.</li>")
        parts.append("</ul></section>")
        if acknowledge_unsupported and unsupported:
            parts.append("<section><h2>Removed — not supported by evidence</h2><ul>")
            for item in unsupported:
                parts.extend(["<li>", html.escape(item["claim_text"]), " — ", html.escape(item["reason"]), "</li>"])
            parts.append("</ul></section>")
        parts.extend(["<footer><p>Judge: ", html.escape(draft["judge_id"]),
                      "</p><p>Narrative verdicts are model judgments; numeric/metadata verdicts are deterministic.</p></footer></body></html>"])
        content = "".join(parts)
    return content, sha256(content.encode("utf-8")).hexdigest()


def _citation_rows(item: dict, evidence: dict[str, dict]) -> list[str]:
    rows = []
    for evidence_id in item["evidence_ids"]:
        row = evidence.get(evidence_id, {})
        citation = f"{evidence_id}; section: {row.get('section') or row.get('kind', 'evidence')}; source: {row.get('source_url') or 'structured evidence'}"
        metric = row.get("metric")
        if metric:
            citation += f"; formula: {metric.get('formula')}; value: {metric.get('display_value', metric.get('result'))}"
            for fact in metric.get("inputs", []):
                citation += f"; input: {fact.get('tag')}={fact.get('value')} {fact.get('unit')} ({fact.get('source_url')})"
        fact = row.get("fact")
        if fact:
            citation += f"; fact: {fact.get('tag')}={fact.get('value')} {fact.get('unit')}"
        rows.append(citation)
    return rows


def _markdown_citations(item: dict, evidence: dict[str, dict]) -> list[str]:
    return [f"- {row}" for row in _citation_rows(item, evidence)]
