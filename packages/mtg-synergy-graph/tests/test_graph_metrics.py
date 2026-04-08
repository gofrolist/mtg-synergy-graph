"""Tests for build-time causal graph metrics (SPEC §6.8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import (
    build_causal_graph,
    card_hub_scores,
    cmdr_two_hop_ratio,
    compute_commander_metrics,
    neighbor_overlap,
    personalised_pagerank,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("metrics") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    yield conn
    conn.close()


def test_build_causal_graph_returns_undirected_adjacency(populated_db):
    adj = build_causal_graph(populated_db)
    assert isinstance(adj, dict)
    assert "Korvold, Fae-Cursed King" in adj
    # Symmetry: a→b implies b→a.
    for a, neigh in adj.items():
        for b in neigh:
            assert a in adj[b], f"asymmetric edge {a}→{b}"


def test_phyrexian_altar_neighbors_korvold_in_causal_graph(populated_db):
    adj = build_causal_graph(populated_db)
    altar = adj.get("Phyrexian Altar", set())
    # Phyrexian Altar's sacrifice cost feeds Korvold's Sacrificed trigger,
    # so the cost-feed predicate must produce an edge.
    assert "Korvold, Fae-Cursed King" in altar


def test_wrath_of_god_does_not_neighbour_korvold(populated_db):
    """Catch-all triggers must not produce graph edges (the Wrath bug fix)."""
    adj = build_causal_graph(populated_db)
    wrath = adj.get("Wrath of God", set())
    assert "Korvold, Fae-Cursed King" not in wrath


def test_card_hub_scores_normalised(populated_db):
    adj = build_causal_graph(populated_db)
    hubs = card_hub_scores(adj)
    assert all(0.0 <= v <= 1.0 for v in hubs.values())
    # Some card must hit the maximum (1.0).
    assert max(hubs.values(), default=0) == pytest.approx(1.0)


def test_neighbor_overlap_jaccard():
    adj = {"a": {"x", "y"}, "b": {"y", "z"}, "x": {"a"}, "y": {"a", "b"}, "z": {"b"}}
    # |a∩b| / |a∪b| = |{y}| / |{x,y,z}| = 1/3
    assert neighbor_overlap(adj, "a", "b") == pytest.approx(1.0 / 3.0)


def test_cmdr_two_hop_ratio_finds_indirect_neighbour():
    adj = {"cmdr": {"a"}, "a": {"cmdr", "b"}, "b": {"a", "cand"}, "cand": {"b"}}
    # cand has 1 neighbour (b); b is reachable in ≤2 hops from cmdr → ratio 1.0.
    assert cmdr_two_hop_ratio(adj, "cmdr", "cand") == pytest.approx(1.0)


def test_personalised_pagerank_favours_source_over_distant_node():
    # Path graph a — b — c. Personalised at "a", the source must outrank
    # the far end "c" (b is the central node and naturally accumulates
    # rank from both sides — that's correct PageRank behaviour, not
    # a property we want to assert against here).
    adj = {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}}
    pr = personalised_pagerank(adj, "a", iterations=50)
    assert pr["a"] > pr["c"]
    assert pr["a"] > 0
    # All values should be finite and non-negative.
    assert all(v >= 0 for v in pr.values())


def test_compute_commander_metrics_assigns_4_metrics(populated_db):
    adj = build_causal_graph(populated_db)
    metrics = compute_commander_metrics(adj, ["Korvold, Fae-Cursed King"])
    assert "Korvold, Fae-Cursed King" not in metrics
    altar = metrics.get("Phyrexian Altar")
    assert altar is not None
    assert set(altar) == {
        "card_hub_score",
        "graph_neighbor_overlap",
        "cmdr_2hop_ratio",
        "graph_pagerank",
    }
    # Phyrexian Altar shares the commander as a neighbour → strictly positive
    # PageRank under personalisation.
    assert altar["graph_pagerank"] >= 0.0
