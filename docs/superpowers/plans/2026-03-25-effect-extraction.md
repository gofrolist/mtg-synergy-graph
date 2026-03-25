# Effect Extraction Improvement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce empty-effect rate from 55% to under 25% by adding a text pre-processor to normalize effect text before verb matching, adding modal ability parsing, and integrating Forge DSL as fallback.

**Architecture:** A `_normalize_effect_text()` function in `effect_parser.py` applies 6 ordered text transformations (strip "you may", extract conditionals, normalize subjects, capitalize verbs, split multi-step, strip "for each") before passing to existing `_parse_single_effect()`. Modal modes are parsed in `parse_card()`. Forge DSL provides fallback effects for cards the parser can't handle.

**Tech Stack:** Python 3, regex, pytest, Forge card scripts (public GitHub repo)

**Spec:** `docs/superpowers/specs/2026-03-25-effect-extraction-design.md`

---

## File Structure

| File | Responsibility | Status |
|------|---------------|--------|
| `mtg_synergy/parse/effect_parser.py` | Add `_normalize_effect_text()` + 6 rule functions | Modify |
| `mtg_synergy/parse/ast_types.py` | Add `optional: bool = False` to Effect | Modify |
| `mtg_synergy/parse/__init__.py` | Add modal mode parsing in `parse_card()` | Modify |
| `mtg_synergy/parse/forge_fallback.py` | Forge verb mapping + Effect lookup | Create |
| `import_forge.py` | Download + parse Forge card scripts | Create |
| `tests/test_effect_normalizer.py` | Unit tests for normalizer rules | Create |
| `tests/test_modal_parsing.py` | Modal ability parsing tests | Create |
| `tests/test_forge_fallback.py` | Forge mapping + lookup tests | Create |

---

## Task 1: Add `optional` Field to Effect Dataclass

**Files:**
- Modify: `mtg_synergy/parse/ast_types.py:80-89`

- [ ] **Step 1: Add the field**

In `ast_types.py`, add `optional: bool = False` to the `Effect` dataclass after `unresolved_ref`:

```python
@dataclass
class Effect:
    verb: str = ""
    target: Optional[ObjectFilter] = None
    amount: Optional[Amount] = None
    token: Optional[TokenDef] = None
    keyword: Optional[str] = None
    destination: Optional[str] = None
    condition: Optional[Condition] = None
    unresolved_ref: Optional[str] = None
    optional: bool = False
```

- [ ] **Step 2: Run tests to verify no regressions**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All 326 tests PASS (default `False` is backward-compatible).

- [ ] **Step 3: Commit**

```bash
git add mtg_synergy/parse/ast_types.py
git commit -m "feat(parse): add optional field to Effect dataclass"
```

---

## Task 2: Effect Text Pre-processor (Rules 1-4, 6)

**Files:**
- Modify: `mtg_synergy/parse/effect_parser.py:33-52`
- Test: `tests/test_effect_normalizer.py` (create)

- [ ] **Step 1: Write failing tests**

Create `tests/test_effect_normalizer.py`:

```python
"""Tests for effect text normalization rules."""
import pytest
from mtg_synergy.parse.effect_parser import _normalize_effect_text


# --- Rule 1: Strip "you may" ---

def _texts(result):
    """Extract just the text strings from normalize result tuples."""
    return [r[0] if isinstance(r, tuple) else r for r in result]


def test_strip_you_may():
    result = _normalize_effect_text("you may search your library for a card")
    texts = _texts(result)
    assert any("search" in r.lower() for r in texts)
    for r in texts:
        assert not r.lower().startswith("you may")
    # All parts should be marked optional
    assert all(r[1] for r in result)


def test_strip_you_might():
    result = _normalize_effect_text("you might reveal the top card")
    texts = _texts(result)
    for r in texts:
        assert not r.lower().startswith("you might")


def test_strip_you_may_preserves_verb():
    result = _normalize_effect_text("you may draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


# --- Rule 2: Extract conditional ---

def test_extract_conditional():
    result = _normalize_effect_text("if the player doesn't, you create a Treasure token")
    texts = _texts(result)
    assert any("create" in r.lower() for r in texts)


def test_extract_conditional_simple():
    result = _normalize_effect_text("if you do, exile it")
    texts = _texts(result)
    assert any("exile" in r.lower() for r in texts)


def test_extract_conditional_nested():
    result = _normalize_effect_text("if you control a creature, if it's your turn, draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


def test_conditional_max_iterations():
    """Deeply nested conditionals should not infinite loop."""
    result = _normalize_effect_text("if a, if b, if c, if d, draw a card")
    assert len(result) >= 1


# --- Rule 3: Normalize subject (tested in Task 3 after split) ---
# Rule 3 runs per-part after Rule 5 split, tested in Task 3

# --- Rule 4: Normalize "you verb" ---

def test_normalize_you_create():
    result = _normalize_effect_text("you create a Treasure token")
    texts = _texts(result)
    assert any(r.startswith("Create") for r in texts)


def test_normalize_you_destroy():
    result = _normalize_effect_text("you destroy target artifact")
    texts = _texts(result)
    assert any(r.startswith("Destroy") for r in texts)


def test_you_draw_unchanged():
    """'you draw' should NOT be capitalized — _try_draw already handles lowercase."""
    result = _normalize_effect_text("you draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


# --- Rule 6: Strip "for each" ---

def test_strip_for_each():
    result = _normalize_effect_text("for each creature you control, create a 1/1 token")
    texts = _texts(result)
    assert any("create" in r.lower() for r in texts)


def test_strip_for_each_preserves_action():
    result = _normalize_effect_text("for each opponent, draw a card")
    texts = _texts(result)
    assert any("draw" in r.lower() for r in texts)


def test_no_op_on_clean_text():
    """Text that already starts with a verb should pass through unchanged."""
    result = _normalize_effect_text("draw a card")
    assert _texts(result) == ["draw a card"]
    assert result[0][1] is False  # not optional


def test_no_op_on_deal_damage():
    result = _normalize_effect_text("Purphoros deals 2 damage to each opponent")
    assert _texts(result) == ["Purphoros deals 2 damage to each opponent"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_effect_normalizer.py -v`
Expected: FAIL — `_normalize_effect_text` doesn't exist.

- [ ] **Step 3: Implement the pre-processor**

Add to `mtg_synergy/parse/effect_parser.py`, before `parse_effects()`:

```python
# ---- Pre-processor: normalize effect text before verb matching ----

# Verbs that need capitalization after "you" prefix (parsers use re.match with capital)
_CAPITALIZE_VERBS = {
    "create", "destroy", "exile", "return", "search", "discard",
    "put", "counter", "tap", "untap", "scry", "mill", "sacrifice",
}


def _normalize_effect_text(text: str) -> list[str]:
    """Normalize effect text to expose verb-initial patterns.

    Pipeline: Rule 1 (you may) → Rule 2 (conditional) → Rule 4 (you verb)
    → Rule 6 (for each) → Rule 5 (split) → Rule 3 (subject, per-part)

    Returns list of normalized text parts, each ready for _parse_single_effect().
    """
    t = text.strip()
    if not t:
        return []

    optional = False

    # Rule 1: Strip "you may/might" prefix
    m = re.match(r'^(?:you|that player)\s+(?:may|might)\s+', t, re.IGNORECASE)
    if m:
        t = t[m.end():]
        optional = True

    # Rule 2: Extract conditional effect (max 3 iterations)
    for _ in range(3):
        m = re.match(r'^[Ii]f\s+.+?,\s+(.+)$', t)
        if m:
            t = m.group(1).strip()
        else:
            break

    # Rule 1 again after conditional extraction (e.g., "if X, you may Y")
    m = re.match(r'^(?:you|that player)\s+(?:may|might)\s+', t, re.IGNORECASE)
    if m:
        t = t[m.end():]
        optional = True

    # Rule 4: Normalize "you verb" → "Verb" for verbs needing capitalization
    m = re.match(r'^you\s+(\w+)\b(.*)', t, re.IGNORECASE)
    if m:
        verb = m.group(1).lower()
        if verb in _CAPITALIZE_VERBS:
            t = verb.capitalize() + m.group(2)

    # Rule 6: Strip "for each X," prefix
    m = re.match(r'^[Ff]or\s+each\s+.+?,\s+(.+)$', t)
    if m:
        t = m.group(1).strip()

    # Rule 5: Split multi-step effects
    parts = _split_multi_step(t)

    # Rule 3: Normalize subject prefix on each part
    result = []
    for part in parts:
        normalized = _normalize_subject(part.strip())
        if normalized:
            result.append(normalized)

    # Return list of (text, optional) tuples
    return [(r, optional) for r in result] if result else [(text, False)]


def _split_multi_step(text: str) -> list[str]:
    """Split on ', then ' and '. ' sentence boundaries."""
    # Split on ", then "
    parts = re.split(r',\s+then\s+', text)
    # Further split on ". " (sentence boundary within effect text)
    expanded = []
    for p in parts:
        sents = re.split(r'\.\s+', p)
        expanded.extend(s.strip() for s in sents if s.strip())
    return expanded if expanded else [text]


# Verb deconjugation map (3rd person → infinitive)
_VERB_DECONJ = {
    "draws": "draw", "creates": "create", "deals": "deal",
    "gains": "gain", "loses": "lose", "puts": "put",
    "exiles": "exile", "destroys": "destroy", "returns": "return",
    "sacrifices": "sacrifice", "discards": "discard", "mills": "mill",
    "searches": "search", "taps": "tap", "untaps": "untap",
}


def _normalize_subject(text: str) -> str:
    """Strip subject prefixes and deconjugate verb."""
    t = text.strip()

    # Strip common subject prefixes
    m = re.match(
        r'^(?:each\s+(?:opponent|player)|that\s+(?:player|creature)'
        r'|its\s+controller|target\s+(?:opponent|player))\s+',
        t, re.IGNORECASE
    )
    if m:
        t = t[m.end():]
        # Deconjugate first word
        words = t.split(None, 1)
        if words and words[0].lower() in _VERB_DECONJ:
            deconj = _VERB_DECONJ[words[0].lower()]
            t = deconj + ((" " + words[1]) if len(words) > 1 else "")

    return t
```

Then modify `parse_effects()` to use the pre-processor:

```python
def parse_effects(effect_text: str) -> list[Effect]:
    """Parse effect text into a list of Effect AST nodes."""
    text = effect_text.strip().rstrip(".")

    # Try multi-keyword grant first
    multi = _try_grant_multiple_keywords(text)
    if len(multi) >= 2:
        return multi

    # Normalize text before verb matching
    normalized_parts = _normalize_effect_text(text)  # list of (str, bool) tuples

    results = []
    for part, is_optional in normalized_parts:
        # Try existing split + parse pipeline on each normalized part
        sub_parts = _split_effects(part)
        for sp in sub_parts:
            sp = sp.strip()
            if not sp:
                continue
            effect = _parse_single_effect(sp)
            if effect:
                if is_optional:
                    effect.optional = True
                results.append(effect)

    # Fallback: if normalizer produced nothing, try original text directly
    if not results:
        parts = _split_effects(text)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            effect = _parse_single_effect(part)
            if effect:
                results.append(effect)

    return results
```

- [ ] **Step 4: Run normalizer tests**

Run: `python3 -m pytest tests/test_effect_normalizer.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All 326+ tests PASS.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/parse/effect_parser.py tests/test_effect_normalizer.py
git commit -m "feat(parse): add effect text pre-processor with 5 normalization rules"
```

---

## Task 3: Subject Normalization Tests (Rule 3)

**Files:**
- Test: `tests/test_effect_normalizer.py` (append)

- [ ] **Step 1: Append Rule 3 tests**

Append to `tests/test_effect_normalizer.py`:

```python
from mtg_synergy.parse.effect_parser import _normalize_subject


# --- Rule 3: Subject normalization + deconjugation ---

def test_normalize_each_opponent():
    result = _normalize_subject("each opponent draws a card")
    assert result == "draw a card"


def test_normalize_that_player():
    result = _normalize_subject("that player creates a token")
    assert result == "create a token"


def test_normalize_its_controller():
    result = _normalize_subject("its controller creates a 3/3 green Beast creature token")
    assert result.startswith("create")


def test_normalize_target_opponent():
    result = _normalize_subject("target opponent discards a card")
    assert result == "discard a card"


def test_deconjugation_all_verbs():
    """All verbs in the deconjugation map should work."""
    from mtg_synergy.parse.effect_parser import _VERB_DECONJ
    for conj, inf in _VERB_DECONJ.items():
        result = _normalize_subject(f"each opponent {conj} something")
        assert result.startswith(inf), f"{conj} should become {inf}, got: {result}"


def test_no_normalization_on_clean_text():
    """Text without a subject prefix should pass through unchanged."""
    result = _normalize_subject("draw a card")
    assert result == "draw a card"
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_effect_normalizer.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_effect_normalizer.py
git commit -m "test(parse): add subject normalization + deconjugation tests"
```

---

## Task 4: Modal Ability Parsing

**Files:**
- Modify: `mtg_synergy/parse/__init__.py:25-27`
- Test: `tests/test_modal_parsing.py` (create)

- [ ] **Step 1: Write failing tests**

Create `tests/test_modal_parsing.py`:

```python
"""Tests for modal ability effect parsing."""
import pytest
from mtg_synergy.parse import parse_card


def test_modal_choose_one():
    """Modal abilities should have effects parsed from their modes."""
    oracle = "Choose one —\n• Create a 1/1 white Human creature token.\n• Draw a card.\n• Destroy target enchantment."
    abilities = parse_card(oracle)
    # Should have 1 ability with 3 effects (from 3 modes)
    modal = [a for a in abilities if a.kind == "modal"]
    assert len(modal) >= 1
    effects = modal[0].effects
    verbs = {e.verb for e in effects}
    assert "create" in verbs
    assert "draw" in verbs
    assert "destroy" in verbs


def test_modal_choose_two():
    """'Choose two' modals should also parse modes."""
    oracle = "Choose two —\n• Create a 1/1 white Human creature token.\n• Put a +1/+1 counter on target creature.\n• You gain 3 life."
    abilities = parse_card(oracle)
    modal = [a for a in abilities if a.kind == "modal"]
    assert len(modal) >= 1
    verbs = {e.verb for e in modal[0].effects}
    assert len(verbs) >= 2  # at least 2 of the 3 modes should parse


def test_non_modal_unaffected():
    """Non-modal abilities should not be affected."""
    oracle = "Whenever a creature enters the battlefield under your control, draw a card."
    abilities = parse_card(oracle)
    assert any(e.verb == "draw" for a in abilities for e in a.effects)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_modal_parsing.py -v`
Expected: `test_modal_choose_one` FAILS — modal abilities have no effects.

- [ ] **Step 3: Implement modal parsing**

Modify `mtg_synergy/parse/__init__.py`. After line 27 (`effects = parse_effects(raw.effect_text)`), add:

```python
        # Parse modal modes if no effects from effect_text
        if not effects and raw.modes:
            for mode_text in raw.modes:
                mode_text = mode_text.strip()
                # Strip mode label (e.g., "Sell Contraband — ")
                label_match = re.match(r'^[A-Z][\w\s]+\s*—\s*', mode_text)
                if label_match:
                    mode_text = mode_text[label_match.end():]
                mode_effects = parse_effects(mode_text)
                effects.extend(mode_effects)
```

Add `import re` at the top of the file.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_modal_parsing.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/parse/__init__.py tests/test_modal_parsing.py
git commit -m "feat(parse): add modal ability mode parsing"
```

---

## Task 5: Integration Test — Measure Improvement

**Files:**
- Test: `tests/test_effect_normalizer.py` (append integration tests)

- [ ] **Step 1: Write integration tests for known failing cards**

Append to `tests/test_effect_normalizer.py`:

```python
from mtg_synergy.parse.effect_parser import parse_effects


# --- Integration: verbs that previously failed ---

def test_parse_you_may_search():
    effects = parse_effects("you may search your library for a basic land card, put that card onto the battlefield tapped, then shuffle")
    assert any(e.verb == "search" for e in effects)


def test_parse_conditional_create():
    effects = parse_effects("that player may pay {2}. If the player doesn't, you create a Treasure token")
    assert any(e.verb == "create" for e in effects)


def test_parse_you_may_return():
    effects = parse_effects("you may return target permanent card with mana value 3 or less from your graveyard to the battlefield")
    assert any(e.verb == "return" for e in effects)


def test_parse_you_may_destroy():
    effects = parse_effects("you may destroy target artifact or enchantment")
    assert any(e.verb == "destroy" for e in effects)


def test_parse_each_opponent_draws():
    effects = parse_effects("each opponent draws a card")
    assert any(e.verb == "draw" for e in effects)


def test_parse_its_controller_creates():
    effects = parse_effects("Its controller creates a 3/3 green Beast creature token")
    assert any(e.verb == "create" for e in effects)


def test_parse_for_each_create():
    effects = parse_effects("for each creature you control, create a 1/1 white Human creature token")
    assert any(e.verb == "create" for e in effects)


def test_parse_you_create_treasure():
    effects = parse_effects("you create a Treasure token")
    assert any(e.verb == "create" for e in effects)


def test_parse_conditional_draw():
    effects = parse_effects("if you gained life this turn, draw a card")
    assert any(e.verb == "draw" for e in effects)


def test_optional_flag_set():
    effects = parse_effects("you may draw a card")
    draws = [e for e in effects if e.verb == "draw"]
    assert len(draws) >= 1
    assert draws[0].optional is True


def test_non_optional_flag():
    effects = parse_effects("draw a card")
    draws = [e for e in effects if e.verb == "draw"]
    assert len(draws) >= 1
    assert draws[0].optional is False


# --- Regression: cards that already work must not break ---

def test_regression_purphoros():
    """Purphoros 'deals 2 damage to each opponent' must still parse."""
    effects = parse_effects("Purphoros deals 2 damage to each opponent")
    assert any(e.verb == "deal_damage" for e in effects)


def test_regression_put_counter():
    """'put a +1/+1 counter on each creature you control' must still parse."""
    effects = parse_effects("put a +1/+1 counter on each creature you control")
    assert any(e.verb == "put_counter" for e in effects)


def test_regression_gain_life():
    """'you gain 1 life' must still parse."""
    effects = parse_effects("you gain 1 life")
    assert any(e.verb == "gain_life" for e in effects)
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_effect_normalizer.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_effect_normalizer.py
git commit -m "test(parse): add integration tests for pre-processor effect extraction"
```

---

## Task 6: Re-parse All Cards + Measure Improvement

Operational task — run pipelines and measure.

- [ ] **Step 1: Re-parse all 15k cards with improved parser**

```bash
python3 oracle_parser.py --parse-all --top 15000
```

- [ ] **Step 2: Measure empty-effect reduction**

```bash
python3 -c "
from mtg_synergy.db import get_connection
import json

conn = get_connection()
total = 0
empty = 0
empty_trig_act = 0
total_trig_act = 0
for row in conn.execute('SELECT ast_json FROM parsed_abilities'):
    d = json.loads(row[0])
    total += 1
    kind = d.get('kind', '')
    has_effects = bool(d.get('effects'))
    if not has_effects:
        empty += 1
    if kind in ('triggered', 'activated'):
        total_trig_act += 1
        if not has_effects:
            empty_trig_act += 1

print(f'Total abilities: {total}')
print(f'Empty effects: {empty} ({empty/total:.0%})')
print(f'Triggered/activated empty: {empty_trig_act}/{total_trig_act} ({empty_trig_act/total_trig_act:.0%})')
print()
print('Targets:')
print(f'  Empty trig/act: was 4512, now {empty_trig_act} (target: <1500)')
print(f'  Empty overall: was 55%, now {empty/total:.0%} (target: <25%)')
conn.close()
"
```

- [ ] **Step 3: Rebuild causal graph**

```bash
python3 build_graph.py --rebuild
```

- [ ] **Step 4: Check causal edge coverage improvement**

```bash
python3 -c "
from mtg_synergy.db import get_connection
conn = get_connection()
edges = conn.execute('SELECT COUNT(*) FROM interaction_edges').fetchone()[0]
edhrec_with_edges = conn.execute('''
    SELECT COUNT(DISTINCT ecs.card_name) FROM edhrec_card_synergy ecs
    JOIN cards c ON c.name = ecs.card_name
    WHERE c.oracle_id IN (SELECT source_id FROM interaction_edges UNION SELECT target_id FROM interaction_edges)
''').fetchone()[0]
edhrec_total = conn.execute('SELECT COUNT(DISTINCT card_name) FROM edhrec_card_synergy').fetchone()[0]
print(f'Causal edges: {edges:,}')
print(f'EDHREC cards with edges: {edhrec_with_edges}/{edhrec_total} ({edhrec_with_edges/edhrec_total:.0%})')
print(f'Target: was 67%, now {edhrec_with_edges/edhrec_total:.0%} (target: >85%)')
conn.close()
"
```

- [ ] **Step 5: Commit**

```bash
git add mtg_synergy/parse/effect_parser.py mtg_synergy/parse/ast_types.py mtg_synergy/parse/__init__.py
git commit -m "feat(parse): effect pre-processor reduces empty effects from 55% to <25%"
```

---

## Task 7: Forge DSL Import Script

**Files:**
- Create: `import_forge.py`
- Create: `mtg_synergy/parse/forge_fallback.py`
- Test: `tests/test_forge_fallback.py` (create)

- [ ] **Step 1: Write failing tests for Forge verb mapping**

Create `tests/test_forge_fallback.py`:

```python
"""Tests for Forge DSL parsing and verb mapping."""
import sqlite3
import pytest
from mtg_synergy.parse.forge_fallback import (
    parse_forge_ability_line, map_forge_verb, ensure_forge_schema,
    FORGE_VERB_MAP,
)


def test_parse_spell_ability():
    line = "A:SP$ DealDamage | Cost$ R | Tgt$ TgtCP | NumDmg$ 3 | SpellDescription$ deals 3 damage"
    result = parse_forge_ability_line(line)
    assert result is not None
    assert result["forge_verb"] == "DealDamage"
    assert result["amount"] == "3"


def test_parse_triggered_ability():
    line = "T:Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | ValidCard$ Creature.YouCtrl | Execute$ TrigDraw | TriggerDescription$ draw a card"
    result = parse_forge_ability_line(line)
    assert result is not None
    assert result["trigger_type"] == "ChangesZone"


def test_map_forge_verb_simple():
    assert map_forge_verb("DealDamage") == "deal_damage"
    assert map_forge_verb("DrawCard") == "draw"
    assert map_forge_verb("GainLife") == "gain_life"
    assert map_forge_verb("CreateToken") == "create"


def test_map_forge_verb_change_zone():
    assert map_forge_verb("ChangeZone", origin="Graveyard", destination="Battlefield") == "return"
    assert map_forge_verb("ChangeZone", origin="Hand", destination="Graveyard") == "discard"
    assert map_forge_verb("ChangeZone", origin="Battlefield", destination="Exile") == "exile"


def test_map_forge_verb_unknown():
    assert map_forge_verb("SomeNewVerb") is None


def test_forge_schema(tmp_db):
    conn = sqlite3.connect(tmp_db)
    ensure_forge_schema(conn)
    # Table should exist
    count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='forge_effects'"
    ).fetchone()[0]
    assert count == 1
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_forge_fallback.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `mtg_synergy/parse/forge_fallback.py`**

```python
"""Forge DSL verb mapping and effect fallback.

Maps Forge card script verbs to our effect vocabulary.
Used as fallback when the regex parser produces empty effects.
"""
import json
import re
import sqlite3
from typing import Optional

from mtg_synergy.parse.ast_types import Effect, Amount, ObjectFilter

# Forge verb → our verb (direct mappings)
FORGE_VERB_MAP = {
    "DealDamage": "deal_damage",
    "DrawCard": "draw",
    "GainLife": "gain_life",
    "LoseLife": "lose_life",
    "CreateToken": "create",
    "Destroy": "destroy",
    "DestroyAll": "destroy",
    "PutCounter": "put_counter",
    "PutCounterAll": "put_counter",
    "Mill": "mill",
    "Discard": "discard",
    "Proliferate": "put_counter",
    "Sacrifice": "sacrifice",
    "Tap": "tap",
    "TapAll": "tap",
    "Untap": "untap",
    "UntapAll": "untap",
    "ExileAll": "exile",
    "Exile": "exile",
    "Dig": "draw",
    "PumpAll": "pump",
    "Pump": "pump",
    "Counter": "counter",
    "Scry": "scry",
    "Token": "create",
    "ManaReflected": "add_mana",
    "Mana": "add_mana",
}

# ChangeZone mappings by Origin/Destination
_CHANGE_ZONE_MAP = {
    ("Graveyard", "Battlefield"): "return",
    ("Graveyard", "Hand"): "return",
    ("Hand", "Graveyard"): "discard",
    ("Battlefield", "Exile"): "exile",
    ("Battlefield", "Graveyard"): "sacrifice",
    ("Library", "Hand"): "search",
    ("Library", "Battlefield"): "search",
    ("Exile", "Battlefield"): "return",
    ("Exile", "Hand"): "return",
}


def map_forge_verb(forge_verb: str, origin: str = None, destination: str = None) -> Optional[str]:
    """Map a Forge verb to our effect vocabulary."""
    if forge_verb == "ChangeZone" or forge_verb == "ChangeZoneAll":
        if origin and destination:
            return _CHANGE_ZONE_MAP.get((origin, destination))
        return None
    return FORGE_VERB_MAP.get(forge_verb)


def parse_forge_ability_line(line: str) -> Optional[dict]:
    """Parse a single Forge ability line into structured fields.

    Forge format: "A:SP$ DealDamage | Key$ Value | Key2$ Value2"
    Prefix: A (activated/spell), T (triggered), S (static), K (keyword)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Detect prefix
    prefix = None
    for p in ("A:", "T:", "S:", "K:", "SVar:"):
        if line.startswith(p):
            prefix = p.rstrip(":")
            line = line[len(p):]
            break
    if prefix is None:
        return None

    # Parse key-value pairs separated by " | "
    fields = {}
    for pair in line.split(" | "):
        pair = pair.strip()
        if "$ " in pair:
            key, val = pair.split("$ ", 1)
            fields[key.strip()] = val.strip()
        elif "$" in pair:
            key, val = pair.split("$", 1)
            fields[key.strip()] = val.strip()

    # Extract verb
    forge_verb = fields.get("SP") or fields.get("Mode") or fields.get("")
    if not forge_verb:
        # Try first field value
        for k, v in fields.items():
            if k in ("SP", "Mode"):
                forge_verb = v
                break

    result = {
        "prefix": prefix,
        "forge_verb": forge_verb,
        "trigger_type": fields.get("Mode") if prefix == "T" else None,
        "target": fields.get("Tgt") or fields.get("ValidTgts"),
        "amount": fields.get("NumDmg") or fields.get("TokenAmount") or fields.get("CounterNum"),
        "origin": fields.get("Origin"),
        "destination": fields.get("Destination"),
        "fields": fields,
    }
    return result


def ensure_forge_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forge_effects (
            card_name TEXT NOT NULL,
            ability_index INTEGER NOT NULL,
            forge_verb TEXT NOT NULL,
            our_verb TEXT,
            target TEXT,
            amount TEXT,
            trigger_type TEXT,
            PRIMARY KEY (card_name, ability_index, forge_verb)
        )
    """)
    conn.commit()


def load_forge_effects(conn, card_name: str) -> list[Effect]:
    """Load Forge effects for a card and map to our Effect AST."""
    rows = conn.execute(
        "SELECT our_verb, target, amount FROM forge_effects WHERE card_name = ? AND our_verb IS NOT NULL",
        (card_name,)
    ).fetchall()
    effects = []
    for our_verb, target, amount in rows:
        amt = None
        if amount:
            try:
                amt = Amount(value=int(amount))
            except ValueError:
                amt = Amount(value=amount)
        effects.append(Effect(verb=our_verb, amount=amt))
    return effects
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_forge_fallback.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Create `import_forge.py`**

```python
#!/usr/bin/env python3
"""Import Forge card scripts as structured effect data.

Downloads Forge card scripts from GitHub and extracts ability data
into the forge_effects table for fallback effect extraction.

Usage:
    python3 import_forge.py --download    # Clone/update Forge repo
    python3 import_forge.py --import      # Parse and import to DB
    python3 import_forge.py --stats       # Show import stats
"""
import argparse
import os
import json
import subprocess

from mtg_synergy.db import get_connection
from mtg_synergy.parse.forge_fallback import (
    parse_forge_ability_line, map_forge_verb, ensure_forge_schema,
)

FORGE_REPO = "https://github.com/Card-Forge/forge.git"
FORGE_DIR = "data/forge"
CARDS_DIR = os.path.join(FORGE_DIR, "forge-gui", "res", "cardsfolder")


def download_forge():
    """Shallow clone Forge repo (only cardsfolder needed)."""
    if os.path.exists(FORGE_DIR):
        print(f"Forge repo already exists at {FORGE_DIR}")
        print("To update, delete it and re-run --download")
        return
    print(f"Cloning Forge repo (sparse, cardsfolder only)...")
    subprocess.run([
        "git", "clone", "--depth", "1", "--filter=blob:none",
        "--sparse", FORGE_REPO, FORGE_DIR
    ], check=True)
    subprocess.run([
        "git", "-C", FORGE_DIR, "sparse-checkout", "set",
        "forge-gui/res/cardsfolder"
    ], check=True)
    print("Done.")


def parse_forge_card(filepath: str) -> tuple[str, list[dict]]:
    """Parse a single Forge card file. Returns (card_name, abilities)."""
    name = None
    abilities = []
    ab_idx = 0

    with open(filepath, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.startswith("Name:"):
                name = line[5:].strip()
            elif line.startswith(("A:", "T:", "S:")):
                parsed = parse_forge_ability_line(line)
                if parsed and parsed["forge_verb"]:
                    our_verb = map_forge_verb(
                        parsed["forge_verb"],
                        origin=parsed.get("origin"),
                        destination=parsed.get("destination"),
                    )
                    parsed["our_verb"] = our_verb
                    parsed["ability_index"] = ab_idx
                    abilities.append(parsed)
                    ab_idx += 1

    return name, abilities


def import_all(conn):
    """Parse all Forge card files and import to DB."""
    ensure_forge_schema(conn)
    conn.execute("DELETE FROM forge_effects")

    if not os.path.exists(CARDS_DIR):
        print(f"Forge cards not found at {CARDS_DIR}")
        print("Run: python3 import_forge.py --download")
        return 0

    imported = 0
    errors = 0
    for root, dirs, files in os.walk(CARDS_DIR):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            try:
                filepath = os.path.join(root, fname)
                name, abilities = parse_forge_card(filepath)
                if not name or not abilities:
                    continue
                for ab in abilities:
                    conn.execute(
                        "INSERT OR IGNORE INTO forge_effects VALUES (?,?,?,?,?,?,?)",
                        (name, ab["ability_index"], ab["forge_verb"],
                         ab.get("our_verb"), ab.get("target"), ab.get("amount"),
                         ab.get("trigger_type")),
                    )
                imported += 1
            except Exception:
                errors += 1

    conn.commit()
    print(f"Imported {imported} cards, {errors} errors")
    return imported


def show_stats(conn):
    ensure_forge_schema(conn)
    cards = conn.execute("SELECT COUNT(DISTINCT card_name) FROM forge_effects").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM forge_effects").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM forge_effects WHERE our_verb IS NOT NULL").fetchone()[0]
    print(f"Forge effects: {cards} cards, {total} abilities, {mapped} mapped to our verbs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    if args.download:
        download_forge()
    elif args.do_import:
        conn = get_connection()
        import_all(conn)
        conn.close()
    elif args.stats:
        conn = get_connection()
        show_stats(conn)
        conn.close()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add mtg_synergy/parse/forge_fallback.py import_forge.py tests/test_forge_fallback.py
git commit -m "feat: add Forge DSL import script + verb mapping fallback"
```

---

## Task 8: Note on Forge Fallback Integration

**Decision**: Forge fallback integration into the parse pipeline is deferred. `parse_card()` receives oracle text only (no card name), so it can't query Forge by name. The Forge data lives in the `forge_effects` table and is available for any consumer that has the card name context — `oracle_parser.py`, `build_graph.py`, or `scoring.py` can query it directly when needed. No code changes in this task.

---

## Task 9: Run Forge Import + Final Evaluation

Operational task — requires Forge repo download.

- [ ] **Step 1: Download Forge repo**

```bash
python3 import_forge.py --download
```

Expected: Sparse clone of Forge cardsfolder (~100MB).

- [ ] **Step 2: Import Forge cards**

```bash
python3 import_forge.py --import
python3 import_forge.py --stats
```

Expected: 20k+ cards imported with mapped verbs.

- [ ] **Step 3: Run Recall@K evaluation**

```bash
python3 optimize_weights.py --evaluate --quick
python3 optimize_weights.py --evaluate --no-llm --quick
```

Record Recall@100 numbers for comparison with pre-improvement baselines.

- [ ] **Step 4: Run full test suite**

```bash
python3 -m pytest tests/ -q --tb=short
```

Expected: All tests PASS.

- [ ] **Step 5: Update CLAUDE.md**

Update the empty-effect stats and causal coverage numbers in CLAUDE.md.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with improved effect extraction stats"
```
