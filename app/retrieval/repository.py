from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain.errors import AccessionNotInManifestError, IngestCountMismatchError
from app.ingestion.chunking import CHUNKER_VERSION, Chunk
from app.ingestion.html_text import EXTRACTOR_VERSION
from app.ingestion.sections import SectionName


INSERT_SQL = """
INSERT INTO chunk_embeddings (
    evidence_id, embedding_model, embedding_model_sha256, chunk_id, ticker, cik,
    accession_number, filing_form, filing_date, section, heading_path, chunk_type,
    text, table_context, start_offset, end_offset, page_number, content_hash,
    token_count, source_url, canonical_sha256, extractor_version, chunker_version, embedding
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (evidence_id, embedding_model_sha256) DO NOTHING
"""


class Repository:
    """Small psycopg repository; every call opens and closes its own connection."""

    def __init__(self, database_url: str, *, connection_factory: Callable[..., Any] | None = None):
        self.database_url = database_url
        self._connection_factory = connection_factory or psycopg.connect

    def _connect(self, *, row_factory=None):
        if row_factory is None:
            return self._connection_factory(self.database_url)
        return self._connection_factory(self.database_url, row_factory=row_factory)

    def ensure_schema(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        statements = [part.strip() for part in schema_path.read_text("utf-8").split(";") if part.strip()]
        with self._connect() as connection:
            connection.execute(statements[0])  # Install the vector type before registering its adapter.
            register_vector(connection)
            for statement in statements[1:]:
                connection.execute(statement)

    def ingest_filing(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
        *,
        embedding_model: str,
        embedding_model_sha256: str,
        allowed_accessions: set[str],
        extractor_version: str = EXTRACTOR_VERSION,
        chunker_version: str = CHUNKER_VERSION,
    ) -> tuple[int, int]:
        if not chunks:
            return 0, 0
        accession = chunks[0].accession_number
        if accession not in allowed_accessions:
            raise AccessionNotInManifestError(accession)
        if any(chunk.accession_number != accession for chunk in chunks):
            raise ValueError("A filing transaction may contain chunks from exactly one accession.")
        if len(chunks) != len(embeddings):
            raise ValueError(f"Received {len(chunks)} chunks but {len(embeddings)} embeddings.")

        inserted = 0
        with self._connect() as connection:
            register_vector(connection)
            with connection.transaction():
                for chunk, values in zip(chunks, embeddings, strict=True):
                    cursor = connection.execute(INSERT_SQL, (
                        chunk.evidence_id, embedding_model, embedding_model_sha256,
                        chunk.chunk_id, chunk.ticker, chunk.cik, chunk.accession_number,
                        chunk.filing_form, chunk.filing_date, chunk.section.value,
                        Jsonb(chunk.heading_path), chunk.chunk_type, chunk.text,
                        chunk.table_context, chunk.start_offset, chunk.end_offset,
                        chunk.page_number, chunk.content_hash, chunk.token_count,
                        str(chunk.source_url), chunk.canonical_sha256,
                        chunk.extractor_version, chunk.chunker_version, Vector(values),
                    ))
                    inserted += max(cursor.rowcount, 0)
                cursor = connection.execute(
                    """SELECT count(*) AS row_count FROM chunk_embeddings
                       WHERE accession_number = %s AND extractor_version = %s
                         AND chunker_version = %s AND embedding_model_sha256 = %s""",
                    (accession, extractor_version, chunker_version, embedding_model_sha256),
                )
                observed = cursor.fetchone()[0]
                if observed != len(chunks):
                    raise IngestCountMismatchError(accession, len(chunks), observed)
        return inserted, len(chunks) - inserted

    def model_hashes_for_versions(
        self,
        accession_number: str,
        *,
        extractor_version: str = EXTRACTOR_VERSION,
        chunker_version: str = CHUNKER_VERSION,
    ) -> list[str]:
        with self._connect() as connection:
            cursor = connection.execute(
                """SELECT DISTINCT embedding_model_sha256 FROM chunk_embeddings
                   WHERE accession_number = %s AND extractor_version = %s
                     AND chunker_version = %s ORDER BY embedding_model_sha256""",
                (accession_number, extractor_version, chunker_version),
            )
            return [row[0] for row in cursor.fetchall()]

    def search_chunks(
        self,
        *,
        accession_number: str,
        query_vector: Sequence[float],
        embedding_model_sha256: str,
        sections: list[SectionName] | None,
        top_k: int,
        extractor_version: str = EXTRACTOR_VERSION,
        chunker_version: str = CHUNKER_VERSION,
    ) -> list[dict[str, Any]]:
        predicates = [
            "accession_number = %s",
            "embedding_model_sha256 = %s",
            "extractor_version = %s",
            "chunker_version = %s",
        ]
        params: list[Any] = [accession_number, embedding_model_sha256, extractor_version, chunker_version]
        if sections is not None:
            predicates.append("section = ANY(%s)")
            params.append([section.value for section in sections])
        # SELECT-list placeholders precede WHERE placeholders in SQL order.
        params = [Vector(query_vector), *params, top_k]
        sql = f"""SELECT evidence_id, ticker, cik, accession_number, filing_form, filing_date,
                         section, chunk_id, text, source_url, start_offset, end_offset,
                         page_number, content_hash, heading_path,
                         embedding <=> %s AS distance
                  FROM chunk_embeddings WHERE {' AND '.join(predicates)}
                  ORDER BY distance, evidence_id LIMIT %s"""
        with self._connect(row_factory=dict_row) as connection:
            register_vector(connection)
            cursor = connection.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def count_rows(self, accession_number: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute("SELECT count(*) FROM chunk_embeddings WHERE accession_number = %s", (accession_number,))
            return cursor.fetchone()[0]

    def chunk_metadata(self, evidence_ids: list[str], embedding_model_sha256: str) -> dict[str, dict[str, Any]]:
        if not evidence_ids:
            return {}
        with self._connect(row_factory=dict_row) as connection:
            cursor = connection.execute(
                """SELECT evidence_id, chunk_type, heading_path FROM chunk_embeddings
                   WHERE evidence_id = ANY(%s) AND embedding_model_sha256 = %s""",
                (evidence_ids, embedding_model_sha256),
            )
            return {row["evidence_id"]: {"chunk_type": row["chunk_type"],
                                         "heading_path": row["heading_path"]}
                    for row in cursor.fetchall()}


def mask_database_url(database_url: str) -> str:
    from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

    parsed = urlsplit(database_url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    has_password_query = any(key.lower() in {"password", "pass", "pwd"} for key, _ in query_pairs)
    if parsed.password is None and not has_password_query:
        return database_url
    user = parsed.username or ""
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host += f":{parsed.port}"
    auth = f"{user}:***@" if parsed.password is not None else ""
    netloc = auth + host
    masked_query = [(key, "***" if key.lower() in {"password", "pass", "pwd"} else value)
                    for key, value in query_pairs]
    return urlunsplit(SplitResult(parsed.scheme, netloc, parsed.path,
                                  urlencode(masked_query, doseq=True), parsed.fragment))
