import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# --- Phase 1 tests: keyword extraction ---

def test_keyword_extraction_simple():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-001",
        "name": "Serra Angel",
        "oracle_text": "Flying\nVigilance",
        "keywords": ["flying", "vigilance"]
    }
    abilities = parse_card(card)
    keyword_abilities = [a for a in abilities if a["ability_type"] == "keyword"]
    assert len(keyword_abilities) == 2
    kw_names = {a["effect"] for a in keyword_abilities}
    assert "flying" in kw_names
    assert "vigilance" in kw_names


def test_keyword_extraction_with_other_text():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-002",
        "name": "Baneslayer Angel",
        "oracle_text": "Flying, first strike, lifelink\nProtection from Demons and from Dragons",
        "keywords": ["flying", "first strike", "lifelink", "protection"]
    }
    abilities = parse_card(card)
    keywords = [a for a in abilities if a["ability_type"] == "keyword"]
    assert len(keywords) >= 3  # flying, first strike, lifelink at minimum


def test_double_faced_card_split():
    from ability_parser import parse_card
    card = {
        "oracle_id": "delver-001",
        "name": "Delver of Secrets // Insectile Aberration",
        "oracle_text": "At the beginning of your upkeep, look at the top card of your library. You may reveal that card. If an instant or sorcery card is revealed this way, transform Delver of Secrets. // Flying",
        "keywords": ["flying", "transform"]
    }
    abilities = parse_card(card)
    # Should have abilities from both faces
    assert len(abilities) >= 2
    # Back face should have flying keyword
    keywords = [a for a in abilities if a["ability_type"] == "keyword"]
    assert any(a["effect"] == "flying" for a in keywords)


# --- Phase 2 tests: pattern matching ---

def test_triggered_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "cathars-001",
        "name": "Cathars' Crusade",
        "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert "creature enters" in triggered[0]["trigger_condition"].lower()
    assert "+1/+1 counter" in triggered[0]["effect"]


def test_activated_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "gavony-001",
        "name": "Gavony Township",
        "oracle_text": "{T}: Add {C}.\n{2}{G}{W}, {T}: Put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated_or_mana = [a for a in abilities if a["ability_type"] in ("activated", "mana")]
    mana = [a for a in abilities if a["is_mana_ability"]]
    assert len(activated_or_mana) >= 2  # Both are activated/mana type
    assert len(mana) == 1  # Only the first is a mana ability
    assert "Add" in mana[0]["effect"]


def test_replacement_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "hardened-001",
        "name": "Hardened Scales",
        "oracle_text": "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
        "keywords": []
    }
    abilities = parse_card(card)
    replacements = [a for a in abilities if a["ability_type"] == "replacement"]
    assert len(replacements) == 1


def test_static_ability():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-static",
        "name": "Glorious Anthem",
        "oracle_text": "Creatures you control get +1/+1.",
        "keywords": []
    }
    abilities = parse_card(card)
    statics = [a for a in abilities if a["ability_type"] == "static"]
    assert len(statics) == 1


def test_sacrifice_activated():
    from ability_parser import parse_card
    card = {
        "oracle_id": "skirk-001",
        "name": "Skirk Prospector",
        "oracle_text": "Sacrifice a Goblin: Add {R}.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] in ("activated", "mana")]
    mana = [a for a in abilities if a["is_mana_ability"]]
    assert len(activated) == 1
    assert "Sacrifice" in activated[0]["cost"]
    assert len(mana) == 1


def test_planeswalker_abilities():
    from ability_parser import parse_card
    card = {
        "oracle_id": "jace-001",
        "name": "Jace, the Mind Sculptor",
        "oracle_text": "+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n\u22121: Return target creature to its owner's hand.\n\u221212: Exile all cards from target player's library, then that player shuffles their hand into their library.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] == "activated"]
    assert len(activated) == 4
    # Check loyalty costs are captured
    costs = [a["cost"] for a in activated]
    assert any("+2" in c for c in costs)
    assert any("\u221212" in c or "-12" in c for c in costs)


def test_saga_abilities():
    from ability_parser import parse_card
    card = {
        "oracle_id": "binding-001",
        "name": "Binding the Old Gods",
        "oracle_text": "I \u2014 Destroy target nonland permanent an opponent controls.\nII \u2014 Search your library for a Forest card, put it onto the battlefield tapped, then shuffle.\nIII \u2014 Exile this Saga, then return it to the battlefield transformed.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 3


def test_triggered_with_if_clause():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-if",
        "name": "Test Card",
        "oracle_text": "Whenever a creature enters the battlefield under your control, if it's a Human, put a +1/+1 counter on it.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert "creature enters" in triggered[0]["trigger_condition"].lower()


def test_adventure_card():
    from ability_parser import parse_card
    card = {
        "oracle_id": "bonecrusher-001",
        "name": "Bonecrusher Giant // Stomp",
        "oracle_text": "Whenever Bonecrusher Giant becomes the target of a spell, Bonecrusher Giant deals 2 damage to that spell's controller. // Damage can't be prevented this turn. Stomp deals 2 damage to any target.",
        "keywords": ["adventure"]
    }
    abilities = parse_card(card)
    # Front face: triggered ability
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) >= 1
    # Back face (adventure): static + effect
    assert len(abilities) >= 2  # at least triggered + adventure parts


# --- Phase 3 tests: effect tagging ---

def test_effect_tagging_token_generation():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-token",
        "name": "Krenko, Mob Boss",
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    tagged = [a for a in abilities if a.get("effect_tags")]
    assert len(tagged) >= 1
    assert "token-generation" in tagged[0]["effect_tags"]


def test_effect_tagging_card_draw():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-draw",
        "name": "Harmonize",
        "oracle_text": "Draw three cards.",
        "keywords": []
    }
    abilities = parse_card(card)
    tagged = [a for a in abilities if a.get("effect_tags")]
    assert any("card-draw" in a["effect_tags"] for a in tagged)


def test_trigger_tagging_creature_etb():
    from ability_parser import parse_card
    card = {
        "oracle_id": "cathars-001",
        "name": "Cathars' Crusade",
        "oracle_text": "Whenever a creature enters the battlefield under your control, put a +1/+1 counter on each creature you control.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert len(triggered) == 1
    assert triggered[0]["trigger_tags"] is not None
    assert "creature-etb" in triggered[0]["trigger_tags"]
    assert "counter-placement" in triggered[0]["effect_tags"]


def test_trigger_tagging_creature_death():
    from ability_parser import parse_card
    card = {
        "oracle_id": "test-death",
        "name": "Blood Artist",
        "oracle_text": "Whenever a creature dies, target opponent loses 1 life and you gain 1 life.",
        "keywords": []
    }
    abilities = parse_card(card)
    triggered = [a for a in abilities if a["ability_type"] == "triggered"]
    assert triggered[0]["trigger_tags"] is not None
    assert "creature-death" in triggered[0]["trigger_tags"]
    assert "life-drain" in triggered[0]["effect_tags"]


def test_sacrifice_outlet_tagging():
    from ability_parser import parse_card
    card = {
        "oracle_id": "skirk-001",
        "name": "Skirk Prospector",
        "oracle_text": "Sacrifice a Goblin: Add {R}.",
        "keywords": []
    }
    abilities = parse_card(card)
    activated = [a for a in abilities if a["ability_type"] in ("activated", "mana")]
    assert any("sacrifice-outlet" in (a.get("effect_tags") or []) for a in activated)
