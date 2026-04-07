# Feature Reference — 93 GBM Features

Maps every model feature to its Forge DSL source, what question it answers, and an example.

> **STALE NOTICE (2026-04-06):** This document has not been refreshed since the
> 2026-03-27 feature expansion and still references removed features like
> `strategy_cosine` (F3 was replaced by `mech_cosine` after strategy elimination).
> It also does not cover the 6 new F25–F31 features added in commit `ce8ca96`
> (shared_verb_count, shared_trigger_count, cmdr_verb_concentration,
> mech_fwd_synergy, mech_rev_synergy, co_producer_score) nor the Phase 1 extraction
> improvements in commits `08ec3ee`, `de95a40`, `8fb10f3`, `854ce00`:
>
> - `trigger_specificity` (F95) now receives `ValidAttacker$` / `ValidBlocker$`
>   filter strings from combat triggers (Attacks/Blocks/AttackerBlockedByCreature).
>   ~78 previously-invisible card profiles now contribute structured combat-filter
>   signal via raw_trigger_filters + IDF.
> - `cost_feeds_cmdr` (F94) now recognizes 9 cost categories (was 5):
>   +`subcounter` (~623 cards), +`exilegrave` (~172, additive with `exile`),
>   +`taptype` (~185), +`return` (~51). All feed through the existing cost_types
>   set — no new feature columns.
>
> A full refresh of this document is tracked separately. Until then, treat the
> per-feature table below as historical. See `packages/mtg-synergy/src/mtg_synergy/recommend/forge_compute.py`
> and `FORGE_FEATURE_NAMES` in `scripts/train_fusion_model.py` for the current
> authoritative feature list.

## Quick Summary

| Category | Features | Count | Data Source |
|----------|----------|-------|-------------|
| Causal Graph | F0–F2, F10–F12 | 6 | `interaction_edges` (21.7M edges) |
| Strategy & Ability | F3–F4 | 2 | `card_strategies` + forge ability vectors |
| Timing & Phase | F5–F6 | 2 | `forge_abilities.trigger_phase` |
| Tribal/CMC | F7–F8 | 2 | `cards.type_line`, `cards.cmc` |
| Forge Profile | F13–F24, F28, F32–F34 | 16 | `forge_abilities` (verb, trigger, keyword, etc.) |
| Zeroed (redundant) | F25–F27, F29–F31 | 6 | — |
| Deck Tags | F35–F39, F52–F54 | 8 | `forge_deck_tags` (has/hints/needs) |
| Scaling/Boolean | F40–F45 | 6 | Variable amounts, mana, tokens |
| Ability Complexity | F46–F51 | 6 | Counts, density, zone interaction |
| Counter Interaction | F55–F57 | 3 | `forge_abilities.counter_type` |
| Functional Fingerprints | F58–F61 | 4 | 33-dim semantic vectors |
| 2-Hop Graph | F62–F63 | 2 | Commander → intermediary → card paths |
| Card Quality | F64–F67 | 4 | Richness, strategies, EDHREC% |
| Tribal Depth | F68–F70 | 3 | Lord/member/token tribal stacking |
| General Demand | F71–F72 | 2 | Verb/type demand matching |
| Mechanics Vectors | F73–F88 | 16 | 8 categories × fwd/rev dot products |
| New Field Features | F89–F92 | 4 | Affected$ scope, pump, type changes |

---

## Causal Graph Features (F0–F2, F10–F12)

| F# | Name | Forge fields | Question it answers | Example |
|---|---|---|---|---|
| F0 | `causal_cmdr_to_card` | `interaction_edges` (cmdr→card strength) | How strongly does the commander produce events the card responds to? | Krenko creates tokens → Skirk Prospector responds to tokens |
| F1 | `causal_card_to_cmdr` | `interaction_edges` (card→cmdr strength) | How strongly does the card produce events the commander responds to? | Phyrexian Altar produces mana → Krenko uses mana |
| F2 | `deck_edge_count` | `interaction_edges` (deck→card count) | How many deck cards synergize with this candidate? | Goblin Instigator connects to 5 other goblins in deck |
| F10 | `causal_composite` | F0+F1 × event diversity × exact edge | Overall causal strength boosted by variety? | Multi-event synergy (token + attack + sacrifice) scores high |
| F11 | `card_hub_score` | Edge index out-degree + in-degree | How connected is this card in the causal graph? | Sol Ring (many connections) vs niche creature |
| F12 | `deck_exact_count` | `filter_precision = 'exact'` edges | How many exact-match (subtype-level) edges exist? | Goblin-specific edges in Goblin deck |

## Strategy & Ability (F3–F4)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F3 | `strategy_cosine` | `card_strategies` vectors | Do commander and card share strategies (tokens, aggro, graveyard)? | Krenko (tokens) + Goblin Bombardment (sacrifice+tokens) |
| F4 | `forge_ability_cosine` | Profile verb/trigger/keyword union | Do they share raw ability types? | Both have Token verb + ChangesZone trigger |

## Timing (F5–F6)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F5 | `phase_match` | `trigger_phase` (Phase$) | Are triggers timed to fire together? | Both trigger at end step |
| F6 | `has_phase_trigger` | `card_phase_order` existence | Does the card have any phase-based trigger? | "At beginning of upkeep" cards |

## Tribal & CMC (F7–F8)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F7 | `tribal_match` | `cards.type_line` subtypes | Is the card a member of the commander's creature type? | Goblin card for Krenko |
| F8 | `cmc` | `cards.cmc` | What does the card cost? (mana curve signal) | Lightning Bolt=1, Omniscience=10 |

## Deck Edge Precision (F9)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F9 | `deck_exact_edge_ratio` | `filter_precision` exact/broad | What fraction of synergies are precise (subtype match) vs broad? | Tribal card: 0.8, generic draw: 0.2 |

## Forge Profile Features (F13–F24)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F13 | `forge_type_synergy` | `trigger_filter` + cmdr subtypes | Does the card trigger on the commander's creature type? | "Whenever a Goblin enters" for Krenko |
| F14 | `cmdr_forge_type_match` | Card subtypes + cmdr `trigger_filter` | Is the card a type the commander triggers on? | Goblin creature in Krenko deck |
| F15 | `forge_ability_depth` | Profile union size | How mechanically complex is the card? | Planeswalker (8 abilities) vs vanilla creature (1) |
| F16 | `forge_anti_tribal` | `required_subtypes`, `excluded_subtypes` | Does the card anti-synergize with the commander's type? | "Nonhuman creature" ability in Human deck |
| F17 | `forge_verb_alignment` | `verb` → `trigger_mode` mapping | Do the card's verbs match the commander's triggers? | Token verb + ChangesZone trigger = alignment |
| F18 | `counter_type_match` | `counter_type` overlap | Do they share counter types (P1P1, ENERGY, etc.)? | Both use +1/+1 counters |
| F19 | `ability_type_ratio_T` | `ability_type = 'T'` | Does the card have triggered abilities? | ETB trigger creature |
| F20 | `ability_type_ratio_A` | `ability_type = 'A'` | Does the card have activated abilities? | Tap ability creature |
| F21 | `zone_alignment` | Card zones ∩ cmdr zones | Do they operate in the same zones? | Both care about graveyard |
| F22 | `target_alignment` | Card `targets` vs cmdr produces | Does the card target what the commander makes? | Card targets creatures + cmdr makes creatures |
| F23 | `forge_keyword_synergy` | `keyword` vs cmdr `trigger_filter` | Do card keywords match cmdr triggers? +combat bonus | Card has Flying + cmdr cares about flying |
| F24 | `activated_ability_count` | Profile activated count | How many activated abilities? | Planeswalker: 3, mana dork: 1 |

## Zeroed Features (F25–F31)

These are kept as columns (for index stability) but always output 0.0:

| F# | Name | Was | Replaced by |
|---|---|---|---|
| F25 | `is_permanent_effect` | Duration$ permanent flag | F90 pump_magnitude |
| F26 | `is_temporary_effect` | Duration$ temporary flag | F90 pump_magnitude |
| F27 | `duration_match` | Duration$ set overlap | Very sparse, negligible signal |
| F29 | `effect_zone_match` | ActiveZones$/EffectZone$ overlap | F83-F84 mech_zones, F21 zone_alignment |
| F30 | `scales_with_board` | SetPower$/AddPower$ X/Y | F91 pump_is_variable, F40-F42 |
| F31 | `is_secondary_trigger` | Secondary$ True | F47 triggered_ability_count |

## Combat & Control (F28, F32)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F28 | `combat_damage_flag` | `CombatDamage$ True` | Does the card care about combat damage? | Sword of X and Y, combat damage triggers |
| F32 | `gain_control` | `GainControl$ True` | Does the card steal permanents? | Act of Treason, Gilded Drake |

## Keywords & Conditions (F33–F34)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F33 | `granted_keyword_count` | `AddKeyword$` from raw_line | How many keywords does the card grant others? | Akroma's Will: grants 5 keywords |
| F34 | `condition_count` | `IsPresent$`, `ConditionPresent$` | How many conditions gate the card's abilities? | "If you control a creature with power 4+" |

## Deck Tag Features (F35–F39, F52–F54)

Source: `forge_deck_tags` table (Forge's deck-building AI: has/hints/needs)

| F# | Name | Computation | Question | Example |
|---|---|---|---|---|
| F35 | `deck_hints_to_has` | cmdr.hints ∩ card.has | Does the card provide what the commander wants? | Cmdr hints Token, card has Token |
| F36 | `deck_has_to_hints` | cmdr.has ∩ card.hints | Does the card want what the commander provides? | Card hints Mill, cmdr has Mill |
| F37 | `deck_needs_to_has` | cmdr.has ∩ card.needs | Does the card need what the commander has? | Card needs sacrifice outlet, cmdr has one |
| F38 | `deck_has_overlap` | cmdr.has ∩ card.has | Do both provide the same things? | Both provide tokens |
| F39 | `deck_hints_overlap` | cmdr.hints ∩ card.hints | Do both want the same things? | Both want tribal synergy |
| F52 | `cmdr_needs_to_card_has` | cmdr.needs ∩ card.has | Does the card satisfy what the commander needs? | Cmdr needs ramp, card has ramp |
| F53 | `card_needs_satisfied` | card.needs met / total needs | What fraction of card's needs are met? | Card needs mill + graveyard, cmdr has both: 1.0 |
| F54 | `needs_rarity` | 1/provider_count per need | How rare are the card's provided needs? | Niche interaction: 2.5, common: 0.1 |

## Scaling Flags (F40–F42)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F40 | `damage_scales` | `NumDmg$ X/Y` | Does damage scale with a variable? | Fireball (X damage) |
| F41 | `draw_scales` | `NumCards$ X/Y` (with Draw verb) | Does card draw scale? | Blue Sun's Zenith (draw X) |
| F42 | `life_scales` | `LifeAmount$ X/Y` | Does life gain/loss scale? | Exsanguinate (drain X) |

## Boolean Flags (F43–F45)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F43 | `produces_mana` | `Produced$` in raw_line | Does the card produce mana? | Sol Ring, Llanowar Elves |
| F44 | `granted_ability_match` | `AddAbility$`, `AddTrigger$` vs cmdr verbs/triggers | Do granted abilities match the commander's mechanics? | Card grants death trigger + cmdr triggers on death |
| F45 | `token_amount_variable` | `TokenAmount$ X` | Does the card create variable # of tokens? | Krenko (X = goblin count) |

## Ability Complexity (F46–F51)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F46 | `total_ability_count` | All abilities in profile | How many abilities total? | Complex planeswalker: 8+, vanilla: 1 |
| F47 | `triggered_ability_count` | Triggered abilities only | How many triggered abilities? | Death trigger + ETB trigger: 2 |
| F48 | `token_power_toughness` | `TokenScript$` parsed P/T | How big are the tokens created? | Angel token (4/4)=8, Goblin (1/1)=2 |
| F49 | `token_keyword_count` | `TokenScript$` keywords | How many keywords do tokens have? | Flying vigilance token: 2 |
| F50 | `zone_graveyard_interact` | Graveyard zone flag overlap | Do both cmdr and card interact with graveyard? | Reanimator cmdr + flashback card |
| F51 | `ability_density` | total_abilities / max(cmc, 1) | Abilities per mana cost (efficiency)? | 1-mana with 3 abilities: 3.0 |

## Counter Interaction (F55–F57)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F55 | `put_counter_ratio` | PutCounter vs Pump verb count | Does the card add counters vs temporary pump? | Hardened Scales: 1.0, Giant Growth: 0.0 |
| F56 | `cmdr_counter_x_put_counter` | Cmdr P1P1 + card PutCounter | Does a P1P1 commander get counter synergy? | Kyler + Hardened Scales: 1.0 |
| F57 | `cmdr_p1p1_card_no_counters` | Cmdr P1P1 + card lacks P1P1 | Is the creature useless for a counter commander? | Kyler + vanilla 2/2: 1.0 (anti-synergy) |

## Functional Fingerprints (F58–F61)

Source: 33-dim semantic vectors encoding produces/requires/amplifies/targets per card.

| F# | Name | Computation | Question | Example |
|---|---|---|---|---|
| F58 | `func_produces_amplifies` | cmdr.amplifies · card.amplifies | Does the card amplify what the commander amplifies? | Token doubler + token amplifier |
| F59 | `func_requires_produces` | cmdr.requires · card.produces | Does the card produce what the commander requires? | Sacrifice outlet for sacrifice commander |
| F60 | `func_card_requires_cmdr` | card.requires · cmdr.produces | Does the commander produce what the card requires? | Card needs creatures + cmdr makes creatures |
| F61 | `func_full_cosine` | Full vector cosine similarity | Overall semantic mechanical match? | Perfect match: 0.8+, no overlap: 0.0 |

## 2-Hop Graph (F62–F63)

| F# | Name | Source | Question | Example |
|---|---|---|---|---|
| F62 | `cmdr_2hop_count` | Commander → intermediary → card | How many 2-hop paths connect them? | Via 5 deck cards: ~2.6 |
| F63 | `cmdr_2hop_ratio` | 2-hop count / hub score | What fraction of connections are indirect? | Niche synergy only via intermediaries: 0.8 |

## Card Quality (F64–F67)

| F# | Name | Source | Question | Example |
|---|---|---|---|---|
| F64 | `forge_ability_richness` | Profile diversity | How mechanically diverse is the card? | Multi-modal planeswalker: high |
| F65 | `card_strategy_count` | `card_strategies` table | How many strategies does the card support? | Sacrifice outlet: 1, flexible card: 3 |
| F66 | `deck_tag_count` | `forge_deck_tags` count | How many Forge AI signals does the card carry? | Multi-purpose: 5-8, narrow: 1-2 |
| F67 | `edhrec_deck_pct` | `edhrec_card_synergy` inclusion% | What % of EDHREC decks include this card? | Sol Ring: 50%+, hidden gem: 2% |

## Tribal Depth (F68–F70)

| F# | Name | Source | Question | Example |
|---|---|---|---|---|
| F68 | `tribal_lord_for_cmdr` | Card pumps cmdr subtypes | Is the card a lord for the commander's tribe? | Goblin Chieftain for Krenko |
| F69 | `tribal_member_of_cmdr` | Card subtypes match cmdr tribal | Is the card a tribe member the cmdr triggers on? | Goblin creature for Krenko |
| F70 | `tribal_synergy_depth` | F7 + F68 + F69 + token match | Overall tribal stacking score (0-4)? | Goblin lord that IS a goblin + makes goblin tokens: 3-4 |

## General Demand (F71–F72)

| F# | Name | Source | Question | Example |
|---|---|---|---|---|
| F71 | `verb_demand_match` | Card verb supply vs cmdr trigger demand | Does the card do what the commander triggers on? | Cmdr wants SpellCast; card is a spell |
| F72 | `type_demand_match` | Card type supply vs cmdr type demand | Does the card produce types the commander wants? | Cmdr wants creatures; card IS a creature |

## Mechanics Vectors (F73–F88)

8 categories × 2 directions (fwd: cmdr consumes · card produces, rev: cmdr produces · card consumes).

| F# | Name | Category concepts | Example |
|---|---|---|---|
| F73-74 | `mech_board_fwd/rev` | creature_enters, artifact_enters, permanent_enters, token_created, creature_available | Token maker + ETB payoff |
| F75-76 | `mech_resource_fwd/rev` | counter_added, card_drawn, life_gained, life_lost, damage_dealt | Counter source + counter payoff |
| F77-78 | `mech_disruption_fwd/rev` | card_discarded, card_milled, creature_dies, target_chosen, permanent_destroyed, creature_sacrificed | Mill card + graveyard cmdr |
| F79-80 | `mech_tempo_fwd/rev` | spell_cast, creature_attacks, creature_blocks | Spell + "whenever you cast" |
| F81-82 | `mech_utility_fwd/rev` | creature_tapped, creature_untapped, creature_pumped, mana_produced, phase_trigger | Tap/untap synergy |
| F83-84 | `mech_zones_fwd/rev` | enters_from_graveyard/exile/hand, goes_to_graveyard/exile | Reanimate + graveyard fill |
| F85-86 | `mech_themes_fwd/rev` | equipment_enters, equipment_equipped, defender_available, etb_doubled | Equipment cmdr + equipment |
| F87-88 | `mech_tribal_fwd/rev` | 80 creature subtypes (goblin, elf, zombie, etc.) | Tribal producer + tribal consumer |

## New Field Features (F89–F92)

| F# | Name | Forge fields | Question | Example |
|---|---|---|---|---|
| F89 | `affected_scope_ratio` | `Affected$` YouCtrl/OppCtrl/Self | Does the card buff your stuff or hurt opponents? | Pump your creatures: 1.0, opponent debuff: 0.0 |
| F90 | `pump_magnitude` | `NumAtt$`, `AddPower$`, `NumDef$`, `AddToughness$` | How big is the pump effect? | +5/+5: 5, +1/+1 lord: 1, variable: 0 (see F91) |
| F91 | `pump_is_variable` | `NumAtt$/AddPower$ = X/Y/AffectedX` | Does the pump scale with a variable? | Craterhoof-style: 1.0, fixed pump: 0.0 |
| F92 | `type_change_tribal_match` | `ChangeType$` vs cmdr subtypes | Does the card change types to match the commander's tribe? | Arcane Adaptation + tribal commander |
