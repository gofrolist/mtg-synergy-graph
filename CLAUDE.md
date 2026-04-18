# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.246, Hi-Syn 224/1000 on the 100-commander golden set.
Filter-axis generalization (2026-04-18):
- Extract the subject type (Land, Creature, Artifact, creature subtype) from a
  commander's ChangesZone BF→GY trigger filter, then narrow cost_feeds_trigger
  candidates to sacrifice costs whose Sac<N/X> target aligns. Replaces
  commander-specific hand-coded rules — the same logic now lifts any future
  card/commander following the pattern.
- Generic subject_zone_feeder rule (non-Creature subjects only): matches
  effect=Sacrifice SacValid=<subject> and mass ChangeZoneAll returns from
  Graveyard. Scope filter rejects opponent-forcing effects on YouCtrl triggers.
- Generic counter_axis_feeder rule: extracts `counters_GE_<TYPE>` qualifier
  from any commander port (trigger / scales_with / static) on non-Self scope.
  Matches candidates on 4 tiers (payoff / producer / etb_counter / self_recur).
- Result: Titania 0.1703 → 0.3210, Marchesa 0.0211 → 0.0476, Hamza 0.0750 →
  0.1094, all via filter-axis extraction with zero commander-specific code.
  Aggregate golden-set NDCG 0.243525 → 0.245677, no regressions.
Non-golden commander coverage (2026-04-18):
- `creatures_as_lands_landfall`: detects Ashaya's type-bending static
  (Affected=Creature, AddType=Land) and emits landfall-payoff matches
  (ChangesZone Land ETB + LandPlayed triggers). Field of the Dead /
  Lotus Cobra / Rampaging Baloths / Scute Swarm now surface for Ashaya.
- `combat_enhancer` broadened: now also fires for Attacks-Self triggers
  when the commander's effect chain contains an engine effect
  (AddPhase / Dig / Play / Mana / Token / DealDamage / Discard / Mill)
  or ≥2 value effects. Etali / Scourge of the Throne / Narset now surface
  extra-combat spells (Relentless Assault, Aggravated Assault, Seize
  the Day). Zur / Wyleth excluded — single Draw or single ChangeZone
  tutor is voltron, not an extra-combat engine.
- Aggregate golden-set NDCG 0.245677 → 0.246137, Hi-Syn 222 → 224.
Vanilla tribal-anchor fallback (2026-04-18):
- `tribal_density` rule now falls back to the commander's literal
  creature subtypes when the commander is a *vanilla anchor* (only
  keyword ports, no triggers / effects / statics). Akroma (Angel),
  Ghalta (Dinosaur), Rorix (Dragon), Grumgully (Goblin Shaman), Konda
  (Samurai) — their EDHREC Hi-Syn is dominated by the tribe but no
  other rule emits a match because they have no mechanical structure.
- Skiplist `{Human, Warrior, Soldier}` — pools too large (Human ~4300)
  or not the recognized EDHREC tribal axis.
- Across 2,559 non-golden commanders: 16 commanders improved (Konda
  +0.67, Moritte +0.49, Leonardo +0.27, Akroma +0.18, Rorix +0.14),
  6 regressed (Gorm -0.23, Zetalpa -0.01 — voltron commanders whose
  Hi-Syn isn't tribal). Net +0.001 aggregate across the broad set;
  golden-set NDCG unchanged.
Flicker gate + creature-count scaling (2026-04-18):
- `flicker_synergy` now fires when the commander's ETB has a
  temporary-exile ChangeZone effect (`Battlefield → Exile` with a
  `ReturnAbility` clause in raw_line). Lagrella, the Magpie's
  "exile-until-she-leaves" engine qualifies. Plain bounce
  (`→ Hand`), saga-timed exile (Vorinclex, Joshua), and reanimation
  (`Graveyard →`, Sharuum) are rejected.
- `scales_with Valid Creature.YouCtrl` (pure creature-count scaling,
  no counter qualifier) now emits token-producer and populate
  complements. Narrow gate: commander must have no trigger / effect
  / cost / replacement port AND all statics must be self-scoped
  (`Affected: Card.Self` or `ValidTarget: Card.Self`). Shanna, Sisay's
  Legacy qualifies; Adeline (attack trigger) and Ghalta (`ReduceCost
  ValidCard`) stay out.
- Non-golden set: 2 commanders crack 0 → non-zero (Lagrella 0 → 0.174,
  Shanna 0 → 0.169). Zero golden-set regressions, 5 new tests.
GY-replay keyword grant + Detain + Domain (2026-04-18):
- `_wants_gy_fill` now also fires when the commander has a Continuous
  static that grants a GY-replay keyword (Unearth / Embalm /
  Eternalize / Encore / Escape / Flashback / Jump-start) to creature
  cards in the graveyard. Sedris, the Traitor King (Unearth) and
  Sliver Gravemother (Encore) — both previously had zero complements
  because their mechanic is "fill the GY, play creatures from it" but
  neither has an explicit ChangeZone GY→BF effect port.
- `flicker_synergy` now accepts `Detain` as a high-value ETB effect.
  Lavinia of the Tenth's "detain opponents' permanents on ETB" is
  mechanically the same shape as Lagrella's temporary exile —
  flickering re-detains new targets.
- `scales_with Domain` now matches basic-land-type adders
  (Prismatic Omen, Dryad of the Ilysian Grove, Nylea's Presence).
  Radha, Coalition Warlord / Nael, Avizoa Aeronaut both scale with
  Domain count.
- Non-golden set: +5 commanders improved (Radha +0.37, Lavinia +0.32,
  Nael +0.29, Sedris +0.20, Zar Ojanen +0.19), zero regressions,
  zero golden-set impact. 835 tests (5 new).
combat_enhancer tightened to is_combat-only (2026-04-18):
- `_find_combat_enhancers` DamageDone branch now requires the
  trigger port's `is_combat` flag (Forge sets this iff the raw trigger
  has `CombatDamage: True`). Spell-damage commanders (Imodane, the
  Pyrohammer / Ghyrson Starn, Kelermorph — their DamageDone triggers
  on Instant.YouCtrl, Sorcery.YouCtrl or Card.Other+YouCtrl with
  `DamageAmount EQ1` rather than CombatDamage: True) were previously
  picked up by combat_enhancer and flooded with extra-combat spells
  that their burn-doubler archetype doesn't want.
- Edward Kenway (Vehicle.YouCtrl + CombatDamage: True) and Saskia
  (Creature.YouCtrl + CombatDamage: True) still qualify — the
  is_combat flag directly captures the distinction rather than
  needing a filter-type allowlist.
- Non-golden set: 3 commanders improved (Ghyrson Starn +0.017,
  Taii Wakeen +0.015, Auntie Blyte +0.027), zero regressions. 3
  new tests.
Port extraction: 108,644 ports from 32,327 cards (GenericChoice + StaticAbilities$
expansion, deduped after A1's 2^N re-walk fix).

### Extra port_attributes (2026-04-16)

Beyond the standard `valid_filter` explosion, the importer also emits:
- `attr_kind='change_type'` for ChangeZone effect ChangeType$ clauses
  (Kaalia's Angel/Demon/Dragon cheat-into-play list).
- `attr_kind='token_color'` + `attr_kind='token_subtype'` for every TokenScript
  produced by a Token effect (including multi-color prefixes like `gw`, `all`
  and artifact-creature format like `c_0_1_a_thopter`).

### card_hints table (2026-04-16)

Normalised projection of Forge's AI annotations:
- `DeckNeeds`, `DeckHints`, `DeckHas` → kind `needs`/`hints`/`has`.
- `BuffedBy` SVar → kind `buffed_by`.

Populated by the importer alongside `cards`. Not yet used by any complement
rule — exploratory rules (`deck_hint_match`, `deck_needs_fulfilled`,
`buffed_by_match`) were prototyped and reverted: each regressed NDCG@30
against EDHREC because the curated matches are broad (e.g. every Aura, every
Token-producer) and dilute the mechanical-port signal. Retained as data
infrastructure for future work.

### Legality filter (2026-04-16)

`cards.legal_commander` is populated from Scryfall's `legalities.commander`
(1 = legal, 0 = not_legal / banned). Cards with `legal_commander=0` (~1,679
rows, mostly silver-border/acorn/Unfinity leakage through the Forge cardsfolder)
are hard-dropped by `SynergyEngine.page()` and `legal_cards()` before scoring —
they can never surface even if they match every mechanical rule. Columns in the
synergy.db `cards` table default to 1 when the upstream Scryfall DB lacks the
column (legacy test fixtures).

## Common Commands

```bash
uv run python scripts/import_cardsfolder.py                              # Import fresh DB
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain
uv run python scripts/compare_edhrec.py --commanders tests/fixtures/golden_set.json
uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json
uv run pytest tests/                                                     # 649 tests, ~1s
```

## Scoring Architecture — Universal Port Matcher

Score = count of distinct mechanical interactions between commander ports
and candidate ports, weighted by specificity (IDF). **No hand-tuned weights.
No global penalties.** The commander's ports ARE the query.

### Complement Rules (`complement_rules.py`)

~10 `ComplementRule` objects define when a (commander_port, candidate_port)
pair creates a synergy. Each wraps an existing mechanical map.

| Rule | Commander Port | Candidate Port | Source |
|------|---------------|---------------|--------|
| trigger_effect | trigger | effect | EVENT_MATCH_MAP |
| cost_feeds_trigger | trigger | cost | COST_FEEDS_TRIGGER |
| trigger_resonance | trigger | trigger | shared event axis |
| effect_resonance | effect | effect | resonant effects |
| replacement_resonance | effect | replacement | doubler matching |
| replacement_producer | replacement | effect | producer matching |
| replacement_blocks | trigger | replacement | anti-synergy |
| sacrifice_cluster | trigger | trigger | death-axis clustering |
| effect_feeds_trigger | effect | trigger | inverted EVENT_MATCH_MAP |
| lord | card subtypes | static | tribal lord matching |
| etb_self | trigger filter | card identity | ETB/dies self-matching |
| scaling | scales_with | card type | Aura/Equipment density |
| spell_density | SpellCast filter | card type | spell-type density |
| tribal_density | token subtypes (gated) | card subtype | tribal density (suppressed for Conspire) |
| zone_resonance | ChangesZone filter | ChangesZone trigger | landfall resonance |
| sacrifice_outlets | ChangesZone death | sacrifice cost | sac outlets for death cmdr |
| panharmonicon | Panharmonicon static | doubled trigger type | Yarok/Isshin/Teysa |
| graveyard_filler | GY-reanimate/cast | self-mill, discard | Meren/Karador/Kess |
| scales_with_density | scales_with port | density contributors | Phenax/Ezuri/Rakdos |
| extra_land_plays | AdjustLandPlays static | landfall triggers | Azusa |
| flicker_synergy | self-ETB trigger | exile+return effects | Gonti |
| cost_payoff | typed discard cost | graveyard return, Retrace/Dredge | Borborygmos |
| opponent_forcing | opp-facing trigger | opponent-targeting effects | Tergrid/Nekusar |
| token_producer | ChangesZone Creature trigger | Token effects | Purphoros |
| go_wide | Continuous creature pump | Token effects | Jetmir |
| voltron | Hexproof/Exalted keyword | Auras and Equipment | Sigarda/Rafiq |
| etb_sac_target | GY reanimate effect | self-ETB + sacrifice cost creatures | Meren/Karador |
| combat_enhancer | DamageDone trigger OR Attacks-Self trigger w/ engine effect (AddPhase/Dig/Play/Mana/Token/DealDamage/Discard/Mill, or 2+ value effects) | AddPhase + Double Strike | Saskia / Etali / Scourge of the Throne |
| wheel_synergy | Drawn trigger (opp-facing OR self+Token/Counter payoff) | Mode:Hand Discard + Draw | Nekusar, Locust God |
| monarch_synergy | BecomeMonarch effect | BecomeMonarch + CantAttackUnless pillowfort | Queen Marchesa |
| counter_target_payoff | scales_with XP + PutCounter P1P1 on Creature.Other | trigger CounterAdded / scales_with CardCounters.P1P1 | Ezuri |
| creature_untap_engine | cost=tap (plain) + effect=Mana | Untap Creature / unfiltered Untap | Selvala |
| populate_stack | CopyPermanent with Populate:True in raw | Other Populate CopyPermanent cards | Ghired |
| artifact_recursion | GY→BF Artifact effect | self-sac artifacts, ETB artifacts | Osgir/Daretti |
| copy_synergy | CopyPermanent effect | populate targets, ETB creatures | Ghired/Riku |
| token_sac_chain | Sacrificed trigger | Treasure/Food/Clue/Blood producers | Korvold |
| panharmonicon (reverse) | cmdr subtype match | candidates doubling cmdr triggers | Kykar+Harmonic Prodigy |
| panharmonicon (stack) | cmdr Panharmonicon | other Panharmonicon statics | Yarok+Panharmonicon |
| evasion | DamageDone combat (non-tribal) | self-unblockable creatures | Saskia/Derevi |
| cheat_cmc | ChangeZone hand→BF with ChangeType | CMC-bucketed type matches (6+, 4-5) | Kaalia |
| cost_reduction_target | ReduceCost static | high-CMC creatures (6+) | Rakdos/Animar |
| pinger | scales_with LifeOppsLost | DealDamage/LoseLife targeting opponents | Rakdos |
| toughness_synergy | scales_with CardToughness | Defender creatures | Phenax |
| cascade_value | Cascade keyword | high-CMC spells with valuable effects | Maelstrom Wanderer |
| subject_zone_feeder | ChangesZone BF→GY trigger w/ non-Creature subject (Land, Artifact, Enchantment) | effect=Sacrifice SacValid=<subject> + effect=ChangeZoneAll mass return from Graveyard. Scope-filtered (reject Defined=Opponent). | Titania (Land); any future subject-specific death-trigger commander |
| counter_axis_feeder | any cmdr port valid_filter contains `counters_GE_<TYPE>` on non-Self scope | counter_axis_payoff > counter_producer (self-sac-only cards dropped) > etb_counter_keyword > self_recur_keyword (P1P1 only) | Marchesa (trigger), Hamza (scales_with); any future counters_GE commander |
| creatures_as_lands_landfall | static with Affected=Creature and AddType=Land | LandPlayed triggers + ChangesZone Land ETB triggers (landfall payoffs) | Ashaya, Soul of the Wild |

### IDF Weighting (`universal_scorer.py`)

Each complement's value = `1 / log2(1 + N)` where N = number of candidates
sharing the same match tuple. Specific matches (Saproling lord: N=3, IDF≈0.50)
worth more than broad matches (sacrifice cost: N=2000, IDF≈0.09).

Density rules use flat weights to prevent IDF from penalizing broad-but-correct
matching: spell_density 0.3, scaling 0.3, tribal_density 0.5, etb_self 0.01,
token_producer 0.25, evasion 0.15, pan_density 0.10.

Multi-rule bonus: cards matching 3+ distinct rule_ids get +0.1 per extra rule
(rewards multi-axis synergy picks like Pitiless Plunderer).

### Algorithm

1. Load commander ports (cached)
2. For each complement rule, find matching candidate ports (2 SQL queries)
3. For card-attribute rules, match against cards table
4. Compute IDF weights from candidate frequency
5. Score each candidate = sum of IDF-weighted synergy - anti-synergy + staple bonus
6. Sort by (-score, cmc, edhrec_rank, name)

### Key Files

- `complement_rules.py` — rules registry, `find_all_complements()`
- `universal_scorer.py` — IDF computation, `score_all_universal()`
- `engine.py` — `SynergyEngine.page()`, public API
- `graph_engine.py` — `EVENT_MATCH_MAP`, `COST_FEEDS_TRIGGER`, port matching primitives

## Conventions

- Cards keyed by Scryfall `oracle_id`.
- SQL fragment interpolation guarded by `_VALID_*_EXPRS` frozensets +
  `ValueError` (never `assert` — stripped by `python -O`).
