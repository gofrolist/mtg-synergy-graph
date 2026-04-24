"""Content-embedding substrate (plan 2026-04-23-003).

Hand-rolled TF-IDF + truncated-SVD over structured per-card features.
Unit 1 ships the pure-function vectorizer; subsequent units add DB
persistence, commander-target caching, inference-path reader, dedup
diagnostic, and the flag-gated scoring term.
"""

from __future__ import annotations

from mtg_synergy_graph.embeddings.svd import truncated_svd
from mtg_synergy_graph.embeddings.vectorizer import (
    TOKEN_FORMAT_VERSION,
    TfidfResult,
    compute_tfidf,
    extract_card_tokens,
)

__all__ = [
    "TOKEN_FORMAT_VERSION",
    "TfidfResult",
    "compute_tfidf",
    "extract_card_tokens",
    "truncated_svd",
]
