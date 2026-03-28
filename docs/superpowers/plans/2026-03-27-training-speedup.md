# Training Pipeline Speedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `--forge-only --rebuild-features` from ~6 min to ~2 min by speeding up GBM training (76% of time) and eliminating SQL during feature building (22% of time).

**Architecture:** Two independent optimizations: (1) tune GBM hyperparams + use CV-informed round count for final model, (2) expand the in-memory edge index with strength + event data so CmdrFeatureContext never needs SQL queries during training.

**Tech Stack:** Python, NumPy, LightGBM, SQLite

---

## Current Timing Breakdown

| Phase | Time | % |
|---|---|---|
| `_load_pairs_for_features` | 7s | 2% |
| `build_forge_feature_matrix` | 79s | 22% |
| `train_forge_gbm` (5-fold CV + final) | 268s | 76% |
| **Total** | **354s** | |

## Target

| Phase | Before | After |
|---|---|---|
| `build_forge_feature_matrix` | 79s | ~20s |
| `train_forge_gbm` | 268s | ~100s |
| **Total** | **354s** | **~130s** |

---

### Task 1: Speed up GBM training

The GBM training is 76% of total time. Three changes with no quality regression:

1. The final model trains with `num_boost_round=1000` but no early stopping — CV folds early-stop at 319-742 rounds, so ~300-700 rounds are wasted.
2. 5 CV folds to 3 folds (same quality at 372k samples, saves 40% CV time).
3. Learning rate 0.03 to 0.05 with proportional round reduction.

**Files:**
- Modify: `train_fusion_model.py:899-976` (train_forge_gbm function)

- [ ] **Step 1: Change CV to 3 folds, increase learning rate, cap final model rounds**

In `train_fusion_model.py`, modify `train_forge_gbm`:

```python
# Line 899-912: Update params
params = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "eval_at": [10, 30],
    "num_leaves": 255,
    "learning_rate": 0.05,       # was 0.03
    "n_estimators": 1500,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_samples": 20,
    "min_data_in_bin": 5,
    "verbose": -1,
    "label_gain": [0, 1, 2, 3, 5, 8, 12, 18, 25, 35],
}

# Line 914: Change n_folds from 5 to 3
splits = make_cv_splits(cmdr_ids, n_folds=3)
```

After the CV loop (after line 967), compute the average best iteration:

```python
avg_best = int(np.mean([b.best_iteration for b in fold_boosters]))
```

This requires collecting boosters. Change the fold loop to also track best iters:

```python
fold_ndcgs = []
fold_best_iters = []
for fold_i, (train_idx, test_idx) in enumerate(splits):
    # ... existing fold code ...
    fold_best_iters.append(booster.best_iteration)
    # ... existing NDCG computation ...
```

Then change the final model training (lines 969-973):

```python
# Train final model on all data, capped at avg CV best iteration + 10% buffer
avg_best = int(np.mean(fold_best_iters) * 1.1)
print(f"  Final model: {avg_best} rounds (avg CV best x 1.1)")
all_group = _build_group_array(cmdr_ids, np.arange(len(cmdr_ids)))
all_data = lgb.Dataset(X, label=y, group=all_group,
                       feature_name=FORGE_FEATURE_NAMES)
final_booster = lgb.train(params, all_data, num_boost_round=avg_best)
```

- [ ] **Step 2: Verify NDCG is not degraded**

Run: `uv run python3 train_fusion_model.py --forge-only 2>&1 | grep -E 'Fold|Mean|total'`

Expected: Mean NDCG@30 >= 0.50 (was 0.5138 with 5 folds, lr=0.03). Total time should drop from ~268s to ~100s.

- [ ] **Step 3: Run EDHREC comparison to verify output quality**

Run: `uv run python3 compare_edhrec.py --forge --quiet 2>&1 | tail -5`

Expected: AVERAGE row should show ~4.8/30 On-EDHREC (same as before).

- [ ] **Step 4: Commit**

```bash
git add train_fusion_model.py
git commit -m "perf: speed up GBM training -- 3 folds, lr=0.05, CV-capped final model"
```

---

### Task 2: Expand edge index with strength + events to eliminate SQL during training

The `_bulk_load_commander_edges` function runs 2 GROUP BY SQL queries per batch of 100 commanders against 18M rows (~56s total for 1358 commanders). The in-memory edge index already has adjacency data but is missing `strength` and `event`. Adding these eliminates all SQL during `build_forge_feature_matrix`.

**Key insight:** 18.3M raw edges aggregate to 16.6M unique (src, tgt) pairs. We store:
- `SUM(strength)` per pair as float32 array
- `set of distinct events` per pair as uint32 bitmask (31 events fit in 32 bits)

The npz cache uses `allow_pickle=False` for safe loading (no arbitrary code execution). Event names are stored as a numpy string array.

**Files:**
- Modify: `src/mtg_synergy/recommend/forge_features.py:344-428` (_build_edge_index)
- Modify: `src/mtg_synergy/recommend/forge_features.py:459-530` (CmdrFeatureContext.__init__ and _init_from_index)
- Modify: `train_fusion_model.py:691-727,776-807` (_bulk_load_commander_edges and build_forge_feature_matrix)
- Test: `tests/test_forge_features.py`

- [ ] **Step 1: Add event encoding helpers to forge_features.py**

At the top of `forge_features.py` (after the imports), add:

```python
def _decode_events(mask, bit_to_event):
    """Decode a uint32 bitmask to a set of event name strings."""
    result = set()
    for bit, name in bit_to_event.items():
        if mask & (1 << bit):
            result.add(name)
    return result
```

- [ ] **Step 2: Expand _build_edge_index to scan strength + event**

Modify `_build_edge_index` in `ForgeFeatureContext`. Change the SQL scan to also read `strength` and `event`, store them in the npz cache, and build aggregated dicts.

The DB scan SQL changes from:
```sql
SELECT source_id, target_id, filter_precision FROM interaction_edges
```
to:
```sql
SELECT source_id, target_id, filter_precision, strength, event FROM interaction_edges
```

The npz cache adds 3 new arrays: `strength` (float32), `event_ids` (uint8), `event_names` (string array). Cache validation adds checks for these keys.

After building the existing adjacency dicts (_adj_out, _adj_in, _exact_out, _exact_in), also build aggregated strength + event dicts using a new `_build_agg_arrays` static method.

The aggregated dicts are:
- `_agg_strength_out[src_idx]` = `{tgt_idx: sum_of_strength}`
- `_agg_events_out[src_idx]` = `{tgt_idx: uint32_bitmask}`
- Same for `_in` direction.

Also store `_event_names`, `_event_to_bit`, `_bit_to_event` on the context.

- [ ] **Step 3: Add `_build_agg_arrays` static method**

Add to `ForgeFeatureContext`, next to `_build_adj_arrays`:

```python
@staticmethod
def _build_agg_arrays(keys, values, strengths, event_ids):
    """Build aggregated strength + event dicts per (key, value) pair.

    Returns:
        agg_strength: dict[key_idx -> dict[val_idx -> float sum]]
        agg_events: dict[key_idx -> dict[val_idx -> uint32 bitmask]]
    """
    if len(keys) == 0:
        return {}, {}
    order = np.argsort(keys)
    sk = keys[order]
    sv = values[order]
    ss = strengths[order]
    se = event_ids[order]

    agg_strength = {}
    agg_events = {}

    changes = np.concatenate([[0], np.where(sk[1:] != sk[:-1])[0] + 1, [len(sk)]])
    for i in range(len(changes) - 1):
        start, end = int(changes[i]), int(changes[i + 1])
        k = int(sk[start])
        str_dict = {}
        evt_dict = {}
        for j in range(start, end):
            v = int(sv[j])
            str_dict[v] = str_dict.get(v, 0.0) + float(ss[j])
            eid = int(se[j])
            if eid < 32:
                evt_dict[v] = evt_dict.get(v, 0) | (1 << eid)
        agg_strength[k] = str_dict
        agg_events[k] = evt_dict

    return agg_strength, agg_events
```

- [ ] **Step 4: Update CmdrFeatureContext._init_from_index to use in-memory data**

Replace the SQL queries in `_init_from_index` with lookups into the aggregated dicts:

```python
def _init_from_index(self, ctx, cmdr_oid, deck_oids):
    """Fast path: use pre-loaded edge index -- no SQL needed."""
    cmdr_idx = ctx.oid_to_idx.get(cmdr_oid)
    idx_to_oid = ctx._idx_to_oid

    self.cmdr_out = {}
    self.cmdr_in = {}
    self.cmdr_out_events = {}
    self.cmdr_in_events = {}

    if cmdr_idx is not None:
        # Outgoing: commander -> targets
        str_dict = ctx._agg_strength_out.get(cmdr_idx, {})
        evt_dict = ctx._agg_events_out.get(cmdr_idx, {})
        for tgt_idx, s in str_dict.items():
            oid = idx_to_oid.get(tgt_idx)
            if oid:
                self.cmdr_out[oid] = s
                mask = evt_dict.get(tgt_idx, 0)
                self.cmdr_out_events[oid] = _decode_events(mask, ctx._bit_to_event)

        # Incoming: sources -> commander
        str_dict = ctx._agg_strength_in.get(cmdr_idx, {})
        evt_dict = ctx._agg_events_in.get(cmdr_idx, {})
        for src_idx, s in str_dict.items():
            oid = idx_to_oid.get(src_idx)
            if oid:
                self.cmdr_in[oid] = s
                mask = evt_dict.get(src_idx, 0)
                self.cmdr_in_events[oid] = _decode_events(mask, ctx._bit_to_event)

    self._init_cmdr_exact_and_deck_edges(ctx, cmdr_oid, deck_oids)
```

- [ ] **Step 5: Simplify build_forge_feature_matrix -- remove SQL edge loading**

In `train_fusion_model.py`, simplify `build_forge_feature_matrix`:

1. Remove the call to `_ensure_event_column(conn)` (no longer needed -- no SQL during feature building).
2. Remove the batch loop with `_bulk_load_commander_edges`. Instead, iterate commanders directly -- `CmdrFeatureContext._init_from_index` now handles everything from memory.
3. Stop passing `preloaded_cmdr_edges` from training code.

The inner loop simplifies to a flat commander iteration (no batches needed):

```python
n_cmdrs = len(cmdr_oids_ordered)
row_idx = 0

for ci, cmdr_oid in enumerate(cmdr_oids_ordered):
    if verbose and ((ci + 1) % 200 == 0 or ci == 0):
        print(f"  Commander {ci+1}/{n_cmdrs}...", flush=True)

    pairs = pairs_by_cmdr.get(cmdr_oid, [])
    if not pairs:
        continue

    deck_oids_for_cmdr = {oid for oid, lbl in pairs if lbl > 0}
    cmdr_ctx = CmdrFeatureContext(ctx, cmdr_oid, deck_oids_for_cmdr)

    cmdr_type_line = card_meta.get(cmdr_oid, {}).get("type_line", "")
    if "\u2014" in cmdr_type_line:
        try:
            cmdr_ctx.cmdr_subtypes = {
                s.lower() for s in cmdr_type_line.split("\u2014")[1].strip().split()
            }
        except (IndexError, AttributeError):
            pass

    cmdr_idx = cmdr_to_idx[cmdr_oid]
    for card_oid, label in pairs:
        meta = card_meta.get(card_oid, {})
        feats = compute_card_features(
            card_oid, meta.get("type_line", ""), float(meta.get("cmc", 0.0)),
            ctx, cmdr_ctx,
        )
        X[row_idx] = feats
        y[row_idx] = float(label)
        cmdr_ids[row_idx] = cmdr_idx
        row_idx += 1
```

- [ ] **Step 6: Remove dead code**

Remove `_bulk_load_commander_edges` function from `train_fusion_model.py` (no longer called). Keep `_ensure_event_column` since it may be useful for other SQL paths.

- [ ] **Step 7: Delete stale edge index cache**

The cache format changed (added strength, event_ids, event_names). The old cache will be detected as invalid (missing keys) and rebuilt automatically, but delete it explicitly to be safe:

```bash
rm -f data/edge_index_cache.npz
```

- [ ] **Step 8: Run full rebuild and validate**

Run: `time uv run python3 train_fusion_model.py --forge-only --rebuild-features 2>&1`

Expected:
- First run rebuilds edge index cache (~40s scan + aggregation)
- Subsequent runs reload from cache (~2-3s)
- No `Bulk-load edges` or SQL messages during feature building
- Feature matrix identical shape: (372896, 51)
- NDCG@30 >= 0.50

- [ ] **Step 9: Run tests**

Run: `uv run pytest tests/test_forge_features.py -v`

Expected: All 40 tests pass.

- [ ] **Step 10: Run EDHREC comparison**

Run: `uv run python3 compare_edhrec.py --forge --quiet 2>&1 | tail -5`

Expected: AVERAGE ~4.8/30 On-EDHREC (unchanged).

- [ ] **Step 11: Commit**

```bash
git add src/mtg_synergy/recommend/forge_features.py train_fusion_model.py
git commit -m "perf: expand edge index with strength+events, eliminate SQL during training"
```
