# Deterministic Stack Optimization — Design Spec

**Date**: 2026-03-25
**Goal**: Make the deterministic signal layers (causal graph, mechanics, tags) world-class so recommendations work for ANY commander at $0 cost, discover non-obvious synergies, and use EDHREC as validation — not a crutch.

**Priority order**: Coverage (C) > Discovery (B) > EDHREC alignment (A)

## Current State

| Signal | Top-30 Overlap | Coverage | Notes |
|--------|----------------|----------|-------|
| LLM scores | 0.675 | 33 commanders | Best single signal, but $0.50/commander |
| Causal graph | 0.571 | 5000 cards parsed | Below baseline (0.597) — noise from broad edges |
| Tower model | disabled | trained on LLM | Circular dependency |
| Mechanics | — | 41% coverage | Good quality, limited reach |
| Tag graph | ~0.55 | 34k cards | Baseline signal |

**Note on metrics**: The current `optimize_weights.py` computes top-30 set overlap (|our_30 & edhrec_30| / 30), not true NDCG (which is position-weighted). This spec uses "top-30 overlap" for the existing metric and introduces Recall@K as the new primary metric (Section 7).

**Key problem**: 1.17M causal edges with uniform strength per precision class. "creature enters" (300+ producers) and "Goblin enters" (10 producers) both get `broad=0.6`. Noise drowns signal.

## Design: 7 Sections

### Section 1: Event IDF Weighting (Producer Side)

**Problem**: Trigger edges get strength based only on filter precision (`exact=1.0, broad=0.6, unfiltered=0.3`). No frequency weighting.

**Solution**: Compute event-level IDF in `build_index()` and multiply into edge strength in `_build_trigger_edges()`.

```
event_producer_idf[event] = log(total_cards / cards_producing_event)
normalized to 0.3 - 3.0 range
```

**Example impact**:
- "creature_enters" (300 producers / 5000 cards) -> IDF ~0.4x -> strength: 0.6 * 0.4 = 0.24
- "goblin_enters" (10 producers) -> IDF ~2.5x -> strength: 0.6 * 2.5 = 1.5

**Where**: `mtg_synergy/causal/graph_builder.py:_build_trigger_edges()`. After `strength = _precision_to_strength(precision)`, multiply by `event_idf[event]`.

**Also apply to**: `_build_amplifies_edges` (uses event types for matching). **Not** `_build_feeds_edges` or `_build_enables_edges` — these use resource-based matching (creature/mana/haste) with hardcoded strengths, not event types. Their fixed strengths remain unchanged.

**IDF computation**: In `CardIndex` (indexer.py), add `producer_counts` and `responder_counts` dicts computed during `build_index()`. Pass to edge builders.

---

### Section 2: Expanded Oracle Parsing (5k -> 15k cards)

**Problem**: Only top 5000 cards by EDHREC rank are parsed. Misses staples in niche archetypes.

**Solution**: Run `oracle_parser.py --parse-all --top 15000`. Parser had 0 failures on 5000 cards — expected to scale safely. Monitor failure rate during 15k parse; new card text patterns may surface edge cases.

**Impact**:
- Better IDF statistics (more data for frequency estimation)
- Niche cards get causal edges (tribal payoffs, uncommon engines)
- Graph grows from ~1.17M to ~3-5M edges (IDF dampens the noise)

**Rebuild sequence**: Parse first -> rebuild graph with IDF -> re-evaluate.

**Cost**: Parse time ~6min, graph build ~2-3min. Acceptable for offline pipeline.

---

### Section 3: Responder IDF (Both Sides of the Edge)

**Problem**: Producer IDF alone isn't enough. If 500 cards trigger on "creature enters," a producer connecting to all of them still creates fan-out noise.

**Solution**: Combined IDF on both sides, with capping to preserve precision hierarchy:

```
combined_idf = min(producer_idf * responder_idf, 3.0)
edge_strength = precision_strength * combined_idf
```

Where:
- `producer_idf = log(N / cards_producing_event)` — rarity of producing this event
- `responder_idf = log(N / cards_responding_to_event)` — rarity of caring about this event
- Each individually normalized to 0.3-3.0 range
- **Combined product capped at 3.0** to prevent inversion of precision hierarchy (without cap, 3.0 * 3.0 = 9.0 would make a `broad=0.6` edge score 5.4, higher than any `exact=1.0` edge)

**Example** (after cap):
- Rare producer + rare responder: min(2.5 * 2.3, 3.0) = 3.0x (capped, high signal)
- Common producer + common responder: min(0.4 * 0.3, 3.0) = 0.12x (heavily dampened)
- Mixed: ~0.4 * 2.0 = 0.8x (moderate)

**Invariant**: `exact` edges always outrank `broad` edges for the same event: `1.0 * idf > 0.6 * idf`.

**Implementation**: `build_index()` already tracks both `_producers` and `_responders` per event. Add cardinality counts to `CardIndex`, pass to edge builders.

---

### Section 4: Chain Scoring (Multi-Card Interaction Paths)

**Problem**: Edges scored independently. Best EDH synergies are chains: A triggers B triggers C. Example: Krenko taps -> creates Goblins -> Goblin Sharpshooter untaps -> deals damage.

**Approach**: Commander-centric 2-3 card chains, computed at query time in `CausalContext`.

**Algorithm** (two-phase: precompute forward map at init, score per-candidate lazily):

**Phase 1 — Init-time forward map** (`CausalContext.__init__`):
Build a set of cards that the commander directly connects to, and for each, cache what deck cards they forward to:
```python
# {intermediate_card: [(deck_target, strength)]}
self._cmdr_forward_map = {}
for edge in self._outgoing[commander_id]:
    mid = edge.target
    if mid not in self._cmdr_forward_map:
        self._cmdr_forward_map[mid] = edge.strength
```
This is `O(cmdr_fanout)` — bounded and fast.

**Phase 2 — Per-candidate scoring** (called from `causal_score()`):
```python
def _chain_bonus(self, candidate_id):
    # Is the candidate connected TO the commander? (candidate -> cmdr link)
    cmdr_link = self._cmdr_forward_map.get(candidate_id, 0)
    if cmdr_link == 0:
        return 0.0
    # Does the candidate also connect to deck cards?
    bonus = 0.0
    for edge in self._outgoing.get(candidate_id, []):
        if edge.target in self.deck_oids:
            bonus += cmdr_link * edge.strength * 0.5
    return bonus
```

The `* 0.5` dampener prevents chains from dominating over direct commander edges. IDF-weighted edges naturally prioritize rare chains.

**Complexity**: `O(candidate_fanout)` per candidate — no quadratic explosion.

**Where**: New method `_chain_bonus()` on `CausalContext`, called from `causal_score()`. Replaces the empty `self._chain_bonus = {}` dict.

**Not doing**: Full infinite loop detection (chain_finder.py) — that's for `--combos`, not recommendations.

---

### Section 5: Commander Archetype Inference

**Problem**: Strategy detection needs deck context or EDHREC data. Only 502/3141 legal commanders have EDHREC data (16% coverage).

**Solution**: Infer archetype from commander's parsed abilities + oracle text + type line alone.

**Three signals**:

1. **Parsed abilities -> event profile**: Commander produces `creature_enters` with subtype `Goblin` -> tribal-goblin. Commander triggers on `dies` -> aristocrats.

2. **Oracle text keyword matching**: Reuse `STRATEGY_KEYWORDS` from `scoring.py` against commander's oracle text. Krenko matches `["create a", "token creature"]` -> tokens.

3. **Type line**: `Legendary Creature — Goblin Warrior` -> goblin-tribal.

**Output**:
```python
@dataclass
class CommanderProfile:
    strategies: set[str]           # detected archetypes
    tribal_type: str | None        # dominant creature type if tribal
    key_events_produced: set[str]  # what the commander generates
    key_events_consumed: set[str]  # what the commander responds to
    key_effects: set[str]          # what the commander does
```

**Precomputation**: Run once over all 3,141 legal commanders, store in `commander_profiles` table. O(1) lookup at recommendation time.

**Table schema**:
```sql
CREATE TABLE commander_profiles (
    oracle_id TEXT PRIMARY KEY,
    strategies TEXT NOT NULL,       -- JSON array: ["tokens", "tribal-goblin"]
    tribal_type TEXT,               -- "Goblin", "Human", etc. or NULL
    events_produced TEXT NOT NULL,  -- JSON array: ["creature_enters", ...]
    events_consumed TEXT NOT NULL,  -- JSON array: ["dies", ...]
    key_effects TEXT NOT NULL       -- JSON array: ["create", "deal_damage", ...]
);
```

**DeckContext integration**: In `DeckContext.__init__` (scoring.py), when `active_strategies` is empty/None:
```python
if not self.active_strategies and self.cmdr_oid:
    row = conn.execute(
        "SELECT strategies, tribal_type FROM commander_profiles WHERE oracle_id = ?",
        (self.cmdr_oid,)).fetchone()
    if row:
        self.active_strategies = set(json.loads(row[0]))
        if row[1] and not self.deck_types:
            self.deck_types = {row[1]}
            self.is_tribal = True
```

**Note**: `CommanderProfile.key_events_produced/consumed` overlaps with `CausalContext._cmdr_events_produced/consumed`. This is intentional — profiles are precomputed offline for all 3k commanders; CausalContext computes at query time from the edge graph (only for the current commander). The profile serves as fallback when causal edges are sparse.

**Where**: New module `mtg_synergy/recommend/commander_profile.py`. Used by `DeckContext` when no `active_strategies` provided.

**Coverage**: 502 EDHREC commanders -> all 3,141 legal commanders (100%).

---

### Section 6: Signal Integration

**Phase 1 — Fix and re-evaluate**:
After implementing IDF + expanded parsing + chains, re-run `optimize_weights.py` with current weights. Measure if causal NDCG improves from 0.571 toward 0.62+.

**Phase 2 — Sequential weight optimization**:
Extend `optimize_weights.py` to optimize more signals:

1. Fix LLM weight, optimize CAUSAL alone -> best CAUSAL weight
2. Fix both, optimize MECHANICS -> best MECHANICS weight
3. Fix all three, optimize TAG_OVERLAP -> best TAG weight
4. Final round with top-3 combos from each to verify

**Scoring formula stays the same** — `compute_dynamic_score()` in `scoring.py` already combines all features. We're improving CAUSAL input quality and finding better weights.

**Constraint**: EDHREC synergy (weight 3.0) stays as tiebreaker. Never dominant — preserves discovery priority.

**Expected outcome**:
- With LLM: NDCG 0.675 -> 0.69+ (causal now helps)
- Without LLM (coverage): NDCG ~0.55 -> 0.62+ (deterministic stack carries)

---

### Section 7: Validation Framework

**Ground truth — two datasets**:

1. **Existing**: `edhrec_card_synergy` table (132k pairs, 502 commanders) — per-card synergy scores. Used for the current top-30 overlap metric and backward-compatible evaluation.

2. **New**: EDHREC average decklists for top 1000 commanders. Fetched via EDHREC's JSON API (`https://json.edhrec.com/pages/average-decks/<slug>.json`). Stored in new table:
```sql
CREATE TABLE edhrec_average_decks (
    commander_slug TEXT NOT NULL,
    card_name TEXT NOT NULL,
    category TEXT,               -- creature, instant, sorcery, etc.
    PRIMARY KEY (commander_slug, card_name)
);
```
**Fetch pipeline**: New script `fetch_edhrec_decks.py` — iterates top 1000 slugs from `edhrec_card_synergy`, fetches average deck JSON, parses card list, stores to DB. Rate-limited to 1 req/sec. Run once (~20 min), refresh monthly.

**Primary metric — Recall@K**:
Of the EDHREC average deck's ~65 non-basic cards, how many appear in our top K?

```
Recall@K = |our_top_K intersection edhrec_avg_deck| / |edhrec_avg_deck_nonbasic|
```

Measured at K=100, K=50, K=30. This is the headline number.

**Backward-compatible metric — Top-30 overlap** (existing):
`|our_top_30 & edhrec_synergy_top_30| / 30`. Kept for continuity with previous evaluations. Note: this is set overlap, not position-weighted NDCG.

**Secondary metric — Role coverage (diagnostic only)**:
Sanity check that we're not missing entire categories. If our top 100 has zero removal but EDHREC avg has 8, that's worth flagging. Not optimized — just reported.

**Novelty report**:
Cards we recommend that EDHREC doesn't include. Manual spot-check for true discoveries vs noise.

**CLI**:
```bash
python3 optimize_weights.py --evaluate                # Recall@K across 1000 commanders
python3 optimize_weights.py --evaluate --no-llm       # Deterministic signals only
python3 optimize_weights.py --evaluate --novelty       # Our unique picks report
python3 optimize_weights.py --evaluate --deck krenko   # Single commander deep dive
```

**Deep dive** prints: our top 100 annotated with which cards are in EDHREC avg deck, role distribution comparison, and novel picks with their signal breakdown.

**Out of scope**: Mana curve comparison, deck building. We're a synergy finder, not a deck builder.

---

## Implementation Order

| Step | Section | Dependencies | Effort |
|------|---------|-------------|--------|
| 1 | Event IDF (S1 + S3) | None | Medium |
| 2 | Expanded parsing (S2) | None (parallel with S1) | Low |
| 3 | Rebuild graph with IDF | S1 + S2 | Low |
| 4 | Chain scoring (S4) | S3 (needs IDF edges) | Medium |
| 5 | Commander profiles (S5) | S2 (needs parsed abilities) | Medium |
| 6 | Validation framework (S7) | None (parallel with S1-S5) | Medium |
| 7 | Re-evaluate + rebalance (S6) | S1-S5, S7 | Low |

Steps 1+2 and 6 can run in parallel. Steps 4+5 can run in parallel after 3.

## Files Modified

| File | Change |
|------|--------|
| `mtg_synergy/causal/indexer.py` | Add event frequency counts to CardIndex |
| `mtg_synergy/causal/graph_builder.py` | Multiply IDF into edge strengths |
| `mtg_synergy/causal/__init__.py` | Chain bonus computation in CausalContext |
| `mtg_synergy/recommend/commander_profile.py` | **New** — CommanderProfile inference |
| `mtg_synergy/recommend/scoring.py` | Use CommanderProfile when no strategies provided |
| `optimize_weights.py` | Extended evaluation modes, Recall@K, --no-llm, --novelty |
| `fetch_edhrec_decks.py` | **New** — Fetch average decklists for top 1000 commanders |
| `oracle_parser.py` | Just run with --top 15000 (no code change) |
| `build_graph.py` | No code change — just rebuild |

## Rollback Plan

Before rebuilding the graph with IDF, back up the current edges:
```sql
CREATE TABLE interaction_edges_v1 AS SELECT * FROM interaction_edges;
```

Run evaluation on BOTH old-graph and new-graph scores for the same commander set before committing to the new graph. If IDF-weighted graph is worse, revert:
```sql
DROP TABLE interaction_edges;
ALTER TABLE interaction_edges_v1 RENAME TO interaction_edges;
```

## Success Criteria

**Primary (Recall@K against EDHREC average decks — the real measure):**
1. **Recall@100** (all signals): > 45% across 1000 commanders
2. **Recall@100** (no LLM, no EDHREC — coverage mode): > 30% across 1000 commanders
3. **Recall@50** (all signals): > 35%

These targets are initial estimates — calibrate after the first evaluation run shows baseline.

**Secondary (quick iteration check — dev speed only):**
4. **Top-30 overlap** must not regress below current baselines (LLM=0.675, combined=0.675). Not a target to optimize — just a sanity check that runs in seconds without needing average deck data.

**Non-metric:**
5. **Commander profile coverage**: 100% of 3,141 legal commanders get auto-inferred profiles
6. **IDF graph must beat non-IDF graph** on the same evaluation before committing
