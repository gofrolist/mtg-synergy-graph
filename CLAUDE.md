# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.251 on the 100-commander golden set.
2026-04-19 audit-driven cleanup deleted 9 net-negative or dead rules
(`etb_sac_target`, `power_matters`, `token_sac_chain`, `pan_density`,
`token_etb_damage` → kept as CONTENTIOUS, `damage_synergy`,
`counter_producer`, `pinger`, `peer_evasion_tribal`, `yard_caster`)
for an aggregate NDCG lift of +0.0049 — see
`scripts/_audit_rule_impact.py` for the per-rule impact methodology
(NDCG@30 metric + golden-set safety net + CONTENTIOUS verdict).
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
modified_axis_feeder rule (2026-04-18):
- New general rule mirroring counter_axis_feeder for the `modified`
  qualifier (a creature with a +1/+1 counter, an Aura attached, or
  Equipment attached). Detects `modified` in any commander port's
  valid_filter or raw_line clauses. Self-anchored conditions
  (Ian the Reckless's `IsPresent: Card.Self+modified`) and clause
  keys carrying the qualifier as a side condition or flavor text
  (`TargetsValid` for Pearl-Ear, `TriggerDescription`,
  `Description`, `SpellDescription`, `StackDescription`,
  `PrecostDesc`) are skipped — they don't make the commander a
  modified-axis archetype.
- Five tiers (deduped, highest-priority wins per card):
  - `modified_p1p1_doubler` — replacement AddCounter with
    ValidCounterType P1P1 and ReplaceWith AddOneMoreCounters
    (Hardened Scales, Doubling Season, Kami of Whispered Hopes).
  - `modified_p1p1_producer` — effect=PutCounter[All] P1P1 on
    Creature scope, excluding self-sac-only producers.
  - `modified_self_grower` — Creature card with PutCounter Self
    P1P1 (Champion of Lambholt, Forgotten Ancient, Managorger
    Hydra). Restricted to creature cards via cards.types JOIN.
  - `modified_proliferate` — any Proliferate effect.
  - `modified_etb_keyword` — etbCounter:P1P1:N keyword + Modular.
- 11 legendary creature commanders use the modified filter; 9 have
  EDHREC data. Kodama of the West Tree 0/10 → 4/10 hi_syn (Hardened
  Scales / Ozolith / Kami of Whispered Hopes / Evolution Sage),
  on_page 0 → 8/30. Chishiro on_page 3 → 8, Red XIII 0 → 6, SP//dr
  hi_syn 0 → 1. Pearl-Ear (Aura tribal) preserved at 5/10 baseline
  via TargetsValid skip; Silver Sable shows minor on_page churn
  (13 → 7) but new top 30 is mechanically more correct (Hardened
  Scales / Doubling Season / etbCounter:P1P1 cards). Multiplier
  3.0× to match counter_axis_feeder. Golden set NDCG unchanged.
  14 new tests; 852 total tests passing, 86% coverage.
Schema-driven gap closures (2026-04-18):
- `damage_doubler_synergy`: replacement.DamageDone with damage-amp
  replacement_result (DmgTwice, DmgTriple, DmgPlus*, Dmg2/3,
  HarshDmg) targeting opponent. Two tiers — damage_amp_stack
  (other replacement-doublers, ~50 cards) > damage_pinger (cards
  with non-combat repeating trigger + DealDamage opponent, ~170).
  Rejects prevention statics (Iroas/Tajic/Emmara/Frodo —
  Prevent: True / PreventionEffect: True), self-target replacements
  (Dralnu/Polukranos/Sekki — ValidTarget: Card.Self / You /
  Permanent.YouCtrl), and damage-decreasing results
  (DmgMinus*, DmgHalf*). Lifts Gisela 0→2 hi_syn, Solphim 0→1,
  Wolverine 0→1, Raphael 0→2, Tor Wauki 0→1; Torbran top-30
  becomes pure doublers (Furnace of Rath, Curse of Bloodletting,
  Mechanized Warfare, Fiery Emancipation, City on Fire).
  Multiplier 2.5×. Closes the cell from 48% → 58% activation.
- `peer_evasion_tribal`: commander has a peer-blocking keyword
  (Horsemanship 29 cards / Shadow 36 cards) → match other cards
  with the SAME keyword. Pools are siloed (horsemanship cmdr
  doesn't pull Shadow, vice versa). General gate: any future
  peer-blocking keyword goes in the `_PEER_BLOCKING_KEYWORDS`
  frozenset. Closes the keyword.Horsemanship cell from 36% →
  100% activation — all 14 P3K legendary horsemanship commanders
  (Cao Ren / Liu Bei / Lu Bu / Lu Meng / Lu Xun / Ma Chao /
  Sun Ce / Xiahou Dun / Yuan Shao / Zhang Fei / Zhang He /
  Zhao Zilong / Lady Zhurong / Guan Yu) now surface their pool.
  Multiplier 2.0×.
- 20 new tests (14 doubler + 6 horsemanship); 872 total tests
  passing, 86% coverage. Golden set NDCG unchanged at 0.246137.
cardpower_axis_feeder (2026-04-19):
- New general rule for the 67 legendary-creature commanders whose
  `SVar:X:Count$CardPower` scales an ability with their own power
  (Combustion Man's damage = power, Krenko TSK's Goblin token count,
  Carmen / Alesha / Ayesha Tanaka's cmcLEX reanimate/Dig cap,
  Inferno of the Star Mounts's charge-up to 20). `Count$CardPower`
  resolves to the commander's own power — different axis from
  `TotalPower` / `greatestPower` which scan the board. The deleted
  `power_matters` rule conflated the two and fed high-power creatures
  to every scales_with Power commander; this rule targets only
  CardPower and feeds **commander-pumping** cards.
- Two deduped tiers (highest priority wins per card):
  - `cardpower_big_attachment`: Equipment/Aura with static Continuous
    `AddPower ≥ 3` OR `AddPower = X/Y/Z` (scaling SVar). ~220 cards —
    Colossus Hammer, Eldrazi Conscription, Grafted Wargear, Kaldra
    Compleat. +1 / +2 trinkets dropped (not meaningful pumps).
  - `cardpower_p1p1_producer`: `effect=PutCounter[All] P1P1` on
    Creature target (not Self); drops self-sac-only distributors via
    `_only_self_sac_cost`. ~400 cards (Rishkar, Drana). Grower
    archetypes (Alesha / Carmen / Krenko TSK / Agatha all put P1P1
    counters on themselves as part of their triggered chain)
    compound with external producers; non-grower CardPower
    commanders still benefit because a P1P1 counter on the commander
    raises the count.
- Disjoint from `voltron` (4 of 67 overlap, gated on
  Hexproof/Exalted/Shroud/Trample) and from `modified_axis_feeder` /
  `counter_axis_feeder` (2 overlap each, require explicit qualifiers).
- Per-rule audit: `positive` verdict — 59 commanders touched,
  ndcgΣ +0.707 with max +0.244 (Raubahn +3 hits, +0.244 NDCG),
  Ayesha Tanaka +0.208, Ian the Reckless +0.139, Combustion Man
  +0.120. One regression: Velomachus Lorehold -0.222 (her
  EDHREC Hi-Syn values high-CMC Instants/Sorceries she cheats via
  Play.cmcLEX, which now yield top-30 slots to Equipment); net
  remains strongly positive. Multiplier 2.5× (one notch below
  counter/modified's 3.0× because the attachment tier partially
  overlaps with the general voltron pool). Golden NDCG unchanged
  at 0.25105. 13 new tests; 972 total.
tap_type_feeder (2026-04-19):
- New rule for the 27 legendary-creature commanders with a
  `cost.tap_type` port (`tapXType<N/SUBJECT>`) — Azami (tap Wizard),
  Urza (Artifact), Aryel (Knight), Kumena (Merfolk), Lathril (Elf),
  Apothecary White (Food), Baylen (Permanent.token), Caparocti
  (Artifact;Creature). Every tap-cost commander wants to fire the
  cost TWICE per rotation, so the universal reward is a sustained
  untap engine.
- Axis-aware via `_classify_tap_type_axis`: extract SUBJECT from
  raw_line, classify as creature / artifact / permanent. Creature-
  taps (Azami) get Seedborn Muse / Prophet of Kruphix / Murkfiend
  Liege but NOT Unwinding Clock. Artifact-taps (Urza) get Unwinding
  Clock + Seedborn Muse (Permanent-subsuming) but NOT Drumbellower.
  Permanent-taps (Baylen) match everything.
- Two tiers (deduped, tier 1 wins):
  - `tap_type_sustained_untap`: static.UntapOtherPlayer whose
    ValidCard matches the axis, non-Self. ~10 per axis.
    Archetype-defining — Seedborn Muse et al.
  - `tap_type_phase_untap`: trigger.Phase + effect.UntapAll on
    axis-matching valid_filter. ~10 per axis. Awakening, White
    Plume Adventurer, Virtue of Loyalty.
- First draft at 3.0x multiplier flooded Aryel/Kumena top-30 with
  untaps and displaced tribal Hi-Syn picks (Aryel -0.167 NDCG,
  Kumena -0.107). Subject-aware filter cut that to -0.039 / +0.004.
  Multiplier lowered to 2.0x (vs counter/modified's 3.0x) because
  the pool is already tight — 20 cards total per axis — so IDF
  ~0.29 per match is premium on its own.
- Per-rule audit verdict: MARGINAL (+0.101 NDCG sum, +1 hit net,
  ratio 0.004 < 0.1 positive threshold). Top lifts: Kirol +0.138,
  Belisarius Cawl +0.080, Shao Jun +0.018. Golden NDCG unchanged
  at 0.25113. 20 new tests (9 axis classifier + 11 rule). 992 total.
hand_size_feeder (2026-04-19):
- New rule for the 24 big-hand commanders with a `scales_with
  ValidHand Card.YouOwn` port whose mechanic rewards LARGE hands
  (Alandra Drakes pump, Damia refill-to-7, Kefnet attack-if-7+,
  Tishana P/T=hand, Soramaro / Kagemaro / Syr Elenora / Alrund /
  Jin-Gitaxias / Kozilek / Doctor Octopus / Duggan / Mr. Foxglove
  / Krang / Leonardo da Vinci).
- The axis is BIDIRECTIONAL — 4 commanders (Hazoret, Neheb,
  Djeru and Hazoret, Flubs the Fool) want EMPTY hands. Feeding
  them SetMaxHandSize: Unlimited staples would be anti-synergy.
  `_is_big_hand_commander` rejects them via small-hand SVarCompare
  signals (`LE0`/`LE1`/`EQ0` fires-on-empty, `GE2`/`GE3` pairs
  with CantAttack/CantBlock) on the hand-binding SVar (extracted
  from `SVar:<X>:Count$ValidHand Card.YouOwn` bindings).
- Single tier: `hand_size_no_max` — static.Continuous with
  `SetMaxHandSize: 'Unlimited'` (~46 cards: Reliquary Tower,
  Thought Vessel, Library of Leng, Spellbook, Venser's Journal,
  Decanter of Endless Water, Folio of Fancies, The Magic Mirror).
  Narrow, archetype-defining — these remove the end-of-turn
  discard cap that would otherwise pin the hand-size axis at 7.
- Per-rule audit verdict: positive. 24 commanders touched, ndcgΣ
  +2.686 (largest per-rule lift ever). Top wins: Soramaro +4 hits
  +0.397 NDCG, Kagemaro +3 +0.349, Syr Elenora +3 +0.335, Kefnet
  +2 +0.272, Alrund +2 +0.270. One regression: Damia -0.194
  (her 79-candidate pool is tiny so 46 new hand-size cards
  displace 7 generic on-page staples — net Hi-Syn unchanged at
  0/10 but on-page drops from 9 → 2). All 4 small-hand
  commanders correctly rejected — zero false positives in the
  gate. Golden set NDCG unchanged at 0.25113.
- 15 new tests — 8 for `_is_big_hand_commander` (no SVar,
  default big-hand, Hazoret GE2, Neheb LE1, Flubs EQ0 via branch,
  Damia LT7 big-hand, Jin-Gitaxias GE7 big-hand, non-hand-SVar
  compare ignored) and 7 for the rule (gate, Reliquary Tower
  surfaces, Hazoret skipped, fixed max rejected, non-static
  Effect rejected, commander self-exclusion, rule_id). 1007
  total tests, 82% coverage.
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
uv run pytest tests/                                                     # 1007 tests, ~1s
uv run python scripts/_audit_rule_impact.py                              # Per-rule impact audit (NDCG + golden safety net), ~10 min
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
| combat_enhancer | DamageDone trigger OR Attacks-Self trigger w/ engine effect (AddPhase/Dig/Play/Mana/Token/DealDamage/Discard/Mill, or 2+ value effects) | AddPhase + Double Strike | Saskia / Etali / Scourge of the Throne |
| wheel_synergy | Drawn trigger (opp-facing OR self+Token/Counter payoff) | Mode:Hand Discard + Draw | Nekusar, Locust God |
| monarch_synergy | BecomeMonarch effect | BecomeMonarch + CantAttackUnless pillowfort | Queen Marchesa |
| counter_target_payoff | scales_with XP + PutCounter P1P1 on Creature.Other | trigger CounterAdded / scales_with CardCounters.P1P1 | Ezuri |
| creature_untap_engine | cost=tap (plain) + effect=Mana | Untap Creature / unfiltered Untap | Selvala |
| populate_stack | CopyPermanent with Populate:True in raw | Other Populate CopyPermanent cards | Ghired |
| artifact_recursion | GY→BF Artifact effect | self-sac artifacts, ETB artifacts | Osgir/Daretti |
| copy_synergy | CopyPermanent effect | populate targets, ETB creatures | Ghired/Riku |
| panharmonicon (reverse) | cmdr subtype match | candidates doubling cmdr triggers | Kykar+Harmonic Prodigy |
| panharmonicon (stack) | cmdr Panharmonicon | other Panharmonicon statics | Yarok+Panharmonicon |
| evasion | DamageDone combat (non-tribal) | self-unblockable creatures | Saskia/Derevi |
| cheat_cmc | ChangeZone hand→BF with ChangeType | CMC-bucketed type matches (6+, 4-5) | Kaalia |
| cost_reduction_target | ReduceCost static | high-CMC creatures (6+) | Rakdos/Animar |
| toughness_synergy | scales_with CardToughness | Defender creatures | Phenax |
| cascade_value | Cascade keyword | high-CMC spells with valuable effects | Maelstrom Wanderer |
| subject_zone_feeder | ChangesZone BF→GY trigger w/ non-Creature subject (Land, Artifact, Enchantment) | effect=Sacrifice SacValid=<subject> + effect=ChangeZoneAll mass return from Graveyard. Scope-filtered (reject Defined=Opponent). | Titania (Land); any future subject-specific death-trigger commander |
| counter_axis_feeder | any cmdr port valid_filter contains `counters_GE_<TYPE>` on non-Self scope | counter_axis_payoff > counter_producer (self-sac-only cards dropped) > etb_counter_keyword > self_recur_keyword (P1P1 only) | Marchesa (trigger), Hamza (scales_with); any future counters_GE commander |
| modified_axis_feeder | any cmdr port carries the `modified` qualifier on a non-Self axis (rejecting `TargetsValid` / description clauses) | modified_p1p1_doubler > modified_p1p1_producer > modified_self_grower (creatures only) > modified_proliferate > modified_etb_keyword (etbCounter:P1P1 + Modular) | Kodama of the West Tree, Red XIII, SP//dr, Sephiroth, Chishiro, Goro-Goro, Silver Sable |
| damage_doubler_synergy | replacement.DamageDone with amp `replacement_result` (DmgTwice/DmgTriple/DmgPlus*) targeting opponent; rejects Prevent / self-target / DmgMinus | damage_amp_stack (other replacement doublers) > damage_pinger (non-combat repeating trigger + DealDamage opponent) | Torbran, Gisela, Solphim, Tor Wauki, Raphael, Wolverine, Neriv, Ojer Axonil, Absorbing Man and Titania |
| creatures_as_lands_landfall | static with Affected=Creature and AddType=Land | LandPlayed triggers + ChangesZone Land ETB triggers (landfall payoffs) | Ashaya, Soul of the Wild |
| cardpower_axis_feeder | `scales_with.CardPower` port (`SVar:X:Count$CardPower`) | cardpower_big_attachment (Equipment/Aura w/ static AddPower ≥ 3 or AddPower=X/Y/Z, ~220) > cardpower_p1p1_producer (PutCounter P1P1 on Creature target, self-sac-only excluded, ~400) | Combustion Man, Krenko (Tin Street Kingpin), Alesha, Carmen, Agatha, Inferno of the Star Mounts, Raubahn, Ian the Reckless |
| tap_type_feeder | `cost.tap_type` port (`tapXType<N/SUBJECT>`). Subject classified as creature / artifact / permanent via `_classify_tap_type_axis` — filters candidates so creature-tappers only see creature/permanent untaps, artifact-tappers only see artifact/permanent untaps. | tap_type_sustained_untap (static.UntapOtherPlayer matching the axis, non-Self, ~10 cards per axis) > tap_type_phase_untap (trigger.Phase + effect.UntapAll matching the axis, ~10 cards) | Azami, Urza (Lord High Artificer), Aryel, Kumena, Lathril, Apothecary White, Baylen, Caparocti |
| hand_size_feeder | `scales_with ValidHand Card.YouOwn` port AND `_is_big_hand_commander(cmdr_ports)` returns True (no `LE0`/`LE1`/`EQ0`/`GE2`/`GE3` SVarCompare on the hand-binding SVar). | hand_size_no_max (static.Continuous with `SetMaxHandSize: 'Unlimited'`, ~46 cards) | Alandra, Damia, Kefnet, Tishana, Soramaro, Kagemaro, Syr Elenora, Alrund, Jin-Gitaxias, Kozilek, Doctor Octopus, Duggan, Mr. Foxglove, Krang, Leonardo da Vinci, and 6 more |

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
