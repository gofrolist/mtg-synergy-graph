from mtg_synergy_graph.complement_rules.density import _commander_has_x_cost_ability


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
