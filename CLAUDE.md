# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.246, Hi-Syn 222/1000 on the 100-commander golden set.
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
| combat_enhancer | DamageDone trigger | AddPhase + Double Strike | Saskia |
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
