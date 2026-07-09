import sqlite3

import pytest

from mtg_synergy_graph.complement_rules import density as density_mod
from mtg_synergy_graph.complement_rules.density import _commander_has_x_cost_ability
from mtg_synergy_graph.complement_rules.registry import attributable_rules_for_port


def _port(port_type, event_class, valid_filter="", raw_line=""):
    return {"port_type": port_type, "event_class": event_class, "valid_filter": valid_filter, "raw_line": raw_line}


def test_xpaid_scales_with_qualifies():
    ports = [_port("scales_with", "xPaid")]
    assert _commander_has_x_cost_ability(ports) is True


def test_xpaid_among_other_ports_qualifies():
    ports = [_port("trigger", "Attacks"), _port("scales_with", "xPaid", raw_line="{'Amount':'X'}")]
    assert _commander_has_x_cost_ability(ports) is True


def test_no_xpaid_rejected():
    ports = [_port("scales_with", "Valid", valid_filter="Creature.YouCtrl")]
    assert _commander_has_x_cost_ability(ports) is False


def test_wrong_port_type_rejected():
    # A non-scales_with port that happens to mention xPaid is not the gate.
    ports = [_port("effect", "xPaid")]
    assert _commander_has_x_cost_ability(ports) is False


def test_empty_ports_rejected():
    assert _commander_has_x_cost_ability([]) is False


@pytest.fixture()
def x_cost_conn(monkeypatch):
    # Emitter tests exercise the firing path — enable the default-OFF flag here;
    # the flag-off test overrides it back to False.
    monkeypatch.setattr(density_mod, "_ENABLE_X_COST_SCALER", True)
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
        INSERT INTO cards VALUES ('Nyxbloom Ancient', 'Creature');
        INSERT INTO cards VALUES ('Goblin Electromancer', 'Creature');
        INSERT INTO cards VALUES ('Academy Journeymage', 'Creature');
        INSERT INTO cards VALUES ('Wizard Lord', 'Creature');
        INSERT INTO cards VALUES ('Contamination', 'Enchantment');
        INSERT INTO cards VALUES ('Plain Bear', 'Creature');
        """
    )
    conn.executescript(
        r"""
        -- T1 doubler
        INSERT INTO card_ports (card_name, port_type, event_class, raw_line)
          VALUES ('Nyxbloom Ancient', 'replacement', 'ProduceMana',
                  '{''Event'': ''ProduceMana'', ''ReplaceWith'': ''ProduceThrice''}');
        -- T2 broad generic reducer (Instant,Sorcery, Amount 1)
        INSERT INTO card_ports (card_name, port_type, event_class, raw_line)
          VALUES ('Goblin Electromancer', 'static', 'ReduceCost',
                  '{''Mode'': ''ReduceCost'', ''ValidCard'': ''Instant,Sorcery'', ''Type'': ''Spell'', ''Amount'': ''1''}');
        -- Self-cost reducer -> EXCLUDED (ValidCard Card.Self)
        INSERT INTO card_ports (card_name, port_type, event_class, raw_line)
          VALUES ('Academy Journeymage', 'static', 'ReduceCost',
                  '{''Mode'': ''ReduceCost'', ''ValidCard'': ''Card.Self'', ''Type'': ''Spell'', ''Amount'': ''1''}');
        -- Tribe-narrow reducer -> EXCLUDED (ValidCard not in broad set)
        INSERT INTO card_ports (card_name, port_type, event_class, raw_line)
          VALUES ('Wizard Lord', 'static', 'ReduceCost',
                  '{''Mode'': ''ReduceCost'', ''ValidCard'': ''Wizard.YouCtrl'', ''Type'': ''Spell'', ''Amount'': ''1''}');
        -- Mana DENIAL replacement -> EXCLUDED (ReplaceWith ProduceB, not Twice/Thrice)
        INSERT INTO card_ports (card_name, port_type, event_class, raw_line)
          VALUES ('Contamination', 'replacement', 'ProduceMana',
                  '{''Event'': ''ProduceMana'', ''ReplaceWith'': ''ProduceB''}');
        """
    )
    conn.commit()
    return conn


_XPAID = {"port_type": "scales_with", "event_class": "xPaid", "valid_filter": "", "raw_line": "{'Amount':'X'}"}


def test_emitter_mana_double_tier_fires(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    comps = _find_x_cost_scaler(x_cost_conn, [_XPAID], set())
    by = {c.candidate: c for c in comps}
    assert by["Nyxbloom Ancient"].rule_id == "x_cost_scaler"
    assert by["Nyxbloom Ancient"].cand_event == "mana_double"
    assert by["Nyxbloom Ancient"].cmdr_event == "x_cost"


def test_emitter_cost_reduce_generic_tier_fires(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    comps = _find_x_cost_scaler(x_cost_conn, [_XPAID], set())
    by = {c.candidate: c for c in comps}
    assert by["Goblin Electromancer"].cand_event == "cost_reduce_generic"


def test_emitter_excludes_self_cost_and_tribe_and_denial(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    got = {c.candidate for c in _find_x_cost_scaler(x_cost_conn, [_XPAID], set())}
    assert "Academy Journeymage" not in got  # Card.Self self-cost reducer
    assert "Wizard Lord" not in got  # tribe-narrow reducer
    assert "Contamination" not in got  # mana-denial, not a doubler
    assert "Plain Bear" not in got  # no relevant port


def test_emitter_one_complement_per_candidate(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    comps = _find_x_cost_scaler(x_cost_conn, [_XPAID], set())
    assert len(comps) == len({c.candidate for c in comps})


def test_emitter_excludes_commander_itself(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    got = {c.candidate for c in _find_x_cost_scaler(x_cost_conn, [_XPAID], {"Nyxbloom Ancient"})}
    assert "Nyxbloom Ancient" not in got


def test_emitter_non_qualifying_commander_empty(x_cost_conn):
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    no_xpaid = {"port_type": "scales_with", "event_class": "Valid", "valid_filter": "Creature.YouCtrl", "raw_line": ""}
    assert _find_x_cost_scaler(x_cost_conn, [no_xpaid], set()) == []


def test_emitter_flag_off_returns_empty(x_cost_conn, monkeypatch):
    monkeypatch.setattr(density_mod, "_ENABLE_X_COST_SCALER", False)
    from mtg_synergy_graph.complement_rules.density import _find_x_cost_scaler

    assert _find_x_cost_scaler(x_cost_conn, [_XPAID], set()) == []


def test_rule_gate_flag_aware():
    port = {"port_type": "scales_with", "event_class": "xPaid", "valid_filter": "", "raw_line": "{'Amount':'X'}"}
    density_mod._ENABLE_X_COST_SCALER = False
    assert "x_cost_scaler" not in attributable_rules_for_port(port)
    density_mod._ENABLE_X_COST_SCALER = True
    try:
        assert "x_cost_scaler" in attributable_rules_for_port(port)
    finally:
        density_mod._ENABLE_X_COST_SCALER = False
