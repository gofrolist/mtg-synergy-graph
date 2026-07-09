import pytest

from mtg_synergy_graph.complement_rules import combat as combat_mod
from mtg_synergy_graph.complement_rules.combat import _commander_has_team_attack_reward


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
