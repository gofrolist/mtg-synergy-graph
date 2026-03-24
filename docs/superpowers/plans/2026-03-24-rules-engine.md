# MTG Rules Engine — Implementation Plan (Part 1: Parser + Rules Engine)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic oracle text parser and rules engine that converts MTG card text into structured ASTs and maps effects to game state changes — the foundation for the causal interaction graph.

**Architecture:** A 4-pass regex + template pipeline parses oracle text into AST nodes (Ability, Trigger, Effect, Cost). A rules engine maps each Effect to StateChanges via verb resolver functions. Everything stored in SQLite `parsed_abilities` table. No LLM calls, no external dependencies.

**Tech Stack:** Python 3.14, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-03-24-rules-engine-design.md`

**Scope:** This plan covers Layers 1-2 (Parser + Rules Engine) from the spec. Layers 3-5 (Graph Builder, Chain Discovery, Integration) will be separate plans after this is validated.

---

## File Structure

```
mtg_synergy/
├── parse/                         ← NEW PACKAGE
│   ├── __init__.py                # Public API: parse_card(), parse_oracle_text()
│   ├── ast_types.py               # All dataclasses: Ability, Trigger, Effect, Cost, ObjectFilter, etc.
│   ├── splitter.py                # Pass 1-2: split abilities, classify kind
│   ├── trigger_parser.py          # Pass 3a: extract trigger event + subject filter
│   ├── effect_parser.py           # Pass 3b: extract effect verb + target + amount
│   ├── cost_parser.py             # Pass 3c: extract mana/tap/sacrifice/life costs
│   ├── resolver.py                # Pass 4: resolve cross-references ("it", "that creature")
│   ├── templates.py               # Template library for complex patterns (modal, scaling, etc.)
│   └── verb_resolvers.py          # Rules engine: Effect → list[StateChange]
tests/
├── test_ast_types.py              # AST dataclass construction + serialization
├── test_splitter.py               # Pass 1-2 tests
├── test_trigger_parser.py         # Trigger extraction tests
├── test_effect_parser.py          # Effect extraction tests
├── test_cost_parser.py            # Cost extraction tests
├── test_resolver.py               # Cross-reference resolution tests
├── test_templates.py              # Template matching tests
├── test_verb_resolvers.py         # Rules engine tests
└── test_parse_integration.py      # End-to-end parse_card() tests with real cards
oracle_parser.py                   # CLI entry point for parsing (root-level script)
```

---

### Task 1: AST Type Definitions

**Files:**
- Create: `mtg_synergy/parse/__init__.py`
- Create: `mtg_synergy/parse/ast_types.py`
- Test: `tests/test_ast_types.py`

- [ ] **Step 1: Write failing test for AST types**

```python
# tests/test_ast_types.py
"""Tests for AST dataclass construction and JSON serialization."""
import json
import pytest


def test_object_filter_basic():
    from mtg_synergy.parse.ast_types import ObjectFilter
    f = ObjectFilter(card_type="creature", subtype="Goblin", controller="you")
    assert f.card_type == "creature"
    assert f.subtype == "Goblin"
    assert f.controller == "you"
    assert f.is_token is None
    assert f.is_another is None


def test_object_filter_defaults():
    from mtg_synergy.parse.ast_types import ObjectFilter
    f = ObjectFilter()
    assert f.card_type is None
    assert f.subtype is None
    assert f.controller is None


def test_amount_fixed():
    from mtg_synergy.parse.ast_types import Amount
    a = Amount(value=2)
    assert a.value == 2
    assert a.scales_with is None


def test_amount_variable():
    from mtg_synergy.parse.ast_types import Amount, ScalesWith
    a = Amount(value="X", scales_with=ScalesWith(what="Goblins you control", how="linear"))
    assert a.value == "X"
    assert a.scales_with.how == "linear"


def test_cost_with_sacrifice():
    from mtg_synergy.parse.ast_types import Cost, ObjectFilter, ManaAmount
    c = Cost(
        mana=ManaAmount(total=2, colors={"generic": 2}),
        tap=True,
        sacrifice=ObjectFilter(card_type="creature"),
    )
    assert c.mana.total == 2
    assert c.tap is True
    assert c.sacrifice.card_type == "creature"
    assert c.pay_life is None


def test_trigger():
    from mtg_synergy.parse.ast_types import Trigger, ObjectFilter
    t = Trigger(
        event="enters_the_battlefield",
        subject=ObjectFilter(card_type="creature", subtype="Goblin", controller="you"),
    )
    assert t.event == "enters_the_battlefield"
    assert t.subject.subtype == "Goblin"
    assert t.condition is None


def test_condition_structured():
    from mtg_synergy.parse.ast_types import Condition
    c = Condition(kind="count_threshold", what="creatures you control",
                  comparator=">=", value=3, restrictiveness="mild")
    assert c.kind == "count_threshold"
    assert c.value == 3
    assert c.raw is None


def test_condition_raw_fallback():
    from mtg_synergy.parse.ast_types import Condition
    c = Condition(kind="raw", raw="if you both own and control it",
                  restrictiveness="severe")
    assert c.kind == "raw"
    assert c.raw == "if you both own and control it"


def test_effect_create_token():
    from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef
    e = Effect(
        verb="create",
        amount=Amount(value=2),
        token=TokenDef(card_type="creature", subtype="Goblin",
                       power=1, toughness=1, keywords=[], color="red"),
    )
    assert e.verb == "create"
    assert e.token.subtype == "Goblin"
    assert e.token.power == 1


def test_ability_triggered():
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="deal_damage", amount=Amount(value=2),
                        target=ObjectFilter(controller="opponent"))],
    )
    assert a.kind == "triggered"
    assert a.trigger.event == "enters_the_battlefield"
    assert len(a.effects) == 1
    assert a.restrictions is None


def test_ability_with_restrictions():
    from mtg_synergy.parse.ast_types import Ability, Effect, Amount, Restrictions
    a = Ability(
        kind="activated",
        effects=[Effect(verb="draw", amount=Amount(value=1))],
        restrictions=Restrictions(once_per_turn=True, sorcery_speed=True),
    )
    assert a.restrictions.once_per_turn is True
    assert a.restrictions.sorcery_speed is True
    assert a.restrictions.once_per_game is False


def test_mana_amount():
    from mtg_synergy.parse.ast_types import ManaAmount
    m = ManaAmount(total=5, colors={"G": 1, "generic": 4})
    assert m.total == 5
    assert m.colors["G"] == 1
    assert m.is_any_color is False


def test_mana_amount_any_color():
    from mtg_synergy.parse.ast_types import ManaAmount
    m = ManaAmount(total=1, colors={"any": 1}, is_any_color=True)
    assert m.is_any_color is True


def test_effect_with_condition():
    from mtg_synergy.parse.ast_types import Effect, Amount, Condition
    e = Effect(
        verb="draw", amount=Amount(value=1),
        condition=Condition(kind="raw", raw="unless that player pays {1}",
                            restrictiveness="mild"),
    )
    assert e.condition.kind == "raw"
    assert e.unresolved_ref is None


def test_effect_with_unresolved_ref():
    from mtg_synergy.parse.ast_types import Effect, Amount, ObjectFilter
    e = Effect(verb="return", amount=Amount(value=1),
               target=ObjectFilter(), destination="battlefield",
               unresolved_ref="it")
    assert e.unresolved_ref == "it"


def test_ability_to_dict():
    """AST nodes should be JSON-serializable via to_dict()."""
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    d = a.to_dict()
    assert d["kind"] == "triggered"
    assert d["trigger"]["event"] == "dies"
    # Should round-trip through JSON
    json_str = json.dumps(d)
    assert "dies" in json_str


def test_ability_from_dict():
    """Deserialization: dict → Ability for DB round-trip."""
    from mtg_synergy.parse.ast_types import Ability, Trigger, Effect, Amount, ObjectFilter
    a = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="draw", amount=Amount(value=1))],
    )
    d = a.to_dict()
    restored = Ability.from_dict(d)
    assert restored.kind == "triggered"
    assert restored.trigger.event == "dies"
    assert restored.effects[0].verb == "draw"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ast_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mtg_synergy.parse'`

- [ ] **Step 3: Implement AST types**

Create `mtg_synergy/parse/__init__.py` (empty for now) and `mtg_synergy/parse/ast_types.py` with all dataclasses from the spec:

- `ObjectFilter` — all fields default to `None`
- `Condition` — kind, what, comparator, value, raw, restrictiveness
- `Trigger` — event, subject (ObjectFilter), condition (optional)
- `Amount` — value (int|str), scales_with (optional ScalesWith)
- `ScalesWith` — what, how
- `TokenDef` — card_type, subtype, power, toughness, keywords, color
- `Cost` — mana (ManaAmount|None), tap (default False), sacrifice, pay_life, discard, exile, loyalty, other
- `ManaAmount` — total, colors (dict), is_any_color (default False)
- `Effect` — verb, target, amount, token, keyword, destination, condition (Condition|None, default None), unresolved_ref (str|None, default None)
- `Restrictions` — once_per_turn, sorcery_speed, once_per_game, your_turn_only (all default False)
- `Ability` — kind, trigger, cost, effects, replacement_of, scope, scaling, restrictions

Add `to_dict()` method on `Ability` that recursively converts all dataclass fields to dicts/primitives for JSON serialization.

Add `from_dict(cls, d: dict) -> Ability` classmethod on `Ability` that reconstructs the full dataclass tree from a dict (for DB deserialization in Task 10's `load_parsed`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_ast_types.py -v`
Expected: All 19 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/__init__.py mtg_synergy/parse/ast_types.py tests/test_ast_types.py
git commit -m "feat(parse): add AST type definitions for oracle text parser"
```

---

### Task 2: Ability Splitter (Pass 1-2)

**Files:**
- Create: `mtg_synergy/parse/splitter.py`
- Test: `tests/test_splitter.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_splitter.py
"""Tests for Pass 1 (split) and Pass 2 (classify) of the oracle parser."""
import pytest
from mtg_synergy.parse.splitter import split_abilities


def test_split_simple_newlines():
    """Multiple abilities separated by newlines."""
    text = "Flying\nWhenever a creature enters the battlefield, draw a card."
    abilities = split_abilities(text)
    assert len(abilities) == 2
    assert abilities[0].kind == "keyword"
    assert abilities[0].raw_text == "Flying"
    assert abilities[1].kind == "triggered"


def test_split_activated():
    """{T}: effect is activated."""
    text = "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "activated"
    assert abilities[0].cost_text == "{T}"
    assert "Create" in abilities[0].effect_text


def test_split_activated_complex_cost():
    """{2}, {T}, Sacrifice a creature: effect."""
    text = "{2}, {T}, Sacrifice a creature: Draw a card."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "activated"
    assert "{2}" in abilities[0].cost_text
    assert "Sacrifice" in abilities[0].cost_text
    assert abilities[0].effect_text == "Draw a card."


def test_split_planeswalker():
    """Loyalty abilities split on +N:, -N:, 0:."""
    text = "+1: Create a 3/3 Kavu creature token with trample.\n−3: Put +1/+1 counters on target creature.\n−6: Draw five cards."
    abilities = split_abilities(text)
    assert len(abilities) == 3
    assert abilities[0].kind == "activated"
    assert abilities[0].loyalty_cost == 1
    assert abilities[1].loyalty_cost == -3
    assert abilities[2].loyalty_cost == -6


def test_split_saga():
    """Saga chapters split on I —, II —, etc."""
    text = "I — Destroy target nonland permanent.\nII — Search your library for a Forest card.\nIII — Exile this Saga."
    abilities = split_abilities(text)
    assert len(abilities) == 3
    assert abilities[0].kind == "triggered"  # Saga chapters are triggered
    assert abilities[0].chapter == 1
    assert abilities[1].chapter == 2


def test_split_replacement_effect():
    """If...would...instead is a replacement."""
    text = "If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "replacement"


def test_split_static():
    """No trigger/cost/if-would → static."""
    text = "Creatures you control get +1/+1."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "static"


def test_split_trigger_modifier():
    """Panharmonicon-style trigger doublers."""
    text = "If a permanent entering the battlefield causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "trigger_modifier"


def test_split_dfc():
    """Double-faced card: split on ' // '."""
    text = "At the beginning of your upkeep, look at the top card. // Flying"
    abilities = split_abilities(text)
    assert len(abilities) == 2
    assert abilities[0].kind == "triggered"
    assert abilities[1].kind == "keyword"


def test_split_reminder_text_stripped():
    """Reminder text in parens is stripped but preserved."""
    text = "Discover 5 (Exile cards from the top of your library until you exile a nonland card with mana value 5 or less. Cast it without paying its mana cost or put it into your hand. Put the rest on the bottom in a random order.)"
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "keyword"
    assert abilities[0].reminder_text is not None
    assert "Exile cards" in abilities[0].reminder_text


def test_split_modal():
    """'Choose one —' splits into mode lines."""
    text = "Choose one —\n• Destroy target artifact.\n• Destroy target enchantment."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].kind == "modal"
    assert len(abilities[0].modes) == 2


def test_skip_flavor_rules_text():
    """Commander designation text is skipped."""
    text = "{T}: Add {G}.\nJared Carthalion can be your commander."
    abilities = split_abilities(text)
    assert len(abilities) == 1  # only the mana ability


def test_split_restriction_detection():
    """Restrictions parsed from ability text."""
    text = "{T}: Draw a card. Activate only once each turn."
    abilities = split_abilities(text)
    assert len(abilities) == 1
    assert abilities[0].restrictions_text == "Activate only once each turn."


def test_when_whenever_at():
    """All trigger words classify as triggered."""
    for text in [
        "When this creature enters the battlefield, draw a card.",
        "Whenever a creature dies, gain 1 life.",
        "At the beginning of your upkeep, scry 1.",
    ]:
        abilities = split_abilities(text)
        assert abilities[0].kind == "triggered", f"Failed for: {text}"
```

The `split_abilities()` function returns a list of `RawAbility` (an intermediate type holding the split result before full parsing):

```python
@dataclass
class RawAbility:
    kind: str           # "triggered", "activated", "static", "replacement", "keyword", "trigger_modifier", "modal"
    raw_text: str       # full original text of this ability
    cost_text: str | None        # for activated: the cost portion
    effect_text: str | None      # for activated/triggered: the effect portion
    trigger_text: str | None     # for triggered: the trigger clause
    loyalty_cost: int | None     # for planeswalker abilities
    chapter: int | None          # for saga chapters
    reminder_text: str | None    # extracted from parentheses
    restrictions_text: str | None  # "Activate only once each turn." etc.
    modes: list[str] | None      # for modal abilities: list of mode texts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_splitter.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement splitter**

Create `mtg_synergy/parse/splitter.py`:

1. Define `RawAbility` dataclass
2. `split_abilities(oracle_text: str) -> list[RawAbility]`:
   - Handle DFC: split on ` // ` first, process each face
   - Handle modal: detect `Choose one —` / `Choose two —`, extract `•` bullet lines
   - Split remaining on `\n`
   - For each line:
     - Strip reminder text `(...)` at end, save to `reminder_text`
     - Strip restriction sentences ("Activate only once each turn.", "Activate only as a sorcery.")
     - Detect planeswalker loyalty: `r'^[+−-]?\d+:'`
     - Detect saga chapter: `r'^[IVX]+ —'`
     - Detect trigger modifier: `r'triggers? an additional time'`
     - Detect replacement: `r'\bwould\b.*\binstead\b'`
     - Detect triggered: `r'^(When(ever)?|At the beginning|At end)'`
     - Detect activated: find `:` with cost indicators before it (`{`, "Sacrifice", "Pay", "Discard", "Exile", "Tap", "Remove")
     - Detect keyword: single word or comma-separated keywords (from Scryfall keyword list)
     - Skip: `r'can be your commander'`, `r'partner with'` (flavor rules text)
     - Default: static

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_splitter.py -v`
Expected: All 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/splitter.py tests/test_splitter.py
git commit -m "feat(parse): add ability splitter (pass 1-2)"
```

---

### Task 3: Cost Parser (Pass 3c)

**Files:**
- Create: `mtg_synergy/parse/cost_parser.py`
- Test: `tests/test_cost_parser.py`

Costs first because they're the most self-contained — no dependency on trigger/effect parsing.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cost_parser.py
"""Tests for activation/casting cost parsing."""
import pytest
from mtg_synergy.parse.cost_parser import parse_cost
from mtg_synergy.parse.ast_types import Cost, ManaAmount, ObjectFilter


def test_tap_only():
    c = parse_cost("{T}")
    assert c.tap is True
    assert c.mana is None
    assert c.sacrifice is None


def test_mana_only():
    c = parse_cost("{2}{G}")
    assert c.mana.total == 3
    assert c.mana.colors == {"G": 1, "generic": 2}
    assert c.tap is False


def test_mana_and_tap():
    c = parse_cost("{2}{G}{W}, {T}")
    assert c.mana.total == 4
    assert c.mana.colors == {"G": 1, "W": 1, "generic": 2}
    assert c.tap is True


def test_sacrifice_creature():
    c = parse_cost("Sacrifice a creature")
    assert c.sacrifice is not None
    assert c.sacrifice.card_type == "creature"


def test_sacrifice_typed():
    c = parse_cost("Sacrifice a Goblin")
    assert c.sacrifice.subtype == "Goblin"
    assert c.sacrifice.card_type == "creature"


def test_complex_cost():
    c = parse_cost("{2}, {T}, Sacrifice a creature")
    assert c.mana.total == 2
    assert c.tap is True
    assert c.sacrifice.card_type == "creature"


def test_pay_life():
    c = parse_cost("Pay 3 life")
    assert c.pay_life == 3


def test_discard():
    c = parse_cost("Discard a card")
    assert c.discard is not None


def test_exile_from_graveyard():
    c = parse_cost("Exile a creature card from your graveyard")
    assert c.exile is not None
    assert c.exile.card_type == "creature"
    assert c.exile.zone == "graveyard"


def test_loyalty_cost():
    c = parse_cost("+1", is_loyalty=True)
    assert c.loyalty == 1
    assert c.tap is False


def test_loyalty_negative():
    c = parse_cost("−3", is_loyalty=True)
    assert c.loyalty == -3


def test_mana_symbols():
    """Parse various mana symbol patterns."""
    c = parse_cost("{W}{U}{B}{R}{G}")
    assert c.mana.total == 5
    assert c.mana.colors == {"W": 1, "U": 1, "B": 1, "R": 1, "G": 1}


def test_hybrid_mana():
    c = parse_cost("{W/U}{W/U}")
    assert c.mana.total == 2  # treat hybrid as 1 each


def test_phyrexian_mana():
    c = parse_cost("{B/P}{B/P}")
    assert c.mana.total == 2


def test_x_mana():
    """{X} is stored as colors["X"] = 1. Total mana excludes X (only fixed costs)."""
    c = parse_cost("{X}{R}")
    assert c.mana.colors.get("R") == 1
    assert c.mana.colors.get("X") == 1  # X stored explicitly
    assert c.mana.total == 1  # total counts only fixed mana (R), not X


def test_add_mana_production():
    """parse_mana_production for 'Add {R}' effects."""
    from mtg_synergy.parse.cost_parser import parse_mana_production
    m = parse_mana_production("Add {R}.")
    assert m.total == 1
    assert m.colors == {"R": 1}


def test_add_any_color():
    from mtg_synergy.parse.cost_parser import parse_mana_production
    m = parse_mana_production("Add one mana of any color.")
    assert m.total == 1
    assert m.is_any_color is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cost_parser.py -v`
Expected: FAIL

- [ ] **Step 3: Implement cost parser**

Create `mtg_synergy/parse/cost_parser.py`:

- `parse_cost(cost_text: str, is_loyalty: bool = False) -> Cost`
  - Split on `,` and trim
  - For each segment: detect mana symbols `{X}`, `{T}`, `Sacrifice`, `Pay N life`, `Discard`, `Exile`, loyalty prefix
  - `_parse_mana_symbols(text) -> ManaAmount` — regex for `{W}`, `{U}`, `{B}`, `{R}`, `{G}`, `{C}`, `{N}`, `{X}`, `{W/U}`, `{B/P}`
  - `_parse_sacrifice(text) -> ObjectFilter` — extract what's being sacrificed
- `parse_mana_production(text: str) -> ManaAmount`
  - Parse "Add {R}{R}" or "Add one mana of any color" from effect text

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_cost_parser.py -v`
Expected: All 17 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/cost_parser.py tests/test_cost_parser.py
git commit -m "feat(parse): add cost parser for mana, tap, sacrifice, life costs"
```

---

### Task 4: Trigger Parser (Pass 3a)

**Files:**
- Create: `mtg_synergy/parse/trigger_parser.py`
- Test: `tests/test_trigger_parser.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trigger_parser.py
"""Tests for trigger event + subject filter extraction."""
import pytest
from mtg_synergy.parse.trigger_parser import parse_trigger
from mtg_synergy.parse.ast_types import Trigger, ObjectFilter, Condition


def test_creature_enters():
    t = parse_trigger("Whenever a creature enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "creature"
    assert t.subject.controller == "you"


def test_goblin_enters():
    t = parse_trigger("Whenever a Goblin enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.subtype == "Goblin"
    assert t.subject.controller == "you"


def test_creature_dies():
    t = parse_trigger("Whenever a creature dies")
    assert t.event == "dies"
    assert t.subject.card_type == "creature"


def test_another_creature_dies():
    t = parse_trigger("Whenever another creature dies")
    assert t.event == "dies"
    assert t.subject.is_another is True
    assert t.subject.card_type == "creature"


def test_nontoken_creature_enters():
    t = parse_trigger("Whenever a nontoken creature enters the battlefield")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "creature"
    assert t.subject.is_token is False


def test_upkeep():
    t = parse_trigger("At the beginning of your upkeep")
    assert t.event == "upkeep"
    assert t.subject is None or t.subject.controller == "you"


def test_end_step():
    t = parse_trigger("At the beginning of each end step")
    assert t.event == "end_step"


def test_deals_combat_damage():
    t = parse_trigger("Whenever this creature deals combat damage to a player")
    assert t.event == "deals_combat_damage"
    assert t.subject is not None


def test_spell_cast_opponent():
    t = parse_trigger("Whenever an opponent casts a spell")
    assert t.event == "cast"
    assert t.subject.controller == "opponent"


def test_spell_cast_typed():
    t = parse_trigger("Whenever you cast an instant or sorcery spell")
    assert t.event == "cast"
    assert t.subject.controller == "you"
    # card_type should capture the spell type


def test_attacks():
    t = parse_trigger("Whenever this creature attacks")
    assert t.event == "attacks"


def test_land_enters():
    t = parse_trigger("Whenever a land enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "land"
    assert t.subject.controller == "you"


def test_gain_life():
    t = parse_trigger("Whenever you gain life")
    assert t.event == "life_gained"


def test_discard():
    t = parse_trigger("Whenever a player discards a card")
    assert t.event == "discard"


def test_condition_extracted():
    t = parse_trigger("Whenever a creature enters the battlefield under your control, if you control five or more creatures")
    assert t.event == "enters_the_battlefield"
    assert t.condition is not None
    assert t.condition.restrictiveness in ("mild", "severe")


def test_equipped_creature():
    t = parse_trigger("Whenever equipped creature deals combat damage to a player")
    assert t.event == "deals_combat_damage"
    # "equipped creature" implies the subject has some equipment filter


def test_artifact_enters():
    t = parse_trigger("Whenever an artifact enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "artifact"
    assert t.subject.controller == "you"


def test_enchantment_enters():
    t = parse_trigger("Whenever an enchantment enters the battlefield under your control")
    assert t.event == "enters_the_battlefield"
    assert t.subject.card_type == "enchantment"


def test_counter_placed():
    t = parse_trigger("Whenever one or more +1/+1 counters are put on a creature you control")
    assert t.event == "counter_placed"
    assert t.subject.card_type == "creature"
    assert t.subject.controller == "you"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_trigger_parser.py -v`
Expected: FAIL

- [ ] **Step 3: Implement trigger parser**

Create `mtg_synergy/parse/trigger_parser.py`:

- `parse_trigger(trigger_text: str) -> Trigger`
- Define ~25 event patterns as a list of `(regex, event_name)` tuples, ordered from most specific to least:
  - `r'enters the battlefield'` → `"enters_the_battlefield"`
  - `r'\bdies\b'` → `"dies"`
  - `r'deals? combat damage to a player'` → `"deals_combat_damage"`
  - `r'deals? damage'` → `"deals_damage"`
  - `r'casts? (a|an) spell'` → `"cast"`
  - `r'attacks'` → `"attacks"`
  - `r'beginning of (your |each )?upkeep'` → `"upkeep"`
  - `r'beginning of (your |each )?(end|end step)'` → `"end_step"`
  - `r'beginning of combat'` → `"beginning_of_combat"`
  - `r'gain(s)? life'` → `"life_gained"`
  - `r'lose(s)? life'` → `"life_lost"`
  - `r'discard'` → `"discard"`
  - `r'draw(s)? a card'` → `"card_drawn"`
  - `r'counters? (are|is) (put|placed)'` → `"counter_placed"`
  - `r'leaves the battlefield'` → `"leaves_the_battlefield"`
  - etc.
- `_extract_subject(text_before_event: str) -> ObjectFilter | None` — parse noun phrase:
  - "a Goblin creature" → `ObjectFilter(card_type="creature", subtype="Goblin")`
  - "another creature" → `ObjectFilter(card_type="creature", is_another=True)`
  - "a nontoken creature" → `ObjectFilter(card_type="creature", is_token=False)`
  - "an artifact" → `ObjectFilter(card_type="artifact")`
  - "an opponent" → `ObjectFilter(controller="opponent")`
- `_extract_controller(text: str) -> str | None` — "under your control" → "you", "an opponent" → "opponent"
- `_extract_condition(text: str) -> Condition | None` — look for "if" clause after the trigger event, classify restrictiveness

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_trigger_parser.py -v`
Expected: All 19 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/trigger_parser.py tests/test_trigger_parser.py
git commit -m "feat(parse): add trigger parser with ~25 event patterns"
```

---

### Task 5: Effect Parser (Pass 3b)

**Files:**
- Create: `mtg_synergy/parse/effect_parser.py`
- Test: `tests/test_effect_parser.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_effect_parser.py
"""Tests for effect verb + target + amount extraction."""
import pytest
from mtg_synergy.parse.effect_parser import parse_effects
from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef, ObjectFilter


def test_create_token():
    effects = parse_effects("Create a 1/1 red Goblin creature token.")
    assert len(effects) == 1
    e = effects[0]
    assert e.verb == "create"
    assert e.amount.value == 1
    assert e.token.subtype == "Goblin"
    assert e.token.power == 1
    assert e.token.toughness == 1


def test_create_multiple_tokens():
    effects = parse_effects("Create two 1/1 white Soldier creature tokens.")
    assert effects[0].amount.value == 2
    assert effects[0].token.subtype == "Soldier"


def test_create_x_tokens():
    effects = parse_effects("Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.")
    e = effects[0]
    assert e.amount.value == "X"
    assert e.amount.scales_with is not None
    assert e.amount.scales_with.how == "linear"
    assert "Goblins" in e.amount.scales_with.what


def test_create_token_with_keywords():
    effects = parse_effects("Create a 3/3 Kavu creature token with trample.")
    assert effects[0].token.keywords == ["trample"]
    assert effects[0].token.power == 3


def test_create_treasure():
    effects = parse_effects("Create a Treasure token.")
    assert effects[0].token.subtype == "Treasure"
    assert effects[0].token.card_type == "artifact"


def test_deal_damage_to_opponent():
    effects = parse_effects("Purphoros deals 2 damage to each opponent.")
    e = effects[0]
    assert e.verb == "deal_damage"
    assert e.amount.value == 2
    assert e.target.controller == "opponent"


def test_draw_card():
    effects = parse_effects("Draw a card.")
    assert effects[0].verb == "draw"
    assert effects[0].amount.value == 1


def test_draw_multiple():
    effects = parse_effects("Draw three cards.")
    assert effects[0].verb == "draw"
    assert effects[0].amount.value == 3


def test_destroy_target():
    effects = parse_effects("Destroy target creature.")
    assert effects[0].verb == "destroy"
    assert effects[0].target.card_type == "creature"


def test_exile():
    effects = parse_effects("Exile target permanent.")
    assert effects[0].verb == "exile"
    assert effects[0].target.card_type == "permanent"


def test_return_to_hand():
    effects = parse_effects("Return target creature to its owner's hand.")
    assert effects[0].verb == "return"
    assert effects[0].destination == "hand"
    assert effects[0].target.card_type == "creature"


def test_return_to_battlefield():
    effects = parse_effects("Return target creature card from your graveyard to the battlefield.")
    e = effects[0]
    assert e.verb == "return"
    assert e.destination == "battlefield"


def test_put_counter():
    effects = parse_effects("Put a +1/+1 counter on target creature.")
    e = effects[0]
    assert e.verb == "put_counter"
    assert e.target.card_type == "creature"


def test_put_counter_each():
    effects = parse_effects("Put a +1/+1 counter on each creature you control.")
    e = effects[0]
    assert e.verb == "put_counter"
    assert e.target.controller == "you"


def test_gain_life():
    effects = parse_effects("You gain 3 life.")
    assert effects[0].verb == "gain_life"
    assert effects[0].amount.value == 3


def test_lose_life():
    effects = parse_effects("Target opponent loses 2 life.")
    assert effects[0].verb == "lose_life"
    assert effects[0].amount.value == 2


def test_sacrifice():
    effects = parse_effects("Each opponent sacrifices a creature.")
    assert effects[0].verb == "sacrifice"
    assert effects[0].target.controller == "opponent"


def test_search_library():
    effects = parse_effects("Search your library for a basic land card, put it onto the battlefield tapped, then shuffle.")
    assert effects[0].verb == "search"


def test_mill():
    effects = parse_effects("Target player mills three cards.")
    assert effects[0].verb == "mill"
    assert effects[0].amount.value == 3


def test_add_mana():
    effects = parse_effects("Add {R}.")
    assert effects[0].verb == "add_mana"


def test_multiple_effects():
    """Sentence with multiple effects separated by 'and' or ','."""
    effects = parse_effects("Target player loses 1 life and you gain 1 life.")
    assert len(effects) == 2
    assert effects[0].verb == "lose_life"
    assert effects[1].verb == "gain_life"


def test_pump():
    effects = parse_effects("Creatures you control get +1/+1 until end of turn.")
    assert effects[0].verb == "pump"


def test_grant_keyword():
    effects = parse_effects("Creatures you control have haste.")
    assert effects[0].verb == "grant_keyword"
    assert effects[0].keyword == "haste"


def test_scry():
    effects = parse_effects("Scry 2.")
    assert effects[0].verb == "scry"
    assert effects[0].amount.value == 2


def test_untap():
    effects = parse_effects("Untap target creature.")
    assert effects[0].verb == "untap"
    assert effects[0].target.card_type == "creature"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_effect_parser.py -v`
Expected: FAIL

- [ ] **Step 3: Implement effect parser**

Create `mtg_synergy/parse/effect_parser.py`:

- `parse_effects(effect_text: str) -> list[Effect]`
  - Split on ` and ` / `, then ` for multi-effect sentences
  - For each clause, match verb patterns (ordered most specific to least):
    - `r'[Cc]reate[s]?\s+(.*?)\s+tokens?'` → "create" + parse token def
    - `r'deals?\s+(\d+|X)\s+damage\s+to\s+(.+)'` → "deal_damage"
    - `r'[Dd]raws?\s+(a card|(\w+) cards?)'` → "draw"
    - `r'[Dd]estroy\s+(.+)'` → "destroy"
    - `r'[Ee]xile\s+(.+)'` → "exile"
    - `r'[Rr]eturn\s+(.+?)\s+to\s+(the battlefield|.*hand)'` → "return"
    - `r'[Pp]ut\s+.*counter'` → "put_counter"
    - `r'gains?\s+(\d+)\s+life'` → "gain_life"
    - `r'loses?\s+(\d+)\s+life'` → "lose_life"
    - `r'[Ss]acrifice'` → "sacrifice"
    - `r'[Ss]earch(es)?\s+(your|their)\s+library'` → "search"
    - `r'mills?\s+(\d+|X)\s+cards?'` → "mill"
    - `r'[Aa]dd\s+\{[WUBRGC]'` → "add_mana"
    - `r'get\s+[+-]\d+/[+-]\d+'` → "pump"
    - `r'(have|has|gain)\s+(flying|haste|trample|...)'` → "grant_keyword"
    - `r'[Ss]cry\s+(\d+)'` → "scry"
    - `r'[Uu]ntap'` → "untap"
- `_parse_token_def(text: str) -> TokenDef` — extract P/T, subtype, color, keywords from token description
- `_parse_amount(text: str) -> Amount` — number words ("two"→2, "three"→3, etc.), "X", or digit
- `_parse_target(text: str) -> ObjectFilter` — "target creature", "each opponent", "all creatures you control"
- `_extract_scaling(text: str) -> ScalesWith | None` — "where X is the number of" / "for each"

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_effect_parser.py -v`
Expected: All 25 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/effect_parser.py tests/test_effect_parser.py
git commit -m "feat(parse): add effect parser with ~20 verb patterns"
```

---

### Task 6: Template Library

**Files:**
- Create: `mtg_synergy/parse/templates.py`
- Test: `tests/test_templates.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_templates.py
"""Tests for template-based pattern matching (the 5% escape hatch)."""
import pytest
from mtg_synergy.parse.templates import apply_templates
from mtg_synergy.parse.ast_types import ScalesWith


def test_for_each_scaling():
    result = apply_templates("deal damage equal to the number of creatures you control")
    assert result.scaling is not None
    assert result.scaling.what == "creatures you control"
    assert result.scaling.how == "linear"


def test_where_x_is():
    result = apply_templates("Create X tokens, where X is the number of Goblins you control")
    assert result.scaling is not None
    assert "Goblins" in result.scaling.what
    assert result.scaling.how == "linear"


def test_double_template():
    """'double the number of' → multiplicative scaling."""
    result = apply_templates("double the number of +1/+1 counters on target creature")
    assert result.scaling is not None
    assert result.scaling.how == "multiplicative"


def test_that_many_plus_one():
    """Hardened Scales: 'that many plus one' → linear +1."""
    result = apply_templates("that many plus one +1/+1 counters are placed on it instead")
    assert result.scaling is not None
    assert result.scaling.how == "linear"


def test_twice_that_many():
    """Doubling Season: 'twice that many' → multiplicative."""
    result = apply_templates("twice that many of those tokens instead")
    assert result.scaling is not None
    assert result.scaling.how == "multiplicative"


def test_no_template_match():
    """Text that doesn't match any template returns None."""
    result = apply_templates("Draw a card.")
    assert result is None


def test_additional_time():
    """Panharmonicon: 'triggers an additional time'."""
    result = apply_templates("that ability triggers an additional time")
    assert result.kind == "trigger_modifier"


def test_reminder_text_decomposition():
    """Extract base effects from keyword reminder text."""
    from mtg_synergy.parse.templates import decompose_reminder
    effects = decompose_reminder("Draw a card, then discard a card. If you discarded a nonland card, put a +1/+1 counter on this creature.")
    assert len(effects) >= 2  # draw + discard at minimum
    verbs = [e.verb for e in effects]
    assert "draw" in verbs
    assert "discard" in verbs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_templates.py -v`
Expected: FAIL

- [ ] **Step 3: Implement templates**

Create `mtg_synergy/parse/templates.py`:

- Define `TemplateResult` dataclass: `kind: str | None` (for trigger_modifier etc.), `scaling: ScalesWith | None`, `effects: list[Effect] | None`
- `apply_templates(text: str) -> TemplateResult | None` — try each template regex, return match or None
- `decompose_reminder(text: str) -> list[Effect]` — parse reminder text by splitting on `. ` or `, then ` and running effect_parser on each clause
- Template list (regex → handler):
  - `r'for each (.+?) (you control|in your graveyard|in your hand)'` → ScalesWith(linear)
  - `r'where X is the number of (.+)'` → ScalesWith(linear)
  - `r'equal to the number of (.+)'` → ScalesWith(linear)
  - `r'double the number'` → ScalesWith(multiplicative)
  - `r'twice that many'` → ScalesWith(multiplicative)
  - `r'that many plus one'` → ScalesWith(linear, +1 modifier)
  - `r'triggers? an additional time'` → trigger_modifier kind
  - `r'Choose (one|two|three)'` → modal split

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_templates.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/templates.py tests/test_templates.py
git commit -m "feat(parse): add template library for complex patterns"
```

---

### Task 7: Cross-Reference Resolver (Pass 4)

**Files:**
- Create: `mtg_synergy/parse/resolver.py`
- Test: `tests/test_resolver.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_resolver.py
"""Tests for cross-reference resolution (Pass 4)."""
import pytest
from mtg_synergy.parse.resolver import resolve_references
from mtg_synergy.parse.ast_types import (
    Ability, Trigger, Effect, Amount, ObjectFilter, TokenDef
)


def test_it_refers_to_trigger_subject():
    """'When a creature dies, return it to the battlefield' → 'it' = creature."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="dies", subject=ObjectFilter(card_type="creature")),
        effects=[Effect(verb="return", amount=Amount(value=1),
                        target=ObjectFilter(),  # empty = unresolved
                        destination="battlefield",
                        unresolved_ref="it")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_that_creature():
    """'that creature' refers to the trigger subject."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield",
                        subject=ObjectFilter(card_type="creature", subtype="Goblin")),
        effects=[Effect(verb="put_counter", amount=Amount(value=1),
                        target=ObjectFilter(),
                        unresolved_ref="that creature")],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"


def test_those_tokens():
    """'those tokens' refers to tokens created by previous effect."""
    ability = Ability(
        kind="triggered",
        trigger=Trigger(event="enters_the_battlefield", subject=ObjectFilter()),
        effects=[
            Effect(verb="create", amount=Amount(value=2),
                   token=TokenDef(card_type="creature", subtype="Goblin",
                                  power=1, toughness=1, keywords=[], color="red")),
            Effect(verb="pump", amount=Amount(value=1),
                   target=ObjectFilter(),
                   unresolved_ref="those tokens"),
        ],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[1].target.card_type == "creature"


def test_no_refs_unchanged():
    """Ability with no cross-references passes through unchanged."""
    ability = Ability(
        kind="static",
        effects=[Effect(verb="pump", amount=Amount(value=1),
                        target=ObjectFilter(card_type="creature", controller="you"))],
    )
    resolved = resolve_references(ability)
    assert resolved.effects[0].target.card_type == "creature"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_resolver.py -v`
Expected: FAIL

- [ ] **Step 3: Implement resolver**

Create `mtg_synergy/parse/resolver.py`:

- `resolve_references(ability: Ability) -> Ability`
  - Build a "context stack": trigger subject is the primary referent
  - For each effect in order, detect unresolved targets by inspecting `effect.target`:
    - If target is an `ObjectFilter` with all fields `None` (empty filter), check the raw effect text (stored as `effect._raw_text` or by checking the original ability text) for pronouns
    - Alternatively, the simpler approach: check if `effect.target` has a special `unresolved` field set by the effect parser when it encounters "it", "that creature", etc.
  - Resolution rules:
    - "it", "that creature", "that card" → copy trigger subject's ObjectFilter
    - "those tokens", "those creatures" → copy previous effect's output filter
    - "its controller" → set controller field
  - Return new Ability with resolved targets

**How pronouns flow through the pipeline:** The effect parser (`parse_effects`) sets `effect.unresolved_ref = "it"` (or similar) when it encounters a pronoun instead of a concrete target. The resolver reads this field and resolves it. Add `unresolved_ref: str | None = None` to the `Effect` dataclass.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_resolver.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/resolver.py tests/test_resolver.py
git commit -m "feat(parse): add cross-reference resolver (pass 4)"
```

---

### Task 8: Verb Resolvers (Rules Engine)

**Files:**
- Create: `mtg_synergy/parse/verb_resolvers.py`
- Test: `tests/test_verb_resolvers.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_verb_resolvers.py
"""Tests for Effect → StateChange mapping (rules engine)."""
import pytest
from mtg_synergy.parse.verb_resolvers import resolve_effect, StateChange
from mtg_synergy.parse.ast_types import Effect, Amount, TokenDef, ObjectFilter


def test_create_creature_token():
    """Create goblin token → enters_the_battlefield + creature_enters."""
    effect = Effect(
        verb="create", amount=Amount(value=2),
        token=TokenDef(card_type="creature", subtype="Goblin",
                       power=1, toughness=1, keywords=[], color="red"),
    )
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "creature_enters" in events
    # Should carry the Goblin filter
    etb = [sc for sc in changes if sc.event == "enters_the_battlefield"][0]
    assert etb.object.subtype == "Goblin"
    assert etb.object.is_token is True
    assert etb.quantity.value == 2


def test_create_treasure_token():
    effect = Effect(
        verb="create", amount=Amount(value=1),
        token=TokenDef(card_type="artifact", subtype="Treasure",
                       power=None, toughness=None, keywords=[], color=None),
    )
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "artifact_enters" in events
    assert "creature_enters" not in events


def test_destroy_creature():
    effect = Effect(verb="destroy", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "dies" in events
    assert "leaves_the_battlefield" in events
    assert "enters_graveyard" in events


def test_destroy_noncreature():
    """Destroying an artifact doesn't produce 'dies' (only creatures die)."""
    effect = Effect(verb="destroy", amount=Amount(value=1),
                    target=ObjectFilter(card_type="artifact"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "dies" not in events
    assert "leaves_the_battlefield" in events


def test_sacrifice():
    """Sacrifice = dies (controlled by you)."""
    effect = Effect(verb="sacrifice", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    dies = [sc for sc in changes if sc.event == "dies"]
    assert len(dies) == 1
    assert dies[0].controller == "you"


def test_deal_damage_to_player():
    effect = Effect(verb="deal_damage", amount=Amount(value=3),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "damage_dealt" in events
    assert "life_lost" in events


def test_deal_damage_to_creature():
    effect = Effect(verb="deal_damage", amount=Amount(value=3),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "damage_dealt" in events
    assert "may_die" in events
    assert "life_lost" not in events


def test_draw():
    effect = Effect(verb="draw", amount=Amount(value=2))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "card_drawn" in events
    assert changes[0].quantity.value == 2


def test_discard():
    effect = Effect(verb="discard", amount=Amount(value=1))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "card_discarded" in events
    assert "enters_graveyard" in events


def test_exile():
    effect = Effect(verb="exile", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "leaves_the_battlefield" in events
    assert "enters_exile" in events
    assert "dies" not in events  # exile ≠ dies


def test_return_to_battlefield():
    effect = Effect(verb="return", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"),
                    destination="battlefield")
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "enters_the_battlefield" in events
    assert "creature_enters" in events


def test_return_to_hand():
    effect = Effect(verb="return", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"),
                    destination="hand")
    changes = resolve_effect(effect)
    events = {sc.event for sc in changes}
    assert "leaves_the_battlefield" in events
    assert "enters_the_battlefield" not in events


def test_put_counter():
    effect = Effect(verb="put_counter", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    assert any(sc.event == "counter_placed" for sc in changes)


def test_gain_life():
    effect = Effect(verb="gain_life", amount=Amount(value=3))
    changes = resolve_effect(effect)
    assert any(sc.event == "life_gained" for sc in changes)


def test_lose_life():
    effect = Effect(verb="lose_life", amount=Amount(value=2),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    assert any(sc.event == "life_lost" for sc in changes)


def test_mill():
    effect = Effect(verb="mill", amount=Amount(value=3),
                    target=ObjectFilter(controller="opponent"))
    changes = resolve_effect(effect)
    assert any(sc.event == "enters_graveyard" for sc in changes)


def test_add_mana():
    """Add mana has no state change events (it's a resource, not a trigger)."""
    effect = Effect(verb="add_mana", amount=Amount(value=1))
    changes = resolve_effect(effect)
    assert len(changes) == 0  # mana production isn't an event


def test_untap():
    effect = Effect(verb="untap", amount=Amount(value=1),
                    target=ObjectFilter(card_type="creature"))
    changes = resolve_effect(effect)
    assert any(sc.event == "untapped" for sc in changes)


def test_unknown_verb():
    """Unknown verbs produce no state changes (graceful degradation)."""
    effect = Effect(verb="unknown_ability", amount=Amount(value=1))
    changes = resolve_effect(effect)
    assert len(changes) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_verb_resolvers.py -v`
Expected: FAIL

- [ ] **Step 3: Implement verb resolvers**

Create `mtg_synergy/parse/verb_resolvers.py`:

- Define `StateChange` dataclass (event, object, zone_from, zone_to, quantity, controller)
- `resolve_effect(effect: Effect) -> list[StateChange]`
  - Dispatch to verb-specific resolver based on `effect.verb`
- One resolver function per verb (~20 verbs initially):
  - `_resolve_create` — ETB + type-specific event (creature_enters, artifact_enters)
  - `_resolve_destroy` — LTB + dies (if creature) + enters_graveyard
  - `_resolve_sacrifice` — same as destroy but controller=you
  - `_resolve_deal_damage` — damage_dealt + life_lost (player) or may_die (creature)
  - `_resolve_draw` — card_drawn
  - `_resolve_discard` — card_discarded + enters_graveyard
  - `_resolve_exile` — LTB + enters_exile (no "dies")
  - `_resolve_return` — zone-dependent: ETB if battlefield, LTB if from battlefield
  - `_resolve_put_counter` — counter_placed
  - `_resolve_gain_life` — life_gained
  - `_resolve_lose_life` — life_lost
  - `_resolve_mill` — enters_graveyard
  - `_resolve_untap` — untapped
  - `_resolve_add_mana` — empty (mana is resource, not event)
  - etc.
- Unknown verbs return empty list (graceful degradation)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_verb_resolvers.py -v`
Expected: All 20 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/verb_resolvers.py tests/test_verb_resolvers.py
git commit -m "feat(parse): add verb resolvers (rules engine: Effect → StateChange)"
```

---

### Task 9: Full Pipeline Integration (parse_card)

**Files:**
- Modify: `mtg_synergy/parse/__init__.py`
- Test: `tests/test_parse_integration.py`

- [ ] **Step 1: Write failing integration tests**

```python
# tests/test_parse_integration.py
"""End-to-end tests: raw oracle text → fully parsed Ability list."""
import pytest
from mtg_synergy.parse import parse_card


def test_purphoros():
    """Triggered: creature enters → deal 2 damage to each opponent."""
    abilities = parse_card(
        oracle_text="Whenever a creature enters the battlefield under your control, Purphoros deals 2 damage to each opponent.",
        type_line="Legendary Enchantment Creature — God",
        mana_cost="{3}{R}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "enters_the_battlefield"
    assert a.trigger.subject.card_type == "creature"
    assert a.trigger.subject.controller == "you"
    assert a.effects[0].verb == "deal_damage"
    assert a.effects[0].amount.value == 2


def test_krenko():
    """Activated: tap → create X Goblin tokens."""
    abilities = parse_card(
        oracle_text="{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        type_line="Legendary Creature — Goblin Warrior",
        mana_cost="{2}{R}{R}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "activated"
    assert a.cost.tap is True
    assert a.effects[0].verb == "create"
    assert a.effects[0].token.subtype == "Goblin"
    assert a.effects[0].amount.value == "X"
    assert a.effects[0].amount.scales_with is not None


def test_hardened_scales():
    """Replacement: +1/+1 counters → that many plus one."""
    abilities = parse_card(
        oracle_text="If one or more +1/+1 counters would be placed on a creature you control, that many plus one +1/+1 counters are placed on it instead.",
        type_line="Enchantment",
        mana_cost="{G}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "replacement"


def test_blood_artist():
    """Triggered: creature dies → drain 1."""
    abilities = parse_card(
        oracle_text="Whenever Blood Artist or another creature dies, target player loses 1 life and you gain 1 life.",
        type_line="Creature — Vampire",
        mana_cost="{1}{B}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "dies"
    assert len(a.effects) == 2  # lose_life + gain_life


def test_phyrexian_altar():
    """Activated: sacrifice creature → add mana of any color."""
    abilities = parse_card(
        oracle_text="Sacrifice a creature: Add one mana of any color.",
        type_line="Artifact",
        mana_cost="{3}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "activated"
    assert a.cost.sacrifice is not None
    assert a.cost.sacrifice.card_type == "creature"
    assert a.effects[0].verb == "add_mana"


def test_syr_konrad():
    """Triggered: creature dies/leaves/mills → 1 damage to each opponent."""
    abilities = parse_card(
        oracle_text="Whenever another creature dies, or a creature card is put into a graveyard from anywhere other than the battlefield, or a creature card leaves your graveyard, Syr Konrad, the Grim deals 1 damage to each opponent.\n{1}{B}: Each player mills a card.",
        type_line="Legendary Creature — Human Knight",
        mana_cost="{3}{B}{B}",
    )
    assert len(abilities) >= 2
    # First ability: triggered with multiple events
    assert abilities[0].kind == "triggered"
    assert abilities[0].effects[0].verb == "deal_damage"
    # Second ability: activated
    assert abilities[1].kind == "activated"
    assert abilities[1].effects[0].verb == "mill"


def test_panharmonicon():
    """Trigger modifier: ETB triggers fire additional time."""
    abilities = parse_card(
        oracle_text="If a permanent entering the battlefield causes a triggered ability of a permanent you control to trigger, that ability triggers an additional time.",
        type_line="Artifact",
        mana_cost="{4}",
    )
    assert len(abilities) >= 1
    assert abilities[0].kind == "trigger_modifier"


def test_rhystic_study():
    """Triggered with condition: opponent casts spell, unless they pay 1."""
    abilities = parse_card(
        oracle_text="Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.",
        type_line="Enchantment",
        mana_cost="{2}{U}",
    )
    assert len(abilities) >= 1
    a = abilities[0]
    assert a.kind == "triggered"
    assert a.trigger.event == "cast"
    assert a.trigger.subject.controller == "opponent"
    assert a.effects[0].verb == "draw"


def test_kyler_two_abilities():
    """Card with two distinct abilities on separate lines."""
    abilities = parse_card(
        oracle_text="Whenever a Human enters the battlefield under your control, put a +1/+1 counter on Kyler, Sigardian Emissary.\nHuman creatures you control get +1/+1 for each +1/+1 counter on Kyler.",
        type_line="Legendary Creature — Human Cleric",
        mana_cost="{3}{G}{W}",
    )
    assert len(abilities) == 2
    assert abilities[0].kind == "triggered"
    assert abilities[0].trigger.subject.subtype == "Human"
    assert abilities[1].kind == "static"


def test_jace_planeswalker():
    """Planeswalker with 4 loyalty abilities."""
    abilities = parse_card(
        oracle_text="+2: Look at the top card of target player's library. You may put that card on the bottom of that player's library.\n0: Draw three cards, then put two cards from your hand on top of your library.\n−1: Return target creature to its owner's hand.\n−12: Exile all cards from target player's library, then that player shuffles their hand into their library.",
        type_line="Legendary Planeswalker — Jace",
        mana_cost="{2}{U}{U}",
    )
    assert len(abilities) == 4
    assert abilities[0].cost.loyalty == 2
    assert abilities[1].cost.loyalty == 0
    assert abilities[2].cost.loyalty == -1
    assert abilities[3].cost.loyalty == -12
    # -1 ability returns creature to hand
    assert abilities[2].effects[0].verb == "return"
    assert abilities[2].effects[0].destination == "hand"


def test_once_per_turn_restriction():
    """Abilities with 'Activate only once each turn' get restrictions."""
    abilities = parse_card(
        oracle_text="{T}: Draw a card. Activate only once each turn.",
        type_line="Artifact",
        mana_cost="{2}",
    )
    assert abilities[0].restrictions is not None
    assert abilities[0].restrictions.once_per_turn is True


def test_sorcery_speed_restriction():
    abilities = parse_card(
        oracle_text="{T}: Add {C}{C}. Activate only as a sorcery.",
        type_line="Land",
        mana_cost="",
    )
    assert abilities[0].restrictions is not None
    assert abilities[0].restrictions.sorcery_speed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_parse_integration.py -v`
Expected: FAIL

- [ ] **Step 3: Implement parse_card pipeline**

Update `mtg_synergy/parse/__init__.py`:

```python
"""Oracle text parser — deterministic, zero-cost card text analysis."""
from mtg_synergy.parse.ast_types import Ability
from mtg_synergy.parse.splitter import split_abilities
from mtg_synergy.parse.trigger_parser import parse_trigger
from mtg_synergy.parse.effect_parser import parse_effects
from mtg_synergy.parse.cost_parser import parse_cost
from mtg_synergy.parse.resolver import resolve_references
from mtg_synergy.parse.templates import apply_templates


def parse_card(oracle_text: str, type_line: str = "", mana_cost: str = "") -> list[Ability]:
    """Parse a card's oracle text into a list of structured Abilities.

    4-pass pipeline:
      Pass 1-2: Split into raw abilities, classify kind
      Pass 3:   Extract trigger/effect/cost details
      Pass 4:   Resolve cross-references
    """
    if not oracle_text or not oracle_text.strip():
        return []

    raw_abilities = split_abilities(oracle_text)
    abilities = []

    for raw in raw_abilities:
        # Pass 3a: Parse trigger
        trigger = None
        if raw.kind == "triggered" and raw.trigger_text:
            trigger = parse_trigger(raw.trigger_text)

        # Pass 3b: Parse effects
        effects = []
        if raw.effect_text:
            effects = parse_effects(raw.effect_text)
        elif raw.kind == "keyword":
            # keyword abilities may not have effect_text
            pass

        # Pass 3c: Parse cost
        cost = None
        if raw.cost_text:
            is_loyalty = raw.loyalty_cost is not None
            cost = parse_cost(raw.cost_text, is_loyalty=is_loyalty)
        if raw.loyalty_cost is not None and cost:
            cost.loyalty = raw.loyalty_cost

        # Apply templates for complex patterns
        template_result = apply_templates(raw.raw_text)
        # ... merge template result with parsed data

        # Parse restrictions
        restrictions = _parse_restrictions(raw.restrictions_text)

        ability = Ability(
            kind=raw.kind,
            trigger=trigger,
            cost=cost,
            effects=effects,
            restrictions=restrictions,
        )

        # Pass 4: Resolve cross-references
        ability = resolve_references(ability)

        abilities.append(ability)

    return abilities
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_parse_integration.py -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Run ALL tests to check for regressions**

Run: `python3 -m pytest tests/ -v`
Expected: All existing tests PASS + all new tests PASS

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/parse/__init__.py tests/test_parse_integration.py
git commit -m "feat(parse): wire up full parse_card() pipeline"
```

---

### Task 10: DB Storage + CLI Entry Point

**Files:**
- Create: `oracle_parser.py` (root-level CLI script)
- Modify: `mtg_synergy/parse/__init__.py` (add save_to_db)
- Test: `tests/test_parse_integration.py` (add DB storage test)

- [ ] **Step 1: Write failing test for DB storage**

Add to `tests/test_parse_integration.py`:

```python
def test_save_and_load_parsed_abilities(tmp_db):
    """Parsed abilities round-trip through SQLite."""
    from mtg_synergy.parse import parse_card, save_parsed, load_parsed, ensure_parse_schema
    import sqlite3

    conn = sqlite3.connect(tmp_db)
    ensure_parse_schema(conn)

    abilities = parse_card(
        oracle_text="Whenever a creature enters the battlefield under your control, draw a card.",
        type_line="Enchantment",
        mana_cost="{2}{U}",
    )

    save_parsed(conn, "test-oracle-id", abilities)
    loaded = load_parsed(conn, "test-oracle-id")

    assert len(loaded) == len(abilities)
    assert loaded[0].kind == "triggered"
    assert loaded[0].trigger.event == "enters_the_battlefield"
    assert loaded[0].effects[0].verb == "draw"
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_parse_integration.py::test_save_and_load_parsed_abilities -v`
Expected: FAIL

- [ ] **Step 3: Implement DB storage**

Add to `mtg_synergy/parse/__init__.py`:

- `ensure_parse_schema(conn)` — CREATE TABLE `parsed_abilities` if not exists (schema from spec)
- `save_parsed(conn, oracle_id, abilities)` — serialize each Ability to JSON, INSERT into `parsed_abilities`
- `load_parsed(conn, oracle_id) -> list[Ability]` — SELECT + deserialize JSON back to Ability objects

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_parse_integration.py::test_save_and_load_parsed_abilities -v`
Expected: PASS

- [ ] **Step 5: Create CLI entry point**

Create `oracle_parser.py` (root-level):

```python
#!/usr/bin/env python3
"""CLI for the oracle text parser.

Usage:
    python3 oracle_parser.py --card "Krenko, Mob Boss" --verbose
    python3 oracle_parser.py --parse-all --top 5000
    python3 oracle_parser.py --stats
"""
import argparse
import json
import sqlite3
import sys

from mtg_synergy.config import DB_PATH, CARDS_JSON
from mtg_synergy.db import get_connection
from mtg_synergy.parse import parse_card, save_parsed, ensure_parse_schema


def main():
    parser = argparse.ArgumentParser(description="Oracle text parser")
    parser.add_argument("--card", help="Parse a single card by name")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--parse-all", action="store_true", help="Parse top N cards")
    parser.add_argument("--top", type=int, default=5000, help="Number of cards to parse")
    parser.add_argument("--stats", action="store_true", help="Show parse coverage stats")
    args = parser.parse_args()

    conn = get_connection()
    ensure_parse_schema(conn)

    if args.card:
        _parse_single(conn, args.card, args.verbose)
    elif args.parse_all:
        _parse_batch(conn, args.top)
    elif args.stats:
        _show_stats(conn)

    conn.close()


def _parse_single(conn, card_name, verbose):
    """Parse a single card and optionally print AST."""
    row = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, mana_cost FROM cards WHERE name = ?",
        (card_name,)
    ).fetchone()
    if not row:
        print(f"Card not found: {card_name}")
        return
    oracle_id, name, oracle_text, type_line, mana_cost = row
    abilities = parse_card(oracle_text or "", type_line or "", mana_cost or "")
    save_parsed(conn, oracle_id, abilities)
    conn.commit()
    print(f"Parsed {name}: {len(abilities)} abilities")
    if verbose:
        for i, a in enumerate(abilities):
            print(f"  [{i}] {a.kind}: {a.to_dict()}")


def _parse_batch(conn, top_n):
    """Parse top N cards by EDHREC rank."""
    rows = conn.execute(
        "SELECT oracle_id, name, oracle_text, type_line, mana_cost FROM cards "
        "WHERE oracle_text IS NOT NULL AND oracle_text != '' "
        "ORDER BY edhrec_rank ASC NULLS LAST LIMIT ?",
        (top_n,)
    ).fetchall()
    parsed, failed = 0, 0
    for oracle_id, name, oracle_text, type_line, mana_cost in rows:
        try:
            abilities = parse_card(oracle_text, type_line or "", mana_cost or "")
            save_parsed(conn, oracle_id, abilities)
            parsed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name}: {e}", file=sys.stderr)
        if parsed % 500 == 0:
            conn.commit()
            print(f"  Parsed {parsed}/{top_n}...")
    conn.commit()
    print(f"Done: {parsed} parsed, {failed} failed out of {len(rows)}")


def _show_stats(conn):
    """Show parse coverage statistics."""
    total_cards = conn.execute("SELECT COUNT(*) FROM cards WHERE oracle_text IS NOT NULL AND oracle_text != ''").fetchone()[0]
    parsed_cards = conn.execute("SELECT COUNT(DISTINCT oracle_id) FROM parsed_abilities").fetchone()[0]
    total_abilities = conn.execute("SELECT COUNT(*) FROM parsed_abilities").fetchone()[0]
    print(f"Total cards with oracle text: {total_cards}")
    print(f"Parsed cards:                 {parsed_cards} ({100*parsed_cards/max(total_cards,1):.1f}%)")
    print(f"Total abilities parsed:       {total_abilities}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add oracle_parser.py mtg_synergy/parse/__init__.py tests/test_parse_integration.py
git commit -m "feat(parse): add DB storage and CLI entry point for oracle parser"
```

---

### Task 11: Parse Coverage Validation

**Files:**
- No new files — uses `oracle_parser.py` against real data

This task validates the parser against real card data. It requires `data/oracle_cards.json` to exist (run `python3 download_cards.py` first if needed).

- [ ] **Step 1: Parse a sample of 50 well-known cards**

Run: `python3 oracle_parser.py --card "Purphoros, God of the Forge" --verbose`
Run: `python3 oracle_parser.py --card "Krenko, Mob Boss" --verbose`
Run: `python3 oracle_parser.py --card "Hardened Scales" --verbose`
Run: `python3 oracle_parser.py --card "Blood Artist" --verbose`
Run: `python3 oracle_parser.py --card "Panharmonicon" --verbose`

Verify each card's AST output matches expectations. Fix parser bugs found during validation.

- [ ] **Step 2: Run batch parse on top 500 cards**

Run: `python3 oracle_parser.py --parse-all --top 500`

Check output: how many failed? Review the failures — are they parser bugs or genuinely unparseable text?

- [ ] **Step 3: Check coverage stats**

Run: `python3 oracle_parser.py --stats`

Expected: ≥85% of the top 500 cards successfully parsed. If below 85%, iterate on the parser (add templates, fix regex) until the threshold is met.

- [ ] **Step 4: Expand to top 5000 once 500 passes**

Run: `python3 oracle_parser.py --parse-all --top 5000`
Run: `python3 oracle_parser.py --stats`

Target: ≥85% parsed. Document any card patterns that consistently fail — these inform future template additions.

- [ ] **Step 5: Commit coverage results**

Commit any parser fixes made during validation:

```bash
git add mtg_synergy/parse/ tests/
git commit -m "feat(parse): validate parser coverage on top 5000 cards"
```

---

## What Comes Next (Part 2 plan — after this is validated)

Part 2 will cover:
- **Task 12-14:** Graph builder (Layer 3) — index producers/responders, build trigger/feeds/amplifies/enables edges, store in `interaction_edges` table
- **Task 15-17:** Chain discoverer (Layer 4) — linear chain DFS, loop detection with resource flow, chain ranking + Spellbook validation
- **Task 18-19:** Integration (Layer 5) — add `causal_score()` to recommendation pipeline, EDHREC regression testing, migration from mechanics_matcher

Part 2 will be planned after Part 1 passes the coverage gate (≥85% of top 5000 cards parsed correctly).
