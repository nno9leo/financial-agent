#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingestion.chunking import embedding_input_for_chunk
from app.ingestion.manifest import load_manifest
from app.ingestion.pipeline import build_filing_pipeline
from app.providers.sec import SecProvider
from app.retrieval.embeddings import FastEmbedEmbedder
from app.retrieval.repository import Repository, mask_database_url
from app.ingestion.manifest import latest_manifest_path


def run(*, root: Path = ROOT, repository: Repository | None = None,
        embedder=None) -> list[tuple[str, int, int]]:
    root = Path(root)
    settings = Settings()
    manifest_path = latest_manifest_path(root / "data/manifests")
    manifest = load_manifest(manifest_path)
    vectorizer = embedder or FastEmbedEmbedder(settings)
    repo = repository or Repository(settings.database_url)
    print(f"DATABASE_URL {mask_database_url(settings.database_url)}")
    repo.ensure_schema()
    provider = SecProvider(settings)
    output: list[tuple[str, int, int]] = []
    try:
        allowed = {entry.accession_number for entry in manifest.entries}
        for entry in manifest.entries:
            cached = provider.get_cached(entry.source_url)
            if cached is None:
                raise FileNotFoundError(f"Pinned filing is not present in the raw cache: {entry.source_url}")
            html, _ = cached
            result = build_filing_pipeline(
                entry, html, vectorizer,
                target_tokens=settings.chunk_target_tokens, max_tokens=settings.chunk_max_tokens,
            )
            embedding_inputs = [embedding_input_for_chunk(chunk) for chunk in result.chunks]
            embeddings = vectorizer.embed_documents(embedding_inputs)
            inserted, skipped = repo.ingest_filing(
                result.chunks, embeddings,
                embedding_model=vectorizer.model_id,
                embedding_model_sha256=vectorizer.model_sha256,
                allowed_accessions=allowed,
            )
            print(f"FILING {entry.ticker} {entry.accession_number} inserted={inserted} skipped={skipped}")
            output.append((entry.accession_number, inserted, skipped))
    finally:
        provider.close()
    return output


if __name__ == "__main__":
    run()
