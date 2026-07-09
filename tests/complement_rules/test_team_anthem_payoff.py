from mtg_synergy_graph.complement_rules.statics import _commander_team_anthem_statics


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
