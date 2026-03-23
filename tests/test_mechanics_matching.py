from mechanics_matcher import card_produces_events, filter_matches


def test_card_produces_events_includes_keywords():
    """Tokens created with keywords should populate has_keyword in output."""
    mechs = [{
        "type": "triggered", "trigger_event": "creature-enters",
        "filter": None, "action": "create-token",
        "detail": {"token": {"subtype": "Spirit", "power": "1", "keywords": ["flying"]}},
        "modifier_target": None, "modifier_how": None, "scope": None, "cost": None,
    }]
    events = card_produces_events(mechs)
    token_events = [e for e in events if e["event"] == "creature-enters"]
    assert len(token_events) > 0
    assert "has_keyword" in token_events[0]["output"]
    assert "flying" in token_events[0]["output"]["has_keyword"]


def test_filter_matches_keyword_with_populated_output():
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you", "has_keyword": ["flying"]}
    assert filter_matches(trigger_filter, producer_output) is True


def test_filter_matches_keyword_mismatch():
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you", "has_keyword": ["haste"]}
    assert filter_matches(trigger_filter, producer_output) is False


def test_filter_matches_keyword_missing():
    trigger_filter = {"has_keyword": "flying"}
    producer_output = {"controller": "you"}
    assert filter_matches(trigger_filter, producer_output) is False


def test_filter_is_equipped_passes_when_set():
    trigger_filter = {"is_equipped": True}
    producer_output = {"controller": "you", "is_equipped": True}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_is_equipped_fails_when_not_set():
    trigger_filter = {"is_equipped": True}
    producer_output = {"controller": "you"}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_counter_type_matches():
    trigger_filter = {"counter_type": "+1/+1"}
    producer_output = {"controller": "you", "counter_type": "+1/+1"}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_counter_type_mismatches():
    trigger_filter = {"counter_type": "+1/+1"}
    producer_output = {"controller": "you", "counter_type": "charge"}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_power_threshold():
    trigger_filter = {"power": ">=3"}
    producer_output = {"controller": "you", "power": 4}
    assert filter_matches(trigger_filter, producer_output) is True

def test_filter_power_below_threshold():
    trigger_filter = {"power": ">=3"}
    producer_output = {"controller": "you", "power": 2}
    assert filter_matches(trigger_filter, producer_output) is False

def test_filter_power_star_rejects():
    """Non-numeric power like * should reject the filter."""
    trigger_filter = {"power": ">=3"}
    producer_output = {"controller": "you", "power": "*"}
    assert filter_matches(trigger_filter, producer_output) is False
