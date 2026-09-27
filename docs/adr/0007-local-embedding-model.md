# ADR 0007 — Local embedding model: `BAAI/bge-small-en-v1.5` via fastembed (ONNX)

- **Status:** Accepted (2026-09-25). Constraint: a local model with no new API key.
- **Date:** 2026-09-25
- **Related:** ADR 0005 (pgvector); each eval run's `environment.json` records the embedding model.

## Context

Retrieval needs dense embeddings for MD&A / Financial Statements chunks stored in pgvector (ADR 0005). Constraints:

- **No new credentials.** No embedding API key is used.
- **Tiny corpus:** two 10-Ks, on the order of a few hundred chunks. Throughput and ANN scale don't matter; install weight, reproducibility and a clean fresh-clone path do.
- **Offline, deterministic unit tests**. A model download must never be part of the normal test run.
- **Runs in WSL on CPU.** A Windows/PowerShell path is also supported, so there is no GPU assumption.
- Embedding quality is not the project's innovation. A common, well-known baseline is preferable to a tuned choice that's hard to explain.

## Decision

- **Model:** `BAAI/bge-small-en-v1.5`. English, 384-dim, MIT license, 512-token input limit.
- **Runtime:** `fastembed==0.8.1` (Qdrant), which runs the model as quantized ONNX (`Qdrant/bge-small-en-v1.5-onnx-Q`, `model_optimized.onnx`, about 0.067 GB) on `onnxruntime`. There is no PyTorch dependency. Verified by inspecting the 0.8.1 wheel on 2026-09-25: this model is fastembed's default and is declared with `dim=384`, `license="mit"`, `size_in_GB=0.067`.
- **Interface:** all code depends on a small `Embedder` protocol in `app/retrieval/embeddings.py`, with `model_id`, `dim`, `embed_documents(texts)` and `embed_query(text)`. `FastEmbedEmbedder` is the only production implementation. Unit tests use a deterministic fake embedder (hash-based vectors of the right dim), so they never download or load the model.
- **Similarity:** normalized vectors with cosine distance (`<=>`) in pgvector, column type `vector(384)`.
- **Query prefix:** none. bge v1.5 was trained so that its retrieval instruction prefix is optional. The choice is fixed in config (`EMBED_QUERY_PREFIX=""`), so adding a prefix later is a single-variable A/B change, not a silent drift.
- **Provenance:** every stored chunk row records `embedding_model` (model id) and `embedding_model_sha256` (sha256 of the ONNX file actually loaded). Search refuses to rank rows whose model id/hash differs from the query embedder's, raising a typed error rather than mixing vector spaces. `environment.json` for eval runs records the same pair.
- **Token limit is a hard invariant:** the model silently truncates input beyond 512 tokens, which would drop text from a chunk without any error. The chunker must measure chunk length with this model's tokenizer and **split** any chunk that would exceed the limit. A test asserts that no stored chunk exceeds 512 tokens including special tokens.
- **Model weights are downloaded on first use** from Hugging Face into a gitignored cache directory (`FASTEMBED_CACHE_DIR`, default `data/models/`). This is a data download, not an executable script. Only real-model integration tests (`@pytest.mark.model`, deselected by default) and the ingestion script trigger it.

## Alternatives considered

| Option | Why not chosen |
|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | Smaller and very common, but noticeably weaker on retrieval benchmarks than bge-small at the same 384 dim. The saving in size is irrelevant at this corpus size. |
| `BAAI/bge-base-en-v1.5` (768-dim) | Better quality, but about 3× the size and compute. A reasonable **later single-variable experiment** (small → base) if baseline failure analysis shows retrieval misses dominate. Not the starting point. |
| `nomic-embed-text-v1.5` (long context) | Longer context isn't needed because chunks are ≤512 tokens by design. It requires task prefixes and a larger download. |
| `sentence-transformers` + PyTorch | Same model family, but the default Linux PyTorch wheel pulls a multi-GB CUDA stack. A fresh clone becomes slow and fragile, and CPU-only index URLs complicate setup. |
| Hosted embedding API (OpenAI, Voyage, …) | Needs a new API key, adds network/cost variance to ingestion, and conflicts with offline re-runs. |

## Consequences

- (+) No credentials, CPU-only, about 67 MB of weights. The install stays light, and the fresh-clone path is `pip install` plus one automatic model download.
- (+) The vector space is pinned by model id + weight hash. Changing the model is detectable and forces re-ingestion rather than silently mixing spaces.
- (+) Unit tests stay offline and fast through the fake embedder.
- (−) The quantized ONNX weights are not bit-identical to the original FP32 PyTorch model. Scores may differ slightly from published numbers. This is acceptable because we compare runs against each other on the same pinned weights, not against external leaderboards.
- (−) English-only, and 512-token chunks mean long MD&A passages are split across chunks. Section/heading metadata on each chunk mitigates lost context.
- (−) The first ingestion needs network access to Hugging Face. It is documented in the README fresh-clone steps.

## Evidence (fill when implemented)

- `Embedder` protocol + `FastEmbedEmbedder` + fake embedder: `app/retrieval/embeddings.py`, `tests/unit/test_embeddings.py`
- 512-token chunk invariant test: `tests/unit/test_chunking.py`
- Model-mismatch typed error test: `tests/unit/test_search.py`
- `environment.json` records `embedding.model_id` and `embedding.weights_sha256` (e.g. `artifacts/evals/cp6b-baseline-v1/environment.json`)
