"""Unit 1 of plan 003 — hidden-gem metric core.

Pure-function tests for ``plausibility`` +
``hidden_gem_hit_rate_for_commander`` +
``aggregate_hidden_gem_hit_rate``.

Every scenario here is DB-agnostic: tests pass tensor rows as plain
``(candidate, rule_id, contribution)`` tuples. The module stays DB-free
by design; Unit 2 shapes sqlite rows into this form.
"""

from __future__ import annotations

import dataclasses

import pytest

from mtg_synergy_graph.bench.hidden_gems import (
    _HIDDEN_GEM_WARN_THRESHOLD,
    HiddenGemEntry,
    HiddenGemReport,
    aggregate_hidden_gem_hit_rate,
    hidden_gem_hit_rate_for_commander,
    plausibility,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_threshold_constant_is_002() -> None:
    """FR4 pins the warning threshold at 0.02."""
    assert pytest.approx(0.02) == _HIDDEN_GEM_WARN_THRESHOLD


# ---------------------------------------------------------------------------
# plausibility
# ---------------------------------------------------------------------------


def test_plausibility_true_when_at_least_two_rules_fire() -> None:
    """FR2 leg 1: ``N_rules_firing(cmdr, cand) >= 2``."""
    rows = [
        ("card_a", "rule_x", 0.3),
        ("card_a", "rule_y", 0.2),
        # card_b below median, only 1 rule — still included to flesh
        # out a median worth comparing against.
        ("card_b", "rule_x", 0.1),
    ]
    assert plausibility("C", "card_a", rows) is True


def test_plausibility_false_when_single_rule_below_median() -> None:
    """One rule firing AND total ≤ median → False."""
    rows = [
        ("card_a", "rule_x", 0.1),
        ("card_b", "rule_x", 1.0),
        ("card_b", "rule_y", 1.0),
        ("card_c", "rule_x", 0.5),
        ("card_c", "rule_z", 0.5),
    ]
    # card_a total 0.1, medians of {0.1, 2.0, 1.0} = 1.0 → 0.1 < 1.0.
    assert plausibility("C", "card_a", rows) is False


def test_plausibility_true_via_median_or_leg() -> None:
    """FR2 leg 2: single rule but contribution > per-commander median."""
    rows = [
        ("card_a", "rule_x", 5.0),  # 1 rule, very high
        ("card_b", "rule_x", 0.1),
        ("card_b", "rule_y", 0.1),
        ("card_c", "rule_x", 0.1),
        ("card_c", "rule_z", 0.1),
    ]
    # card_a total 5.0, totals are {5.0, 0.2, 0.2}, median 0.2 → 5.0 > 0.2.
    assert plausibility("C", "card_a", rows) is True


def test_plausibility_false_when_equal_to_median() -> None:
    """Strict inequality — ``total == median`` → False (unless N_rules leg)."""
    rows = [
        ("card_a", "rule_x", 1.0),  # 1 rule
        ("card_b", "rule_x", 1.0),  # 1 rule
        ("card_c", "rule_x", 1.0),  # 1 rule
    ]
    # All equal, median = 1.0, card_a total = 1.0 → NOT strictly greater.
    assert plausibility("C", "card_a", rows) is False


def test_plausibility_ignores_negative_contributions() -> None:
    """Negatives shouldn't count toward ``N_rules_firing`` or total.

    If a rule subtracts, the card is less plausible, not more.
    """
    rows = [
        # card_a: 2 negative + 1 positive → only 1 firing, total = 0.5
        ("card_a", "rule_neg_1", -0.5),
        ("card_a", "rule_neg_2", -0.3),
        ("card_a", "rule_pos", 0.5),
        ("card_b", "rule_pos", 1.0),
        ("card_b", "rule_pos2", 1.0),
    ]
    # card_a only has 1 rule > 0; totals (pos only) = {0.5, 2.0},
    # median = 1.25, card_a = 0.5 < 1.25 → False.
    assert plausibility("C", "card_a", rows) is False


def test_plausibility_all_zero_contributions_always_false() -> None:
    """Every contribution = 0 → both legs fail → every candidate False."""
    rows = [
        ("card_a", "rule_x", 0.0),
        ("card_a", "rule_y", 0.0),
        ("card_b", "rule_x", 0.0),
    ]
    assert plausibility("C", "card_a", rows) is False
    assert plausibility("C", "card_b", rows) is False


def test_plausibility_zero_median_falls_back_to_n_rules() -> None:
    """When every non-zero candidate total is 0 → median is vacuous.

    If the median leg would make every non-zero candidate "greater than
    zero" and therefore plausible, that's wrong. Fall back to the
    N_rules leg.
    """
    # All contributions zero — nothing positive.
    rows = [
        ("card_a", "rule_x", 0.0),
        ("card_b", "rule_x", 0.0),
    ]
    assert plausibility("C", "card_a", rows) is False


def test_plausibility_single_candidate_no_zero_division() -> None:
    """Commander with one candidate → median = that one value; clean."""
    rows = [("card_a", "rule_x", 0.7)]
    # Only one candidate, only one rule → N_rules = 1, median = 0.7,
    # total = 0.7, NOT strictly greater → False.
    assert plausibility("C", "card_a", rows) is False


def test_plausibility_candidate_missing_from_rows_is_false() -> None:
    """Asking about a candidate with no rule firings → False."""
    rows = [
        ("card_a", "rule_x", 0.5),
        ("card_a", "rule_y", 0.5),
    ]
    assert plausibility("C", "card_missing", rows) is False


def test_plausibility_duplicate_rule_ids_sum_into_total() -> None:
    """Same rule_id firing twice — both count toward N_rules + total.

    Unit 1 does no deduplication: tensor rows are authoritative.
    If Unit 2's query ever produces duplicates, we treat each row as
    real.
    """
    rows = [
        ("card_a", "rule_x", 0.4),
        ("card_a", "rule_x", 0.4),  # duplicate rule_id
        ("card_b", "rule_x", 0.1),
    ]
    # card_a has 2 firings → True via N_rules leg.
    assert plausibility("C", "card_a", rows) is True


# ---------------------------------------------------------------------------
# hidden_gem_hit_rate_for_commander
# ---------------------------------------------------------------------------


def test_hit_rate_happy_path_two_hidden_plausible() -> None:
    """5 of our top-30 hidden, EDHREC covers 3; remaining 2 each fire 3
    rules → rate = 2/30."""
    our_top_30 = ["c1", "c2", "c3", "c4", "c5", *[f"filler_{i}" for i in range(25)]]
    edhrec = {"c1", "c2", "c3"}
    # c4, c5 each have 3 rules firing → plausible.
    rows: list[tuple[str, str, float]] = [
        ("c4", "rule_a", 0.3),
        ("c4", "rule_b", 0.2),
        ("c4", "rule_c", 0.1),
        ("c5", "rule_a", 0.2),
        ("c5", "rule_b", 0.2),
        ("c5", "rule_c", 0.2),
        # Some non-hidden candidates to stabilize the median.
        ("c1", "rule_a", 0.1),
    ]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    assert entry is not None
    assert entry.commander == "C"
    assert entry.rate == pytest.approx(2 / 30)
    assert set(entry.hidden_cards) == {"c4", "c5"}


def test_hit_rate_happy_path_median_or_leg_one_hidden() -> None:
    """1 hidden card, N_rules=1 but total > median → rate = 1/30."""
    our_top_30 = ["c1", *[f"filler_{i}" for i in range(29)]]
    edhrec: set[str] = set()  # c1 is hidden
    rows: list[tuple[str, str, float]] = [
        # c1 single rule, very high total.
        ("c1", "rule_a", 10.0),
        # baseline candidates so median = small.
        ("other_1", "rule_a", 0.1),
        ("other_2", "rule_a", 0.1),
    ]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    assert entry is not None
    assert entry.rate == pytest.approx(1 / 30)
    assert entry.hidden_cards == ("c1",)


def test_hit_rate_empty_hidden_set_zero_rate() -> None:
    """EDHREC covers all our top-30 → rate 0, empty hidden_cards list."""
    our_top_30 = [f"c_{i}" for i in range(30)]
    edhrec = set(our_top_30)
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, [])
    assert entry is not None
    assert entry.rate == 0.0
    assert entry.hidden_cards == ()


def test_hit_rate_edhrec_none_returns_none() -> None:
    """``edhrec_top_30=None`` → skip (function returns None)."""
    our_top_30 = [f"c_{i}" for i in range(30)]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, None, [])
    assert entry is None


def test_hit_rate_single_candidate_no_crash() -> None:
    """One candidate across entire commander → median logic holds."""
    our_top_30 = ["only_card"]
    edhrec: set[str] = set()
    rows = [("only_card", "rule_a", 0.5)]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    assert entry is not None
    # N_rules = 1, median = 0.5, total = 0.5 → not plausible.
    assert entry.rate == 0.0
    assert entry.hidden_cards == ()


def test_hit_rate_all_zero_contributions_no_plausible() -> None:
    """All-zero tensor → plausibility False everywhere → rate 0."""
    our_top_30 = [f"c_{i}" for i in range(30)]
    edhrec: set[str] = set()
    rows = [(f"c_{i}", "rule_a", 0.0) for i in range(30)]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    assert entry is not None
    assert entry.rate == 0.0
    assert entry.hidden_cards == ()


def test_hit_rate_duplicates_in_our_top_30_raises() -> None:
    """Duplicate in ``our_top_30`` → ``ValueError``."""
    our_top_30 = ["c1", "c2", "c1"]
    with pytest.raises(ValueError, match="our_top_30 must be unique"):
        hidden_gem_hit_rate_for_commander("C", our_top_30, set(), [])


def test_hit_rate_determinism_same_inputs_same_output() -> None:
    """R7: repeated calls on identical inputs → identical dataclass."""
    our_top_30 = ["c1", "c2", "c3", *[f"filler_{i}" for i in range(27)]]
    edhrec = {"c1"}
    rows = [
        ("c2", "rule_a", 0.3),
        ("c2", "rule_b", 0.3),
        ("c3", "rule_a", 0.1),
    ]
    a = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    b = hidden_gem_hit_rate_for_commander("C", list(our_top_30), set(edhrec), list(rows))
    assert a == b
    assert a is not None and b is not None
    assert a.rate == b.rate
    assert a.hidden_cards == b.hidden_cards


def test_hit_rate_ordering_of_our_top_30_does_not_matter() -> None:
    """Reordering ``our_top_30`` → same entry (set semantics on input)."""
    cards = ["c1", "c2", "c3", *[f"filler_{i}" for i in range(27)]]
    edhrec = {"c1"}
    rows = [
        ("c2", "rule_a", 0.3),
        ("c2", "rule_b", 0.3),
    ]
    a = hidden_gem_hit_rate_for_commander("C", cards, edhrec, rows)
    reordered = list(reversed(cards))
    b = hidden_gem_hit_rate_for_commander("C", reordered, edhrec, rows)
    assert a is not None and b is not None
    assert a.rate == b.rate
    assert set(a.hidden_cards) == set(b.hidden_cards)


def test_hit_rate_hidden_cards_tuple_sorted() -> None:
    """``hidden_cards`` output is sorted for deterministic equality.

    Without sorting, set-subtraction order would depend on hash
    randomization and break the deep-equality determinism test.
    """
    our_top_30 = ["zeta", "alpha", "mu", *[f"filler_{i}" for i in range(27)]]
    edhrec: set[str] = set()
    rows: list[tuple[str, str, float]] = [
        ("zeta", "rule_a", 1.0),
        ("zeta", "rule_b", 1.0),
        ("alpha", "rule_a", 1.0),
        ("alpha", "rule_b", 1.0),
        ("mu", "rule_a", 1.0),
        ("mu", "rule_b", 1.0),
    ]
    entry = hidden_gem_hit_rate_for_commander("C", our_top_30, edhrec, rows)
    assert entry is not None
    # alphabetical order; not insertion order.
    assert entry.hidden_cards == ("alpha", "mu", "zeta")


# ---------------------------------------------------------------------------
# aggregate_hidden_gem_hit_rate
# ---------------------------------------------------------------------------


def test_aggregate_mixes_none_and_real_entries() -> None:
    """None entries → skipped_commanders; mean over non-None."""
    e1 = HiddenGemEntry(commander="C1", rate=0.1, hidden_cards=("x",))
    e2 = HiddenGemEntry(commander="C2", rate=0.2, hidden_cards=("y",))
    report = aggregate_hidden_gem_hit_rate([("C1", e1), ("C2", e2), ("C3_skipped", None)])
    assert isinstance(report, HiddenGemReport)
    assert report.aggregate == pytest.approx(0.15)
    assert set(report.per_commander.keys()) == {"C1", "C2"}
    assert report.skipped_commanders == ("C3_skipped",)


def test_aggregate_all_none_gives_none_aggregate() -> None:
    """Every entry None → aggregate None; all commanders skipped."""
    report = aggregate_hidden_gem_hit_rate([("C1", None), ("C2", None), ("C3", None)])
    assert report.aggregate is None
    assert report.per_commander == {}
    assert set(report.skipped_commanders) == {"C1", "C2", "C3"}


def test_aggregate_empty_input_gives_none_aggregate() -> None:
    """Empty iterable → aggregate None; no crash."""
    report = aggregate_hidden_gem_hit_rate([])
    assert report.aggregate is None
    assert report.per_commander == {}
    assert report.skipped_commanders == ()


def test_aggregate_single_real_entry() -> None:
    """Single real entry → aggregate = that entry's rate."""
    e = HiddenGemEntry(commander="C1", rate=0.333, hidden_cards=())
    report = aggregate_hidden_gem_hit_rate([("C1", e)])
    assert report.aggregate == pytest.approx(0.333)
    assert report.per_commander == {"C1": e}
    assert report.skipped_commanders == ()


def test_aggregate_preserves_entries_in_per_commander_dict() -> None:
    """Non-None entries are stored verbatim, keyed by commander name."""
    e1 = HiddenGemEntry(commander="C1", rate=0.1, hidden_cards=("a", "b"))
    e2 = HiddenGemEntry(commander="C2", rate=0.5, hidden_cards=("c",))
    report = aggregate_hidden_gem_hit_rate([("C1", e1), ("C2", e2)])
    assert report.per_commander["C1"] is e1
    assert report.per_commander["C2"] is e2


# ---------------------------------------------------------------------------
# Dataclass semantics
# ---------------------------------------------------------------------------


def test_hidden_gem_entry_is_frozen() -> None:
    """``HiddenGemEntry`` is frozen for immutability (FR per project rules)."""
    e = HiddenGemEntry(commander="C", rate=0.1, hidden_cards=("x",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.rate = 0.2  # type: ignore[misc]


def test_hidden_gem_report_is_frozen() -> None:
    """``HiddenGemReport`` is frozen."""
    r = HiddenGemReport(aggregate=0.1, per_commander={}, skipped_commanders=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.aggregate = 0.2  # type: ignore[misc]


def test_hidden_gem_entry_deep_equality() -> None:
    """Equivalent inputs → equal dataclass instances (supports R7)."""
    a = HiddenGemEntry(commander="C", rate=0.5, hidden_cards=("x", "y"))
    b = HiddenGemEntry(commander="C", rate=0.5, hidden_cards=("x", "y"))
    assert a == b
