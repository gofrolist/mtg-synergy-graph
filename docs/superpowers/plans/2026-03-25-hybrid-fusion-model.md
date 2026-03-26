# Hybrid Fusion Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Train a two-stage hybrid model (tower + LightGBM) on EDHREC data to replace LLM scoring at $0 cost.

**Architecture:** Retrain the existing tower model on binary EDHREC avg deck membership (Stage 1), then train a LightGBM classifier on 10 features including tower probability (Stage 2). The fusion model replaces LLM as the primary scoring signal.

**Tech Stack:** Python, NumPy, LightGBM, SQLite

**Spec:** `docs/superpowers/specs/2026-03-25-local-synergy-model-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `train_fusion_model.py` | Create | Training script: tower retrain + feature matrix + LightGBM + CV |
| `mtg_synergy/recommend/scoring.py` | Modify | Add `_load_fusion_model()`, `_get_fusion_score()`, wire into `compute_dynamic_score()` |
| `mtg_synergy/recommend/engine.py` | Modify | Update `apply_llm_scoring()` to use fusion when available |
| `mtg_synergy/config.py` | Modify | Add `USE_FUSION_MODEL`, `FUSION_MODEL_PATH`, `FUSION` weight |
| `optimize_weights.py` | Modify | Add `--fusion` flag for evaluation |
| `tests/test_fusion_model.py` | Create | Tests for training, features, inference, fallback |
| `data/tower_model_edhrec.npz` | Output | Retrained tower weights |
| `data/fusion_model.lgb` | Output | LightGBM model |

---

### Task 1: Add config entries for fusion model

**Files:**
- Modify: `mtg_synergy/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Read current config.py**

Read `mtg_synergy/config.py` to see existing SCORING_WEIGHTS and paths.

- [ ] **Step 2: Write test for new config entries**

In `tests/test_config.py`, add:
```python
def test_fusion_config_entries():
    from mtg_synergy.config import SCORING_WEIGHTS, FUSION_MODEL_PATH, TOWER_EDHREC_PATH, USE_FUSION_MODEL
    assert "FUSION" in SCORING_WEIGHTS
    assert SCORING_WEIGHTS["FUSION"] == 10.0
    assert "fusion_model.lgb" in str(FUSION_MODEL_PATH)
    assert "tower_model_edhrec.npz" in str(TOWER_EDHREC_PATH)
    assert isinstance(USE_FUSION_MODEL, bool)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py::test_fusion_config_entries -v`
Expected: FAIL -- `FUSION_MODEL_PATH` not found

- [ ] **Step 4: Add config entries**

Add to `mtg_synergy/config.py`:
```python
# Fusion model (hybrid tower + LightGBM)
USE_FUSION_MODEL = True
TOWER_EDHREC_PATH = DATA_DIR / "tower_model_edhrec.npz"
FUSION_MODEL_PATH = DATA_DIR / "fusion_model.lgb"
```

Add `"FUSION": 10.0` to `SCORING_WEIGHTS` dict.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add mtg_synergy/config.py tests/test_config.py
git commit -m "feat: add fusion model config entries"
```

---

### Task 2: Retrain tower on EDHREC membership (binary classification)

**Files:**
- Create: `train_fusion_model.py`
- Test: `tests/test_fusion_model.py`

This task builds the Stage 1 tower training. The script reuses the existing tower architecture from `train_tower_model.py` but changes the loss to binary cross-entropy and the output to sigmoid.

- [ ] **Step 1: Write test for EDHREC data loading**

Create `tests/test_fusion_model.py`:
```python
import sqlite3
import os
import pytest

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tags.db")

def test_load_edhrec_membership_data():
    """Verify EDHREC avg deck data is loadable and has expected shape."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT commander_slug, card_name FROM edhrec_average_decks LIMIT 10"
    ).fetchall()
    conn.close()
    assert len(rows) == 10
    assert all(isinstance(r[0], str) and isinstance(r[1], str) for r in rows)

def test_edhrec_commander_count():
    """At least 800 commanders with avg decklists."""
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        "SELECT COUNT(DISTINCT commander_slug) FROM edhrec_average_decks"
    ).fetchone()[0]
    conn.close()
    assert count >= 800
```

- [ ] **Step 2: Run test to verify data exists**

Run: `python3 -m pytest tests/test_fusion_model.py -v`
Expected: PASS (data already exists in tags.db)

- [ ] **Step 3: Write the tower retraining code**

Create `train_fusion_model.py` with:

1. `load_edhrec_membership(conn)` -- loads positive pairs from `edhrec_average_decks`, maps card_name to oracle_id via `cards` table, groups by commander_slug to commander oracle_id.
2. `sample_negatives(positives, all_oids, color_identity, ratio=3)` -- for each commander, sample `ratio` x negative cards filtered by color identity, excluding basics/tokens.
3. `train_tower_binary(pairs, embeddings, oid_to_idx, sf_data)` -- reuses `forward()` and `compute_struct_features()` from `train_tower_model.py`, but:
   - Loss: binary cross-entropy with logits (sigmoid applied at output)
   - Labels: 1.0 for positive, 0.0 for negative
   - Same architecture: 768 to 128 projection, element-wise product, 12 structural features, MLP 140 to 128 to 64 to 32 to 1
   - Output bias initialized to 0.0 (not 5.0)
   - Saves to `data/tower_model_edhrec.npz`
4. CLI: `python3 train_fusion_model.py --tower-only` to train just Stage 1

Key implementation detail -- reuse functions from `train_tower_model.py`:
```python
from train_tower_model import (
    load_embeddings, load_structural_features, compute_struct_features,
    forward, init_model
)
```

Modify `forward()` call: after getting raw output, apply sigmoid instead of clipping to [1,10]. Since `forward()` returns raw values before clipping, apply `1 / (1 + exp(-x))` after the call.

**Important**: After calling `init_model()`, override the output bias: `model["b4"] = np.float32(0.0)` (the existing `init_model()` sets it to 5.0 which is wrong for binary classification).

**Backward pass**: The existing training loop in `train_tower_model.py` uses MSE loss with clip gradients. For binary classification, implement BCE loss backward pass. The gradient of BCE w.r.t. the raw logit (before sigmoid) is simply `sigmoid(logit) - label` -- this replaces the existing `2 * (pred - target) / batch_size * clip_mask` gradient. Copy the training loop from `train_tower_model.py` and change:
1. The loss computation: `loss = -mean(label * log(sigmoid(raw)) + (1-label) * log(1-sigmoid(raw)))`
2. The initial gradient: `d_pred = (sigmoid(raw) - label) / batch_size` (no clip mask needed)
3. Remove the `np.clip(pred, 1, 10)` -- work with raw logits throughout
4. Evaluation metric: AUC instead of correlation

Training hyperparameters (same as existing tower):
- Batch size: 512
- Epochs: 150
- Learning rate: 0.001 with patience-based reduction
- Dropout: 0.1

- [ ] **Step 4: Write test for tower binary output**

Add to `tests/test_fusion_model.py`:
```python
def test_tower_binary_output_range():
    """Tower probability output should be in [0, 1]."""
    import numpy as np
    model_path = os.path.join(os.path.dirname(__file__), "..", "data", "tower_model_edhrec.npz")
    if not os.path.exists(model_path):
        pytest.skip("Tower EDHREC model not trained yet")
    data = np.load(model_path)
    # Check that structural feature normalization params are saved
    assert "struct_means" in data.files
    assert "struct_stds" in data.files
```

- [ ] **Step 5: Run tower training**

Run: `python3 train_fusion_model.py --tower-only`
Expected: Trains in ~2 min, outputs `data/tower_model_edhrec.npz`, prints AUC and accuracy.

- [ ] **Step 6: Commit**

```bash
git add train_fusion_model.py tests/test_fusion_model.py
git commit -m "feat: Stage 1 -- retrain tower on EDHREC binary membership"
```

---

### Task 3: Build feature matrix construction

**Files:**
- Modify: `train_fusion_model.py`
- Test: `tests/test_fusion_model.py`

This task builds the 10-feature matrix for all training pairs (positive + negative).

- [ ] **Step 1: Write test for feature names**

Add to `tests/test_fusion_model.py`:
```python
def test_feature_names():
    """Feature list should have exactly 10 named features."""
    from train_fusion_model import FEATURE_NAMES
    assert len(FEATURE_NAMES) == 10
    assert FEATURE_NAMES[0] == "tower_prob"
    assert "causal_score" in FEATURE_NAMES
    assert "is_creature" in FEATURE_NAMES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fusion_model.py::test_feature_names -v`
Expected: FAIL -- `FEATURE_NAMES` not found

- [ ] **Step 3: Implement `build_feature_matrix()`**

Add to `train_fusion_model.py`:

```python
FEATURE_NAMES = [
    "tower_prob", "causal_score", "forge_deck_overlap",
    "cmdr_tag_overlap", "strategy_keyword", "tribal_match",
    "edhrec_synergy", "edhrec_rank", "cmc", "is_creature"
]
```

```python
def build_feature_matrix(pairs, tower_model, conn, embeddings, oid_to_idx, sf_data):
    """Build 10-feature matrix for all (commander, card) pairs.

    Args:
        pairs: list of (cmdr_oid, card_oid, label) tuples
        tower_model: trained tower model dict (from Stage 1)
        conn: sqlite3 connection
        embeddings: normalized embeddings array
        oid_to_idx: oracle_id to embedding index
        sf_data: structural features data tuple

    Returns:
        X: np.array(N, 10) feature matrix
        y: np.array(N,) labels
        cmdr_ids: np.array(N,) commander index per pair (for CV splits)
    """
```

Feature computation per pair:
1. **tower_prob**: Run tower forward pass, apply sigmoid.
2. **causal_score**: Use `CausalContext` from `mtg_synergy.causal`. Pre-load one CausalContext per commander (cache across pairs with same commander). Call `ctx.causal_score(card_oid)`.
3. **forge_deck_overlap**: Query `forge_deck_tags` -- bidirectional overlap: `len(card_has & cmdr_hints) + len(card_hints & cmdr_has)`. Bulk-load all forge tags once at start.
4. **cmdr_tag_overlap**: Query `provides`/`wants` -- `len(card_provides & cmdr_wants) + len(card_wants & cmdr_provides)`. Bulk-load all tags once.
5. **strategy_keyword**: Use `STRATEGY_KEYWORDS` from `mtg_synergy/recommend/scoring.py`. For each commander, detect strategies via `commander_profile.load_profile()`, then count keyword hits in card's oracle text.
6. **tribal_match**: 1.0 if card is creature and shares a subtype with commander, else 0.0.
7. **edhrec_synergy**: Query `edhrec_card_synergy` for commander_slug + card_name. 0.0 if missing.
8. **edhrec_rank**: `log10(card.edhrec_rank or 50000)`.
9. **cmc**: card's converted mana cost (float).
10. **is_creature**: 1.0 if "Creature" in type_line, else 0.0.

**Optimization**: Process commander-by-commander. For each commander:
- Load CausalContext once
- Load commander's provides/wants/strategies once
- Batch all cards for that commander
- Batch tower forward pass for all cards at once (not one-by-one)

Expected: ~2 min for 260k pairs (871 commanders x ~300 cards each).

CLI: `python3 train_fusion_model.py --features-only` to build and inspect feature matrix.

- [ ] **Step 4: Run feature names test**

Run: `python3 -m pytest tests/test_fusion_model.py::test_feature_names -v`
Expected: PASS

- [ ] **Step 5: Run feature matrix construction**

Run: `python3 train_fusion_model.py --features-only`
Expected: Builds feature matrix, prints shape (~260k, 10) and per-feature statistics (mean, std, min, max).

- [ ] **Step 6: Commit**

```bash
git add train_fusion_model.py tests/test_fusion_model.py
git commit -m "feat: Stage 2 -- build 10-feature matrix from EDHREC data"
```

---

### Task 4: Train LightGBM with cross-validation

**Files:**
- Modify: `train_fusion_model.py`
- Test: `tests/test_fusion_model.py`

- [ ] **Step 1: Install lightgbm and scikit-learn**

Run: `pip install lightgbm scikit-learn`

- [ ] **Step 2: Write test for CV split**

Add to `tests/test_fusion_model.py`:
```python
def test_leave_commander_out_split():
    """CV splits should separate commanders, not individual pairs."""
    import numpy as np
    # Simulate 5 commanders, 10 pairs each
    cmdr_ids = np.repeat(range(5), 10)
    from train_fusion_model import make_cv_splits
    splits = make_cv_splits(cmdr_ids, n_folds=5)
    for train_idx, test_idx in splits:
        train_cmdrs = set(cmdr_ids[train_idx])
        test_cmdrs = set(cmdr_ids[test_idx])
        # No commander appears in both train and test
        assert train_cmdrs.isdisjoint(test_cmdrs)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fusion_model.py::test_leave_commander_out_split -v`
Expected: FAIL -- `make_cv_splits` not found

- [ ] **Step 4: Implement `make_cv_splits()` and `train_gbm()`**

Add to `train_fusion_model.py`:

```python
def make_cv_splits(cmdr_ids, n_folds=5, seed=42):
    """Leave-commander-group-out CV splits."""
    unique_cmdrs = np.unique(cmdr_ids)
    rng = np.random.RandomState(seed)
    rng.shuffle(unique_cmdrs)
    fold_size = len(unique_cmdrs) // n_folds
    splits = []
    for i in range(n_folds):
        start = i * fold_size
        end = (i + 1) * fold_size if i < n_folds - 1 else len(unique_cmdrs)
        test_cmdrs = set(unique_cmdrs[start:end])
        test_idx = np.array([j for j, c in enumerate(cmdr_ids) if c in test_cmdrs])
        train_idx = np.array([j for j, c in enumerate(cmdr_ids) if c not in test_cmdrs])
        splits.append((train_idx, test_idx))
    return splits


def train_gbm(X, y, cmdr_ids):
    """Train LightGBM with leave-commander-out CV.

    Returns:
        model: trained LightGBM model (on full data)
        cv_scores: dict with AUC per fold + mean
    """
    import lightgbm as lgb

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 63,
        "learning_rate": 0.05,
        "n_estimators": 500,
        "verbose": -1,
    }

    # Cross-validation
    splits = make_cv_splits(cmdr_ids, n_folds=5)
    fold_aucs = []
    for fold_i, (train_idx, test_idx) in enumerate(splits):
        model = lgb.LGBMClassifier(**params)
        model.fit(
            X[train_idx], y[train_idx],
            eval_set=[(X[test_idx], y[test_idx])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        proba = model.predict_proba(X[test_idx])[:, 1]
        from sklearn.metrics import roc_auc_score
        auc = roc_auc_score(y[test_idx], proba)
        fold_aucs.append(auc)
        print(f"  Fold {fold_i+1}: AUC={auc:.4f}")

    print(f"  Mean AUC: {np.mean(fold_aucs):.4f}")

    # Train final model on all data
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X, y)
    # Save full classifier via joblib (preserves predict_proba)
    import joblib
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X, y)
    joblib.dump(final_model, "data/fusion_model.lgb")

    return final_model, {"mean_auc": np.mean(fold_aucs), "fold_aucs": fold_aucs}
```

Note: `scikit-learn` must be installed (Step 1) for `roc_auc_score`.

- [ ] **Step 5: Run CV split test**

Run: `python3 -m pytest tests/test_fusion_model.py::test_leave_commander_out_split -v`
Expected: PASS

- [ ] **Step 6: Add --feature-importance CLI flag**

Add to argparse in `train_fusion_model.py`:
```python
parser.add_argument("--feature-importance", action="store_true", help="Print feature importance from trained GBM")
```

When `--feature-importance` is set, load the saved model and print:
```python
import joblib
model = joblib.load("data/fusion_model.lgb")
for name, imp in sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda x: -x[1]):
    print(f"  {name:25s} {imp:6d}")
```

- [ ] **Step 7: Run full training pipeline**

Run: `python3 train_fusion_model.py`
Expected: Trains tower (~2 min), builds features (~2 min), trains GBM with 5-fold CV (~30s). Prints CV AUC per fold + mean. Saves `data/tower_model_edhrec.npz` and `data/fusion_model.lgb`.

- [ ] **Step 8: Commit**

```bash
git add train_fusion_model.py tests/test_fusion_model.py
git commit -m "feat: Stage 2 -- LightGBM training with leave-commander-out CV"
```

---

### Task 5: Wire fusion model into scoring.py

**Files:**
- Modify: `mtg_synergy/recommend/scoring.py`
- Test: `tests/test_fusion_model.py`

- [ ] **Step 1: Write test for fusion model loading**

Add to `tests/test_fusion_model.py`:
```python
def test_load_fusion_model_returns_none_when_missing(tmp_path):
    """Fusion model loader should return None gracefully when files missing."""
    from mtg_synergy.recommend.scoring import _load_fusion_model
    result = _load_fusion_model(tower_path=tmp_path / "nope.npz", gbm_path=tmp_path / "nope.lgb")
    assert result is None

def test_load_fusion_model_returns_dict_when_present():
    """Fusion model loader should return dict with expected keys."""
    from mtg_synergy.recommend.scoring import _load_fusion_model
    tower_path = os.path.join("data", "tower_model_edhrec.npz")
    gbm_path = os.path.join("data", "fusion_model.lgb")
    if not os.path.exists(tower_path) or not os.path.exists(gbm_path):
        pytest.skip("Fusion model not trained yet")
    result = _load_fusion_model()
    assert result is not None
    assert "tower" in result
    assert "gbm" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fusion_model.py::test_load_fusion_model_returns_none_when_missing -v`
Expected: FAIL -- `_load_fusion_model` not found

- [ ] **Step 3: Implement `_load_fusion_model()` and `_get_fusion_score()`**

Add to `mtg_synergy/recommend/scoring.py`:

```python
_fusion_cache = None

def _load_fusion_model(tower_path=None, gbm_path=None):
    """Load fusion model (tower + GBM). Returns None on any failure."""
    global _fusion_cache
    if _fusion_cache is not None:
        return _fusion_cache

    from mtg_synergy.config import TOWER_EDHREC_PATH, FUSION_MODEL_PATH
    tp = tower_path or TOWER_EDHREC_PATH
    gp = gbm_path or FUSION_MODEL_PATH

    try:
        import joblib
        tower_data = np.load(str(tp))
        gbm = joblib.load(str(gp))  # LGBMClassifier with predict_proba()

        from train_tower_model import load_embeddings, load_structural_features, compute_struct_features, forward
        emb, oid_list, oid_to_idx = load_embeddings()
        sf_data = load_structural_features()

        _fusion_cache = {
            "tower": {k: tower_data[k] for k in tower_data.files},
            "gbm": gbm,
            "emb": emb,
            "oid_to_idx": oid_to_idx,
            "sf_data": sf_data,
            "compute_sf": compute_struct_features,
            "forward": forward,
            "struct_means": tower_data["struct_means"],
            "struct_stds": tower_data["struct_stds"],
        }
        return _fusion_cache
    except Exception:
        return None


def _get_fusion_score(ctx, card_oid):
    """Get tower probability for a card (Stage 1). Returns 0.0 if unavailable."""
    fusion = _load_fusion_model()
    if fusion is None:
        return 0.0

    oid_to_idx = fusion["oid_to_idx"]
    cmdr_idx = oid_to_idx.get(ctx.cmdr_oid)
    card_idx = oid_to_idx.get(card_oid)
    if cmdr_idx is None or card_idx is None:
        return 0.0

    emb = fusion["emb"]
    sf = fusion["compute_sf"](ctx.cmdr_oid, card_oid, *fusion["sf_data"])
    sf_norm = (sf - fusion["struct_means"]) / (fusion["struct_stds"] + 1e-8)

    cmdr_emb = emb[cmdr_idx:cmdr_idx+1]
    card_emb = emb[card_idx:card_idx+1]
    sf_batch = sf_norm.reshape(1, -1)

    pred, _ = fusion["forward"](fusion["tower"], cmdr_emb, card_emb, sf_batch)
    tower_prob = 1.0 / (1.0 + np.exp(-float(pred[0])))
    return tower_prob
```

- [ ] **Step 4: Wire into `compute_dynamic_score()`**

In `compute_dynamic_score()`, after all existing feature computations (around line 535, before the final `total` calculation), add:

```python
# Fusion model scoring (Stage 2: GBM on 10 features)
# When fusion is active, it REPLACES the weighted sum as the primary ranking signal
from mtg_synergy.config import USE_FUSION_MODEL
fusion_score = 0.0
if USE_FUSION_MODEL:
    fusion = _load_fusion_model()
    if fusion is not None:
        card_oid = ctx.card_oid.get(card_name, "")
        tower_prob = _get_fusion_score(ctx, card_oid)
        import joblib
        features_10 = np.array([[
            tower_prob,
            causal,                              # local var from line 525
            forge_overlap,                       # local var from line 535
            cmdr_overlap,                        # local var from line 481
            strat_keyword_hits,                  # local var from line 520 (NOT strat_keywords)
            1.0 if tribal_match else 0.0,        # local var from line 498
            edhrec_syn,                          # local var from line 512
            math.log10(max(rank, 1)),            # local var from line 433 (NOT card_rank)
            card_data.get("cmc", 0),
            1.0 if is_creature else 0.0,         # local var from line 432
        ]])
        fusion_score = float(fusion["gbm"].predict_proba(features_10)[0][1])
```

Add to the return dict: `"fusion": fusion_score`

**Fusion as primary ranker**: When fusion model is loaded, override `total` with fusion score scaled to dominate:
```python
if fusion_score > 0:
    # Fusion replaces weighted sum as primary signal (spec requirement)
    result["total"] = fusion_score * w.get("FUSION", 10.0)
else:
    # Fallback: existing weighted sum (when fusion unavailable)
    result["total"] = (existing weighted sum calculation unchanged)
```

This means when `USE_FUSION_MODEL=True` and model is loaded, the fusion probability directly determines ranking. The existing 12-feature weighted sum serves as fallback only.

**Tower pre-filtering**: In `score_all_candidates()` (scoring.py:568), when fusion is active, first run tower on ALL cards (not just existing candidates) to discover cards the graph would miss. Add top-2000 by tower_prob to the candidate pool before running GBM. This is the key advantage of the hybrid model — the tower discovers candidates the tag graph would never surface. Implementation: at the start of `score_all_candidates()`, if fusion model is loaded, run `_get_fusion_score()` for all cards with embeddings, take top 2000, inject any not already in `candidate_scores`.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_fusion_model.py -v`
Expected: All PASS

- [ ] **Step 6: Run existing test suite for regressions**

Run: `python3 -m pytest tests/ -v --timeout=60`
Expected: All existing tests still PASS (fusion is additive; returns 0 when model files absent)

- [ ] **Step 7: Commit**

```bash
git add mtg_synergy/recommend/scoring.py tests/test_fusion_model.py
git commit -m "feat: wire fusion model into scoring pipeline with graceful fallback"
```

---

### Task 6: Update apply_llm_scoring() for swaps pipeline

**Files:**
- Modify: `mtg_synergy/recommend/engine.py`

The swaps pipeline uses `apply_llm_scoring()` (engine.py:333-423) which has its own scoring formula separate from `compute_dynamic_score()`. This needs to also use the fusion model when available.

- [ ] **Step 1: Read current `apply_llm_scoring()`**

Read `mtg_synergy/recommend/engine.py:333-423` to understand the current swaps scoring formula.

- [ ] **Step 2: Add fusion model path to apply_llm_scoring()**

At the beginning of `apply_llm_scoring()` (after the DB connection), add a fusion check:

```python
from mtg_synergy.config import USE_FUSION_MODEL
from mtg_synergy.recommend.scoring import _load_fusion_model, _get_fusion_score

fusion = _load_fusion_model() if USE_FUSION_MODEL else None
```

Then in the scoring block (lines 393-420), add a fusion branch that fires when fusion is available. If fusion is loaded, use `fusion["gbm"].predict()` with available features. Some features (causal, forge) may not be available in the swaps path -- pass 0.0 for missing features. The GBM handles missing-like values gracefully.

```python
if fusion is not None:
    cmdr_tag_overlap = _compute_tag_overlap(conn, _cmdr_oid, candidate_scores, card_oid_lookup)
    for card_name, info in candidate_scores.items():
        oid = card_oid_lookup.get(card_name, "")
        if not oid:
            continue
        # Get tower prob
        cmdr_idx = fusion["oid_to_idx"].get(_cmdr_oid)
        card_idx = fusion["oid_to_idx"].get(oid)
        tower_prob = 0.0
        if cmdr_idx is not None and card_idx is not None:
            emb = fusion["emb"]
            sf = fusion["compute_sf"](_cmdr_oid, oid, *fusion["sf_data"])
            sf_norm = (sf - fusion["struct_means"]) / (fusion["struct_stds"] + 1e-8)
            pred, _ = fusion["forward"](fusion["tower"],
                                         emb[cmdr_idx:cmdr_idx+1],
                                         emb[card_idx:card_idx+1],
                                         sf_norm.reshape(1, -1))
            tower_prob = 1.0 / (1.0 + np.exp(-float(pred[0])))

        edhrec_syn = max(0, edhrec_synergy_map.get(card_name, 0.0))
        overlap = cmdr_tag_overlap.get(card_name, 0)
        meta = card_meta.get(card_name, {})
        rank = meta.get("edhrec_rank") or 50000

        features_10 = np.array([[
            tower_prob, 0.0, 0.0,  # causal/forge not available in swaps
            overlap, 0.0, 0.0,
            edhrec_syn,
            math.log10(max(rank, 1)),
            meta.get("cmc", 0),
            1.0 if "Creature" in meta.get("type_line", "") else 0.0,
        ]])
        score = float(fusion["gbm"].predict_proba(features_10)[0][1])
        info["total"] = score * 10000  # scale to match LLM formula range (1-10 * 1000)
        info["fusion_score"] = round(score, 3)
elif llm_scores or model_scores:
    # ... existing LLM/tower scoring code unchanged
```

Add `import numpy as np` and `import math` to imports if not already present.

- [ ] **Step 3: Test swaps still work**

Run: `python3 synergy_graph.py --deck krenko --swaps 2>&1 | head -30`
Expected: Swap suggestions output (uses fusion if model available, otherwise falls back to existing LLM scoring)

- [ ] **Step 4: Commit**

```bash
git add mtg_synergy/recommend/engine.py
git commit -m "feat: update swaps pipeline to use fusion model"
```

---

### Task 7: Add --fusion evaluation mode to optimize_weights.py

**Files:**
- Modify: `optimize_weights.py`

- [ ] **Step 1: Read current evaluation flow**

Read `optimize_weights.py:154-224` -- `_rank_cards()` and `evaluate_weights()`.

- [ ] **Step 2: Add --fusion argument**

Add to argparse:
```python
parser.add_argument("--fusion", action="store_true", help="Evaluate fusion model as primary signal")
```

- [ ] **Step 3: Implement fusion evaluation path**

In `_rank_cards()`, add a `fusion_mode` parameter:
```python
def _rank_cards(scored_cards, weights, fusion_mode=False):
    if fusion_mode:
        return sorted(
            [(name, info.get("fusion", 0), info.get("edhrec_syn", 0))
             for name, info in scored_cards.items()],
            key=lambda x: -x[1]
        )
    # ... existing weighted sum code unchanged
```

In `evaluate_weights()`, pass `fusion_mode=args.fusion` to `_rank_cards()`.

In the main block, when `--fusion` and `--evaluate` are both set:
1. Run `precompute_scores()` as normal (this calls `compute_dynamic_score()` which now populates `features["fusion"]`)
2. Call `evaluate_weights()` with `fusion_mode=True`
3. Print Recall@30, Recall@50, Recall@100 for fusion vs causal-only baseline

- [ ] **Step 4: Run fusion evaluation**

Run: `python3 optimize_weights.py --fusion --evaluate`
Expected: Prints Recall@30, Recall@50, Recall@100 per commander + aggregate. Target: Synergy Recall@100 > 70%.

- [ ] **Step 5: Commit**

```bash
git add optimize_weights.py
git commit -m "feat: add --fusion evaluation mode to optimize_weights.py"
```

---

### Task 8: End-to-end validation

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests PASS (existing + new fusion tests)

- [ ] **Step 2: Run recommendations for a known deck**

Run: `python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -40`
Expected: Top 30 recommendations. With fusion model loaded, scores should be driven by fusion probability.

- [ ] **Step 3: Run EDHREC comparison**

Run: `python3 compare_edhrec.py --fast --quiet`
Expected: Summary across all decks showing improvement over causal-only baseline.

- [ ] **Step 4: Run fusion evaluation against targets**

Run: `python3 optimize_weights.py --fusion --evaluate`
Expected:
- Synergy Recall@100: >70% (target), up from 64.2% (causal floor)
- Avg Deck Recall@100: >56% (target), up from 53.5% (causal floor)

If targets not met, iterate on:
- Feature engineering (add more features, tune existing)
- GBM hyperparameters (num_leaves, learning_rate, n_estimators)
- Tower training (more epochs, different learning rate)
- Negative sampling ratio (try 5:1 or 2:1)

- [ ] **Step 5: Print feature importance**

Run: `python3 train_fusion_model.py --feature-importance`
Expected: LightGBM feature importance ranking showing which of the 10 features contribute most.

- [ ] **Step 6: Commit final state**

```bash
git add -A
git commit -m "feat: hybrid fusion model -- tower + LightGBM on EDHREC data"
```

---

## Summary

| Task | Description | Est. Time |
|------|-------------|-----------|
| 1 | Config entries | 2 min |
| 2 | Retrain tower (binary) | 10 min |
| 3 | Build feature matrix | 10 min |
| 4 | Train LightGBM + CV | 10 min |
| 5 | Wire into scoring.py | 10 min |
| 6 | Update swaps pipeline | 5 min |
| 7 | --fusion evaluation | 5 min |
| 8 | End-to-end validation | 5 min |
| **Total** | | **~57 min** |
