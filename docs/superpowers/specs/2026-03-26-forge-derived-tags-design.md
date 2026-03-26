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

### New Script: `derive_forge_tags.py`

Reads `forge_abilities` + `forge_svars` + `cards.type_line`, writes to existing `provides`/`wants` tables (wipe and repopulate). Runs after every Forge import.

### Provides Rules (from Forge verbs)

| Forge verb | Provides tag | Notes |
|---|---|---|
| `Token` | `token` | Default for creature tokens |
| `Token` (Treasure/Clue/Food/Blood) | `token-treasure` / `token-clue` / `token-food` / `token-blood` | Parse from token_script |
| `Draw` | `draw` | |
| `Mana` | `mana` | Mana production |
| `ManaReflected` | `mana` | |
| `DealDamage` | `deal-damage` | Single target |
| `DamageAll` | `deal-damage-all` | Board-wide damage |
| `Destroy` | `destroy` | Single target removal |
| `DestroyAll` | `destroy-all` | Board wipe |
| `PutCounter` | `put-counter` | Single target |
| `PutCounterAll` | `put-counter-all` | Board-wide counters |
| `RemoveCounter` | `remove-counter` | |
| `GainLife` | `gain-life` | |
| `LoseLife` | `lose-life` | Drain/life loss |
| `Pump` | `pump` | Single target buff |
| `PumpAll` | `pump-all` | Board-wide buff |
| `Mill` | `mill` | |
| `Scry` | `scry` | |
| `Surveil` | `surveil` | Different from scry — cards enter graveyard |
| `Dig` | `dig` | Look at top N, pick some |
| `Discard` | `discard` | Force discard |
| `Sacrifice` (as verb, not cost) | `force-sacrifice` | Forces opponent to sacrifice |
| `SacrificeAll` | `force-sacrifice-all` | |
| `Counter` | `counter-spell` | Counter a spell/ability |
| `GainControl` | `gain-control` | Steal effects |
| `CopyPermanent` | `copy-permanent` | |
| `CopySpellAbility` | `copy-spell` | |
| `ReduceCost` | `reduce-cost` | |
| `RaiseCost` | `raise-cost` | Tax effects |
| `Animate` | `animate` | Return to battlefield |
| `ChangeZone` dest=Battlefield from Graveyard | `reanimate` | Graveyard → battlefield |
| `ChangeZone` dest=Hand from Graveyard | `graveyard-to-hand` | |
| `ChangeZone` dest=Exile | `exile` | Exile removal |
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
| `Goad` | `goad` | |
| `Amass` | `amass` | |
| `Fog` | `fog` | Damage prevention |
| `PreventDamage` | `prevent-damage` | |
| `Regenerate` | `regenerate` | |
| `SetState` | `set-state` | Phase out, transform, etc. |
| `Continuous` | *(skip — too generic, handled by keywords)* | |
| `Effect` | *(skip — wrapper verb)* | |
| `Charm` | *(skip — modal, sub-abilities carry the verbs)* | |

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
| `Attacks` | — | — | `attacks` |
| `AttackersDeclared` | — | — | `attackers-declared` |
| `AttackerBlocked` | — | — | `attacker-blocked` |
| `AttackerUnblocked` | — | — | `attacker-unblocked` |
| `Blocks` | — | — | `blocks` |
| `SpellCast` | — | — | `spell-cast` |
| `DamageDone` | — | — | `damage-done` |
| `DamageDoneOnce` | — | — | `damage-done` |
| `Sacrificed` | — | — | `sacrificed` |
| `LifeGained` | — | — | `life-gained` |
| `LifeLost` | — | — | `life-lost` |
| `Drawn` | — | — | `card-drawn` |
| `Discarded` | — | — | `discarded` |
| `Phase` (Beginning/Upkeep/Draw/End) | — | — | `phase-trigger` |
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
| `Exploited` | — | — | `exploited` |
| `TokenCreated` | — | — | `token-created` |
| `Crewed` | — | — | `crewed` |
| `Mutates` | — | — | `mutated` |
| `Explores` | — | — | `explored` |

### Wants Rules (from cost parsing)

| Cost pattern | Wants tag |
|---|---|
| `Sac` in cost string | `sacrifice-fodder` |

### Wants Rules (tribal from trigger_filter)

When `trigger_filter` contains a creature type (e.g., `Goblin.YouCtrl`, `Creature.Zombie+Other`), add wants tag `{type}-tribal`. This captures "this card triggers when a Goblin does something" → wants Goblins.

### Skipped Verbs

These Forge verbs are too generic or are wrapper/internal mechanics that don't map to meaningful synergy tags:

- `Continuous` — static effects, too broad (covered by keywords)
- `Effect` — generic wrapper
- `Charm` — modal spell, sub-abilities carry the real verbs
- `DelayedTrigger` — timing mechanic, not a game action
- `RepeatEach` — iteration mechanic
- `ChooseCard` / `ChooseType` / `ChooseColor` / `ChoosePlayer` / `ChooseSource` — selection mechanics
- `GenericChoice` / `Branch` — decision mechanics
- `AlternativeCost` / `OptionalCost` — cost mechanics
- `Cleanup` — internal bookkeeping
- `StoreSVar` — internal variable storage

Verbs with <30 occurrences are also skipped (niche mechanics like `Subgame`, `RestartGame`, `Meld`).

## Pipeline Integration

### New-set update workflow (updated)

```bash
python3 download_cards.py                          # 1. Refresh Scryfall
python3 import_forge.py --download --import        # 2. Update Forge data
python3 derive_forge_tags.py                       # 3. Derive provides/wants from Forge (NEW)
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
   a. Apply verb→provides rules
   b. Apply trigger→wants rules
   c. Apply cost→provides/wants rules
   d. Apply keyword→provides rules
   e. Apply tribal rules (type_line + token_script + trigger_filter)
3. Wipe provides/wants tables
4. Bulk INSERT new tags
5. Print stats: cards tagged, tags per card, coverage
```

### Files

| File | Action |
|---|---|
| `derive_forge_tags.py` | **NEW** — main derivation script |
| `provides` / `wants` tables | Repopulated with Forge-derived tags |
| `batch_tagger.py` | No longer needed for provides/wants (kept for other uses) |
| `reclassify_tags.py` | **REMOVED** — sub-tag reclassification no longer needed |
| `tag_db.py fix-tribal` | Absorbed into `derive_forge_tags.py` |
| `tag_db.py rebuild-registry` | Still needed if registry used elsewhere |
| `SEMANTIC_BRIDGES` in constants.py | **REMOVED** — no longer needed; Forge tags are specific enough |
| `graph/edges.py` | Unchanged — reads from same `provides`/`wants` tables |
| `scoring.py` | Unchanged — tag overlap features read from same tables |
| `CLAUDE.md` | Updated with new pipeline |

## Success Criteria

| Metric | Target |
|---|---|
| Coverage | ≥90% of cards get at least one provides tag (currently 98% with LLM, Forge covers 93%) |
| Accuracy | Spot-check 20 cards: Forge-derived tags match card function |
| Recall@100 | Fusion model stays within 2% of 88.7% baseline |
| Pipeline time | `derive_forge_tags.py` runs in <60s for all 32k cards |
| Simplification | Remove `batch_tagger.py` dependency, `reclassify_tags.py`, `SEMANTIC_BRIDGES` |

## Tag Vocabulary Summary

~50 provides tags (from verbs + keywords + tribal):
- Game actions: `token`, `draw`, `mana`, `deal-damage`, `destroy`, `put-counter`, `mill`, `scry`, `surveil`, `dig`, `discard`, `counter-spell`, `gain-life`, `lose-life`, `pump`, `pump-all`, `sacrifice-outlet`, `exile`, `reanimate`, etc.
- Keywords: `flying`, `trample`, `haste`, `lifelink`, `deathtouch`, `flash`, `hexproof`, `equip`, etc.
- Tribal: `goblin-tribal`, `zombie-tribal`, `elf-tribal`, etc.

~30 wants tags (from triggers + zones + costs):
- Zone triggers: `dies`, `enters-battlefield`, `enters-graveyard`, `exiled`, `bounced`
- Combat: `attacks`, `blocks`, `attacker-blocked`, `attacker-unblocked`
- Events: `spell-cast`, `damage-done`, `sacrificed`, `life-gained`, `card-drawn`, `discarded`, `counter-added`, `land-played`, `token-created`
- Costs: `sacrifice-fodder`
- Tribal: `goblin-tribal`, `zombie-tribal`, etc. (from trigger_filter)
