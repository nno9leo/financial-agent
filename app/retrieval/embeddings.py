from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Protocol, Sequence

from app.config import Settings
from app.domain.errors import EmbeddingModelError

MODEL_DIM = 384
FAKE_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class Embedder(Protocol):
    model_id: str
    model_sha256: str
    dim: int

    def count_tokens(self, text: str) -> int: ...
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Offline deterministic test embedder; not suitable for retrieval quality."""

    model_id = "fake/hash-v1"
    model_sha256 = hashlib.sha256(model_id.encode()).hexdigest()
    dim = MODEL_DIM

    def count_tokens(self, text: str) -> int:
        return len(FAKE_TOKEN_PATTERN.findall(text)) + 2

    def token_boundaries(self, text: str) -> list[int]:
        return [match.end() for match in FAKE_TOKEN_PATTERN.finditer(text)]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        values = []
        seed = text.encode("utf-8")
        counter = 0
        while len(values) < self.dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(digest), 4):
                raw = int.from_bytes(digest[offset:offset + 4], "big")
                values.append((raw / 0xFFFFFFFF) * 2 - 1)
                if len(values) == self.dim:
                    break
            counter += 1
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]


class FastEmbedEmbedder:
    dim = MODEL_DIM

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.model_id = self.settings.embedding_model_id
        if self.model_id != "BAAI/bge-small-en-v1.5":
            raise EmbeddingModelError(f"The chunk index is pinned to BAAI/bge-small-en-v1.5, got {self.model_id!r}.")
        if self.settings.embedding_query_prefix != "":
            raise EmbeddingModelError("ADR 0007 requires an empty embedding query prefix.")
        try:
            from fastembed import TextEmbedding
            from tokenizers import Tokenizer
            self._model = TextEmbedding(model_name=self.model_id, cache_dir=str(self.settings.fastembed_cache_dir))
            model_impl = self._model.model
            model_dir = Path(model_impl._model_dir)
            model_file = model_dir / model_impl.model_description.model_file
            if not model_file.is_file():
                raise EmbeddingModelError(f"Loaded ONNX model file not found: {model_file}")
            digest = hashlib.sha256()
            with model_file.open("rb") as stream:
                for piece in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(piece)
            self.model_sha256 = digest.hexdigest()
            tokenizer_path = model_dir / "tokenizer.json"
            if not tokenizer_path.is_file():
                raise EmbeddingModelError(f"Loaded model tokenizer not found: {tokenizer_path}")
            self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
            self._tokenizer.no_truncation()
        except EmbeddingModelError:
            raise
        except Exception as exc:
            raise EmbeddingModelError(f"Could not load pinned FastEmbed model/tokenizer: {exc}") from exc

    def count_tokens(self, text: str) -> int:
        return len(self._tokenizer.encode(text, add_special_tokens=True).ids)

    def token_boundaries(self, text: str) -> list[int]:
        encoding = self._tokenizer.encode(text, add_special_tokens=True)
        return [end for start, end in encoding.offsets if end > start]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._checked_vector(vector) for vector in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        prefix = self.settings.embedding_query_prefix
        return self._checked_vector(next(iter(self._model.query_embed(prefix + text))))

    def _checked_vector(self, vector) -> list[float]:
        values = [float(value) for value in vector]
        if len(values) != self.dim:
            raise EmbeddingModelError(f"Expected {self.dim}-dimensional vector, received {len(values)}.")
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise EmbeddingModelError("Embedding model returned a zero vector.")
        normalized = [value / norm for value in values]
        return normalized
