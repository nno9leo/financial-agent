#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingestion.manifest import load_manifest
from app.retrieval.embeddings import FastEmbedEmbedder
from app.retrieval.repository import Repository
from app.ingestion.manifest import latest_manifest_path
from app.retrieval.search import SearchFilingChunksInput, search_filing_chunks


def _run_queries(queries, *, manifest, repository, embedder):
    results = []
    pinned = {entry.ticker: entry.accession_number for entry in manifest.entries}
    for query in queries:
        result = search_filing_chunks(
            SearchFilingChunksInput(question=query["question"], company=query["company"], top_k=5),
            repository=repository, embedder=embedder, manifest=manifest,
        )
        expected_accession = pinned[query["company"]]
        assert result.accession_number == expected_accession
        assert all(evidence.accession_number == expected_accession for evidence in result.evidence)
        metadata = repository.chunk_metadata([item.evidence_id for item in result.evidence], result.embedding_model_sha256)
        hits = [[item.evidence_id, item.section, metadata[item.evidence_id]["chunk_type"],
                 item.retrieval_score, metadata[item.evidence_id]["heading_path"], item.text[:300]]
                for item in result.evidence]
        results.append({"id": query["id"], "accession_number": result.accession_number, "top5": hits})
    return results


def run(*, root: Path = ROOT):
    root = Path(root)
    settings = Settings()
    query_path = root / "evals/smoke/retrieval_queries.json"
    queries = json.loads(query_path.read_text("utf-8"))
    manifest = load_manifest(latest_manifest_path(root / "data/manifests"))
    repository = Repository(settings.database_url)
    embedder = FastEmbedEmbedder(settings)
    first = _run_queries(queries, manifest=manifest, repository=repository, embedder=embedder)
    second = _run_queries(queries, manifest=manifest, repository=repository, embedder=embedder)
    assert first == second, "Smoke search results changed across consecutive runs."
    output = root / "artifacts/smoke/retrieval_smoke.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in first), encoding="utf-8")
    print("ID\tACCESSION\tHITS")
    for row in first:
        print(f"{row['id']}\t{row['accession_number']}\t{len(row['top5'])}")
    print(f"WROTE {output}")
    return output


if __name__ == "__main__":
    run()
