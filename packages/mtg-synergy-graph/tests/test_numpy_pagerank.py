"""Tests for the numpy-backed personalised PageRank (Phase 4.6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.graph_metrics import (
    HAS_NUMPY,
    build_causal_graph,
    numpy_personalised_pagerank,
    personalised_pagerank,
)

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(not HAS_NUMPY, reason="numpy not installed")


# ---------------------------------------------------------------------------
# Correctness vs pure-Python CSR implementation
# ---------------------------------------------------------------------------


def test_numpy_matches_pure_python_on_path_graph():
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b", "d"}, "d": {"c"}}
    py = personalised_pagerank(adj, "a", iterations=50)
    np = numpy_personalised_pagerank(adj, "a", iterations=50)
    for k in py:
        assert py[k] == pytest.approx(np[k], abs=1e-9), f"mismatch on {k}"


def test_numpy_matches_pure_python_on_star_graph():
    adj = {
        "center": {"a", "b", "c", "d"},
        "a": {"center"},
        "b": {"center"},
        "c": {"center"},
        "d": {"center"},
    }
    for source in ("center", "a"):
        py = personalised_pagerank(adj, source, iterations=50)
        np = numpy_personalised_pagerank(adj, source, iterations=50)
        for k in py:
            assert py[k] == pytest.approx(np[k], abs=1e-9), f"{source} → {k}"


def test_numpy_matches_pure_python_on_dangling_node():
    """Node 'd' has no outgoing edges (dangling)."""
    adj = {"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": set()}
    py = personalised_pagerank(adj, "a", iterations=50)
    np = numpy_personalised_pagerank(adj, "a", iterations=50)
    for k in py:
        assert py[k] == pytest.approx(np[k], abs=1e-9)


def test_numpy_handles_unknown_source():
    adj = {"a": {"b"}, "b": {"a"}}
    assert numpy_personalised_pagerank(adj, "ghost") == {}


def test_numpy_returns_dict_keyed_by_node_name():
    adj = {"a": {"b"}, "b": {"a"}}
    pr = numpy_personalised_pagerank(adj, "a", iterations=10)
    assert isinstance(pr, dict)
    assert set(pr) == {"a", "b"}
    assert all(isinstance(v, float) for v in pr.values())


# ---------------------------------------------------------------------------
# Sanity check on the small fixture causal graph
# ---------------------------------------------------------------------------


def test_numpy_pagerank_runs_on_fixture_graph(tmp_path):
    from mtg_synergy_graph.db import open_db
    from mtg_synergy_graph.importer import import_cards_folder

    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    adj = build_causal_graph(conn)
    pr = numpy_personalised_pagerank(adj, "Korvold, Fae-Cursed King", iterations=20)
    conn.close()

    # Source must outrank average — personalised PR concentrates here.
    assert pr["Korvold, Fae-Cursed King"] > 1.0 / len(pr)
    # Phyrexian Altar is a known cost-feed neighbour of Korvold and should
    # have a non-zero personalised rank.
    assert pr.get("Phyrexian Altar", 0.0) > 0.0
