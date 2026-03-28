# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses a two-stage fusion model (tower pre-filter + LightGBM) with two modes: a baseline trained on EDHREC data, and a forge-only model that reasons purely from card mechanics (zero EDHREC dependency). The forge model can evaluate new cards day-1 without community playtesting data.

### Signal Architecture (recommendation pipeline)

```
Two modes available:

BASELINE (--recommend): EDHREC-trained, optimized for known commanders
  1. Tower pre-filter: EDHREC tower scores 13k cards, takes top 3000
  2. Fusion GBM: 8 features including edhrec_synergy, edhrec_rank
  3. AUC=0.999 on EDHREC commanders

FORGE-ONLY (--recommend --forge): Zero oracle text, pure Forge mechanical synergy
  1. Color-identity filter → all legal cards scored directly by GBM (no tower pre-filter)
  2. Forge LambdaRank GBM: 63 features, EDHREC labels, forge-native features
     100% Forge-native: no oracle text, no embeddings, no neural network
     25 profile fields per card extracted from forge_abilities (verbs, triggers, keywords,
     counter_types, targets, ability_types, trigger_filters, required_subtypes,
     granted_keywords, conditions, duration, effect_zones, scales_with, grants_types,
     combat_damage, is_secondary, gain_control, damage_amount, cards_drawn, life_amount,
     produces_mana, mana_colors, counter_num_variable, grants_abilities, token_amount_variable)
     + forge_deck_tags (Forge's deck-building AI: has/hints/needs theme signals)
     Top features: card_hub_score 10%, strategy_cosine 9%, deck_edge_count 8%,
     cmc 8%, forge_ability_cosine 7%, forge_ability_depth 6%
  3. Forge mechanics vectors: 107-dim shared concept space encoding ALL mechanical
     interactions (27 game concepts + 80 subtypes). Captures synergy through
     card produces → commander consumes dot product.
  4. Can evaluate new cards day-1 without playtesting data
  5. Works for any of 3,141+ commanders (not just 1,361 with EDHREC)
  6. NDCG@30 = 0.53 on leave-commander-out CV

CAUSAL GRAPH (shared by both modes):
  - 18.4M edges across 30+ event types (verb_event_map extracted from Forge Java source)
  - SubAbility chains followed: 72k abilities (12.7k from secondary effects)
  - IDF weighting, chain scoring, anti-synergy detection
  - Synthetic edges (6.3M): SpellCast, Attacks, LandPlayed,
    ChangesZone+Battlefield (creatures, artifacts, enchantments, planeswalkers entering)
  - Entity presence edges (1.8M): token creators → "for each [Type]" scaling,
    sacrifice outlets, tap abilities. Connects Krenko → Brightstone Ritual.
  - Exact (subtype match, 0.3) and broad (card_type match, 0.15) precision levels
  - Materialized filter_precision column for fast queries
```

### Current Performance

Baseline model EDHREC alignment: **29.6/30 On-EDHREC, 4.6/30 Hi-Syn** (binary classifier)
Forge model EDHREC alignment: **4.8/30 On-EDHREC, 0.8/30 Hi-Syn** (mechanical synergy, by design finds different cards)

Note: "On-EDHREC" = our recs appear on EDHREC page. "Hi-Syn" = our recs have synergy >= 0.3.
Forge model deliberately finds mechanical synergies OUTSIDE EDHREC (avg 25/30 NotEDH).

| Commander | Forge On-EDHREC | Forge Hi-Syn | Baseline On-EDHREC |
|---|---|---|---|
| Krenko (goblin tribal) | 12/30 | 4/30 | 30/30 |
| Kyler (human tribal) | 4/30 | 2/30 | 30/30 |
| Y'shtola (draw/lifegain) | 4/30 | 3/30 | 30/30 |
| Ur-Dragon (dragon tribal) | 9/30 | 0/30 | 30/30 |
| Atraxa (counters/proliferate) | 5/30 | 0/30 | 27/30 |

## Common Commands

```bash
# === DATA PIPELINE ===
python3 download_cards.py                  # Refresh Scryfall data (~150MB)
python3 import_forge.py --download --import  # Update Forge ability data
python3 build_graph.py --forge --rebuild   # Build causal interaction graph from Forge (~17M edges)
python3 build_graph.py --stats             # Graph stats
python3 strategy_detector.py --populate    # Assign strategies
python3 fetch_spellbook.py                 # Fetch 82k combos

# === EDHREC data ===
python3 fetch_edhrec_decks.py --refresh    # Fetch EDHREC average decklists for top 1000 commanders

# === Fusion model (hybrid tower + LightGBM) ===
python3 train_fusion_model.py                  # Full pipeline: tower + features + GBM (~5 min)
python3 train_fusion_model.py --tower-only     # Stage 1 only: retrain EDHREC tower (~2 min)
python3 train_fusion_model.py --forge-tower    # Train forge tower on causal graph (~6 min)
python3 train_fusion_model.py --forge-only     # Train forge GBM (uses cached features, ~50s)
python3 train_fusion_model.py --forge-only --rebuild-features  # Rebuild feature cache + train (~4 min)
python3 train_fusion_model.py --features-only  # Build + inspect feature matrix
python3 train_fusion_model.py --feature-importance  # Print GBM feature importance
python3 train_fusion_model.py --holdout-eval   # True generalization (train 80% / test 20%)

# === Recommendations ===
python3 synergy_graph.py --deck krenko --recommend          # Baseline (EDHREC model)
python3 synergy_graph.py --deck krenko --recommend --forge  # Forge-only (no EDHREC)
python3 synergy_graph.py --deck krenko --swaps       # Card swap suggestions
python3 synergy_graph.py --deck krenko --combos      # 3-tier combo detection
python3 synergy_graph.py --deck krenko --deck-view   # Deck analysis

# === Comparison & validation ===
python3 compare_edhrec.py --refresh --quiet    # Compare all decks vs EDHREC
python3 compare_edhrec.py --deck krenko --fast  # Single deck (cached)
python3 compare_edhrec.py --fast --quiet        # Summary only (0.07s)
python3 compare_edhrec.py --forge --quiet      # Compare forge model vs EDHREC
python3 optimize_weights.py --evaluate           # Evaluate current weights (Recall@100)
python3 optimize_weights.py --fusion --evaluate  # Evaluate fusion model (Recall@100)

# Tests
uv run pytest tests/ -v                        # Run all tests
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → download_cards.py → data/oracle_cards.json (36k cards)
                                        ↓
                    import_forge.py → forge_abilities + forge_name_map tables
                                        ↓
                    build_graph.py --forge → interaction_edges table (17.1M causal edges, 30 event types)
                                        ↓
                    strategy_detector.py → card_strategies table
                                        ↓
                    fetch_spellbook.py → spellbook_combos table (82k combos)
                                        ↓
                    fetch_edhrec_decks.py → edhrec_card_synergy table (132k pairs)
                                        ↓
                    train_fusion_model.py → data/tower_model_edhrec.npz + data/fusion_model.lgb
                                        ↓
                              synergy_graph.py --deck <name>
                              (tower pre-filter → fusion model → causal graph)
```

### New-set update workflow

```bash
python3 download_cards.py                               # 1. Refresh Scryfall
python3 import_forge.py --download --import             # 2. Update Forge data
python3 build_graph.py --forge --rebuild                # 3. Rebuild causal graph
python3 strategy_detector.py --populate                 # 4. Strategies
python3 fetch_spellbook.py                              # 5. Refresh combos
python3 fetch_edhrec_decks.py --refresh                 # 6. Refresh EDHREC data (if new set)
python3 train_fusion_model.py                           # 7. Retrain fusion model (~5 min, $0)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Card metadata from Scryfall |
| abilities | ~76k | Parsed oracle text abilities |
| card_strategies | ~88k | Strategy assignments |
| spellbook_combos | ~82k | Commander Spellbook combos |
| spellbook_combo_cards | ~289k | Combo ↔ card junction |
| interaction_edges | ~18.4M | Causal edges from Forge: 30+ event types + 6.3M synthetic + 1.8M entity-presence edges |
| commander_profiles | ~3.4k | Auto-inferred commander archetypes (strategies, tribal, events) |
| edhrec_card_synergy | ~132k | EDHREC synergy scores for 502 commanders |
| forge_abilities | ~72k | Raw Forge ability data + SubAbility chain expansions (12.7k expanded rows). 20 columns: 19 consumed in features or during import, 1 unused (unless_cost). sub_ability column is resolved during import by expanding chains into separate rows. |
| forge_deck_tags | ~14k | Forge deck-building AI: has (what card provides), hints (what card wants), needs (what card requires). 9,868 unique cards. |
| forge_name_map | ~31k | Forge card name → oracle_id mapping (prefers non-token versions) |

### Fusion Models

**Baseline** (data/tower_model_edhrec.npz + data/fusion_model.lgb):
- Tower trained on EDHREC deck membership, GBM on 8 features with edhrec_synergy/rank
- AUC=0.999 on training commanders, ~25% Recall@100 on unseen
- Training: `python3 train_fusion_model.py`

**Forge-only** (data/fusion_model_forge.lgb):
- No tower model, no embeddings, no neural network — pure LightGBM on Forge data
- LambdaRank GBM on 63 features (shared via `src/mtg_synergy/recommend/forge_features.py`):
  100% Forge-native with 25 profile fields per card:
  causal scores (6), strategy (2), forge_ability_cosine, phase (2), tribal,
  card types (6), cmc, deck edges (3), causal_composite, card_hub_score,
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
  produces_mana, counter_num_variable, grants_abilities, token_amount_variable
- Forge profiles extract ALL raw_line fields: granted_keywords, conditions,
  duration, effect_zones, scales_with, grants_types, combat_damage, is_secondary,
  gain_control, damage_amount, cards_drawn, life_amount, required_subtypes,
  produces_mana, mana_colors, counter_num_variable, grants_abilities,
  token_amount_variable
  (from cost, defined, ValidCards$, Affected$, Produced$, CounterNum$,
  AddAbility$, TokenAmount$ fields)
- forge_deck_tags: Forge's deck-building AI (has/hints/needs) for 9,868 cards
  Maps what a card provides, wants, and requires in a deck
- Mechanics vectors (`src/mtg_synergy/recommend/mechanics_vectors.py`): 107-dim shared
  concept space (27 game concepts + 80 subtypes). Effects and triggers map to same
  dimensions. Dot product = mechanical synergy score.
- Training: EDHREC labels (1,355 commanders), forge-native features
  Self-supervised causal labels overfit (features ARE the causal graph)
- Training: `python3 train_fusion_model.py --forge-only --rebuild-features` (~5 min)
- Feature importance: card_hub_score 10%, strategy_cosine 9%, deck_edge_count 8%,
  cmc 8%, forge_ability_cosine 7%, forge_ability_depth 6%, forge_mech_fwd/rev 3%
- Edge index cached to npz (~2s reload vs ~40s DB scan)
- Edge index pre-loaded: CmdrFeatureContext uses in-memory adjacency
- Training data: generic staples (>30% deck frequency) filtered from positives
- Hard negative sampling: 50% strategy/subtype overlap + 50% random
- GBM: LambdaRank, num_leaves=255, lr=0.03, n_estimators=1500, label_gain=[0,1,2,3,5,8,12,18,25,35]

Both towers share architecture: 768→128 projection, MLP 140→128→64→32→1, sigmoid output
(Tower only used for baseline mode, not forge mode)

### Recommendation Pipeline (synergy_graph.py --recommend [--forge])

```
1. Candidate selection:
   Baseline: EDHREC tower scores 13k cards, takes top 3000
   Forge: Color-identity filter → ALL legal cards (no tower, no embeddings)
2. Score all candidates with GBM (batch predict, ~0.5s for 8000 cards):
   Baseline: 8 features (tower_prob, causal, edhrec_synergy, edhrec_rank, ...)
   Forge:    63 features (LambdaRank, 100% Forge-native, no oracle text)
3. Sort and output top 30 with clickable Scryfall hyperlinks (OSC 8)
Total time: ~1.5s (forge mode, no neural net overhead)
```

### Swap System (synergy_graph.py --swaps)

Suggests card swaps with multi-layer protection:
- **Infrastructure protection**: Cards providing removal/protection/ramp/draw → never cut
- **Tribal protection**: Creatures matching deck's dominant type → never cut
- **Changeling/Shapeshifter**: Always protected in tribal decks
- **Commander synergy protection**: Top 20 cards by causal graph edge score → never cut
- **Combo protection**: Cards in Spellbook combos with commander → never cut

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Circular trigger chain |
| Synergy | `synergy` | Interaction without confirmed loop |

## Key Files

### `src/mtg_synergy/parse/` package (deterministic oracle text parser)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/parse/__init__.py` | `parse_card()` pipeline + DB save/load |
| `src/mtg_synergy/parse/ast_types.py` | AST dataclasses: Ability, Effect, Trigger, Cost, ObjectFilter, etc. |
| `src/mtg_synergy/parse/splitter.py` | Pass 1-2: split oracle text into abilities, classify kind |
| `src/mtg_synergy/parse/trigger_parser.py` | Pass 3a: extract trigger events + subject filters (~25 patterns) |
| `src/mtg_synergy/parse/effect_parser.py` | Pass 3b: extract effect verbs + targets + amounts (~20 verbs) |
| `src/mtg_synergy/parse/cost_parser.py` | Pass 3c: parse mana/tap/sacrifice/life/loyalty costs |
| `src/mtg_synergy/parse/resolver.py` | Pass 4: resolve cross-references ("it", "that creature") |
| `src/mtg_synergy/parse/templates.py` | Template library for complex patterns (scaling, modal) |
| `src/mtg_synergy/parse/verb_resolvers.py` | Rules engine: Effect → StateChange (what game events occur) |
| `src/mtg_synergy/parse/forge_import.py` | Import Forge ability data into DB |

### `src/mtg_synergy/causal/` package (interaction graph + chain discovery)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/causal/__init__.py` | DB storage, CausalContext (pre-loaded scoring), anti-synergy detection |
| `src/mtg_synergy/causal/types.py` | Edge, EdgeDetail, Chain, ResourceDelta, LoopAnalysis dataclasses |
| `src/mtg_synergy/causal/indexer.py` | Index cards by events produced/consumed for fast edge building |
| `src/mtg_synergy/causal/graph_builder.py` | Build trigger/feeds/amplifies/enables/tribal edges |
| `src/mtg_synergy/causal/chain_finder.py` | DFS chain discovery + infinite loop detection |
| `src/mtg_synergy/causal/resource_flow.py` | Cost/production tracking for loop validation |

### `src/mtg_synergy/` package (core logic)

| Module | Purpose |
|---|---|
| `src/mtg_synergy/config.py` | Centralized paths, thresholds, and DB settings |
| `src/mtg_synergy/constants.py` | ACTION_EVENT_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES |
| `src/mtg_synergy/db.py` | Centralized DB connection factory |
| `src/mtg_synergy/cli.py` | CLI dispatcher (argparse + command routing) |
| `src/mtg_synergy/recommend/engine.py` | `recommend_cards()` — tower pre-filter + fusion model pipeline |
| `src/mtg_synergy/recommend/swaps.py` | `suggest_swaps()` — multi-layer card swap suggestions |
| `src/mtg_synergy/recommend/scoring.py` | `DeckContext`, `score_all_candidates()`, `tower_prefilter()` |
| `src/mtg_synergy/recommend/forge_features.py` | Shared 63-feature computation: `ForgeFeatureContext` (25 profile fields, edge index, mechanics vectors, deck tags — no embeddings), `CmdrFeatureContext`, `compute_card_features()` |
| `src/mtg_synergy/recommend/mechanics_vectors.py` | 107-dim forge mechanics vectors: shared game concept space for effect→trigger synergy |
| `src/mtg_synergy/recommend/affinity.py` | Commander affinity scoring |
| `src/mtg_synergy/recommend/commander_profile.py` | Auto-infer archetype for any of 3,141 commanders |
| `src/mtg_synergy/combos/detector.py` | `find_combos()`, `find_combos_tiered()`, `find_partial_combos()` |
| `src/mtg_synergy/combos/anti_synergy.py` | Anti-synergy detection |
| `src/mtg_synergy/combos/display.py` | Combo output formatting and validation |
| `src/mtg_synergy/analysis/deck.py` | Deck synergy display and analysis |
| `src/mtg_synergy/analysis/strategy.py` | Strategy detection, candidate filtering, commander builds |
| `src/mtg_synergy/causal/verb_event_map.py` | Forge verb → trigger event mapping (extracted from Java source) |

### Root-level scripts (pipelines + entry points)

| File | Purpose |
|---|---|
| `synergy_graph.py` | Thin wrapper — re-exports from `mtg_synergy/`, CLI entry point |
| `train_fusion_model.py` | Hybrid fusion model: tower retrain + LightGBM + holdout eval |
| `compare_edhrec.py` | Fast EDHREC comparison tool (parallel, cached) |
| `ability_parser.py` | Deterministic oracle text parser |
| `strategy_detector.py` | Rule-based strategy detection |
| `tag_db.py` | SQLite DB card query utilities + Scryfall tagger integration |
| `fetch_spellbook.py` | Commander Spellbook API fetcher |
| `build_graph.py` | Causal interaction graph builder CLI (--forge --rebuild, --stats) |
| `import_forge.py` | Forge ability data importer |
| `optimize_weights.py` | Weight optimization + Recall@K evaluation (--evaluate, --fusion) |
| `fetch_edhrec_decks.py` | Fetch EDHREC average decklists for top 1000 commanders |

## Key Conventions

- Cards keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency)
- Tribal tags auto-assigned from creature type_line (e.g., Human creature → tribal match)
- Deck configs live in `decks/` (15 decks: kyler, krenko, yshtola, atraxa, edgar, kaalia, niv_mizzet, pantlaza, sram, syr_konrad, tatyova, ur_dragon, urza, sauron)
- Package uses `src/` layout (`src/mtg_synergy/`), built with `uv_build` backend
- Fine-tuning uses `.venv` with unsloth + torch (Python 3.12, not system Python 3.14)
- Tests: 425 tests in `tests/`, run with `uv run pytest tests/`
- Spellbook combo boosts must check color identity (fixed: 364 wrong-color boosts deleted)
- The provides/wants tag tables are kept for backward compat but are not used by the hot path (--recommend, --swaps); they are populated by `derive_forge_tags.py` if needed
