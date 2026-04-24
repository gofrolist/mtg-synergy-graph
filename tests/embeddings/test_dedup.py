"""Unit 6 (plan 2026-04-23-003) — embedding dedup pure-logic tests.

Covers:

* Activation-set grouping, ``min_activation`` filter, ``threshold``
  override.
* Numeric correctness: identical activation sets → cosine ≈ 1;
  orthogonal sets → cosine ≈ 0; partial overlap → intermediate.
* Edge cases: missing vectors, empty input, single rule.
* Ranking: output is sorted by cosine desc with lexicographic
  tiebreak.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from mtg_synergy_graph.embeddings.dedup import DedupPair, compute_embedding_dedup


def _unit_vector(*components: float, dim: int = 128) -> np.ndarray:
    """Build a deterministic L2-normalized float32 vector for tests.

    The non-zero components are placed at the first ``len(components)``
    positions; remaining dims are zero. ``_unit_vector(1.0)`` gives
    ``e_0``; ``_unit_vector(0.0, 1.0)`` gives ``e_1``; etc. Callers
    can use this to construct trivially-orthogonal or trivially-
    parallel vectors without random noise.
    """
    vec = np.zeros(dim, dtype=np.float32)
    for i, c in enumerate(components):
        vec[i] = c
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return (vec / norm).astype(np.float32)


def _tensor_rows(activations: dict[str, Sequence[str]]) -> list[tuple[str, str, str, float]]:
    """Shape ``{rule_id: [candidate, ...]}`` into tensor-row tuples."""
    rows: list[tuple[str, str, str, float]] = []
    for rule_id, cands in activations.items():
        for cand in cands:
            # Commander name is unused by the dedup algorithm; any
            # stable string is fine. Contribution must be positive
            # since the handler filters on ``> 0`` upstream — use 1.0.
            rows.append(("CmdrA", cand, rule_id, 1.0))
    return rows


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_identical_activation_sets_produce_cosine_one() -> None:
    """Two rules with the same activation cards → cosine ≈ 1 → one pair."""
    cards = [f"c{i}" for i in range(25)]
    vectors = {c: _unit_vector(1.0, 0.0) for c in cards}
    rows = _tensor_rows({"rule_a": cards, "rule_b": cards})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.95, min_activation=5)

    assert len(pairs) == 1
    assert pairs[0].rule_a == "rule_a"
    assert pairs[0].rule_b == "rule_b"
    assert pairs[0].cosine == pytest.approx(1.0, rel=1e-6)
    assert pairs[0].activation_a_size == 25
    assert pairs[0].activation_b_size == 25


def test_orthogonal_activation_sets_do_not_flag() -> None:
    """Two rules with disjoint cards mapped to orthogonal unit vectors →
    cosine ≈ 0 → not flagged."""
    cards_a = [f"a{i}" for i in range(25)]
    cards_b = [f"b{i}" for i in range(25)]
    vectors = {**{c: _unit_vector(1.0, 0.0) for c in cards_a}, **{c: _unit_vector(0.0, 1.0) for c in cards_b}}
    rows = _tensor_rows({"rule_a": cards_a, "rule_b": cards_b})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=5)
    assert pairs == []


def test_overlapping_sets_produce_cosine_below_one() -> None:
    """80% overlap → high cosine but strictly below 1.0."""
    # Rule A's activation: cards 0..24 (all on direction e_0).
    # Rule B's activation: cards 5..24 (same direction) + 25..29 (orthogonal).
    # Both mean vectors lean heavily on e_0 since 20/25 overlap, so
    # cosine should be high but < 1.
    e0_cards = [f"e0_{i}" for i in range(25)]
    orth_cards = [f"orth_{i}" for i in range(5)]
    vectors = {**{c: _unit_vector(1.0, 0.0) for c in e0_cards}, **{c: _unit_vector(0.0, 1.0) for c in orth_cards}}
    rows = _tensor_rows({"rule_a": e0_cards, "rule_b": e0_cards[5:] + orth_cards})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=5)
    assert len(pairs) == 1
    assert 0.5 < pairs[0].cosine < 1.0


# ---------------------------------------------------------------------------
# min_activation threshold
# ---------------------------------------------------------------------------


def test_min_activation_drops_tiny_activation_sets() -> None:
    """A rule with 2 cards is dropped when ``min_activation=3``."""
    vectors = {c: _unit_vector(1.0, 0.0) for c in ("a", "b", "c", "d")}
    rows = _tensor_rows({"big_rule": ["a", "b", "c", "d"], "tiny_rule": ["a", "b"]})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=3)
    # ``tiny_rule`` is dropped → only ``big_rule`` remains → no pair
    # possible (need ≥2 rules for a pair).
    assert pairs == []


def test_min_activation_high_prunes_all() -> None:
    """A very high ``min_activation`` prunes every rule → empty output."""
    vectors = {c: _unit_vector(1.0, 0.0) for c in ("a", "b", "c")}
    rows = _tensor_rows({"r1": ["a", "b", "c"], "r2": ["a", "b", "c"]})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=100)
    assert pairs == []


# ---------------------------------------------------------------------------
# threshold override
# ---------------------------------------------------------------------------


def test_threshold_override_lower_flags_pair() -> None:
    """At threshold=0.5 a borderline pair flags; at 0.95 it does not."""
    # Both rules have 5 cards on direction e_0; rule_b adds 5 on e_1.
    # Mean cosine comes out around 0.707 → flagged at 0.5, not at 0.95.
    e0_cards = [f"e0_{i}" for i in range(5)]
    e1_cards = [f"e1_{i}" for i in range(5)]
    vectors = {**{c: _unit_vector(1.0, 0.0) for c in e0_cards}, **{c: _unit_vector(0.0, 1.0) for c in e1_cards}}
    rows = _tensor_rows({"rule_a": e0_cards, "rule_b": e0_cards + e1_cards})

    pairs_loose = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=3)
    pairs_strict = compute_embedding_dedup(rows, vectors, threshold=0.95, min_activation=3)

    assert len(pairs_loose) == 1
    assert pairs_strict == []


# ---------------------------------------------------------------------------
# Missing vectors / degenerate inputs
# ---------------------------------------------------------------------------


def test_rule_with_no_vector_hits_is_silently_excluded() -> None:
    """Rule whose cards aren't in ``vectors`` at all → excluded; no KeyError."""
    vectors = {c: _unit_vector(1.0, 0.0) for c in ("a", "b", "c")}
    rows = _tensor_rows(
        {
            "known_rule": ["a", "b", "c"],
            "ghost_rule": ["missing_1", "missing_2", "missing_3"],
        }
    )

    # Should not raise KeyError; only known_rule makes it through but
    # needs a partner so no pairs. Use min_activation=3 so both rules
    # clear the activation floor.
    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=3)
    assert pairs == []


def test_empty_tensor_rows_returns_empty_list() -> None:
    """No rows → no pairs, no errors."""
    vectors = {"a": _unit_vector(1.0)}
    pairs = compute_embedding_dedup([], vectors, threshold=0.5, min_activation=1)
    assert pairs == []


def test_single_rule_returns_empty_list() -> None:
    """One rule → no possible pair → empty output (no single-rule self-pair)."""
    cards = ["a", "b", "c"]
    vectors = {c: _unit_vector(1.0) for c in cards}
    rows = _tensor_rows({"only_rule": cards})

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=1)
    assert pairs == []


# ---------------------------------------------------------------------------
# Ranking / sort order
# ---------------------------------------------------------------------------


def test_output_sorted_by_cosine_descending_with_tiebreak() -> None:
    """5 rules with known pairwise cosines render in descending-cosine order."""
    # Build five rules with 5 cards each, all unit vectors along e_0 so
    # every pair has cosine 1.0 — deterministic tiebreak ordering
    # exercises the ``(rule_a, rule_b)`` lexicographic secondary key.
    vectors: dict[str, np.ndarray] = {}
    activations: dict[str, list[str]] = {}
    for rule_letter in "abcde":
        cards = [f"{rule_letter}_{i}" for i in range(5)]
        for c in cards:
            vectors[c] = _unit_vector(1.0)
        activations[f"rule_{rule_letter}"] = cards

    pairs = compute_embedding_dedup(_tensor_rows(activations), vectors, threshold=0.5, min_activation=3)

    # C(5,2) = 10 pairs, all cosine 1.0.
    assert len(pairs) == 10
    # All cosines equal → sort should be lexicographic on rule names.
    rendered_pairs = [(p.rule_a, p.rule_b) for p in pairs]
    assert rendered_pairs == sorted(rendered_pairs)


def test_output_sort_with_distinct_cosines() -> None:
    """When cosines differ, larger comes first."""
    # rule_a + rule_b → cosine 1 (both on e_0).
    # rule_a + rule_c → cosine ≈ 0.707 (e_0 vs 45° rotated).
    e0_cards = [f"e0_{i}" for i in range(5)]
    rotated_cards = [f"rot_{i}" for i in range(5)]
    vectors = {**{c: _unit_vector(1.0, 0.0) for c in e0_cards}, **{c: _unit_vector(1.0, 1.0) for c in rotated_cards}}
    rows = _tensor_rows(
        {
            "rule_a": e0_cards,
            "rule_b": e0_cards,
            "rule_c": rotated_cards,
        }
    )

    pairs = compute_embedding_dedup(rows, vectors, threshold=0.5, min_activation=3)

    assert len(pairs) == 3
    # Highest cosine (rule_a / rule_b) should lead.
    assert pairs[0].rule_a == "rule_a"
    assert pairs[0].rule_b == "rule_b"
    assert pairs[0].cosine == pytest.approx(1.0, rel=1e-6)
    # Remaining two pairs share cosine ≈ 0.707 → lexicographic
    # tiebreak: (rule_a, rule_c) before (rule_b, rule_c).
    assert pairs[1].rule_a == "rule_a"
    assert pairs[1].rule_b == "rule_c"
    assert pairs[2].rule_a == "rule_b"
    assert pairs[2].rule_b == "rule_c"


# ---------------------------------------------------------------------------
# DedupPair shape
# ---------------------------------------------------------------------------


def test_dedup_pair_is_frozen() -> None:
    """``DedupPair`` mutation raises — verifies ``frozen=True`` contract."""
    pair = DedupPair(rule_a="a", rule_b="b", cosine=0.99, activation_a_size=10, activation_b_size=12)
    with pytest.raises((AttributeError, TypeError)):
        pair.cosine = 0.0  # type: ignore[misc]  — intentionally illegal
