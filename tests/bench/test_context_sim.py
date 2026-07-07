"""Pure-function tests for the deck-context kill-test instrument (plan 2026-07-06-001)."""

from mtg_synergy_graph.bench.context_sim import (
    aggregate_context_scores,
    select_context,
)
from mtg_synergy_graph.complement_rules import PortComplement


def _comp(cand, rule="trigger_effect", direction="synergy", cmdr_ev="Sacrificed", cand_ev="Token"):
    return PortComplement(
        rule_id=rule,
        direction=direction,
        candidate=cand,
        cmdr_event=cmdr_ev,
        cand_event=cand_ev,
        filter_group="",
    )


def test_select_context_skips_zero_rule_candidates_and_caps_at_k():
    pool = ("A", "B", "C", "D")
    n_rules = {"A": 2, "B": 0, "C": 1, "D": 3}
    assert select_context(pool, n_rules, k=2) == ("A", "C")


def test_select_context_short_pool_returns_all_eligible():
    assert select_context(("A",), {"A": 1}, k=5) == ("A",)


def test_aggregate_dedups_on_idf_key_and_sums_weights():
    comps = [_comp("X"), _comp("X"), _comp("X", cand_ev="Treasure")]
    idf = {
        ("trigger_effect", "Sacrificed", "Token", ""): 0.5,
        ("trigger_effect", "Sacrificed", "Treasure", ""): 0.25,
    }
    out = aggregate_context_scores(comps, idf, ctx_card="CTX")
    assert out == {"X": 0.75}  # duplicate key counted once


def test_aggregate_excludes_anti_synergy_and_self():
    comps = [_comp("X", direction="anti_synergy"), _comp("CTX")]
    out = aggregate_context_scores(comps, {}, ctx_card="CTX")
    assert out == {}
