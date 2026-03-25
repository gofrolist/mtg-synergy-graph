# Forge Data Foundation — Implementation Plan (Plan A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Forge's 32k encoded cards as the primary structured effect data source, with new Forge-native AST types and a filter grammar parser. This is the data foundation that Plan B (causal graph rewrite + new oracle parser) builds on.

**Architecture:** New `forge_types.py` defines ForgeFilter, ForgeTrigger, ForgeEffect dataclasses aligned with Forge's DSL vocabulary. `forge_filter_parser.py` parses Forge's `Creature.YouCtrl+powerGE4` filter grammar. `forge_import.py` does a full two-pass import of 32k cards with shallow SVar resolution for triggered ability verbs. DeckHas/DeckHints tags are imported as a new scoring signal.

**Tech Stack:** Python 3, SQLite, pytest, Forge card scripts (already downloaded at `data/forge/`)

**Spec:** `docs/superpowers/specs/2026-03-25-forge-native-architecture-design.md`

**Scope:** This is Plan A of 2. Plan A delivers the data foundation (types, import, DeckHas scoring). Plan B (separate document) delivers the causal graph rewrite and new oracle parser.

---

## File Structure

| File | Responsibility | Status |
|------|---------------|--------|
| `mtg_synergy/parse/forge_types.py` | ForgeFilter, ForgeTrigger, ForgeEffect dataclasses | Create |
| `mtg_synergy/parse/forge_filter_parser.py` | Parse Forge filter grammar strings | Create |
| `mtg_synergy/parse/forge_import.py` | Full Forge DSL import with shallow SVar resolution | Create |
| `mtg_synergy/recommend/scoring.py` | Add forge_deck_overlap feature | Modify |
| `import_forge.py` | Thin CLI wrapper for forge_import.py | Modify (rewrite) |
| `tests/test_forge_types.py` | ForgeFilter, ForgeTrigger, ForgeEffect tests | Create |
| `tests/test_forge_filter_parser.py` | Filter grammar parsing tests | Create |
| `tests/test_forge_import.py` | Full import pipeline tests | Create |
| `tests/test_forge_deck_tags.py` | DeckHas/DeckHints scoring tests | Create |

---

## Task 1: Forge Types (ForgeFilter, ForgeTrigger, ForgeEffect)

**Files:**
- Create: `mtg_synergy/parse/forge_types.py`
- Test: `tests/test_forge_types.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_types.py`:

```python
"""Tests for Forge-native AST types."""
import pytest
from mtg_synergy.parse.forge_types import ForgeFilter, ForgeTrigger, ForgeEffect


def test_forge_filter_defaults():
    f = ForgeFilter()
    assert f.card_types == []
    assert f.controller is None
    assert f.raw is None


def test_forge_filter_creature():
    f = ForgeFilter(card_types=["Creature"], controller="YouCtrl")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"


def test_forge_filter_multi_type():
    f = ForgeFilter(card_types=["Instant", "Sorcery"])
    assert len(f.card_types) == 2


def test_forge_filter_with_stats():
    f = ForgeFilter(card_types=["Creature"], power_ge=4, is_attacking=True)
    assert f.power_ge == 4
    assert f.is_attacking is True


def test_forge_trigger_changes_zone():
    t = ForgeTrigger(mode="ChangesZone", origin="Any", destination="Battlefield")
    assert t.mode == "ChangesZone"
    assert t.destination == "Battlefield"


def test_forge_trigger_spell_cast():
    t = ForgeTrigger(mode="SpellCast",
                     valid_card=ForgeFilter(card_types=["Card"]),
                     trigger_zones=["Battlefield"])
    assert t.mode == "SpellCast"
    assert t.valid_card.card_types == ["Card"]


def test_forge_effect_deal_damage():
    e = ForgeEffect(verb="DealDamage", num_damage=3,
                    target=ForgeFilter(card_types=["Creature"]))
    assert e.verb == "DealDamage"
    assert e.num_damage == 3


def test_forge_effect_draw():
    e = ForgeEffect(verb="Draw", num_cards=1, defined="You")
    assert e.verb == "Draw"
    assert e.num_cards == 1


def test_forge_effect_token():
    e = ForgeEffect(verb="Token", token_script="g_1_1_goblin")
    assert e.verb == "Token"


def test_forge_effect_change_zone():
    e = ForgeEffect(verb="ChangeZone", zone_origin="Graveyard",
                    zone_destination="Battlefield")
    assert e.zone_origin == "Graveyard"


def test_forge_effect_optional():
    e = ForgeEffect(verb="Draw", optional=True)
    assert e.optional is True


def test_forge_filter_serialization():
    """ForgeFilter should be JSON-serializable via to_dict/from_dict."""
    from mtg_synergy.parse.forge_types import forge_filter_to_dict, forge_filter_from_dict
    f = ForgeFilter(card_types=["Creature"], controller="YouCtrl", power_ge=4)
    d = forge_filter_to_dict(f)
    f2 = forge_filter_from_dict(d)
    assert f2.card_types == ["Creature"]
    assert f2.controller == "YouCtrl"
    assert f2.power_ge == 4
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_forge_types.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement forge_types.py**

Create `mtg_synergy/parse/forge_types.py`:

```python
"""Forge-native AST types for structured card data.

Aligned with Forge's 20-year battle-tested DSL vocabulary.
These replace the original ast_types.py Effect/Trigger/ObjectFilter
with Forge-compatible equivalents.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ForgeFilter:
    """Filter for card/permanent matching. Parses Forge filter strings.

    Example: 'Creature.YouCtrl+powerGE4+attacking'
    -> ForgeFilter(card_types=["Creature"], controller="YouCtrl", power_ge=4, is_attacking=True)
    """
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    controller: str | None = None
    zone: str | None = None
    power_ge: int | None = None
    power_le: int | None = None
    toughness_ge: int | None = None
    toughness_le: int | None = None
    cmc_ge: int | None = None
    cmc_le: int | None = None
    is_token: bool | None = None
    is_attacking: bool | None = None
    is_blocking: bool | None = None
    is_tapped: bool | None = None
    has_keyword: str | None = None
    is_legendary: bool | None = None
    is_other: bool | None = None
    is_self: bool | None = None
    is_remembered: bool | None = None
    attached_by: str | None = None
    raw: str | None = None


@dataclass
class ForgeTrigger:
    """Trigger condition aligned with Forge's 134 trigger modes."""
    mode: str = ""
    valid_card: ForgeFilter | None = None
    origin: str | None = None
    destination: str | None = None
    phase: str | None = None
    trigger_zones: list[str] = field(default_factory=list)


@dataclass
class ForgeEffect:
    """Effect action aligned with Forge's 50+ effect verbs."""
    verb: str = ""
    target: ForgeFilter | None = None
    defined: str | None = None
    amount: str | None = None
    sub_ability: str | None = None
    optional: bool = False
    num_damage: int | None = None
    num_cards: int | None = None
    keyword: str | None = None
    token_script: str | None = None
    counter_type: str | None = None
    zone_origin: str | None = None
    zone_destination: str | None = None


def forge_filter_to_dict(f: ForgeFilter) -> dict:
    """Serialize ForgeFilter to JSON-compatible dict (skip None/empty)."""
    d = {}
    if f.card_types:
        d["card_types"] = f.card_types
    if f.subtypes:
        d["subtypes"] = f.subtypes
    for field_name in ("controller", "zone", "power_ge", "power_le",
                       "toughness_ge", "toughness_le", "cmc_ge", "cmc_le",
                       "is_token", "is_attacking", "is_blocking", "is_tapped",
                       "has_keyword", "is_legendary", "is_other", "is_self",
                       "is_remembered", "attached_by", "raw"):
        val = getattr(f, field_name)
        if val is not None:
            d[field_name] = val
    return d


def forge_filter_from_dict(d: dict) -> ForgeFilter:
    """Deserialize ForgeFilter from dict."""
    return ForgeFilter(
        card_types=d.get("card_types", []),
        subtypes=d.get("subtypes", []),
        controller=d.get("controller"),
        zone=d.get("zone"),
        power_ge=d.get("power_ge"),
        power_le=d.get("power_le"),
        toughness_ge=d.get("toughness_ge"),
        toughness_le=d.get("toughness_le"),
        cmc_ge=d.get("cmc_ge"),
        cmc_le=d.get("cmc_le"),
        is_token=d.get("is_token"),
        is_attacking=d.get("is_attacking"),
        is_blocking=d.get("is_blocking"),
        is_tapped=d.get("is_tapped"),
        has_keyword=d.get("has_keyword"),
        is_legendary=d.get("is_legendary"),
        is_other=d.get("is_other"),
        is_self=d.get("is_self"),
        is_remembered=d.get("is_remembered"),
        attached_by=d.get("attached_by"),
        raw=d.get("raw"),
    )
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_types.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/forge_types.py tests/test_forge_types.py
git commit -m "feat(parse): add Forge-native AST types (ForgeFilter, ForgeTrigger, ForgeEffect)"
```

---

## Task 2: Forge Filter Grammar Parser

**Files:**
- Create: `mtg_synergy/parse/forge_filter_parser.py`
- Test: `tests/test_forge_filter_parser.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_filter_parser.py`:

```python
"""Tests for Forge filter grammar parser."""
import pytest
from mtg_synergy.parse.forge_filter_parser import parse_forge_filter


def test_simple_card_type():
    f = parse_forge_filter("Creature")
    assert f.card_types == ["Creature"]


def test_type_with_controller():
    f = parse_forge_filter("Creature.YouCtrl")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"


def test_type_union():
    f = parse_forge_filter("Instant,Sorcery")
    assert f.card_types == ["Instant", "Sorcery"]


def test_subtype():
    f = parse_forge_filter("Goblin.YouCtrl")
    assert f.subtypes == ["Goblin"]
    assert f.controller == "YouCtrl"


def test_power_ge():
    f = parse_forge_filter("Creature.YouCtrl+powerGE4")
    assert f.card_types == ["Creature"]
    assert f.power_ge == 4


def test_cmc_ge():
    f = parse_forge_filter("Card.cmcGE5")
    assert f.card_types == ["Card"]
    assert f.cmc_ge == 5


def test_attacking():
    f = parse_forge_filter("Creature.attacking")
    assert f.card_types == ["Creature"]
    assert f.is_attacking is True


def test_token():
    f = parse_forge_filter("Creature.token")
    assert f.is_token is True


def test_other():
    f = parse_forge_filter("Creature.Other+YouCtrl")
    assert f.is_other is True
    assert f.controller == "YouCtrl"


def test_self():
    f = parse_forge_filter("Card.Self")
    assert f.is_self is True


def test_legendary():
    f = parse_forge_filter("Creature.Legendary+YouCtrl")
    assert f.is_legendary is True
    assert f.controller == "YouCtrl"


def test_with_keyword():
    f = parse_forge_filter("Creature+withFlying")
    assert f.has_keyword == "Flying"


def test_tapped_untapped():
    f = parse_forge_filter("Creature.untapped+YouCtrl")
    assert f.is_tapped is False
    assert f.controller == "YouCtrl"


def test_complex_filter():
    f = parse_forge_filter("Creature.YouCtrl+powerGE4+attacking+Other")
    assert f.card_types == ["Creature"]
    assert f.controller == "YouCtrl"
    assert f.power_ge == 4
    assert f.is_attacking is True
    assert f.is_other is True


def test_unparsed_stored_in_raw():
    f = parse_forge_filter("Card.IsRemembered+EffectSource")
    assert f.raw is not None
    assert "IsRemembered" in f.raw


def test_empty_string():
    f = parse_forge_filter("")
    assert f.card_types == []


def test_opponent_ctrl():
    f = parse_forge_filter("Creature.OppCtrl")
    assert f.controller == "OppCtrl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_forge_filter_parser.py -v`

- [ ] **Step 3: Implement the parser**

Create `mtg_synergy/parse/forge_filter_parser.py`:

```python
"""Parse Forge filter grammar strings into ForgeFilter objects.

Forge filter format: 'CardType.modifier+modifier+modifier'
Examples:
  'Creature.YouCtrl+powerGE4+attacking'
  'Instant,Sorcery'
  'Card.Self'
  'Goblin.YouCtrl+Other'
"""
import re
from mtg_synergy.parse.forge_types import ForgeFilter

# Known card types in Forge
_CARD_TYPES = {
    "creature", "artifact", "enchantment", "instant", "sorcery",
    "planeswalker", "land", "permanent", "spell", "card",
    "tribal", "battle",
}

# Controller modifiers
_CONTROLLERS = {"youctrl", "oppctrl", "youown", "youdontctrl"}

# Boolean modifiers
_BOOL_MODIFIERS = {
    "attacking": ("is_attacking", True),
    "blocking": ("is_blocking", True),
    "tapped": ("is_tapped", True),
    "untapped": ("is_tapped", False),
    "token": ("is_token", True),
    "nontoken": ("is_token", False),
    "legendary": ("is_legendary", True),
    "other": ("is_other", True),
    "self": ("is_self", True),
    "isremembered": ("is_remembered", True),
}

# Numeric comparison patterns
_NUMERIC_RE = re.compile(r'^(power|toughness|cmc)(GE|LE|EQ)(\d+)$', re.IGNORECASE)


def parse_forge_filter(filter_str: str) -> ForgeFilter:
    """Parse a Forge filter string into a ForgeFilter object."""
    if not filter_str or not filter_str.strip():
        return ForgeFilter()

    f = ForgeFilter(raw=filter_str)

    # Split on '+' to get top-level modifiers
    # But first handle '.' separator (type.modifier)
    # Forge format: 'Type.mod1+mod2' or 'Type1,Type2' or 'Type.mod1.mod2+mod3'
    parts = filter_str.replace(".", "+").split("+")

    unparsed = []
    for part in parts:
        part = part.strip()
        if not part:
            continue

        part_lower = part.lower()

        # Check for comma-separated type union
        if "," in part:
            for t in part.split(","):
                t = t.strip()
                if t.lower() in _CARD_TYPES:
                    f.card_types.append(t)
                else:
                    f.subtypes.append(t)
            continue

        # Check if it's a known card type
        if part_lower in _CARD_TYPES:
            f.card_types.append(part)
            continue

        # Check controller
        if part_lower in _CONTROLLERS:
            f.controller = part
            continue

        # Check boolean modifiers
        if part_lower in _BOOL_MODIFIERS:
            attr, val = _BOOL_MODIFIERS[part_lower]
            setattr(f, attr, val)
            continue

        # Check 'with' keyword prefix
        if part_lower.startswith("with"):
            kw = part[4:]  # strip "with"
            if kw:
                f.has_keyword = kw
            continue

        # Check 'without' keyword prefix
        if part_lower.startswith("without"):
            continue  # skip negative keyword filters for now

        # Check numeric comparisons (powerGE4, cmcLE3, etc.)
        m = _NUMERIC_RE.match(part)
        if m:
            stat, op, val = m.group(1).lower(), m.group(2).upper(), int(m.group(3))
            if op == "GE":
                setattr(f, f"{stat}_ge", val)
            elif op == "LE":
                setattr(f, f"{stat}_le", val)
            continue

        # Check for attached_by patterns
        if part_lower in ("attachedby", "enchantedby", "equippedby"):
            f.attached_by = part
            continue

        # Check for zone names
        _ZONES = {"battlefield", "graveyard", "hand", "library", "exile", "command", "stack"}
        if part_lower in _ZONES:
            f.zone = part
            continue

        # Not a known card type, controller, or zone — might be a creature subtype
        if part[0].isupper() and part_lower not in _CARD_TYPES and part_lower not in _ZONES:
            f.subtypes.append(part)
            continue

        # Unparsed modifier
        unparsed.append(part)

    # If we have unparsed modifiers, store them in raw
    if unparsed:
        f.raw = filter_str  # keep full string for unparsed cases
    elif not unparsed:
        f.raw = None  # fully parsed — clear raw

    return f
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_filter_parser.py -v`
Expected: All 18 tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/parse/forge_filter_parser.py tests/test_forge_filter_parser.py
git commit -m "feat(parse): add Forge filter grammar parser"
```

---

## Task 3: Full Forge Import with Shallow SVar Resolution

**Files:**
- Create: `mtg_synergy/parse/forge_import.py`
- Modify: `import_forge.py` (rewrite as thin CLI wrapper)
- Test: `tests/test_forge_import.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_import.py`:

```python
"""Tests for full Forge DSL import pipeline."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import (
    parse_forge_card_file, ensure_forge_schema, shallow_svar_resolve,
    extract_ability_fields,
)


RHYSTIC_STUDY = """Name:Rhystic Study
ManaCost:2 U
Types:Enchantment
T:Mode$ SpellCast | ValidCard$ Card | ValidActivatingPlayer$ Opponent | TriggerZones$ Battlefield | Execute$ TrigDraw | TriggerDescription$ Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.
SVar:TrigDraw:DB$ Draw | Defined$ You | UnlessCost$ 1 | UnlessPayer$ TriggeredActivator | NumCards$ 1 | OptionalDecider$ You
Oracle:Whenever an opponent casts a spell, you may draw a card unless that player pays {1}."""

LIGHTNING_BOLT = """Name:Lightning Bolt
ManaCost:R
Types:Instant
A:SP$ DealDamage | ValidTgts$ Any | NumDmg$ 3 | SpellDescription$ CARDNAME deals 3 damage to any target.
Oracle:Lightning Bolt deals 3 damage to any target."""

SOL_RING = """Name:Sol Ring
ManaCost:1
Types:Artifact
A:AB$ Mana | Cost$ T | Produced$ C | Amount$ 2 | SpellDescription$ Add {C}{C}.
K:ETBReplacement:ETBTapped:Self
Oracle:{T}: Add {C}{C}."""

KRENKO = """Name:Krenko, Mob Boss
ManaCost:2 R R
Types:Legendary Creature Goblin Warrior
PT:3/3
A:AB$ Token | Cost$ T | TokenScript$ r_1_1_goblin | TokenAmount$ X | References$ X | SpellDescription$ Create X 1/1 red Goblin creature tokens.
SVar:X:Count$Valid Goblin.YouCtrl
DeckHas:Ability$Token
DeckHints:Type$Goblin
Oracle:{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."""


def test_parse_lightning_bolt():
    card = parse_forge_card_file(LIGHTNING_BOLT)
    assert card["name"] == "Lightning Bolt"
    assert len(card["abilities"]) == 1
    ab = card["abilities"][0]
    assert ab["ability_type"] == "A"
    assert ab["verb"] == "DealDamage"
    assert ab["amount"] == "3"


def test_parse_rhystic_study_svar_resolve():
    card = parse_forge_card_file(RHYSTIC_STUDY)
    assert card["name"] == "Rhystic Study"
    # Trigger should have verb resolved from SVar
    triggers = [a for a in card["abilities"] if a["ability_type"] == "T"]
    assert len(triggers) == 1
    assert triggers[0]["verb"] == "Draw"  # resolved from SVar:TrigDraw:DB$ Draw
    assert triggers[0]["trigger_mode"] == "SpellCast"
    assert triggers[0]["amount"] == "1"  # NumCards$ 1 from SVar


def test_parse_sol_ring_keyword():
    card = parse_forge_card_file(SOL_RING)
    keywords = [a for a in card["abilities"] if a["ability_type"] == "K"]
    assert len(keywords) >= 1


def test_parse_krenko_deck_tags():
    card = parse_forge_card_file(KRENKO)
    assert any(t["tag_type"] == "has" and "Token" in t["tag"] for t in card["deck_tags"])
    assert any(t["tag_type"] == "hints" and "Goblin" in t["tag"] for t in card["deck_tags"])


def test_parse_krenko_token_ability():
    card = parse_forge_card_file(KRENKO)
    ab = card["abilities"][0]
    assert ab["verb"] == "Token"
    assert ab["token_script"] == "r_1_1_goblin"


def test_svars_collected():
    card = parse_forge_card_file(RHYSTIC_STUDY)
    assert "TrigDraw" in card["svars"]
    assert "DB$ Draw" in card["svars"]["TrigDraw"]


def test_shallow_svar_resolve():
    svars = {"TrigDraw": "DB$ Draw | Defined$ You | NumCards$ 1"}
    fields = shallow_svar_resolve("TrigDraw", svars)
    assert fields["verb"] == "Draw"
    assert fields.get("amount") == "1"


def test_schema_creation(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'forge_%'"
    ).fetchall()]
    assert "forge_abilities" in tables
    assert "forge_deck_tags" in tables
    assert "forge_svars" in tables
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_forge_import.py -v`

- [ ] **Step 3: Implement forge_import.py**

Create `mtg_synergy/parse/forge_import.py`:

```python
"""Full Forge DSL import with shallow SVar resolution.

Two-pass import:
  Pass 1: Collect SVars per card
  Pass 2: Parse ability lines, resolve Execute$ references via SVars
"""
import os
import re

CARDS_DIR_DEFAULT = os.path.join("data", "forge", "forge-gui", "res", "cardsfolder")


def ensure_forge_schema(conn):
    """Create Forge tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_abilities (
            card_name TEXT NOT NULL,
            ability_index INTEGER NOT NULL,
            ability_type TEXT NOT NULL,
            verb TEXT,
            trigger_mode TEXT,
            trigger_filter TEXT,
            trigger_origin TEXT,
            trigger_destination TEXT,
            trigger_phase TEXT,
            trigger_zones TEXT,
            target TEXT,
            defined TEXT,
            amount TEXT,
            cost TEXT,
            keyword TEXT,
            token_script TEXT,
            counter_type TEXT,
            sub_ability TEXT,
            unless_cost TEXT,
            raw_line TEXT NOT NULL,
            PRIMARY KEY (card_name, ability_index)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_deck_tags (
            card_name TEXT NOT NULL,
            tag_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (card_name, tag_type, tag)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_svars (
            card_name TEXT NOT NULL,
            svar_name TEXT NOT NULL,
            svar_value TEXT NOT NULL,
            PRIMARY KEY (card_name, svar_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forge_ab_name ON forge_abilities(card_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_forge_tags_name ON forge_deck_tags(card_name)")
    conn.commit()


def _parse_kv_line(line: str) -> dict:
    """Parse 'Key$ Value | Key2$ Value2' into dict."""
    fields = {}
    for pair in line.split(" | "):
        pair = pair.strip()
        if "$ " in pair:
            key, val = pair.split("$ ", 1)
            fields[key.strip()] = val.strip()
        elif "$" in pair:
            key, val = pair.split("$", 1)
            fields[key.strip()] = val.strip()
    return fields


def shallow_svar_resolve(svar_name: str, svars: dict) -> dict:
    """Resolve one level of SVar to extract verb and parameters.

    Input: SVar value like 'DB$ Draw | Defined$ You | NumCards$ 1'
    Returns: {verb, amount, defined, target, keyword, ...}
    """
    svar_value = svars.get(svar_name, "")
    if not svar_value:
        return {}
    fields = _parse_kv_line(svar_value)
    result = {}
    result["verb"] = fields.get("DB") or fields.get("SP")
    result["defined"] = fields.get("Defined")
    result["target"] = fields.get("ValidTgts") or fields.get("Tgt")
    result["amount"] = (fields.get("NumDmg") or fields.get("NumCards")
                        or fields.get("TokenAmount") or fields.get("CounterNum"))
    result["keyword"] = fields.get("KW")
    result["token_script"] = fields.get("TokenScript")
    result["counter_type"] = fields.get("CounterType")
    result["unless_cost"] = fields.get("UnlessCost")
    result["sub_ability"] = fields.get("SubAbility")
    return {k: v for k, v in result.items() if v is not None}


def extract_ability_fields(line: str, prefix: str, svars: dict) -> dict:
    """Extract structured fields from an ability line."""
    fields = _parse_kv_line(line)

    result = {
        "ability_type": prefix,
        "raw_line": f"{prefix}:{line}",
        "verb": None,
        "trigger_mode": None,
        "trigger_filter": None,
        "trigger_origin": None,
        "trigger_destination": None,
        "trigger_phase": None,
        "trigger_zones": None,
        "target": None,
        "defined": None,
        "amount": None,
        "cost": None,
        "keyword": None,
        "token_script": None,
        "counter_type": None,
        "sub_ability": None,
        "unless_cost": None,
    }

    if prefix == "A":
        result["verb"] = fields.get("SP") or fields.get("AB")
        result["cost"] = fields.get("Cost")
    elif prefix == "T":
        result["trigger_mode"] = fields.get("Mode")
        result["trigger_filter"] = fields.get("ValidCard")
        result["trigger_origin"] = fields.get("Origin")
        result["trigger_destination"] = fields.get("Destination")
        result["trigger_phase"] = fields.get("Phase")
        result["trigger_zones"] = fields.get("TriggerZones")
        # Shallow SVar resolution for verb
        execute_ref = fields.get("Execute")
        if execute_ref:
            resolved = shallow_svar_resolve(execute_ref, svars)
            result["verb"] = resolved.get("verb")
            if not result.get("amount"):
                result["amount"] = resolved.get("amount")
            if not result.get("defined"):
                result["defined"] = resolved.get("defined")
            if not result.get("target"):
                result["target"] = resolved.get("target")
            if not result.get("keyword"):
                result["keyword"] = resolved.get("keyword")
            if not result.get("token_script"):
                result["token_script"] = resolved.get("token_script")
            if not result.get("counter_type"):
                result["counter_type"] = resolved.get("counter_type")
            if not result.get("unless_cost"):
                result["unless_cost"] = resolved.get("unless_cost")
            if not result.get("sub_ability"):
                result["sub_ability"] = resolved.get("sub_ability")
    elif prefix == "S":
        result["verb"] = fields.get("SP") or fields.get("Mode")
    elif prefix == "K":
        # K: lines store keyword name directly
        kw_part = line.split("|")[0].strip()
        if ":" in kw_part:
            result["keyword"] = kw_part.split(":")[0].strip()
        else:
            result["keyword"] = kw_part.strip()

    # Common fields across all types
    if not result.get("target"):
        result["target"] = fields.get("ValidTgts") or fields.get("Tgt")
    if not result.get("defined"):
        result["defined"] = fields.get("Defined")
    if not result.get("amount"):
        result["amount"] = (fields.get("NumDmg") or fields.get("NumCards")
                            or fields.get("TokenAmount") or fields.get("CounterNum")
                            or fields.get("Amount"))
    if not result.get("keyword"):
        result["keyword"] = fields.get("KW")
    if not result.get("token_script"):
        result["token_script"] = fields.get("TokenScript")
    if not result.get("counter_type"):
        result["counter_type"] = fields.get("CounterType")
    if not result.get("sub_ability"):
        result["sub_ability"] = fields.get("SubAbility")
    if not result.get("unless_cost"):
        result["unless_cost"] = fields.get("UnlessCost")

    return result


def parse_forge_card_file(text: str) -> dict:
    """Parse a Forge card file text into structured data.

    Returns: {name, abilities: [...], svars: {...}, deck_tags: [...]}
    """
    name = None
    svars = {}
    abilities = []
    deck_tags = []
    ab_idx = 0

    # Pass 1: collect SVars and metadata
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("Name:"):
            name = line[5:].strip()
        elif line.startswith("SVar:"):
            # SVar:VarName:value
            rest = line[5:]
            colon_idx = rest.index(":")
            svar_name = rest[:colon_idx].strip()
            svar_value = rest[colon_idx + 1:].strip()
            svars[svar_name] = svar_value
        elif line.startswith("DeckHas:"):
            for tag in line[8:].strip().split(" & "):
                deck_tags.append({"tag_type": "has", "tag": tag.strip()})
        elif line.startswith("DeckHints:"):
            for tag in line[10:].strip().split(" & "):
                deck_tags.append({"tag_type": "hints", "tag": tag.strip()})
        elif line.startswith("DeckNeeds:"):
            for tag in line[10:].strip().split(" & "):
                deck_tags.append({"tag_type": "needs", "tag": tag.strip()})

    # Pass 2: parse ability lines with SVar resolution
    for line in text.strip().split("\n"):
        line = line.strip()
        prefix = None
        for p in ("A:", "T:", "S:", "K:", "R:"):
            if line.startswith(p):
                prefix = p[0]
                line_body = line[len(p):]
                break
        if prefix is None:
            continue

        ab = extract_ability_fields(line_body, prefix, svars)
        ab["ability_index"] = ab_idx
        abilities.append(ab)
        ab_idx += 1

    return {
        "name": name,
        "abilities": abilities,
        "svars": svars,
        "deck_tags": deck_tags,
    }


def import_card_to_db(conn, card: dict):
    """Insert a parsed card into the forge_* tables."""
    name = card["name"]
    if not name:
        return

    for ab in card["abilities"]:
        conn.execute(
            "INSERT OR REPLACE INTO forge_abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, ab["ability_index"], ab["ability_type"], ab.get("verb"),
             ab.get("trigger_mode"), ab.get("trigger_filter"),
             ab.get("trigger_origin"), ab.get("trigger_destination"),
             ab.get("trigger_phase"), ab.get("trigger_zones"),
             ab.get("target"), ab.get("defined"), ab.get("amount"),
             ab.get("cost"), ab.get("keyword"), ab.get("token_script"),
             ab.get("counter_type"), ab.get("sub_ability"),
             ab.get("unless_cost"), ab.get("raw_line", "")),
        )

    for tag in card["deck_tags"]:
        conn.execute(
            "INSERT OR IGNORE INTO forge_deck_tags VALUES (?,?,?)",
            (name, tag["tag_type"], tag["tag"]),
        )

    for svar_name, svar_value in card["svars"].items():
        conn.execute(
            "INSERT OR REPLACE INTO forge_svars VALUES (?,?,?)",
            (name, svar_name, svar_value),
        )


def import_all(conn, cards_dir=None):
    """Import all Forge card files to DB."""
    if cards_dir is None:
        cards_dir = CARDS_DIR_DEFAULT

    ensure_forge_schema(conn)
    conn.execute("DELETE FROM forge_abilities")
    conn.execute("DELETE FROM forge_deck_tags")
    conn.execute("DELETE FROM forge_svars")

    if not os.path.exists(cards_dir):
        print(f"Forge cards not found at {cards_dir}")
        return 0

    imported = 0
    errors = 0
    for root, dirs, files in os.walk(cards_dir):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            try:
                with open(os.path.join(root, fname), "r", errors="ignore") as f:
                    text = f.read()
                card = parse_forge_card_file(text)
                if card["name"]:
                    import_card_to_db(conn, card)
                    imported += 1
            except Exception:
                errors += 1

    conn.commit()
    print(f"Imported {imported} cards ({errors} errors)")
    return imported


def build_name_mapping(conn):
    """Build forge_name → oracle_id mapping for card matching."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_name_map (
            forge_name TEXT PRIMARY KEY,
            oracle_id TEXT NOT NULL
        )
    """)
    conn.execute("DELETE FROM forge_name_map")

    # Exact match
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name = fa.card_name
    """)

    # DFC front face match
    conn.execute("""
        INSERT OR IGNORE INTO forge_name_map (forge_name, oracle_id)
        SELECT DISTINCT fa.card_name, c.oracle_id
        FROM forge_abilities fa
        JOIN cards c ON c.name LIKE fa.card_name || ' //%'
        WHERE fa.card_name NOT IN (SELECT forge_name FROM forge_name_map)
    """)

    conn.commit()
    matched = conn.execute("SELECT COUNT(*) FROM forge_name_map").fetchone()[0]
    total = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_abilities").fetchone()[0]
    print(f"Name mapping: {matched}/{total} Forge cards matched to Scryfall oracle_ids")
    return matched


def show_stats(conn):
    """Print import statistics."""
    ensure_forge_schema(conn)
    cards = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_abilities").fetchone()[0]
    abilities = conn.execute("SELECT COUNT(*) FROM forge_abilities").fetchone()[0]
    with_verb = conn.execute("SELECT COUNT(*) FROM forge_abilities WHERE verb IS NOT NULL").fetchone()[0]
    triggers = conn.execute("SELECT COUNT(*) FROM forge_abilities WHERE ability_type = 'T'").fetchone()[0]
    trig_with_verb = conn.execute(
        "SELECT COUNT(*) FROM forge_abilities WHERE ability_type = 'T' AND verb IS NOT NULL"
    ).fetchone()[0]
    deck_has = conn.execute("SELECT COUNT(*) FROM forge_deck_tags WHERE tag_type = 'has'").fetchone()[0]
    deck_hints = conn.execute("SELECT COUNT(*) FROM forge_deck_tags WHERE tag_type = 'hints'").fetchone()[0]
    svars = conn.execute("SELECT COUNT(*) FROM forge_svars").fetchone()[0]

    print(f"Forge import stats:")
    print(f"  Cards: {cards}")
    print(f"  Abilities: {abilities} ({with_verb} with verb)")
    print(f"  Triggers: {triggers} ({trig_with_verb} with resolved verb via SVar)")
    print(f"  DeckHas tags: {deck_has}")
    print(f"  DeckHints tags: {deck_hints}")
    print(f"  SVars: {svars}")
```

- [ ] **Step 4: Rewrite import_forge.py as thin CLI wrapper**

Replace `import_forge.py` contents:

```python
#!/usr/bin/env python3
"""CLI wrapper for Forge DSL import.

Usage:
    python3 import_forge.py --download    # Clone Forge repo (sparse)
    python3 import_forge.py --import      # Full import with SVar resolution
    python3 import_forge.py --stats       # Show import stats
    python3 import_forge.py --map         # Build name mapping to Scryfall
"""
import argparse
import subprocess
import os

FORGE_REPO = "https://github.com/Card-Forge/forge.git"
FORGE_DIR = "data/forge"


def download_forge():
    if os.path.exists(FORGE_DIR):
        print(f"Forge repo already exists at {FORGE_DIR}")
        return
    print("Cloning Forge repo (sparse, cardsfolder only)...")
    subprocess.run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", FORGE_REPO, FORGE_DIR
    ], check=True)
    subprocess.run([
        "git", "-C", FORGE_DIR, "sparse-checkout", "set",
        "forge-gui/res/cardsfolder"
    ], check=True)
    print("Done.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--map", action="store_true", help="Build name mapping")
    args = parser.parse_args()

    if args.download:
        download_forge()
        return

    from mtg_synergy.db import get_connection
    from mtg_synergy.parse.forge_import import import_all, show_stats, build_name_mapping

    conn = get_connection()
    if args.do_import:
        import_all(conn)
    elif args.stats:
        show_stats(conn)
    elif args.map:
        build_name_mapping(conn)
    else:
        parser.print_help()
    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_forge_import.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`

- [ ] **Step 7: Commit**

```bash
git add mtg_synergy/parse/forge_import.py import_forge.py tests/test_forge_import.py
git commit -m "feat(parse): full Forge DSL import with shallow SVar resolution"
```

---

## Task 4: Run Full Forge Import

Operational task — run the import pipeline on the 32k card dataset.

- [ ] **Step 1: Run full import**

```bash
python3 import_forge.py --import
python3 import_forge.py --stats
```

Expected: ~32k cards imported, majority of triggers have resolved verbs.

- [ ] **Step 2: Build name mapping**

```bash
python3 import_forge.py --map
```

Expected: ~28k+ Forge cards matched to Scryfall oracle_ids.

- [ ] **Step 3: Verify key cards**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
for name in ['Lightning Bolt', 'Rhystic Study', 'Craterhoof Behemoth', 'Smothering Tithe', 'Krenko, Mob Boss']:
    row = conn.execute('SELECT ability_type, verb, trigger_mode, keyword FROM forge_abilities WHERE card_name = ?', (name,)).fetchall()
    print(f'{name}: {[(r[0], r[1] or r[3] or r[2]) for r in row]}')
conn.close()
"
```

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/parse/forge_import.py
git commit -m "feat: run full Forge import — 32k cards with SVar resolution"
```

---

## Task 5: DeckHas/DeckHints Scoring Integration

**Files:**
- Modify: `mtg_synergy/recommend/scoring.py`
- Test: `tests/test_forge_deck_tags.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_forge_deck_tags.py`:

```python
"""Tests for DeckHas/DeckHints scoring integration."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_import import ensure_forge_schema


def _setup_deck_tags(conn):
    """Insert test DeckHas/DeckHints data.

    DeckHas = what a card provides. DeckHints = what a card wants in the deck.
    Overlap = candidate_has & cmdr_hints + candidate_hints & cmdr_has.
    """
    ensure_forge_schema(conn)
    # Commander: provides tokens, wants goblins AND tokens
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'has', 'Ability$Token')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'hints', 'Type$Goblin')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Krenko, Mob Boss', 'hints', 'Ability$Token')")
    # Candidate: provides tokens, wants goblins
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Goblin Instigator', 'has', 'Ability$Token')")
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Goblin Instigator', 'hints', 'Type$Goblin')")
    # Unrelated card: provides counter ability
    conn.execute("INSERT INTO forge_deck_tags VALUES ('Counterspell', 'has', 'Ability$Counter')")
    conn.commit()


def test_forge_deck_overlap(tmp_db):
    from mtg_synergy.recommend.scoring import compute_forge_deck_overlap
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    # Instigator has Token -> matches Krenko hints Token = 1
    # Instigator hints Goblin -> doesn't match Krenko has Token = 0
    # Total overlap = 1
    overlap = compute_forge_deck_overlap(conn, "Krenko, Mob Boss", "Goblin Instigator")
    assert overlap >= 1
    conn.close()


def test_forge_deck_overlap_zero(tmp_db):
    from mtg_synergy.recommend.scoring import compute_forge_deck_overlap
    conn = sqlite3.connect(tmp_db)
    _setup_deck_tags(conn)
    overlap = compute_forge_deck_overlap(conn, "Krenko, Mob Boss", "Counterspell")
    assert overlap == 0
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Add FORGE_DECK_OVERLAP weight to config.py**

Add to `mtg_synergy/config.py` SCORING_WEIGHTS dict:
```python
    "FORGE_DECK_OVERLAP": 3.0,   # Forge DeckHas/DeckHints human-curated tag overlap
```

- [ ] **Step 4: Add forge deck tag loading to DeckContext and scoring**

Add to `mtg_synergy/recommend/scoring.py`:

First, in `DeckContext.__init__`, after the EDHREC loading block, cache commander's forge tags:
```python
        # Forge DeckHas/DeckHints tags (cached for all candidates)
        self.forge_cmdr_has = set()
        self.forge_cmdr_hints = set()
        try:
            for r in conn.execute(
                "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
                (commander,)).fetchall():
                if r[0] == "has":
                    self.forge_cmdr_has.add(r[1])
                elif r[0] == "hints":
                    self.forge_cmdr_hints.add(r[1])
        except Exception:
            pass

        # Bulk-load all candidate forge tags
        self.forge_card_has = {}   # {card_name: set of tags}
        self.forge_card_hints = {} # {card_name: set of tags}
        try:
            for r in conn.execute("SELECT card_name, tag_type, tag FROM forge_deck_tags"):
                if r[1] == "has":
                    self.forge_card_has.setdefault(r[0], set()).add(r[2])
                elif r[1] == "hints":
                    self.forge_card_hints.setdefault(r[0], set()).add(r[2])
        except Exception:
            pass
```

Then add the standalone function (for testing) and wire into `compute_dynamic_score`:

```python
def compute_forge_deck_overlap(conn, commander_name: str, candidate_name: str) -> int:
    """Count matching DeckHas/DeckHints between commander and candidate.

    DeckHas = what the card provides (abilities, types)
    DeckHints = what the card wants in the deck

    Overlap = (candidate provides what commander wants) +
              (commander provides what candidate wants)
    """
    cmdr_has = set()
    cmdr_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (commander_name,)
    ).fetchall():
        if r[0] == "has":
            cmdr_has.add(r[1])
        elif r[0] == "hints":
            cmdr_hints.add(r[1])

    cand_has = set()
    cand_hints = set()
    for r in conn.execute(
        "SELECT tag_type, tag FROM forge_deck_tags WHERE card_name = ?",
        (candidate_name,)
    ).fetchall():
        if r[0] == "has":
            cand_has.add(r[1])
        elif r[0] == "hints":
            cand_hints.add(r[1])

    return len(cand_has & cmdr_hints) + len(cand_hints & cmdr_has)
```

Then in `compute_dynamic_score`, add a new Feature after the causal feature:

```python
    # --- Feature 12: Forge DeckHas/DeckHints overlap ---
    forge_overlap = 0
    if ctx.forge_cmdr_hints or ctx.forge_cmdr_has:
        cand_has = ctx.forge_card_has.get(card_name, set())
        cand_hints = ctx.forge_card_hints.get(card_name, set())
        forge_overlap = len(cand_has & ctx.forge_cmdr_hints) + len(cand_hints & ctx.forge_cmdr_has)
```

And add `forge_overlap * w.get("FORGE_DECK_OVERLAP", 0)` to the total in the `# --- Combine ---` block.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_forge_deck_tags.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/recommend/scoring.py tests/test_forge_deck_tags.py
git commit -m "feat(scoring): add DeckHas/DeckHints overlap scoring from Forge tags"
```

---

## Task 6: Cleanup + Validation

- [ ] **Step 1: Drop old forge_effects table and retire old forge_fallback**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
conn.execute('DROP TABLE IF EXISTS forge_effects')
conn.commit()
print('Dropped old forge_effects table')
conn.close()
"
```

Update `tests/test_forge_fallback.py`: remove `test_forge_schema` test (it creates the now-dropped `forge_effects` table). Keep `test_map_forge_verb_*` and `test_parse_*` tests — they test pure functions that still work. Add a skip marker if the module will be retired in Plan B.

- [ ] **Step 2: Run full test suite**

```bash
python3 -m pytest tests/ -q --tb=short
```

Fix any tests that reference `forge_effects` table. The `test_forge_fallback.py::test_forge_schema` test should be removed or updated to test `forge_abilities` table instead.

- [ ] **Step 3: Verify stats**

```bash
python3 import_forge.py --stats
```

- [ ] **Step 4: Update CLAUDE.md**

Add Forge import to the pipeline docs, update DB schema table.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with Forge data foundation"
```
