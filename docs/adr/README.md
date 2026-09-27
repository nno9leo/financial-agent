# Architecture Decision Records

Short ADRs for the project's key design decisions. Status starts as **Proposed** and moves to **Accepted** once the implementing module has landed and its tests back the decision. The "Evidence" section must point at real code/tests/artifacts — never at target numbers.

| # | Decision | Status |
|---|---|---|
| [0001](0001-structured-facts-vs-text-retrieval.md) | Structured numbers (companyfacts) separate from text retrieval | Proposed |
| [0002](0002-pin-accession.md) | Pin accession numbers (incl. companyfacts snapshot drift) | Accepted |
| [0003](0003-single-agent.md) | Single agent with explicit query plan | Proposed |
| [0004](0004-two-layer-verifier.md) | Citation verifier: deterministic layer before semantic layer | Proposed |
| [0005](0005-pgvector.md) | PostgreSQL + pgvector for chunk storage (exact scan) | Accepted |
| [0006](0006-eval-data-composition.md) | Eval dataset composition | Proposed |
| [0007](0007-local-embedding-model.md) | Local embedding model: bge-small-en-v1.5 via fastembed (ONNX) | Accepted |
| [0008](0008-agent-runtime-model.md) | Agent runtime model: Claude Sonnet 5 (claude-sonnet-5, thinking medium) via Pi, pinned per run | Accepted |
| [0009](0009-semantic-judge-model.md) | Semantic judge: GPT-6 Sol (reasoning high) via OpenRouter, separate from answering model | Accepted |
