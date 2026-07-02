"""Pool-scaled flat weights — plan 2026-07-02-002 Unit 6.

``_ENABLE_POOL_SCALED_FLAT_WEIGHTS`` (default False) scales each flat
key's weight by ``log2(1+floor)/log2(1+pool_N)`` for pools above the
floor — per basis key, so each tribe / spell type prices separately.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

import mtg_synergy_graph.universal_scorer as us_mod
from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.universal_scorer import _compute_idf_basis


def _comps(rule_id: str, cand_event: str, n: int) -> list[PortComplement]:
    return [
        PortComplement(
            rule_id=rule_id,
            direction="synergy",
            candidate=f"Cand{cand_event}{i}",
            cmdr_event="tribal",
            cand_event=cand_event,
        )
        for i in range(n)
    ]


KEY_GOBLIN = ("tribal_body", "tribal", "Goblin", "")
KEY_SQUIRREL = ("tribal_body", "tribal", "Squirrel", "")


def test_flag_default_is_false():
    assert us_mod._ENABLE_POOL_SCALED_FLAT_WEIGHTS is False


def test_flag_off_constant_weight_any_pool():
    basis = _compute_idf_basis(_comps("tribal_body", "Goblin", 500))
    assert basis.flat_weights[KEY_GOBLIN] == pytest.approx(0.3)


def test_flag_on_small_pool_keeps_full_weight():
    with patch.object(us_mod, "_ENABLE_POOL_SCALED_FLAT_WEIGHTS", True):
        basis = _compute_idf_basis(_comps("tribal_body", "Squirrel", 30))
    assert basis.flat_weights[KEY_SQUIRREL] == pytest.approx(0.3)


def test_flag_on_large_pool_scales_down():
    with patch.object(us_mod, "_ENABLE_POOL_SCALED_FLAT_WEIGHTS", True):
        basis = _compute_idf_basis(_comps("tribal_body", "Goblin", 1500))
    expected = 0.3 * math.log2(31) / math.log2(1501)
    assert basis.flat_weights[KEY_GOBLIN] == pytest.approx(expected)
    assert basis.flat_weights[KEY_GOBLIN] < 0.15


def test_flag_on_per_key_separation():
    """A 40-card tribe and a 4000-card tribe under the same rule price
    separately — the Unit 6 doc-review keying requirement."""
    comps = _comps("tribal_body", "Squirrel", 40) + _comps("tribal_body", "Goblin", 4000)
    with patch.object(us_mod, "_ENABLE_POOL_SCALED_FLAT_WEIGHTS", True):
        basis = _compute_idf_basis(comps)
    w_small = basis.flat_weights[KEY_SQUIRREL]
    w_large = basis.flat_weights[KEY_GOBLIN]
    assert w_small > w_large
    assert w_small == pytest.approx(0.3 * math.log2(31) / math.log2(41))


def test_cond_mult_composes_with_pool_scaling():
    comps = [
        PortComplement(
            rule_id="tribal_body",
            direction="synergy",
            candidate=f"C{i}",
            cmdr_event="tribal",
            cand_event="Goblin",
            filter_group="Goblin:cond",
        )
        for i in range(100)
    ]
    with patch.object(us_mod, "_ENABLE_POOL_SCALED_FLAT_WEIGHTS", True):
        basis = _compute_idf_basis(comps)
    key = ("tribal_body", "tribal", "Goblin", "Goblin:cond")
    expected = 0.3 * (math.log2(31) / math.log2(101)) * 0.5
    assert basis.flat_weights[key] == pytest.approx(expected)
