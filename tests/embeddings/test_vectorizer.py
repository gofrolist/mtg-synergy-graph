"""Unit 1 tests — feature-bag extraction + TF-IDF math.

Covers:

* Token-grammar correctness: port-row columns, port_attributes rows,
  and cards.keywords all emit the documented token shapes.
* TF-IDF values match a hand-computed reference within 1e-9.
* ``min_df=2`` prunes tokens that appear on only one card.
* Duplicate tokens on a single card survive TF (raw-count behavior).
* Zero-token cards produce zero rows that downstream SVD + L2 handle
  without NaN.
* Deterministic ordering: ``card_names`` sorted ascending,
  ``vocabulary`` sorted ascending.
"""

from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
import pytest

from mtg_synergy_graph.embeddings.vectorizer import (
    TOKEN_FORMAT_VERSION,
    compute_tfidf,
    extract_card_tokens,
)

#: Minimal in-memory schema containing only the columns the vectorizer
#: reads. Deliberately narrower than ``tests/conftest.py``'s rules_db
#: so this unit does not couple to conftest evolution (Unit 3's scope).
_VECTORIZER_SCHEMA = """\
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


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_VECTORIZER_SCHEMA)
    return conn


def _insert_card(
    conn: sqlite3.Connection,
    name: str,
    keywords: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cards (name, keywords) VALUES (?, ?)",
        (name, json.dumps(keywords) if keywords is not None else None),
    )


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    zone_origin: str | None = None,
    zone_destination: str | None = None,
    counter_type: str | None = None,
    branch_kind: str | None = None,
    attributes: list[tuple[str, str]] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, "
        "zone_origin, zone_destination, counter_type, branch_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            card_name,
            port_type,
            event_class,
            zone_origin,
            zone_destination,
            counter_type,
            branch_kind,
        ),
    )
    port_id = int(cur.lastrowid or 0)
    if attributes:
        conn.executemany(
            "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, ?, ?)",
            [(port_id, k, v) for (k, v) in attributes],
        )
    return port_id


def test_token_format_version_is_v1() -> None:
    assert TOKEN_FORMAT_VERSION == "v1"  # noqa: S105 — version tag, not a secret


def test_extract_tokens_port_row_columns() -> None:
    conn = _fresh_db()
    _insert_card(conn, "Alpha", keywords=["Flying"])
    _insert_port(
        conn,
        "Alpha",
        "trigger",
        "SpellCast",
        zone_origin="Hand",
        zone_destination="Battlefield",
        counter_type="P1P1",
        branch_kind="root",
    )
    conn.commit()

    corpus = extract_card_tokens(conn)

    assert set(corpus["Alpha"]) == {
        "port_type:trigger",
        "event_class:SpellCast",
        "zone_origin:Hand",
        "zone_destination:Battlefield",
        "counter_type:P1P1",
        "branch_kind:root",
        "keyword:Flying",
    }


def test_extract_tokens_skips_null_optional_columns() -> None:
    conn = _fresh_db()
    _insert_card(conn, "Beta")
    _insert_port(conn, "Beta", "effect", "Draw")
    conn.commit()

    corpus = extract_card_tokens(conn)

    # Only the two required columns + nothing else (card has no keywords).
    assert corpus["Beta"] == ("port_type:effect", "event_class:Draw")


def test_extract_tokens_emits_attr_rows() -> None:
    conn = _fresh_db()
    _insert_card(conn, "Gamma")
    _insert_port(
        conn,
        "Gamma",
        "trigger",
        "ETB",
        attributes=[("type", "Creature"), ("subtype", "Goblin")],
    )
    conn.commit()

    corpus = extract_card_tokens(conn)

    assert "attr:type:Creature" in corpus["Gamma"]
    assert "attr:subtype:Goblin" in corpus["Gamma"]


def test_extract_tokens_keywords_json_decoded() -> None:
    conn = _fresh_db()
    _insert_card(conn, "Delta", keywords=["Flying", "Haste", "Trample"])
    _insert_port(conn, "Delta", "keyword", "Flying")
    conn.commit()

    corpus = extract_card_tokens(conn)

    assert sum(1 for t in corpus["Delta"] if t.startswith("keyword:")) == 3
    assert "keyword:Flying" in corpus["Delta"]
    assert "keyword:Haste" in corpus["Delta"]
    assert "keyword:Trample" in corpus["Delta"]


def test_extract_tokens_card_with_no_ports_and_no_keywords() -> None:
    conn = _fresh_db()
    _insert_card(conn, "Orphan")
    conn.commit()

    corpus = extract_card_tokens(conn)

    assert "Orphan" in corpus
    assert corpus["Orphan"] == ()


def test_extract_tokens_tolerates_missing_port_attributes_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # No port_attributes table at all — simulates a very minimal fixture.
    conn.executescript(
        """
        CREATE TABLE cards (name TEXT PRIMARY KEY, keywords TEXT);
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name TEXT NOT NULL,
            port_type TEXT NOT NULL,
            event_class TEXT NOT NULL,
            zone_origin TEXT,
            zone_destination TEXT,
            counter_type TEXT,
            branch_kind TEXT
        );
        """
    )
    conn.execute("INSERT INTO cards (name, keywords) VALUES ('Solo', NULL)")
    conn.execute("INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Solo', 'trigger', 'ETB')")
    conn.commit()

    corpus = extract_card_tokens(conn)

    assert corpus["Solo"] == ("port_type:trigger", "event_class:ETB")


def test_extract_tokens_ignores_malformed_keywords_json() -> None:
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO cards (name, keywords) VALUES (?, ?)",
        ("Bad", "{this is not json"),
    )
    conn.commit()

    corpus = extract_card_tokens(conn)

    # Malformed JSON doesn't raise; the card still appears with an empty tuple.
    assert corpus["Bad"] == ()


def test_compute_tfidf_matches_hand_computed_values() -> None:
    """5-card synthetic corpus with a known TF-IDF reference."""
    corpus: dict[str, tuple[str, ...]] = {
        "A": ("x", "y"),
        "B": ("x", "z"),
        "C": ("x", "y", "z"),
        "D": ("y", "w"),  # 'w' appears only here → pruned by min_df=2
        "E": ("x", "z"),
    }

    result = compute_tfidf(corpus, min_df=2)

    # Card names sorted; 'w' pruned because df=1.
    assert result.card_names == ["A", "B", "C", "D", "E"]
    assert result.vocabulary == ["x", "y", "z"]

    # IDF = ln((1+N)/(1+df)) + 1, N=5
    # df(x) = 4 → idf = ln(6/5) + 1
    # df(y) = 3 → idf = ln(6/4) + 1
    # df(z) = 3 → idf = ln(6/4) + 1
    idf_x = math.log(6.0 / 5.0) + 1.0
    idf_y = math.log(6.0 / 4.0) + 1.0
    idf_z = math.log(6.0 / 4.0) + 1.0

    expected = np.array(
        [
            [idf_x, idf_y, 0.0],  # A: x, y
            [idf_x, 0.0, idf_z],  # B: x, z
            [idf_x, idf_y, idf_z],  # C: x, y, z
            [0.0, idf_y, 0.0],  # D: y (w pruned)
            [idf_x, 0.0, idf_z],  # E: x, z
        ],
        dtype=np.float64,
    )

    np.testing.assert_allclose(result.tfidf_matrix, expected, atol=1e-9)


def test_compute_tfidf_prunes_tokens_below_min_df() -> None:
    corpus: dict[str, tuple[str, ...]] = {
        "A": ("shared", "unique_a"),
        "B": ("shared", "unique_b"),
    }

    result = compute_tfidf(corpus, min_df=2)

    assert result.vocabulary == ["shared"]
    assert result.tfidf_matrix.shape == (2, 1)


def test_compute_tfidf_min_df_one_retains_everything() -> None:
    corpus: dict[str, tuple[str, ...]] = {
        "A": ("singleton",),
    }

    result = compute_tfidf(corpus, min_df=1)

    assert result.vocabulary == ["singleton"]
    assert result.tfidf_matrix.shape == (1, 1)


def test_compute_tfidf_duplicate_tokens_counted_raw() -> None:
    corpus: dict[str, tuple[str, ...]] = {
        "A": ("keyword:Flying", "keyword:Flying", "keyword:Flying"),
        "B": ("keyword:Flying",),
    }

    result = compute_tfidf(corpus, min_df=1)

    idf = math.log(3.0 / 3.0) + 1.0  # df=2, N=2 → ln((1+2)/(1+2))+1 = 1.0
    # Row A has TF=3 → value = 3 * 1.0 = 3.0
    # Row B has TF=1 → value = 1 * 1.0 = 1.0
    assert result.vocabulary == ["keyword:Flying"]
    np.testing.assert_allclose(
        result.tfidf_matrix,
        np.array([[3.0 * idf], [1.0 * idf]], dtype=np.float64),
        atol=1e-9,
    )


def test_compute_tfidf_empty_corpus() -> None:
    result = compute_tfidf({}, min_df=1)

    assert result.card_names == []
    assert result.vocabulary == []
    assert result.tfidf_matrix.shape == (0, 0)


def test_compute_tfidf_all_tokens_pruned() -> None:
    # Every token appears on exactly one card; min_df=2 kills them all.
    corpus: dict[str, tuple[str, ...]] = {
        "A": ("unique_a",),
        "B": ("unique_b",),
    }

    result = compute_tfidf(corpus, min_df=2)

    assert result.vocabulary == []
    assert result.tfidf_matrix.shape == (2, 0)


def test_compute_tfidf_rejects_min_df_zero() -> None:
    with pytest.raises(ValueError, match="min_df must be >= 1"):
        compute_tfidf({"A": ("x",)}, min_df=0)


def test_compute_tfidf_deterministic_ordering() -> None:
    # Same corpus supplied in different insertion orders must produce
    # identical output (sorted card_names + sorted vocabulary).
    corpus_1: dict[str, tuple[str, ...]] = {
        "Z": ("t1", "t2"),
        "A": ("t1", "t3"),
        "M": ("t1", "t2", "t3"),
    }
    corpus_2: dict[str, tuple[str, ...]] = {
        "A": ("t3", "t1"),
        "M": ("t2", "t3", "t1"),
        "Z": ("t2", "t1"),
    }

    r1 = compute_tfidf(corpus_1, min_df=1)
    r2 = compute_tfidf(corpus_2, min_df=1)

    assert r1.card_names == r2.card_names == ["A", "M", "Z"]
    assert r1.vocabulary == r2.vocabulary == ["t1", "t2", "t3"]
    np.testing.assert_array_equal(r1.tfidf_matrix, r2.tfidf_matrix)
