from app.retrieval.embeddings import Embedder
from app.retrieval.repository import Repository
from app.retrieval.search import (SearchFilingChunksInput, SearchFilingChunksOutput,
                                  search_filing_chunks)


class SearchFilingChunksTool:
    """Dependency-injected contract for the search_filing_chunks domain tool."""

    def __init__(self, repository: Repository, embedder: Embedder):
        self.repository = repository
        self.embedder = embedder

    def __call__(self, request: SearchFilingChunksInput) -> SearchFilingChunksOutput:
        return search_filing_chunks(request, repository=self.repository, embedder=self.embedder)
