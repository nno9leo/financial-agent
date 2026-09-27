# ADR 0005 — PostgreSQL + pgvector for chunk storage and retrieval

- **Status:** Accepted (2026-09-25, implemented)
- **Date:** 2026-09-25
- **Handoff refs:** §6, §14, Q16

## Context
Corpus is tiny (two 10-Ks). The need is strict metadata filtering (ticker, accession, section) combined with vector similarity, reproducible local deployment, and a stack consistent with the FastAPI/PostgreSQL backend.

## Decision
- Store chunks with full provenance and embeddings in PostgreSQL + pgvector via Docker Compose, bound to 127.0.0.1.
- Retrieval always filters by accession (and optionally section) before ranking.
- Embeddings: `vector(384)`, cosine distance, model pinned per [ADR 0007](0007-local-embedding-model.md); rows carry the embedding model id + weight hash.
- **No ANN index** (HNSW/IVFFlat): with a few hundred rows an exact scan is fast and fully deterministic. Ties break on `evidence_id`.
- Rows are keyed by `(evidence_id, embedding_model_sha256)`. Search filters by current model hash and extractor/chunker version, so stale or mixed vector spaces are never ranked.
- Vector-only is the P0 baseline; BM25/hybrid only as a later single-variable experiment.

## Consequences
- (+) SQL filtering makes "top-k never crosses accession" enforceable and testable.
- (−) Requires Docker for local runs; not chosen for ANN scale.

## Evidence (fill when implemented)
- Schema / repository: `app/retrieval/schema.sql`, `app/retrieval/repository.py`.
- Idempotent re-ingestion: `tests/integration/test_search_repository.py`, plus the live re-run inserting 0 of 412 rows.
- Top-k never crosses accession: the same integration file (a more-similar chunk in the other accession is excluded), plus the smoke invariant.
- Retrieval baseline: `artifacts/smoke/retrieval_smoke.jsonl`, 4/6 answering chunk in top-5 (relevance judged manually by reading the top results).
