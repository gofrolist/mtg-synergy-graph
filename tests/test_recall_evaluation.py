"""Tests for Recall@K evaluation metric."""
import pytest


def test_recall_at_k_perfect():
    from optimize_weights import compute_recall_at_k
    our_top = ["A", "B", "C", "D", "E"]
    edhrec_deck = {"A", "B", "C", "D", "E"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 1.0


def test_recall_at_k_zero():
    from optimize_weights import compute_recall_at_k
    our_top = ["X", "Y", "Z"]
    edhrec_deck = {"A", "B", "C"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=3) == 0.0


def test_recall_at_k_partial():
    from optimize_weights import compute_recall_at_k
    our_top = ["A", "B", "X", "Y", "Z"]
    edhrec_deck = {"A", "B", "C", "D"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 0.5


def test_recall_at_k_respects_limit():
    from optimize_weights import compute_recall_at_k
    our_top = ["X", "Y", "A", "B", "C"]
    edhrec_deck = {"A", "B", "C"}
    assert compute_recall_at_k(our_top, edhrec_deck, k=2) == 0.0
    assert compute_recall_at_k(our_top, edhrec_deck, k=5) == 1.0


def test_evaluate_weights_no_llm():
    from optimize_weights import evaluate_weights
    precomputed = {
        "test-commander": [
            ("Card A", {"edhrec_syn": 0.5, "causal": 3.0, "llm": 8}),
            ("Card B", {"edhrec_syn": 0.3, "causal": 1.0, "llm": 6}),
            ("Card C", {"edhrec_syn": 0.1, "causal": 5.0, "llm": 0}),
        ]
    }
    score, n = evaluate_weights({"LLM": 0, "CAUSAL": 1.0}, precomputed)
    assert n == 1
    assert isinstance(score, float)


def test_rank_cards_uses_all_features():
    from optimize_weights import _rank_cards
    scored = [
        ("Card A", {"llm": 10, "causal": 0, "cmdr_overlap": 5, "mechanics": 3}),
        ("Card B", {"llm": 0, "causal": 5, "cmdr_overlap": 0, "mechanics": 0}),
    ]
    # With only CMDR_TAG_OVERLAP weight, Card A should rank higher
    ranked = _rank_cards(scored, {"CMDR_TAG_OVERLAP": 10})
    assert ranked[0][0] == "Card A"
    # With only CAUSAL weight, Card B should rank higher
    ranked2 = _rank_cards(scored, {"CAUSAL": 10})
    assert ranked2[0][0] == "Card B"
