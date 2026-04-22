"""Unit tests for _find_cheat_cmc_bonus label invariants.

Pins the contract established in commit ``003bfcb`` (unify Angel /
Demon / Dragon subtypes into one IDF pool via ``cand_event='cheatable'``
and drop the ``cmc_mid`` bracket). A regression to per-subtype
``cand_event`` or a restored ``cmc_mid`` bracket would silently
reintroduce the inverted-IDF bug where cheap Demons outscored
expensive Angels on Kaalia.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.density import _find_cheat_cmc_bonus


@pytest.fixture()
def conn(rules_db):
    """Local alias for the shared ``rules_db`` fixture (schema lives in
    conftest.py)."""
    return rules_db


def _add_card(
    conn: sqlite3.Connection,
    name: str,
    *,
    card_types: str,
    subtypes: str,
    cmc: int,
) -> None:
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc) VALUES (?, ?, ?, ?)",
        (name, card_types, subtypes, cmc),
    )


def _kaalia_effect_port() -> dict:
    """Kaalia's Attacks trigger execute: ChangeZone Hand→Battlefield
    with ChangeType filter covering Angel/Demon/Dragon."""
    return {
        "port_type": "effect",
        "event_class": "ChangeZone",
        "valid_filter": "",
        "zone_origin": "Hand",
        "zone_destination": "Battlefield",
        "branch_kind": "execute",
        "effect_conditional": 0,
        "raw_line": (
            "{'Mode': 'ChangeZone', 'Origin': 'Hand', 'Destination': 'Battlefield', "
            "'ChangeType': 'Creature.Angel+YouCtrl,Creature.Demon+YouCtrl,"
            "Creature.Dragon+YouCtrl', 'Tapped': 'True'}"
        ),
    }


class TestCheatCmcLabels:
    def test_cand_event_is_unified_cheatable(self, conn):
        """Every emitted complement must carry ``cand_event='cheatable'``,
        not the per-subtype label (Angel / Demon / Dragon). Unifying
        pools all three subtypes into one IDF bucket per CMC bracket."""
        _add_card(conn, "Avacyn, Angel of Hope", card_types="Creature", subtypes="Angel", cmc=8)
        _add_card(conn, "Rune-Scarred Demon", card_types="Creature", subtypes="Demon", cmc=7)
        _add_card(conn, "Balefire Dragon", card_types="Creature", subtypes="Dragon", cmc=7)
        results = _find_cheat_cmc_bonus(conn, [_kaalia_effect_port()], set())
        assert results
        assert all(r.cand_event == "cheatable" for r in results)

    def test_filter_group_is_cmc_high(self, conn):
        """All emitted complements carry ``filter_group='cmc_high'``
        — the cmc_mid bracket was dropped so this is the only label."""
        _add_card(conn, "Avacyn, Angel of Hope", card_types="Creature", subtypes="Angel", cmc=8)
        results = _find_cheat_cmc_bonus(conn, [_kaalia_effect_port()], set())
        assert results
        assert all(r.filter_group == "cmc_high" for r in results)

    def test_cmc_below_6_not_emitted(self, conn):
        """CMC 4 and 5 creatures of the target subtypes are excluded
        from cheat_cmc output. Before the cmc_mid drop, CMC 4-5 had a
        smaller pool and therefore higher per-card IDF than CMC ≥ 6,
        which inverted the mana-saved semantic. CMC 4-5 cheats still
        score via trigger_effect on their ETB chain."""
        _add_card(conn, "Kaalia, Zenith Seeker", card_types="Creature", subtypes="Angel", cmc=4)
        _add_card(conn, "Master of Cruelties", card_types="Creature", subtypes="Demon", cmc=5)
        _add_card(conn, "Rune-Scarred Demon", card_types="Creature", subtypes="Demon", cmc=7)
        results = _find_cheat_cmc_bonus(conn, [_kaalia_effect_port()], set())
        names = {r.candidate for r in results}
        assert "Rune-Scarred Demon" in names
        assert "Kaalia, Zenith Seeker" not in names
        assert "Master of Cruelties" not in names

    def test_rule_id_and_cmdr_event(self, conn):
        _add_card(conn, "Avacyn, Angel of Hope", card_types="Creature", subtypes="Angel", cmc=8)
        results = _find_cheat_cmc_bonus(conn, [_kaalia_effect_port()], set())
        assert results
        assert all(r.rule_id == "cheat_cmc" for r in results)
        assert all(r.cmdr_event == "cheat_into_play" for r in results)

    def test_no_commander_without_cheat_effect(self, conn):
        """A commander without a ChangeZone cheat effect gets nothing."""
        _add_card(conn, "Avacyn, Angel of Hope", card_types="Creature", subtypes="Angel", cmc=8)
        non_cheat_ports = [
            {
                "port_type": "trigger",
                "event_class": "Attacks",
                "valid_filter": "Card.Self",
                "raw_line": "{'Mode': 'Attacks'}",
            }
        ]
        assert _find_cheat_cmc_bonus(conn, non_cheat_ports, set()) == []

    def test_excludes_commander_self(self, conn):
        _add_card(conn, "Kaalia of the Vast", card_types="Creature", subtypes="Angel", cmc=4)
        _add_card(conn, "Avacyn, Angel of Hope", card_types="Creature", subtypes="Angel", cmc=8)
        results = _find_cheat_cmc_bonus(conn, [_kaalia_effect_port()], {"Kaalia of the Vast"})
        names = {r.candidate for r in results}
        assert "Kaalia of the Vast" not in names
