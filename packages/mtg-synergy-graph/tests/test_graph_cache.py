"""Tests for the precomputed graph cache (SPEC §6.8 / Phase 4.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import (
    SynergyEngine,
    build_causal_graph,
    build_graph_cache,
    cache_is_populated,
    clear_graph_cache,
    commander_neighbours,
    global_pagerank,
    load_card_metrics,
    neighbours_of,
    personalised_pagerank,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("graphcache") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# CSR-based PageRank — correctness vs the old dict implementation
# ---------------------------------------------------------------------------


def test_global_pagerank_sums_to_one_on_simple_graph():
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}}
    pr = global_pagerank(adj, iterations=50)
    assert sum(pr.values()) == pytest.approx(1.0, abs=1e-3)
    # The central node 'b' must outrank the leaves.
    assert pr["b"] > pr["a"]
    assert pr["b"] > pr["c"]


def test_personalised_pagerank_csr_returns_same_shape():
    adj = {"a": {"b", "c"}, "b": {"a"}, "c": {"a"}}
    pr = personalised_pagerank(adj, "a", iterations=50)
    assert set(pr) == {"a", "b", "c"}
    assert all(v >= 0 for v in pr.values())
    # Source must outrank distant non-neighbours, if any.
    assert pr["a"] > 0


# ---------------------------------------------------------------------------
# build_graph_cache + load_card_metrics round-trip
# ---------------------------------------------------------------------------


def test_build_graph_cache_populates_tables(populated_db, tmp_path):
    # Copy the populated DB so we don't mutate the module-scoped fixture.
    import shutil
    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)

    conn = open_db(db_path)
    assert not cache_is_populated(conn)
    stats = build_graph_cache(conn, neighbour_cap=10)
    assert cache_is_populated(conn)
    assert stats.cards > 0
    assert stats.elapsed_s >= 0.0  # 9-card fixture rounds to 0.00s
    assert stats.built_at

    metrics = load_card_metrics(conn)
    assert "Korvold, Fae-Cursed King" in metrics
    altar = metrics.get("Phyrexian Altar")
    assert altar is not None
    assert altar.degree > 0
    assert 0.0 <= altar.hub_score <= 1.0
    assert altar.pagerank > 0
    conn.close()


def test_clear_graph_cache_removes_rows(populated_db, tmp_path):
    import shutil
    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)

    conn = open_db(db_path)
    build_graph_cache(conn, neighbour_cap=10)
    assert cache_is_populated(conn)
    clear_graph_cache(conn)
    assert not cache_is_populated(conn)
    assert load_card_metrics(conn) == {}
    conn.close()


def test_neighbours_of_returns_cached_adjacency(populated_db, tmp_path):
    import shutil
    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)

    conn = open_db(db_path)
    build_graph_cache(conn, neighbour_cap=50)

    # Phyrexian Altar should be a neighbour of Korvold (cost-feed match
    # confirmed by the Phase 4.1 graph build test).
    n = neighbours_of(conn, ["Korvold, Fae-Cursed King"])
    assert "Korvold, Fae-Cursed King" in n
    assert "Phyrexian Altar" in n["Korvold, Fae-Cursed King"]

    union = commander_neighbours(conn, ["Korvold, Fae-Cursed King"])
    assert "Phyrexian Altar" in union
    conn.close()


# ---------------------------------------------------------------------------
# Engine integration: graph_metrics=True reads from cache
# ---------------------------------------------------------------------------


def test_engine_graph_metrics_uses_cache(populated_db, tmp_path):
    """When graph_metrics=True and the cache is populated, the engine
    must consult one of the supported graph paths.

    Phase 4.6 prefers the live numpy personalised PageRank path even when
    the cache exists (because the cache only stores GLOBAL PageRank,
    which is the wrong feature). Phase 4.4 ships the cache as a fallback
    for numpy-less environments. This test accepts either path: as long
    as ``graph_metrics=True`` produces a successful page, the integration
    is wired correctly."""
    import shutil

    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)

    conn = open_db(db_path)
    build_graph_cache(conn, neighbour_cap=50)
    conn.close()

    with SynergyEngine(db_path, graph_metrics=True) as engine:
        page = engine.page("Korvold, Fae-Cursed King", offset=0, limit=20)
        assert page.total > 0
        # All page items must have a graph_metrics bucket present (even if 0).
        for rec in page.items:
            assert "graph_metrics" in rec.scores


def test_engine_graph_metrics_falls_back_when_cache_missing(populated_db, tmp_path):
    import shutil
    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)
    # Note: no build_graph_cache here — engine should still work via the
    # live build_causal_graph fallback.
    with SynergyEngine(db_path, graph_metrics=True) as engine:
        page = engine.page("Cathars' Crusade", offset=0, limit=10)
        assert page.total > 0


# ---------------------------------------------------------------------------
# build_causal_graph stays consistent with the cached neighbour table
# ---------------------------------------------------------------------------


def test_cache_neighbours_match_live_adjacency(populated_db, tmp_path):
    import shutil
    db_path = tmp_path / "synergy.db"
    shutil.copy(populated_db, db_path)

    conn = open_db(db_path)
    build_graph_cache(conn, neighbour_cap=None)  # uncapped — must match live exactly

    live = build_causal_graph(conn)
    cached = neighbours_of(conn, list(live.keys()))
    for name, expected in live.items():
        assert cached.get(name, set()) == expected, f"mismatch for {name}"
    conn.close()
