# Remove Baseline/Tower, Forge-Only Default

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the dead baseline/tower MLP code path, making forge the only recommendation model. `--recommend` defaults to forge. ~1500 lines of dead code removed.

**Architecture:** Bottom-up removal: delete standalone tower files first, then gut scoring.py (remove DeckContext, score_all_candidates, tower_prefilter, _load_tower_model, _load_fusion_model, _get_tower_score, _get_fusion_score), simplify engine.py and swaps.py to forge-only, strip baseline from train_fusion_model.py, clean up config.py, update tests.

**Tech Stack:** Python, SQLite, LightGBM

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `train_tower_model.py` | Delete | Entire file — tower training code |
| `card_embeddings.py` | Delete | Entire file — card2vec embedding code |
| `src/mtg_synergy/recommend/scoring.py` | Gut | Remove DeckContext, score_all_candidates, tower_prefilter, _load_tower_model, _load_fusion_model, _get_tower_score, _get_fusion_score, compute_dynamic_score. Keep only color_identity_filter + score_forge_candidates |
| `src/mtg_synergy/recommend/engine.py` | Simplify | Remove use_forge branching, baseline path. Always forge. |
| `src/mtg_synergy/recommend/swaps.py` | Simplify | Switch from tower_prefilter+DeckContext to forge scoring |
| `src/mtg_synergy/cli.py` | Simplify | `--forge` becomes no-op, `--recommend` always uses forge |
| `src/mtg_synergy/config.py` | Clean | Remove TOWER_*, FUSION_*, USE_FUSION_MODEL, SCORING_WEIGHTS, RECOMMENDATION_WEIGHTS |
| `train_fusion_model.py` | Strip | Remove FEATURE_NAMES, build_feature_matrix, train_gbm, train_tower_binary, train_tower_forge, holdout_evaluation, --tower-only/--features-only/--forge-tower/--holdout-eval/--feature-importance args. Keep forge training. |
| `optimize_weights.py` | Delete | Uses old scoring weights system |
| `tests/test_fusion_model.py` | Rewrite | Remove baseline tests, keep CV split test and forge tests |
| `tests/test_config.py` | Rewrite | Remove tests for deleted config entries |
| `CLAUDE.md` | Update | Remove baseline documentation |

---

### Task 1: Delete standalone tower/embedding files

**Files:**
- Delete: `train_tower_model.py`
- Delete: `card_embeddings.py`
- Delete: `optimize_weights.py`

- [ ] **Step 1: Delete the files**

```bash
git rm train_tower_model.py card_embeddings.py optimize_weights.py
```

- [ ] **Step 2: Verify no imports break**

Run: `uv run python3 -c "from mtg_synergy.recommend.forge_features import ForgeFeatureContext; print('OK')"`

Expected: OK (forge code doesn't import tower code).

- [ ] **Step 3: Commit**

```bash
git commit -m "refactor: delete train_tower_model.py, card_embeddings.py, optimize_weights.py (dead code)"
```

---

### Task 2: Gut scoring.py — remove all baseline/tower code

The bulk of the removal. scoring.py currently has ~780 lines. After this task it should have ~200 (just color_identity_filter + score_forge_candidates).

**Files:**
- Modify: `src/mtg_synergy/recommend/scoring.py`

- [ ] **Step 1: Rewrite scoring.py**

Keep only:
1. The module docstring (updated)
2. `color_identity_filter()` function (lines 418-434)
3. `score_forge_candidates()` function (lines 437-531)

Remove everything else:
- `DeckContext` class (lines 17-123)
- `compute_dynamic_score()` (lines 126-232)
- `score_all_candidates()` (lines 235-415)
- `_tower_cache`, `_load_tower_model()`, `_get_tower_score()` (lines 533-601)
- `_fusion_cache`, `_load_fusion_model()` (lines 604-662)
- `tower_prefilter()` (lines 665-755)
- `_get_fusion_score()` (lines 758-778)

The new scoring.py should be:

```python
"""Forge-only scoring — computes synergy at recommendation time.

Uses forge features (71-dim) with LightGBM LambdaRank.
No tower model, no embeddings, no EDHREC features.
"""
import json
import os

from mtg_synergy.config import DATA_DIR


def color_identity_filter(conn, cmdr_oid: str, color_identity: set,
                          deck_cards: set = None) -> list:
    """Return all color-legal non-token cards as (oracle_id, name) pairs."""
    # [KEEP the existing implementation from lines 418-434 exactly as-is]


def score_forge_candidates(candidate_scores: dict, cards: list, conn,
                           commander: str, deck_cards: set,
                           deck_types: set = None,
                           active_strategies: set = None) -> None:
    """Score candidates with forge-only GBM (71 features, no EDHREC)."""
    # [KEEP the existing implementation from lines 437-531 exactly as-is]
```

Read the current file first to get the exact code for `color_identity_filter` and `score_forge_candidates`, then write the new file with only those two functions. Keep all their internal imports (numpy, lightgbm, etc.) as they are.

Update the import in the `SCORING_WEIGHTS` reference: `score_forge_candidates` currently imports `from mtg_synergy.config import DATA_DIR, SCORING_WEIGHTS`. Change to just `from mtg_synergy.config import DATA_DIR` (SCORING_WEIGHTS will be deleted in Task 5).

- [ ] **Step 2: Verify forge scoring still works**

Run: `uv run python3 -c "from mtg_synergy.recommend.scoring import color_identity_filter, score_forge_candidates; print('OK')"`

Expected: OK

- [ ] **Step 3: Verify old imports fail**

Run: `uv run python3 -c "from mtg_synergy.recommend.scoring import DeckContext" 2>&1 | head -3`

Expected: ImportError (DeckContext no longer exists)

- [ ] **Step 4: Commit**

```bash
git add src/mtg_synergy/recommend/scoring.py
git commit -m "refactor: gut scoring.py — remove DeckContext, tower, baseline scoring (keep forge only)"
```

---

### Task 3: Simplify engine.py — forge-only recommendation

**Files:**
- Modify: `src/mtg_synergy/recommend/engine.py`

- [ ] **Step 1: Remove baseline branching**

The key changes in `recommend_cards()`:
1. Remove `use_forge: bool = False` parameter
2. Remove the import of `DeckContext, score_all_candidates, tower_prefilter`
3. Remove the `else` branch (lines 62-91) that does tower_prefilter
4. Remove the `else` branch (lines 98-103) that does baseline scoring
5. Remove the causal graph enrichment block (lines 106-138) that depends on `ctx` from DeckContext
6. Always use `color_identity_filter` + `score_forge_candidates`

The import line changes from:
```python
from mtg_synergy.recommend.scoring import (
    DeckContext, score_all_candidates, tower_prefilter,
    color_identity_filter, score_forge_candidates)
```
to:
```python
from mtg_synergy.recommend.scoring import (
    color_identity_filter, score_forge_candidates)
```

The body simplifies: remove `if use_forge:` / `else:` branching — the forge path is now the only path. Remove `ctx = ctx if not use_forge else None` and the entire causal graph enrichment block below it (lines 106-138).

Also remove `tower_str` and `edhrec_str` from the output formatting (lines 227-228, 231) since those features no longer exist.

- [ ] **Step 2: Verify the function still works**

Run: `uv run python3 -c "from mtg_synergy.recommend.engine import recommend_cards; print('OK')"`

Expected: OK

- [ ] **Step 3: Commit**

```bash
git add src/mtg_synergy/recommend/engine.py
git commit -m "refactor: simplify engine.py — forge-only recommendation, no tower/baseline"
```

---

### Task 4: Simplify swaps.py — use forge scoring

**Files:**
- Modify: `src/mtg_synergy/recommend/swaps.py`

- [ ] **Step 1: Replace tower_prefilter + DeckContext with forge scoring**

Change the import from:
```python
from mtg_synergy.recommend.scoring import (
    DeckContext, score_all_candidates, compute_dynamic_score, tower_prefilter)
```
to:
```python
from mtg_synergy.recommend.scoring import (
    color_identity_filter, score_forge_candidates)
```

In `suggest_swaps()` (around lines 114-146), replace the tower_prefilter + DeckContext + score_all_candidates block with forge-only scoring:

```python
    # Get swap-in candidates via color-identity filter
    ci_results = color_identity_filter(
        conn, cmdr_oid, color_identity or set(), deck_cards=deck_cards)

    cand_scores = {}
    for oid, name in ci_results:
        if name not in deck_cards:
            cand_scores[name] = {
                "total": 0.0, "partners": [], "multi_sig": 0,
                "commander_synergy": 0.0, "key_synergy": 0.0,
            }

    # Score candidates with forge model
    score_forge_candidates(cand_scores, cards, conn, commander, deck_cards,
                           deck_types=deck_types, active_strategies=active_strategies)
```

For deck card scoring (lines 135-146), the old code uses `compute_dynamic_score()` (baseline). Replace with forge scoring: score each deck card the same way. Create a temporary dict for each deck card, run `score_forge_candidates` on it, extract the total. Or simpler: compute features directly.

Actually, the simplest approach: score deck cards individually using the forge feature pipeline. Build a single candidate dict with all deck cards, run `score_forge_candidates` on it:

```python
    # Score deck cards on the same scale
    deck_card_scores = {}
    for card_name in deck_cards:
        if card_name != commander:
            deck_card_scores[card_name] = {
                "total": 0.0, "partners": [], "multi_sig": 0,
                "commander_synergy": 0.0, "key_synergy": 0.0,
            }

    if deck_card_scores:
        score_forge_candidates(deck_card_scores, cards, conn, commander, deck_cards,
                               deck_types=deck_types, active_strategies=active_strategies)

    deck_scores = {}
    for card_name in deck_cards:
        if card_name == commander:
            deck_scores[card_name] = {"total": 999.0, "partners": 0}
        elif card_name in deck_card_scores:
            deck_scores[card_name] = {"total": deck_card_scores[card_name]["total"], "partners": 0}
        else:
            deck_scores[card_name] = {"total": 0.0, "partners": 0}
```

Also remove the `edhrec_slug` parameter from `suggest_swaps()` since it was only used for `DeckContext`.

- [ ] **Step 2: Verify imports work**

Run: `uv run python3 -c "from mtg_synergy.recommend.swaps import suggest_swaps; print('OK')"`

Expected: OK

- [ ] **Step 3: Commit**

```bash
git add src/mtg_synergy/recommend/swaps.py
git commit -m "refactor: simplify swaps.py — use forge scoring instead of tower+DeckContext"
```

---

### Task 5: Clean config.py — remove tower/baseline entries

**Files:**
- Modify: `src/mtg_synergy/config.py`

- [ ] **Step 1: Remove dead config entries**

Remove these lines/blocks:
- `EMBEDDINGS_NPY` (line 13)
- `EMBEDDINGS_INDEX` (line 14)
- `TOWER_MODEL_PATH` (line 15)
- `USE_FUSION_MODEL` (line 18)
- `TOWER_EDHREC_PATH` (line 19)
- `FUSION_MODEL_PATH` (line 20)
- `SCORING_WEIGHTS` dict (lines 22-39)
- `RECOMMENDATION_WEIGHTS` dict (lines 41-46)
- `GRAPH` dict (lines 48-55) — only used by old graph scoring
- `MECHANICS` dict (lines 57-61) — only used by old scoring

The resulting config.py should have:
```python
"""Centralized paths, thresholds, and configuration constants."""
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "tags.db"
CARDS_JSON = DATA_DIR / "oracle_cards.json"

# ── Swap suggestion thresholds ────────────────────────────────────────
SWAP = {
    "MIN_MECHANICS_PROTECTION": 2.0,
    "TRIBAL_THRESHOLD": 0.15,
}

# ── DB connection settings ────────────────────────────────────────────
DB_PRAGMAS = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
}
```

- [ ] **Step 2: Fix any imports of removed config entries**

Check for imports of removed entries:

```bash
uv run python3 -c "from mtg_synergy.config import DATA_DIR, DB_PATH, CARDS_JSON, SWAP, DB_PRAGMAS; print('OK')"
```

Also verify scoring.py doesn't import SCORING_WEIGHTS (should have been fixed in Task 2):

```bash
uv run python3 -c "from mtg_synergy.recommend.scoring import score_forge_candidates; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add src/mtg_synergy/config.py
git commit -m "refactor: clean config.py — remove tower/baseline/scoring weight entries"
```

---

### Task 6: Simplify CLI — forge is default

**Files:**
- Modify: `src/mtg_synergy/cli.py`

- [ ] **Step 1: Make --forge a no-op**

Keep `--forge` arg for backward compatibility but don't pass it to `recommend_cards()` (which no longer accepts `use_forge`):

Change line 38-39:
```python
    parser.add_argument("--forge", action="store_true",
                        help="(deprecated, forge is now the default) Use forge-only model")
```

Remove `use_forge=args.forge` from the `recommend_cards()` call (line 176).

Also update the comment at line 76 from "tower pre-filter handles candidate discovery" to "forge model handles candidate discovery".

- [ ] **Step 2: Verify CLI works**

Run: `uv run python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -5`

Expected: Should show "Using forge-only model" and start recommending.

- [ ] **Step 3: Commit**

```bash
git add src/mtg_synergy/cli.py
git commit -m "refactor: --recommend defaults to forge, --forge is deprecated no-op"
```

---

### Task 7: Strip baseline training from train_fusion_model.py

This is the biggest single change. Remove all baseline/tower training code, keep only forge training.

**Files:**
- Modify: `train_fusion_model.py`

- [ ] **Step 1: Remove baseline-only code**

Remove these items:

1. **Imports from train_tower_model** (lines 22-28): `from train_tower_model import ...` — delete entirely
2. **TOWER_EDHREC_PATH** constant (line 40)
3. **FEATURE_NAMES** list (lines 42-45) — baseline 8-feature list
4. **`build_feature_matrix()`** function (~lines 376-687) — builds baseline features using tower model
5. **`train_gbm()`** function (~lines 937-983) — baseline GBM trainer
6. **`train_tower_binary()`** function (~lines 985-1317) — tower model training
7. **`train_tower_forge()`** function (~lines 1410-1652) — forge tower training
8. **`_load_pairs_for_features()` oid_to_idx parameter** — remove the `oid_to_idx=None` parameter and all embedding-related filtering inside it. The forge path doesn't need it (it passes `oid_to_idx=None` already).
9. **`holdout_evaluation()`** function (~lines 1909-2118) — uses baseline model
10. **CLI args**: Remove `--tower-only`, `--features-only`, `--feature-importance`, `--holdout-eval`, `--drop-feature`, `--forge-tower` from argparse
11. **main() branches**: Remove `args.holdout_eval`, `args.forge_tower`, `args.feature_importance`, `args.features_only`, `args.tower_only`, and the full-pipeline else branch. Keep only the `args.forge_only` branch (make it the default).

The `--forge-only` flag becomes the default behavior (no flag needed), but keep it for backward compat.

The new `main()` should be:

```python
def main():
    parser = argparse.ArgumentParser(description="Train forge LightGBM model")
    parser.add_argument(
        "--forge-only",
        action="store_true",
        default=True,
        help="(default) Train forge-only GBM on EDHREC labels with forge features",
    )
    parser.add_argument(
        "--rebuild-features",
        action="store_true",
        help="Force rebuild feature matrix (ignore cache)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("FORGE-ONLY MODEL — EDHREC labels, forge features")
    print("=" * 60)

    cache_path = os.path.join(DATA_DIR, "forge_features_cache.npz")
    if os.path.exists(cache_path) and not args.rebuild_features:
        print(f"\nLoading cached feature matrix from {cache_path}")
        cached = np.load(cache_path)
        X_forge = cached["X"]
        y_forge = cached["y"]
        cmdr_ids_forge = cached["cmdr_ids"]
        print(f"  Matrix: {X_forge.shape}, positives: {int((y_forge > 0).sum())}, "
              f"negatives: {int((y_forge == 0).sum())}")
    else:
        conn = sqlite3.connect(DB_PATH)
        pairs_by_cmdr = _load_pairs_for_features(conn)
        conn.close()

        print("\n--- Building FORGE-ONLY feature matrix ---")
        X_forge, y_forge, cmdr_ids_forge = build_forge_feature_matrix(pairs_by_cmdr)

        np.savez(cache_path, X=X_forge, y=y_forge, cmdr_ids=cmdr_ids_forge)
        print(f"  Feature matrix cached to {cache_path}")

    print("\n" + "=" * 60)
    print("Training FORGE-ONLY model (EDHREC labels, forge features)")
    print("=" * 60)
    _, forge_scores = train_forge_gbm(X_forge, y_forge, cmdr_ids_forge)

    if "mean_ndcg30" in forge_scores:
        print(f"\n  Forge-only NDCG@30: {forge_scores['mean_ndcg30']:.4f}")

    import lightgbm as lgb
    forge_model_path = os.path.join(DATA_DIR, "fusion_model_forge.lgb")
    try:
        booster = lgb.Booster(model_file=forge_model_path)
        importances = booster.feature_importance(importance_type="split")
        names = booster.feature_name()
    except Exception:
        import joblib
        m = joblib.load(forge_model_path)
        importances = m.feature_importances_
        names = FORGE_FEATURE_NAMES
    print(f"\n  Forge model feature importance:")
    total_imp = sum(importances)
    for name, imp in sorted(zip(names, importances), key=lambda x: -x[1]):
        print(f"    {name:25s} {imp:6d} ({imp/total_imp*100:5.1f}%)")
```

Also clean up `_load_pairs_for_features()`: remove the `oid_to_idx=None` parameter and the embedding filtering block that uses it (it's always None in the forge path).

- [ ] **Step 2: Verify forge training still works**

Run: `uv run python3 train_fusion_model.py --forge-only 2>&1 | head -10`

Expected: Loads cached features, trains successfully.

- [ ] **Step 3: Verify default (no args) also works**

Run: `uv run python3 train_fusion_model.py 2>&1 | head -10`

Expected: Same as --forge-only (it's now the default).

- [ ] **Step 4: Commit**

```bash
git add train_fusion_model.py
git commit -m "refactor: strip baseline training from train_fusion_model.py (forge-only)"
```

---

### Task 8: Update tests

**Files:**
- Modify: `tests/test_fusion_model.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Rewrite test_fusion_model.py**

Remove:
- `test_tower_binary_output_range` (tower model test)
- `test_feature_names` (baseline 8-feature test)
- `test_load_fusion_model_returns_none_when_missing` (baseline fusion model)
- `test_load_fusion_model_returns_dict_when_present` (baseline fusion model)

Keep:
- `test_load_edhrec_membership_data` (EDHREC data is still used for labels)
- `test_edhrec_commander_count` (same)
- `test_leave_commander_out_split` (shared utility)

Add a forge feature name test:
```python
def test_forge_feature_names():
    """Forge feature list should have exactly 71 named features."""
    from train_fusion_model import FORGE_FEATURE_NAMES
    assert len(FORGE_FEATURE_NAMES) == 71
    assert "causal_cmdr_to_card" in FORGE_FEATURE_NAMES
    assert "ability_density" in FORGE_FEATURE_NAMES
    assert "tower_prob" not in FORGE_FEATURE_NAMES
```

- [ ] **Step 2: Rewrite test_config.py**

```python
def test_config_paths_exist():
    from mtg_synergy.config import PROJECT_ROOT, DATA_DIR, DB_PATH
    assert PROJECT_ROOT.is_dir()
    assert DATA_DIR.is_dir()


def test_swap_config():
    from mtg_synergy.config import SWAP
    assert "MIN_MECHANICS_PROTECTION" in SWAP
    assert "TRIBAL_THRESHOLD" in SWAP
```

- [ ] **Step 3: Run all tests**

Run: `uv run pytest tests/ -v 2>&1 | tail -30`

Expected: All tests pass (except the 6 pre-existing causal integration failures).

- [ ] **Step 4: Commit**

```bash
git add tests/test_fusion_model.py tests/test_config.py
git commit -m "test: update tests for forge-only (remove baseline/tower tests)"
```

---

### Task 9: Delete baseline model files + cleanup

**Files:**
- Delete: `data/tower_model*.npz`, `data/fusion_model.lgb`, `data/*.bak`
- Modify: `.gitignore` (if tower files are tracked)

- [ ] **Step 1: Check what's tracked**

```bash
git ls-files data/tower_model* data/fusion_model.lgb data/*.bak
```

If any are tracked, `git rm` them. If gitignored, just delete.

- [ ] **Step 2: Delete model files**

```bash
rm -f data/tower_model.npz data/tower_model_edhrec.npz data/tower_model_forge.npz data/tower_model_llm_backup.npz
rm -f data/fusion_model.lgb data/fusion_model.lgb.bak data/fusion_model_forge.lgb.bak
rm -f data/card2vec_embeddings.npy data/card2vec_index.json
```

Keep `data/fusion_model_forge.lgb` (the active forge model).

- [ ] **Step 3: Verify recommend still works**

Run: `uv run python3 synergy_graph.py --deck krenko --recommend 2>&1 | head -10`

Expected: Works, no file-not-found errors.

- [ ] **Step 4: Commit if any tracked files were removed**

```bash
git add -A data/
git commit -m "chore: delete baseline model files (tower, fusion_model.lgb, embeddings)"
```

---

### Task 10: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Simplify signal architecture**

Remove the entire BASELINE section from the Signal Architecture. The forge-only section becomes the only mode. Remove `--forge` from command examples (it's now default).

Key changes:
- Remove "Two modes available" framing — just describe the one mode
- Remove BASELINE section entirely
- `--recommend` examples no longer need `--forge`
- Remove references to tower model, embeddings, EDHREC features in GBM
- Remove tower training commands (`--tower-only`, `--forge-tower`)
- Update Key Files table: remove train_tower_model.py, card_embeddings.py, optimize_weights.py entries
- Update Fusion Models section: remove baseline model description
- Remove "Both towers share architecture" line
- Update command examples

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md — remove baseline/tower documentation, forge-only"
```
