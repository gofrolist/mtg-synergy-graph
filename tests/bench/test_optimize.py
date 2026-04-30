"""Tests for ``mtg_synergy_graph.bench.optimize``.

Built incrementally alongside the optimizer's units. Unit 1 tests live
under ``TestCompositeObjective``, ``TestRandomSplit``, and
``TestLoadEdhrecLabels``; later units append their own classes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.optimize import (
    composite_objective,
    load_edhrec_labels,
    random_split,
)

# ---------------------------------------------------------------------------
# composite_objective
# ---------------------------------------------------------------------------


class TestCompositeObjective:
    def test_blends_axes_at_alpha_half(self) -> None:
        result = composite_objective(
            per_commander_ndcg={"a": 0.4, "b": 0.4},
            per_commander_gem={"a": 0.84, "b": 0.84},
            alpha=0.5,
        )
        assert result.composite == pytest.approx(0.62)
        assert result.mean_ndcg == pytest.approx(0.4)
        assert result.gem_rate == pytest.approx(0.84)
        assert result.n_commanders == 2
        assert result.n_commanders_with_gem == 2

    @pytest.mark.parametrize(
        ("alpha", "expected"),
        [(0.0, 0.9), (0.3, 0.78), (0.5, 0.7), (0.7, 0.62), (1.0, 0.5)],
    )
    def test_alpha_is_linear_blend(self, alpha: float, expected: float) -> None:
        result = composite_objective(
            per_commander_ndcg={"a": 0.5},
            per_commander_gem={"a": 0.9},
            alpha=alpha,
        )
        assert result.composite == pytest.approx(expected)

    def test_no_gem_data_collapses_to_alpha_ndcg(self) -> None:
        result = composite_objective(
            per_commander_ndcg={"a": 0.4, "b": 0.6},
            per_commander_gem={"a": None, "b": None},
            alpha=0.5,
        )
        # 0.5 * 0.5 + 0 (gem term contributes 0 when no data)
        assert result.composite == pytest.approx(0.25)
        assert result.gem_rate is None
        assert result.n_commanders == 2
        assert result.n_commanders_with_gem == 0

    def test_partial_gem_data_means_over_present_only(self) -> None:
        result = composite_objective(
            per_commander_ndcg={"a": 0.4, "b": 0.4, "c": 0.4},
            per_commander_gem={"a": 0.8, "b": None, "c": 0.6},
            alpha=0.5,
        )
        assert result.gem_rate == pytest.approx(0.7)
        assert result.n_commanders == 3
        assert result.n_commanders_with_gem == 2
        # 0.5 * 0.4 + 0.5 * 0.7 = 0.55
        assert result.composite == pytest.approx(0.55)

    def test_empty_input_returns_zero(self) -> None:
        result = composite_objective({}, {}, alpha=0.5)
        assert result.composite == 0.0
        assert result.mean_ndcg == 0.0
        assert result.gem_rate is None
        assert result.n_commanders == 0
        assert result.n_commanders_with_gem == 0

    @pytest.mark.parametrize("alpha", [-0.1, 1.1, -1.0, 2.0, float("inf")])
    def test_alpha_out_of_range_raises(self, alpha: float) -> None:
        with pytest.raises(ValueError, match="alpha"):
            composite_objective({"a": 0.5}, {"a": 0.5}, alpha=alpha)

    def test_returns_frozen_dataclass(self) -> None:
        from dataclasses import FrozenInstanceError

        result = composite_objective({"a": 0.5}, {"a": 0.5}, alpha=0.5)
        with pytest.raises(FrozenInstanceError):
            result.composite = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# random_split
# ---------------------------------------------------------------------------


@pytest.fixture()
def cards_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Real SQLite DB with a minimal cards table covering all bucket types."""
    conn = sqlite3.connect(tmp_path / "synergy.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE cards (
            name TEXT PRIMARY KEY,
            color_identity TEXT
        )
        """
    )
    rows = [
        ("Mono A", "R"),
        ("Mono B", "B"),
        ("Mono C", "U"),
        ("Mono D", "G"),
        ("TwoColor A", "R,U"),
        ("TwoColor B", "B,W"),
        ("ThreeColor A", "R,U,B"),
        ("ThreeColor B", "G,W,U"),
        ("Colorless A", ""),
        ("Untagged", None),
    ]
    conn.executemany("INSERT INTO cards (name, color_identity) VALUES (?, ?)", rows)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestRandomSplit:
    def test_determinism_same_seed(self) -> None:
        commanders = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
        a = random_split(commanders, None, seed=42)
        b = random_split(commanders, None, seed=42)
        assert a.train == b.train
        assert a.held == b.held

    def test_different_seeds_diverge(self) -> None:
        commanders = list("abcdefghij")
        a = random_split(commanders, None, seed=1)
        b = random_split(commanders, None, seed=99)
        # With 10 commanders and uncorrelated seeds, identical splits are
        # vanishingly unlikely (~1 in C(10, 8) ≈ 1/45). Test asserts at least
        # one of train/held differs.
        assert (a.train, a.held) != (b.train, b.held)

    def test_train_ratio_default_eighty_twenty(self) -> None:
        commanders = list("abcdefghij")  # 10 commanders
        result = random_split(commanders, None, train_ratio=0.8, seed=42)
        assert len(result.train) == 8
        assert len(result.held) == 2
        # Disjoint and exhaustive
        assert set(result.train).isdisjoint(set(result.held))
        assert set(result.train) | set(result.held) == set(commanders)

    def test_alternative_train_ratio(self) -> None:
        commanders = list("abcdefghij")
        result = random_split(commanders, None, train_ratio=0.5, seed=42)
        assert len(result.train) == 5
        assert len(result.held) == 5

    def test_color_bucket_reporting_covers_all_input(self, cards_conn: sqlite3.Connection) -> None:
        commanders = [
            "Mono A",
            "Mono B",
            "Mono C",
            "Mono D",
            "TwoColor A",
            "TwoColor B",
            "ThreeColor A",
            "ThreeColor B",
            "Colorless A",
            "Untagged",
        ]
        result = random_split(commanders, cards_conn, train_ratio=0.8, seed=42)
        # Every commander accounted for in exactly one bucket × fold cell.
        total = sum(v["train"] + v["held"] for v in result.color_buckets.values())
        assert total == len(commanders)
        # All four bucket types appear (4 mono, 2 2c, 2 3c+, 2 colorless).
        assert result.color_buckets["mono"]["train"] + result.color_buckets["mono"]["held"] == 4
        assert result.color_buckets["2c"]["train"] + result.color_buckets["2c"]["held"] == 2
        assert result.color_buckets["3c+"]["train"] + result.color_buckets["3c+"]["held"] == 2
        assert result.color_buckets["colorless"]["train"] + result.color_buckets["colorless"]["held"] == 2

    def test_color_bucket_empty_buckets_dropped(self, cards_conn: sqlite3.Connection) -> None:
        # Only mono commanders → bucket report should not list "2c" / "3c+".
        commanders = ["Mono A", "Mono B", "Mono C", "Mono D"]
        result = random_split(commanders, cards_conn, train_ratio=0.5, seed=7)
        assert "mono" in result.color_buckets
        assert "2c" not in result.color_buckets
        assert "3c+" not in result.color_buckets

    def test_no_conn_falls_back_to_colorless(self) -> None:
        commanders = ["a", "b", "c", "d"]
        result = random_split(commanders, None, train_ratio=0.5, seed=1)
        # Without DB lookup every commander becomes "colorless" — the bucket
        # report is informational only.
        total_colorless = result.color_buckets["colorless"]["train"] + result.color_buckets["colorless"]["held"]
        assert total_colorless == 4

    def test_unknown_commander_falls_into_colorless(self, cards_conn: sqlite3.Connection) -> None:
        # Commander not in the cards table → no row → defaults to colorless.
        commanders = ["Mono A", "Stranger"]
        result = random_split(commanders, cards_conn, train_ratio=0.5, seed=1)
        # Stranger shows up in colorless bucket.
        colorless_total = (
            result.color_buckets.get("colorless", {"train": 0, "held": 0})["train"]
            + result.color_buckets.get("colorless", {"train": 0, "held": 0})["held"]
        )
        assert colorless_total == 1

    @pytest.mark.parametrize("ratio", [0.0, 1.0, -0.1, 1.1])
    def test_invalid_ratio_raises(self, ratio: float) -> None:
        with pytest.raises(ValueError, match="train_ratio"):
            random_split(["a", "b"], None, train_ratio=ratio, seed=42)

    def test_empty_commanders_returns_empty_split(self) -> None:
        result = random_split([], None, train_ratio=0.8, seed=42)
        assert result.train == ()
        assert result.held == ()


# ---------------------------------------------------------------------------
# load_edhrec_labels
# ---------------------------------------------------------------------------


@pytest.fixture()
def edhrec_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Synthetic EDHREC DB with one commander populated across sections."""
    conn = sqlite3.connect(tmp_path / "tags.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)")
    rows = [
        ("test-commander", "Hi A", "High Synergy Cards", 0.9),
        ("test-commander", "Hi B", "High Synergy Cards", 0.8),
        ("test-commander", "Hi C", "High Synergy Cards", 0.7),
        ("test-commander", "Top A", "Top Cards", 0.5),
        ("test-commander", "Top B", "Top Cards", 0.4),
        ("test-commander", "Other A", "Creatures", 0.3),
    ]
    conn.executemany(
        "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestLoadEdhrecLabels:
    def test_high_synergy_cards_get_three(self, edhrec_conn: sqlite3.Connection) -> None:
        result = load_edhrec_labels(edhrec_conn, "Test Commander")
        assert result.graded_labels["Hi A"] == 3.0
        assert result.graded_labels["Hi B"] == 3.0
        assert result.graded_labels["Hi C"] == 3.0

    def test_other_sections_get_one(self, edhrec_conn: sqlite3.Connection) -> None:
        result = load_edhrec_labels(edhrec_conn, "Test Commander")
        assert result.graded_labels["Top A"] == 1.0
        assert result.graded_labels["Top B"] == 1.0
        assert result.graded_labels["Other A"] == 1.0

    def test_top_30_is_high_synergy_only(self, edhrec_conn: sqlite3.Connection) -> None:
        result = load_edhrec_labels(edhrec_conn, "Test Commander")
        assert result.top_30_set == frozenset({"Hi A", "Hi B", "Hi C"})

    def test_no_data_returns_empty_labels_and_none_top30(self, edhrec_conn: sqlite3.Connection) -> None:
        result = load_edhrec_labels(edhrec_conn, "Unknown Commander")
        assert dict(result.graded_labels) == {}
        assert result.top_30_set is None

    def test_slug_conversion_handles_punctuation(self, edhrec_conn: sqlite3.Connection) -> None:
        # "Test Commander" → slug "test-commander" — punctuation stripping is
        # exercised by passing a name with apostrophes and commas.
        conn = edhrec_conn
        conn.execute(
            "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) VALUES (?, ?, ?, ?)",
            ("korvold-fae-cursed-king", "Sacrifice Outlet", "High Synergy Cards", 0.95),
        )
        conn.commit()
        result = load_edhrec_labels(conn, "Korvold, Fae-Cursed King")
        assert "Sacrifice Outlet" in result.graded_labels
        assert result.graded_labels["Sacrifice Outlet"] == 3.0
        assert result.top_30_set == frozenset({"Sacrifice Outlet"})

    def test_graded_labels_is_immutable_view(self, edhrec_conn: sqlite3.Connection) -> None:
        result = load_edhrec_labels(edhrec_conn, "Test Commander")
        with pytest.raises(TypeError):
            result.graded_labels["Hi A"] = 999.0  # type: ignore[index]
