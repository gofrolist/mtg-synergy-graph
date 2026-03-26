# Forge-Derived Tags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LLM-generated provides/wants tags with deterministic tags derived from Forge ability data.

**Architecture:** New `derive_forge_tags.py` script reads `forge_abilities` + `forge_name_map` tables, applies ~90 verb→provides rules and ~35 trigger→wants rules, and repopulates the existing `provides`/`wants` tables. Also derives `cards.role`. Consumers (graph builder, scoring, swaps) keep reading the same tables but get better data.

**Tech Stack:** Python 3, SQLite, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-26-forge-derived-tags-design.md`

---

### Task 1: Core derivation engine — verb→provides mapping

**Files:**
- Create: `derive_forge_tags.py`
- Create: `tests/test_derive_forge_tags.py`

This task builds the core script with verb→provides rules. The script reads `forge_abilities` joined with `forge_name_map` to get oracle_ids, applies verb mappings, and writes to the `provides` table.

- [ ] **Step 1: Write tests for verb→provides mapping**

```python
# tests/test_derive_forge_tags.py
"""Tests for derive_forge_tags.py — Forge ability → provides/wants tag derivation."""
import pytest


def test_verb_to_provides_mapping():
    """Core verb mapping: each Forge verb maps to exactly one provides tag."""
    from derive_forge_tags import VERB_TO_PROVIDES
    # Key verbs must be mapped
    assert VERB_TO_PROVIDES["Token"] == "token"
    assert VERB_TO_PROVIDES["Draw"] == "draw"
    assert VERB_TO_PROVIDES["Mana"] == "mana"
    assert VERB_TO_PROVIDES["DealDamage"] == "deal-damage"
    assert VERB_TO_PROVIDES["DamageAll"] == "deal-damage-all"
    assert VERB_TO_PROVIDES["Destroy"] == "destroy"
    assert VERB_TO_PROVIDES["DestroyAll"] == "destroy-all"
    assert VERB_TO_PROVIDES["PutCounter"] == "put-counter"
    assert VERB_TO_PROVIDES["GainLife"] == "gain-life"
    assert VERB_TO_PROVIDES["LoseLife"] == "lose-life"
    assert VERB_TO_PROVIDES["Pump"] == "pump"
    assert VERB_TO_PROVIDES["PumpAll"] == "pump-all"
    assert VERB_TO_PROVIDES["Mill"] == "mill"
    assert VERB_TO_PROVIDES["Scry"] == "scry"
    assert VERB_TO_PROVIDES["Surveil"] == "surveil"
    assert VERB_TO_PROVIDES["Dig"] == "dig"
    assert VERB_TO_PROVIDES["Counter"] == "counter-spell"
    assert VERB_TO_PROVIDES["Sacrifice"] == "force-sacrifice"
    assert VERB_TO_PROVIDES["Proliferate"] == "proliferate"
    assert VERB_TO_PROVIDES["Connive"] == "connive"
    assert VERB_TO_PROVIDES["Poison"] == "poison"
    assert VERB_TO_PROVIDES["Detain"] == "detain"
    assert VERB_TO_PROVIDES["AddTurn"] == "extra-turn"
    assert VERB_TO_PROVIDES["Play"] == "free-cast"
    assert VERB_TO_PROVIDES["CantBlockBy"] == "evasion-grant"
    assert VERB_TO_PROVIDES["CantAttack"] == "restrict-attack"
    assert VERB_TO_PROVIDES["RaiseCost"] == "raise-cost"
    # Skipped verbs must NOT be in the dict
    for skip in ["Continuous", "Effect", "Charm", "Cleanup", "StoreSVar",
                 "DelayedTrigger", "RepeatEach", "ChooseCard", "Branch"]:
        assert skip not in VERB_TO_PROVIDES, f"{skip} should be skipped"


def test_verb_mapping_completeness():
    """Every mapped verb produces a non-empty string tag."""
    from derive_forge_tags import VERB_TO_PROVIDES
    assert len(VERB_TO_PROVIDES) >= 60  # spec has ~90 mapped verbs
    for verb, tag in VERB_TO_PROVIDES.items():
        assert isinstance(tag, str) and len(tag) > 0, f"Bad tag for {verb}"
        assert " " not in tag, f"Tag for {verb} contains spaces: '{tag}'"


def test_derive_provides_from_abilities():
    """Given a list of ability rows, derive correct provides tags."""
    from derive_forge_tags import derive_provides_from_ability
    # Sol Ring: verb=Mana
    tags = derive_provides_from_ability(
        verb="Mana", keyword=None, cost="T", token_script=None,
        counter_type=None, raw_line="A:AB$ Mana | Cost$ T | Produced$ C | Amount$ 2",
        target=None)
    assert "mana" in tags
    assert "tap-ability" in tags  # T in cost

    # Lightning Bolt: verb=DealDamage
    tags = derive_provides_from_ability(
        verb="DealDamage", keyword=None, cost=None, token_script=None,
        counter_type=None, raw_line="", target="Any")
    assert "deal-damage" in tags

    # Krenko: verb=Token, token_script has goblin
    tags = derive_provides_from_ability(
        verb="Token", keyword=None, cost="T", token_script="r_1_1_goblin",
        counter_type=None, raw_line="", target=None)
    assert "token" in tags
    assert "goblin-tribal" in tags
    assert "tap-ability" in tags


def test_token_type_detection():
    """Token script parsing detects treasure, clue, food, blood tokens."""
    from derive_forge_tags import derive_provides_from_ability
    # Treasure token
    tags = derive_provides_from_ability(
        verb="Token", keyword=None, cost=None, token_script="c_treasure",
        counter_type=None, raw_line="", target=None)
    assert "token-treasure" in tags
    assert "token" in tags

    # Clue token
    tags = derive_provides_from_ability(
        verb="Token", keyword=None, cost=None, token_script="c_clue_draw",
        counter_type=None, raw_line="", target=None)
    assert "token-clue" in tags


def test_sacrifice_in_cost():
    """Sac in cost string → sacrifice-outlet provides tag."""
    from derive_forge_tags import derive_provides_from_ability
    # Ashnod's Altar: verb=Mana, cost includes Sac
    tags = derive_provides_from_ability(
        verb="Mana", keyword=None, cost="Sac<1/Creature.YouCtrl>",
        token_script=None, counter_type=None, raw_line="", target=None)
    assert "sacrifice-outlet" in tags
    assert "mana" in tags


def test_keyword_provides():
    """Keywords map to provides tags."""
    from derive_forge_tags import derive_provides_from_ability
    tags = derive_provides_from_ability(
        verb=None, keyword="Flying", cost=None, token_script=None,
        counter_type=None, raw_line="", target=None)
    assert "flying" in tags

    tags = derive_provides_from_ability(
        verb=None, keyword="Equip", cost=None, token_script=None,
        counter_type=None, raw_line="", target=None)
    assert "equip" in tags


def test_changezone_verb_provides():
    """ChangeZone verb maps to zone-dependent provides tags via raw_line parsing."""
    from derive_forge_tags import derive_provides_from_ability
    # Reanimate: Graveyard → Battlefield
    tags = derive_provides_from_ability(
        verb="ChangeZone", keyword=None, cost=None, token_script=None,
        counter_type=None, target="Creature",
        raw_line="SP$ ChangeZone | Origin$ Graveyard | Destination$ Battlefield")
    assert "reanimate" in tags

    # Graveyard → Hand
    tags = derive_provides_from_ability(
        verb="ChangeZone", keyword=None, cost=None, token_script=None,
        counter_type=None, target=None,
        raw_line="SP$ ChangeZone | Origin$ Graveyard | Destination$ Hand")
    assert "graveyard-to-hand" in tags

    # Cheat into play: Hand/Library → Battlefield
    tags = derive_provides_from_ability(
        verb="ChangeZone", keyword=None, cost=None, token_script=None,
        counter_type=None, target=None,
        raw_line="SP$ ChangeZone | Origin$ Hand | Destination$ Battlefield")
    assert "cheat-into-play" in tags

    # Exile removal
    tags = derive_provides_from_ability(
        verb="ChangeZone", keyword=None, cost=None, token_script=None,
        counter_type=None, target="Creature",
        raw_line="SP$ ChangeZone | Origin$ Battlefield | Destination$ Exile")
    assert "remove" in tags

    # Generic fallback
    tags = derive_provides_from_ability(
        verb="ChangeZone", keyword=None, cost=None, token_script=None,
        counter_type=None, target=None,
        raw_line="SP$ ChangeZone | Origin$ Library | Destination$ Hand")
    assert "change-zone" in tags


def test_investigate_dual_provides():
    """Investigate provides both investigate AND token-clue."""
    from derive_forge_tags import derive_provides_from_ability
    tags = derive_provides_from_ability(
        verb="Investigate", keyword=None, cost=None, token_script=None,
        counter_type=None, raw_line="", target=None)
    assert "investigate" in tags
    assert "token-clue" in tags


def test_skipped_verb_produces_no_tags():
    """Skipped verbs produce no verb-based tags (only cost-based if applicable)."""
    from derive_forge_tags import derive_provides_from_ability
    tags = derive_provides_from_ability(
        verb="Continuous", keyword=None, cost=None, token_script=None,
        counter_type=None, raw_line="", target=None)
    assert len(tags) == 0

    tags = derive_provides_from_ability(
        verb="Effect", keyword=None, cost=None, token_script=None,
        counter_type=None, raw_line="", target=None)
    assert len(tags) == 0


def test_token_script_tribal_variants():
    """Token script parsing handles various creature type patterns."""
    from derive_forge_tags import derive_provides_from_ability
    # Spirit token
    tags = derive_provides_from_ability(
        verb="Token", keyword=None, cost=None, token_script="w_1_1_spirit",
        counter_type=None, raw_line="", target=None)
    assert "spirit-tribal" in tags
    assert "token" in tags

    # Zombie token
    tags = derive_provides_from_ability(
        verb="Token", keyword=None, cost=None, token_script="b_2_2_zombie",
        counter_type=None, raw_line="", target=None)
    assert "zombie-tribal" in tags
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'derive_forge_tags'`

- [ ] **Step 3: Implement derive_forge_tags.py — verb mapping + provides derivation**

Create `derive_forge_tags.py` with:

1. `VERB_TO_PROVIDES` dict — all ~90 verb→tag mappings from the spec
2. `KEYWORD_TO_PROVIDES` dict — keyword→tag mappings
3. `SKIPPED_VERBS` set — verbs that are wrapper/internal mechanics
4. `TOKEN_TYPE_PATTERNS` dict — patterns in token_script for treasure/clue/food/blood
5. `TRIBAL_TYPES` list — known MTG creature types for tribal detection
6. `derive_provides_from_ability(verb, keyword, cost, token_script, counter_type, raw_line, target)` function — returns set of provides tags for one ability row
7. `derive_provides_for_card(abilities, type_line)` function — aggregates tags from all abilities + type_line tribal

Key implementation details:
- ChangeZone verb: parse `Origin$` and `Destination$` from raw_line using regex `r'Origin\$\s*(\w+)'` and `r'Destination\$\s*(\w+)'`
- Token tribal: extract creature type from token_script by matching against TRIBAL_TYPES
- Sacrifice in cost: check for `Sac` in cost string
- Tribal from type_line: split type_line on ` — `, take second part, check each word against TRIBAL_TYPES

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add derive_forge_tags.py tests/test_derive_forge_tags.py
git commit -m "feat: derive_forge_tags core — verb→provides mapping"
```

---

### Task 2: Trigger→wants mapping

**Files:**
- Modify: `derive_forge_tags.py`
- Modify: `tests/test_derive_forge_tags.py`

Add trigger→wants tag derivation using trigger_mode, trigger_origin, trigger_destination columns.

- [ ] **Step 1: Write tests for trigger→wants mapping**

Add to `tests/test_derive_forge_tags.py`:

```python
def test_trigger_to_wants_mapping():
    """Core trigger mapping: trigger_mode + zone info → wants tag."""
    from derive_forge_tags import TRIGGER_TO_WANTS
    assert "ChangesZone" not in TRIGGER_TO_WANTS  # zone-dependent, handled separately
    assert TRIGGER_TO_WANTS["Attacks"] == "attacks"
    assert TRIGGER_TO_WANTS["SpellCast"] == "spell-cast"
    assert TRIGGER_TO_WANTS["DamageDone"] == "damage-done"
    assert TRIGGER_TO_WANTS["Sacrificed"] == "sacrificed"
    assert TRIGGER_TO_WANTS["LifeGained"] == "life-gained"
    assert TRIGGER_TO_WANTS["Drawn"] == "card-drawn"
    assert TRIGGER_TO_WANTS["LandPlayed"] == "land-played"
    assert TRIGGER_TO_WANTS["TokenCreated"] == "token-created"
    assert TRIGGER_TO_WANTS["TurnFaceUp"] == "turn-face-up"


def test_zone_trigger_mapping():
    """ChangesZone trigger with origin/destination → fine-grained wants."""
    from derive_forge_tags import derive_wants_from_trigger
    # Battlefield → Graveyard = dies
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard", None)
    assert "dies" in tags

    # Any → Battlefield = enters-battlefield
    tags = derive_wants_from_trigger("ChangesZone", "Any", "Battlefield", None)
    assert "enters-battlefield" in tags

    # Battlefield → Exile = exiled
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Exile", None)
    assert "exiled" in tags

    # Graveyard → Battlefield = leaves-graveyard
    tags = derive_wants_from_trigger("ChangesZone", "Graveyard", "Battlefield", None)
    assert "leaves-graveyard" in tags

    # Battlefield → Any (fallback) = leaves-battlefield
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Any", None)
    assert "leaves-battlefield" in tags

    # NULL origin → Battlefield = enters-battlefield
    tags = derive_wants_from_trigger("ChangesZone", None, "Battlefield", None)
    assert "enters-battlefield" in tags


def test_trigger_filter_tribal():
    """Trigger filter with creature type → tribal wants tag."""
    from derive_forge_tags import derive_wants_from_trigger
    # Boggart Shenanigans: trigger_filter = "Goblin.YouCtrl"
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard",
                                      "Goblin.YouCtrl")
    assert "dies" in tags
    assert "goblin-tribal" in tags

    # Generic creature — no tribal
    tags = derive_wants_from_trigger("ChangesZone", "Any", "Battlefield",
                                      "Card.Self")
    assert "enters-battlefield" in tags
    # "Card" and "Self" are not tribal types


def test_sacrifice_in_cost_wants():
    """Sac in cost → sacrifice-fodder wants tag."""
    from derive_forge_tags import derive_provides_from_ability
    tags = derive_provides_from_ability(
        verb="Mana", keyword=None, cost="Sac<1/Creature.YouCtrl>",
        token_script=None, counter_type=None, raw_line="", target=None)
    # sacrifice-outlet is in provides; sacrifice-fodder should come from wants
    assert "sacrifice-outlet" in tags


def test_derive_wants_for_cost():
    """Cost-based wants tags."""
    from derive_forge_tags import derive_wants_from_cost
    tags = derive_wants_from_cost("Sac<1/Creature.YouCtrl>")
    assert "sacrifice-fodder" in tags

    tags = derive_wants_from_cost("T")
    assert len(tags) == 0  # tap cost doesn't create wants


def test_changezone_all_trigger():
    """ChangesZoneAll trigger → mass-zone-change wants tag."""
    from derive_forge_tags import derive_wants_from_trigger
    tags = derive_wants_from_trigger("ChangesZoneAll", None, None, None)
    assert "mass-zone-change" in tags


def test_additional_trigger_modes():
    """Important trigger modes beyond the basic set."""
    from derive_forge_tags import derive_wants_from_trigger
    assert "blocks" in derive_wants_from_trigger("Blocks", None, None, None)
    assert "discarded" in derive_wants_from_trigger("Discarded", None, None, None)
    assert "counter-added" in derive_wants_from_trigger("CounterAdded", None, None, None)
    assert "counter-added" in derive_wants_from_trigger("CounterAddedOnce", None, None, None)
    assert "phase-trigger" in derive_wants_from_trigger("Phase", None, None, None)
    assert "tapped" in derive_wants_from_trigger("Taps", None, None, None)
    assert "untapped" in derive_wants_from_trigger("Untaps", None, None, None)
    assert "becomes-target" in derive_wants_from_trigger("BecomesTarget", None, None, None)
    assert "attacker-blocked" in derive_wants_from_trigger("AttackerBlocked", None, None, None)
    assert "attacker-blocked" in derive_wants_from_trigger("AttackerBlockedByCreature", None, None, None)
    assert "attacker-unblocked" in derive_wants_from_trigger("AttackerUnblocked", None, None, None)
    assert "spell-cast" in derive_wants_from_trigger("SpellCastOrCopy", None, None, None)


def test_trigger_filter_other_qualifier():
    """Trigger filter with +Other qualifier still extracts tribal type."""
    from derive_forge_tags import derive_wants_from_trigger
    tags = derive_wants_from_trigger("ChangesZone", "Battlefield", "Graveyard",
                                      "Creature.Zombie+Other+YouCtrl")
    assert "zombie-tribal" in tags
    assert "dies" in tags
```

- [ ] **Step 2: Run tests to verify new tests fail**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v -k "trigger or zone or wants"`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement trigger→wants mapping**

Add to `derive_forge_tags.py`:

1. `TRIGGER_TO_WANTS` dict — simple trigger_mode→tag mappings (Attacks→attacks, SpellCast→spell-cast, etc.)
2. `ZONE_TRIGGER_MAP` — nested dict for ChangesZone: `{(origin, destination): wants_tag}`
3. `derive_wants_from_trigger(trigger_mode, origin, destination, trigger_filter)` function — returns set of wants tags
4. `derive_wants_from_cost(cost)` function — returns set of wants tags (sacrifice-fodder if Sac in cost)

Zone mapping logic:
- Exact match first: `(origin, dest)` in ZONE_TRIGGER_MAP
- Battlefield→Any fallback → `leaves-battlefield`
- Any/NULL→Battlefield → `enters-battlefield`
- Any→Graveyard → `enters-graveyard`
- Trigger filter tribal: parse creature types from filter string, add `{type}-tribal` wants

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add derive_forge_tags.py tests/test_derive_forge_tags.py
git commit -m "feat: trigger→wants mapping with zone-based tags"
```

---

### Task 3: Role derivation

**Files:**
- Modify: `derive_forge_tags.py`
- Modify: `tests/test_derive_forge_tags.py`

Derive `cards.role` from Forge verbs/keywords, replacing LLM-assigned roles.

- [ ] **Step 1: Write tests for role derivation**

Add to `tests/test_derive_forge_tags.py`:

```python
def test_derive_role():
    """Role derived from provides tags."""
    from derive_forge_tags import derive_role
    # Removal cards
    assert derive_role({"destroy"}, "Instant") == "removal"
    assert derive_role({"counter-spell"}, "Instant") == "removal"
    assert derive_role({"destroy-all"}, "Sorcery") == "removal"

    # Ramp cards
    assert derive_role({"mana"}, "Artifact") == "ramp"
    assert derive_role({"reduce-cost"}, "Creature") == "ramp"

    # Draw cards
    assert derive_role({"draw"}, "Enchantment") == "draw"
    assert derive_role({"surveil"}, "Creature") == "draw"

    # Protection
    assert derive_role({"fog"}, "Instant") == "protection"
    assert derive_role({"hexproof", "indestructible"}, "Creature") == "protection"

    # Lands
    assert derive_role(set(), "Basic Land — Plains") == "land"
    assert derive_role({"mana"}, "Land") == "land"

    # Threats (has token/pump/damage but nothing else)
    assert derive_role({"token", "pump-all"}, "Creature") == "threat"
    assert derive_role({"deal-damage"}, "Creature") == "threat"

    # Utility (fallback)
    assert derive_role({"flash"}, "Instant") == "utility"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_derive_forge_tags.py::test_derive_role -v`
Expected: FAIL

- [ ] **Step 3: Implement derive_role()**

Add to `derive_forge_tags.py`:

```python
REMOVAL_TAGS = {"destroy", "destroy-all", "counter-spell", "exile",
                "remove", "force-sacrifice", "force-sacrifice-all"}
RAMP_TAGS = {"mana", "reduce-cost"}
DRAW_TAGS = {"draw", "dig", "scry", "surveil"}
PROTECTION_TAGS = {"fog", "prevent-damage", "regenerate", "hexproof",
                   "indestructible", "ward", "shroud", "protection-grant"}
THREAT_TAGS = {"token", "pump", "pump-all", "deal-damage", "deal-damage-all",
               "put-counter", "put-counter-all"}

def derive_role(provides_tags: set, type_line: str) -> str:
    if "Land" in type_line:
        return "land"
    if provides_tags & REMOVAL_TAGS:
        return "removal"
    if provides_tags & RAMP_TAGS:
        return "ramp"
    if provides_tags & DRAW_TAGS:
        return "draw"
    if provides_tags & PROTECTION_TAGS:
        return "protection"
    if provides_tags & THREAT_TAGS:
        return "threat"
    return "utility"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add derive_forge_tags.py tests/test_derive_forge_tags.py
git commit -m "feat: derive card role from Forge verbs/keywords"
```

---

### Task 4: Full pipeline — DB wipe/repopulate + CLI

**Files:**
- Modify: `derive_forge_tags.py`
- Modify: `tests/test_derive_forge_tags.py`

Wire up the complete pipeline: read from forge_abilities, derive all tags, wipe provides/wants, bulk insert, update cards.role.

- [ ] **Step 1: Write integration test**

Add to `tests/test_derive_forge_tags.py`:

```python
import sqlite3
import os

def test_full_pipeline_on_real_db():
    """Run derive_all on real DB, verify coverage and key cards."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)  # dry_run: compute but don't write

    # Coverage: at least 90% of cards with Forge data get provides tags
    assert stats["cards_with_provides"] >= 28000, f"Low provides coverage: {stats['cards_with_provides']}"
    assert stats["cards_with_wants"] >= 10000, f"Low wants coverage: {stats['cards_with_wants']}"
    assert stats["total_provides_tags"] >= 80000, f"Low provides count: {stats['total_provides_tags']}"

    # Spot-check key cards
    card_tags = stats["card_tags"]  # {oracle_id: {"provides": set, "wants": set}}

    # Find Krenko's oracle_id
    conn = sqlite3.connect(db_path)
    krenko_oid = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Krenko, Mob Boss'").fetchone()[0]
    sol_ring_oid = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Sol Ring'").fetchone()[0]
    skullclamp_oid = conn.execute(
        "SELECT oracle_id FROM cards WHERE name = 'Skullclamp'").fetchone()[0]
    conn.close()

    # Krenko: token + goblin-tribal + tap-ability
    assert "token" in card_tags[krenko_oid]["provides"]
    assert "goblin-tribal" in card_tags[krenko_oid]["provides"]

    # Sol Ring: mana + tap-ability
    assert "mana" in card_tags[sol_ring_oid]["provides"]

    # Skullclamp: draw + equip, wants dies
    assert "draw" in card_tags[skullclamp_oid]["provides"]
    assert "dies" in card_tags[skullclamp_oid]["wants"]


def test_dfc_tags_merged():
    """DFC back face abilities merge into front face oracle_id."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    import sqlite3
    conn = sqlite3.connect(db_path)
    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    card_tags = stats["card_tags"]

    # Birgi, God of Storytelling // Harnfel, Horn of Bounty
    # Front: mana production. Back: draw/discard.
    # Both should merge into one oracle_id.
    birgi_oid = conn.execute(
        "SELECT oracle_id FROM cards WHERE name LIKE 'Birgi%'").fetchone()
    if birgi_oid:
        birgi_tags = card_tags.get(birgi_oid[0], {"provides": set()})
        # Should have tags from at least the front face
        assert len(birgi_tags["provides"]) > 0

    conn.close()


def test_cards_without_forge_data_skipped():
    """Cards with no forge_name_map entry produce no tags (not crash)."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    # Should complete without errors. Some cards will have empty tag sets.
    assert stats["cards_skipped"] >= 0


def test_role_derivation_spot_check():
    """Spot-check role derivation for well-known cards."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")
    if not os.path.exists(db_path):
        pytest.skip("No tags.db available")

    from derive_forge_tags import derive_all
    stats = derive_all(db_path, dry_run=True)
    roles = stats["card_roles"]  # {oracle_id: role}

    conn = sqlite3.connect(db_path)
    def oid(name):
        row = conn.execute("SELECT oracle_id FROM cards WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    assert roles.get(oid("Sol Ring")) == "ramp"
    assert roles.get(oid("Swords to Plowshares")) == "removal"
    assert roles.get(oid("Rhystic Study")) == "draw"
    assert roles.get(oid("Krenko, Mob Boss")) == "threat"
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_derive_forge_tags.py::test_full_pipeline_on_real_db -v`
Expected: FAIL — `derive_all` not defined

- [ ] **Step 3: Implement derive_all() and CLI**

Add to `derive_forge_tags.py`:

`derive_all(db_path, dry_run=False)` function:
1. Open DB connection
2. Load all forge_abilities rows joined with forge_name_map: `SELECT fa.*, fnm.oracle_id FROM forge_abilities fa JOIN forge_name_map fnm ON fnm.forge_name = fa.card_name`
3. Group by oracle_id
4. For each oracle_id's abilities: call derive_provides_from_ability() for each row, derive_wants_from_trigger() for triggered abilities, derive_wants_from_cost() for costs
5. Load type_line from cards table, add tribal from type_line
6. Derive role from aggregated provides tags + type_line
7. If not dry_run: wipe provides/wants tables, bulk INSERT, update cards.role
8. Return stats dict with coverage numbers and card_tags/card_roles for testing

CLI with argparse:
- `python3 derive_forge_tags.py` — full pipeline (wipe + repopulate)
- `python3 derive_forge_tags.py --dry-run` — compute and print stats without writing
- `python3 derive_forge_tags.py --card "Krenko, Mob Boss"` — show derived tags for one card
- `python3 derive_forge_tags.py --stats` — show coverage summary

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: All PASS

- [ ] **Step 5: Run the script in dry-run mode and verify coverage**

Run: `python3 derive_forge_tags.py --dry-run`
Expected output: ≥90% provides coverage, ≥30% wants coverage, spot-check output

- [ ] **Step 6: Commit**

```bash
git add derive_forge_tags.py tests/test_derive_forge_tags.py
git commit -m "feat: full derive_forge_tags pipeline with CLI"
```

---

### Task 5: ACTION_EVENT_BRIDGES + update constants.py

**Files:**
- Modify: `mtg_synergy/constants.py`
- Modify: `tests/test_derive_forge_tags.py`

Replace SEMANTIC_BRIDGES with ACTION_EVENT_BRIDGES. Update _provides_satisfies_want() to use the new bridge set.

- [ ] **Step 1: Write tests**

Add to `tests/test_derive_forge_tags.py`:

```python
def test_action_event_bridges():
    """Bridges connect provides actions to wants events."""
    from mtg_synergy.constants import ACTION_EVENT_BRIDGES
    # Key bridges must exist
    assert ("draw", "card-drawn") in ACTION_EVENT_BRIDGES
    assert ("token", "token-created") in ACTION_EVENT_BRIDGES
    assert ("token", "enters-battlefield") in ACTION_EVENT_BRIDGES
    assert ("deal-damage", "damage-done") in ACTION_EVENT_BRIDGES
    assert ("gain-life", "life-gained") in ACTION_EVENT_BRIDGES
    assert ("sacrifice-outlet", "dies") in ACTION_EVENT_BRIDGES
    assert ("sacrifice-outlet", "sacrificed") in ACTION_EVENT_BRIDGES
    assert ("destroy", "dies") in ACTION_EVENT_BRIDGES
    assert ("put-counter", "counter-added") in ACTION_EVENT_BRIDGES
    assert ("mill", "enters-graveyard") in ACTION_EVENT_BRIDGES
    assert ("discard", "discarded") in ACTION_EVENT_BRIDGES
    assert ("proliferate", "counter-added") in ACTION_EVENT_BRIDGES


def test_provides_satisfies_want_new_vocab():
    """_provides_satisfies_want works with new Forge-derived tags."""
    from mtg_synergy.constants import _provides_satisfies_want
    # Exact match
    assert _provides_satisfies_want("draw", "draw")
    # Bridge match
    assert _provides_satisfies_want("draw", "card-drawn")
    assert _provides_satisfies_want("destroy", "dies")
    assert _provides_satisfies_want("token", "enters-battlefield")
    # No match
    assert not _provides_satisfies_want("draw", "dies")
    assert not _provides_satisfies_want("mill", "attacks")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -k "bridges or satisfies" -v`
Expected: FAIL

- [ ] **Step 3: Update constants.py**

In `mtg_synergy/constants.py`:

1. Replace the body of `SEMANTIC_BRIDGES` dict (lines 10-543) with ACTION_EVENT_BRIDGES entries. Keep it as a **dict** `{(provides_tag, wants_tag): 1.0}` so consumers that iterate `.items()` with weight values (like `edges.py` line 41) keep working without changes. All bridge weights are 1.0 (deterministic, no confidence scaling).
2. Add backward-compat alias: `ACTION_EVENT_BRIDGES = SEMANTIC_BRIDGES` so new code can use the new name
3. Update `_provides_satisfies_want()` to check: (a) exact match, (b) `(provides_tag, wants_tag) in SEMANTIC_BRIDGES`
4. Update `TRIGGER_EFFECT_BRIDGES` (lines 566-606) to use new tag names. New mappings:
   - `"token"` → `{"enters-battlefield", "token-created"}`
   - `"destroy"` → `{"dies"}`
   - `"sacrifice-outlet"` → `{"dies", "sacrificed"}`
   - `"deal-damage"` → `{"damage-done"}`
   - `"draw"` → `{"card-drawn"}`
   - `"gain-life"` → `{"life-gained"}`
   - `"lose-life"` → `{"life-lost"}`
   - `"put-counter"` → `{"counter-added"}`
   - `"mill"` → `{"enters-graveyard"}`
   - `"discard"` → `{"discarded"}`
   - `"reanimate"` → `{"enters-battlefield", "leaves-graveyard"}`

Note: Since SEMANTIC_BRIDGES keeps its name as a dict, all 6+ files that import it (`edges.py`, `combos/detector.py`, `synergy_graph.py`, `tag_db.py`, `validate_system.py`, `train_synergy_model.py`) continue to work without import changes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -k "bridges or satisfies" -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/constants.py tests/test_derive_forge_tags.py
git commit -m "feat: replace SEMANTIC_BRIDGES with ACTION_EVENT_BRIDGES"
```

---

### Task 6: Update swaps.py tag references

**Files:**
- Modify: `mtg_synergy/recommend/swaps.py` (lines 11-18 SYNERGY_PROVIDES, lines 36-40 INFRASTRUCTURE_PROVIDES)
- Modify: `tests/test_derive_forge_tags.py`

Update the hardcoded tag name sets in swaps.py to match the new Forge-derived vocabulary.

- [ ] **Step 1: Write test**

```python
def test_swaps_tag_sets_use_forge_vocabulary():
    """SYNERGY_PROVIDES and INFRASTRUCTURE_PROVIDES use Forge tag names."""
    from mtg_synergy.recommend.swaps import SYNERGY_PROVIDES
    # New Forge vocabulary tags
    assert "token" in SYNERGY_PROVIDES
    assert "put-counter" in SYNERGY_PROVIDES
    assert "sacrifice-outlet" in SYNERGY_PROVIDES
    # Old LLM tags should not be present
    assert "tokens-creature" not in SYNERGY_PROVIDES
    assert "counter-placement" not in SYNERGY_PROVIDES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_derive_forge_tags.py::test_swaps_tag_sets_use_forge_vocabulary -v`
Expected: FAIL

- [ ] **Step 3: Update swaps.py**

Replace SYNERGY_PROVIDES (lines 11-18) with new Forge tag names:
```python
SYNERGY_PROVIDES = {
    "token", "token-treasure", "put-counter", "put-counter-all",
    "sacrifice-outlet", "pump", "pump-all", "deal-damage",
    "gain-life", "lose-life", "mill", "reanimate", "copy-permanent",
    "proliferate", "amass", "connive", "explore",
}
```

Replace INFRASTRUCTURE_PROVIDES (inside _classify_card_slot, lines 36-40):
```python
INFRASTRUCTURE_PROVIDES = {
    "destroy", "destroy-all", "counter-spell", "exile",
    "force-sacrifice", "mana", "reduce-cost", "draw",
    "dig", "scry", "surveil", "fog", "prevent-damage",
    "hexproof", "indestructible", "ward",
}
```

Also update `_classify_card_slot()` to use `role` from cards table (which derive_forge_tags.py now populates) instead of checking provides tags for infrastructure.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_derive_forge_tags.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/recommend/swaps.py tests/test_derive_forge_tags.py
git commit -m "feat: update swaps.py tag references to Forge vocabulary"
```

---

### Task 7: Run full pipeline + retrain fusion model + evaluate

**Files:**
- Modify: `derive_forge_tags.py` (run for real)
- No new code — this is validation

- [ ] **Step 0: Run test suite before migration to confirm clean baseline**

```bash
python3 -m pytest tests/ -v
```

Expected: All tests pass. This confirms Tasks 1-6 didn't break anything.

- [ ] **Step 1: Backup existing tags + run derive_forge_tags.py**

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('data/tags.db')
conn.execute('DROP TABLE IF EXISTS provides_backup')
conn.execute('DROP TABLE IF EXISTS wants_backup')
conn.execute('CREATE TABLE provides_backup AS SELECT * FROM provides')
conn.execute('CREATE TABLE wants_backup AS SELECT * FROM wants')
conn.commit()
print('Backup created: provides_backup, wants_backup')
conn.close()
"
python3 derive_forge_tags.py
```

Expected: Coverage stats printed, provides/wants tables repopulated. Target: ≥90% provides coverage. Backup tables preserved for rollback if needed.

- [ ] **Step 2: Spot-check individual cards**

```bash
python3 derive_forge_tags.py --card "Krenko, Mob Boss"
python3 derive_forge_tags.py --card "Skullclamp"
python3 derive_forge_tags.py --card "Swords to Plowshares"
python3 derive_forge_tags.py --card "Rhystic Study"
python3 derive_forge_tags.py --card "Dragon Fodder"
python3 derive_forge_tags.py --card "Ashnod's Altar"
python3 derive_forge_tags.py --card "Craterhoof Behemoth"
python3 derive_forge_tags.py --card "Smothering Tithe"
python3 derive_forge_tags.py --card "Syr Konrad, the Grim"
python3 derive_forge_tags.py --card "Atraxa, Praetors' Voice"
```

Verify each card's tags match its actual game function.

- [ ] **Step 3: Run all tests**

```bash
python3 -m pytest tests/ -v
```

Expected: All pass. Some old tests referencing LLM tag names may fail — those need updating (see step 4).

- [ ] **Step 4: Fix any broken tests that reference old tag names**

Tests in `tests/test_semantic_bridges.py`, `tests/test_subtag_swaps_edges.py`, `tests/test_tag_registry_subtags.py`, `tests/test_reclassify_tags.py` may reference old LLM vocabulary. Update or remove as needed.

- [ ] **Step 5: Retrain fusion model**

```bash
python3 train_fusion_model.py
```

The `cmdr_tag_overlap` feature values will change. Retrain to adapt GBM weights.

- [ ] **Step 6: Evaluate fusion model**

```bash
python3 optimize_weights.py --fusion --evaluate
```

Expected: Recall@100 within 2% of 88.7% baseline. If significantly worse, investigate which tag changes caused the regression.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: migrate to Forge-derived tags — full pipeline validated"
```

---

### Task 8: Update CLAUDE.md + cleanup

**Files:**
- Modify: `CLAUDE.md`
- Remove dead code references

- [ ] **Step 1: Update CLAUDE.md**

Update these sections:
- **Tag Schema**: Replace LLM tag description with Forge-derived tag description, new vocabulary summary
- **Enrichment Pipeline**: Replace `batch_tagger.py` steps with `derive_forge_tags.py`
- **New-set update workflow**: Update step 3
- **Common Commands**: Add `derive_forge_tags.py` commands
- **Sub-tag vocabulary**: Remove old 6→20 sub-tag section, replace with Forge verb→tag section
- **Key Files**: Add `derive_forge_tags.py`, note `batch_tagger.py` is no longer needed for tags

- [ ] **Step 2: Run final test suite**

```bash
python3 -m pytest tests/ -v
```

Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Forge-derived tag system"
```
