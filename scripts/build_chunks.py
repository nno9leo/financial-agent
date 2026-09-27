#!/usr/bin/env python3
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingestion.pipeline import build_filing_pipeline
from app.ingestion.manifest import load_manifest
from app.providers.sec import SecProvider
from app.retrieval.embeddings import FastEmbedEmbedder


def _write_outputs(result, entry, output_root: Path) -> Path:
    target = output_root / entry.accession_number
    target.mkdir(parents=True, exist_ok=True)
    (target / "canonical.txt").write_text(result.canonical_text, encoding="utf-8", newline="\n")
    section_payload = {name.value: record.model_dump(mode="json") for name, record in sorted(result.sections.items(), key=lambda pair: pair[0].value)}
    (target / "sections.json").write_text(json.dumps(section_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with (target / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for chunk in result.chunks:
            stream.write(json.dumps(chunk.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n")
    return target


def _print_summary(result, entry) -> None:
    print(f"FILING {entry.ticker} {entry.accession_number} canonical_chars={len(result.canonical_text)} canonical_sha256={result.canonical_sha256}")
    print(f"RUNNING_HEADERS {entry.ticker} {json.dumps(result.removed_patterns, sort_keys=True)}")
    for name, section in sorted(result.sections.items(), key=lambda pair: pair[0].value):
        chunks = [chunk for chunk in result.chunks if chunk.section == name]
        counts = Counter(chunk.chunk_type for chunk in chunks)
        tokens = [chunk.token_count for chunk in chunks]
        median = statistics.median(tokens) if tokens else 0
        lines = result.canonical_text[section.start_offset:section.end_offset].splitlines()
        longest_line = max((len(line) for line in lines), default=0)
        long_lines = sum(len(line) > 3_000 for line in lines)
        print(f"SECTION {name.value} heading={section.heading_text!r} chars={section.end_offset-section.start_offset} chunks_text={counts['text']} chunks_table={counts['table']} token_count_min_median_max={min(tokens) if tokens else 0}/{median}/{max(tokens) if tokens else 0} longest_line_chars={longest_line} lines_over_3000={long_lines}")


def build_all(*, root: Path = ROOT, embedder=None) -> list[Path]:
    root = Path(root)
    settings = Settings(_env_file=None, data_dir=root / "data", fastembed_cache_dir=root / "data/models")
    manifest = load_manifest(root / "data/manifests/filings.v1.json")
    vectorizer = embedder or FastEmbedEmbedder(settings)
    provider = SecProvider(settings)
    outputs = []
    try:
        for entry in manifest.entries:
            html, _provenance = provider.get_cached_or_fetch(entry.source_url)
            result = build_filing_pipeline(entry, html, vectorizer,
                target_tokens=settings.chunk_target_tokens, max_tokens=settings.chunk_max_tokens)
            outputs.append(_write_outputs(result, entry, root / "data/normalized"))
            _print_summary(result, entry)
    finally:
        provider.close()
    return outputs


if __name__ == "__main__":
    build_all()
