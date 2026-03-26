# Local Synergy Model — Design Spec

**Date**: 2026-03-25
**Goal**: Replace LLM scoring with a hybrid local model (tower + LightGBM) that scores any commander × card pair instantly, retrains when Forge updates. Zero ongoing cost.

## Why

- LLM scoring costs $0.50/commander, covers only 33/3438 commanders (1%)
- Static data — stale after every set release, must re-score to include new cards
- LLM synergy recall (72.5%) is the ceiling we want to match with a local model
- Causal-only recall (64.2% synergy, 53.5% avg deck) is the floor

## Current State

| Signal | Synergy Recall@100 | Avg Deck Recall@100 | Cost | Coverage |
|--------|-------------------|--------------------|----- |----------|
| LLM (33 commanders) | 72.5% | 57.1% | $0.50/cmdr | 4% |
| Causal ($0) | 64.2% | 53.5% | $0 | 100% |
| Tower model (current) | ~65% | ~53% | $0 | 100% |
| Conditional blend | 64.4% | 53.4% | $0 | 100% |

## Architecture: Two-Stage Hybrid

### Stage 1 — Tower Model (retrained)

Retrain the existing two-tower neural net on EDHREC avg deck membership.

- **Architecture**: Same as current (768→128 projection per tower, element-wise product, MLP head) with 12 structural features unchanged (MLP input: 128+12=140)
- **Loss**: Binary cross-entropy (was MSE on LLM scores)
- **Output**: Sigmoid probability P(card belongs in commander's deck), range 0-1
- **Training data**: 871 EDHREC commanders, from `edhrec_average_decks` table (columns: commander_slug, card_name, category)
  - Positive: cards in average decklist (~75 per commander, ~65k total)
  - Negative: 3:1 random cards filtered by color identity, excluding basics/tokens
  - Total: ~260k training pairs
- **Purpose**: Captures semantic embedding similarity — "this card's text is conceptually related to what this commander does"
- **Output file**: `data/tower_model_edhrec.npz`

### Stage 2 — LightGBM Classifier

Gradient boosting on tower probability + 9 handcrafted features.

- **Library**: LightGBM (fast, handles missing values natively, no GPU)
- **Target**: Binary — card in EDHREC avg deck (same labels as tower)
- **Output**: `predict_proba()` probability used for final ranking
- **Hyperparameters**: `num_leaves=63, learning_rate=0.05, n_estimators=500, early_stopping=50`
- **Output file**: `data/fusion_model.lgb`

### Feature Vector (10 features per commander×card pair)

| # | Feature | Source | Notes |
|---|---------|--------|-------|
| 1 | `tower_prob` | Stage 1 tower output | P(in deck), 0-1 |
| 2 | `causal_score` | interaction_edges (9.2M Forge-native edges) | IDF-weighted |
| 3 | `forge_deck_overlap` | forge_deck_tags (14k tags) | Bidirectional: `len(card_has & cmdr_hints) + len(card_hints & cmdr_has)` |
| 4 | `cmdr_tag_overlap` | provides/wants tables (105k+89k) | Card ↔ commander overlap |
| 5 | `strategy_keyword` | Oracle text pattern match via `STRATEGY_KEYWORDS` dict in scoring.py (~80 patterns across 20 strategies) | Count of matching keywords for deck's detected strategies |
| 6 | `tribal_match` | Creature type line overlap | Binary |
| 7 | `edhrec_synergy` | edhrec_card_synergy (230k pairs) | 0 if missing |
| 8 | `edhrec_rank` | cards table | log10(popularity rank) |
| 9 | `cmc` | cards table | Converted mana cost |
| 10 | `is_creature` | cards table | Binary |

**Dropped features** (redundant post-Forge migration):
- `mechanics_score` — overlaps with causal_score (both detect trigger→effect chains)
- `strategy_overlap` — from card_strategies, uses old tag vocabulary

### Inference Flow

```
recommend_cards(commander)
  → tower.predict(commander_emb, all_card_embs)    # <100ms, 34k cards
  → take top 2000 by tower_prob (+ any with causal_score > threshold as safety net)
  → build feature matrix for candidates
  → gbm.predict_proba(features)                    # <10ms, 2000 cards
  → rank by GBM probability, return top 30
```

### Cross-Validation

- **Method**: 5-fold leave-commander-group-out
- Train on 80% of commanders (697), test on 20% (174)
- Tests generalization to unseen commanders (the real use case)
- EDHREC synergy as a feature: no leakage because CV split is by commander (each commander's synergy scores are computed independently by EDHREC). For commanders outside the 871 training set, `edhrec_synergy=0` — the model must handle this gracefully via LightGBM's native missing-value support

## Training Approach

GBM trained on binary deck membership (Approach 3 from brainstorming):
- **Target**: 1 if card in EDHREC avg deck, 0 if not (clean labels)
- **EDHREC synergy as input feature**, not target — GBM learns "high synergy → likely in deck" without being enslaved to noisy scores
- **Negative sampling**: 3:1 ratio, color-identity filtered, excluding basics/tokens

## Integration

### New files
- `train_fusion_model.py` — trains both stages, constructs feature matrix, outputs tower + GBM models
- `data/tower_model_edhrec.npz` — retrained tower weights
- `data/fusion_model.lgb` — LightGBM model

### Modified files
- `mtg_synergy/recommend/scoring.py` — add fusion model loading + inference, replaces both `compute_dynamic_score()` (recommendations) and `apply_llm_scoring()` (swaps) paths
- `mtg_synergy/config.py` — add `USE_FUSION_MODEL = True`, `FUSION` weight (10.0)
- `optimize_weights.py` — add `--fusion` evaluation mode

### Unchanged
- `synergy_graph.py`, `cli.py` — call `recommend_cards()` internally
- `compare_edhrec.py` — already uses recommendation pipeline
- All Forge/causal/tag code — untouched, features read at inference time

### Fallback behavior
- If `data/fusion_model.lgb` or `data/tower_model_edhrec.npz` missing/corrupt: fall back to current causal+tags pipeline (same pattern as existing `_load_tower_model()` which returns `None` on failure)
- If `lightgbm` not installed: skip fusion scoring, use causal fallback

### New dependency
- `lightgbm` (pip install, no GPU, ~2MB)

### Feature matrix construction time budget
- 871 CausalContext instantiations × ~0.1s each = ~87s
- Tag/Forge/embedding lookups: ~30s (bulk-loaded)
- Total feature matrix build: ~2 min (within <5 min budget with training)

## Retrain Pipeline (new set update)

```bash
python3 fetch_edhrec_decks.py --refresh        # Refresh EDHREC avg decklists + synergy (if new set)
python3 import_forge.py --download --import    # Update Forge data
python3 build_graph.py --rebuild               # Rebuild causal graph
python3 train_fusion_model.py                  # Retrain both stages (<5 min)
# All 3438 commanders scored in <10 seconds
```

## Success Criteria

| Metric | Floor (causal) | Ceiling (LLM) | Target |
|--------|---------------|---------------|--------|
| Synergy Recall@100 | 64.2% | 72.5% | >70% |
| Avg Deck Recall@100 | 53.5% | 57.1% | >56% |
| Synergy Recall@30 | 25.3% | ~35% | >33% |
| Training time | N/A | N/A | <5 min |
| Inference time | N/A | N/A | <1ms/card |
| Coverage | 100% | 4% | 100% |
| Cost per set update | $0 | $0.50/cmdr | $0 |

## Prerequisite Data (verified available)

- 871 EDHREC commanders: synergy scores (230k pairs) + average decklists
- Forge: 59k abilities, 14k DeckHas/DeckHints tags
- Forge causal graph: 9.2M IDF-weighted edges
- Card embeddings: 768-dim for 34k cards (data/embeddings.npy)
- Commander profiles: 3,438 with auto-detected strategies

## Implementation Tasks

1. Retrain tower on EDHREC membership (binary cross-entropy, sigmoid output)
2. Build 10-feature matrix for 871 commanders (positive + negative sampling)
3. Train LightGBM with 5-fold leave-commander-out CV
4. Wire fusion model into scoring.py as primary signal (FUSION weight=10.0)
5. Add `--fusion` evaluation mode to optimize_weights.py
6. Evaluate on dual metrics (synergy recall + avg deck recall)
7. Build retrain pipeline in train_fusion_model.py
