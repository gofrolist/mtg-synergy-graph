# Forge-Native Architecture — Design Spec

**Date**: 2026-03-25
**Goal**: Replace our hand-built effect/trigger vocabulary with Forge's battle-tested 20-year DSL. Use Forge's 32k encoded cards as primary data source, rebuild the causal graph on 134 trigger modes and 50+ effect verbs, and derive a new oracle parser from Forge patterns.

**Priority**: This is an architectural rewrite of the parse + causal subsystems. Everything downstream (scoring, recommendations, swaps) consumes the output unchanged.

## Current State vs Forge

| Dimension | Ours | Forge |
|-----------|------|-------|
| Effect verbs | 22 | 50+ |
| Trigger types | ~25 | 200 |
| Filter grammar | `ObjectFilter(card_type, subtype, controller)` | `Creature.YouCtrl+powerGE4+attacking` |
| Cards with structured effects | 15,000 (45% have effects) | 32,327 (100% encoded) |
| EDHREC edge coverage | 70% | ~98% (Forge covers nearly all) |
| Provides/wants tags | 34k cards (LLM-generated) | 7,228 DeckHas + 3,948 DeckHints (human-curated) |
| Data quality | Regex-parsed (55% empty effects) | Hand-encoded by volunteers over 20 years |

## Design: 7 Sections

### Section 1: New AST Types (Forge-Native)

Replace `Effect`, `Trigger`, `ObjectFilter` in `ast_types.py` with Forge-aligned dataclasses in a new `forge_types.py`.

**ForgeFilter** — replaces `ObjectFilter`:
```python
@dataclass
class ForgeFilter:
    """Parses Forge filter strings like 'Creature.YouCtrl+powerGE4+attacking'"""
    card_types: list[str] = field(default_factory=list)  # [Creature], [Instant, Sorcery], etc. (comma unions)
    subtypes: list[str] = field(default_factory=list)  # Goblin, Human, etc.
    controller: str | None = None      # YouCtrl, OppCtrl, YouOwn, YouDontCtrl
    zone: str | None = None            # Battlefield, Graveyard, Hand, Library, Exile
    is_remembered: bool | None = None  # IsRemembered (Forge internal reference)
    attached_by: str | None = None     # AttachedBy, EnchantedBy, EquippedBy
    power_ge: int | None = None        # powerGE4
    power_le: int | None = None
    toughness_ge: int | None = None
    toughness_le: int | None = None
    cmc_ge: int | None = None          # cmcGE5
    cmc_le: int | None = None
    is_token: bool | None = None       # token / nonToken
    is_attacking: bool | None = None
    is_blocking: bool | None = None
    is_tapped: bool | None = None      # tapped / untapped
    has_keyword: str | None = None     # withFlying, withHaste
    is_legendary: bool | None = None
    is_other: bool | None = None       # Other (not self)
    is_self: bool | None = None        # Self
    raw: str | None = None             # Original Forge filter string for unparsed modifiers
```

Parsed from Forge strings by splitting on `+` and `.`:
```
"Creature.YouCtrl+powerGE4+attacking"
-> ForgeFilter(card_type="Creature", controller="YouCtrl", power_ge=4, is_attacking=True)
```

**ForgeTrigger** — replaces `Trigger`:
```python
@dataclass
class ForgeTrigger:
    mode: str = ""              # ChangesZone, SpellCast, DamageDone, Attacks, Phase, etc.
    valid_card: ForgeFilter | None = None  # What triggers it
    origin: str | None = None   # For ChangesZone: Any, Graveyard, Hand, Library, Exile
    destination: str | None = None  # Battlefield, Graveyard, Hand, etc.
    phase: str | None = None    # For Phase triggers: Upkeep, End of Turn, etc.
    trigger_zones: list[str] = field(default_factory=list)  # Where this ability works from
```

**ForgeEffect** — replaces `Effect`:
```python
@dataclass
class ForgeEffect:
    verb: str = ""              # DealDamage, Draw, Token, ChangeZone, Pump, etc.
    target: ForgeFilter | None = None
    defined: str | None = None  # Self, You, Opponent, Targeted, Remembered
    amount: str | None = None   # Numeric or variable (X, Y)
    sub_ability: str | None = None  # SVar reference for chaining
    optional: bool = False
    num_damage: int | None = None
    num_cards: int | None = None
    keyword: str | None = None
    token_script: str | None = None
    counter_type: str | None = None
    zone_origin: str | None = None
    zone_destination: str | None = None
```

**Design decision**: `ForgeFilter.raw` stores the original filter string. We import ALL Forge data and progressively parse more filters without losing information. **Expected day-one parse coverage**: ~60-70% of filters will parse into typed fields. The remaining 30-40% (Forge-internal references like `IsRemembered`, `EffectSource`, complex relationship modifiers) are stored in `raw` and treated as opaque for matching purposes. This is acceptable — the 60-70% that parse cleanly cover the synergy-relevant filters (card type, subtype, controller, power/toughness).

---

### Section 2: Forge Import as Primary Data Source

Full DSL import preserving all information. Three tables:

```sql
-- One row per ability
forge_abilities (
    card_name TEXT NOT NULL,
    ability_index INTEGER NOT NULL,
    ability_type TEXT NOT NULL,        -- A, T, S, K, R
    verb TEXT,                         -- SP$ value: DealDamage, Draw, Token, etc.
    trigger_mode TEXT,                 -- Mode$ for T: lines: ChangesZone, SpellCast, etc.
    trigger_filter TEXT,               -- ValidCard$ value
    trigger_origin TEXT,               -- Origin$ for ChangesZone
    trigger_destination TEXT,          -- Destination$ for ChangesZone
    trigger_phase TEXT,                -- Phase$ for Phase triggers
    trigger_zones TEXT,                -- TriggerZones$ value
    target TEXT,                       -- Tgt$/ValidTgts$ value
    defined TEXT,                      -- Defined$ value
    amount TEXT,                       -- NumDmg$, NumCards$, TokenAmount$, etc.
    cost TEXT,                         -- Cost$ value
    keyword TEXT,                      -- KW$ or keyword name
    token_script TEXT,                 -- TokenScript$ reference
    counter_type TEXT,                 -- CounterType$ value
    sub_ability TEXT,                  -- SubAbility$ SVar reference
    unless_cost TEXT,                  -- UnlessCost$ for optional effects
    raw_line TEXT NOT NULL,            -- Full original DSL line
    PRIMARY KEY (card_name, ability_index)
)

-- Deck building tags
forge_deck_tags (
    card_name TEXT NOT NULL,
    tag_type TEXT NOT NULL,            -- 'has', 'hints', 'needs'
    tag TEXT NOT NULL,                 -- 'Ability$Token', 'Type$Artifact', etc.
    PRIMARY KEY (card_name, tag_type, tag)
)

-- SVars for effect chaining
forge_svars (
    card_name TEXT NOT NULL,
    svar_name TEXT NOT NULL,
    svar_value TEXT NOT NULL,          -- Full SVar definition
    PRIMARY KEY (card_name, svar_name)
)
```

**Shallow SVar Resolution** (required for triggered abilities):

Nearly all triggered abilities (`T:` lines) use `Execute$ TrigDraw` which references an SVar: `SVar:TrigDraw:DB$ Draw | NumCards$ 1 | ...`. The verb (`Draw`) lives in the SVar, not the trigger line itself. The import must follow ONE level of `Execute$ -> SVar -> DB$` to extract the verb.

Algorithm:
1. First pass: collect all SVars per card into `forge_svars` table
2. Second pass: for each `T:` line with `Execute$`, look up the SVar, extract `DB$` value as verb
3. Also extract SVar's parameters (NumDmg$, NumCards$, etc.) into the ability row

This is NOT full SVar chain resolution (multi-hop). It's a single dereference: `Execute$ X -> SVar:X:DB$ Verb`. Sub-abilities (`SubAbility$`) within SVars are stored as references but not followed.

**Import pipeline**: Rewrite `import_forge.py` to extract ALL fields with shallow SVar resolution. Two passes over each card file. Drop the old `forge_effects` table.

**Keyword abilities** (`K:` lines): Stored with `verb=NULL`, `keyword=<keyword_name>`. Example: `K:Flying` → `ability_type='K', keyword='Flying', verb=NULL`.

**Card name matching**: Join `forge_abilities.card_name` to `cards.name`. Requires a normalization layer for DFC/split cards:
- Forge: filename `delver_of_secrets.txt` contains `Name:Delver of Secrets`
- Scryfall: `Delver of Secrets // Insectile Aberration`
- Strategy: match on front face name. Build a `forge_name_to_oracle_id` mapping table during import by matching `forge_abilities.card_name` against `cards.name` (exact) or `cards.name LIKE forge_name || ' //%'` (DFC front face).

---

### Section 3: Causal Graph on Forge Vocabulary

Index directly on Forge's 134 trigger modes. The trigger's `ValidCard` filter provides specificity that IDF was approximating.

**New indexer design**:
- **Producer index**: `{(verb, target_filter_hash): [(card_name, ability_idx)]}`
- **Responder index**: `{(trigger_mode, valid_card_filter_hash): [(card_name, ability_idx)]}`

**Edge matching**: A producer matches a responder when the producer's effect could satisfy the responder's trigger.

```
Producer: Token(creature, Goblin) [Krenko creates Goblin tokens]
Responder: ChangesZone(ValidCard=Creature) [Purphoros triggers on creature entering]
Responder: ChangesZone(ValidCard=Goblin) [Goblin lord triggers on Goblin entering]

Purphoros: broad match (strength 0.6)
Goblin lord: exact match (strength 1.0)
```

**Filter matching** replaces `_compute_filter_precision()`:
```
If responder.valid_card has subtype AND producer matches it -> exact (1.0)
If responder.valid_card has card_type only AND producer matches -> broad (0.6)
If responder.valid_card has no filter -> unfiltered (0.3)
```

Same logic but encoded in ForgeFilter grammar. IDF still applies on top for event frequency dampening.

**Mapping Forge effects to game events** (which triggers fire):

| Forge Verb | Game Event(s) Produced |
|------------|----------------------|
| Token | ChangesZone(Destination=Battlefield) — creature/artifact enters |
| ChangeZone | ChangesZone(Origin, Destination) — varies by zones |
| DealDamage | DamageDone |
| Destroy | ChangesZone(Destination=Graveyard) — permanent dies |
| Sacrifice | Sacrificed + ChangesZone(Destination=Graveyard) |
| GainLife | LifeGained |
| LoseLife | (no standard trigger) |
| Draw | Drawn |
| PutCounter | (counter placed triggers) |
| Discard | Discarded |
| Mill | ChangesZone(Origin=Library, Destination=Graveyard) |
| Tap | Taps |

This mapping is the new "verb resolvers" — but instead of 20 hand-written functions, it's a lookup table derived from Forge's actual trigger/effect relationships across 32k cards.

**Additional verbs requiring event mappings** (not in initial table above):
| Forge Verb | Count | Game Event(s) |
|------------|-------|---------------|
| GainControl | 71 | (ownership change, no standard trigger) |
| CopyPermanent | 36 | ChangesZone(Destination=Battlefield) — copy enters |
| Animate/AnimateAll | 110 | (grants abilities, no zone change) |
| Charm | 484 | Resolved via sub-verb in SVar (each choice is a separate verb) |
| Effect | 170 | Generic wrapper — resolve from SVar chain |
| RepeatEach | 74 | Iterates over a set, executing a sub-effect per item |

`Charm` and `Effect` are meta-verbs that delegate to SVars. Shallow SVar resolution (Section 2) extracts the actual verbs from their choices.

---

### Section 4: New Oracle Parser (Forge-Pattern-Derived)

For the ~1,600 cards Forge doesn't cover (newest sets), build a parser from Forge's 32k `(oracle_text, structured_effect)` pairs.

**Process**:
1. Group 32k cards by Forge verb: `{DealDamage: [oracle1, oracle2, ...], Draw: [...], ...}`
2. For each verb, extract common oracle text patterns
3. Write one regex per verb that matches 90%+ of that verb's oracle texts
4. Same for triggers: group by trigger_mode, derive patterns

**Example** (DealDamage, 632 cards):
```
Oracle patterns:
  "deals {N} damage to {target}" — 92%
  "deals damage equal to {X}" — 6%
  "deals {N} damage to each {target}" — 3%
-> Regex: r'deals\s+(\d+|X)\s+damage\s+(?:equal\s+to\s+.+?\s+)?to\s+(.+)'
```

**File structure**:
```
mtg_synergy/parse/
  forge_types.py            — ForgeFilter, ForgeTrigger, ForgeEffect dataclasses
  forge_filter_parser.py    — Parse "Creature.YouCtrl+powerGE4" strings
  forge_import.py           — Import Forge DSL files to DB (replaces import_forge.py)
  oracle_effects.py         — NEW parser: oracle text -> ForgeEffect
  oracle_triggers.py        — NEW parser: oracle text -> ForgeTrigger
  oracle_costs.py           — Keep existing cost_parser, minor updates
```

**Validation**: Run parser on all 32k Forge cards, compare output to Forge encoding. Report per-verb match rate. Target: 80%+ overall.

**parse_card() flow**:
```
1. Look up card_name in forge_abilities table
2. If found -> return Forge-encoded abilities (primary source)
3. If not found -> run new oracle parser (fallback for gap cards)
```

---

### Section 5: DeckHas/DeckHints as Signal Layer

Forge provides human-curated deck building tags:
- `DeckHas` (7,228 cards) = what the card provides: `Ability$Token`, `Ability$Counters`, `Ability$LifeGain`
- `DeckHints` (3,948 cards) = what the card wants: `Type$Artifact`, `Type$Zombie`, `Ability$Graveyard`
- `DeckNeeds` (1,202 cards) = hard requirements

These complement our existing provides/wants tags (34k cards, LLM-generated). Where both exist, Forge's are higher quality (human-curated).

**Integration in `mtg_synergy/recommend/scoring.py`**: Add `forge_deck_overlap` feature:
```python
# Count matching DeckHas (candidate) <-> DeckHints (commander) and vice versa
forge_overlap = len(candidate_has & commander_hints) + len(candidate_hints & commander_has)
```

Added to `compute_dynamic_score()` as a new weighted feature alongside existing `cmdr_tag_overlap`.

---

### Section 6: Migration Path

**Retired** (after new system validated):
```
mtg_synergy/parse/effect_parser.py       — replaced by oracle_effects.py
mtg_synergy/parse/trigger_parser.py      — replaced by oracle_triggers.py
mtg_synergy/parse/verb_resolvers.py      — replaced by Forge verb->event mapping table
mtg_synergy/parse/resolver.py            — Forge handles coreference
mtg_synergy/parse/templates.py           — Forge encodes scaling/modal
mtg_synergy/parse/splitter.py            — Forge already splits abilities
mtg_synergy/parse/ast_types.py           — replaced by forge_types.py
mtg_synergy/parse/forge_fallback.py      — absorbed into forge_import.py
```

**Kept, modified**:
```
mtg_synergy/parse/__init__.py            — new parse_card() with Forge lookup + parser fallback
mtg_synergy/parse/cost_parser.py         — minor updates for Forge cost format
mtg_synergy/causal/indexer.py            — rewritten for Forge vocabulary
mtg_synergy/causal/graph_builder.py      — rewritten with ForgeFilter matching
mtg_synergy/causal/__init__.py           — CausalContext updated for new edge types
mtg_synergy/recommend/scoring.py         — add forge_deck_overlap feature
import_forge.py                          — thin CLI wrapper calling mtg_synergy/parse/forge_import.py
```

**New**:
```
mtg_synergy/parse/forge_types.py         — ForgeFilter, ForgeTrigger, ForgeEffect
mtg_synergy/parse/forge_filter_parser.py — Parse Forge filter grammar
mtg_synergy/parse/forge_import.py        — Full Forge DSL import
mtg_synergy/parse/oracle_effects.py      — New effect parser
mtg_synergy/parse/oracle_triggers.py     — New trigger parser
```

**Migration strategy**: Build new system alongside old. Both coexist in the codebase. Once new system's Recall@K beats old, flip. Old files retired (not deleted until confirmed).

**Preserved signal layers**: The `card_mechanics` table (7k+ cards, LLM-extracted structured mechanics) and the mechanics matching engine (`mechanics_matcher.py`) are independent of the parser rewrite. They continue working as-is in the scoring pipeline. The `provides`/`wants` tag tables (34k cards) also remain — they complement Forge's DeckHas/DeckHints.

**DB migration**: New `forge_abilities`, `forge_deck_tags`, `forge_svars` tables. Old `parsed_abilities`, `interaction_edges` tables backed up, then rebuilt from Forge data.

---

### Section 7: Success Criteria + Testing

**Target improvements**:

| Metric | Current | Target |
|--------|---------|--------|
| Recall@100 (all signals) | 48.5% | >55% |
| Recall@100 (no LLM) | 49.6% | >55% |
| Recall@30 (all signals) | 16.1% | >25% |
| EDHREC edge coverage | 70% | >95% |
| Trigger vocabulary | ~25 types | 200 types |
| Effect vocabulary | 22 verbs | 50+ verbs |
| Cards with structured effects | 15k (45% have effects) | 32k+ (100%) |
| New parser vs Forge match rate | N/A | >80% |
| Causal-only Recall@100 (no LLM, no EDHREC) | 49.6% | >52% |

**Testing**:
- `tests/test_forge_types.py` — ForgeFilter parsing from strings, ForgeTrigger, ForgeEffect
- `tests/test_forge_import.py` — Import pipeline, schema, data integrity, 32k card count
- `tests/test_forge_filter_parser.py` — Filter grammar: `Creature.YouCtrl+powerGE4` etc.
- `tests/test_oracle_effects.py` — New parser vs Forge ground truth (sample per verb)
- `tests/test_oracle_triggers.py` — New trigger parser vs Forge ground truth
- `tests/test_causal_forge.py` — Causal graph on Forge vocabulary, edge matching
- Integration: full pipeline (import -> parse -> index -> score -> recommend)

**Rollback**: Old system files kept in codebase. If new system regresses Recall@K, revert `parse_card()` to use old parser. DB tables coexist (old `parsed_abilities` + new `forge_abilities`).

---

## Implementation Order

| Step | Section | Dependencies | Effort |
|------|---------|-------------|--------|
| 1 | ForgeFilter parser (S1 partial) | None | Medium |
| 2 | Forge types (S1) | Step 1 | Low |
| 3 | Forge full import (S2) | Step 2 | Medium |
| 4 | DeckHas/DeckHints import + scoring (S5) | Step 3 | Low |
| 5 | New oracle effect parser (S4 partial) | Step 3 (needs Forge data) | High |
| 6 | New oracle trigger parser (S4 partial) | Step 3 | High |
| 7 | Causal indexer rewrite (S3) | Steps 2, 3 | High |
| 8 | Causal graph builder rewrite (S3) | Step 7 | High |
| 9 | parse_card() integration (S6) | Steps 3, 5, 6 | Medium |
| 10 | Evaluate + compare (S7) | All above | Low |

Steps 5+6 can run in parallel. Steps 7+8 are sequential. Step 4 is independent after Step 3.

## Out of Scope

- Forge SVar chain resolution (complex subroutine execution) — we read SVars for metadata but don't execute them
- Forge AI hints — interesting but not needed for synergy scoring
- Token script parsing (TokenScript$ references) — we extract the token creation verb, not the full token definition
- Replacement effects (R: lines) — 1,623 cards, handle in a future pass. **Bridge**: The current "amplifies" edge builder (oracle text regex for Doubling Season, Panharmonicon, etc.) is preserved in the new graph builder as a legacy fallback until R: parsing is added. These are among the most synergy-dense cards in Commander and must not lose coverage.
