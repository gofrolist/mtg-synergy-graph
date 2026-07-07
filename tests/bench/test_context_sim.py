"""Pure-function tests for the deck-context kill-test instrument (plan 2026-07-06-001)."""

import json
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.context_sim import (
    ContextCell,
    ContextSim,
    ContextSimError,
    _parse_cells,
    _plausible_set,
    aggregate_context_scores,
    assemble_cell,
    assemble_whitelist,
    select_context,
)
from mtg_synergy_graph.complement_rules import PortComplement

_DB = Path("data/synergy.db")


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


def test_plausible_set_two_rules_qualifies_regardless_of_total():
    # "Low" has only 1.0 total (below the positive median of the other
    # totals) but 2 distinct rules — the >=2-rules leg alone qualifies it.
    n_rules_map = {"Low": 2, "Mid": 1, "High": 1}
    base_totals = {"Low": 1.0, "Mid": 5.0, "High": 10.0}
    assert _plausible_set(n_rules_map, base_totals) == frozenset({"Low", "High"})


def test_plausible_set_single_rule_above_median_qualifies():
    # positive median of {1.0, 5.0, 10.0} is 5.0; only High (10.0 > 5.0)
    # clears the median-OR leg via a single rule.
    n_rules_map = {"Low": 1, "Mid": 1, "High": 1}
    base_totals = {"Low": 1.0, "Mid": 5.0, "High": 10.0}
    result = _plausible_set(n_rules_map, base_totals)
    assert "High" in result
    assert "Mid" not in result  # 5.0 is not > median 5.0 (strict inequality)
    assert "Low" not in result  # 1.0 <= median


def test_plausible_set_degenerate_all_nonpositive_totals_only_two_rules_leg_qualifies():
    # All totals <= 0 -> the positive-total median is 0.0 (degenerate),
    # so the median-OR leg contributes nothing; only the >=2-rules leg
    # can admit a candidate. This is the guard the Important finding
    # said context_sim.py was dropping (see hidden_gems._cohort_median /
    # _plausibility_from_maps: median <= 0 forces the OR-leg to False
    # rather than degenerating to `total > 0`).
    n_rules_map = {"TwoRules": 2, "OneRule": 1, "ZeroRules": 0}
    base_totals = {"TwoRules": 0.0, "OneRule": 0.0, "ZeroRules": -3.0}
    assert _plausible_set(n_rules_map, base_totals) == frozenset({"TwoRules"})


def test_parse_cells_malformed_raises_context_sim_error():
    with pytest.raises(ContextSimError, match="malformed cell"):
        _parse_cells("10,0.1;not-a-cell")


def test_parse_cells_empty_raises_context_sim_error():
    with pytest.raises(ContextSimError, match="produced no cells"):
        _parse_cells("   ;  ")


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


def test_assemble_whitelist_new_entrant_scores_and_illegal_excluded():
    sim = _sim()
    wl = {"NEW": 1.0, "OFFCOLOR": 1.0}
    top = assemble_whitelist(sim, wl, 0.5)
    # NEW (legal, not in base_totals) gets base 0 + bonus 0.5 -> ranks above C (1.0)? No:
    # A=3.0, B=2.0, C=1.0, NEW=0.5 -> ("A", "B", "C", "NEW")
    assert top == ("A", "B", "C", "NEW")
    assert "OFFCOLOR" not in top  # not in legal_pool, excluded regardless of whitelist membership


def test_assemble_whitelist_commander_never_enters():
    sim = _sim()
    wl = {"Cmdr": 99.0}
    assert "Cmdr" not in assemble_whitelist(sim, wl, 0.5)


def test_build_parser_has_subcommands():
    from mtg_synergy_graph.bench.context_sim import build_parser

    p = build_parser()
    args = p.parse_args(["bands", "--fixture", "tests/fixtures/golden_set_archetype_payoff.json"])
    assert args.command == "bands"


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_payoff_subtypes_slimefoot():
    import sqlite3

    from mtg_synergy_graph.bench.context_sim import payoff_subtypes

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    subs = payoff_subtypes(conn, "Slimefoot, the Stowaway")
    assert "Saproling" in subs


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_whitelist_scores_cover_subtype_bodies_and_producers():
    import sqlite3

    from mtg_synergy_graph.bench.context_sim import whitelist_scores

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    wl = whitelist_scores(conn, "Slimefoot, the Stowaway")
    assert wl  # non-empty for a cohort commander


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_bands_smoke_two_commanders(tmp_path):
    from mtg_synergy_graph.bench.context_sim import main

    rc = main(
        [
            "bands",
            "--fixture",
            "tests/fixtures/golden_set_archetype_payoff.json",
            "--limit-commanders",
            "2",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    report = json.loads((tmp_path / "bands.json").read_text())
    assert "ndcg_band" in report and report["n_commanders"] == 2
