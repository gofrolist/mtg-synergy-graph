"""Unit tests for the validation harness (SPEC §10)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph import (
    GoldenSetReport,
    SynergyEngine,
    bootstrap_golden_set,
    check_golden_set,
    commander_to_slug,
    compare_to_edhrec,
    compute_ndcg,
    edhrec_labels_for_commander,
    regression_failed,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# compute_ndcg
# ---------------------------------------------------------------------------


def test_ndcg_perfect_ranking_is_one():
    labels = {"a": 3.0, "b": 2.0, "c": 1.0}
    assert compute_ndcg(["a", "b", "c"], labels, k=3) == pytest.approx(1.0)


def test_ndcg_reverse_ranking_below_one():
    labels = {"a": 3.0, "b": 2.0, "c": 1.0}
    score = compute_ndcg(["c", "b", "a"], labels, k=3)
    assert 0.0 < score < 1.0


def test_ndcg_zero_when_predicted_misses_all_labels():
    labels = {"a": 1.0, "b": 1.0}
    assert compute_ndcg(["x", "y", "z"], labels, k=10) == 0.0


def test_ndcg_zero_when_labels_empty():
    assert compute_ndcg(["a", "b"], {}, k=10) == 0.0


def test_ndcg_handles_k_smaller_than_predicted():
    labels = {"a": 5.0, "b": 5.0, "c": 5.0}
    # With k=1 we only count the first prediction.
    assert compute_ndcg(["a", "b", "c"], labels, k=1) == pytest.approx(1.0)


def test_ndcg_dcg_formula_matches_definition():
    """Hand-compute DCG@2 for a graded ranking and check the result."""
    labels = {"a": 3.0, "b": 2.0}
    # DCG  = (2^3-1)/log2(2) + (2^2-1)/log2(3) = 7/1 + 3/log2(3)
    # IDCG = same — perfect ranking
    expected = 1.0
    assert compute_ndcg(["a", "b"], labels, k=2) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# commander_to_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Korvold, Fae-Cursed King",   "korvold-fae-cursed-king"),
        ("Tymna the Weaver",           "tymna-the-weaver"),
        ("Atraxa, Praetors' Voice",    "atraxa-praetors-voice"),
        ("Krenko, Mob Boss",           "krenko-mob-boss"),
        ("The Doctor",                 "the-doctor"),
    ],
)
def test_commander_to_slug(name, slug):
    assert commander_to_slug(name) == slug


# ---------------------------------------------------------------------------
# edhrec_labels_for_commander + compare_to_edhrec — fake EDHREC db
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_edhrec_db(tmp_path):
    """Build an in-memory EDHREC db with the same schema as data/tags.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE edhrec_card_synergy (
            commander_slug TEXT,
            card_name      TEXT,
            synergy        REAL,
            inclusion      INTEGER,
            num_decks      INTEGER,
            section        TEXT
        )
        """
    )
    rows = [
        ("korvold-fae-cursed-king", "Phyrexian Altar",       0.40, 5000, 5000, "High Synergy Cards"),
        ("korvold-fae-cursed-king", "Dockside Extortionist", 0.35, 8000, 8000, "Top Cards"),
        ("korvold-fae-cursed-king", "Sol Ring",              0.05, 30000, 30000, "Mana Artifacts"),
        ("korvold-fae-cursed-king", "Mayhem Devil",          0.49, 14000, 14000, "High Synergy Cards"),
        ("korvold-fae-cursed-king", "Negative Synergy Card", -0.10, 100, 100, "Creatures"),
    ]
    conn.executemany(
        "INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def test_edhrec_labels_drops_negative_synergy(fake_edhrec_db):
    labels = edhrec_labels_for_commander(fake_edhrec_db, "Korvold, Fae-Cursed King")
    assert "Phyrexian Altar" in labels
    assert "Mayhem Devil" in labels
    assert labels["Mayhem Devil"] == pytest.approx(0.49)
    assert "Negative Synergy Card" not in labels


def test_compare_to_edhrec_counts_per_section(fake_edhrec_db):
    from mtg_synergy_graph import Recommendation, RecommendationPage

    items = [
        Recommendation(rank=1, card="Mayhem Devil",          total_score=100, scores={}, contributing_ports=[]),
        Recommendation(rank=2, card="Phyrexian Altar",       total_score=50,  scores={}, contributing_ports=[]),
        Recommendation(rank=3, card="Dockside Extortionist", total_score=40,  scores={}, contributing_ports=[]),
        Recommendation(rank=4, card="Random Card Not On EDHREC", total_score=10, scores={}, contributing_ports=[]),
    ]
    page = RecommendationPage(
        commander=["Korvold, Fae-Cursed King"],
        color_identity=["B", "G", "R"],
        total=4, offset=0, limit=10,
        generated_at="2026-01-01T00:00:00",
        forge_version="test", spec_version="1.2.2", engine_version="0.1.0",
        items=items,
    )
    cmp = compare_to_edhrec(page, fake_edhrec_db, top_n=10)
    assert cmp.hi_syn_hits == 2     # Mayhem Devil + Phyrexian Altar
    assert cmp.top_hits == 1        # Dockside
    assert cmp.on_page_hits == 3
    assert cmp.not_edh == 1
    assert cmp.hi_syn_size == 2


# ---------------------------------------------------------------------------
# Golden Set bootstrap + check round-trip — uses the fixture import
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def golden_set_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("golden") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES)
    conn.close()
    return db_path


def test_bootstrap_writes_baseline(golden_set_db, tmp_path):
    out = tmp_path / "golden_baseline.json"
    with SynergyEngine(golden_set_db) as engine:
        report = bootstrap_golden_set(
            engine,
            ["Korvold, Fae-Cursed King", "Cathars' Crusade"],
            out,
        )
    assert isinstance(report, GoldenSetReport)
    assert len(report.entries) == 2
    payload = json.loads(out.read_text())
    assert payload["version"] == 1
    names = {e["commander"] for e in payload["entries"]}
    assert "Korvold, Fae-Cursed King" in names


def test_check_no_regression_after_bootstrap(golden_set_db, tmp_path):
    """Bootstrap then immediately check — there must be zero drift."""
    out = tmp_path / "golden_baseline.json"
    with SynergyEngine(golden_set_db) as engine:
        bootstrap_golden_set(
            engine,
            ["Korvold, Fae-Cursed King", "Cathars' Crusade"],
            out,
        )
        report = check_golden_set(engine, out)
    assert report.rank_shifts == ()
    assert report.ndcg_drops == ()
    assert report.drift == ()
    assert not regression_failed(report)


def test_check_detects_top10_drift(golden_set_db, tmp_path):
    """Synthesise a baseline that doesn't match the engine's actual top-10
    and verify the regression detector catches it."""
    out = tmp_path / "golden_baseline.json"
    payload = {
        "version": 1,
        "entries": [
            {
                "commander": "Korvold, Fae-Cursed King",
                "top10": ["Fictional Card 1", "Fictional Card 2", "Fictional Card 3",
                          "Fictional Card 4", "Fictional Card 5", "Fictional Card 6",
                          "Fictional Card 7", "Fictional Card 8", "Fictional Card 9",
                          "Fictional Card 10"],
                "ndcg30": 0.95,
            }
        ],
    }
    out.write_text(json.dumps(payload))
    with SynergyEngine(golden_set_db) as engine:
        report = check_golden_set(engine, out)
    assert report.rank_shifts, "expected rank-shift regression"
    assert regression_failed(report)


def test_regression_failed_aggregate_ndcg_drop():
    fake = GoldenSetReport(
        entries=[],
        rank_shifts=[],
        ndcg_drops=[],
        aggregate_ndcg=0.40,
        baseline_ndcg=0.45,
    )
    assert regression_failed(fake, ndcg_tolerance=0.005)
