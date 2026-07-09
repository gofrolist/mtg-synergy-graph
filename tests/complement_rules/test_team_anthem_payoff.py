import sqlite3

import pytest

from mtg_synergy_graph.complement_rules import statics as statics_mod
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port
from mtg_synergy_graph.complement_rules.statics import (
    _commander_team_anthem_statics,
    _find_team_anthem_payoffs,
)


def _static(affected_scope, raw_line):
    return {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": affected_scope,
        "raw_line": raw_line,
    }


def test_youctrl_permanent_keyword_anthem_qualifies():
    # Avacyn shape: grants Indestructible to your other permanents.
    ports = [_static("Permanent.Other+YouCtrl", "{'AddKeyword': 'Indestructible'}")]
    assert _commander_team_anthem_statics(ports) == ports


def test_youctrl_creature_pump_anthem_qualifies():
    # Iroas shape: Menace to your creatures.
    ports = [_static("Creature.YouCtrl", "{'AddKeyword': 'Menace'}")]
    assert len(_commander_team_anthem_statics(ports)) == 1


def test_symmetric_anthem_no_youctrl_rejected():
    # Ascendant Evincar shape: Creature.Black+Other, no YouCtrl (symmetric).
    ports = [_static("Creature.Black+Other", "{'AddPower': '1', 'AddToughness': '1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_self_only_static_rejected():
    # Voltron sub-shape, out of scope.
    ports = [_static("Card.Self", "{'AddKeyword': 'Double Strike'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_subtype_anthem_rejected():
    # Goblin lord — lord's territory, base type is a subtype not Creature/Permanent.
    ports = [_static("Goblin.YouCtrl", "{'AddPower': '1', 'AddToughness': '1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_drawback_static_rejected():
    # Negative pump is not a payoff.
    ports = [_static("Creature.YouCtrl", "{'AddPower': '-1', 'AddToughness': '-1'}")]
    assert _commander_team_anthem_statics(ports) == []


def test_non_static_port_ignored():
    trigger = {"port_type": "trigger", "event_class": "Attacks", "affected_scope": "", "raw_line": ""}
    assert _commander_team_anthem_statics([trigger]) == []


@pytest.fixture()
def anthem_conn(monkeypatch):
    # The rule is flag-gated default-OFF (added in this task's implementation
    # step). Every emitter test below exercises the firing path, so enable the
    # flag here; the flag-off test overrides it back to False explicitly.
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY,
            card_name TEXT,
            port_type TEXT,
            event_class TEXT,
            affected_scope TEXT,
            raw_line TEXT
        );
        """
    )
    # A creature-token producer (P/T in TokenScript).
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Grave Titan', 'effect', 'Token', ?)",
        ("{'TokenScript': 'b_2_2_zombie'}",),
    )
    # A non-creature (Treasure) producer — no P/T in TokenScript.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Smothering Tithe', 'effect', 'Token', ?)",
        ("{'TokenScript': 'c_a_treasure'}",),
    )
    # A token doubler (replacement.CreateToken).
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Doubling Season', 'replacement', 'CreateToken', '{}')",
    )
    conn.commit()
    return conn


_AVACYN_STATIC = {
    "port_type": "static",
    "event_class": "Continuous",
    "affected_scope": "Permanent.Other+YouCtrl",
    "raw_line": "{'AddKeyword': 'Indestructible'}",
}


def test_emitter_creature_producer_fires(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    by_cand = {c.candidate for c in comps}
    assert "Grave Titan" in by_cand
    grave_titan_comp = next(c for c in comps if c.candidate == "Grave Titan")
    assert grave_titan_comp.rule_id == "team_anthem_payoff"
    assert grave_titan_comp.cand_event == "token_producer"


def test_emitter_treasure_maker_excluded(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    assert "Smothering Tithe" not in {c.candidate for c in comps}


def test_emitter_doubler_tier(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set())
    doubling_season_comp = next(c for c in comps if c.candidate == "Doubling Season")
    assert doubling_season_comp.cand_event == "token_doubler"


def test_emitter_no_qualifying_static_returns_empty(anthem_conn):
    self_only = dict(_AVACYN_STATIC, affected_scope="Card.Self")
    assert _find_team_anthem_payoffs(anthem_conn, [self_only], set()) == []


def test_emitter_dedup_single_complement_per_candidate(anthem_conn):
    # A card that is BOTH a doubler and a creature producer resolves to ONE
    # complement, at the strong (doubler) tier.
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Ojer Taq', 'effect', 'Token', ?)",
        ("{'TokenScript': 'w_1_1_human'}",),
    )
    anthem_conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
        "VALUES ('Ojer Taq', 'replacement', 'CreateToken', '{}')",
    )
    anthem_conn.commit()
    comps = [c for c in _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) if c.candidate == "Ojer Taq"]
    assert len(comps) == 1
    assert comps[0].cand_event == "token_doubler"


def test_emitter_excludes_commander_itself(anthem_conn):
    comps = _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], {"Grave Titan"})
    assert "Grave Titan" not in {c.candidate for c in comps}


def test_emitter_flag_off_returns_empty(anthem_conn, monkeypatch):
    # anthem_conn enabled the flag; override it off — the guard must short-circuit.
    monkeypatch.setattr(statics_mod, "_ENABLE_TEAM_ANTHEM_PAYOFF", False)
    assert _find_team_anthem_payoffs(anthem_conn, [_AVACYN_STATIC], set()) == []


def test_rule_gate_flag_aware():
    port = {
        "port_type": "static",
        "event_class": "Continuous",
        "affected_scope": "Creature.YouCtrl",
        "raw_line": "{'AddKeyword': 'Menace'}",
    }
    # Flag off: gate reports NO coverage so gap_report/quality see the truth.
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
    assert "team_anthem_payoff" not in attributable_rules_for_port(port)
    statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = True
    try:
        assert "team_anthem_payoff" in attributable_rules_for_port(port)
    finally:
        statics_mod._ENABLE_TEAM_ANTHEM_PAYOFF = False
