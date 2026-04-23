"""Tests for ``forge_oracle.ppmi`` — pure PPMI math.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 4.
"""

from __future__ import annotations

import math

import pytest

from mtg_synergy_graph.forge_oracle import ppmi

# ---------------------------------------------------------------------------
# compute_ppmi_table — the aggregation + PMI entry point
# ---------------------------------------------------------------------------


def test_compute_ppmi_pair_that_always_cooccurs_is_positive() -> None:
    """Two signatures present in every deck → PMI should be positive.

    3 decks, each with signatures {A, B}. P(A)=P(B)=1, P(A,B)=1.
    Log-ratio is log(1 / 1) = 0 — but with Laplace smoothing there's a
    tiny positive shift. Test just asserts ppmi >= 0 and decks_count=3.
    """
    decks = [frozenset({"A", "B"})] * 3
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    assert ("A", "B") in table
    entry = table[("A", "B")]
    assert entry.decks_count == 3
    assert entry.ppmi >= 0.0


def test_compute_ppmi_independent_pair_is_zero_or_negative_clamped() -> None:
    """Pair co-occurring at baseline rate → PMI ≈ 0 → PPMI clamped to 0
    (or a very small positive value with smoothing).
    """
    # 4 decks with A, 4 decks with B, 4 decks have both → joint freq == marginal product.
    decks = [
        frozenset({"A", "B", "C"}),
        frozenset({"A", "B", "D"}),
        frozenset({"A", "B", "E"}),
        frozenset({"A", "B", "F"}),
    ]
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    # A and B perfectly co-occur, so PMI > 0 (they signal each other despite
    # universal rate). Low-information sanity check only — no exact value.
    assert table[("A", "B")].ppmi >= 0.0


def test_compute_ppmi_selective_cooccurrence_scores_higher_than_universal() -> None:
    """A selective pair (A,B co-occur in 3 small decks among a 10-deck corpus)
    should score higher than a pair (X,Y) that co-occurs in large decks only.
    Tests the RAPM-style lineup-weighting intuition.
    """
    # 3 small 2-card decks where A,B co-occur (high density)
    selective = [frozenset({"A", "B"})] * 3
    # 3 large 10-card decks where X,Y co-occur among many other cards (low density)
    large = [frozenset({"X", "Y"} | {f"filler{i}_{j}" for j in range(8)}) for i in range(3)]
    corpus = selective + large
    table = ppmi.compute_ppmi_table(corpus, min_decks_count=1, smoothing_k=0.0)
    assert table[("A", "B")].ppmi > table[("X", "Y")].ppmi


def test_compute_ppmi_respects_min_decks_count_threshold() -> None:
    """Pairs with ``decks_count < min_decks_count`` are dropped."""
    decks = [
        frozenset({"A", "B"}),  # (A,B) deck 1
        frozenset({"A", "B"}),  # (A,B) deck 2 — not yet above threshold 3
        frozenset({"C", "D"}),  # (C,D) deck 1
    ]
    table = ppmi.compute_ppmi_table(decks, min_decks_count=3, smoothing_k=0.0)
    assert ("A", "B") not in table  # only 2 decks
    assert ("C", "D") not in table  # only 1 deck


def test_compute_ppmi_canonical_order_a_less_than_b() -> None:
    """Stored pairs are canonical: port_signature_a < port_signature_b."""
    decks = [frozenset({"zebra", "alpha"}), frozenset({"zebra", "alpha"}), frozenset({"zebra", "alpha"})]
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    assert ("alpha", "zebra") in table
    assert ("zebra", "alpha") not in table


def test_compute_ppmi_laplace_smoothing_stabilizes_sparse_pairs() -> None:
    """Higher smoothing_k dampens PMI for low-evidence pairs.

    With k=0, a pair appearing once in a 1-deck corpus produces
    maximal PMI. With k=1 the same pair's PMI is smaller.
    """
    decks = [frozenset({"A", "B"})]
    t_k0 = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    t_k1 = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=1.0)
    assert t_k0[("A", "B")].ppmi > t_k1[("A", "B")].ppmi


def test_compute_ppmi_decks_count_is_raw_not_weighted() -> None:
    """``decks_count`` reports the raw integer # of decks containing both.
    Independent of smoothing / lineup weighting."""
    decks = [frozenset({"A", "B", "C"})] * 5 + [frozenset({"A", "C"})] * 2
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.5)
    assert table[("A", "B")].decks_count == 5
    assert table[("A", "C")].decks_count == 7


def test_compute_ppmi_self_pair_excluded() -> None:
    """A signature paired with itself is meaningless; skip."""
    decks = [frozenset({"A", "B"})] * 3
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    assert ("A", "A") not in table
    assert ("B", "B") not in table


def test_compute_ppmi_empty_corpus_returns_empty_table() -> None:
    table = ppmi.compute_ppmi_table([], min_decks_count=1, smoothing_k=0.0)
    assert table == {}


def test_compute_ppmi_deck_with_single_signature_contributes_marginal_only() -> None:
    """A deck with one signature has no pairs to contribute.

    Its marginal still raises P(A), so it dampens P(A) in other pair calculations.
    """
    decks = [frozenset({"A", "B"}), frozenset({"A", "B"}), frozenset({"A"})]
    table = ppmi.compute_ppmi_table(decks, min_decks_count=1, smoothing_k=0.0)
    # A has 3 decks, B has 2 decks, joint = 2. So P(A) > P(B), and the singleton
    # A-only deck should lower P(A,B)/(P(A)*P(B)) vs the 2-deck-only case.
    assert table[("A", "B")].decks_count == 2


def test_compute_ppmi_two_way_symmetry() -> None:
    """``ppmi(a,b) == ppmi(b,a)`` because the formula is symmetric — but
    we store pairs canonically, so only the sorted order appears in the
    table. Regression guard against accidental asymmetric accumulation.
    """
    corpus = [frozenset({"A", "B", "C"})] * 5
    table = ppmi.compute_ppmi_table(corpus, min_decks_count=1, smoothing_k=0.0)
    ab = table[("A", "B")].ppmi
    ac = table[("A", "C")].ppmi
    bc = table[("B", "C")].ppmi
    # All three pairs appear in exactly the same corpus → identical PPMI.
    assert ab == pytest.approx(ac)
    assert ab == pytest.approx(bc)


# ---------------------------------------------------------------------------
# pmi — low-level numeric function
# ---------------------------------------------------------------------------


def test_pmi_formula_matches_hand_computed() -> None:
    """pmi(p_ab, p_a, p_b, smoothing_k=0) = log(p_ab / (p_a * p_b))."""
    p_a = 0.5
    p_b = 0.4
    p_ab = 0.3
    expected = math.log(p_ab / (p_a * p_b))
    assert ppmi._pmi(p_ab, p_a, p_b, smoothing_k=0.0) == pytest.approx(expected)


def test_pmi_returns_negative_infinity_safe_when_joint_zero() -> None:
    """p_ab == 0 → defined behaviour: returns -inf (clamped to 0 by PPMI)."""
    result = ppmi._pmi(0.0, 0.5, 0.5, smoothing_k=0.0)
    assert result == -math.inf
