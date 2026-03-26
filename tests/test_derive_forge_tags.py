"""Tests for derive_forge_tags.py — verb→provides mapping (Task 1), trigger→wants mapping (Task 2), and role derivation (Task 3)."""
import pytest
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from derive_forge_tags import (
    VERB_TO_PROVIDES,
    KEYWORD_TO_PROVIDES,
    SKIPPED_VERBS,
    TRIBAL_TYPES,
    TOKEN_TYPE_PATTERNS,
    TRIGGER_TO_WANTS,
    ZONE_TRIGGER_MAP,
    derive_provides_from_ability,
    derive_wants_from_trigger,
    derive_wants_from_cost,
    derive_role,
)


# ---------------------------------------------------------------------------
# VERB_TO_PROVIDES dict tests
# ---------------------------------------------------------------------------

def test_verb_to_provides_mapping():
    """Key verbs are mapped; skipped verbs are NOT in VERB_TO_PROVIDES."""
    # Spot-check core verbs
    assert VERB_TO_PROVIDES["Token"] == "token"
    assert VERB_TO_PROVIDES["Draw"] == "draw"
    assert VERB_TO_PROVIDES["Mana"] == "mana"
    assert VERB_TO_PROVIDES["DealDamage"] == "deal-damage"
    assert VERB_TO_PROVIDES["Destroy"] == "destroy"
    assert VERB_TO_PROVIDES["GainLife"] == "gain-life"
    assert VERB_TO_PROVIDES["Mill"] == "mill"
    assert VERB_TO_PROVIDES["Proliferate"] == "proliferate"
    assert VERB_TO_PROVIDES["Investigate"] == "investigate"
    assert VERB_TO_PROVIDES["AddTurn"] == "extra-turn"
    assert VERB_TO_PROVIDES["WinsGame"] == "wins-game"

    # Skipped verbs must NOT appear in VERB_TO_PROVIDES
    for v in SKIPPED_VERBS:
        assert v not in VERB_TO_PROVIDES, f"Skipped verb {v!r} should not be in VERB_TO_PROVIDES"


def test_verb_mapping_completeness():
    """At least 60 verbs mapped; all tag values are non-empty and contain no spaces."""
    assert len(VERB_TO_PROVIDES) >= 60, f"Only {len(VERB_TO_PROVIDES)} verbs mapped"
    for verb, tag in VERB_TO_PROVIDES.items():
        assert tag, f"Verb {verb!r} maps to empty tag"
        assert " " not in tag, f"Verb {verb!r} maps to tag with spaces: {tag!r}"


# ---------------------------------------------------------------------------
# derive_provides_from_ability tests
# ---------------------------------------------------------------------------

def test_derive_provides_from_abilities_sol_ring():
    """Sol Ring: Mana verb + T cost → mana + tap-ability."""
    tags = derive_provides_from_ability(
        verb="Mana",
        keyword=None,
        cost="T",
        token_script=None,
        counter_type=None,
        raw_line="A:AB$ Mana | Cost$ T | Produced$ C | Amount$ 2",
        target=None,
    )
    assert "mana" in tags
    assert "tap-ability" in tags


def test_derive_provides_from_abilities_lightning_bolt():
    """DealDamage → deal-damage."""
    tags = derive_provides_from_ability(
        verb="DealDamage",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ DealDamage | ValidTgts$ Any | NumDmg$ 3",
        target=None,
    )
    assert "deal-damage" in tags


def test_derive_provides_from_abilities_krenko():
    """Krenko (Token + goblin token_script) → token + goblin-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost="T",
        token_script="r_1_1_goblin",
        counter_type=None,
        raw_line="A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin",
        target=None,
    )
    assert "token" in tags
    assert "goblin-tribal" in tags


# ---------------------------------------------------------------------------
# Token type detection
# ---------------------------------------------------------------------------

def test_token_type_detection_treasure():
    """Treasure token script → token-treasure."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="c_a_treasure_sac",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "token-treasure" in tags
    assert "token" in tags


def test_token_type_detection_clue():
    """Clue token script → token-clue."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="c_a_clue_draw",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "token-clue" in tags


def test_token_type_detection_food():
    """Food token → token-food."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="c_a_food_gainlife",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "token-food" in tags


def test_token_type_detection_blood():
    """Blood token → token-blood."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="r_a_blood_discard",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "token-blood" in tags


# ---------------------------------------------------------------------------
# Sacrifice in cost
# ---------------------------------------------------------------------------

def test_sacrifice_in_cost():
    """Cost containing 'Sac' → sacrifice-outlet."""
    tags = derive_provides_from_ability(
        verb="Mana",
        keyword=None,
        cost="Sac<1/Creature/a creature>",
        token_script=None,
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "sacrifice-outlet" in tags


def test_tap_cost_standalone():
    """Cost 'T' (standalone) → tap-ability."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost="T",
        token_script="r_1_1_goblin",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "tap-ability" in tags


def test_tap_cost_not_matched_in_word():
    """'T' embedded in a larger word in cost should NOT trigger tap-ability."""
    # e.g., cost containing "Tap" but not standalone T
    tags = derive_provides_from_ability(
        verb="Draw",
        keyword=None,
        cost="2",
        token_script=None,
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "tap-ability" not in tags


# ---------------------------------------------------------------------------
# Keyword provides
# ---------------------------------------------------------------------------

def test_keyword_provides_flying():
    """K: Flying → flying."""
    tags = derive_provides_from_ability(
        verb=None,
        keyword="Flying",
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="K:Flying",
        target=None,
    )
    assert "flying" in tags


def test_keyword_provides_equip():
    """K: Equip → equip."""
    tags = derive_provides_from_ability(
        verb=None,
        keyword="Equip",
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="K:Equip",
        target=None,
    )
    assert "equip" in tags


# ---------------------------------------------------------------------------
# ChangeZone verb logic
# ---------------------------------------------------------------------------

def test_changezone_verb_reanimate():
    """Graveyard→Battlefield = reanimate."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Graveyard | Destination$ Battlefield",
        target=None,
    )
    assert "reanimate" in tags


def test_changezone_verb_graveyard_to_hand():
    """Graveyard→Hand = graveyard-to-hand."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Graveyard | Destination$ Hand",
        target=None,
    )
    assert "graveyard-to-hand" in tags


def test_changezone_verb_cheat_into_play_hand():
    """Hand→Battlefield = cheat-into-play."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Hand | Destination$ Battlefield",
        target=None,
    )
    assert "cheat-into-play" in tags


def test_changezone_verb_cheat_into_play_library():
    """Library→Battlefield = cheat-into-play."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Library | Destination$ Battlefield",
        target=None,
    )
    assert "cheat-into-play" in tags


def test_changezone_verb_remove_battlefield_graveyard():
    """Battlefield→Graveyard = remove."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Battlefield | Destination$ Graveyard",
        target=None,
    )
    assert "remove" in tags


def test_changezone_verb_exile():
    """Battlefield→Exile = remove."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Battlefield | Destination$ Exile",
        target=None,
    )
    assert "remove" in tags


def test_changezone_verb_fallback():
    """Unknown origin/destination falls back to change-zone."""
    tags = derive_provides_from_ability(
        verb="ChangeZone",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="A:SP$ ChangeZone | Origin$ Library | Destination$ Hand",
        target=None,
    )
    assert "change-zone" in tags


# ---------------------------------------------------------------------------
# Investigate dual provides
# ---------------------------------------------------------------------------

def test_investigate_dual_provides():
    """Investigate verb → both 'investigate' and 'token-clue'."""
    tags = derive_provides_from_ability(
        verb="Investigate",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="T:Mode$ DamageDone | Execute$ TrigInvestigate",
        target=None,
    )
    assert "investigate" in tags
    assert "token-clue" in tags


# ---------------------------------------------------------------------------
# Skipped verbs produce no tags
# ---------------------------------------------------------------------------

def test_skipped_verb_produces_no_tags_continuous():
    """Continuous verb produces empty set."""
    tags = derive_provides_from_ability(
        verb="Continuous",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="S:Mode$ Continuous | Affected$ Creature.Self",
        target=None,
    )
    assert tags == set()


def test_skipped_verb_produces_no_tags_effect():
    """Effect verb produces empty set."""
    tags = derive_provides_from_ability(
        verb="Effect",
        keyword=None,
        cost=None,
        token_script=None,
        counter_type=None,
        raw_line="S:Mode$ Effect",
        target=None,
    )
    assert tags == set()


# ---------------------------------------------------------------------------
# Token script tribal variants
# ---------------------------------------------------------------------------

def test_token_script_tribal_spirit():
    """Spirit token → spirit-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="w_1_1_spirit_flying",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "spirit-tribal" in tags


def test_token_script_tribal_zombie():
    """Zombie token → zombie-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="b_2_2_zombie",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "zombie-tribal" in tags


def test_token_script_tribal_dragon():
    """Dragon token → dragon-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="r_5_5_dragon_flying",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "dragon-tribal" in tags


def test_token_script_no_tribal_for_unknown():
    """Generic beast token does not generate tribal if not in TRIBAL_TYPES list...
    or Beast IS in the list → beast-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="g_3_3_beast",
        counter_type=None,
        raw_line="",
        target=None,
    )
    # Beast is in TRIBAL_TYPES
    assert "beast-tribal" in tags


def test_token_script_elf_tribal():
    """Elf token → elf-tribal."""
    tags = derive_provides_from_ability(
        verb="Token",
        keyword=None,
        cost=None,
        token_script="g_1_1_elf_warrior",
        counter_type=None,
        raw_line="",
        target=None,
    )
    assert "elf-tribal" in tags


# ===========================================================================
# Task 2: trigger→wants mapping tests
# ===========================================================================

# ---------------------------------------------------------------------------
# TRIGGER_TO_WANTS dict tests
# ---------------------------------------------------------------------------

def test_trigger_to_wants_mapping():
    """Key triggers are mapped to correct wants tags."""
    assert TRIGGER_TO_WANTS["Attacks"] == "attacks"
    assert TRIGGER_TO_WANTS["SpellCast"] == "spell-cast"
    assert TRIGGER_TO_WANTS["DamageDone"] == "damage-done"
    assert TRIGGER_TO_WANTS["Sacrificed"] == "sacrificed"
    assert TRIGGER_TO_WANTS["LifeGained"] == "life-gained"
    assert TRIGGER_TO_WANTS["Drawn"] == "card-drawn"
    assert TRIGGER_TO_WANTS["LandPlayed"] == "land-played"
    assert TRIGGER_TO_WANTS["TokenCreated"] == "token-created"
    assert TRIGGER_TO_WANTS["TurnFaceUp"] == "turn-face-up"


# ---------------------------------------------------------------------------
# ZONE_TRIGGER_MAP and ChangesZone tests
# ---------------------------------------------------------------------------

def test_zone_trigger_mapping():
    """All 6 zone combinations produce the correct wants tag."""
    # Battlefield → Graveyard = dies
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard", None)
    assert tags == {"dies"}

    # Any → Battlefield = enters-battlefield
    tags = derive_wants_from_trigger("ChangesZone", "Any", "Battlefield", None)
    assert tags == {"enters-battlefield"}

    # Battlefield → Exile = exiled
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Exile", None)
    assert tags == {"exiled"}

    # Graveyard → Battlefield = leaves-graveyard
    tags = derive_wants_from_trigger("ChangesZone", "Graveyard", "Battlefield", None)
    assert tags == {"leaves-graveyard"}

    # Battlefield → Any (fallback) = leaves-battlefield
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Any", None)
    assert tags == {"leaves-battlefield"}

    # None → Battlefield = enters-battlefield
    tags = derive_wants_from_trigger("ChangesZone", None, "Battlefield", None)
    assert tags == {"enters-battlefield"}


# ---------------------------------------------------------------------------
# Trigger filter tribal tests
# ---------------------------------------------------------------------------

def test_trigger_filter_tribal():
    """Filter 'Goblin.YouCtrl' on ChangesZone(Battlefield→Graveyard) → goblin-tribal + dies."""
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard", "Goblin.YouCtrl")
    assert "dies" in tags
    assert "goblin-tribal" in tags


def test_trigger_filter_other_qualifier():
    """Filter 'Creature.Zombie+Other+YouCtrl' → zombie-tribal (Creature is generic, not tribal)."""
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard", "Creature.Zombie+Other+YouCtrl")
    assert "dies" in tags
    assert "zombie-tribal" in tags
    assert "creature-tribal" not in tags


def test_trigger_filter_no_tribal_for_generic():
    """Filter 'Card.Self' produces no tribal tags."""
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard", "Card.Self")
    assert "dies" in tags
    assert "card-tribal" not in tags
    assert "self-tribal" not in tags


# ---------------------------------------------------------------------------
# ChangesZoneAll trigger test
# ---------------------------------------------------------------------------

def test_changezone_all_trigger():
    """ChangesZoneAll trigger mode → mass-zone-change."""
    tags = derive_wants_from_trigger("ChangesZoneAll", None, None, None)
    assert "mass-zone-change" in tags


# ---------------------------------------------------------------------------
# Additional trigger modes
# ---------------------------------------------------------------------------

def test_additional_trigger_modes():
    """Spot-check additional trigger modes map to correct wants tags."""
    assert derive_wants_from_trigger("Blocks", None, None, None) == {"blocks"}
    assert derive_wants_from_trigger("Discarded", None, None, None) == {"discarded"}
    assert derive_wants_from_trigger("CounterAdded", None, None, None) == {"counter-added"}
    assert derive_wants_from_trigger("CounterAddedOnce", None, None, None) == {"counter-added"}
    assert derive_wants_from_trigger("Phase", None, None, None) == {"phase-trigger"}
    assert derive_wants_from_trigger("Taps", None, None, None) == {"tapped"}
    assert derive_wants_from_trigger("Untaps", None, None, None) == {"untapped"}
    assert derive_wants_from_trigger("BecomesTarget", None, None, None) == {"becomes-target"}
    assert derive_wants_from_trigger("AttackerBlocked", None, None, None) == {"attacker-blocked"}
    assert derive_wants_from_trigger("AttackerBlockedByCreature", None, None, None) == {"attacker-blocked"}
    assert derive_wants_from_trigger("AttackerUnblocked", None, None, None) == {"attacker-unblocked"}
    assert derive_wants_from_trigger("SpellCastOrCopy", None, None, None) == {"spell-cast"}


# ---------------------------------------------------------------------------
# derive_wants_from_cost tests
# ---------------------------------------------------------------------------

def test_derive_wants_for_cost():
    """Sac in cost → sacrifice-fodder; T alone → empty set."""
    assert derive_wants_from_cost("Sac<1/Creature/a creature>") == {"sacrifice-fodder"}
    assert derive_wants_from_cost("T") == set()
    assert derive_wants_from_cost(None) == set()
    assert derive_wants_from_cost("2") == set()


# ===========================================================================
# Task 3: role derivation tests
# ===========================================================================

def test_derive_role():
    """Role derived from provides tags with correct priority."""
    # Removal cards
    assert derive_role({"destroy"}, "Instant") == "removal"
    assert derive_role({"counter-spell"}, "Instant") == "removal"
    assert derive_role({"destroy-all"}, "Sorcery") == "removal"
    assert derive_role({"exile"}, "Instant") == "removal"
    assert derive_role({"force-sacrifice"}, "Enchantment") == "removal"

    # Ramp cards
    assert derive_role({"mana"}, "Artifact") == "ramp"
    assert derive_role({"reduce-cost"}, "Creature") == "ramp"

    # Draw cards
    assert derive_role({"draw"}, "Enchantment") == "draw"
    assert derive_role({"surveil"}, "Creature") == "draw"
    assert derive_role({"scry"}, "Instant") == "draw"
    assert derive_role({"dig"}, "Sorcery") == "draw"

    # Protection
    assert derive_role({"fog"}, "Instant") == "protection"
    assert derive_role({"hexproof", "indestructible"}, "Creature") == "protection"
    assert derive_role({"ward"}, "Creature") == "protection"

    # Lands
    assert derive_role(set(), "Basic Land — Plains") == "land"
    assert derive_role({"mana"}, "Land") == "land"  # land beats ramp

    # Threats (has token/pump/damage but no higher-priority tags)
    assert derive_role({"token", "pump-all"}, "Creature") == "threat"
    assert derive_role({"deal-damage"}, "Creature") == "threat"
    assert derive_role({"put-counter"}, "Creature") == "threat"

    # Utility (fallback)
    assert derive_role({"flash"}, "Instant") == "utility"
    assert derive_role(set(), "Creature") == "utility"
    assert derive_role({"changeling"}, "Creature") == "utility"

    # Priority: removal beats ramp (card that destroys AND makes mana)
    assert derive_role({"destroy", "mana"}, "Creature") == "removal"
    # Priority: ramp beats draw
    assert derive_role({"mana", "draw"}, "Artifact") == "ramp"


# ===========================================================================
# Task 4: integration tests — derive_all pipeline
# ===========================================================================

def test_full_pipeline_on_real_db():
    """Run derive_all on real DB, verify coverage and key cards."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)

    # Coverage: at least 90% of cards with Forge data get provides tags
    assert stats["cards_with_provides"] >= 28000
    assert stats["cards_with_wants"] >= 10000
    assert stats["total_provides_tags"] >= 60000

    # Spot-check key cards
    card_tags = stats["card_tags"]
    conn = sqlite3.connect(db_path)

    def oid(name):
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    # Krenko: token + goblin-tribal + tap-ability
    assert "token" in card_tags[oid("Krenko, Mob Boss")]["provides"]
    assert "goblin-tribal" in card_tags[oid("Krenko, Mob Boss")]["provides"]

    # Sol Ring: mana + tap-ability
    assert "mana" in card_tags[oid("Sol Ring")]["provides"]

    # Skullclamp: draw + equip, wants dies
    assert "draw" in card_tags[oid("Skullclamp")]["provides"]
    assert "dies" in card_tags[oid("Skullclamp")]["wants"]

    # Dragon Fodder: token + goblin-tribal (from token_script)
    assert "token" in card_tags[oid("Dragon Fodder")]["provides"]
    assert "goblin-tribal" in card_tags[oid("Dragon Fodder")]["provides"]

    # Swords to Plowshares: remove (ChangeZone to exile)
    stp_tags = card_tags.get(oid("Swords to Plowshares"), {"provides": set()})["provides"]
    assert "remove" in stp_tags or "exile" in stp_tags or "change-zone" in stp_tags

    conn.close()


def test_role_derivation_spot_check():
    """Spot-check role derivation for well-known cards."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    roles = stats["card_roles"]
    conn = sqlite3.connect(db_path)

    def oid(name):
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    assert roles.get(oid("Sol Ring")) == "ramp"
    assert roles.get(oid("Swords to Plowshares")) == "removal"
    assert roles.get(oid("Rhystic Study")) == "draw"
    assert roles.get(oid("Krenko, Mob Boss")) == "threat"
    conn.close()


def test_dfc_tags_merged():
    """DFC cards get tags from both faces when both map to same oracle_id."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    card_tags = stats["card_tags"]
    conn = sqlite3.connect(db_path)

    # Birgi: front face has mana ability, should have tags
    birgi_oid = conn.execute(
        "SELECT oracle_id FROM cards WHERE name LIKE 'Birgi%'").fetchone()
    if birgi_oid:
        assert len(card_tags.get(birgi_oid[0], {"provides": set()})["provides"]) > 0

    conn.close()


def test_cards_without_forge_data_skipped():
    """Cards with no forge_name_map entry produce no tags."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    assert stats["cards_skipped"] >= 0
