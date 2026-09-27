import math
import re

import pytest

from app.config import Settings
from app.retrieval.embeddings import FastEmbedEmbedder, FakeEmbedder


def test_fake_embedder_counts_untruncated_tokens_and_is_deterministic():
    embedder = FakeEmbedder()
    long_text = "revenue growth 2025 " * 230
    assert embedder.count_tokens(long_text) > 600
    vector_a = embedder.embed_query("financial statement")
    vector_b = embedder.embed_query("financial statement")
    assert vector_a == vector_b and len(vector_a) == 384
    assert math.isclose(math.sqrt(sum(value * value for value in vector_a)), 1.0, rel_tol=1e-6)


@pytest.mark.model
def test_real_fastembed_model_hash_dimension_norm_and_untruncated_tokens():
    embedder = FastEmbedEmbedder(Settings(_env_file=None, fastembed_cache_dir="data/models"))
    vector = embedder.embed_query("financial statement")
    assert len(vector) == 384
    assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, rel_tol=1e-5)
    assert re.fullmatch(r"[0-9a-f]{64}", embedder.model_sha256)
    assert embedder.count_tokens("token " * 700) > 600
