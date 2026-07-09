import sqlite3

import pytest

from mtg_synergy_graph.complement_rules import combat as combat_mod
from mtg_synergy_graph.complement_rules.combat import _commander_has_team_attack_reward
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port


@pytest.fixture(autouse=True)
def _no_tribal(monkeypatch):
    # Default: the commander has no tribal subtype. Individual tests override.
    monkeypatch.setattr(combat_mod, "_commander_subtypes_from_ports", lambda *a, **k: set())


def _trig(event_class, valid_filter="", raw_line=""):
    return {"port_type": "trigger", "event_class": event_class, "valid_filter": valid_filter, "raw_line": raw_line}


def _kw(event_class):
    return {"port_type": "keyword", "event_class": event_class, "valid_filter": "", "raw_line": event_class}


def _pumpall(valid_filter):
    return {"port_type": "effect", "event_class": "PumpAll", "valid_filter": valid_filter, "raw_line": ""}


def test_attacks_team_scope_valid_filter_qualifies():
    ports = [_trig("Attacks", valid_filter="Creature.YouCtrl")]
    assert _commander_has_team_attack_reward(None, ports, set()) is True


def test_attackersdeclared_attackingplayer_you_qualifies():
    # Aloy/Caesar shape: valid_filter empty, scope in raw_line.
    ports = [
        _trig("AttackersDeclared", raw_line="{'AttackingPlayer': 'You', 'ValidAttackers': 'Creature.Artifact+YouCtrl'}")
    ]
    assert _commander_has_team_attack_reward(None, ports, set()) is True


def test_self_attack_plus_team_pumpall_qualifies():
    # Agrus Kos shape: Attacks Card.Self trigger + PumpAll over attacking creatures.
    ports = [_trig("Attacks", valid_filter="Card.Self"), _pumpall("Creature.attacking+Red")]
    assert _commander_has_team_attack_reward(None, ports, set()) is True


def test_self_attack_without_team_pump_rejected():
    # "Whenever CARDNAME attacks, draw" — self-benefit, evasion on others irrelevant.
    ports = [_trig("Attacks", valid_filter="Card.Self")]
    assert _commander_has_team_attack_reward(None, ports, set()) is False


def test_attackersdeclared_opponent_attack_rejected():
    # AttackingPlayer not You -> not our board.
    ports = [_trig("AttackersDeclared", raw_line="{'AttackingPlayer': 'Opponent'}")]
    assert _commander_has_team_attack_reward(None, ports, set()) is False


def test_exalted_commander_rejected():
    # Rafiq shape: Exalted rewards attacking ALONE — incompatible with wide board.
    ports = [_trig("AttackersDeclared", raw_line="{'AttackingPlayer': 'You'}"), _kw("Exalted")]
    assert _commander_has_team_attack_reward(None, ports, set()) is False


def test_tribal_commander_rejected(monkeypatch):
    # Najeela shape: has a Warrior subtype -> routed to tribal rules.
    monkeypatch.setattr(combat_mod, "_commander_subtypes_from_ports", lambda *a, **k: {"Warrior"})
    ports = [_trig("AttackersDeclared", raw_line="{'AttackingPlayer': 'You'}")]
    assert _commander_has_team_attack_reward(None, ports, set()) is False


def test_no_attack_trigger_rejected():
    ports = [_trig("Sacrificed", valid_filter="Creature.YouCtrl")]
    assert _commander_has_team_attack_reward(None, ports, set()) is False


@pytest.fixture()
def evasion_conn(monkeypatch):
    # Emitter tests exercise the firing path — enable the default-OFF flag here;
    # the flag-off test overrides it back to False.
    monkeypatch.setattr(combat_mod, "_ENABLE_ATTACK_REWARD_EVASION", True)
    # This commander qualifies (team-scope Attacks trigger); no tribal subtype
    # (the autouse _no_tribal fixture already stubs that).
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE cards (name TEXT, card_types TEXT);
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY, card_name TEXT, port_type TEXT,
            event_class TEXT, granted_keyword TEXT, valid_filter TEXT, raw_line TEXT
        );
        """
    )
    conn.executescript(
        """
        INSERT INTO cards VALUES ('Serra Angel', 'Creature');
        INSERT INTO cards VALUES ('Goblin Piker', 'Creature');
        INSERT INTO cards VALUES ('Dauthi Slayer', 'Creature');
        INSERT INTO cards VALUES ('Ornithopter Token', 'Artifact');
        """
    )
    conn.executescript(
        """
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
            VALUES ('Serra Angel', 'keyword', 'Flying', 'Flying');
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
            VALUES ('Dauthi Slayer', 'keyword', 'Shadow', 'Shadow');
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
            VALUES ('Ornithopter Token', 'keyword', 'Flying', 'Flying');
        """
    )
    conn.commit()
    return conn


_TEAM_TRIGGER = {"port_type": "trigger", "event_class": "Attacks", "valid_filter": "Creature.YouCtrl", "raw_line": ""}


def test_emitter_soft_evasion_creature_fires(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    comps = _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], set())
    by = {c.candidate: c for c in comps}
    assert by["Serra Angel"].rule_id == "attack_reward_evasion"
    assert by["Serra Angel"].cand_event == "evasion_soft"
    assert by["Serra Angel"].cmdr_event == "attack_reward"


def test_emitter_hard_tier(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    comps = _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], set())
    by = {c.candidate: c for c in comps}
    assert by["Dauthi Slayer"].cand_event == "evasion_hard"


def test_emitter_excludes_non_creature(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    comps = _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], set())
    assert "Ornithopter Token" not in {c.candidate for c in comps}


def test_emitter_no_double_no_vanilla(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    comps = _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], set())
    # Goblin Piker (no evasion keyword) gets no complement.
    assert "Goblin Piker" not in {c.candidate for c in comps}
    # One complement per candidate.
    assert len(comps) == len({c.candidate for c in comps})


def test_emitter_excludes_commander_itself(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    comps = _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], {"Serra Angel"})
    assert "Serra Angel" not in {c.candidate for c in comps}


def test_emitter_non_qualifying_commander_empty(evasion_conn):
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    self_only = {"port_type": "trigger", "event_class": "Attacks", "valid_filter": "Card.Self", "raw_line": ""}
    assert _find_attack_reward_evasion(evasion_conn, [self_only], set()) == []


def test_emitter_flag_off_returns_empty(evasion_conn, monkeypatch):
    monkeypatch.setattr(combat_mod, "_ENABLE_ATTACK_REWARD_EVASION", False)
    from mtg_synergy_graph.complement_rules.combat import _find_attack_reward_evasion

    assert _find_attack_reward_evasion(evasion_conn, [_TEAM_TRIGGER], set()) == []


def test_rule_gate_flag_aware():
    port = {
        "port_type": "trigger",
        "event_class": "AttackersDeclared",
        "valid_filter": "",
        "raw_line": "{'AttackingPlayer': 'You'}",
    }
    combat_mod._ENABLE_ATTACK_REWARD_EVASION = False
    assert "attack_reward_evasion" not in attributable_rules_for_port(port)
    combat_mod._ENABLE_ATTACK_REWARD_EVASION = True
    try:
        assert "attack_reward_evasion" in attributable_rules_for_port(port)
    finally:
        combat_mod._ENABLE_ATTACK_REWARD_EVASION = False
