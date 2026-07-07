"""Pure-function tests for the deck-context kill-test instrument (plan 2026-07-06-001)."""

from mtg_synergy_graph.bench.context_sim import (
    ContextCell,
    ContextSim,
    aggregate_context_scores,
    assemble_cell,
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


def _sim(**over):
    base = dict(
        commander="Cmdr",
        base_totals={"A": 3.0, "B": 2.0, "C": 1.0},
        base_top_30=("A", "B", "C"),
        pool_order=("A", "B", "C"),
        legal_pool=frozenset({"A", "B", "C", "NEW", "ILLEGAL_ELSEWHERE"}),
        context_max=("A", "B"),
        ctx_scores={"A": {"NEW": 4.0, "OFFCOLOR": 9.0}, "B": {"C": 4.0}},
        cmc_lookup={},
        rank_lookup={},
        graded_labels={},
        edhrec_top_30=None,
        zero_score_labels=frozenset(),
    )
    base.update(over)
    return ContextSim(**base)


def test_w0_cell_is_identity():
    sim = _sim()
    assert assemble_cell(sim, ContextCell(k_context=0, w_ctx=0.0)) == ("A", "B", "C")


def test_new_entrant_scores_and_illegal_excluded():
    sim = _sim()
    top = assemble_cell(sim, ContextCell(k_context=2, w_ctx=1.0))
    # NEW gets 1.0 * (4.0/2) = 2.0; C gets 1.0 + 4.0/2 = 3.0
    assert top == ("A", "C", "B", "NEW")
    assert "OFFCOLOR" not in top  # not in legal_pool


def test_commander_never_enters():
    sim = _sim(ctx_scores={"A": {"Cmdr": 99.0}, "B": {}})
    assert "Cmdr" not in assemble_cell(sim, ContextCell(2, 1.0))
