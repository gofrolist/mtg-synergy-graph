# Effect Extraction Improvement — Design Spec

**Date**: 2026-03-25
**Goal**: Fix the 55% empty-effect rate in parsed abilities by adding a text pre-processor and Forge DSL fallback, increasing causal edge coverage from 67% to 85%+ for EDHREC-relevant cards.

**Priority**: This is the #1 bottleneck for causal graph quality. Only 13.7% of candidates have non-zero causal scores because most abilities produce no StateChanges due to empty effects.

## Current State

| Metric | Value |
|--------|-------|
| Total parsed abilities | 30,961 |
| With effects | 13,901 (45%) |
| Empty effects | 17,060 (55%) |
| Empty triggered/activated | 4,512 (most impactful for causal edges) |
| Empty static | 7,308 (mostly correct — passive modifiers) |
| Empty keyword | 4,678 (correctly empty — keywords don't produce effects) |
| EDHREC cards with causal edges | 67% |

## Root Cause

The effect parser (`effect_parser.py`) uses 22 `_try_*` functions with regexes that assume **verb-initial sentence structure**. This fails on:

| Failure pattern | Count | Example |
|----------------|-------|---------|
| "you may" prefix | 1,338 | "you may search your library..." |
| Conditional clause | 857 | "if you gained life, draw a card" |
| Subject prefix | 584 | "each opponent draws a card" |
| Modal abilities | 352 | "choose one — • Create • Draw • Destroy" |
| "for each X" scaling | 150 | "for each creature, create a token" |
| Multi-step effects | 129 | "search..., put..., then shuffle" |
| "you verb" lowercase | ~200 | "you create a Treasure token" |
| Other (rare mechanics) | 610 | initiative, manifest, explore, venture |

## Design: 7 Sections

### Section 1: Pre-processor Pipeline

Add `_normalize_effect_text(text: str) -> list[str]` to `effect_parser.py`, called from `parse_effects()` before `_parse_single_effect()`.

Pipeline order:
```
effect_text
  → Rule 1: _strip_you_may()
  → Rule 2: _extract_conditional() (max 3 iterations)
  → Rule 4: _normalize_you_verb()
  → Rule 6: _strip_for_each()
  → Rule 5: _split_multi_step()  → list of parts
  → For each part:
    → Rule 3: _normalize_subject() + deconjugate verb
    → _parse_single_effect()
```

**Key ordering**: Rule 5 (split) runs BEFORE Rule 3 (subject normalization), so each part gets deconjugated independently. This prevents "each opponent draws a card and loses 1 life" from only deconjugating the first verb.

Each rule is a pure function `str → str` (or `str → list[str]` for multi-step).

**Relationship to `templates.py`**: The existing `decompose_reminder()` in `templates.py` also strips conditionals and splits on ", then ". The pre-processor replaces that logic for effect text — `decompose_reminder()` continues to handle reminder text and scaling detection. No duplication: different input contexts, different purposes.

Existing `_parse_single_effect()` and all 22 `_try_*` functions remain unchanged.

---

### Section 2: Normalization Rules

**Note**: Rules are numbered for reference but execute in the order shown in Section 1's pipeline diagram (1, 2, 4, 6, 5, then 3 per-part). Rule 5 (split) runs before Rule 3 (subject normalization) so each part gets independent deconjugation.

**Rule 1 — Strip "you may/might"** (fixes ~1,338):
```
Pattern: ^(?:you|that player)\s+(?:may|might)\s+
Action: strip prefix, mark effect as optional
"you may search your library" → "search your library" (optional=True)
```
**Design note**: "You may" indicates an optional effect in MTG. We strip it for parsing but track it as a flag on the returned Effect for potential future use (cost/benefit analysis). For the causal graph, optional effects still represent what the card CAN do.

**Rule 2 — Extract conditional effect** (fixes ~857):
```
Pattern: ^[Ii]f\s+.+?,\s+(.+)$
Action: take capture group (the effect after the comma)
"if the player doesn't, you create a Treasure token" → "you create a Treasure token"
"if you do, exile it" → "exile it"
```
Applied in a loop with **max 3 iterations** to handle nested conditionals ("if X, if Y, do Z"). Loop terminates when the text no longer starts with "if" or iteration limit reached.

**Rule 3 — Normalize subject prefix** (fixes ~584):
```
Pattern: ^(?:each\s+(?:opponent|player)|that\s+(?:player|creature)|its\s+controller|target\s+(?:opponent|player))\s+
Action: strip prefix, deconjugate first verb
"each opponent draws a card" → "draw a card"
"its controller creates a 3/3" → "create a 3/3"
```

Deconjugation map (finite, exact), applied to the **first word** of each part after Rule 5 splitting:
```python
_VERB_DECONJ = {
    "draws": "draw", "creates": "create", "deals": "deal",
    "gains": "gain", "loses": "lose", "puts": "put",
    "exiles": "exile", "destroys": "destroy", "returns": "return",
    "sacrifices": "sacrifice", "discards": "discard", "mills": "mill",
    "searches": "search", "taps": "tap", "untaps": "untap",
}
```
Because Rule 3 runs AFTER Rule 5 (split), each clause gets its own deconjugation. "each opponent draws a card and loses 1 life" → split → ["draws a card", "loses 1 life"] → deconj each → ["draw a card", "lose 1 life"].

**Rule 4 — Normalize "you verb" → "Verb"** (fixes ~200):
```
Pattern: ^you\s+(create|destroy|exile|return|search|discard|put|counter|tap|untap|scry|mill|sacrifice)\b
Action: capitalize verb, drop "you"
"you create a Treasure token" → "Create a Treasure token"
```
Applied for all verbs where existing `_try_*` parsers use `re.match` with a capitalized start. Verbs like "draw", "gain", "lose" use `re.search` and already handle lowercase — excluded to avoid unnecessary transforms.

**Rule 5 — Split multi-step effects** (fixes ~129):
```
Split on ", then " delimiter
"search your library for a card, put it onto the battlefield, then shuffle"
→ ["search your library for a card", "put it onto the battlefield", "shuffle"]
```
Each part parsed independently. Also split on ". " (sentence boundary within effect text).

**Rule 6 — Strip "for each X" prefix** (fixes ~150):
```
Pattern: ^[Ff]or\s+each\s+.+?,\s+(.+)$
Action: take capture group
"for each creature you control, create a 1/1 token" → "create a 1/1 token"
```
The "for each" scaling is handled separately by templates.py.

---

### Section 3: Modal Abilities

The splitter already extracts modes into `raw.modes` list. Currently `parse_card()` in `__init__.py` ignores them.

Fix: In `parse_card()`, after the `if raw.effect_text:` block:

```python
if raw.modes and not effects:
    for mode_text in raw.modes:
        mode_effects = parse_effects(mode_text)
        effects.extend(mode_effects)
```

Each mode is effect text that runs through the same pre-processor + parser pipeline. All mode effects attach to one ability (the card CAN produce any of them).

Saga chapters already work — the splitter creates separate RawAbility objects per chapter. The pre-processor fixes any prefix issues in chapter text.

---

### Section 4: Static Abilities

No changes needed. The 7,308 empty-effect statics are overwhelmingly passive modifiers (enters tapped, hand size, cost reduction). Parsing them would add noise. The few statics with real game events are already caught by existing parsers or will be caught by the pre-processor's subject normalization.

---

### Section 5: Verb Deconjugation

After Rule 3 strips a subject prefix, the verb remains conjugated ("draws" not "draw"). Handled by a finite lookup table applied as the last step of Rule 3.

The `_VERB_DECONJ` map from Section 2 covers all 15 verb forms the parser recognizes. No stemming, no NLP — just exact string replacement of the first word.

---

### Section 6: Testing Strategy

**Unit tests** (`tests/test_effect_normalizer.py`):
- Each of the 6 rules: 3-4 test cases (typical, edge case, no-op)
- Verb deconjugation: all 15 entries
- Modal mode parsing: 2-3 modal cards

**Integration test**:
- Parse a set of known-failing cards, verify effects are now extracted
- Count empty-effect reduction on the full 15k parsed set

**Regression test**:
- Re-parse cards that currently work (Purphoros, Krenko, Cathars' Crusade) and verify identical AST output

**Success criteria**:
1. Empty-effect triggered/activated: 4,512 → under 1,500 (>65% reduction)
2. Empty-effect overall: 55% → under 25%
3. Cards with at least one non-empty effect: increase from ~45% to ~70%
4. Zero regressions on existing 326 tests
5. Causal edge coverage of EDHREC cards: 67% → 85%+

---

### Section 7: Forge DSL Mining

**Data source**: Forge card scripts from `Card-Forge/forge` GitHub repo. Each card is a `.txt` file in `res/cardsfolder/` with structured DSL encoding.

**DSL format example**:
```
Name:Lightning Bolt
ManaCost:R
Types:Instant
A:SP$ DealDamage | Cost$ R | Tgt$ TgtCP | NumDmg$ 3
Oracle:Lightning Bolt deals 3 damage to any target.
```

Ability prefixes: `A:` (activated/spell), `T:` (triggered), `S:` (static), `K:` (keyword).
Key fields: `SP$` (effect type), `Cost$`, `Tgt$`, `NumDmg$`, `TokenAmount$`, `CounterNum$`, `TriggerType$`.

**Forge verb → our verb mapping**:
```python
FORGE_VERB_MAP = {
    "DealDamage": "deal_damage", "DrawCard": "draw", "GainLife": "gain_life",
    "LoseLife": "lose_life", "CreateToken": "create", "Destroy": "destroy",
    "DestroyAll": "destroy", "PutCounter": "put_counter", "Mill": "mill",
    "Discard": "discard", "Proliferate": "proliferate",
    "Sacrifice": "sacrifice", "Tap": "tap", "Untap": "untap",
    "ExileAll": "exile", "Exile": "exile", "Dig": "draw",
    "PumpAll": "pump", "Pump": "pump", "Counter": "counter",
}
```

**ChangeZone handling**: Forge's `ChangeZone` covers ALL zone transitions. The mapping depends on `Origin$` and `Destination$` fields:
- `Origin$ Graveyard | Destination$ Battlefield` → `return`
- `Origin$ Hand | Destination$ Graveyard` → `discard`
- `Origin$ Library | Destination$ Hand` → `draw` (search variant)
- `Origin$ Battlefield | Destination$ Exile` → `exile`
- `Origin$ Battlefield | Destination$ Graveyard` → `sacrifice` or `destroy`

**Pipeline**:
1. Script `import_forge.py`: download/clone Forge's `res/cardsfolder/`, parse `.txt` files, extract ability lines, store in `forge_effects` table
2. Module `mtg_synergy/parse/forge_fallback.py`: lookup card by name, map Forge verbs to our vocabulary, return `list[Effect]`

**Table schema**:
```sql
CREATE TABLE forge_effects (
    card_name TEXT NOT NULL,
    ability_index INTEGER NOT NULL,
    forge_verb TEXT NOT NULL,
    our_verb TEXT,           -- mapped via FORGE_VERB_MAP
    target TEXT,
    amount TEXT,
    trigger_type TEXT,
    PRIMARY KEY (card_name, ability_index, forge_verb)
);
```

**Two uses**:

**Fallback**: In `parse_card()`, after the pre-processor + parser, if effects are still empty AND forge_effects has data for this card, inject Forge's effects (mapped to our Effect AST).

**Validation**: Script `validate_parser.py --forge` — for cards where both our parser and Forge have data, compare effect verbs. Report disagreements. Not a blocker — a diagnostic tool.

---

## Implementation Order

| Step | Section | Dependencies | Effort |
|------|---------|-------------|--------|
| 1 | Pre-processor + rules (S1-S2) | None | Medium |
| 2 | Verb deconjugation (S5) | S1 | Low |
| 3 | Modal parsing (S3) | S1 | Low |
| 4 | Tests (S6) | S1-S3 | Medium |
| 5 | Re-parse all 15k cards | S1-S4 | Low (run pipeline) |
| 6 | Rebuild causal graph | S5 | Low (run pipeline) |
| 7 | Forge import (S7) | None (parallel with S1-S4) | Medium |
| 8 | Forge fallback integration | S5, S7 | Low |
| 9 | Evaluate Recall@K improvement | S6, S8 | Low |

Steps 1-4 and 7 can run in parallel. Step 7 (Forge) is independent of the pre-processor.

## Files Modified/Created

| File | Change |
|------|--------|
| `mtg_synergy/parse/effect_parser.py` | Add `_normalize_effect_text()` + 6 rule functions |
| `mtg_synergy/parse/ast_types.py` | Add `optional: bool = False` field to `Effect` dataclass |
| `mtg_synergy/parse/__init__.py` | Add modal mode parsing in `parse_card()` |
| `mtg_synergy/parse/forge_fallback.py` | **New** — Forge verb mapping + Effect lookup |
| `import_forge.py` | **New** — Download + parse Forge card scripts |
| `tests/test_effect_normalizer.py` | **New** — Unit tests for all 6 rules |
| `tests/test_forge_fallback.py` | **New** — Forge mapping + integration tests |

## Rollback Plan

The pre-processor only adds a normalization layer before existing parsers. If it causes false parses, set `NORMALIZE_EFFECTS = False` in config.py to bypass it. Forge fallback is opt-in (only used when our parser produces empty effects).

## Out of Scope

- New `_try_*` functions for rare mechanics (initiative, manifest, explore, venture)
- Coreference resolution ("those creatures", "the exiled card")
- Coordination ambiguity ("gains X if red, and Y if white")
- These are the "two walls" from parser research — addressed by Forge fallback, not by regex
