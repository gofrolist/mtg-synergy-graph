"""anthem_payoff — type-scoped anthems for creature-token producers.

Plan 2026-07-02-002 Unit 10: closes the global-anthem slice of the
159-card static.Continuous NO_RULES block. Axis-gated (commander must
produce creature tokens); IDF-weighted (never flat); tribal-scoped
anthems stay lord's territory.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.statics import _find_anthem_payoffs


@pytest.fixture()
def conn(rules_db: sqlite3.Connection) -> sqlite3.Connection:
    return rules_db


def _port(conn, card, port_type, event_class, *, affected_scope="", raw_line="", valid_filter=""):
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
        "affected_scope, branch_kind, raw_line) VALUES (?, ?, ?, ?, ?, 'root', ?)",
        (card, port_type, event_class, valid_filter, affected_scope, raw_line),
    )


def _token_cmdr_ports(conn, name="TokenCmdr", script="w_1_1_human"):
    _port(conn, name, "effect", "Token", raw_line=f"{{'TokenScript': '{script}'}}")
    return [dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", (name,)).fetchall()]


def _anthem(
    conn,
    name,
    scope="Creature.YouCtrl",
    raw="{'Mode':'Continuous','Affected':'Creature.YouCtrl','AddPower':'1','AddToughness':'1'}",
):
    _port(conn, name, "static", "Continuous", affected_scope=scope, raw_line=raw)


class TestAnthemPayoff:
    def test_token_commander_matches_global_anthem(self, conn):
        cmdr_ports = _token_cmdr_ports(conn)
        _anthem(conn, "Glorious Anthem")
        results = _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"})
        by_cand = {r.candidate: r for r in results}
        assert "Glorious Anthem" in by_cand
        assert by_cand["Glorious Anthem"].rule_id == "anthem_payoff"
        assert by_cand["Glorious Anthem"].cand_event == "creature_anthem"

    def test_keyword_granting_anthem_matches(self, conn):
        cmdr_ports = _token_cmdr_ports(conn)
        _anthem(
            conn,
            "Intangible Wind",
            raw="{'Mode':'Continuous','Affected':'Creature.YouCtrl','AddKeyword':'Flying'}",
        )
        results = _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"})
        assert "Intangible Wind" in {r.candidate for r in results}

    def test_noncreature_token_commander_gets_nothing(self, conn):
        """Treasure-token scripts carry no P/T segment — the axis is
        not supplied, no anthem emission."""
        cmdr_ports = _token_cmdr_ports(conn, script="c_a_treasure_sac")
        _anthem(conn, "Glorious Anthem")
        assert _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"}) == []

    def test_opponent_scoped_anthem_excluded(self, conn):
        cmdr_ports = _token_cmdr_ports(conn)
        _anthem(
            conn,
            "Enemy Anthem",
            scope="Creature.OppCtrl",
            raw="{'Mode':'Continuous','Affected':'Creature.OppCtrl','AddPower':'1'}",
        )
        assert _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"}) == []

    def test_drawback_static_excluded(self, conn):
        cmdr_ports = _token_cmdr_ports(conn)
        _anthem(
            conn,
            "Curse of Weakness",
            raw="{'Mode':'Continuous','Affected':'Creature.YouCtrl','AddPower':'-1'}",
        )
        assert _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"}) == []

    def test_tribal_scoped_anthem_stays_lords_territory(self, conn):
        """Goblin.YouCtrl anthems are subtype-scoped — lord's match, not
        anthem_payoff's (prevents double counting)."""
        cmdr_ports = _token_cmdr_ports(conn)
        _anthem(
            conn,
            "Goblin King",
            scope="Goblin.YouCtrl",
            raw="{'Mode':'Continuous','Affected':'Goblin.YouCtrl','AddPower':'1'}",
        )
        assert _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"}) == []

    def test_null_affected_scope_skipped(self, conn):
        """NULL/empty affected_scope rows never crash the matcher — the
        etb_tapped_stax NULL-filter invariant precedent."""
        cmdr_ports = _token_cmdr_ports(conn)
        _port(
            conn,
            "Weird Static",
            "static",
            "Continuous",
            raw_line="{'Mode':'Continuous','AddPower':'1'}",
        )
        assert _find_anthem_payoffs(conn, cmdr_ports, {"TokenCmdr"}) == []

    def test_no_token_production_no_emission(self, conn):
        _port(conn, "SpellCmdr", "trigger", "SpellCast", valid_filter="Card.Self")
        cmdr_ports = [
            dict(r) for r in conn.execute("SELECT * FROM card_ports WHERE card_name = ?", ("SpellCmdr",)).fetchall()
        ]
        _anthem(conn, "Glorious Anthem")
        assert _find_anthem_payoffs(conn, cmdr_ports, {"SpellCmdr"}) == []
