CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    evidence_id text NOT NULL,
    embedding_model text NOT NULL,
    embedding_model_sha256 text NOT NULL,
    chunk_id text NOT NULL,
    ticker text NOT NULL,
    cik text NOT NULL,
    accession_number text NOT NULL,
    filing_form text NOT NULL,
    filing_date date NOT NULL,
    section text NOT NULL,
    heading_path jsonb NOT NULL,
    chunk_type text NOT NULL,
    text text NOT NULL,
    table_context text,
    start_offset integer NOT NULL,
    end_offset integer NOT NULL,
    page_number integer,
    content_hash text NOT NULL,
    token_count integer NOT NULL,
    source_url text NOT NULL,
    canonical_sha256 text NOT NULL,
    extractor_version text NOT NULL,
    chunker_version text NOT NULL,
    embedding vector(384) NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (evidence_id, embedding_model_sha256)
);

CREATE INDEX IF NOT EXISTS chunk_embeddings_accession_section_idx
    ON chunk_embeddings (accession_number, section);
