"""Content-embedding substrate (plan 2026-04-23-003).

Hand-rolled TF-IDF + truncated-SVD over structured per-card features.
Unit 1 ships the pure-function vectorizer; Unit 2 adds the schema,
config hash, and blob store; subsequent units add commander-target
caching, inference-path reader, dedup diagnostic, and the flag-gated
scoring term.

Re-exports are resolved lazily (PEP 562, 2026-06-09): most submodules
import numpy at module scope, but the rule-only scoring path
(embedding flag off) must stay importable on a base install where
numpy is absent — numpy ships with the optional ``[graph]`` extra.
Eager imports here used to drag numpy into every
``score_all_universal`` call via the package init.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mtg_synergy_graph.embeddings.commander_target import (
        build_commander_target_vector,
        clear_cache,
    )
    from mtg_synergy_graph.embeddings.config import (
        EmbeddingConfigCorruptError,
        EmbeddingConfigError,
        EmbeddingConfigInputs,
        EmbeddingConfigMissingError,
        EmbeddingConfigStaleError,
        compute_embedding_hash,
        get_embedding_config_inputs,
        read_stored_config,
        verify_current_or_raise,
    )
    from mtg_synergy_graph.embeddings.contribution import (
        embedding_contribution,
        load_card_embeddings_verified,
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

#: name → submodule providing it. Drives the lazy ``__getattr__``.
_EXPORTS: dict[str, str] = {
    "build_commander_target_vector": "commander_target",
    "clear_cache": "commander_target",
    "EmbeddingConfigCorruptError": "config",
    "EmbeddingConfigError": "config",
    "EmbeddingConfigInputs": "config",
    "EmbeddingConfigMissingError": "config",
    "EmbeddingConfigStaleError": "config",
    "compute_embedding_hash": "config",
    "get_embedding_config_inputs": "config",
    "read_stored_config": "config",
    "verify_current_or_raise": "config",
    "embedding_contribution": "contribution",
    "load_card_embeddings_verified": "contribution",
    "load_card_embeddings": "store",
    "read_vector": "store",
    "write_vectors": "store",
    "truncated_svd": "svd",
    "TOKEN_FORMAT_VERSION": "_constants",
    "TfidfResult": "vectorizer",
    "compute_tfidf": "vectorizer",
    "extract_card_tokens": "vectorizer",
}

__all__ = [
    "TOKEN_FORMAT_VERSION",
    "EmbeddingConfigCorruptError",
    "EmbeddingConfigError",
    "EmbeddingConfigInputs",
    "EmbeddingConfigMissingError",
    "EmbeddingConfigStaleError",
    "TfidfResult",
    "build_commander_target_vector",
    "clear_cache",
    "compute_embedding_hash",
    "compute_tfidf",
    "embedding_contribution",
    "extract_card_tokens",
    "get_embedding_config_inputs",
    "load_card_embeddings",
    "load_card_embeddings_verified",
    "read_stored_config",
    "read_vector",
    "truncated_svd",
    "verify_current_or_raise",
    "write_vectors",
]


def __getattr__(name: str) -> object:
    submodule = _EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    # Cache so subsequent lookups (and monkeypatching) hit the module
    # dict directly instead of re-entering this hook.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
