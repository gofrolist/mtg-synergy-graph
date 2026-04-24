"""Integration tests for the ``--explain`` embedding render lines.

Plan 2026-04-23-003 Unit 7 / FR6 — verify that ``SynergyEngine.page``
with ``include_explanations=True`` emits an ``embedding_contribution:``
decomposition line plus a ``nearest covered neighbors:`` line when the
flag is on AND the candidate has a nonzero embedding contribution.
With the flag OFF (module default), neither line appears — the render
path short-circuits at ``universal_score.embedding_contribution == 0``.

These tests wire the whole chain:
    SynergyEngine.page() → score_all_universal() → embedding_contribution
    → _render_explanation → _render_embedding_block → nearest_covered_neighbors

No mocking of the embedding functions — the DB is populated with
deterministic synthetic vectors and the real ``load_card_embeddings_verified``
round-trip is exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.embeddings import commander_target as ct
from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import contribution as emb_contribution
from mtg_synergy_graph.embeddings import store as emb_store
from mtg_synergy_graph.engine import SynergyEngine


def _make_unit_vector(seed: int, dim: int = 128) -> np.ndarray:
    """Deterministic L2-normalized float32 vector."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    assert norm > 0.0
    return (vec / norm).astype(np.float32)


def _seed_engine_db(db_path: Path) -> None:
    """Populate ``synergy.db`` with a commander + a few candidates + ports.

    The commander has an ETB trigger; Token Maker has a Token effect that
    matches via ``trigger_effect``; Counter Doubler has a replacement
    port. A generic creature without meaningful ports is included so the
    embedding-only contribution path is exercisable on the rule-empty tail.
    """
    conn = open_db(db_path)
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Token Maker", "Creature", "", 3, "R", 500, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Counter Doubler", "Enchantment", "", 3, "R", 300, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Generic Creature", "Creature", "Human", 2, "R", 5000, 1),
    )

    # Commander has an ETB trigger on creatures.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Counter Doubler", "replacement", "PutCounter", "Creature.YouCtrl", "{double}"),
    )

    # Populate deterministic embeddings for every card. Must be done
    # *before* the engine opens the DB because ``write_vectors`` runs
    # its own transaction and the engine keeps a cached connection.
    vectors = {
        "Test Commander": _make_unit_vector(seed=1),
        "Token Maker": _make_unit_vector(seed=2),
        "Counter Doubler": _make_unit_vector(seed=3),
        "Generic Creature": _make_unit_vector(seed=4),
    }
    emb_store.write_vectors(conn, vectors, emb_config.get_embedding_config_inputs())

    conn.commit()
    conn.close()


@pytest.fixture()
def engine_with_embeddings(tmp_path: Path) -> SynergyEngine:
    """SynergyEngine opened against a DB seeded with ports + vectors."""
    db_path = tmp_path / "synergy.db"
    _seed_engine_db(db_path)
    return SynergyEngine(db_path)


# ---------------------------------------------------------------------------
# Flag ON → embedding lines render
# ---------------------------------------------------------------------------


def test_explain_includes_embedding_lines_when_flag_on(
    engine_with_embeddings: SynergyEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + w_emb=0.5 + populated vectors → at least one candidate's
    explanation contains the ``embedding_contribution:`` line and the
    ``nearest covered neighbors:`` line.
    """
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    ct.clear_cache()

    page = engine_with_embeddings.page(
        "Test Commander",
        offset=0,
        limit=10,
        include_explanations=True,
    )

    joined: list[str] = []
    for item in page.items:
        if item.explanation:
            joined.extend(item.explanation)

    has_embedding_line = any("embedding_contribution:" in line for line in joined)
    has_neighbors_line = any("nearest covered neighbors:" in line for line in joined)

    assert has_embedding_line, (
        "Flag on + nonzero w_emb + populated vectors should produce at least "
        "one 'embedding_contribution:' line in --explain output. Got:\n" + "\n".join(joined)
    )
    # Neighbors line only fires when the candidate has rule-covered
    # peers; with the current 3-candidate fixture Token Maker + Counter
    # Doubler both accumulate trigger_effect hits so the rule_covered
    # set has at least two entries.
    assert has_neighbors_line, (
        "With multiple rule-covered candidates there should be a 'nearest "
        "covered neighbors:' line. Got:\n" + "\n".join(joined)
    )


def test_explain_embedding_line_decomposes_decay_times_cosine(
    engine_with_embeddings: SynergyEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedding line exposes decay × cosine decomposition verbatim."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    ct.clear_cache()

    page = engine_with_embeddings.page(
        "Test Commander",
        offset=0,
        limit=10,
        include_explanations=True,
    )

    lines: list[str] = []
    for item in page.items:
        if item.explanation:
            lines.extend(item.explanation)

    emb_lines = [line for line in lines if "embedding_contribution:" in line]
    assert emb_lines, "expected at least one embedding_contribution line"
    # Format: "  embedding_contribution: +X.XXX (decay × cosine = Y.YY × Z.ZZZ)"
    for line in emb_lines:
        assert "(decay" in line and "cosine" in line, (
            f"embedding line should include decay x cosine decomposition: {line}"
        )


# ---------------------------------------------------------------------------
# Flag OFF → no embedding lines render
# ---------------------------------------------------------------------------


def test_explain_omits_embedding_lines_when_flag_off(
    engine_with_embeddings: SynergyEngine,
) -> None:
    """Module default: flag is False → no embedding lines should appear.

    Exercises the identity-preservation guarantee of Unit 7: even with
    populated ``card_embeddings`` in the DB, the explain render
    short-circuits at ``embedding_contribution == 0``.
    """
    # No monkeypatch — rely on the module default (False).
    assert emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION is False

    page = engine_with_embeddings.page(
        "Test Commander",
        offset=0,
        limit=10,
        include_explanations=True,
    )

    joined: list[str] = []
    for item in page.items:
        if item.explanation:
            joined.extend(item.explanation)

    assert not any("embedding_contribution:" in line for line in joined), (
        "Flag off → explain must NOT include 'embedding_contribution:' lines. Got:\n" + "\n".join(joined)
    )
    assert not any("nearest covered neighbors:" in line for line in joined), (
        "Flag off → explain must NOT include 'nearest covered neighbors:' lines. Got:\n" + "\n".join(joined)
    )
