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
            ("Card A", 0.5, 3.0, 8),
            ("Card B", 0.3, 1.0, 6),
            ("Card C", 0.1, 5.0, 0),
        ]
    }
    score, n = evaluate_weights({"LLM": 0, "CAUSAL": 1.0}, precomputed)
    assert n == 1
    assert isinstance(score, float)
