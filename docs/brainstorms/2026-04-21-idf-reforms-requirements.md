---
date: 2026-04-21
topic: idf-reforms
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 3)
status: draft (brainstorm 3 of 7)
depends_on:
  phase_1_2: 2026-04-21-unified-eval-harness-requirements.md (FR3 --rule ablation for audit)
  phase_3: 2026-04-21-typed-port-graph-requirements.md (FR1 canonical vocabulary for depth weighting)
---

# Requirements: IDF Reforms — BM25F, Conditional Denominator, Synapomorphy Depth Weighting

## Problem Statement

The current IDF weighting in `universal_scorer._score_universe` has three observable failure modes:

1. **Port-volume bias.** Cards with many ports (Atraxa, Niv-Mizzet Reborn, complex planeswalkers) win a disproportionate share of rules by sheer volume. Current scoring sums raw IDF weights without length normalization.
2. **Global-denominator distortion.** IDF treats rarity as "rare in the full 32 k-card universe," so counter-matter ports are marked rare even though they are baseline for Atraxa. The most thematic cards get under-weighted because their ports are common in the relevant subset.
3. **Signal-flat tuple matching.** A generic "creature ETB" port and a specific "self-only creature ETB that draws a card" port carry the same weight when IDF counts both equally. The deeper specialization should be rewarded.

Each failure mode has a well-studied IR technique behind its fix (BM25 for length normalization, BM25F for per-field weighting, conditional / pool-specific DF for denominator scoping, synapomorphy-style depth weighting from cladistics for trait specificity). None are novel — they are disciplined, decades-old retrieval mathematics applied to a port-bag.

## Goals

1. Dampen port-heavy volume winners via BM25 length normalization on per-card port counts.
2. Treat port shape as structured multi-field documents via BM25F, weighting `triggers`, `activated_costs`, `static_buffs`, `replacement_effects`, `keywords`, `scales_with` independently.
3. Compute IDF inside the color-identity × legal-commander pool, not the 32 k universe, via a conditional denominator.
4. Reward exponentially the most-derived matching port shape (synapomorphy weighting) — deferred to phase 3 because it cleanly requires Survivor 2's canonical vocabulary.
5. Ship each reform independently, each audited under the guardrail, so regressions are localized.

## Non-Goals

- Automated BM25 / BM25F hyperparameter tuning. First cut uses literature defaults (`k1=1.2`, `b=0.75`) and a single audit-driven sweep over `b`. Coordinate-descent weight optimization is a separate follow-up that depends on Survivor 1.
- Changing which ports are extracted. `ports.py` output is the input to IDF; this brainstorm does not extend extraction.
- Removing the existing `_RULE_QUALITY_MULTIPLIER` or `_FLAT_WEIGHT_OVERRIDES` tables. Those are rule-level multipliers and survive the IDF change.
- Replacing IDF entirely (e.g., with PPMI or learned weights). IDF stays; the three reforms refine how it's computed.

## Users and Scenarios

| Scenario | Before | After |
|---|---|---|
| Atraxa recommendation lists Niv-Mizzet Reborn at rank 4 (a port-heavy rainbow card with no real counter synergy) | Current IDF rewards port volume | BM25 length-norm dampens; Niv drops. Focused counter-matter cards rise. |
| Karn Liberated outscored by draft chaff with spurious overlapping `SCALES_WITH` ports | Raw IDF counts | BM25F weights static-buff matches differently from trigger matches; chaff loses |
| Counter-matter cards score low for Atraxa because "rare in Magic" | Global-universe IDF | Conditional denominator (white/black/green/blue pool only); counter-matter ports become baseline; *stacks* of counter mechanics become the signal |
| Two creature-ETB rules fire on a card, one generic one specific — equal contribution | Flat IDF | Synapomorphy depth weighting exponentially rewards the more-derived port chain (phase 3, post-Survivor 2) |

## Functional Requirements

### FR1 — BM25F scoring (phase 1)

Replace the per-port IDF sum in `_score_universe` with:

```
score = Σ_f Σ_p_in_f   w_f · idf(p) · (tf_p · (k1+1)) / (tf_p + k1·(1 − b + b · |D_f| / avg|D_f|))
```

where `f` ranges over fields `{triggers, activated_costs, static_buffs, replacement_effects, keywords, scales_with}`, `|D_f|` = per-field port count on candidate, `avg|D_f|` = mean across candidate pool, `w_f` = per-field weight. Defaults: `k1=1.2`, `b=0.75`, `w_f=1.0` for all fields.

First audit: sweep `b ∈ {0.0, 0.25, 0.5, 0.75, 1.0}` with `w_f=1.0`; pick the `b` value that maximizes aggregate NDCG without any commander regression exceeding HARMFUL threshold. Commit that value; tuning `w_f` is a second, follow-up audit.

### FR2 — Conditional (color/legal) denominator (phase 2)

Split IDF computation into two paths:

- **Global IDF** (retained for rule-weight computation, hand-tuned multipliers, and legacy `--explain` output).
- **Conditional IDF**, computed per commander at query time: denominator = `|candidates matching color_identity AND legal_commander=1|`. This replaces the global IDF used inside `_score_universe`'s per-candidate scoring.

Cache conditional-IDF tables per `(color_identity_bitmask, port_signature)` — 32 color identities × ~10 k distinct port signatures = bounded. Rebuilt on `bench.py audit --repin`.

### FR3 — Synapomorphy depth weighting (phase 3, blocks on Survivor 2)

For each (commander-port, candidate-port) match, compute the "match depth" = length of the longest shared canonical-node-kind-plus-attribute chain (e.g., `CAST → CAST.Creature → CAST.Creature.non-legendary → CAST.Creature.non-legendary.costs-more-than-2`). Match contribution becomes `base_contribution · α^depth` for small `α > 1` (initial `α = 1.2`; audited).

Requires Survivor 2's canonical vocabulary to name the chain levels; stays in backlog until Survivor 2 FR1 lands.

### FR4 — Per-reform audit gate

Each reform ships behind a feature-flag / config constant so the order of adoption is reversible:
- `_IDF_METHOD = "legacy" | "bm25f"`
- `_IDF_DENOMINATOR = "global" | "conditional"`
- `_MATCH_WEIGHT = "flat" | "synapomorphy_depth"`

Audit workflow per reform:
1. Flip the config constant.
2. Run `bench.py audit` against pinned baseline.
3. If verdict is positive or MARGINAL: commit + re-pin.
4. If CONTENTIOUS: investigate per-commander losers; either tune and retry, or revert.
5. If HARMFUL: revert immediately. The reform is not accepted.

### FR5 — Preserve `--explain` fidelity

`recommend.py --explain` output continues to work. Per-port contribution breakdowns reflect the active IDF method. Add a diagnostic line `idf_method: bm25f(b=0.5)` so users reading explanations know the active config.

## Success Criteria

1. **BM25F landing.** Aggregate NDCG@30 on 100-cmdr golden set stays ≥ current baseline (0.262) after BM25F + best `b` sweep. No commander regresses beyond HARMFUL threshold. Port-heavy cards (Atraxa, Niv-Mizzet Reborn, Sliver Hive, Golos, Tiamat) show measurable drop in their top-30 ranks for unrelated commanders. Verified by direct inspection of 5-10 affected commander pages.
2. **Conditional denominator landing.** Aggregate NDCG stays ≥ post-BM25F baseline. Atraxa, Karn, Narset, Teferi (commanders with highly-themed port profiles) show measurable uplift on their hi-syn cards. Verified via `bench.py audit --commander <name>` diff vs pre-change baseline.
3. **Depth weighting landing (after Survivor 2).** Aggregate NDCG stays ≥ post-conditional baseline. Commanders with deeply-specified trigger chains (e.g., Scurry Oak, Rowan Kenrith) show uplift on cards that *also* have deep matching chains. CONTENTIOUS verdict is acceptable if the uplift concentrates on the chosen depth-target commanders and losses are on shallow/generic commanders.
4. **Reversibility.** Each reform can be toggled off via a single config constant without code revert. Used during audit sweeps and as emergency revert if a later change surfaces a regression attributed to this one.
5. **Explainability.** Every score in `recommend.py --explain` continues to show per-rule contributions; the per-field breakdown is available via a new `--explain-fields` flag.

## Constraints

- No ML, no training. All weights either literature defaults or audit-driven single-value picks.
- Conditional IDF cache must fit in memory alongside the existing candidate cache — budget ~50 MB for the 32 × 10 k table.
- BM25F must not change per-candidate runtime by more than 20% vs current scoring. Per-field aggregation is a constant factor per candidate.
- Each config constant is a single static value, not a dict of overrides. Per-rule or per-commander IDF variation would re-introduce the hand-tuning treadmill; the goal is to replace tuning with math.

## Open Questions (For Planning Phase)

- Exact field set for BM25F — does `scales_with` need splitting into `scales_with.value` vs `scales_with.valid` since they behave differently?
- Whether conditional IDF uses `color_identity_bitmask` exactly or includes format legality (Commander banlist changes over time).
- How to handle candidate pools smaller than ~50 cards where IDF numerics get noisy (e.g., 5-color commanders with narrow keyword profiles).
- Depth weighting's `α` choice — fixed or per-field?
- BM25F per-field weights `w_f` — when to tune them (after initial landing? concurrent with landing?).

## Out of Scope for This Brainstorm

- Automated coordinate-descent tuning of `b`, `k1`, `α`, `w_f`. Requires Survivor 1's fast audit harness; will be a separate effort.
- Replacing IDF with PPMI / learned weights.
- Per-archetype IDF variants.

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 3.
- Prerequisites: `2026-04-21-unified-eval-harness-requirements.md` FR3 / FR5 for audit gate; `2026-04-21-typed-port-graph-requirements.md` FR1 for synapomorphy depth weighting in phase 3.
- Guardrail: `memory/feedback_audit_every_change.md` — each reform is audited independently; no bundled landings.
- Prior art references captured in `ce-ideate` session scratch (BM25, BM25F, PPMI, cladistic synapomorphy); retained as a pointer for planning.
