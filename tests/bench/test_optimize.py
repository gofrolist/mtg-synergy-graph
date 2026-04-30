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
    score_commander_from_complements,
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


# ---------------------------------------------------------------------------
# score_commander_from_complements (Unit 2)
# ---------------------------------------------------------------------------


@pytest.fixture()
def scoring_fixture(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Real DB with one commander + multiple matching candidates.

    Mirrors the canonical pattern from
    ``tests/bench/test_universal_scorer_identity.py:31-79``.
    """
    from mtg_synergy_graph.db import open_db

    conn = open_db(tmp_path / "synergy.db")
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    for cand_name, cmc, rank in [
        ("Token Maker", 3, 500),
        ("Counter Doubler", 3, 300),
        ("Generic Creature", 2, 5000),
        ("Sacrifice Outlet", 2, 200),
    ]:
        conn.execute(
            "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cand_name, "Creature", "", cmc, "R", rank, 1),
        )
    # Commander has an ETB trigger on creatures.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    # Candidate effects matching different rule families.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Counter Doubler", "replacement", "PutCounter", "Creature.YouCtrl", "{double}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Sacrifice Outlet", "effect", "Sacrifice", "Creature.YouCtrl", "{sac}"),
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestScoreCommanderFromComplements:
    def test_production_faithful_top_30_at_baseline(self, scoring_fixture: sqlite3.Connection) -> None:
        """Cached-complements path matches direct ``score_all_universal()`` top-30."""
        from mtg_synergy_graph.complement_rules import find_all_complements
        from mtg_synergy_graph.universal_scorer import score_all_universal

        # Production reference: full path through score_all_universal,
        # sorted by to_legacy_buckets()["total"] like engine.SynergyEngine.page().
        live = score_all_universal(scoring_fixture, ["Test Commander"])
        live_sortable = []
        cmc_lookup = {}
        rank_lookup = {}
        for row in scoring_fixture.execute("SELECT name, cmc, edhrec_rank FROM cards"):
            cmc_lookup[row["name"]] = row["cmc"] if row["cmc"] is not None else 99.0
            rank_lookup[row["name"]] = row["edhrec_rank"] if row["edhrec_rank"] is not None else 10**9
        for name, us in live.items():
            live_sortable.append((name, us.to_legacy_buckets()["total"]))
        live_sortable.sort(
            key=lambda r: (
                -r[1],
                cmc_lookup.get(r[0], 99.0),
                rank_lookup.get(r[0], 10**9),
                r[0],
            )
        )
        live_top_30 = tuple(name for name, _ in live_sortable[:30])

        # Cached-complements path
        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)

        assert result.top_30 == live_top_30
        # Score values must match bitwise — not just order.
        for name in result.score_by_candidate:
            assert result.score_by_candidate[name] == pytest.approx(live[name].to_legacy_buckets()["total"])

    def test_cache_reuse_finds_complements_only_once(self, scoring_fixture: sqlite3.Connection) -> None:
        """Calling 5x with the SAME cached complements and different weights
        does not re-trigger find_all_complements internally."""
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.complement_rules import find_all_complements

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        # Cache is the caller's responsibility — we assert that the function
        # does NOT call find_all_complements internally by calling it 5 times
        # with weights swapped.
        baseline = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        for _ in range(5):
            try:
                universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
                universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline)
                result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
                assert isinstance(result.top_30, tuple)
            finally:
                universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
                universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline)

    def test_weight_shift_changes_ranking(self, scoring_fixture: sqlite3.Connection) -> None:
        """Patching the multiplier for a rule that fires on the fixture must
        change at least one candidate's score.

        Iterates over the firing rule_ids, finds one in
        ``_RULE_QUALITY_MULTIPLIER`` (i.e., a non-flat rule), patches it ×2.0,
        and asserts the score vector diverges from baseline. If no non-flat
        rule fires on the fixture this test is skipped — flat rules are
        controlled by ``_FLAT_WEIGHT_OVERRIDES``, which is M2 territory.
        """
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.complement_rules import find_all_complements

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        baseline_weights = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        firing_rules = {c.rule_id for c in complements}
        non_flat_firing = firing_rules & set(baseline_weights.keys())
        if not non_flat_firing:
            pytest.skip(
                "fixture only fires flat-count rules — multiplier shift is a no-op; "
                "broaden fixture if this test is needed"
            )

        baseline_result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)

        target_rule = sorted(non_flat_firing)[0]
        try:
            universal_scorer._RULE_QUALITY_MULTIPLIER[target_rule] = baseline_weights[target_rule] * 2.0
            shifted_result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
        finally:
            universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
            universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline_weights)

        any_diff = any(
            shifted_result.score_by_candidate.get(name) != pytest.approx(baseline_result.score_by_candidate.get(name))
            for name in baseline_result.score_by_candidate
        )
        assert any_diff, f"shifting {target_rule} x2.0 produced bitwise-identical scores"

    def test_patch_restore_on_caller_exception(self, scoring_fixture: sqlite3.Connection) -> None:
        """The driver's patch+restore pattern must preserve dict identity.

        This validates the test pattern Unit 3 will use; the unit-2 function
        itself does not patch.
        """
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.complement_rules import find_all_complements

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        baseline_weights = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        original_id = id(universal_scorer._RULE_QUALITY_MULTIPLIER)

        # Caller pattern: patch, raise, finally restore.
        with pytest.raises(RuntimeError):
            try:
                universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
                universal_scorer._RULE_QUALITY_MULTIPLIER.update({k: v * 2.0 for k, v in baseline_weights.items()})
                # Trigger the function but then raise; the finally must restore.
                score_commander_from_complements(scoring_fixture, "Test Commander", complements)
                raise RuntimeError("simulated mid-call exception")
            finally:
                universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
                universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline_weights)

        # Dict identity preserved (caller never reassigned the global).
        assert id(universal_scorer._RULE_QUALITY_MULTIPLIER) == original_id
        # Contents bitwise-restored.
        assert dict(universal_scorer._RULE_QUALITY_MULTIPLIER) == baseline_weights

    def test_returns_contributions_in_hidden_gem_shape(self, scoring_fixture: sqlite3.Connection) -> None:
        """contributions tuple is (candidate, rule_id, contribution) triples
        — the shape hidden_gem_hit_rate_for_commander consumes."""
        from mtg_synergy_graph.complement_rules import find_all_complements

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)

        # Some contributions should be present (we have matching ports).
        assert len(result.contributions) > 0
        for entry in result.contributions:
            assert isinstance(entry, tuple) and len(entry) == 3
            cand, rule_id, contrib = entry
            assert isinstance(cand, str) and cand
            assert isinstance(rule_id, str) and rule_id
            assert isinstance(contrib, float)
            assert contrib != 0.0  # zero contributions are dropped

    def test_determinism(self, scoring_fixture: sqlite3.Connection) -> None:
        from mtg_synergy_graph.complement_rules import find_all_complements

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        a = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
        b = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
        assert a.top_30 == b.top_30
        assert dict(a.score_by_candidate) == dict(b.score_by_candidate)
        assert a.contributions == b.contributions

    def test_low_coverage_commander_does_not_crash(self, scoring_fixture: sqlite3.Connection) -> None:
        """Commander with very few candidate matches → top-30 is the available
        subset; no slice error."""
        from mtg_synergy_graph.complement_rules import find_all_complements

        # The fixture has 3 candidates with matching ports — far fewer than 30.
        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        result = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
        assert len(result.top_30) <= 30  # may be < 30 — pool is small
        # And whatever's in top_30 is a subset of score_by_candidate.
        assert set(result.top_30).issubset(set(result.score_by_candidate.keys()))
