"""Tests for the branch-aware SVar chain walker (SPEC §5.4)."""

from __future__ import annotations

from mtg_synergy_graph import (
    CONDITIONAL_BRANCH_KINDS,
    walk_svar_chain,
)


def _names(chain) -> list[str]:
    return [n.svar_name for n in chain]


def test_korvold_sacrifice_chain_walks_subability(korvold):
    """TrigPutCounter → DBDraw via SubAbility$. Chain depth must increment."""
    chain = walk_svar_chain("TrigPutCounter", korvold["svars"])
    assert _names(chain) == ["TrigPutCounter", "DBDraw"]
    root, child = chain
    assert root.branch_kind == "root"
    assert root.is_conditional is False
    assert child.branch_kind == "subability"
    assert child.branch_parent == "TrigPutCounter"
    assert child.chain_depth == 1
    assert child.is_conditional is False


def test_scute_swarm_branch_walker_emits_two_branches(scute_swarm):
    """TrigBranch → DBCopy (true) + DBToken (false) — both must be flagged
    is_conditional and inherit the right branch_kind."""
    chain = walk_svar_chain("TrigBranch", scute_swarm["svars"])
    by_name = {n.svar_name: n for n in chain}
    assert "TrigBranch" in by_name
    assert "DBCopy" in by_name
    assert "DBToken" in by_name

    assert by_name["TrigBranch"].branch_kind == "root"
    assert by_name["TrigBranch"].is_conditional is False

    assert by_name["DBCopy"].branch_kind == "true"
    assert by_name["DBCopy"].is_conditional is True
    assert by_name["DBCopy"].branch_parent == "TrigBranch"

    assert by_name["DBToken"].branch_kind == "false"
    assert by_name["DBToken"].is_conditional is True
    assert by_name["DBToken"].branch_parent == "TrigBranch"


def test_conditional_branch_kinds_constant_matches_spec():
    assert frozenset({"true", "false", "win", "otherwise", "choice"}) == CONDITIONAL_BRANCH_KINDS


def test_walker_terminates_on_unknown_svar():
    assert walk_svar_chain("DoesNotExist", {"X": "DB$ Pump"}) == []


def test_walker_avoids_infinite_cycle():
    """SVar A points to B, B points back to A — must terminate."""
    svars = {
        "A": "DB$ Pump | SubAbility$ B",
        "B": "DB$ Draw | SubAbility$ A",
    }
    chain = walk_svar_chain("A", svars)
    # We should see A and B at most a couple of times, never an infinite loop.
    assert len(chain) < 10
    assert "A" in _names(chain)
    assert "B" in _names(chain)
