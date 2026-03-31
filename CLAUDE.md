# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses a LightGBM LambdaRank model trained on EDHREC labels. Production model uses edhrec_deck_pct as a feature (top importance); set EDHREC_FREE=1 for pure Forge-native inference (day-1 new card evaluation, ~7% lower NDCG).

### Signal Architecture (recommendation pipeline)

```
FORGE MODEL (--recommend): Pure Forge mechanical synergy
  1. Color-identity filter → all legal cards scored directly by GBM
  2. Forge LambdaRank GBM: 105 features, EDHREC labels, forge-native features
     29 profile fields per card extracted from forge_abilities (verbs, triggers, keywords,
     counter_types, targets, ability_types, trigger_filters, required_subtypes,
     granted_keywords, conditions, duration, effect_zones, scales_with, grants_types,
     combat_damage, is_secondary, gain_control, damage_amount, cards_drawn, life_amount,
     produces_mana, mana_colors, counter_num_variable, grants_abilities, token_amount_variable,
     has_static_anthem, counters_on_lands, counter_trigger_themes, has_p1p1)
     + forge_deck_tags (Forge's deck-building AI: has/hints/needs theme signals)
     + ability counts (total, triggered), token complexity (P/T, keywords),
       zone interaction (graveyard, exile), ability density
     + deck tag expansion (cmdr_needs_to_card_has, card_needs_satisfied, needs_rarity)
     + counter/anthem distinction (put_counter_ratio, cmdr_counter_x_put_counter,
       static_anthem_counter_cmdr, counters_on_lands, cmdr_p1p1_card_no_counters)
     + functional fingerprints (33-dim semantic vectors):
       produces (12): tokens, p1p1_counters, mana, cards, damage, life, etc.
       requires (11): creature_etb, death, spell_cast, combat, lifegain, etc.
       amplifies (4): counter/token/damage/lifegain doublers
       targets (6): creatures, self, lands, players, artifacts, any
       4 dot-product features: produces·amplifies, requires·produces, full cosine
     Top features: edhrec_deck_pct 10%, card_hub_score 7%, deck_edge_count 6%,
     ability_density 5%, cmdr_2hop_ratio 4.5%, strategy_cosine 5%, cmdr_2hop_count 3.1%
  3. Forge mechanics vectors: 116-dim shared concept space encoding ALL mechanical
     interactions (36 game concepts + 80 subtypes). Captures synergy through
     card produces → commander consumes dot product.
     Zone-aware concepts: enters_from_graveyard/exile/hand, goes_to_graveyard/exile
     Theme concepts: equipment_enters, equipment_equipped, defender_available, etb_doubled
  4. Can evaluate new cards day-1 without playtesting data
  5. Works for any of 3,141+ commanders (not just 1,361 with EDHREC)
  6. NDCG@30 = 0.53 on leave-commander-out CV (3:1 negatives, sample-weighted, early_stop=40)
     Training labels: edhrec_card_synergy (703k rows, continuous synergy grading)
     Grade 5=synergy≥0.40, 4=synergy≥0.20, 3=synergy≥0.05, 2=synergy 0-0.05, 1=negative, 0=random neg
     EDHREC synergy = deck_inclusion% - color_baseline% (commander-specific affinity)
     Training: 3:1 negative ratio (2.1M negatives), 3-tier sampling:
       1/3 strategy/subtype overlap, 1/3 tag overlap, 1/3 random
     Per-grade sample weights: grade 5→3x, grade 4→2x
     2,724 commanders with EDHREC data (87% of legal commanders)
     (variance ±0.3 between runs due to GBM non-determinism)

CAUSAL GRAPH:
  - 20.6M edges across 30+ event types (verb_event_map extracted from Forge Java source)
  - SubAbility chains followed: 72k abilities (12.7k from secondary effects)
  - IDF weighting, chain scoring, anti-synergy detection
  - Synthetic edges (6.3M): SpellCast, Attacks, LandPlayed,
    ChangesZone+Battlefield (creatures, artifacts, enchantments, planeswalkers entering)
  - Entity presence edges (2.6M): token creators → "for each [Type]" scaling,
    sacrifice outlets, tap abilities. Connects Krenko → Brightstone Ritual.
  - Continuous pump edges (54k): subtype-specific lord/anthem effects → matching creatures.
    E.g., Kyler → all Humans (ContinuousPump event). Enables feedback loop detection.
  - Exact (subtype match, 0.3) and broad (card_type match, 0.15) precision levels
  - Materialized filter_precision column for fast queries
```

### Current Performance

Forge model finds mechanical synergies from Forge data alone (no EDHREC at inference).
Evaluation: compare our top 30 recommendations against EDHREC's "High Synergy Cards" section.

Use `scripts/compare_edhrec.py --commander "Name"` or `--all` to evaluate.

## Common Commands

```bash
# === DATA PIPELINE ===
python3 scripts/download_cards.py                  # Refresh Scryfall data (~150MB)
python3 scripts/import_forge.py --download --import  # Update Forge ability data
python3 scripts/build_graph.py --rebuild   # Build causal interaction graph from Forge (~17M edges)
python3 scripts/build_graph.py --stats             # Graph stats
python3 scripts/strategy_detector.py --populate    # Assign strategies
python3 scripts/fetch_spellbook.py                 # Fetch 82k combos

# === EDHREC data ===
python3 scripts/fetch_edhrec_all.py                    # Fetch next 500 new commanders (synergy + avg decks)
python3 scripts/fetch_edhrec_all.py --max 2000         # Fetch up to 2000 new commanders
python3 scripts/fetch_edhrec_all.py --refresh-top 200  # Re-fetch top 200 popular commanders (stale data)
python3 scripts/fetch_edhrec_all.py --refresh          # Re-fetch ALL existing commanders
python3 scripts/fetch_edhrec_all.py --stats            # Show coverage stats

# === Forge model (LightGBM LambdaRank) ===
python3 scripts/train_fusion_model.py    # Train forge GBM (cached features, parallel folds, ~3 min)
python3 scripts/train_fusion_model.py --rebuild-features  # Rebuild features (shared ctx, 8 workers) + train (~7 min)
python3 scripts/train_fusion_model.py --quick             # Single-fold fast iteration (~2 min)
python3 scripts/train_fusion_model.py --tune              # Parallel HP search + train (~12 min)

# === Recommendations ===
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend     # Recommend cards (GBM + mechanical bonus)
uv run mtg-synergy --commander "Krenko, Mob Boss" --recommend --top 10
uv run mtg-synergy --commander "Krenko, Mob Boss" --gems         # Hidden gems (pure mechanical, no popularity)
uv run mtg-synergy --commander "Krenko, Mob Boss" --combos       # Combo detection

# === Comparison & validation ===
python3 scripts/compare_edhrec.py --commander "Krenko, Mob Boss"  # Single commander vs EDHREC
python3 scripts/compare_edhrec.py --all --quiet                    # All commanders summary
python3 scripts/validate_recommendations.py --top 100              # Pipeline validation (model + scoring)
python3 scripts/train_fusion_model.py --validate      # Train + validate in one step

# Tests
uv run pytest tests/ -v                        # Run all 154 tests
uv run pytest tests/test_recommendation_quality.py -v              # Pipeline quality tests only
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → scripts/download_cards.py → data/oracle_cards.json (36k cards)
                                                ↓
                    scripts/import_forge.py → forge_abilities + forge_name_map tables
                                                ↓
                    scripts/build_graph.py --rebuild → interaction_edges table (17.1M causal edges, 30 event types)
                                                ↓
                    scripts/strategy_detector.py → card_strategies table
                                                ↓
                    scripts/fetch_spellbook.py → spellbook_combos table (82k combos)
                                                ↓
                    scripts/fetch_edhrec_all.py → edhrec_card_synergy table (733k pairs, 2,761 cmdrs)
                                                ↓
                    scripts/train_fusion_model.py → data/fusion_model_forge.lgb
                                                ↓
                              uv run mtg-synergy --commander "Name" <name>
                              (color filter → forge GBM → causal graph)
```

### New-set update workflow

```bash
python3 scripts/download_cards.py                               # 1. Refresh Scryfall
python3 scripts/import_forge.py --download --import             # 2. Update Forge data
python3 scripts/build_graph.py --rebuild                # 3. Rebuild causal graph
python3 scripts/strategy_detector.py --populate                 # 4. Strategies
python3 scripts/fetch_spellbook.py                              # 5. Refresh combos
python3 scripts/fetch_edhrec_all.py --max 2000 --refresh-top 200  # 6. Refresh EDHREC (new + top 200 stale)
python3 scripts/train_fusion_model.py --rebuild-features --validate  # 7. Retrain + validate (~8 min, $0)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Card metadata from Scryfall |
| card_strategies | ~88k | Strategy assignments |
| spellbook_combos | ~82k | Commander Spellbook combos |
| spellbook_combo_cards | ~289k | Combo ↔ card junction |
| interaction_edges | ~20.6M | Causal edges from Forge: 30+ event types + 6.3M synthetic + 2.6M entity-presence + 54k continuous pump + 896k theme synergy edges |
| edhrec_card_synergy | ~733k | EDHREC synergy scores for 2,761 commanders (87% coverage) |
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions (12.7k expanded rows). 20 columns: 19 consumed in features or during import, 1 unused (unless_cost). sub_ability column is resolved during import by expanding chains into separate rows. |
| forge_deck_tags | ~14k | Forge deck-building AI: has (what card provides), hints (what card wants), needs (what card requires). 9,868 unique cards. |
| forge_name_map | ~31k | Forge card name → oracle_id mapping (prefers non-token versions) |

### Forge Model

**Forge model** (data/fusion_model_forge.lgb):
- LambdaRank GBM on 105 features (shared via `src/mtg_synergy/recommend/forge_features.py`):
  100% Forge-native with 29 profile fields per card:
  causal scores (6), strategy (2), forge_ability_cosine, phase (2), tribal,
  card types (6), cmc, deck edges (3, log-scaled), causal_composite, card_hub_score (log-scaled),
  forge_type_synergy, cmdr_forge_type_match, shared_forge_mechanics,
  forge_ability_depth, forge_anti_tribal, forge_verb_alignment,
  forge_mech_fwd/rev, counter_type_match, ability_type_ratio_T/A,
  zone_alignment, target_alignment, forge_keyword_synergy,
  activated_ability_count, granted_keyword_synergy, shared_conditions,
  is_permanent_effect, is_temporary_effect, duration_match,
  combat_damage_flag, effect_zone_match, scales_with_board,
  grants_types_match, is_secondary_trigger, gain_control,
  granted_keyword_count, condition_count,
  deck_hints_to_has, deck_has_to_hints, deck_needs_to_has,
  deck_has_overlap, deck_hints_overlap (Forge deck-building AI tags),
  damage_scales, draw_scales, life_scales,
  produces_mana, counter_num_variable, grants_abilities, token_amount_variable,
  total_ability_count, triggered_ability_count, token_power_toughness,
  token_keyword_count, zone_graveyard_interact, zone_exile_interact,
  ability_density, cmdr_needs_to_card_has, card_needs_satisfied, needs_rarity,
  temp_buff_counter_cmdr, put_counter_ratio, cmdr_counter_x_put_counter,
  static_anthem_counter_cmdr, counters_on_lands, cmdr_p1p1_card_no_counters,
  func_produces_amplifies, func_requires_produces, func_card_requires_cmdr, func_full_cosine
     + 2-hop graph (2): cmdr_2hop_count (3.1%), cmdr_2hop_ratio (4.5%)
     + card quality (5): edhrec_deck_pct (10%), forge_ability_richness, card_in_forge,
       card_strategy_count, deck_tag_count
     + theme features (15): equipment (cmdr/card/match), enchantress (cmdr/card/match),
       defender (cmdr/card/match), ETB doubler (card/cmdr_density/match),
       tribal depth (lord/member/combined)
- Functional fingerprints (`ForgeFeatureContext._func_fingerprints`): 33-dim semantic
  vectors per card encoding produces/requires/amplifies/targets. Dot products between
  commander and card fingerprints capture synergy without hand-coded rules.
- Training data: commander-illegal cards filtered from negative pool
- Forge profiles extract ALL raw_line fields: granted_keywords, conditions,
  duration, effect_zones, scales_with, grants_types, combat_damage, is_secondary,
  gain_control, damage_amount, cards_drawn, life_amount, required_subtypes,
  produces_mana, mana_colors, counter_num_variable, grants_abilities,
  token_amount_variable, has_static_anthem, counters_on_lands,
  counter_trigger_themes, has_p1p1
  (from cost, defined, ValidCards$, Affected$, Produced$, CounterNum$,
  AddAbility$, TokenAmount$, Event$, ReplaceWith$ fields)
- forge_deck_tags: Forge's deck-building AI (has/hints/needs) for 9,868 cards
  Maps what a card provides, wants, and requires in a deck
- Mechanics vectors (`src/mtg_synergy/recommend/mechanics_vectors.py`): 116-dim shared
  concept space (36 game concepts + 80 subtypes). Effects and triggers map to same
  dimensions. Dot product = mechanical synergy score.
  Theme concepts: equipment_enters, equipment_equipped, defender_available, etb_doubled
- Training data: edhrec_card_synergy (703k rows, 2,724 commanders), continuous synergy grading
  Grade 5=synergy>=0.40, 4=synergy>=0.20, 3=synergy>=0.05, 2=synergy 0-0.05, 1=negative, 0=random neg
  EDHREC synergy = deck_inclusion% - color_baseline% (commander-specific affinity)
  Generic staples (>30% frequency) demoted from grade 4/5 to 3
  Commander-illegal cards filtered from negative pool
  3:1 negative ratio, 3-tier sampling: 1/3 strategy/subtype, 1/3 tag overlap, 1/3 random
- Training: `python3 scripts/train_fusion_model.py --rebuild-features` (~7 min)
  Feature build: shared ForgeFeatureContext via fork pool (8 workers, ~17s)
  CV folds: 3 folds trained in parallel via ProcessPoolExecutor (thread-pinned)
  Batch features: vectorized array indexing for ~50 features, loop for ~35
  Vectorized NDCG@30, CV splits, weight arrays, group arrays
  Use `--tune` for parallel HP search (~12 min). Default uses cached best HP.
- Feature importance: edhrec_deck_pct 9%, deck_edge_count 6%, strategy_cosine 5%,
  card_hub_score 5%, cmdr_2hop_ratio 4.5%, ability_density 4%, cmdr_2hop_count 3.3%
- EDHREC_FREE=1 env var disables edhrec_deck_pct for pure Forge-native inference
- Edge index: two-layer cache (npz raw edges + adjacency dict cache)
  First run: npz ~0.1s + adj build ~11s. Subsequent: adj cache ~1.5s (train) / ~0.3s (inference)
  Auto-invalidates on edge count, card count, or strength mode change
- Edge index pre-loaded at inference: CmdrFeatureContext uses in-memory adjacency (~3s total)
- Per-grade sample weights: grade 5 x3, grade 4 x2
- Post-scoring penalties (scoring.py `_apply_penalties()`):
  - required_subtype mismatch (×0.4): card requires creature types the commander doesn't have
  - excluded_subtypes (×0.3): card excludes commander's creature type from effects
  - wrong token type (×0.5): tribal commander, card creates wrong token type
  - wrong counter type (×0.4): card puts non-P1P1 counters (M1M1, TIME) for P1P1 commander
  - non-counter creatures for counter commanders (×0.6): no has_p1p1 + no P1P1 counter verbs
  - niche counter penalty (×0.4): TIME/EXPERIENCE/ENERGY-only cards when commander doesn't use them
  - counters on lands for counter commanders (×0.4): Earthbend, land-targeting PutCounter
  - "Choose a Background" / "Doctor's companion" hard filter (score=-1e9)
  - wrong-color needs hard filter (score=-1e9): e.g., Pearl Medallion in mono-G
  - unmet Type$ needs/hints (×0.3): e.g., needs=Type$Dinosaur in Human deck
- DFC-aware subtype extraction: `config.extract_subtypes()` parses both faces
- Pipeline validation: `--validate` flag or `test_recommendation_quality.py` (7 tests)
- GBM: LambdaRank, num_leaves=767, lr=0.025, n_estimators=1000 (early_stop=40),
    label_gain=[0,1,3,6,15,30], bagging_freq=5, colsample_bytree=0.6, feature_fraction_bynode=0.9

### Recommendation Pipeline (scripts/synergy_graph.py --commander "Name" --recommend)

```
1. Candidate selection: Color-identity filter → ALL legal cards
2. Score all candidates with GBM (batch predict, ~0.5s for 13k cards):
   105 Forge-native features (LambdaRank)
3. Post-scoring: anti-synergy penalties + mechanical synergy bonus (±15%)
   Bonus: produces↔consumes dot product, verb→trigger alignment,
   creature ETB / sacrifice outlet / spellcast pattern matches
4. Sort and output top N with clickable Scryfall hyperlinks (OSC 8)
Total time: ~3s (with adjacency cache) / ~7s (first run, builds cache)
```

### Hidden Gem Engine (scripts/synergy_graph.py --commander "Name" --gems)

Pure mechanical reasoning — no GBM model, no popularity bias.
Scores cards by: mechanics vector dot products (produces↔consumes),
causal graph edge strength (direct + 2-hop), verb→trigger alignment,
ETB/sacrifice/spellcast pattern matching, functional fingerprint cosine.
Filters OUT cards appearing in >5% of EDHREC decks → surfaces
mechanically-synergistic cards that nobody plays.

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Circular trigger chain |
| Synergy | `synergy` | Interaction without confirmed loop |

## Key Files

### `src/mtg_synergy/parse/` package (Forge data import)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/parse/forge_import.py` | Import Forge ability data into DB |
| `src/mtg_synergy/parse/forge_filter_parser.py` | Parse Forge ValidCard$ filter expressions |
| `src/mtg_synergy/parse/forge_types.py` | ForgeFilter dataclass for parsed filters |

### `src/mtg_synergy/causal/` package (interaction graph)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/causal/__init__.py` | DB storage, CausalContext (pre-loaded scoring), anti-synergy detection |
| `src/mtg_synergy/causal/types.py` | Edge, EdgeDetail dataclasses |
| `src/mtg_synergy/causal/forge_indexer.py` | Index cards by events from Forge data |
| `src/mtg_synergy/causal/forge_graph_builder.py` | Build causal edges from Forge data |
| `src/mtg_synergy/causal/idf.py` | Shared IDF computation + PRECISION_STRENGTH constant |
| `src/mtg_synergy/causal/verb_event_map.py` | Forge verb → trigger event mapping (extracted from Java source) |

### `src/mtg_synergy/` package (core logic)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/config.py` | Centralized paths, thresholds, DB settings, `extract_subtypes()` (DFC-aware) |
| `src/mtg_synergy/constants.py` | STAPLE_ROLES |
| `src/mtg_synergy/db.py` | Centralized DB connection factory |
| `src/mtg_synergy/tag_db.py` | SQLite tag DB utilities (schema, queries, import) |
| `src/mtg_synergy/cli.py` | CLI dispatcher (argparse + command routing) |
| `src/mtg_synergy/recommend/engine.py` | `recommend_cards()` — forge model recommendation pipeline |
| `src/mtg_synergy/recommend/scoring.py` | `color_identity_filter()`, `score_forge_candidates()`, `_apply_penalties()`, `_apply_mechanical_bonus()` |
| `src/mtg_synergy/recommend/hidden_gems.py` | `find_hidden_gems()` — pure mechanical synergy engine, no popularity bias |
| `src/mtg_synergy/recommend/forge_features.py` | Shared 105-feature computation: `ForgeFeatureContext` (7 `_load_*` methods), `CmdrFeatureContext`, `compute_card_features()` (6 grouped helpers), `compute_batch_features()` |
| `src/mtg_synergy/recommend/mechanics_vectors.py` | 116-dim forge mechanics vectors: shared game concept space (36 concepts + 80 subtypes) |
| `src/mtg_synergy/recommend/cmdr_patterns.py` | `detect_cmdr_patterns()` — shared commander mechanical flag detection |
| `src/mtg_synergy/recommend/affinity.py` | Commander affinity scoring |
| `src/mtg_synergy/combos/detector.py` | `find_combos_tiered()`, `find_partial_combos()`, `compute_strategy_relevance()` |
| `src/mtg_synergy/combos/anti_synergy.py` | Anti-synergy detection |
| `src/mtg_synergy/combos/display.py` | `show_combos_tiered()` — combo output formatting |
| `src/mtg_synergy/analysis/strategy.py` | `_detect_deck_types()` — tribal type detection |

### `scripts/` directory (pipelines + entry points)

| File | Purpose |
|---|---|
| `scripts/synergy_graph.py` | Thin wrapper — re-exports from `mtg_synergy/`, CLI entry point |
| `scripts/train_fusion_model.py` | Forge LambdaRank GBM training + feature cache rebuild + `--validate` |
| `scripts/compare_edhrec.py` | Compare recommendations vs EDHREC High Synergy section |
| `scripts/validate_recommendations.py` | End-to-end pipeline validation (model + scoring + penalties) |
| `scripts/strategy_detector.py` | Rule-based strategy detection |
| `scripts/fetch_spellbook.py` | Commander Spellbook API fetcher |
| `scripts/build_graph.py` | Causal interaction graph builder CLI (--rebuild, --stats) |
| `scripts/import_forge.py` | Forge ability data importer |
| `scripts/fetch_edhrec_all.py` | Fetch EDHREC synergy + avg decks (concurrent, refresh support) |
| `scripts/download_cards.py` | Scryfall bulk data downloader |

## Key Conventions

- Cards keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `scripts/download_cards.py` first
- API calls use `urllib.request` (stdlib only, no external HTTP dependency)
- Dependencies: only `numpy` + `lightgbm` (pyproject.toml)
- All scripts use `from mtg_synergy.config import DB_PATH, DATA_DIR` for paths
- Tribal subtypes extracted via `config.extract_subtypes()` (DFC-aware, both faces)
- CLI uses `--commander "Name"` or `uv run mtg-synergy --commander "Name"`
- Package uses `src/` layout (`src/mtg_synergy/`), built with `uv_build` backend
- Pipeline scripts live in `scripts/`, library modules in `src/mtg_synergy/`
- Tests: 154 tests in `tests/`, run with `uv run pytest tests/`
  - 7 end-to-end pipeline quality tests (`test_recommendation_quality.py`)
  - Requires trained model + populated DB (auto-skipped if missing)
- After training, always run `--validate` to check full pipeline (not just NDCG):
  `python3 scripts/train_fusion_model.py --validate`
- Adjacency cache uses np.savez (not legacy serialization) for security
- Shared helpers: `causal/idf.py` (IDF + precision strength), `recommend/cmdr_patterns.py` (commander mechanical flags)
- Scoring penalties in `_apply_penalties()` apply to ALL commanders (not just tribal)
- Spellbook combo boosts must check color identity
