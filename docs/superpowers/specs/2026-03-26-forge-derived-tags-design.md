# Forge-Derived Tags — Design Spec

**Date**: 2026-03-26
**Goal**: Replace LLM-generated `provides`/`wants` tags with deterministic tags derived from Forge's structured ability data. Same tables, same consumers, zero LLM cost, auto-maintained on Forge import.

## Why

- LLM tags cost money to regenerate and are unreliable (hallucinations, inconsistency)
- Forge already encodes what each card does in structured code format (59k abilities, 93% card coverage)
- Two parallel tag vocabularies (LLM tags vs Forge DeckHas/DeckHints) create confusion
- Tags should be derivable from game mechanics, not invented by LLMs
- The provides/wants graph is the weakest signal (6.6% candidate coverage vs causal 13.3%) — improving tag quality won't hurt, simplifying maintenance helps

## Current State

| Source | Coverage | Maintenance | Cost |
|--------|----------|-------------|------|
| LLM provides/wants (batch_tagger.py) | 98% (33,550 cards) | Re-run LLM per set | $0.50/1000 cards |
| Forge abilities | 93% (31,696 cards) | Auto from Forge import | $0 |
| Forge DeckHas/DeckHints | 21% (7,171 cards) | Auto from Forge import | $0 |

## Design: One Forge Verb = One Tag

Each Forge verb maps 1:1 to a provides tag. No merging of different game actions — a card that cares about `Surveil` is different from one that cares about `Scry`.

**Naming convention**: Provides tags are action nouns (what the card does: `draw`, `destroy`, `token`). Wants tags are event nouns (what must happen to trigger: `dies`, `spell-cast`, `enters-battlefield`).

### New Script: `derive_forge_tags.py`

Reads `forge_abilities` + `forge_svars` + `cards.type_line`, writes to existing `provides`/`wants` tables (wipe and repopulate). Runs after every Forge import.

### Provides Rules (from Forge verbs)

| Forge verb | Provides tag | Notes |
|---|---|---|
| `Token` | `token` | Default for creature tokens |
| `Token` (Treasure/Clue/Food/Blood) | `token-treasure` / `token-clue` / `token-food` / `token-blood` | Parse from token_script |
| `Draw` | `draw` | |
| `Mana` / `ManaReflected` | `mana` | Mana production |
| `DealDamage` | `deal-damage` | Single target |
| `DamageAll` | `deal-damage-all` | Board-wide damage |
| `Destroy` | `destroy` | Single target removal |
| `DestroyAll` | `destroy-all` | Board wipe |
| `PutCounter` | `put-counter` | Single target |
| `PutCounterAll` | `put-counter-all` | Board-wide counters |
| `RemoveCounter` | `remove-counter` | |
| `MultiplyCounter` | `multiply-counter` | |
| `GainLife` | `gain-life` | |
| `LoseLife` | `lose-life` | Drain/life loss |
| `Pump` | `pump` | Single target buff |
| `PumpAll` | `pump-all` | Board-wide buff |
| `Debuff` | `debuff` | Remove keywords/abilities |
| `Mill` | `mill` | |
| `Scry` | `scry` | |
| `Surveil` | `surveil` | Different from scry — cards enter graveyard |
| `Dig` / `DigUntil` | `dig` | Look at top N, pick some |
| `Discard` | `discard` | Force discard |
| `Sacrifice` (as verb, not cost) | `force-sacrifice` | Forces opponent to sacrifice |
| `SacrificeAll` | `force-sacrifice-all` | |
| `Counter` | `counter-spell` | Counter a spell/ability |
| `GainControl` | `gain-control` | Steal effects |
| `CopyPermanent` / `Clone` | `copy-permanent` | |
| `CopySpellAbility` | `copy-spell` | |
| `ReduceCost` | `reduce-cost` | |
| `RaiseCost` | `raise-cost` | Tax effects |
| `Animate` / `AnimateAll` | `animate` | Permanent becomes creature (NOT reanimation) |
| `ChangeZone` | *(zone-dependent, see below)* | Parse `Origin$`/`Destination$` from raw_line |
| `ChangeZoneAll` | `change-zone-all` | Mass zone change |
| `Untap` | `untap` | |
| `UntapAll` | `untap-all` | |
| `Tap` | `tap-target` | Tap opponent's stuff |
| `TapAll` | `tap-all` | |
| `Fight` | `fight` | |
| `Proliferate` | `proliferate` | |
| `Explore` | `explore` | |
| `Connive` | `connive` | |
| `Investigate` | `investigate` | (also provides `token-clue`) |
| `Goad` / `MustAttack` | `goad` | Force attack |
| `Amass` | `amass` | |
| `Fog` | `fog` | Damage prevention |
| `PreventDamage` | `prevent-damage` | |
| `Regenerate` | `regenerate` | |
| `CantBlockBy` | `evasion-grant` | "Can't be blocked by..." |
| `CantBlock` | `restrict-block` | Target can't block |
| `CantAttack` | `restrict-attack` | Stax/pillow-fort |
| `Play` | `free-cast` | Cast without paying mana cost |
| `Seek` | `tutor` | Search library (Arena digital tutor) |
| `AddTurn` | `extra-turn` | |
| `Attach` | `attach` | Equipment/aura auto-attach |
| `CastWithFlash` | `flash-grant` | Grant flash to other cards |
| `Protection` | `protection-grant` | Protection from colors/types |
| `SetState` | `set-state` | Phase out, transform, etc. |
| `PeekAndReveal` | `peek` | Top-of-library matters |

### ChangeZone Verb Rules (zone-dependent provides)

For `ChangeZone` effects (verb, not trigger), parse `Origin$` and `Destination$` from `raw_line`:

| Origin | Destination | Provides tag |
|---|---|---|
| `Graveyard` | `Battlefield` | `reanimate` |
| `Graveyard` | `Hand` | `graveyard-to-hand` |
| `Battlefield` | `Graveyard` / `Exile` | `remove` |
| `Hand` / `Library` | `Battlefield` | `cheat-into-play` |
| *(other)* | *(other)* | `change-zone` (generic fallback) |

### Skipped Verbs

These Forge verbs are too generic, are wrapper/internal mechanics, or have <20 occurrences:

- `Continuous` — static effects, too broad (covered by keywords)
- `Effect` — generic wrapper
- `Charm` — modal spell, sub-abilities carry the real verbs
- `DelayedTrigger` / `RepeatEach` — timing/iteration mechanics
- `ChooseCard` / `ChooseType` / `ChooseColor` / `ChoosePlayer` / `ChooseSource` — selection mechanics
- `GenericChoice` / `Branch` — decision mechanics
- `AlternativeCost` / `OptionalCost` — cost mechanics
- `Cleanup` / `StoreSVar` — internal bookkeeping
- Verbs with <20 occurrences (niche: `Subgame`, `RestartGame`, `Meld`, etc.)

### Provides Rules (from cost parsing)

| Cost pattern | Provides tag |
|---|---|
| `Sac` in cost string | `sacrifice-outlet` |
| `T` in cost (tap self) | `tap-ability` |

### Provides Rules (from keywords)

| Keyword | Provides tag |
|---|---|
| `Flying` | `flying` |
| `Trample` | `trample` |
| `Haste` | `haste` |
| `Vigilance` | `vigilance` |
| `Lifelink` | `lifelink` |
| `Deathtouch` | `deathtouch` |
| `First Strike` | `first-strike` |
| `Double Strike` | `double-strike` |
| `Menace` | `menace` |
| `Reach` | `reach` |
| `Flash` | `flash` |
| `Hexproof` | `hexproof` |
| `Indestructible` | `indestructible` |
| `Defender` | `defender` |
| `Equip` | `equip` |
| `Enchant` | `enchant` |
| `Prowess` | `prowess` |
| `Flashback` | `flashback` |
| `Cycling` | `cycling` |
| `Madness` | `madness` |
| `Changeling` | `changeling` |
| `Crew` | `crew` |
| `Ward` | `ward` |
| `Affinity` | `affinity` |
| `Convoke` | `convoke` |
| `Shroud` | `shroud` |
| `Landwalk` | `landwalk` |

### Provides Rules (tribal — three sources)

| Source | Example | Tag |
|---|---|---|
| `cards.type_line` contains creature type | Goblin Chieftain | `goblin-tribal` |
| `token_script` contains type | Dragon Fodder (`r_1_1_goblin`) | `goblin-tribal` |
| `trigger_filter` contains type | Boggart Shenanigans (`Goblin.YouCtrl`) | `goblin-tribal` |

Tribal types extracted by parsing token_script names and trigger_filter patterns for known MTG creature types (Goblin, Zombie, Elf, Human, Dragon, Vampire, Merfolk, Soldier, Spirit, Angel, Demon, Dinosaur, Beast, Bird, Cat, Dog, Rat, etc.). Use the same type list as the existing `fix-tribal` logic.

### Wants Rules (from Forge triggers + zone info)

| Trigger mode | Origin | Destination | Wants tag |
|---|---|---|---|
| `ChangesZone` | `Battlefield` | `Graveyard` | `dies` |
| `ChangesZone` | `Any` / NULL | `Battlefield` | `enters-battlefield` |
| `ChangesZone` | `Battlefield` | `Exile` | `exiled` |
| `ChangesZone` | `Graveyard` | `Battlefield` | `leaves-graveyard` |
| `ChangesZone` | `Any` | `Graveyard` | `enters-graveyard` |
| `ChangesZone` | `Battlefield` | `Hand` | `bounced` |
| `ChangesZone` | `Battlefield` | `Any` | `leaves-battlefield` (fallback) |
| `ChangesZoneAll` | — | — | `mass-zone-change` |
| `Attacks` | — | — | `attacks` |
| `AttackersDeclared` | — | — | `attackers-declared` |
| `AttackerBlocked` / `AttackerBlockedByCreature` | — | — | `attacker-blocked` |
| `AttackerUnblocked` | — | — | `attacker-unblocked` |
| `Blocks` | — | — | `blocks` |
| `SpellCast` / `SpellCastOrCopy` | — | — | `spell-cast` |
| `DamageDone` / `DamageDoneOnce` | — | — | `damage-done` |
| `Sacrificed` | — | — | `sacrificed` |
| `LifeGained` | — | — | `life-gained` |
| `LifeLost` | — | — | `life-lost` |
| `Drawn` | — | — | `card-drawn` |
| `Discarded` / `DiscardedAll` | — | — | `discarded` |
| `Phase` | — | — | `phase-trigger` |
| `CounterAdded` / `CounterAddedOnce` | — | — | `counter-added` |
| `Taps` | — | — | `tapped` |
| `Untaps` | — | — | `untapped` |
| `BecomesTarget` | — | — | `becomes-target` |
| `Cycled` | — | — | `cycled` |
| `Scry` (as trigger) | — | — | `scry-trigger` |
| `Surveil` (as trigger) | — | — | `surveil-trigger` |
| `TapsForMana` | — | — | `taps-for-mana` |
| `LandPlayed` | — | — | `land-played` |
| `Transformed` | — | — | `transformed` |
| `TurnFaceUp` | — | — | `turn-face-up` |
| `Exploited` | — | — | `exploited` |
| `TokenCreated` / `TokenCreatedOnce` | — | — | `token-created` |
| `Crewed` / `BecomesCrewed` | — | — | `crewed` |
| `Mutates` | — | — | `mutated` |
| `Explores` | — | — | `explored` |

### Wants Rules (from cost parsing)

| Cost pattern | Wants tag |
|---|---|
| `Sac` in cost string | `sacrifice-fodder` |

### Wants Rules (tribal from trigger_filter)

When `trigger_filter` contains a creature type (e.g., `Goblin.YouCtrl`, `Creature.Zombie+Other`), add wants tag `{type}-tribal`. This captures "this card triggers when a Goblin does something" → wants Goblins.

## Action-to-Event Bridges

Since provides tags are actions and wants tags are events, the graph builder needs bridges to connect them. Replace the old `SEMANTIC_BRIDGES` with a smaller, deterministic `ACTION_EVENT_BRIDGES` dict:

```python
ACTION_EVENT_BRIDGES = {
    # provides tag → wants tag (action causes this event)
    ("draw", "card-drawn"),
    ("token", "token-created"),
    ("token", "enters-battlefield"),
    ("deal-damage", "damage-done"),
    ("deal-damage-all", "damage-done"),
    ("gain-life", "life-gained"),
    ("lose-life", "life-lost"),
    ("mill", "enters-graveyard"),
    ("sacrifice-outlet", "sacrificed"),
    ("sacrifice-outlet", "dies"),
    ("destroy", "dies"),
    ("destroy-all", "dies"),
    ("force-sacrifice", "sacrificed"),
    ("force-sacrifice", "dies"),
    ("put-counter", "counter-added"),
    ("put-counter-all", "counter-added"),
    ("discard", "discarded"),
    ("reanimate", "enters-battlefield"),
    ("reanimate", "leaves-graveyard"),
    ("goad", "attacks"),
    ("surveil", "enters-graveyard"),
    ("explore", "enters-graveyard"),  # explore can mill
    ("proliferate", "counter-added"),
}
```

These are deterministic MTG rules: destroying a creature causes it to die, drawing triggers card-drawn events, etc. ~25 bridges vs the old 100+ SEMANTIC_BRIDGES.

## DFC / Adventure / Split Card Handling

Forge stores back faces and adventure halves as separate card files with separate names.

**Rules:**
1. **DFC front face**: Maps to Scryfall oracle_id via `forge_name_map`. Gets all tags from its Forge file.
2. **DFC back face**: If `forge_name_map` maps it to the SAME oracle_id as the front face, merge tags. If no mapping, skip (back face abilities are secondary).
3. **Adventure halves**: Stored as separate Forge files (e.g., `Stomp` for Bonecrusher Giant). Map via `forge_name_map` — if the adventure half maps to the parent card's oracle_id, merge tags. Otherwise skip.
4. **Split cards**: Same as DFC — each half may map separately. Merge into one oracle_id if both halves map.

The `forge_name_map` table already handles most of this — it maps Forge names to oracle_ids. Cards with no oracle_id mapping are skipped (~254 cards, <1% loss).

## Role Derivation

The `cards.role` column (currently populated by LLM via `batch_tagger.py`) is used by the swap system for infrastructure protection. Derive it from Forge verbs:

| Forge verb(s) | Role |
|---|---|
| `Destroy` / `DestroyAll` / `Counter` / `ChangeZone` to exile | `removal` |
| `Mana` / `ManaReflected` / `ReduceCost` | `ramp` |
| `Draw` / `Dig` / `DigUntil` / `Scry` / `Surveil` | `draw` |
| `Regenerate` / `PreventDamage` / `Fog` + `Hexproof` / `Indestructible` / `Ward` keywords | `protection` |
| `Token` / `Pump` / `PumpAll` / `DealDamage` + no other roles | `threat` |
| `cards.type_line` LIKE `%Land%` | `land` |
| *(fallback)* | `utility` |

First matching role wins (priority order as listed). This replaces the LLM-assigned role with deterministic derivation.

## Pipeline Integration

### New-set update workflow (updated)

```bash
python3 download_cards.py                          # 1. Refresh Scryfall
python3 import_forge.py --download --import        # 2. Update Forge data
python3 derive_forge_tags.py                       # 3. Derive provides/wants/role from Forge (NEW)
python3 ability_parser.py                          # 4. Parse abilities
python3 strategy_detector.py --populate            # 5. Strategies
python3 oracle_parser.py --parse-all --top 5000    # 6. Parse oracle text
python3 build_graph.py --rebuild                   # 7. Rebuild causal graph
python3 fetch_edhrec_decks.py --refresh            # 8. Refresh EDHREC data
python3 train_fusion_model.py                      # 9. Retrain fusion model
```

Step 3 replaces the old: `batch_tagger.py` → `tag_db.py import` → `tag_db.py backfill` → `fix-tribal` → `rebuild-registry` → `reclassify_tags.py`

### `derive_forge_tags.py` internals

```
1. Load forge_abilities + forge_name_map (oracle_id mapping)
2. For each card with Forge data:
   a. Apply verb→provides rules (including ChangeZone zone parsing from raw_line)
   b. Apply trigger→wants rules (using trigger_origin/trigger_destination columns)
   c. Apply cost→provides/wants rules
   d. Apply keyword→provides rules
   e. Apply tribal rules (type_line + token_script + trigger_filter)
   f. Derive role from verbs/keywords
3. Wipe provides/wants tables
4. Bulk INSERT new tags
5. Update cards.role column
6. Print stats: cards tagged, tags per card, coverage
```

### Files

| File | Action |
|---|---|
| `derive_forge_tags.py` | **NEW** — main derivation script |
| `provides` / `wants` tables | Repopulated with Forge-derived tags |
| `cards.role` column | Repopulated with Forge-derived roles |
| `batch_tagger.py` | No longer needed for provides/wants/role |
| `reclassify_tags.py` | **REMOVED** — sub-tag reclassification no longer needed |
| `tag_db.py fix-tribal` | Absorbed into `derive_forge_tags.py` |
| `SEMANTIC_BRIDGES` in constants.py | **REPLACED** by `ACTION_EVENT_BRIDGES` (~25 deterministic bridges) |
| `TRIGGER_EFFECT_BRIDGES` in constants.py | Updated to new tag vocabulary |
| `graph/edges.py` | Unchanged — reads from same `provides`/`wants` tables, uses new bridges |
| `scoring.py` | Unchanged — tag overlap features read from same tables |
| `mtg_synergy/recommend/swaps.py` | **UPDATED** — `SYNERGY_PROVIDES`, `INFRASTRUCTURE_PROVIDES` updated to new tag names |
| `CLAUDE.md` | Updated with new pipeline and tag vocabulary |

## Success Criteria

| Metric | Target |
|---|---|
| Coverage | ≥90% of cards get at least one provides tag (Forge covers 93% of cards) |
| Accuracy | Spot-check 20 cards: Forge-derived tags match card function |
| Role accuracy | Spot-check: Krenko=threat, Sol Ring=ramp, Swords=removal, Rhystic=draw |
| Recall@100 | Fusion model stays within 2% of 88.7% baseline (retrain after migration) |
| Pipeline time | `derive_forge_tags.py` runs in <60s for all 32k cards |
| Simplification | Remove `batch_tagger.py` dependency, `reclassify_tags.py`, old `SEMANTIC_BRIDGES` |

**Note**: The fusion model's `cmdr_tag_overlap` feature will produce different values with the new vocabulary. The model should be retrained after tag migration (`train_fusion_model.py`).
