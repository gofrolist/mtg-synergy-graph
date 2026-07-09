# tests/complement_rules/test_aristocrats_death_bridge.py
from mtg_synergy_graph.complement_rules.aristocrats import _commander_is_aristocrats


def _port(port_type, event_class, **kw):
    base = {
        "port_type": port_type,
        "event_class": event_class,
        "cost_subtype": "",
        "cost_target": "",
        "zone_origin": "",
        "zone_destination": "",
        "valid_filter": "",
    }
    base.update(kw)
    return base


def test_sac_outlet_creature_other_qualifies():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Creature.Other/another creature", cost_target="other")]
    assert _commander_is_aristocrats(ports) is True


def test_sac_outlet_creature_any_qualifies():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Creature", cost_target="any")]
    assert _commander_is_aristocrats(ports) is True


def test_death_trigger_payoff_qualifies():
    ports = [_port("trigger", "ChangesZone", zone_origin="Battlefield", zone_destination="Graveyard")]
    assert _commander_is_aristocrats(ports) is True


def test_sac_self_only_rejected():
    # Sacrificing itself (e.g. a fling body) is not a sac-outlet engine.
    ports = [_port("cost", "sacrifice", cost_subtype="1/CARDNAME", cost_target="self")]
    assert _commander_is_aristocrats(ports) is False


def test_sac_artifact_rejected():
    ports = [_port("cost", "sacrifice", cost_subtype="1/Artifact", cost_target="any")]
    assert _commander_is_aristocrats(ports) is False


def test_etb_changeszone_not_death_rejected():
    # Battlefield->Exile or ->Hand is not a death trigger.
    ports = [_port("trigger", "ChangesZone", zone_origin="Battlefield", zone_destination="Exile")]
    assert _commander_is_aristocrats(ports) is False


def test_empty_ports_rejected():
    assert _commander_is_aristocrats([]) is False
