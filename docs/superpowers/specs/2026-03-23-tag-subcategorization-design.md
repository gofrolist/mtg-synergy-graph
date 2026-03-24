# Tag Sub-categorization Design

Split 6 generic tags into 20 specific sub-tags so the tag overlap tiebreaker becomes discriminative enough to re-enable in the recommendation scoring formula.

## Problem

The tag overlap tiebreaker (`cmdr_tag_overlap`) is computed but disabled in `engine.py` because generic tags create false positives:

- `creature-board` (39.2% of cards) — nearly every card "wants" creatures on board
- `combat-events` (23.4%) — too broad to distinguish attack vs damage vs block triggers
- `creature-etb` (21.1%) — conflates tribal ETB with generic value ETB
- `creature-pump` (14.7%) — lords, anthems, combat tricks, and self-growth lumped together
- `token-generation` (13.0%) — creature tokens, treasure, and tribal tokens mixed
- `evasion-grant` (12.5%) — flying, unblockable, and menace serve different strategies

Experiments showed the tiebreaker helps tribal decks (Krenko +8) but hurts others (Sram -8, Edgar -3) because generic overlap is noise, not signal.

## Solution

Replace 6 parent tags with 20 sub-tags via LLM reclassification. Update SEMANTIC_BRIDGES to use higher-weight, more precise connections. Re-enable the overlap tiebreaker.

## Sub-category Definitions

### creature-pump (provides, 4,947 cards) → 4 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `pump-lord` | Permanently buffs creatures of a specific type | Goblin King, Lord of Atlantis, Mayor of Avabruck |
| `pump-anthem` | Permanently buffs all/most creatures regardless of type | Glorious Anthem, Gaea's Anthem, Elesh Norn |
| `pump-combat` | Temporary combat buffs, voltron equipment/auras | Giant Growth, Embercleave, All That Glitters |
| `pump-self` | Card grows itself via counters or scaling | Tarmogoyf, Managorger Hydra, Champion of the Parish |

### creature-board (wants, 12,918 cards) → 4 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `board-tokens` | Payoff for having token creatures specifically | Doubling Season, Anointed Procession, Parallel Lives |
| `board-tribal` | Payoff for having creatures of a specific type on board | Kindred Charge, Coat of Arms, Door of Destinies |
| `board-go-wide` | Payoff for having many creatures (count matters) | Craterhoof Behemoth, Overwhelming Stampede, Triumph of the Hordes |
| `board-generic` | Needs creatures on board but not count/type dependent | Skullclamp, Ashnod's Altar, Lightning Greaves |

### creature-etb (wants, 6,964 cards) → 3 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `etb-value` | Triggers on any creature entering for value (draw, removal, ramp) | Panharmonicon, Yarok, Soul of the Harvest |
| `etb-tokens` | Triggers on creature entry to make tokens or deal damage | Molten Echoes, Impact Tremors, Warstorm Surge |
| `etb-tribal` | Triggers on specific creature type entering | Realmwalker, Herald's Horn, Vanquisher's Banner |

### combat-events (wants, 7,693 cards) → 3 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `combat-attack` | Triggers on attacking or beginning of combat | Reconnaissance, Beastmaster Ascension, Marton Stromgald |
| `combat-damage` | Triggers on dealing combat damage (to player or creature) | Edric, Toski, Ohran Frostfang |
| `combat-block` | Triggers on blocking or cares about being blocked | Vengeful Ancestor, Iroas, Dolmen Gate |

### token-generation (provides, 4,368 cards) → 3 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `tokens-creature` | Creates creature tokens | Krenko, Dragon Broodmother, Bitterblossom |
| `tokens-artifact` | Creates treasure, clue, food, or other artifact tokens | Smothering Tithe, Dockside Extortionist, Tireless Tracker |
| `tokens-tribal` | Creates tokens of a specific creature type | Goblin token generators, Zombie token generators |

### evasion-grant (provides, 4,192 cards) → 3 sub-tags

| Sub-tag | Definition | Examples |
|---------|-----------|----------|
| `evasion-flying` | Grants or has flying | Wonder, Archetype of Imagination |
| `evasion-unblockable` | Makes creatures unblockable | Whispersilk Cloak, Rogue's Passage, Aqueous Form |
| `evasion-menace` | Grants menace, fear, intimidate, or similar partial evasion | Goblin War Drums, Archetype of Finality |

## Reclassification Pipeline

### Approach

Focused reclassification — not full retagging. Each card keeps all its other tags; only the target tag is replaced with its sub-tag.

### LLM Prompt Strategy

One prompt template per parent tag. Input: `(card_name, oracle_text, type_line, current_tag)`. Output: JSON with the chosen sub-tag from the allowed list.

Batch ~50 cards per request to stay within token limits. Use OpenAI Batch API for 50% cost reduction.

### Processing

1. Query all `(oracle_id, tag)` pairs from provides/wants for the 6 target tags
2. Join with cards table to get oracle_text and type_line
3. Group by parent tag, write JSONL batch requests
4. Submit to Batch API, poll for completion
5. Parse results: `DELETE` old tag from provides/wants, `INSERT` new sub-tag
6. Cards the LLM can't confidently classify get the `-generic` fallback variant (e.g., `board-generic`)

### Cost Estimate

~40k tag instances across 6 tags → ~800 batch requests → ~$1.50-2.50 via Batch API.

## Multi-category Policy

Cards must receive exactly **one** sub-tag per parent tag. Pick the most specific match:
- `tokens-tribal` takes priority over `tokens-creature` (every tribal token is a creature token)
- `board-tribal` takes priority over `board-go-wide` (tribal count-matters is more specific)
- `pump-lord` takes priority over `pump-anthem` (type-specific buff is more specific)

The reclassification prompt instructs the LLM: "Choose the single most specific sub-category. If the card fits multiple, pick the narrower one."

## Tag Registry Updates

### tag_registry.json

- Remove 6 parent tag entries
- Add 20 new sub-tag entries with:
  - `kind`: preserved from parent (provides or wants)
  - `definition`: from the tables above
  - `aliases`: redistributed from parent tag aliases to best-fit sub-tag

### Alias Migration

The 100+ aliases on parent tags are redistributed:
- `goblin-token-generation` → alias of `tokens-tribal`
- `board-wide-buff` → alias of `pump-anthem`
- `tribal-boost` → alias of `pump-lord`
- `combat-damage-events` → alias of `combat-damage`
- etc.

## SEMANTIC_BRIDGES Updates

Replace parent-to-parent bridges with specific sub-tag bridges at higher weights:

| Old bridge | New bridges |
|-----------|-------------|
| `token-generation → creature-etb` (0.8) | `tokens-creature → etb-value` (0.9), `tokens-tribal → etb-tribal` (0.95) |
| `token-generation → creature-board` (0.7) | `tokens-creature → board-tokens` (0.95), `tokens-creature → board-go-wide` (0.8) |
| `creature-pump → creature-board` (0.6) | `pump-lord → board-tribal` (0.95), `pump-anthem → board-go-wide` (0.9) |

Key insight: sub-tags allow higher bridge weights because the connection is more precise. A lord pumping tribal creatures → near-certain synergy with tribal board payoffs (0.95), vs the old conservative generic bridge (0.6).

All existing bridges referencing parent tags must be audited and remapped or removed. There are ~25 bridge entries referencing these 6 tags — each must be mapped to its sub-tag equivalent during implementation.

## TRIGGER_EFFECT_BRIDGES Updates

`TRIGGER_EFFECT_BRIDGES` in `constants.py` maps effect tags to trigger tags for combo detection. It references parent tags:
- `"token-generation": {"creature-etb"}` → `"tokens-creature": {"etb-value", "etb-tokens"}, "tokens-tribal": {"etb-tribal"}`
- `"creature-pump": {"attack-events"}` → `"pump-lord": {"attack-events"}, "pump-anthem": {"attack-events"}`

All entries referencing the 6 parent tags must be remapped. If missed, combo detection silently breaks because it can no longer match tag names.

## Downstream Code Updates

The following files hardcode parent tag names and must be updated:

| File | What to update |
|------|---------------|
| `normalize_tags.py` (lines 107-136) | Inference rules auto-add parent tags (e.g., `token-generation` → infers `creature-etb`). Must infer sub-tags instead, or remove rules and rely on LLM classification. **Critical — will overwrite reclassification if not fixed.** |
| `ability_parser.py` (lines 112-116) | Keyword-to-tag mappings produce `token-generation` for fabricate/embalm/eternalize/amass/populate. Must produce the appropriate `tokens-*` sub-tag. |
| `strategy_detector.py` (lines 22-108) | Strategy rules reference `token-generation`, `creature-pump`, `evasion-grant`. Must use sub-tag sets (e.g., `{"tokens-creature", "tokens-artifact", "tokens-tribal"}` for the tokens strategy). |
| `swaps.py` (line 29-30) | `SYNERGY_PROVIDES` set includes `token-generation`, `creature-pump`. Must include all sub-tag variants. |
| `prompt_builder.py` | Reads `tag_registry.json` for LLM vocabulary. Will automatically pick up new sub-tags once registry is updated — confirm the prompt instructions are clear enough that the LLM uses sub-tags for new cards. |
| 8+ test files | Tests assert parent tag names in `SEMANTIC_BRIDGES`, combo detection, and tag data. Must be updated to use sub-tag names. |

## Tiebreaker Re-enablement

### Scoring Change (engine.py)

Current (line 490, overlap computed but unused):
```python
info["total"] = score_val * 1000.0 + tower_score * 10.0 + rank_tiebreak * 0.1
```

After:
```python
info["total"] = score_val * 1000.0 + overlap * 20.0 + tower_score * 10.0 + rank_tiebreak * 0.1
```

The `× 20` weight means overlap can shift up to ~100 points within the same LLM tier (where tower contributes ~50-100 points). Overlap becomes the dominant tiebreaker within a score tier.

### board-generic Handling

`board-generic` will likely still be very large (~8,000+ cards). To prevent it from acting as a near-universal tag that undermines the tiebreaker, exclude `board-generic` from the overlap calculation. Only specific sub-tags (`board-tokens`, `board-tribal`, `board-go-wide`) contribute to overlap scoring.

### Tuning Escape Hatch

If `× 20` is too aggressive for some decks, make it IDF-weighted using the existing `compute_idf` function in `mtg_synergy/graph/idf.py` — rare sub-tags get full weight, common ones get reduced. Try the simple version (flat weight + board-generic exclusion) first.

## Validation Plan

1. **Baseline**: Run `compare_edhrec.py --fast --quiet` before changes → record all 15 deck scores
2. **After reclassification** (tiebreaker still off): Re-run comparison to confirm no regression from tag changes alone
3. **After enabling tiebreaker**: Re-run comparison

### Success Criteria

- No deck loses more than 2 points
- Average improves from 13.4/30 toward 15-17/30
- Previously-hurt decks (Sram, Edgar) don't regress

### Failure Response

If a deck regresses: investigate which sub-tags cause false overlap, tune the `× 20` weight, or add specific sub-tags to an exclusion list.

## Out of Scope

- Retraining the tower model (uses embeddings, not tags)
- Re-running LLM synergy scoring (tags are a tiebreaker, not the primary signal)
- Changing mechanics extraction
- Splitting any tags beyond the 6 identified targets
