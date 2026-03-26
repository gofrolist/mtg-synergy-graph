# Tower Pre-filter with Color Identity — Design Spec

**Date**: 2026-03-26
**Goal**: Replace graph-based candidate discovery + EDHREC injection with tower batch scoring + color identity filter. Faster, better coverage, no EDHREC bias in candidate selection.

## Why

- Graph candidate pool only covers ~50% of EDHREC avg deck cards (607 candidates for Krenko)
- EDHREC injection adds ~12 more but biases toward EDHREC's opinion
- Tower model can score ALL legal cards in <200ms with 97-99% Recall@3000
- Color identity filter eliminates 60-75% of illegal cards upfront

## Measured Performance

| Commanders | R@2000 | R@3000 | Speed |
|---|---|---|---|
| Trained (5 tested) | 99% | 99% | 70-190ms |
| Unseen (6 tested) | 94% | 97% | 70-190ms |
| Current graph | 50% | N/A | seconds |

## Design

### New function: `tower_prefilter()`

In `mtg_synergy/recommend/scoring.py`:

```python
def tower_prefilter(conn, cmdr_oid: str, color_identity: set, top_n: int = 3000) -> list:
    """Score all color-legal cards with the fusion tower and return top N.

    Returns: [(oracle_id, name, tower_prob), ...] sorted by probability descending.
    """
```

Logic:
1. Load fusion tower model (cached singleton via `_load_fusion_model()`)
2. Query all cards with matching color identity: `SELECT oracle_id, name, color_identity FROM cards`
3. Filter: `card_ci.issubset(commander_ci)` and `oid != cmdr_oid` and `oid in oid_to_idx`
4. Batch forward pass: all legal card embeddings × commander embedding → sigmoid probabilities
5. Sort by probability, return top N with (oracle_id, name, tower_prob)

### Modified: `recommend_cards()`

In `mtg_synergy/recommend/engine.py`:

Current:
```python
candidate_scores = _candidate_scores(graph, deck_cards, commander, key_cards)
# then score_all_candidates does EDHREC injection + scoring
```

New:
```python
# Tower pre-filter: find top 3000 candidates by tower P(in deck)
from mtg_synergy.recommend.scoring import tower_prefilter, DeckContext, score_all_candidates
prefiltered = tower_prefilter(conn, cmdr_oid, color_identity, top_n=3000)

# Build candidate_scores from tower results (excluding deck cards)
candidate_scores = {}
for oid, name, tower_prob in prefiltered:
    if name not in deck_cards:
        candidate_scores[name] = {
            "total": 0.0, "partners": [], "multi_sig": 0,
            "commander_synergy": 0.0, "key_synergy": 0.0,
        }

# Enrich with graph partner info (for display only)
for card in deck_cards:
    for edge in graph_adjacency.get(card, []):
        if edge["target"] in candidate_scores:
            info = candidate_scores[edge["target"]]
            info["partners"].append((card, edge["score"], edge["signals"]))

# Score candidates with full fusion model
score_all_candidates(candidate_scores, cards, ctx, conn)
```

### Modified: `score_all_candidates()`

Remove the EDHREC injection block (lines 323-357). Tower pre-filter already includes all cards worth scoring — EDHREC synergy is still used as Feature F8 in scoring, just not for injection.

### Unchanged

- `build_graph()` — still builds the graph (needed by swaps + partner display)
- `suggest_swaps()` — still uses graph for swap candidate discovery
- `DeckContext` — still loads EDHREC synergy for Feature F8 scoring
- Fusion model GBM — still scores using 10-feature vector
- Combo detection — still uses spellbook data

### Fallback

If fusion tower model is not loaded (files missing), fall back to current graph-based candidate discovery. This preserves backward compatibility.

## Files

| File | Action |
|---|---|
| `mtg_synergy/recommend/scoring.py` | Add `tower_prefilter()` function, remove EDHREC injection from `score_all_candidates()` |
| `mtg_synergy/recommend/engine.py` | Use `tower_prefilter()` for candidates, enrich with graph partners for display |
| `mtg_synergy/config.py` | Add `TOWER_PREFILTER_TOP_N = 3000` |

## Success Criteria

| Metric | Target |
|---|---|
| Recall@100 | Within 2% of 89.0% baseline |
| Candidate discovery time | <250ms (was seconds for graph building) |
| Unseen commander R@3000 | ≥95% |
| All tests pass | 485 tests |
