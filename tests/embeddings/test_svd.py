"""Unit 1 tests — truncated-SVD + row-wise L2 normalization.

Covers:

* Deterministic output: same input produces bitwise-identical output
  across two calls (``np.array_equal`` contract).
* L2-normalized rows: ``||row||_2`` is ``1.0`` within float tolerance
  for non-zero-norm inputs.
* Zero-norm rows survive normalization as all-zeros, not NaN.
* ``k`` is clamped when the input matrix is smaller than ``k`` columns.
* Integration with the vectorizer: 50 synthetic cards → 50 distinct
  unit-norm 128-dim vectors.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np

from mtg_synergy_graph.embeddings.svd import truncated_svd
from mtg_synergy_graph.embeddings.vectorizer import (
    compute_tfidf,
    extract_card_tokens,
)

#: Same minimal schema as ``test_vectorizer.py`` — kept local to avoid
#: coupling to conftest, per Unit 1 scope.
_SCHEMA = """\
CREATE TABLE cards (
    name     TEXT PRIMARY KEY,
    keywords TEXT
);
CREATE TABLE card_ports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name        TEXT NOT NULL,
    port_type        TEXT NOT NULL,
    event_class      TEXT NOT NULL,
    zone_origin      TEXT,
    zone_destination TEXT,
    counter_type     TEXT,
    branch_kind      TEXT
);
CREATE TABLE port_attributes (
    port_id    INTEGER NOT NULL,
    attr_kind  TEXT NOT NULL,
    attr_value TEXT NOT NULL
);
"""


def _seed_fifty_card_db() -> sqlite3.Connection:
    """Build a 50-card DB with varied port/keyword shapes.

    Each card has a 3-port fingerprint drawn from a 10-element pool so
    card vectors differ pair-wise while sharing enough tokens to exceed
    ``min_df=2``. Also seeds a single keyword per card.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)

    port_pool = [
        ("trigger", "SpellCast"),
        ("trigger", "ETB"),
        ("trigger", "Dies"),
        ("effect", "Draw"),
        ("effect", "DealDamage"),
        ("effect", "Token"),
        ("static", "Lord"),
        ("cost", "Sacrifice"),
        ("replacement", "ChangeZone"),
        ("keyword", "Flying"),
    ]
    keyword_pool = [
        "Flying",
        "Haste",
        "Trample",
        "Vigilance",
        "Lifelink",
        "Deathtouch",
        "Reach",
        "Menace",
        "Ward",
        "Indestructible",
    ]

    for i in range(50):
        card_name = f"Card{i:03d}"
        # Each card gets a 2-keyword multiset unique to its index so the
        # combination ``(port_signature, keyword_signature)`` is
        # guaranteed pairwise-distinct across 50 cards. Keywords appear
        # on ≥ 2 cards so they survive ``min_df=2``.
        kw_a = keyword_pool[i % len(keyword_pool)]
        kw_b = keyword_pool[(i // len(keyword_pool)) % len(keyword_pool)]
        conn.execute(
            "INSERT INTO cards (name, keywords) VALUES (?, ?)",
            (card_name, json.dumps([kw_a, kw_b])),
        )
        # 3-port fingerprint drawn from the shared pool — ports repeat
        # across cards (shared structure is what makes TF-IDF useful).
        picks = [
            port_pool[i % len(port_pool)],
            port_pool[(i * 3 + 1) % len(port_pool)],
            port_pool[(i * 7 + 2) % len(port_pool)],
        ]
        for pt, ev in picks:
            conn.execute(
                "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
                (card_name, pt, ev),
            )
    conn.commit()
    return conn


def test_truncated_svd_is_deterministic() -> None:
    rng = np.random.default_rng(seed=42)
    tfidf = rng.standard_normal((30, 20)).astype(np.float64)

    first = truncated_svd(tfidf, k=8)
    second = truncated_svd(tfidf, k=8)

    assert np.array_equal(first, second)


def test_truncated_svd_rows_are_unit_norm() -> None:
    rng = np.random.default_rng(seed=7)
    tfidf = np.abs(rng.standard_normal((25, 40))).astype(np.float64)

    embedding = truncated_svd(tfidf, k=16)

    assert embedding.shape == (25, 16)
    norms = np.linalg.norm(embedding, axis=1)
    np.testing.assert_allclose(norms, np.ones(25), atol=1e-9)


def test_truncated_svd_handles_zero_row_without_nan() -> None:
    tfidf = np.array(
        [
            [1.0, 2.0, 3.0],
            [0.0, 0.0, 0.0],  # zero row — would NaN under a naive divide
            [4.0, 5.0, 6.0],
        ],
        dtype=np.float64,
    )

    embedding = truncated_svd(tfidf, k=2)

    assert not np.isnan(embedding).any()
    # The zero-input row must remain zero after normalization.
    assert np.all(embedding[1] == 0.0)
    # Non-zero rows are unit-norm.
    assert np.isclose(np.linalg.norm(embedding[0]), 1.0)
    assert np.isclose(np.linalg.norm(embedding[2]), 1.0)


def test_truncated_svd_pads_when_k_exceeds_rank() -> None:
    # 3×2 matrix; rank ≤ 2 but we request k=5 → pad with 3 zero columns.
    tfidf = np.array(
        [
            [1.0, 2.0],
            [3.0, 4.0],
            [5.0, 6.0],
        ],
        dtype=np.float64,
    )

    embedding = truncated_svd(tfidf, k=5)

    assert embedding.shape == (3, 5)
    # Padding columns must be exactly zero.
    assert np.all(embedding[:, 2:] == 0.0)
    # Non-zero prefix columns carry the signal → rows still unit-norm.
    norms = np.linalg.norm(embedding, axis=1)
    np.testing.assert_allclose(norms, np.ones(3), atol=1e-9)


def test_truncated_svd_empty_matrix() -> None:
    # Empty input — returns (0, k) zero matrix, not a crash.
    embedding = truncated_svd(np.zeros((0, 5), dtype=np.float64), k=8)

    assert embedding.shape == (0, 8)


def test_truncated_svd_zero_column_matrix() -> None:
    # Zero columns (no vocabulary survived min_df) — same contract.
    embedding = truncated_svd(np.zeros((4, 0), dtype=np.float64), k=8)

    assert embedding.shape == (4, 8)
    assert np.all(embedding == 0.0)


def test_integration_fifty_cards_distinct_unit_vectors() -> None:
    conn = _seed_fifty_card_db()
    try:
        corpus = extract_card_tokens(conn)
        tfidf_result = compute_tfidf(corpus, min_df=2)
        embedding = truncated_svd(tfidf_result.tfidf_matrix, k=128)

        assert embedding.shape == (50, 128)

        # Every row is unit-norm (no card has zero tokens in this
        # fixture).
        norms = np.linalg.norm(embedding, axis=1)
        np.testing.assert_allclose(norms, np.ones(50), atol=1e-9)

        # All 50 rows are pairwise distinct.
        unique_rows = {tuple(row) for row in embedding.tolist()}
        assert len(unique_rows) == 50
    finally:
        conn.close()


def test_integration_zero_token_card_yields_zero_vector() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO cards (name, keywords) VALUES ('Alpha', NULL)")
    conn.execute(
        "INSERT INTO cards (name, keywords) VALUES ('Beta', ?)",
        (json.dumps(["Flying", "Haste"]),),
    )
    conn.execute("INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Beta', 'trigger', 'ETB')")
    # Only Alpha has zero tokens. Provide at least one token on Beta
    # that survives min_df=1 so compute_tfidf returns a non-empty matrix.
    conn.commit()
    try:
        corpus = extract_card_tokens(conn)
        tfidf = compute_tfidf(corpus, min_df=1)
        embedding = truncated_svd(tfidf.tfidf_matrix, k=4)

        alpha_idx = tfidf.card_names.index("Alpha")
        # Alpha had zero tokens → its TF-IDF row is all-zero → embedding
        # row is all-zero after normalization (no NaN).
        assert np.all(embedding[alpha_idx] == 0.0)
        assert not np.isnan(embedding).any()
    finally:
        conn.close()
