# tests/complement_rules/test_aristocrats_death_bridge.py
import sqlite3

import pytest

from mtg_synergy_graph.complement_rules import aristocrats as arm
from mtg_synergy_graph.complement_rules.aristocrats import _commander_is_aristocrats
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port


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


@pytest.fixture()
def arm_conn(monkeypatch):
    # Emitter tests exercise the firing path — enable the default-OFF flag.
    monkeypatch.setattr(arm, "_ENABLE_ARISTOCRATS_DEATH_BRIDGE", True)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE cards (name TEXT, card_types TEXT);
        CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY, card_name TEXT, port_type TEXT,
            event_class TEXT, granted_keyword TEXT, valid_filter TEXT,
            zone_origin TEXT, zone_destination TEXT, execute_ref TEXT,
            source_svar TEXT
        );
        """
    )
    conn.executescript(
        """
        INSERT INTO cards VALUES ('Blood Artist', 'Creature');
        INSERT INTO cards VALUES ('Self Death Draw', 'Creature');
        INSERT INTO cards VALUES ('Opponent Payoff', 'Creature');
        INSERT INTO cards VALUES ('Reassembling Skeleton', 'Creature');
        INSERT INTO cards VALUES ('Undying Body', 'Creature');
        INSERT INTO cards VALUES ('Sun Titan', 'Creature');
        INSERT INTO cards VALUES ('Plain Bear', 'Creature');
        INSERT INTO cards VALUES ('Comma Drainer', 'Creature');
        INSERT INTO cards VALUES ('Comma Fodder', 'Creature');
        INSERT INTO cards VALUES ('OppOwn Payoff', 'Creature');
        INSERT INTO cards VALUES ('Dual Tier Body', 'Creature');
        """
    )
    conn.executescript(
        """
        -- Blood Artist: death payoff watching OTHER creatures, drains via execute_ref
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref, source_svar)
          VALUES ('Blood Artist', 'trigger', 'ChangesZone', 'Card.Self,Creature.Other', 'Battlefield', 'Graveyard', 'TrigLoseLife', NULL);
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Blood Artist', 'effect', 'LoseLife', 'TrigLoseLife');
        -- Self Death Draw: triggers only on ITSELF dying -> one-shot, EXCLUDED
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref)
          VALUES ('Self Death Draw', 'trigger', 'ChangesZone', 'Card.Self', 'Battlefield', 'Graveyard', 'TrigDraw');
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Self Death Draw', 'effect', 'Draw', 'TrigDraw');
        -- Opponent Payoff: watches only opponents' creatures dying -> EXCLUDED
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref)
          VALUES ('Opponent Payoff', 'trigger', 'ChangesZone', 'Creature.OppCtrl', 'Battlefield', 'Graveyard', 'TrigDmg');
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Opponent Payoff', 'effect', 'DealDamage', 'TrigDmg');
        -- Reassembling Skeleton: self-return grave->bf (no valid_filter) -> tier2
        INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination)
          VALUES ('Reassembling Skeleton', 'effect', 'ChangeZone', 'Graveyard', 'Battlefield');
        -- Undying Body: keyword fodder -> tier2
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
          VALUES ('Undying Body', 'keyword', 'Undying', 'Undying');
        -- Sun Titan: reanimator returning OTHER cards -> EXCLUDED from tier2
        INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination, valid_filter)
          VALUES ('Sun Titan', 'effect', 'ChangeZone', 'Graveyard', 'Battlefield', 'Permanent.YouCtrl+cmcLE3');
        -- Comma Drainer: comma-list zone_destination ('Graveyard,Exile') -> tier1
        -- (regression: exact-equality zone match dropped this; instr() keeps it).
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref, source_svar)
          VALUES ('Comma Drainer', 'trigger', 'ChangesZone', 'Creature.Other+YouCtrl', 'Battlefield', 'Graveyard,Exile', 'TrigLoseLife', NULL);
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Comma Drainer', 'effect', 'LoseLife', 'TrigLoseLife');
        -- Comma Fodder: comma-list zone_origin ('Graveyard,Exile') self-return -> tier2
        -- (regression: exact-equality zone match dropped this; instr() keeps it).
        INSERT INTO card_ports (card_name, port_type, event_class, zone_origin, zone_destination)
          VALUES ('Comma Fodder', 'effect', 'ChangeZone', 'Graveyard,Exile', 'Battlefield');
        -- OppOwn Payoff: watches only opponent-OWNED creatures dying -> EXCLUDED
        -- (regression: old exclusion only checked OppCtrl, admitting OppOwn).
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref)
          VALUES ('OppOwn Payoff', 'trigger', 'ChangesZone', 'Creature.OppOwn', 'Battlefield', 'Graveyard', 'TrigDmg2');
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('OppOwn Payoff', 'effect', 'DealDamage', 'TrigDmg2');
        -- Dual Tier Body: qualifies for BOTH death_payoff (drain trigger) AND
        -- recursive_fodder (Undying) -> must be emitted ONCE, as the stronger
        -- death_payoff tier (cross-tier seen-set dedup).
        INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, zone_origin, zone_destination, execute_ref, source_svar)
          VALUES ('Dual Tier Body', 'trigger', 'ChangesZone', 'Creature.Other', 'Battlefield', 'Graveyard', 'TrigDrain2', NULL);
        INSERT INTO card_ports (card_name, port_type, event_class, source_svar)
          VALUES ('Dual Tier Body', 'effect', 'LoseLife', 'TrigDrain2');
        INSERT INTO card_ports (card_name, port_type, event_class, granted_keyword)
          VALUES ('Dual Tier Body', 'keyword', 'Undying', 'Undying');
        """
    )
    conn.commit()
    return conn


_SAC = {
    "port_type": "cost",
    "event_class": "sacrifice",
    "cost_subtype": "1/Creature.Other/another creature",
    "cost_target": "other",
    "zone_origin": "",
    "zone_destination": "",
    "valid_filter": "",
}


def test_emitter_death_payoff_tier_fires(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    comps = _find_aristocrats_death_bridge(arm_conn, [_SAC], set())
    by = {c.candidate: c for c in comps}
    assert by["Blood Artist"].rule_id == "aristocrats_death_bridge"
    assert by["Blood Artist"].cand_event == "death_payoff"
    assert by["Blood Artist"].cmdr_event == "death_engine"


def test_emitter_recursive_fodder_tier_fires(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    got = {c.candidate: c.cand_event for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], set())}
    assert got.get("Reassembling Skeleton") == "recursive_fodder"
    assert got.get("Undying Body") == "recursive_fodder"
    # Comma-list zone_origin ('Graveyard,Exile') self-return — instr() keeps it.
    assert got.get("Comma Fodder") == "recursive_fodder"


def test_emitter_comma_list_zone_death_payoff_fires(arm_conn):
    # Regression: a death trigger with a comma-list zone_destination
    # ('Graveyard,Exile') must be admitted (exact-equality zone match dropped it).
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    got = {c.candidate: c.cand_event for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], set())}
    assert got.get("Comma Drainer") == "death_payoff"


def test_emitter_exclusions(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    got = {c.candidate for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], set())}
    assert "Self Death Draw" not in got  # self-death one-shot
    assert "Opponent Payoff" not in got  # opponent-scoped (OppCtrl)
    assert "OppOwn Payoff" not in got  # opponent-scoped (OppOwn — regression)
    assert "Sun Titan" not in got  # reanimator returning other cards
    assert "Plain Bear" not in got  # no relevant port


def test_emitter_one_complement_per_candidate(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    comps = _find_aristocrats_death_bridge(arm_conn, [_SAC], set())
    assert len(comps) == len({c.candidate for c in comps})
    # Dual Tier Body qualifies for BOTH tiers; cross-tier seen-set dedup must
    # emit it exactly once, as the stronger death_payoff tier (scanned first).
    dual = [c for c in comps if c.candidate == "Dual Tier Body"]
    assert len(dual) == 1
    assert dual[0].cand_event == "death_payoff"


def test_emitter_excludes_commander_itself(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    got = {c.candidate for c in _find_aristocrats_death_bridge(arm_conn, [_SAC], {"Blood Artist"})}
    assert "Blood Artist" not in got


def test_emitter_non_qualifying_commander_empty(arm_conn):
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    non = {
        "port_type": "cost",
        "event_class": "sacrifice",
        "cost_subtype": "1/Artifact",
        "cost_target": "any",
        "zone_origin": "",
        "zone_destination": "",
        "valid_filter": "",
    }
    assert _find_aristocrats_death_bridge(arm_conn, [non], set()) == []


def test_emitter_flag_off_returns_empty(arm_conn, monkeypatch):
    monkeypatch.setattr(arm, "_ENABLE_ARISTOCRATS_DEATH_BRIDGE", False)
    from mtg_synergy_graph.complement_rules.aristocrats import _find_aristocrats_death_bridge

    assert _find_aristocrats_death_bridge(arm_conn, [_SAC], set()) == []


def test_rule_gate_flag_aware():
    port = {
        "port_type": "trigger",
        "event_class": "ChangesZone",
        "zone_origin": "Battlefield",
        "zone_destination": "Graveyard",
        "cost_subtype": "",
        "cost_target": "",
        "valid_filter": "",
    }
    arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = False
    assert "aristocrats_death_bridge" not in attributable_rules_for_port(port)
    arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = True
    try:
        assert "aristocrats_death_bridge" in attributable_rules_for_port(port)
    finally:
        arm._ENABLE_ARISTOCRATS_DEATH_BRIDGE = False
