"""Tests for the token_etb_damage rule.

Commanders that repeatedly create creature tokens (The Locust God,
Krenko, Edgar Markov) want cards whose creature-ETB trigger deals
damage: Impact Tremors, Purphoros, Witty Roastmaster. Every token
that enters = damage ping to opponents.

Narrow gate: commander has a Token effect in any branch.
Candidate must have BOTH a ChangesZone creature-ETB trigger AND a
DealDamage effect targeting a Player/Opponent — i.e. a self-contained
ETB-damage card.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Integration: requires the full 32k-card synergy.db. Tagged with the
# `integration` marker so CI deselects via `-m "not integration"`;
# skipif is a fallback for `pytest -m integration` without the data.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not Path("data/synergy.db").exists(),
        reason="requires data/synergy.db (run scripts/import_cardsfolder.py)",
    ),
]


@pytest.fixture(scope="module")
def conn():
    c = sqlite3.connect("data/synergy.db")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestTokenEtbDamage:
    def test_locust_god_matches_impact_tremors(self, conn):
        from mtg_synergy_graph.complement_rules.tokens import (
            _find_token_etb_damage,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["The Locust God"])
        results = _find_token_etb_damage(conn, ports, {"The Locust God"})
        names = {r.candidate for r in results}
        assert "Impact Tremors" in names
        assert "Purphoros, God of the Forge" in names
        assert "Witty Roastmaster" in names

    def test_activated_token_commander_no_match(self, conn):
        """Ghave/Krenko/Rhys create tokens via activated abilities, not
        passive triggers. Their strategy (counter multiplication, goblin
        tribal, elf tribal) is separate from ETB-damage synergy —
        including them caused regressions for Ghave and others."""
        from mtg_synergy_graph.complement_rules.tokens import (
            _find_token_etb_damage,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        for cmdr in ["Krenko, Mob Boss", "Ghave, Guru of Spores", "Rhys the Redeemed"]:
            ports = load_ports_for_set(conn, [cmdr])
            results = _find_token_etb_damage(conn, ports, {cmdr})
            assert results == [], f"{cmdr} activated-only token — must not match"

    def test_prossh_matches(self, conn):
        """Prossh creates Kobold tokens on ETB (trigger-driven)."""
        from mtg_synergy_graph.complement_rules.tokens import (
            _find_token_etb_damage,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["Prossh, Skyraider of Kher"])
        results = _find_token_etb_damage(conn, ports, {"Prossh, Skyraider of Kher"})
        names = {r.candidate for r in results}
        assert "Impact Tremors" in names

    def test_non_token_commander_no_match(self, conn):
        """Commanders without any Token effect should not match."""
        from mtg_synergy_graph.complement_rules.tokens import (
            _find_token_etb_damage,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        # Rhystic Study: creates no tokens (draws cards). Marchesa: returns
        # creatures from GY, no tokens.
        for cmdr in ["Rhystic Study", "Marchesa, the Black Rose"]:
            ports = load_ports_for_set(conn, [cmdr])
            # Only check commanders that genuinely have no Token effect port.
            has_token = any(
                (p.get("port_type") or "").strip() == "effect" and (p.get("event_class") or "").strip() == "Token"
                for p in ports
            )
            if has_token:
                continue
            results = _find_token_etb_damage(conn, ports, {cmdr})
            assert results == [], f"{cmdr} has no Token effect"

    def test_rule_id(self, conn):
        from mtg_synergy_graph.complement_rules.tokens import (
            _find_token_etb_damage,
        )
        from mtg_synergy_graph.graph_engine import load_ports_for_set

        ports = load_ports_for_set(conn, ["The Locust God"])
        results = _find_token_etb_damage(conn, ports, {"The Locust God"})
        assert all(r.rule_id == "token_etb_damage" for r in results)
