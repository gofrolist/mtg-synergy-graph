# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses a two-stage fusion model (tower pre-filter + LightGBM) trained on EDHREC avg deck data, with a Forge-derived causal interaction graph as a scoring signal.

### Signal Architecture (recommendation pipeline)

```
For any commander, recommendations use 3 signal layers:

1. TOWER PRE-FILTER (candidate discovery, <200ms)
   - Two-tower neural net: commander_embedding × card_embedding → P(card in deck)
   - Filters to color-legal cards, takes top 3000 by tower probability
   - Trained on 871 EDHREC commanders × ~260k pairs (AUC=0.979)
   - Replaces all tag-based candidate discovery

2. FUSION MODEL (final ranking, primary signal for 871 EDHREC commanders, $0 cost)
   - LightGBM on 8 features: tower_prob, causal_score, forge_deck_overlap,
     tribal_match, edhrec_synergy, edhrec_rank, cmc, is_creature
   - 5-fold leave-commander-out CV, mean AUC=0.999
   - Recall@100=89.4% on training commanders
   - Held-out generalization: ~25% Recall@100 on unseen commanders
   - train_fusion_model.py → data/tower_model_edhrec.npz + data/fusion_model.lgb

3. CAUSAL GRAPH (interaction scoring signal + fallback for non-EDHREC commanders)
   - 9.2M causal edges derived from Forge ability data (forge_abilities table)
   - Edge types: triggers, feeds, amplifies, enables, tribal
   - IDF weighting: rare events score higher than common ones
   - Chain scoring: commander → candidate → deck card paths get bonus
   - Anti-synergy detection (e.g., Rest in Peace in graveyard decks)
   - Recall@100=64.2% standalone
```

### Current Performance

Average EDHREC alignment: **14.9/30** (up from 2.8/30 baseline, 5.3x improvement)

| Commander | Score | Signal source |
|---|---|---|
| Sram (equipment/aura) | 23/30 | Fusion model |
| Krenko (goblin tribal) | 23/30 | Fusion model |
| Syr Konrad (graveyard) | 21/30 | Fusion model |
| Ur-Dragon (dragon tribal) | 20/30 | Fusion model |
| Pantlaza (dinosaur) | 17/30 | Fusion model |
| Urza (artifacts) | 17/30 | Fusion model |
| Edgar (vampire tribal) | 14/30 | Fusion model |
| Kyler (human tribal) | 13/30 | Fusion model |
| Tatyova (landfall) | 12/30 | Fusion model |
| Sauron (amass/ring) | 11/30 | Fusion model |
| Y'shtola (draw/lifegain) | 11/30 | Fusion model |
| Niv-Mizzet (draw/damage) | 10/30 | Fusion model |
| Kaalia (angels/demons/dragons) | 9/30 | Fusion model |
| Atraxa (counters/proliferate) | 7/30 | Fusion model |

## Common Commands

```bash
# === DATA PIPELINE ===
python3 download_cards.py                  # Refresh Scryfall data (~150MB)
python3 import_forge.py --download --import  # Update Forge ability data
python3 build_graph.py --forge --rebuild   # Build causal interaction graph from Forge (~9.2M edges)
python3 build_graph.py --stats             # Graph stats
python3 strategy_detector.py --populate    # Assign strategies
python3 fetch_spellbook.py                 # Fetch 82k combos

# === EDHREC data ===
python3 fetch_edhrec_decks.py --refresh    # Fetch EDHREC average decklists for top 1000 commanders

# === Fusion model (hybrid tower + LightGBM) ===
python3 train_fusion_model.py                  # Full pipeline: tower + features + GBM (~5 min)
python3 train_fusion_model.py --tower-only     # Stage 1 only: retrain tower (~2 min)
python3 train_fusion_model.py --features-only  # Build + inspect 8-feature matrix
python3 train_fusion_model.py --feature-importance  # Print GBM feature importance
python3 train_fusion_model.py --holdout-eval   # True generalization (train 80% / test 20%)
python3 train_fusion_model.py --holdout-eval --drop-feature edhrec_synergy  # Ablation

# === Recommendations ===
python3 synergy_graph.py --deck krenko --recommend   # Top 30 recommendations
python3 synergy_graph.py --deck krenko --swaps       # Card swap suggestions
python3 synergy_graph.py --deck krenko --combos      # 3-tier combo detection
python3 synergy_graph.py --deck krenko --deck-view   # Deck analysis

# === Comparison & validation ===
python3 compare_edhrec.py --refresh --quiet    # Compare all decks vs EDHREC
python3 compare_edhrec.py --deck krenko --fast  # Single deck (cached)
python3 compare_edhrec.py --fast --quiet        # Summary only (0.07s)
python3 optimize_weights.py --evaluate           # Evaluate current weights (Recall@100)
python3 optimize_weights.py --fusion --evaluate  # Evaluate fusion model (Recall@100)

# Tests
python3 -m pytest tests/ -v                    # Run all tests
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → download_cards.py → data/oracle_cards.json (36k cards)
                                        ↓
                    import_forge.py → forge_abilities + forge_name_map tables
                                        ↓
                    build_graph.py --forge → interaction_edges table (9.2M causal edges)
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
| interaction_edges | ~9.2M | Causal edges from Forge: triggers, feeds, amplifies, enables, tribal |
| commander_profiles | ~3.4k | Auto-inferred commander archetypes (strategies, tribal, events) |
| edhrec_card_synergy | ~132k | EDHREC synergy scores for 502 commanders |
| forge_abilities | ~59k | Raw Forge ability data (verb, trigger_mode, cost, raw_line) |
| forge_name_map | ~36k | Forge card name → oracle_id mapping |

### Fusion Model (data/tower_model_edhrec.npz + data/fusion_model.lgb)

Two-stage hybrid model trained on EDHREC avg deck membership:
- **Stage 1 — Tower** (data/tower_model_edhrec.npz):
  - Architecture: 768→128 projection, MLP 140→128→64→32→1, sigmoid output P(card in deck)
  - Trained on 871 EDHREC commanders × ~260k pairs (65k positive + 195k negative)
  - AUC=0.979, accuracy=93.4%
- **Stage 2 — LightGBM** (data/fusion_model.lgb):
  - 8 features: tower_prob, causal_score, forge_deck_overlap, tribal_match,
    edhrec_synergy, edhrec_rank, cmc, is_creature
  - 5-fold leave-commander-out CV, mean AUC=0.999
  - Feature importance: edhrec_rank > tower_prob > edhrec_synergy > cmc
- **Performance on EDHREC commanders**: Recall@100=89.4%
- **Held-out generalization**: ~25% Recall@100 on unseen commanders
- **Training**: `python3 train_fusion_model.py` (~5 min)
- **Evaluation**: `python3 optimize_weights.py --fusion --evaluate`
- **Holdout eval**: `python3 train_fusion_model.py --holdout-eval`
- **Feature ablation**: `python3 train_fusion_model.py --holdout-eval --drop-feature edhrec_synergy`

### Recommendation Pipeline (synergy_graph.py --recommend)

```
1. Tower pre-filter: score all color-legal cards, take top 3000 candidates
2. Score all candidates with full fusion model:
   a. Build 8-feature vector per candidate
      (tower_prob, causal_score, forge_deck_overlap, tribal_match,
       edhrec_synergy, edhrec_rank, cmc, is_creature)
   b. GBM predict_proba → fusion score (PRIMARY signal)
3. Sort and output top 30
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

### `mtg_synergy/parse/` package (deterministic oracle text parser)

| Module | Purpose |
|---|---|
| `mtg_synergy/parse/__init__.py` | `parse_card()` pipeline + DB save/load |
| `mtg_synergy/parse/ast_types.py` | AST dataclasses: Ability, Effect, Trigger, Cost, ObjectFilter, etc. |
| `mtg_synergy/parse/splitter.py` | Pass 1-2: split oracle text into abilities, classify kind |
| `mtg_synergy/parse/trigger_parser.py` | Pass 3a: extract trigger events + subject filters (~25 patterns) |
| `mtg_synergy/parse/effect_parser.py` | Pass 3b: extract effect verbs + targets + amounts (~20 verbs) |
| `mtg_synergy/parse/cost_parser.py` | Pass 3c: parse mana/tap/sacrifice/life/loyalty costs |
| `mtg_synergy/parse/resolver.py` | Pass 4: resolve cross-references ("it", "that creature") |
| `mtg_synergy/parse/templates.py` | Template library for complex patterns (scaling, modal) |
| `mtg_synergy/parse/verb_resolvers.py` | Rules engine: Effect → StateChange (what game events occur) |
| `mtg_synergy/parse/forge_import.py` | Import Forge ability data into DB |

### `mtg_synergy/causal/` package (interaction graph + chain discovery)

| Module | Purpose |
|---|---|
| `mtg_synergy/causal/__init__.py` | DB storage, CausalContext (pre-loaded scoring), anti-synergy detection |
| `mtg_synergy/causal/types.py` | Edge, EdgeDetail, Chain, ResourceDelta, LoopAnalysis dataclasses |
| `mtg_synergy/causal/indexer.py` | Index cards by events produced/consumed for fast edge building |
| `mtg_synergy/causal/graph_builder.py` | Build trigger/feeds/amplifies/enables/tribal edges |
| `mtg_synergy/causal/chain_finder.py` | DFS chain discovery + infinite loop detection |
| `mtg_synergy/causal/resource_flow.py` | Cost/production tracking for loop validation |

### `mtg_synergy/` package (core logic)

| Module | Purpose |
|---|---|
| `mtg_synergy/config.py` | Centralized paths, thresholds, and DB settings |
| `mtg_synergy/constants.py` | ACTION_EVENT_BRIDGES, TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES |
| `mtg_synergy/db.py` | Centralized DB connection factory |
| `mtg_synergy/cli.py` | CLI dispatcher (argparse + command routing) |
| `mtg_synergy/recommend/engine.py` | `recommend_cards()` — tower pre-filter + fusion model pipeline |
| `mtg_synergy/recommend/swaps.py` | `suggest_swaps()` — multi-layer card swap suggestions |
| `mtg_synergy/recommend/scoring.py` | `DeckContext`, `score_all_candidates()`, `tower_prefilter()` |
| `mtg_synergy/recommend/affinity.py` | Commander affinity scoring |
| `mtg_synergy/recommend/commander_profile.py` | Auto-infer archetype for any of 3,141 commanders |
| `mtg_synergy/combos/detector.py` | `find_combos()`, `find_combos_tiered()`, `find_partial_combos()` |
| `mtg_synergy/combos/anti_synergy.py` | Anti-synergy detection |
| `mtg_synergy/combos/display.py` | Combo output formatting and validation |
| `mtg_synergy/analysis/deck.py` | Deck synergy display and analysis |
| `mtg_synergy/analysis/strategy.py` | Strategy detection, candidate filtering, commander builds |
| `mtg_synergy/analysis/visualization.py` | Interactive HTML/D3 visualization |

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
- Fine-tuning uses `.venv` with unsloth + torch (Python 3.12, not system Python 3.14)
- Tests: ~386 tests in `tests/`
- Spellbook combo boosts must check color identity (fixed: 364 wrong-color boosts deleted)
- The provides/wants tag tables are kept for backward compat but are not used by the hot path (--recommend, --swaps); they are populated by `derive_forge_tags.py` if needed
