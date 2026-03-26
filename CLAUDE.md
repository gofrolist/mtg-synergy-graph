# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MTG Synergy Graph — a tool for analyzing Magic: The Gathering EDH/Commander deck synergies. The system uses multiple signal layers to find card synergies: LLM scoring, a trained two-tower neural model, structured game mechanics extraction, and a tag-based provides/wants graph.

### Signal Architecture (recommendation pipeline)

```
For any commander, recommendations use 6 signal layers:

0. FUSION MODEL (PRIMARY signal for 871 EDHREC commanders, $0 cost)
   - Two-stage hybrid: retrained tower (AUC=0.979) + LightGBM (CV AUC=0.999)
   - Tower: binary P(card in deck) from 768-dim embeddings + 12 structural features
   - GBM: 10 features (tower_prob, causal, forge, tags, tribal, edhrec_synergy, rank, cmc, etc.)
   - Trained on 871 EDHREC commanders × ~260k pairs (positive + 3:1 negative)
   - Recall@100=89.4% on training commanders (replaces LLM scoring at $0)
   - Held-out generalization: ~25% Recall@100 on unseen commanders
   - CAUSAL GRAPH is the fallback for unknown commanders
   - train_fusion_model.py → data/tower_model_edhrec.npz + data/fusion_model.lgb

1. LLM SCORES (legacy, 33 commanders only — superseded by fusion for EDHREC commanders)
   - Pre-scored via score_synergies.py (OpenAI gpt-5.4-mini or local gemma3:12b)
   - 33 commanders scored, ~180k pairs
   - Integer 1-10 scale, was primary ranking signal before fusion model

2. CAUSAL GRAPH (deterministic, $0 cost, any commander — fallback for non-EDHREC commanders)
   - Oracle text parser → AST → verb resolvers → StateChanges
   - 7.6M IDF-weighted causal edges: triggers, feeds, amplifies, enables, tribal
   - IDF weighting: rare events (goblin_enters) get 3x, common (creature_enters) get 0.12x
   - Chain scoring: commander → candidate → deck card paths get bonus
   - Anti-synergy detection (Rest in Peace=-2.72 in graveyard decks)
   - Commander relevance, effect impact, strategy alignment, bidirectional bonus
   - Lazy-loaded per card (0.1s init, <50MB memory)
   - 15,000 cards parsed (up from 5,000), 30,961 abilities
   - Recall@100=64.2% standalone

3. TOWER MODEL (legacy, trained on LLM scores — superseded by fusion tower)
   - Two-tower neural net: commander_embedding × card_embedding → synergy
   - Trained on 99k LLM-scored pairs, corr=0.75 with LLM scores
   - Scores ALL cards for any commander in <100ms

4. MECHANICS ENGINE (filter-aware event chain matching)
   - 7105 cards with structured mechanics extracted via LLM
   - 6 matching modes: event chain, card-IS-event, shared trigger,
     modifier, enabler, self-sacrifice

5. TAG GRAPH (provides/wants edges, baseline signal)
   - 34k cards tagged with role/provides/wants
   - Composite edges: provides→wants + peer-enabler + shared-wants + embedding
   - Commander 5x edge weight, keyword-only creature penalty
   - Strategy multiplier, tribal boost, combo completion bonus
```

### Current Performance

Average EDHREC alignment: **14.9/30** (up from 2.8/30 baseline, 5.3x improvement)

| Commander | Score | Signal source |
|---|---|---|
| Sram (equipment/aura) | 23/30 | LLM + mechanics |
| Krenko (goblin tribal) | 23/30 | LLM + EDHREC tiebreaker |
| Syr Konrad (graveyard) | 21/30 | LLM + mechanics |
| Ur-Dragon (dragon tribal) | 20/30 | LLM + DFC fix |
| Pantlaza (dinosaur) | 17/30 | LLM + type matching |
| Urza (artifacts) | 17/30 | LLM + mechanics |
| Edgar (vampire tribal) | 14/30 | LLM + tribal tags |
| Kyler (human tribal) | 13/30 | LLM + tribal tags |
| Tatyova (landfall) | 12/30 | LLM + tower model |
| Sauron (amass/ring) | 11/30 | LLM (gemma3 local) |
| Y'shtola (draw/lifegain) | 11/30 | LLM + event chains |
| Niv-Mizzet (draw/damage) | 10/30 | LLM + event chains |
| Kaalia (angels/demons/dragons) | 9/30 | LLM (EDHREC top cards already in deck) |
| Atraxa (counters/proliferate) | 7/30 | LLM (counter build vs EDHREC superfriends) |

## Common Commands

```bash
# === EXISTING PIPELINE ===
python3 download_cards.py                  # Refresh Scryfall data (~150MB)
python3 derive_forge_tags.py               # Derive provides/wants/role from Forge abilities (wipe + repopulate)
python3 derive_forge_tags.py --dry-run     # Preview without writing
python3 derive_forge_tags.py --card "Krenko, Mob Boss"  # Show tags for one card
python3 ability_parser.py                  # Parse oracle text into abilities
python3 fetch_spellbook.py                 # Fetch 82k combos
python3 strategy_detector.py --populate    # Assign strategies

# === NEW: Synergy scoring pipeline ===
# Score a commander with OpenAI (best quality, ~$0.50/commander, ~2 min)
python3 score_synergies.py --commander "Krenko, Mob Boss"
python3 score_synergies.py --commander "Krenko, Mob Boss" --batch-api  # 50% cheaper

# Score a commander locally with Ollama (free, ~90 min)
python3 score_synergies.py --commander "Krenko, Mob Boss" --provider ollama --model gemma3:12b

# Score all deck commanders
python3 score_synergies.py --all-decks
python3 score_synergies.py --all-decks --batch-api

# Score new set cards against all known commanders
python3 score_synergies.py --new-cards data/new_set.json

# Check scoring progress
python3 score_synergies.py --stats

# === NEW: Mechanics extraction ===
python3 extract_mechanics.py --batch 1000   # Extract next 1000 cards by EDHREC rank
python3 extract_mechanics.py --card "Krenko, Mob Boss"  # Single card
python3 extract_mechanics.py --stats        # Check coverage
python3 extract_mechanics.py --validate     # Test matching quality

# === Fusion model (hybrid tower + LightGBM) ===
python3 train_fusion_model.py                  # Full pipeline: tower + features + GBM (~5 min)
python3 train_fusion_model.py --tower-only     # Stage 1 only: retrain tower (~2 min)
python3 train_fusion_model.py --features-only  # Build + inspect 10-feature matrix
python3 train_fusion_model.py --feature-importance  # Print GBM feature importance
python3 train_fusion_model.py --holdout-eval   # True generalization (train 80% / test 20%)
python3 train_fusion_model.py --holdout-eval --drop-feature edhrec_synergy  # Ablation

# === Legacy tower model training (superseded by fusion) ===
python3 train_tower_model.py               # Train from LLM scores (~45s)
python3 train_tower_model.py --predict "Any Commander"  # Score all cards (<100ms)
python3 train_tower_model.py --stats       # Check training data

# === Recommendations ===
python3 synergy_graph.py --deck krenko --recommend   # Top 30 recommendations
python3 synergy_graph.py --deck krenko --swaps       # Card swap suggestions
python3 synergy_graph.py --deck krenko --combos      # 3-tier combo detection
python3 synergy_graph.py --deck krenko --deck-view   # Deck analysis

# === Comparison & validation ===
python3 compare_edhrec.py --refresh --quiet    # Compare all decks vs EDHREC
python3 compare_edhrec.py --deck krenko --fast  # Single deck (cached)
python3 compare_edhrec.py --fast --quiet        # Summary only (0.07s)

# === NEW: Deterministic rules engine ===
python3 oracle_parser.py --parse-all --top 5000  # Parse oracle text into ASTs (0 failures)
python3 oracle_parser.py --card "Krenko, Mob Boss" --verbose  # Single card parse
python3 oracle_parser.py --stats                 # Parse coverage stats
python3 build_graph.py --rebuild                 # Build causal interaction graph (~1.17M edges)
python3 build_graph.py --stats                   # Graph stats
python3 optimize_weights.py --quick              # Optimize weights against 502 EDHREC commanders
python3 optimize_weights.py --evaluate           # Evaluate current weights (Recall@100)
python3 optimize_weights.py --fusion --evaluate  # Evaluate fusion model (Recall@100)

# Tests
python3 -m pytest tests/ -v                    # Run all 326 tests
```

## Architecture

### Enrichment Pipeline

```
Scryfall API → download_cards.py → data/oracle_cards.json (36k cards)
                                        ↓
                              derive_forge_tags.py → provides/wants/role in data/tags.db
                                        ↓
                    ability_parser.py → abilities table
                                        ↓
                    fetch_spellbook.py → spellbook_combos table (82k combos)
                                        ↓
                    strategy_detector.py → card_strategies table
                                        ↓
                    extract_mechanics.py → card_mechanics table (7105 cards)
                                        ↓
                    score_synergies.py → synergy_scores table (99k pairs, 33 commanders)
                                        ↓
                    oracle_parser.py → parsed_abilities table (5000 cards, 10635 abilities)
                                        ↓
                    build_graph.py → interaction_edges table (7.6M IDF-weighted causal edges)
                                        ↓
                    train_fusion_model.py → data/tower_model_edhrec.npz + data/fusion_model.lgb
                                        ↓
                              synergy_graph.py --deck <name>
                              (fusion model primary → causal fallback → tags)
```

### New-set update workflow

```bash
python3 download_cards.py                               # 1. Refresh Scryfall
python3 import_forge.py --download --import             # 2. Update Forge data
python3 derive_forge_tags.py                            # 3. Derive provides/wants from Forge
python3 ability_parser.py                               # 4. Parse abilities
python3 strategy_detector.py --populate                 # 5. Strategies
python3 extract_mechanics.py --batch 1000               # 6. Extract mechanics for new cards
python3 oracle_parser.py --parse-all --top 5000         # 7. Parse oracle text
python3 build_graph.py --rebuild                        # 8. Rebuild causal graph
python3 fetch_edhrec_decks.py --refresh                 # 9. Refresh EDHREC data (if new set)
python3 train_fusion_model.py                           # 10. Retrain fusion model (~5 min, $0)
```

### DB Schema (data/tags.db)

| Table | Rows | Purpose |
|---|---|---|
| cards | 33,930 | Card metadata from Scryfall |
| provides | 105k+ | Card provides tags (incl. auto-tribal from type_line) |
| wants | 89,356 | Card wants tags |
| abilities | 76,601 | Parsed oracle text abilities |
| card_strategies | 88,304 | Strategy assignments |
| spellbook_combos | 82,103 | Commander Spellbook combos |
| spellbook_combo_cards | 288,973 | Combo ↔ card junction |
| card_mechanics | ~17k | Structured game mechanics (LLM-extracted) |
| synergy_scores | ~180k | Commander × card synergy scores (LLM + auto) |
| parsed_abilities | ~31k | Deterministic oracle text ASTs (from oracle_parser.py, 15k cards) |
| interaction_edges | ~7.6M | IDF-weighted causal edges: triggers, feeds, amplifies, enables, tribal |
| commander_profiles | ~3.4k | Auto-inferred commander archetypes (strategies, tribal, events) |
| edhrec_card_synergy | ~132k | EDHREC synergy scores for 502 commanders |

### Tag Schema (3-field)

Tags are derived deterministically from Forge ability data (59k abilities, 93% card coverage) by `derive_forge_tags.py`. Zero LLM cost; auto-maintained on Forge import.

Each card is tagged with:
- **role**: land, removal, ramp, draw, protection, threat, utility — derived from Forge verbs
- **provides**: ~90 tags — game actions (token, draw, mana, destroy, put-counter, mill, scry, surveil, etc.), keywords (flying, trample, etc.), tribal (goblin-tribal, human-tribal, etc.)
- **wants**: ~35 tags — events the card benefits from (dies, enters-battlefield, spell-cast, damage-done, sacrificed, etc.)

Tags are 1:1 with Forge verbs/triggers; no sub-tag hierarchy. Tribal tags auto-assigned from creature type_line.

### Mechanics Schema (card_mechanics table)

Each card's abilities are extracted as structured JSON:
- **mechanic_type**: triggered, activated, static, replacement, keyword
- **trigger_event**: creature-enters, spell-cast, creature-dies, etc. (27 canonical events)
- **trigger_filter**: JSON filter (e.g., `{"subtype": "Human", "controller": "you"}`)
- **effect_action**: create-token, deal-damage, draw-card, etc. (35 canonical actions)
- **cost**: activation cost for activated abilities

Matching modes in mechanics_matcher.py:
1. **Event chain**: A produces events → B triggers on those events
2. **Card-IS-event**: B being cast/entering satisfies A's trigger filter (tribal matching)
3. **Shared trigger**: A and B both respond to same event type
4. **Modifier**: A amplifies/doubles what B does
5. **Enabler**: A's static scope applies to B's creature types
6. **Self-sacrifice**: Cards with sacrifice-self cost produce creature-dies event

### Synergy Scoring (synergy_scores table)

Pre-computed commander × card synergy scores on 1-10 scale:
- **Scoring providers**: OpenAI (gpt-5.4-mini), local Ollama (gemma3:12b, normalized)
- **Pre-filtering**: Top 2000 candidates per commander (by tag overlap + mechanics + EDHREC rank)
- **Auto-scoring**: Cards with zero synergy signals auto-scored as 2
- **Spellbook boost**: Cards in confirmed combos with commander boosted to 9
- **No EDHREC score overrides**: LLM/auto scores are preserved as-is (EDHREC used only at runtime as tiebreaker)
- **Resume-safe**: Every batch committed immediately, re-run picks up where it stopped
- **Batch API**: `--batch-api` flag for 50% cheaper OpenAI processing

### Fusion Model (data/tower_model_edhrec.npz + data/fusion_model.lgb)

Two-stage hybrid model trained on EDHREC avg deck membership:
- **Stage 1 — Tower** (data/tower_model_edhrec.npz):
  - Same architecture as legacy tower (768→128 projection, MLP 140→128→64→32→1)
  - Binary cross-entropy loss, sigmoid output P(card in deck)
  - Trained on 871 EDHREC commanders × ~260k pairs (65k positive + 195k negative)
  - AUC=0.979, accuracy=93.4%
- **Stage 2 — LightGBM** (data/fusion_model.lgb):
  - 10 features: tower_prob, causal_score, forge_deck_overlap, cmdr_tag_overlap,
    strategy_keyword, tribal_match, edhrec_synergy, edhrec_rank, cmc, is_creature
  - 5-fold leave-commander-out CV, mean AUC=0.999
  - Feature importance: edhrec_rank > tower_prob > edhrec_synergy > cmc
- **Performance on EDHREC commanders**: Recall@100=89.4% (replaces LLM at $0)
- **Held-out generalization**: ~25% Recall@100 on unseen commanders (model over-relies
  on edhrec_synergy for known commanders; causal graph fallback handles unknown)
- **Ablation**: Dropping edhrec_synergy barely changes generalization (~24→26% Recall@100);
  edhrec_rank and tower_prob carry most of the generalizable signal
- **Training**: `python3 train_fusion_model.py` (~5 min)
- **Evaluation**: `python3 optimize_weights.py --fusion --evaluate`
- **Holdout eval**: `python3 train_fusion_model.py --holdout-eval`
- **Feature ablation**: `python3 train_fusion_model.py --holdout-eval --drop-feature edhrec_synergy`

### Legacy Tower Model (data/tower_model.npz)

Two-tower neural network trained on LLM synergy scores (superseded by fusion model):
- **Input**: Commander embedding (768-dim) × Card embedding (768-dim) + 12 structural features
- **Architecture**: Projection (768→128) → element-wise product → MLP (140→128→64→32→1)
- **Training**: 99k pairs from 33 commanders, Adam optimizer, 150 epochs, ~45s
- **Performance**: correlation=0.75, MAE=1.01 with LLM scores
- **Inference**: <100ms for ALL cards for any commander
- **Status**: Weight=0 in SCORING_WEIGHTS (disabled), kept for backward compat

### Recommendation Pipeline (synergy_graph.py --recommend)

```
1. Build graph (provides→wants edges + embeddings + peer-enabler)
2. Inject LLM≥7 candidates from synergy_scores (bypasses graph candidate pool)
3. Inject EDHREC≥0.25 synergy candidates from edhrec_card_synergy (DFC-aware)
4. Inject mechanics≥1.5 candidates from card_mechanics
5. Score all candidates with compute_dynamic_score():
   a. Compute 12 base features (tags, strategy, tribal, causal, forge, rank, etc.)
   b. If USE_FUSION_MODEL and model loaded:
      → Run tower P(in deck) + build 10-feature vector → GBM predict_proba
      → total = fusion_score × FUSION_weight (PRIMARY signal)
   c. Else (fallback): weighted sum of 12 base features
6. Sort and output top 30
```

### Swap System (synergy_graph.py --swaps)

Suggests card swaps with multi-layer protection:
- **Infrastructure protection**: Cards providing removal/protection/ramp/draw → never cut
- **Tribal protection**: Creatures matching deck's dominant type → never cut
- **Changeling/Shapeshifter**: Always protected in tribal decks
- **Commander synergy protection**: Top 20 cards by commander graph edge score → never cut
- **Mechanics protection**: Cards with mechanics score ≥2.0 with commander → never cut
- **Combo protection**: Cards in Spellbook combos with commander → never cut

### Combo Detection (3-tier)

| Tier | Label | Detection |
|------|-------|-----------|
| Confirmed Infinite | `infinite-confirmed` | All combo cards match a Commander Spellbook entry |
| Likely Combo | `combo-likely` | Provides→wants cycle + circular trigger chain |
| Synergy | `synergy` | Provides→wants cycle without trigger chain |

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
| `mtg_synergy/constants.py` | ACTION_EVENT_BRIDGES (~25 deterministic action→event mappings, same variable name as old SEMANTIC_BRIDGES for compat), TRIGGER_EFFECT_BRIDGES, STAPLE_ROLES |
| `mtg_synergy/db.py` | Centralized DB connection factory |
| `mtg_synergy/cli.py` | CLI dispatcher (argparse + command routing) |
| `mtg_synergy/graph/builder.py` | `build_graph()` — composite edge graph |
| `mtg_synergy/graph/edges.py` | Edge computation: provides→wants, peer, shared-wants, embedding |
| `mtg_synergy/graph/idf.py` | IDF tag weighting |
| `mtg_synergy/recommend/engine.py` | `recommend_cards()` — 4-layer scoring pipeline |
| `mtg_synergy/recommend/swaps.py` | `suggest_swaps()` — multi-layer card swap suggestions |
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
| `derive_forge_tags.py` | Derive provides/wants/role tags from Forge abilities (wipe + repopulate, $0) |
| `score_synergies.py` | LLM synergy scoring (OpenAI + Ollama + Batch API) |
| `extract_mechanics.py` | Structured mechanics extraction from oracle text |
| `mechanics_matcher.py` | Filter-aware event chain matching engine |
| `train_fusion_model.py` | Hybrid fusion model: tower retrain + LightGBM + holdout eval |
| `train_tower_model.py` | Legacy two-tower neural synergy model training (superseded by fusion) |
| `compare_edhrec.py` | Fast EDHREC comparison tool (parallel, cached) |
| `batch_tagger.py` | Legacy: LLM-based card tagging (superseded by derive_forge_tags.py) |
| `ability_parser.py` | Deterministic oracle text parser |
| `strategy_detector.py` | Rule-based strategy detection |
| `tag_db.py` | SQLite DB management |
| `fetch_spellbook.py` | Commander Spellbook API fetcher |
| `reclassify_tags.py` | Legacy: re-map LLM tags to sub-tags (superseded by derive_forge_tags.py) |
| `oracle_parser.py` | Deterministic oracle text parser CLI (parse-all, card, stats) |
| `build_graph.py` | Causal interaction graph builder CLI (rebuild, stats) |
| `optimize_weights.py` | Weight optimization + Recall@K evaluation (--evaluate, --fusion, --no-llm, --novelty, --deck) |
| `fetch_edhrec_decks.py` | Fetch EDHREC average decklists for top 1000 commanders |

## Key Conventions

- Cards keyed by `oracle_id` (Scryfall UUID) for dedup across reprints
- `data/oracle_cards.json` is gitignored (~150MB); must run `download_cards.py` first
- API calls use `urllib.request` (no `requests` dependency)
- Tags use kebab-case (e.g., `mana`, `dies`)
- Tribal tags auto-assigned from Forge type_line (e.g., Human creature → provides `human-tribal`)
- Deck configs live in `decks/` (15 decks: kyler, krenko, yshtola, atraxa, edgar, kaalia, niv_mizzet, pantlaza, sram, syr_konrad, tatyova, ur_dragon, urza, sauron)
- LLM scoring uses gpt-5.4-mini (requires `max_completion_tokens` not `max_tokens`)
- Local scoring uses gemma3:12b via Ollama (best quality/speed local model)
- Qwen3 models need `think: false` in Ollama payload to disable thinking
- Fine-tuning uses `.venv` with unsloth + torch (Python 3.12, not system Python 3.14)
- Tests: 439 tests in `tests/`
- Spellbook combo boosts must check color identity (fixed: 364 wrong-color boosts deleted)
- provides/wants tags are derived from Forge data; do not edit manually or re-run LLM tagger
