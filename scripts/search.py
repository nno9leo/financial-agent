#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingestion.sections import SectionName
from app.retrieval.embeddings import FastEmbedEmbedder
from app.retrieval.repository import Repository
from app.retrieval.search import SearchFilingChunksInput, search_filing_chunks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--company", required=True)
    parser.add_argument("--accession-number")
    parser.add_argument("--section", action="append", choices=[section.value for section in SectionName])
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    settings = Settings()
    repository = Repository(settings.database_url)
    embedder = FastEmbedEmbedder(settings)
    sections = [SectionName(section) for section in args.section] if args.section is not None else None
    result = search_filing_chunks(
        SearchFilingChunksInput(question=args.question, company=args.company,
                                accession_number=args.accession_number, sections=sections,
                                top_k=args.top_k),
        repository=repository, embedder=embedder,
    )
    metadata = repository.chunk_metadata([item.evidence_id for item in result.evidence], result.embedding_model_sha256)
    for item in result.evidence:
        details = metadata[item.evidence_id]
        print(f"{item.evidence_id}\t{item.section}\t{item.retrieval_score:.6f}\t"
              f"{details['heading_path']}\t{item.text[:200]}")


if __name__ == "__main__":
    main()
