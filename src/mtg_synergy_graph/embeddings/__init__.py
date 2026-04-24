"""Content-embedding substrate (plan 2026-04-23-003).

Hand-rolled TF-IDF + truncated-SVD over structured per-card features.
Unit 1 ships the pure-function vectorizer; Unit 2 adds the schema,
config hash, and blob store; subsequent units add commander-target
caching, inference-path reader, dedup diagnostic, and the flag-gated
scoring term.
"""

from __future__ import annotations

from mtg_synergy_graph.embeddings.commander_target import (
    build_commander_target_vector,
    clear_cache,
)
from mtg_synergy_graph.embeddings.config import (
    EmbeddingConfigError,
    EmbeddingConfigInputs,
    EmbeddingConfigMissingError,
    EmbeddingConfigStaleError,
    compute_embedding_hash,
    get_embedding_config_inputs,
    verify_current_or_raise,
)
from mtg_synergy_graph.embeddings.store import (
    load_card_embeddings,
    read_vector,
    write_vectors,
)
from mtg_synergy_graph.embeddings.svd import truncated_svd
from mtg_synergy_graph.embeddings.vectorizer import (
    TOKEN_FORMAT_VERSION,
    TfidfResult,
    compute_tfidf,
    extract_card_tokens,
)

__all__ = [
    "TOKEN_FORMAT_VERSION",
    "EmbeddingConfigError",
    "EmbeddingConfigInputs",
    "EmbeddingConfigMissingError",
    "EmbeddingConfigStaleError",
    "TfidfResult",
    "build_commander_target_vector",
    "clear_cache",
    "compute_embedding_hash",
    "compute_tfidf",
    "extract_card_tokens",
    "get_embedding_config_inputs",
    "load_card_embeddings",
    "read_vector",
    "truncated_svd",
    "verify_current_or_raise",
    "write_vectors",
]
