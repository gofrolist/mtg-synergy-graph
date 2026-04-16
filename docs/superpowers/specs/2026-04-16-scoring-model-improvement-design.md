# Scoring Model Improvement — Design

**Date:** 2026-04-16
**Status:** Approved
**Baseline:** NDCG@30 = 0.19397 (100-commander golden set)

## Problem

Broad data-driven rules (density rules, etb_self, etc.) rely on hand-tuned flat
weight overrides because pure IDF scoring overweights them. CLAUDE.md states
"no hand-tuned weights — specificity is derived from the data," so the current
scoring math is working around the data rather than reflecting it.

Root cause investigation surfaced three independent issues:

1. **Exponential port extraction bug.** `extract_effect_ports` in
   [ports.py:305-318](../../../src/mtg_synergy_graph/ports.py:305) recursively
   walks SubAbility chains that `walk_svar_chain` already walked. A linear
   N-deep SVar chain emits `2^N - 1` ports instead of N. 88 cards (0.3% of
   the DB) produce 57,249 ports — 31% of the entire `card_ports` table.
   Examples:
   - `Akroma, Vision of Ixidor`: 16,389 ports (correct: 14)
   - `Nature Demands an Offering`: 16,384 ports
   - `Largepox`: 5,632 ports

   IDF denominators are inflated by this bogus data. `PumpAll` with filter
   `Creature.Other+YouCtrl+withPartner` has N=8,192 — 99% from Akroma's
   duplicate emissions. Flat weight overrides in `_FLAT_WEIGHT_OVERRIDES`
   exist because IDF ratios are corrupted at the source.

2. **Unused high-signal Forge DSL data.**

   | Field | Ports/cards with value | Status |
   |---|---|---|
   | `ChangeType` on ChangeZone effects | 3,547 ports | In raw_line only |
   | `TokenScript` on Token effects | 3,733 ports | In raw_line only |
   | `DeckNeeds` | 1,198 cards | In `cards` table, not queried |
   | `DeckHints` | 3,906 cards | Same |
   | `DeckHas` | 7,114 cards | Same |
   | `BuffedBy` SVar | 1,047 cards | In `card_svars`, not queried |

   `ChangeType` tells us exactly what types a card cheats into play (Kaalia:
   Angel/Demon/Dragon). `DeckNeeds`/`DeckHints`/`DeckHas` are Forge's
   curated AI annotations — higher-signal than any mechanical match we can
   derive.

3. **Hand-tuned dampening constants.** Beyond `_FLAT_WEIGHT_OVERRIDES`,
   `_RULE_QUALITY_MULTIPLIER` also contains hand-tuned values
   (damage_synergy 0.5, value_engine 0.5, trigger_resonance 0.7, etc.).
   Both dicts need re-evaluation after the data cleanup.

## Goals

1. Eliminate the data bug so IDF denominators reflect real card population.
2. Extract all unused high-signal DSL data into queryable shape.
3. Remove hand-tuned weights where the cleaned-up data supports it.
4. Introduce curated-hint matching as a new rule tier, separate from
   mechanical port matching.
5. No NDCG regression at any phase; final aggregate NDCG > current 0.19397.

## Non-goals

- Rewriting the scoring architecture (still IDF over port complements).
- Reworking the complement rules registry structure.
- Changing the EDHREC scoring contribution or graph metrics.
- Modifying the public API (`SynergyEngine.page()`).

## Architecture

Three sequential phases. Each lands on `main` with a measured NDCG delta
before the next starts. No phase modifies the rule registry structure itself.

```
Phase A (data)     →  Phase C (scoring)  →  Phase B (new rule)
clean the DB          re-derive weights     add curated-hint rule
NDCG checkpoint       NDCG checkpoint       NDCG checkpoint
```

| Phase | Layer | Files touched |
|---|---|---|
| A | Data | `ports.py`, `importer.py`, `schema.sql` |
| C | Math | `universal_scorer.py` |
| B | Rules | `complement_rules/hints.py` (new), `complement_rules/__init__.py`, `universal_scorer.py` (registration) |

---

## Phase A — Data-layer cleanup

### A1. Fix exponential SubAbility re-walk

**File:** `src/mtg_synergy_graph/ports.py`

In `extract_effect_ports` at lines 305-318, the current code walks the
SubAbility chain via `walk_svar_chain` (which already returns all nodes in
the chain) and then recursively calls `extract_effect_ports` on every
returned node — re-walking the remainder of the chain each time.

**Fix:** emit exactly one port per `ChainNode` without re-walking.
`walk_svar_chain` is already the source of truth for chain traversal.

**TDD approach:**
1. RED: Add pytest cases asserting
   - `Akroma, Vision of Ixidor` has 14 `PumpAll` effect ports
   - `Nature Demands an Offering` has ≤ 16 ports total
   - `Largepox` has ≤ 16 ports
2. Run — tests fail with current code.
3. GREEN: apply the one-call-only fix.
4. Verify existing 212-test suite still passes.

### A2. Extract `ChangeType` into `port_attributes`

**File:** `src/mtg_synergy_graph/importer.py` (ChangeZone effect handling)

Parse `ChangeType` strings like
`Creature.Angel+YouCtrl,Creature.Demon+YouCtrl,Creature.Dragon+YouCtrl`
and emit one attribute per comma-separated clause, exploded by
the existing filter-parsing helpers.

New `port_attributes.attr_kind` value: `"change_type"`. Value is the
subtype or type name (Angel, Demon, Dragon).

### A3. Extract `TokenScript` into `port_attributes`

Parse `TokenScript` strings like `w_1_1_soldier` into:
- `(attr_kind="token_color", attr_value="W")`
- `(attr_kind="token_subtype", attr_value="Soldier")`

Multi-choice scripts (`w_1_1_human,u_1_1_merfolk,r_1_1_goblin`) emit one
set per option.

### A4. New `card_hints` table for Forge AI annotations

**File:** `src/mtg_synergy_graph/schema.sql` + `importer.py`

```sql
CREATE TABLE IF NOT EXISTS card_hints (
  card_name  TEXT NOT NULL REFERENCES cards(name),
  kind       TEXT NOT NULL,     -- 'needs' | 'hints' | 'has' | 'buffed_by'
  category   TEXT NOT NULL,     -- 'Type' | 'Ability' | 'Color' | 'Keyword' | 'Name'
  value      TEXT NOT NULL,
  PRIMARY KEY (card_name, kind, category, value)
);

CREATE INDEX IF NOT EXISTS idx_card_hints_lookup
  ON card_hints(kind, category, value);
```

The importer already populates `cards.deck_needs` / `deck_hints` / `deck_has`
as raw JSON. Add a post-processing pass that parses those JSON documents
into `card_hints` rows normalised shape. Populate `kind='needs'|'hints'|'has'`
respectively.

### A5. Extract `BuffedBy` SVar

In the same post-processing pass, query `card_svars` for rows where
`svar_name='BuffedBy'`, split the comma-separated value, infer `category`
by lookup against the Forge type/keyword vocabulary, and insert rows with
`kind='buffed_by'`.

### A6. Validation gate

1. `uv run pytest tests/` — all existing + new A1 tests pass.
2. `uv run python scripts/import_cardsfolder.py` — fresh DB.
3. Verify total `card_ports` row count drops by ≥25% vs the 184,106 current.
4. `uv run python scripts/golden_set_track.py --baseline tests/fixtures/golden_set_run.json`
5. **Pass criterion:** NDCG ≥ 0.189 (baseline minus tolerance) AND
   port count dropped by ≥ 25%.
6. Commit with `fix:`-prefixed message recording the NDCG delta.
7. Update `tests/fixtures/golden_set_run.json` with the new baseline.

---

## Phase C — Scoring math cleanup

Goal: delete the hand-tuned constants identified as an anti-pattern in
CLAUDE.md, and verify the cleaned-up math against the golden set.

### C1 variant — drop flat weights, keep concentration dampening

1. Remove `_FLAT_WEIGHT_OVERRIDES` dict
   ([universal_scorer.py:296-304](../../../src/mtg_synergy_graph/universal_scorer.py:296)).
2. Remove `_FLAT_COUNT_RULES` frozenset
   ([universal_scorer.py:275-285](../../../src/mtg_synergy_graph/universal_scorer.py:275)).
   All rules now go through `1 / log₂(1 + N)` IDF.
3. Keep the signal-concentration dampening in `UniversalScore.score`
   ([universal_scorer.py:206-212](../../../src/mtg_synergy_graph/universal_scorer.py:206)) —
   it's data-derived, not per-rule, so it's not hand-tuning in the sense
   CLAUDE.md prohibits.
4. Run golden-set tracker; record NDCG.

### C2 variant — drop flat weights + data-derived ceiling for broad-filter rules

Same as C1 plus:

- For rules that match via identity filters (etb_self, zone_resonance), if
  the filter group has N > threshold (start with N ≥ 1000), apply a ceiling:
  `weight = min(idf, k / log₂(1 + distinct_filter_groups_in_rule))`.
- Intuition: Yarok's generic `Creature` filter competes with many other
  commanders' `Creature` filters, so the signal is diluted. Kaalia's
  `Creature.Angel` filter is unique — it keeps full IDF weight.
- `k` picked so post-C2 NDCG matches or beats C1 on golden set. Acceptable
  if picking `k` can be framed as "percentile of filter-group distinctness",
  not a free constant.

### C3 audit — `_RULE_QUALITY_MULTIPLIER`

Located at
[universal_scorer.py:308-319](../../../src/mtg_synergy_graph/universal_scorer.py:308).
Each entry (damage_synergy 0.5, value_engine 0.5, cost_reduction_target 0.5,
trigger_resonance 0.7) is hand-tuned. Flip each to 1.0 independently,
measure NDCG, drop any whose removal doesn't cost NDCG.

### Winner selection

- Run all three experiments against the post-A baseline.
- Pick highest NDCG@30.
- Tie within ±0.002 → prefer simpler variant (C1 > C2 > C3-partial).
- Commit with `perf:` prefix, recording delta vs post-A NDCG.

---

## Phase B — Curated-hint rules

Depends on A4/A5 (card_hints table populated).

### B1. Rule `deck_hint_match` (symmetric)

**File:** `src/mtg_synergy_graph/complement_rules/hints.py` (new)

Fire when commander's `card_hints` rows with `kind='has'` share a
`(category, value)` tuple with candidate's rows with `kind='needs'`.

IDF key: `(rule_id, category, category, value)` — each `(category, value)`
bucket has its own N. Rare shared values (e.g. `Ability=Delirium`, N=12)
score high; common ones (`Ability=Token`, N=1200) score low. This makes
specificity data-derived.

Bucket mapping: add `deck_hint_match → "hint_match"` to `_RULE_TO_BUCKET`.

### B2. Rule `deck_needs_fulfilled` (commander-centric)

Fire when commander has `kind='needs'` row with `category='Type'` (or
`'Color'`, `'Keyword'`) and candidate's `cards.types`/`subtypes`/`colors`/
`keywords` contain `value`.

Distinct rule_id from B1 because the match predicate differs (hint-vs-type,
not hint-vs-hint). IDF and pair bonuses handle them independently.

Handles the sparse-hint case: even candidates without their own
annotations still surface when commander states a need.

### B3. Rule `buffed_by_match`

Fire when candidate's `BuffedBy` value matches commander's types/subtypes
(or vice versa). Uses `card_hints` rows with `kind='buffed_by'`.

### B4. Pair bonuses

Add to `_SYNERGY_PAIRS`
([universal_scorer.py:114-140](../../../src/mtg_synergy_graph/universal_scorer.py:114)):

| Pair | Bonus |
|---|---|
| `{deck_hint_match, trigger_effect}` | 0.04 |
| `{deck_needs_fulfilled, tribal_density}` | 0.03 |
| `{buffed_by_match, lord}` | 0.03 |

### B5. Per-rule validation

For each of B1, B2, B3:

1. Enable the rule alone (others patched off).
2. Measure standalone NDCG delta vs post-C baseline.
3. Drop any rule with delta < +0.001 (pure additive noise).
4. Enable the surviving set together; measure combined delta.
5. Commit surviving rules with `feat:` prefix.

---

## Testing strategy

- **Unit tests** per phase. Targets:
  - A1: Akroma/Nature Demands/Largepox port counts.
  - A2: ChangeType attr extraction on Kaalia, Bring to Light,
    Gamble, Reanimate.
  - A3: TokenScript extraction on Scute Swarm,
    Tireless Provisioner.
  - A4/A5: card_hints populated from at least 5 canonical examples per
    `kind` value.
  - C1-C3: weight computation on synthetic port fixtures.
  - B1-B3: rule firing on fixture commanders with known DeckHas/DeckNeeds
    values.
- Each phase target: ≥80% coverage on new/modified code (project standard).
- Regression: existing 212-test suite must stay green.
- NDCG gates at end of each phase — acceptance criterion, not advisory.
- Update `tests/fixtures/golden_set_run.json` at each NDCG checkpoint.

## Error handling & risk

| Risk | Mitigation |
|---|---|
| Schema migration breaks existing queries | New `card_hints` table and new `port_attributes.attr_kind` values are additive. No existing columns removed. Fresh re-import required (project standard). |
| Phase A NDCG drops slightly from lost duplicate-port signal | Tolerance window of -0.005; A completes even with a small drop because the 31% noise reduction unlocks Phase C gains. |
| Phase C1/C2/C3 all regress NDCG | Revert Phase C; commit only phase A. Phase B still lands on post-A baseline. |
| DeckNeeds/DeckHints too sparse for Phase B to matter | Expected — mitigated by `deck_needs_fulfilled` which matches against non-annotated candidates via types/subtypes. If Phase B's combined NDCG delta < +0.002, drop Phase B. |
| Hint-based rule conflicts with an existing mechanical rule | Distinct rule_ids ensure IDF computes independently. Pair bonuses reward combined matches, not compound them into noise. |

## Rollback

Each phase is a separate commit (or small stack) on `main`:

- Phase A: revert the data commit + re-import from previous Forge dump.
- Phase C: revert the math commit; DB unaffected.
- Phase B: revert the rule commit; registry returns to prior state.

Any phase can be rolled back independently since it only touches one layer.

## Acceptance criteria

- [ ] Phase A: NDCG ≥ 0.189 AND `card_ports` row count drops ≥25%.
- [ ] Phase C: NDCG ≥ post-A baseline. Winning variant committed.
- [ ] Phase B: Combined NDCG delta ≥ +0.002 vs post-C baseline.
- [ ] `_FLAT_WEIGHT_OVERRIDES` removed from the codebase.
- [ ] All existing + new tests pass.
- [ ] CLAUDE.md updated to reflect new rule registry and removed flat weights.

## Out of scope

- Graph metrics, EDHREC scoring, staple bonuses — unchanged.
- Public API (`SynergyEngine.page()`) — unchanged.
- Rule-specific algorithm rewrites — only registration/weights/new rules.
