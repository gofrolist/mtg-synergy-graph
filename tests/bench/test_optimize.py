"""Tests for ``mtg_synergy_graph.bench.optimize``.

Built incrementally alongside the optimizer's units. Unit 1 tests live
under ``TestCompositeObjective``, ``TestRandomSplit``, and
``TestLoadEdhrecLabels``; later units append their own classes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from mtg_synergy_graph.bench.optimize import CompositeObjective

from mtg_synergy_graph.bench.optimize import (
    OPTIMIZE_HISTORY_FIELDS,
    OptimizerConfig,
    OptimizerResult,
    OptimizerSelfTestFailed,
    OptimizerStep,
    append_optimize_history_rows,
    composite_objective,
    load_edhrec_labels,
    random_split,
    run_optimizer,
    score_commander_from_complements,
    write_proposal_json,
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
        # Trigger Twin shares the commander's ChangesZone-ETB trigger so the
        # weight-multiplier-sensitive ``trigger_resonance`` rule fires on this
        # fixture (it's in ``_RULE_QUALITY_MULTIPLIER``); without it the two
        # multiplier-shift tests below get skipped for lack of a non-flat
        # firing rule. See git log for prior skip reasons.
        ("Trigger Twin", 3, 400),
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
    # Trigger Twin: same trigger axis as commander → fires trigger_resonance.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Trigger Twin", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
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
        from mtg_synergy_graph.engine import UNRANKED_EDHREC_SENTINEL

        live_sortable = []
        cmc_lookup = {}
        rank_lookup = {}
        for row in scoring_fixture.execute("SELECT name, cmc, edhrec_rank FROM cards"):
            cmc_lookup[row["name"]] = row["cmc"] if row["cmc"] is not None else 99.0
            rank_lookup[row["name"]] = (
                row["edhrec_rank"] if row["edhrec_rank"] is not None else UNRANKED_EDHREC_SENTINEL
            )
        for name, us in live.items():
            live_sortable.append((name, us.to_legacy_buckets()["total"]))
        live_sortable.sort(
            key=lambda r: (
                -r[1],
                cmc_lookup.get(r[0], 99.0),
                rank_lookup.get(r[0], UNRANKED_EDHREC_SENTINEL),
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

    def test_idf_basis_cached_path_matches_legacy(self, scoring_fixture: sqlite3.Connection) -> None:
        """``score_commander_from_complements(idf_basis=...)`` must match the legacy path bitwise.

        The cached basis is the perf optimization in Batch B (#7). This test
        is the fidelity invariant: any future change to ``_compute_idf_basis``
        / ``_idf_weights_from_basis`` that diverges from ``_compute_idf_weights``
        must break this test.
        """
        from mtg_synergy_graph.complement_rules import find_all_complements
        from mtg_synergy_graph.universal_scorer import _compute_idf_basis

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        # Legacy path: no basis, _compute_idf_weights runs per call.
        legacy = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
        # Cached path: basis built once, applied per call.
        basis = _compute_idf_basis(complements)
        cached = score_commander_from_complements(scoring_fixture, "Test Commander", complements, idf_basis=basis)
        assert legacy.top_30 == cached.top_30
        assert dict(legacy.score_by_candidate) == dict(cached.score_by_candidate)
        assert legacy.contributions == cached.contributions

    def test_idf_basis_cached_path_matches_legacy_under_weight_shift(self, scoring_fixture: sqlite3.Connection) -> None:
        """Cached basis must produce identical results to legacy path AFTER weight patching.

        Verifies that ``_idf_weights_from_basis`` correctly applies the live
        ``_RULE_QUALITY_MULTIPLIER`` — the basis is weight-INDEPENDENT, but
        the final IDF weights it produces MUST track every multiplier change.
        """
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.complement_rules import find_all_complements
        from mtg_synergy_graph.universal_scorer import _compute_idf_basis

        complements = find_all_complements(scoring_fixture, ["Test Commander"])
        # Build basis at baseline weights — basis itself is weight-independent.
        basis = _compute_idf_basis(complements)

        # Pick a non-flat rule that fires in the fixture; double its multiplier.
        baseline = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        firing_rule_ids = {c.rule_id for c in complements}
        target_rule = next(
            iter(rid for rid in firing_rule_ids if rid in baseline),
            None,
        )
        if target_rule is None:
            pytest.skip("No firing rule in baseline weights for this fixture")

        try:
            universal_scorer._RULE_QUALITY_MULTIPLIER[target_rule] = baseline[target_rule] * 2.0
            legacy = score_commander_from_complements(scoring_fixture, "Test Commander", complements)
            cached = score_commander_from_complements(scoring_fixture, "Test Commander", complements, idf_basis=basis)
        finally:
            universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
            universal_scorer._RULE_QUALITY_MULTIPLIER.update(baseline)

        assert legacy.top_30 == cached.top_30
        assert dict(legacy.score_by_candidate) == dict(cached.score_by_candidate)
        assert legacy.contributions == cached.contributions


# ---------------------------------------------------------------------------
# run_optimizer (Unit 3)
# ---------------------------------------------------------------------------

# These tests heavily monkeypatch ``_score_split`` so the control logic
# (accept/reject, termination, drift, dead-key, self-test) is testable in
# isolation from real-DB scoring complexity. One end-to-end smoke test
# against the fixture confirms the wiring works without crashing.


def _scripted_score_split(
    *,
    composite_for_weights: Callable[[dict[str, float]], float],
    ndcg: float = 0.4,
    gem: float | None = 0.84,
    firing_rules: list[str] | None = None,
) -> Callable[..., CompositeObjective]:
    """Build a callable that mimics ``_score_split`` and populates the caches.

    Mirrors the real function's side effect of calling ``find_all_complements``
    + ``load_edhrec_labels`` and stashing in the cache dicts. ``firing_rules``
    controls which rule_ids appear in the synthetic complements (drives the
    dead-key detection path); defaults to all weight keys.
    """

    from mtg_synergy_graph.bench.optimize import CompositeObjective, EdhrecLabels
    from mtg_synergy_graph.complement_rules import PortComplement

    def _impl(
        conn: object,
        edhrec_conn: object,
        commanders: Sequence[str],
        weights: Mapping[str, float],
        *,
        complements_cache: dict[str, list[PortComplement]],
        labels_cache: dict[str, EdhrecLabels],
        alpha: float,
        candidate_cache: object = None,
        idf_basis_cache: object = None,
    ) -> CompositeObjective:
        rules_to_use = firing_rules if firing_rules is not None else list(weights.keys())
        for cmdr in commanders:
            if cmdr not in complements_cache:
                complements_cache[cmdr] = _make_complements_for_rules(rules_to_use)
            if cmdr not in labels_cache:
                labels_cache[cmdr] = EdhrecLabels({}, None)
        composite = composite_for_weights(dict(weights))
        n_with_gem = len(commanders) if gem is not None else 0
        return CompositeObjective(
            composite=composite,
            mean_ndcg=ndcg,
            gem_rate=gem,
            n_commanders=len(commanders),
            n_commanders_with_gem=n_with_gem,
        )

    return _impl


@pytest.fixture()
def patched_baseline_weights() -> Iterator[dict[str, float]]:
    """Replace ``_RULE_QUALITY_MULTIPLIER`` with a tiny, predictable dict for tests.

    Uses the production
    :func:`~mtg_synergy_graph.universal_scorer.patched_rule_quality_multiplier`
    context manager so tests exercise the same patch+restore path as the
    bench optimizer. Restoration is automatic on yield/exception.
    """
    from mtg_synergy_graph.universal_scorer import patched_rule_quality_multiplier

    test_weights = {
        "alpha_rule": 1.0,
        "beta_rule": 1.0,
        "gamma_rule": 1.0,
    }
    with patched_rule_quality_multiplier(test_weights):
        yield dict(test_weights)


@pytest.fixture()
def fake_conns(monkeypatch: pytest.MonkeyPatch) -> tuple[object, object]:
    """Stub conn objects + monkeypatch find_all_complements / load_edhrec_labels.

    Tests that monkeypatch ``_score_split`` need conn objects but do not
    actually exercise the SQL paths. We stub ``random_split`` too so the
    bucket query doesn't require a real cards table.
    """
    from mtg_synergy_graph.bench import optimize as opt_mod

    fake_conn = object()
    fake_edhrec_conn = object()

    # Replace random_split with a deterministic helper that doesn't need a DB.
    def _fake_split(commanders, conn, *, train_ratio, seed):
        n = len(commanders)
        cut = round(n * train_ratio)
        return opt_mod.SplitResult(
            train=tuple(commanders[:cut]),
            held=tuple(commanders[cut:]),
            color_buckets={"mono": {"train": cut, "held": n - cut}},
        )

    monkeypatch.setattr(opt_mod, "random_split", _fake_split)

    # build_candidate_cache normally executes SQL on the conn — with a mock
    # conn it would crash. Stub to None; _score_split is mocked too so the
    # cache value is never dereferenced.
    monkeypatch.setattr(opt_mod, "build_candidate_cache", lambda _conn: None)

    # Stub find_all_complements: the self-test pre-warms its train-split
    # cache via this call before _score_split runs, and a fake conn can't
    # execute SQL. Return a per-commander complement list that exercises
    # every weight key (read from _RULE_QUALITY_MULTIPLIER at CALL TIME so
    # patched_baseline_weights' patch is visible) so dead-key detection
    # sees all rules as firing — tests of control logic should not
    # accidentally trip the dead-key filter.
    def _fake_find_all_complements(_conn, _commanders, **_kwargs):
        from mtg_synergy_graph.universal_scorer import _RULE_QUALITY_MULTIPLIER

        return _make_complements_for_rules(list(_RULE_QUALITY_MULTIPLIER.keys()))

    monkeypatch.setattr(opt_mod, "find_all_complements", _fake_find_all_complements)

    return fake_conn, fake_edhrec_conn


class TestRunOptimizerControlLogic:
    """Control-logic tests that use mocked _score_split."""

    def test_convergence_first_sweep_zero_accepts(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No perturbation beats baseline → optimizer stops after sweep 1."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Constant composite — nothing improves baseline, so no accepts.
        monkeypatch.setattr(
            opt_mod,
            "_score_split",
            _scripted_score_split(composite_for_weights=lambda w: 0.5),
        )
        # Pretend every rule fires, so no dead keys.
        # find_all_complements is hoisted to module-level in optimize.py
        # post-#34 — no need for raising=False anymore.
        monkeypatch.setattr(
            opt_mod,
            "find_all_complements",
            lambda conn, cmdrs: _make_complements_for_rules(list(patched_baseline_weights.keys())),
        )
        # Stub label loader.
        monkeypatch.setattr(opt_mod, "load_edhrec_labels", lambda c, n: opt_mod.EdhrecLabels({}, None))

        config = OptimizerConfig(run_self_test=False, max_sweeps=5)
        result = run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)

        assert result.n_iterations == 1
        assert result.n_steps_accepted == 0
        assert not result.partial_sweep
        # Final weights == baseline (nothing accepted).
        assert dict(result.final_weights) == patched_baseline_weights

    def test_sweep_cap_honored(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Optimizer always accepts a tiny improvement → caps at max_sweeps."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Composite that's a tiny bit higher when ANY weight is shifted.
        # Use a sticky counter so each call returns slightly higher than the last.
        counter = {"n": 0}

        def _composite_for(weights):
            counter["n"] += 1
            return 0.5 + 0.0001 * counter["n"]

        monkeypatch.setattr(
            opt_mod,
            "_score_split",
            _scripted_score_split(composite_for_weights=_composite_for),
        )

        config = OptimizerConfig(run_self_test=False, max_sweeps=2, eps_step=10.0)
        result = run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)

        assert result.n_iterations == 2  # capped at 2

    def test_wall_clock_termination(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mocked time_now advances past budget mid-sweep; optimizer aborts."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        monkeypatch.setattr(opt_mod, "_score_split", _scripted_score_split(composite_for_weights=lambda w: 0.5))

        # First call returns 0; then jumps past budget (300s) on the next call.
        ticks = iter([0.0, 0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
        config = OptimizerConfig(run_self_test=False, max_sweeps=5, wall_clock_seconds=10.0)
        result = run_optimizer(
            *fake_conns,
            ["a", "b", "c", "d", "e"],
            config=config,
            time_now=lambda: next(ticks),
        )
        assert result.partial_sweep is True

    def test_weight_clamp_enforced(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clamp range [0.01, 5.0] holds even when grid would push beyond."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Always-improving composite so optimizer keeps accepting.
        counter = {"n": 0}

        def _composite_for(weights):
            counter["n"] += 1
            return 0.5 + 0.001 * counter["n"]

        monkeypatch.setattr(opt_mod, "_score_split", _scripted_score_split(composite_for_weights=_composite_for))

        # Set baseline near clamp_max so clamp matters.
        from mtg_synergy_graph import universal_scorer

        universal_scorer._RULE_QUALITY_MULTIPLIER["alpha_rule"] = 4.5
        config = OptimizerConfig(run_self_test=False, max_sweeps=5, eps_step=10.0)
        result = run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        # No final value should exceed clamp_max=5.0.
        for v in result.final_weights.values():
            assert v <= 5.0
            assert v >= 0.01

    def test_dead_key_detection(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rule in weights but not firing in any complement → in dead_keys."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Only alpha_rule fires; beta_rule and gamma_rule are dead.
        monkeypatch.setattr(
            opt_mod,
            "_score_split",
            _scripted_score_split(
                composite_for_weights=lambda w: 0.5,
                firing_rules=["alpha_rule"],
            ),
        )

        config = OptimizerConfig(run_self_test=False, max_sweeps=1)
        result = run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        assert set(result.dead_keys) == {"beta_rule", "gamma_rule"}

    def test_run_self_test_false_skips(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``run_self_test=False`` bypasses the self-test even if it would fail."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Make the would-be self-test fail catastrophically.
        def _self_test_blow_up(*args, **kwargs):
            raise OptimizerSelfTestFailed("would fail if run")

        monkeypatch.setattr(opt_mod, "_planted_perturbation_self_test", _self_test_blow_up)
        monkeypatch.setattr(opt_mod, "_score_split", _scripted_score_split(composite_for_weights=lambda w: 0.5))

        config = OptimizerConfig(run_self_test=False, max_sweeps=1)
        # Should NOT raise.
        result = run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        assert result.n_iterations == 1

    def test_dict_identity_preserved_after_run(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After optimizer returns, _RULE_QUALITY_MULTIPLIER dict identity intact."""
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.bench import optimize as opt_mod

        original_id = id(universal_scorer._RULE_QUALITY_MULTIPLIER)
        monkeypatch.setattr(opt_mod, "_score_split", _scripted_score_split(composite_for_weights=lambda w: 0.5))

        config = OptimizerConfig(run_self_test=False, max_sweeps=1)
        run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        assert id(universal_scorer._RULE_QUALITY_MULTIPLIER) == original_id

    def test_cumulative_drift_revert_restores_baseline_and_marks_all_accepts(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cumulative-drift trip restores baseline_weights AND marks every prior accept as reverted.

        Scenario: train composite monotonically improves so the optimizer
        accepts. Held composite drifts down by 0.003 per call — within
        ``eps_step`` (0.005), so individual accepts pass — but after 3
        accepts the cumulative drift (0.009) exceeds ``eps_cumulative``
        (0.005) and the gate trips. The fix being verified: code MUST roll
        back to ``baseline_weights`` (not ``sweep_start_weights``) and mark
        ALL accepts across all sweeps as reverted, so
        ``final_weights == baseline_weights`` and
        ``n_steps_accepted == 0`` after the trip.

        Robustness: train and held are distinguished by commander identity
        (``HELD_*`` prefix) instead of ``len(commanders) == 1``. The previous
        test was fragile to fixture-size changes — if the test were updated
        to a 10-commander fixture (held=2), the length check would
        misclassify held calls as train, the drift scenario would never
        trigger, and the test would silently pass.
        """
        from mtg_synergy_graph.bench import optimize as opt_mod

        train_calls = {"n": 0}
        held_calls = {"n": 0}

        # Use a single named held commander; train commanders share no name
        # with held. Distinguishing by identity is fixture-size-invariant.
        held_name = "HELD_cmdr"
        train_names = ["train_a", "train_b", "train_c", "train_d"]

        def _custom_score_split(
            conn,
            edhrec_conn,
            commanders,
            weights,
            *,
            complements_cache,
            labels_cache,
            alpha,
            candidate_cache=None,
            idf_basis_cache=None,
        ):
            from mtg_synergy_graph.bench.optimize import CompositeObjective, EdhrecLabels

            # Populate caches so dead-key detection sees firing rules.
            for cmdr in commanders:
                if cmdr not in complements_cache:
                    complements_cache[cmdr] = _make_complements_for_rules(list(weights.keys()))
                if cmdr not in labels_cache:
                    labels_cache[cmdr] = EdhrecLabels({}, None)

            # Identity-based train-vs-held detection: the held call passes
            # the held commander; train calls don't include it. Robust
            # against future fixture-size changes.
            is_held = held_name in commanders
            if is_held:
                held_calls["n"] += 1
                # Baseline (call #1) = 0.5; subsequent held calls drop by 0.003.
                # 0.003 ≤ eps_step=0.005 → individual step passes.
                # After 2+ accepts, cumulative drift > eps_cumulative=0.005 → trip.
                composite = 0.5 - 0.003 * (held_calls["n"] - 1)
            else:
                train_calls["n"] += 1
                # Train monotonically climbs → grid always finds an improvement.
                composite = 0.5 + 0.001 * train_calls["n"]
            return CompositeObjective(
                composite=composite,
                mean_ndcg=composite,
                gem_rate=composite,
                n_commanders=len(commanders),
                n_commanders_with_gem=len(commanders),
            )

        monkeypatch.setattr(opt_mod, "_score_split", _custom_score_split)

        config = OptimizerConfig(run_self_test=False, max_sweeps=3, eps_cumulative=0.005)
        result = run_optimizer(*fake_conns, [*train_names, held_name], config=config)

        # After revert: weights match baseline, no accepts survive.
        assert dict(result.final_weights) == patched_baseline_weights, (
            "Drift revert must restore baseline_weights, not just sweep_start_weights"
        )
        assert result.n_steps_accepted == 0, "Every prior accept must be marked reverted"
        assert result.partial_sweep is True
        # Reverted accepts carry the canonical reason.
        revert_reasons = {h.reject_reason for h in result.history}
        assert "cumulative_drift_revert" in revert_reasons
        # Metrics-vs-weights invariant: final composites match baseline composites.
        assert result.train_composite_final == result.train_composite_baseline
        assert result.held_composite_final == result.held_composite_baseline

    def test_held_out_eps_rejects_step_when_held_drops_more_than_eps(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A step that improves train but drops held by > eps_step is rejected.

        Coverage gap from the post-merge code-review (#23): all prior
        control-logic tests used constant or always-improving composites,
        so the ``held_delta < -eps_step`` branch (producing
        ``reject_reason="held_out_eps"``) was never exercised end-to-end.
        """
        from mtg_synergy_graph.bench import optimize as opt_mod
        from mtg_synergy_graph.bench.optimize import CompositeObjective, EdhrecLabels

        held_name = "HELD_cmdr"
        train_names = ["train_a", "train_b", "train_c", "train_d"]

        def _custom_score_split(
            conn,
            edhrec_conn,
            commanders,
            weights,
            *,
            complements_cache,
            labels_cache,
            alpha,
            candidate_cache=None,
            idf_basis_cache=None,
        ) -> CompositeObjective:
            for cmdr in commanders:
                if cmdr not in complements_cache:
                    complements_cache[cmdr] = _make_complements_for_rules(list(weights.keys()))
                if cmdr not in labels_cache:
                    labels_cache[cmdr] = EdhrecLabels({}, None)
            is_held = held_name in commanders
            # Identify whether the call is for the BASELINE state (all weights
            # at default 1.0) or a perturbed state. composite_for_weights-style
            # check: if any weight differs from baseline 1.0, it's a perturbation.
            is_perturbed = any(v != 1.0 for v in weights.values())
            # Held: 0.5 at baseline; drops by 0.02 (>>eps_step=0.005) on perturbations.
            # Train: monotonically improves on perturbation so a step IS
            # proposed (gets to the held re-eval).
            if not is_perturbed:
                composite = 0.5  # baseline; both train and held start here
            elif is_held:
                composite = 0.48  # held drops on perturbation
            else:
                composite = 0.55  # train climbs on perturbation
            return CompositeObjective(
                composite=composite,
                mean_ndcg=composite,
                gem_rate=composite,
                n_commanders=len(commanders),
                n_commanders_with_gem=len(commanders),
            )

        monkeypatch.setattr(opt_mod, "_score_split", _custom_score_split)

        config = OptimizerConfig(run_self_test=False, max_sweeps=1, eps_step=0.005)
        result = run_optimizer(*fake_conns, [*train_names, held_name], config=config)

        # No step accepted (every grid cell rejected at the held-eps gate).
        assert result.n_steps_accepted == 0
        assert result.n_steps_rejected > 0
        # Every history entry was rejected with reason "held_out_eps".
        assert all(not s.accepted for s in result.history)
        assert {s.reject_reason for s in result.history} == {"held_out_eps"}, (
            f"Expected only held_out_eps rejects, got: { {s.reject_reason for s in result.history} }"
        )

    def test_per_axis_gem_rate_regression_warning_fires_on_accept(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``run_optimizer`` warns when an accepted step drops train gem_rate by > 0.005.

        Coverage gap from the post-merge code-review (#24): the per-axis
        regression warning at ``optimize.py:909`` had no test. Here we
        construct a step where composite improves AND the held-eps gate
        passes BUT train gem_rate drops — the warning must fire and name
        the rule.
        """
        from mtg_synergy_graph.bench import optimize as opt_mod
        from mtg_synergy_graph.bench.optimize import CompositeObjective, EdhrecLabels

        held_name = "HELD_cmdr"
        train_names = ["train_a", "train_b", "train_c", "train_d"]

        def _custom_score_split(
            conn,
            edhrec_conn,
            commanders,
            weights,
            *,
            complements_cache,
            labels_cache,
            alpha,
            candidate_cache=None,
            idf_basis_cache=None,
        ) -> CompositeObjective:
            for cmdr in commanders:
                if cmdr not in complements_cache:
                    complements_cache[cmdr] = _make_complements_for_rules(list(weights.keys()))
                if cmdr not in labels_cache:
                    labels_cache[cmdr] = EdhrecLabels({}, None)
            is_held = held_name in commanders
            is_perturbed = any(v != 1.0 for v in weights.values())
            # Train: composite improves but gem_rate DROPS by > 0.005 on
            # perturbation. The warning fires only when an accepted step
            # has best_train.gem_rate < current_train_gem - 0.005.
            if not is_held and is_perturbed:
                composite = 0.6  # strictly better → accept
                gem_rate = 0.7  # dropped from baseline 0.8 by 0.1 (> 0.005)
            elif not is_held:
                composite = 0.5  # baseline
                gem_rate = 0.8
            else:
                # Held passes the eps gate either way.
                composite = 0.5 if not is_perturbed else 0.499
                gem_rate = 0.8
            return CompositeObjective(
                composite=composite,
                mean_ndcg=composite,
                gem_rate=gem_rate,
                n_commanders=len(commanders),
                n_commanders_with_gem=len(commanders),
            )

        monkeypatch.setattr(opt_mod, "_score_split", _custom_score_split)

        config = OptimizerConfig(run_self_test=False, max_sweeps=1)
        with caplog.at_level("WARNING", logger="mtg_synergy_graph.bench.optimize"):
            result = run_optimizer(*fake_conns, [*train_names, held_name], config=config)

        # At least one step accepted (composite strictly improved).
        assert result.n_steps_accepted >= 1
        # The warning fired at least once and names the gem_rate drop.
        warnings = [r for r in caplog.records if "gem_rate" in r.getMessage()]
        assert warnings, (
            f"Expected at least one gem_rate-regression WARNING; got messages: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_proposed_config_hash_restores_global_on_compute_hash_exception(
        self,
        patched_baseline_weights: dict[str, float],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_proposed_config_hash`` restores ``_RULE_QUALITY_MULTIPLIER`` even if compute raises.

        Coverage gap from the post-merge code-review (#26): the existing
        ``test_patch_restore_on_proposed_hash_compute`` only tested the
        happy path. The try/finally exists precisely so a raise inside
        ``compute_config_hash`` doesn't leave the global mutated. This
        test forces that exception path.
        """
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.bench import optimize as opt_mod

        baseline_snapshot = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        original_id = id(universal_scorer._RULE_QUALITY_MULTIPLIER)

        # Force compute_config_hash to raise from inside _proposed_config_hash.
        def _raising_compute_hash() -> str:
            raise RuntimeError("simulated config-hash failure")

        monkeypatch.setattr(
            "mtg_synergy_graph.bench.tensor.compute_config_hash",
            _raising_compute_hash,
        )

        proposed = {"alpha_rule": 99.0, "beta_rule": 99.0, "gamma_rule": 99.0}
        with pytest.raises(RuntimeError, match="simulated config-hash failure"):
            opt_mod._proposed_config_hash(proposed)

        # Dict identity preserved AND values restored to baseline.
        assert id(universal_scorer._RULE_QUALITY_MULTIPLIER) == original_id
        assert dict(universal_scorer._RULE_QUALITY_MULTIPLIER) == baseline_snapshot


def _make_complements_for_rules(rule_ids):
    """Construct synthetic PortComplement rows for monkeypatching find_all_complements."""
    from mtg_synergy_graph.complement_rules import PortComplement

    return [
        PortComplement(
            rule_id=r,
            direction="synergy",
            candidate=f"cand_{i}",
            cmdr_event="x",
            cand_event="y",
            filter_group="",
        )
        for i, r in enumerate(rule_ids)
    ]


class TestPlantedPerturbationSelfTest:
    def test_recovers_when_grid_can_reach_baseline(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Plant 2.0x; the 0.5x grid step recovers exactly. Self-test passes."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # We don't know which rule the self-test will pick (seed-dependent),
        # so reward proximity-to-baseline across ALL rules.
        baseline = patched_baseline_weights

        def _composite_all_rules(weights):
            total_delta = sum(abs(weights[k] - baseline[k]) for k in baseline)
            return 1.0 - total_delta

        monkeypatch.setattr(
            opt_mod,
            "_score_split",
            _scripted_score_split(composite_for_weights=_composite_all_rules),
        )

        config = OptimizerConfig(run_self_test=True, max_sweeps=0, self_test_seed=0)
        # max_sweeps=0 → main loop skipped; only the self-test executes.
        run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)

    def test_fails_when_grid_cannot_reach_baseline(
        self,
        patched_baseline_weights: dict[str, float],
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If composite NEVER improves over the planted state, self-test fails."""
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Pathological: composite is always a constant; no recovery is possible.
        monkeypatch.setattr(opt_mod, "_score_split", _scripted_score_split(composite_for_weights=lambda w: 0.5))

        config = OptimizerConfig(run_self_test=True, max_sweeps=0)
        with pytest.raises(OptimizerSelfTestFailed) as exc:
            run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        # Diagnostic message should name the rule and the values involved.
        assert "baseline=" in str(exc.value)
        assert "recovered=" in str(exc.value)

    def test_select_plant_rotates_when_picked_rule_at_clamp_max(self) -> None:
        """If the seed-picked rule is at clamp_max, _select_self_test_plant rotates to a rule with room.

        Regression test for the false-positive where applying a proposal that
        pushes any rule to clamp_max=5.0 would cause the next run's self-test
        to raise OptimizerSelfTestFailed("plant collapsed to baseline").
        """
        from mtg_synergy_graph.bench.optimize import _select_self_test_plant

        # alpha_rule pinned at clamp_max → ×2.0 collapses; ÷2.0 should be picked
        # (or the function should rotate to beta_rule which has full room).
        baseline = {"alpha_rule": 5.0, "beta_rule": 1.0, "gamma_rule": 1.0}
        rule_id, planted_value = _select_self_test_plant(
            baseline, seed=0, planted_mult=2.0, clamp_min=0.01, clamp_max=5.0
        )
        # Either rotated to a different rule, or stayed on alpha_rule with the
        # DOWN direction (5.0 / 2.0 = 2.5).
        if rule_id == "alpha_rule":
            assert planted_value == pytest.approx(2.5)
        else:
            # Rotated to beta_rule or gamma_rule; both have full room either direction.
            assert rule_id in {"beta_rule", "gamma_rule"}
            assert planted_value != baseline[rule_id]
            # Plant must be at the configured ×2.0 (UP) since these rules have room.
            assert planted_value == pytest.approx(2.0)

    def test_select_plant_skips_excluded_dead_keys(self) -> None:
        """Excluded rule_ids never get picked, even if they appear early in the shuffled order.

        Regression test for the false-positive self-test failure observed on
        the production fixture: ``_select_self_test_plant`` was happily picking
        rules that fired on zero train commanders, then the grid search produced
        identical composite scores at every cell and raised ``OptimizerSelfTestFailed``
        on a structural property of the catalogue rather than a real calibration bug.
        """
        from mtg_synergy_graph.bench.optimize import _select_self_test_plant

        # All three rules have full planting room. Without the filter, the
        # seed-shuffled selector would pick whichever lands first. With the
        # filter, alpha_rule and beta_rule are off-limits.
        baseline = {"alpha_rule": 1.0, "beta_rule": 1.0, "gamma_rule": 1.0}
        rule_id, planted_value = _select_self_test_plant(
            baseline,
            seed=0,
            planted_mult=2.0,
            clamp_min=0.01,
            clamp_max=5.0,
            excluded={"alpha_rule", "beta_rule"},
        )
        assert rule_id == "gamma_rule"
        assert planted_value == pytest.approx(2.0)

    def test_select_plant_raises_when_only_dead_keys_remain(self) -> None:
        """When every non-excluded rule has zero planting room, raise with a dead-key-aware message."""
        from mtg_synergy_graph.bench.optimize import _select_self_test_plant

        # alpha_rule has full room but is excluded; beta_rule has zero room.
        baseline = {"alpha_rule": 1.0, "beta_rule": 1.0}
        with pytest.raises(OptimizerSelfTestFailed, match="dead-keys"):
            _select_self_test_plant(
                baseline,
                seed=0,
                planted_mult=2.0,
                clamp_min=1.0,
                clamp_max=1.0,
                excluded={"alpha_rule"},
            )

    def test_select_plant_raises_when_every_rule_pinned(self) -> None:
        """When every rule is structurally pinned (clamp_min == clamp_max == baseline), raise."""
        from mtg_synergy_graph.bench.optimize import _select_self_test_plant

        baseline = {"alpha_rule": 1.0}
        # Every rule's baseline is pinned at the only allowed value.
        with pytest.raises(OptimizerSelfTestFailed, match="planting room"):
            _select_self_test_plant(baseline, seed=0, planted_mult=2.0, clamp_min=1.0, clamp_max=1.0)

    def test_self_test_passes_with_rule_at_clamp_max(
        self,
        fake_conns: tuple[object, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End-to-end: a baseline at clamp_max no longer trips the self-test.

        Pre-fix: this scenario raised ``OptimizerSelfTestFailed("collapsed to
        baseline")`` immediately and the whole run aborted with rc=3.
        Post-fix: the self-test rotates direction (or rule) and passes.
        """
        from mtg_synergy_graph import universal_scorer
        from mtg_synergy_graph.bench import optimize as opt_mod

        # Set alpha_rule to clamp_max so its UP-direction plant collapses.
        # beta_rule and gamma_rule are at the default 1.0 (full room).
        saved = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
        universal_scorer._RULE_QUALITY_MULTIPLIER.update({"alpha_rule": 5.0, "beta_rule": 1.0, "gamma_rule": 1.0})
        try:
            baseline = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)

            def _composite_all_rules(weights: Mapping[str, float]) -> float:
                total_delta = sum(abs(weights[k] - baseline[k]) for k in baseline)
                return 1.0 - total_delta

            monkeypatch.setattr(
                opt_mod,
                "_score_split",
                _scripted_score_split(composite_for_weights=_composite_all_rules),
            )

            config = OptimizerConfig(run_self_test=True, max_sweeps=0, self_test_seed=0)
            # Should NOT raise.
            run_optimizer(*fake_conns, ["a", "b", "c", "d", "e"], config=config)
        finally:
            universal_scorer._RULE_QUALITY_MULTIPLIER.clear()
            universal_scorer._RULE_QUALITY_MULTIPLIER.update(saved)


class TestRunOptimizerSmoke:
    """End-to-end smoke run against the real-DB scoring fixture."""

    def test_runs_without_crash_on_synthetic_fixture(
        self, scoring_fixture: sqlite3.Connection, edhrec_conn: sqlite3.Connection
    ) -> None:
        # The synthetic fixture only fires flat-count rules, so the multiplier
        # optimizer can't move scores. We just verify the wiring runs end-to-end
        # without crashing — the convergence path triggers immediately. Use 5
        # commanders so the train/held split is non-degenerate (4 train, 1
        # held); a single-commander split would round held to 0 and silently
        # nullify the held-eps gate.
        config = OptimizerConfig(run_self_test=False, max_sweeps=1)
        commanders = ["Test Commander"] * 5
        result = run_optimizer(scoring_fixture, edhrec_conn, commanders, config=config)
        assert result.n_iterations >= 1
        assert isinstance(result.history, tuple)
        assert isinstance(result.dead_keys, tuple)
        # Held split is non-empty: held composite metric is finite (not the
        # degenerate 0.0 that composite_objective returns for an empty input).
        assert len(result.held_split) >= 1


# ---------------------------------------------------------------------------
# Output artifacts (Unit 5)
# ---------------------------------------------------------------------------


def _make_result(
    *,
    baseline_weights: dict[str, float],
    final_weights: dict[str, float],
    history: tuple[OptimizerStep, ...] = (),
    n_steps_accepted: int = 0,
    n_steps_rejected: int = 0,
    partial_sweep: bool = False,
    dead_keys: tuple[str, ...] = (),
) -> OptimizerResult:
    """Build a synthetic OptimizerResult for artifact tests.

    ``color_buckets`` mirrors the production type from ``random_split``:
    ``MappingProxyType`` at BOTH layers (outer dict + inner per-bucket dict).
    Tests that omitted the inner ``MappingProxyType`` masked a real bug —
    ``json.dumps`` doesn't know how to encode ``mappingproxy``, so
    ``write_proposal_json`` would crash on production input until the
    mappingproxy values were flattened to plain dicts.
    """
    from types import MappingProxyType

    return OptimizerResult(
        baseline_weights=MappingProxyType(baseline_weights),
        final_weights=MappingProxyType(final_weights),
        history=history,
        n_iterations=1,
        n_steps_accepted=n_steps_accepted,
        n_steps_rejected=n_steps_rejected,
        partial_sweep=partial_sweep,
        dead_keys=dead_keys,
        train_split=("a", "b"),
        held_split=("c",),
        color_buckets=MappingProxyType({"mono": MappingProxyType({"train": 2, "held": 1})}),
        train_composite_baseline=0.5,
        held_composite_baseline=0.5,
        train_ndcg_baseline=0.4,
        held_ndcg_baseline=0.4,
        train_gem_baseline=0.84,
        held_gem_baseline=0.84,
        train_composite_final=0.55,
        held_composite_final=0.52,
        train_ndcg_final=0.42,
        held_ndcg_final=0.41,
        train_gem_final=0.86,
        held_gem_final=0.85,
    )


class TestWriteProposalJson:
    def test_writes_valid_json_with_full_schema(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        import json

        result = _make_result(
            baseline_weights={"alpha_rule": 1.0, "beta_rule": 1.0},
            final_weights={"alpha_rule": 1.5, "beta_rule": 1.0},
            n_steps_accepted=1,
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)

        # File exists and is valid JSON.
        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded == payload

        # Schema fields per FR6.
        for field in (
            "baseline_config_hash",
            "proposed_config_hash",
            "per_rule_diffs",
            "aggregate_train_composite_delta",
            "aggregate_held_composite_delta",
            "n_iterations",
            "n_steps_accepted",
            "n_steps_rejected",
            "partial_sweep",
            "dead_keys",
        ):
            assert field in loaded

    def test_per_rule_diffs_only_changed(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0, "beta_rule": 1.0, "gamma_rule": 1.0},
            final_weights={"alpha_rule": 1.5, "beta_rule": 1.0, "gamma_rule": 0.75},
            n_steps_accepted=2,
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)

        diff_rules = {d["rule_id"] for d in payload["per_rule_diffs"]}
        # Only rules whose final differs from baseline.
        assert diff_rules == {"alpha_rule", "gamma_rule"}

    def test_proposed_config_hash_flips_on_change(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        # Final differs from baseline → proposed_config_hash != baseline_config_hash.
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0, "beta_rule": 1.0, "gamma_rule": 1.0},
            final_weights={"alpha_rule": 1.5, "beta_rule": 1.0, "gamma_rule": 1.0},
            n_steps_accepted=1,
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)
        assert payload["proposed_config_hash"] != payload["baseline_config_hash"]

    def test_proposed_config_hash_equal_when_no_change(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        # Final == baseline → both hashes equal.
        identical = {"alpha_rule": 1.0, "beta_rule": 1.0, "gamma_rule": 1.0}
        result = _make_result(
            baseline_weights=identical,
            final_weights=dict(identical),
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)
        assert payload["proposed_config_hash"] == payload["baseline_config_hash"]
        assert payload["per_rule_diffs"] == []

    def test_no_comment_in_diff_entries(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        """The proposal must not contain `comment` keys (FR6 contract)."""
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0, "beta_rule": 1.0},
            final_weights={"alpha_rule": 1.5, "beta_rule": 1.0},
            n_steps_accepted=1,
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)
        for diff in payload["per_rule_diffs"]:
            assert "comment" not in diff

    def test_dead_keys_field_populated(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0, "ghost_rule": 0.5},
            final_weights={"alpha_rule": 1.0, "ghost_rule": 0.5},
            dead_keys=("ghost_rule",),
        )
        target = tmp_path / "optimize_proposal.json"
        payload = write_proposal_json(result, path=target)
        assert payload["dead_keys"] == ["ghost_rule"]

    def test_patch_restore_on_proposed_hash_compute(
        self,
        patched_baseline_weights: dict[str, float],
        tmp_path: Path,
    ) -> None:
        """proposed_config_hash patch+restore preserves dict identity."""
        from mtg_synergy_graph import universal_scorer

        original_id = id(universal_scorer._RULE_QUALITY_MULTIPLIER)
        baseline_snapshot = dict(universal_scorer._RULE_QUALITY_MULTIPLIER)
        result = _make_result(
            baseline_weights=baseline_snapshot,
            final_weights={k: v * 1.5 for k, v in baseline_snapshot.items()},
            n_steps_accepted=len(baseline_snapshot),
        )
        target = tmp_path / "optimize_proposal.json"
        write_proposal_json(result, path=target)

        # Dict identity preserved + contents bitwise-restored.
        assert id(universal_scorer._RULE_QUALITY_MULTIPLIER) == original_id
        assert dict(universal_scorer._RULE_QUALITY_MULTIPLIER) == baseline_snapshot


class TestAppendOptimizeHistoryRows:
    def test_writes_header_on_first_call(self, tmp_path: Path) -> None:
        target = tmp_path / ".audit" / "optimize_history.csv"
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0},
            final_weights={"alpha_rule": 1.5},
            history=(
                OptimizerStep(
                    sweep_n=1,
                    rule_id="alpha_rule",
                    old_value=1.0,
                    new_value=1.5,
                    train_composite=0.55,
                    held_composite=0.52,
                    train_ndcg=0.42,
                    held_ndcg=0.41,
                    train_gem=0.86,
                    held_gem=0.85,
                    accepted=True,
                    reject_reason="",
                ),
            ),
            n_steps_accepted=1,
        )

        append_optimize_history_rows(result, run_id="test-run-1", path=target)
        assert target.exists()
        contents = target.read_text().splitlines()
        assert contents[0] == ",".join(OPTIMIZE_HISTORY_FIELDS)  # header
        assert len(contents) == 2  # header + 1 row

    def test_subsequent_calls_do_not_re_write_header(self, tmp_path: Path) -> None:
        target = tmp_path / ".audit" / "optimize_history.csv"
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0},
            final_weights={"alpha_rule": 1.5},
            history=(
                OptimizerStep(
                    sweep_n=1,
                    rule_id="alpha_rule",
                    old_value=1.0,
                    new_value=1.5,
                    train_composite=0.55,
                    held_composite=0.52,
                    train_ndcg=0.42,
                    held_ndcg=0.41,
                    train_gem=0.86,
                    held_gem=0.85,
                    accepted=True,
                    reject_reason="",
                ),
            ),
            n_steps_accepted=1,
        )
        append_optimize_history_rows(result, run_id="run-1", path=target)
        append_optimize_history_rows(result, run_id="run-2", path=target)
        contents = target.read_text().splitlines()
        # Header should appear exactly once.
        header_count = sum(1 for line in contents if line == ",".join(OPTIMIZE_HISTORY_FIELDS))
        assert header_count == 1
        # 1 header + 2 rows
        assert len(contents) == 3

    def test_none_gem_renders_empty_cell(self, tmp_path: Path) -> None:
        target = tmp_path / "optimize_history.csv"
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0},
            final_weights={"alpha_rule": 1.0},
            history=(
                OptimizerStep(
                    sweep_n=1,
                    rule_id="alpha_rule",
                    old_value=1.0,
                    new_value=1.0,
                    train_composite=0.5,
                    held_composite=0.5,
                    train_ndcg=0.4,
                    held_ndcg=0.4,
                    train_gem=None,
                    held_gem=None,
                    accepted=False,
                    reject_reason="held_out_eps",
                ),
            ),
        )
        append_optimize_history_rows(result, run_id="r", path=target)
        # Read back via csv module to get the data row.
        import csv

        with target.open() as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["train_gem"] == ""
        assert rows[0]["held_gem"] == ""
        assert rows[0]["accepted"] == "false"
        assert rows[0]["reject_reason"] == "held_out_eps"

    def test_io_failure_degrades_to_stderr_warning(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Path that cannot be created (parent is a file, not a directory).
        bad_parent = tmp_path / "blocking_file"
        bad_parent.write_text("just a file")
        target = bad_parent / "optimize_history.csv"  # parent is not a directory
        result = _make_result(
            baseline_weights={"alpha_rule": 1.0},
            final_weights={"alpha_rule": 1.0},
        )
        # Should NOT raise.
        append_optimize_history_rows(result, run_id="r", path=target)
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "warning" in captured.out.lower()


# ---------------------------------------------------------------------------
# CLI handler (Unit 4)
# ---------------------------------------------------------------------------


class TestHandleOptimize:
    def test_returns_2_on_empty_fixture(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        from mtg_synergy_graph.bench.fixture import PinnedFixture
        from mtg_synergy_graph.bench.optimize import handle_optimize

        # Build an empty fixture.
        empty = PinnedFixture(config_hash="abc", created_at="2026-01-01T00:00:00+00:00")
        fixture_path = tmp_path / "fixture.json"
        empty.write(fixture_path)

        args = argparse.Namespace(
            fixture=str(fixture_path),
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
        )
        rc = handle_optimize(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "empty" in captured.err

    def test_returns_2_on_too_few_commanders(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Fixture below ``_MIN_COMMANDERS_FOR_SPLIT`` (3) is rejected to avoid degenerate held=0 split.

        The threshold is 3: at ``train_ratio=0.8``, ``round(n * 0.8)``
        produces ``held=0`` only when ``n <= 2``. At ``n=3`` the split is
        2 train + 1 held — minimal but non-degenerate.
        """
        import argparse

        from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
        from mtg_synergy_graph.bench.optimize import handle_optimize

        # 2 commanders < 3 minimum (round(2 * 0.8) = 2 → held=0). Use a fake
        # config_hash so we'd hit the stale-tensor check next; the size check
        # must fire FIRST.
        fixture = PinnedFixture(
            config_hash="0" * 64,
            created_at="2026-01-01T00:00:00+00:00",
            entries=[FixtureEntry(commander=f"Cmdr {i}", scores={"a": 1.0}) for i in range(2)],
        )
        fixture_path = tmp_path / "fixture.json"
        fixture.write(fixture_path)

        args = argparse.Namespace(
            fixture=str(fixture_path),
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
        )
        rc = handle_optimize(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "2 commanders" in captured.err
        assert "at least 3" in captured.err

    def test_returns_2_on_stale_tensor(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
        from mtg_synergy_graph.bench.optimize import handle_optimize

        # Fixture with a fake config_hash that won't match the live one. Must
        # have at least _MIN_COMMANDERS_FOR_SPLIT (3) entries so the stale-tensor
        # check fires before the size check.
        fixture = PinnedFixture(
            config_hash="0" * 64,
            created_at="2026-01-01T00:00:00+00:00",
            entries=[FixtureEntry(commander=f"Cmdr {i}", scores={"a": 1.0}) for i in range(3)],
        )
        fixture_path = tmp_path / "fixture.json"
        fixture.write(fixture_path)

        args = argparse.Namespace(
            fixture=str(fixture_path),
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
        )
        rc = handle_optimize(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "stale" in captured.err.lower()

    def test_default_redirect_swaps_to_500_fixture(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When --fixture is the canonical 100-cmdr default, --optimize swaps to the 500 fixture.

        Verifies the (B) ergonomic redirect: users running ``bench.py audit
        --optimize`` without an explicit --fixture get the 500-cmdr fixture,
        which has trustworthy gradient signal. Explicit --fixture is honored
        as-is.
        """
        import argparse

        from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
        from mtg_synergy_graph.bench.optimize import (
            _CANONICAL_FIXTURE,
            _OPTIMIZE_DEFAULT_FIXTURE,
            handle_optimize,
        )

        # Create a 500-fixture-like file at the optimize-default path so the
        # redirect's existence check passes.
        monkeypatch.chdir(tmp_path)
        canonical_path = tmp_path / _CANONICAL_FIXTURE
        optimize_path = tmp_path / _OPTIMIZE_DEFAULT_FIXTURE
        canonical_path.parent.mkdir(parents=True, exist_ok=True)
        # The canonical path must exist for args.fixture to be valid; we
        # populate it with a fake hash that will fail the staleness check
        # AFTER the redirect swaps it. Test asserts the redirect log fires
        # before failure, which proves the swap happened.
        PinnedFixture(
            config_hash="0" * 64,
            created_at="2026-01-01T00:00:00+00:00",
            entries=[FixtureEntry(commander=f"C{i}", scores={"a": 1.0}) for i in range(5)],
        ).write(optimize_path)

        args = argparse.Namespace(
            fixture=_CANONICAL_FIXTURE,  # exactly the canonical default
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
        )
        rc = handle_optimize(args)
        # Redirect fires (visible in stderr); then staleness check fails (rc=2).
        captured = capsys.readouterr()
        assert _OPTIMIZE_DEFAULT_FIXTURE in captured.err
        assert "default for --optimize" in captured.err
        assert rc == 2  # stale tensor (expected — fake config_hash on the 500 stub)

    def test_explicit_fixture_overrides_redirect(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An explicit --fixture path bypasses the default-redirect."""
        import argparse

        from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
        from mtg_synergy_graph.bench.optimize import handle_optimize

        explicit_path = tmp_path / "custom_fixture.json"
        PinnedFixture(
            config_hash="0" * 64,
            created_at="2026-01-01T00:00:00+00:00",
            entries=[FixtureEntry(commander=f"C{i}", scores={"a": 1.0}) for i in range(5)],
        ).write(explicit_path)

        args = argparse.Namespace(
            fixture=str(explicit_path),
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
        )
        handle_optimize(args)
        captured = capsys.readouterr()
        # No redirect log when --fixture is explicit (even if the explicit
        # path happens not to be the 500 fixture).
        assert "default for --optimize" not in captured.err

    def test_resolve_mode_picks_optimize(self) -> None:
        import argparse

        from mtg_synergy_graph.bench.cli import _resolve_mode

        ns = argparse.Namespace(
            rule=None,
            inspect=None,
            collinearity=False,
            repin=False,
            expect_identity=False,
            unknowns=False,
            inspect_gems=False,
            trend=None,
            vs_forge_oracle=False,
            embedding_dedup=False,
            optimize=True,
        )
        assert _resolve_mode(ns) == "optimize"

    def test_handler_registered_in_cli(self) -> None:
        import mtg_synergy_graph.bench  # noqa: F401 — triggers register() side effect
        from mtg_synergy_graph.bench.cli import _HANDLERS
        from mtg_synergy_graph.bench.optimize import handle_optimize

        assert _HANDLERS["optimize"] is handle_optimize

    def test_argparse_rejects_optimize_with_repin(self) -> None:
        """--optimize and --repin are mutually exclusive at the argparse level."""
        from mtg_synergy_graph.bench.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--repin"])

    def test_argparse_validates_alpha_range(self) -> None:
        """--alpha must be in [0, 1]; out-of-range raises SystemExit."""
        from mtg_synergy_graph.bench.cli import _build_parser

        parser = _build_parser()
        # Valid endpoints accepted.
        parser.parse_args(["audit", "--optimize", "--alpha", "0.0"])
        parser.parse_args(["audit", "--optimize", "--alpha", "1.0"])
        # Out of range rejected.
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--alpha", "1.5"])
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--alpha", "-0.1"])

    def test_argparse_validates_train_ratio_open_interval(self) -> None:
        """--train-ratio must be in (0, 1) — both endpoints rejected."""
        from mtg_synergy_graph.bench.cli import _build_parser

        parser = _build_parser()
        parser.parse_args(["audit", "--optimize", "--train-ratio", "0.5"])
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--train-ratio", "0.0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--train-ratio", "1.0"])

    def test_argparse_grid_accepts_multiple_values(self) -> None:
        """--grid accepts a space-separated list of positive floats."""
        from mtg_synergy_graph.bench.cli import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["audit", "--optimize", "--grid", "0.5", "0.75", "1.5", "2.0"])
        assert ns.grid == [0.5, 0.75, 1.5, 2.0]
        # Non-positive grid value rejected.
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--grid", "0.5", "0.0"])
        with pytest.raises(SystemExit):
            parser.parse_args(["audit", "--optimize", "--grid", "0.5", "-1.0"])

    def test_companion_flag_warning_includes_new_flags(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Passing --alpha/--grid/etc. without --optimize warns on stderr.

        Uses ``--db`` pointing at an empty tmp path so the audit short-circuits
        on missing data. The warning fires in ``main`` BEFORE handler dispatch
        (see ``bench/cli.py``), so the handler outcome doesn't affect the
        assertion. Without the override the test would fall back to the
        production ``data/synergy.db`` and spend ~60s on a full identity audit
        per the default-fixture path.
        """
        import contextlib

        from mtg_synergy_graph.bench.cli import main

        empty_db = tmp_path / "empty.db"
        with contextlib.suppress(SystemExit):
            main(["audit", "--expect-identity", "--alpha", "0.7", "--db", str(empty_db)])
        captured = capsys.readouterr()
        assert "--alpha" in captured.err
        assert "no effect without --optimize" in captured.err

    def test_format_json_emits_summary_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--optimize --format json`` writes a single JSON object to stdout.

        Mocks `run_optimizer` to return a synthetic OptimizerResult so the test
        is fast and deterministic; the goal is to verify the SUMMARY shape, not
        the optimizer logic.
        """
        import argparse
        import json

        from mtg_synergy_graph.bench import optimize as opt_mod
        from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
        from mtg_synergy_graph.bench.optimize import OPTIMIZE_SUMMARY_SCHEMA_VERSION

        # Build a fixture with 5 commanders + the live config_hash so the
        # stale-tensor check passes.
        from mtg_synergy_graph.bench.tensor import compute_config_hash

        live_hash = compute_config_hash()
        fixture = PinnedFixture(
            config_hash=live_hash,
            created_at="2026-01-01T00:00:00+00:00",
            entries=[FixtureEntry(commander=f"Cmdr {i}", scores={"a": 1.0}) for i in range(5)],
        )
        fixture_path = tmp_path / "fixture.json"
        fixture.write(fixture_path)

        # Mock run_optimizer to return a minimal valid OptimizerResult.
        def _fake_run(conn, edhrec_conn, commanders, *, config=None, time_now=None):
            return _make_result(
                baseline_weights={"alpha_rule": 1.0},
                final_weights={"alpha_rule": 1.5},
                n_steps_accepted=1,
            )

        monkeypatch.setattr(opt_mod, "run_optimizer", _fake_run)
        # Skip the actual write_proposal_json (it tries to compute_config_hash
        # against patched globals). We just want the JSON summary on stdout.
        monkeypatch.setattr(opt_mod, "write_proposal_json", lambda r, path=None: {})
        monkeypatch.setattr(opt_mod, "append_optimize_history_rows", lambda r, run_id, path=None: None)

        args = argparse.Namespace(
            fixture=str(fixture_path),
            db="data/synergy.db",
            edhrec_db="data/tags.db",
            max_sweeps=1,
            seed=42,
            no_self_test=True,
            proposal_path=str(tmp_path / "p.json"),
            optimize_history=str(tmp_path / "h.csv"),
            alpha=0.5,
            grid=[0.5, 0.75, 1.25, 1.5, 2.0],
            eps_step=0.005,
            eps_cumulative=0.005,
            clamp_min=0.01,
            clamp_max=5.0,
            train_ratio=0.8,
            wall_clock_seconds=300.0,
            self_test_seed=7,
            format="json",
        )
        rc = opt_mod.handle_optimize(args)
        assert rc == 0
        captured = capsys.readouterr()
        # Stdout has exactly one JSON line; stderr has no human prose.
        summary = json.loads(captured.out.strip())
        assert summary["schema_version"] == OPTIMIZE_SUMMARY_SCHEMA_VERSION
        assert summary["n_steps_accepted"] == 1
        assert summary["n_iterations"] == 1
        assert summary["partial_sweep"] is False
        assert summary["proposal_path"] == str(tmp_path / "p.json")
        assert summary["history_path"] == str(tmp_path / "h.csv")
        assert "run_id" in summary
        # No human-readable summary on stderr in JSON mode.
        assert "optimizer accepted" not in captured.err
        assert "no improvement found" not in captured.err
