# Local Synergy Model — Design Spec (Next Session)

**Date**: 2026-03-25
**Goal**: Replace LLM scoring with a local model that scores any commander × card pair instantly, retrains automatically when Forge updates with new sets. Zero ongoing cost.

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

## Architecture

Train a model that takes (commander_features, card_features) → synergy_score.

### Training Data
- **Positive labels**: EDHREC average deck cards (829 commanders × ~75 cards = 62k positive pairs)
- **Negative labels**: Random cards NOT in the average deck (sample 3:1 negative:positive)
- **Features per card**: Forge verb/trigger profile, DeckHas/DeckHints tags, embeddings, type line, subtypes, keywords
- **Features per commander**: Same as card features + commander profile (strategies, tribal type, events produced/consumed)

### Model Options

**Option A: Gradient Boosting (XGBoost/LightGBM)**
- Input: handcrafted features (tag overlap, Forge verb compatibility, tribal match, etc.)
- Fast to train (<1 min), interpretable, easy to retrain
- Pro: works well with sparse features, no GPU needed
- Con: limited by feature engineering quality

**Option B: Neural Tower Model (improved)**
- Input: embeddings (768-dim) + structural features
- Current tower architecture but trained on EDHREC avg deck membership instead of causal scores
- Pro: learns from raw embeddings, less feature engineering
- Con: narrow score distribution issue (current model outputs 3.0-5.3)

**Option C: Hybrid — Gradient Boosting on Neural Features**
- Tower model produces an embedding-based score
- Gradient boosting combines tower score + all 12 handcrafted features
- Best of both: neural generalization + feature engineering precision

### Recommended: Option C (Hybrid)

1. Retrain tower on EDHREC avg deck membership (binary: in deck or not)
2. Tower outputs a probability (0-1) — fixes the narrow distribution issue
3. Gradient boosting takes: tower_prob + causal + tag_overlap + Forge_deck + mechanics + ... → final score
4. Train on 829 commanders, validate with cross-validation (leave commanders out)

### Retrain Pipeline

```
New set released
  → Forge community updates card scripts (~2 weeks)
  → python3 import_forge.py --download && --import
  → python3 build_graph.py --forge
  → python3 train_tower_model.py --forge-causal
  → python3 train_fusion_model.py
  → All commanders scored in <10 seconds
```

### Success Criteria

| Metric | Current (causal) | Target |
|--------|-----------------|--------|
| Synergy Recall@100 | 64.2% | >70% |
| Avg Deck Recall@100 | 53.5% | >56% |
| Synergy Recall@30 | 25.3% | >33% |
| Training time | N/A | <5 min |
| Inference time | <1ms/card | <1ms/card |
| Coverage | 100% | 100% |
| Cost per set update | $0 | $0 |

### Evaluation

- 829 commanders with both EDHREC synergy scores and average decklists
- Dual metric: Average Deck Recall + Synergy Recall
- Cross-validation: train on 80% of commanders, test on 20%
- Compare against: LLM-only (72.5% synergy ceiling), causal-only (64.2% floor)

## Prerequisite Data (already available)

- 871 EDHREC commanders: synergy scores (231k pairs) + average decklists (66k cards)
- Forge: 32k cards with structured effects, 12.7k DeckHas/DeckHints tags
- Forge causal graph: 9.2M edges, 135 trigger modes
- Card embeddings: 768-dim for 34k cards
- Commander profiles: 3,438 with auto-detected strategies
- Full 12-feature evaluator in optimize_weights.py

## Implementation Tasks (estimated)

1. Retrain tower on EDHREC membership (binary classification)
2. Build gradient boosting feature matrix (829 cmdr × ~250 cards)
3. Train + cross-validate fusion model
4. Wire into scoring.py as new primary signal
5. Build retrain pipeline script
6. Evaluate on dual metrics
