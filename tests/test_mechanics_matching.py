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


def test_two_step_chain_respects_filter():
    """Two-step chain filter: only candidates whose output satisfies the deck card's
    trigger filter should receive the chain bonus.

    Setup:
      - commander responds to life-gained (draws a card) — cannot be triggered by
        the candidates directly (they produce creature-enters, not life-gained)
      - deck1: creature-enters {subtype: Goblin} → gain-life  (bridges to commander)
      - cand1: creates Elf tokens  → Elf creature-enters, rejected by Goblin filter → score 0
      - cand2: creates Goblin tokens → Goblin creature-enters, passes filter → score 1.0
    """
    from mechanics_matcher import compute_deck_synergies

    cmdr_oid = "cmdr"
    all_mechanics = {
        # Commander: life-gained → draw a card (does NOT respond to creature-enters)
        "cmdr": [{"type": "triggered", "trigger_event": "life-gained",
                  "filter": None, "action": "draw-card", "detail": None,
                  "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
        # Deck bridge: Goblin enters → gain life (produces life-gained for commander)
        "deck1": [{"type": "triggered", "trigger_event": "creature-enters",
                   "filter": {"subtype": "Goblin"}, "action": "gain-life",
                   "detail": None,
                   "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
        # Negative candidate: creates Elf tokens — filter rejects, no chain
        "cand1": [{"type": "activated", "trigger_event": None,
                   "filter": None, "action": "create-token",
                   "detail": {"token": {"subtype": "Elf", "power": "1"}},
                   "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
        # Positive candidate: creates Goblin tokens — filter passes, chain forms
        "cand2": [{"type": "activated", "trigger_event": None,
                   "filter": None, "action": "create-token",
                   "detail": {"token": {"subtype": "Goblin", "power": "1"}},
                   "modifier_target": None, "modifier_how": None, "scope": None, "cost": None}],
    }
    card_types = {"cmdr": "", "deck1": "Creature", "cand1": "Creature", "cand2": "Creature"}
    scores = compute_deck_synergies(cmdr_oid, ["cand1", "cand2"], all_mechanics, card_types,
                                     deck_oids={"deck1"})
    # cand1 (Elf tokens): deck1 filter rejects Elf → no chain
    assert scores.get("cand1", 0) == 0, (
        f"Expected no chain bonus for Elf-token producer (filter rejects Elf), "
        f"got score={scores.get('cand1')}"
    )
    # cand2 (Goblin tokens): deck1 filter accepts Goblin → chain bonus awarded
    assert scores.get("cand2", 0) > 0, (
        f"Expected chain bonus for Goblin-token producer (filter accepts Goblin), "
        f"got score={scores.get('cand2')}"
    )
