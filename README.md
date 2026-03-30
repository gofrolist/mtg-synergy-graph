# MTG Synergy Graph

Analyze Magic: The Gathering EDH/Commander deck synergies using a LightGBM LambdaRank model trained on EDHREC data, powered by Forge game engine mechanics.

The system extracts card abilities from MTG Forge's game engine, builds a 20M+ edge causal interaction graph, and trains a learning-to-rank model to recommend synergistic cards for any of 3,141+ commanders.

## Quick Start

```bash
# 1. Download Scryfall bulk data (~150MB)
python3 download_cards.py

# 2. Import Forge ability data
python3 import_forge.py --download --import

# 3. Build causal interaction graph (~20M edges)
python3 build_graph.py --rebuild

# 4. Assign strategies
python3 strategy_detector.py --populate

# 5. Fetch EDHREC synergy data (training labels)
python3 fetch_edhrec_all.py --max 500

# 6. Train the model (~7 min)
python3 train_fusion_model.py --forge-only --rebuild-features

# 7. Get recommendations
python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend
```

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (package manager)
- No external API keys required (100% local)

```bash
# Install dependencies
uv sync
```

## How It Works

```
FORGE MODEL (--recommend): Zero oracle text, pure mechanical synergy

  1. Color-identity filter -> all legal cards for this commander
  2. LightGBM LambdaRank scores every candidate (105 features):
     - 29 profile fields per card from Forge abilities
     - Causal graph features (edge counts, hub scores, 2-hop paths)
     - Strategy cosine similarity
     - Forge deck tag overlap (has/hints/needs)
     - 33-dim functional fingerprints (produces/requires/amplifies/targets)
     - 116-dim mechanics vectors (shared concept space)
     - Counter/anthem/tribal distinction features
  3. Post-scoring: anti-synergy penalties + mechanical synergy bonus
  4. Top N results with clickable Scryfall links

  NDCG@30 = 0.53 | Works for 3,141+ commanders | Day-1 new card evaluation
```

## Commands

### Data Pipeline

```bash
python3 download_cards.py                    # Refresh Scryfall data (~150MB)
python3 import_forge.py --download --import  # Update Forge ability data
python3 build_graph.py --rebuild     # Build causal graph (~20M edges)
python3 strategy_detector.py --populate      # Assign strategies
python3 fetch_spellbook.py                   # Fetch 82k combos
```

### EDHREC Data

```bash
python3 fetch_edhrec_all.py                    # Fetch next 500 new commanders
python3 fetch_edhrec_all.py --max 2000         # Fetch up to 2000 new commanders
python3 fetch_edhrec_all.py --refresh-top 200  # Re-fetch top 200 popular commanders
python3 fetch_edhrec_all.py --stats            # Show coverage stats
```

### Model Training

```bash
python3 train_fusion_model.py --forge-only                     # Train (~3 min, cached features)
python3 train_fusion_model.py --forge-only --rebuild-features  # Rebuild features + train (~7 min)
python3 train_fusion_model.py --forge-only --quick             # Single-fold fast iteration (~2 min)
python3 train_fusion_model.py --forge-only --tune              # Hyperparameter search (~12 min)
```

### Recommendations

```bash
python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend       # Top 30 recommendations
python3 synergy_graph.py --commander "Krenko, Mob Boss" --recommend --top 10
python3 synergy_graph.py --commander "Krenko, Mob Boss" --gems            # Hidden gems (no popularity bias)
python3 synergy_graph.py --commander "Krenko, Mob Boss" --combos          # Combo detection
```

### Evaluation

```bash
python3 compare_edhrec.py --commander "Krenko, Mob Boss"  # Single commander vs EDHREC
python3 compare_edhrec.py --all --quiet                    # All commanders summary
```

### Tests

```bash
uv run pytest tests/ -v    # Run all 363 tests
```

## Architecture

### Enrichment Pipeline

```
Scryfall API -> download_cards.py -> data/oracle_cards.json (36k cards)
                                         |
                     import_forge.py -> forge_abilities + forge_name_map tables
                                         |
                     build_graph.py --forge -> interaction_edges (20.6M causal edges)
                                         |
                     strategy_detector.py -> card_strategies table
                                         |
                     fetch_spellbook.py -> spellbook_combos (82k combos)
                                         |
                     fetch_edhrec_all.py -> edhrec_card_synergy (733k pairs, 2,724 cmdrs)
                                         |
                     train_fusion_model.py -> data/fusion_model_forge.lgb
                                         |
                               synergy_graph.py --commander "Name" --recommend
```

### New-Set Update Workflow

```bash
python3 download_cards.py                                          # 1. Refresh Scryfall
python3 import_forge.py --download --import                        # 2. Update Forge data
python3 build_graph.py --rebuild                           # 3. Rebuild causal graph
python3 strategy_detector.py --populate                            # 4. Strategies
python3 fetch_spellbook.py                                         # 5. Refresh combos
python3 fetch_edhrec_all.py --max 2000 --refresh-top 200          # 6. Refresh EDHREC
python3 train_fusion_model.py --forge-only --rebuild-features     # 7. Retrain (~7 min, $0)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | ~36k | Card metadata from Scryfall |
| abilities | ~76k | Parsed oracle text abilities |
| card_strategies | ~88k | Strategy assignments |
| spellbook_combos | ~82k | Commander Spellbook combos |
| spellbook_combo_cards | ~289k | Combo <-> card junction |
| interaction_edges | ~20.6M | Causal edges: 30+ event types, synthetic, entity-presence, continuous pump, theme synergy |
| commander_profiles | ~3.4k | Auto-inferred commander archetypes |
| edhrec_card_synergy | ~733k | EDHREC synergy scores (2,724 commanders, 87% coverage) |
| forge_abilities | ~72k | Forge ability data + SubAbility chain expansions |
| forge_deck_tags | ~14k | Forge deck-building AI: has/hints/needs tags |
| forge_name_map | ~31k | Forge card name -> oracle_id mapping |

### Recommendation Pipeline

```
1. Candidate selection: color-identity filter -> all legal cards
2. Score all candidates with GBM (batch predict, ~0.5s for 13k cards)
3. Post-scoring: anti-synergy penalties + mechanical synergy bonus
4. Sort and output top N with Scryfall hyperlinks
Total: ~3s (cached) / ~7s (first run)
```

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Circular trigger chain |
| Synergy | `synergy` | Interaction without confirmed loop |

## Project Structure

```
.
├── synergy_graph.py           # CLI entry point (recommend, gems, combos)
├── train_fusion_model.py      # LightGBM LambdaRank training
├── compare_edhrec.py          # Evaluate recommendations vs EDHREC
├── build_graph.py             # Causal interaction graph builder
├── strategy_detector.py       # Rule-based strategy detection
├── download_cards.py          # Scryfall bulk data downloader
├── import_forge.py            # Forge ability data importer
├── fetch_edhrec_all.py        # EDHREC synergy + avg deck fetcher
├── fetch_spellbook.py         # Commander Spellbook API fetcher
│
├── tag_db.py                  # SQLite DB query utilities
├── card_db.py                 # In-memory card lookup
│
├── src/mtg_synergy/
│   ├── cli.py                 # CLI dispatcher
│   ├── config.py              # Paths, thresholds, DB settings
│   ├── db.py                  # DB connection factory
│   ├── recommend/
│   │   ├── engine.py          # recommend_cards() pipeline
│   │   ├── scoring.py         # Color filter, GBM scoring, mechanical bonus
│   │   ├── forge_features.py  # 105-feature computation (ForgeFeatureContext)
│   │   ├── mechanics_vectors.py  # 116-dim shared concept space
│   │   ├── hidden_gems.py     # Pure mechanical synergy (no popularity)
│   │   ├── swaps.py           # Card swap suggestions
│   │   ├── affinity.py        # Commander affinity scoring
│   │   └── commander_profile.py  # Auto-infer commander archetype
│   ├── causal/
│   │   ├── __init__.py        # DB storage, CausalContext, anti-synergy
│   │   ├── graph_builder.py   # Build causal edges from parsed abilities
│   │   ├── forge_graph_builder.py  # Build edges from Forge data
│   │   ├── indexer.py         # Index cards by events produced/consumed
│   │   └── verb_event_map.py  # Forge verb -> trigger event mapping
│   ├── parse/
│   │   ├── __init__.py        # parse_card() pipeline
│   │   ├── forge_import.py    # Forge DSL import with SVar resolution
│   │   └── ...                # AST types, parsers, resolvers
│   ├── combos/                # Combo detection + anti-synergy
│   └── analysis/              # Deck analysis + strategy detection
│
├── tests/                     # 363 tests
└── data/                      # DB, models, caches (mostly gitignored)
```

## Key Design Decisions

- **100% Forge-native**: No oracle text parsing, no embeddings, no neural networks. All features derived from Forge game engine data.
- **LambdaRank**: Learning-to-rank optimizes directly for recommendation ordering (NDCG), not classification.
- **EDHREC as training signal only**: Model learns from EDHREC synergy scores but can evaluate any commander, including those without EDHREC data.
- **Day-1 new cards**: Set `EDHREC_FREE=1` for pure mechanical inference (~7% lower NDCG but works for cards with zero play data).
- **Cards keyed by `oracle_id`**: Scryfall UUID deduplicates across reprints.

## License

MIT
