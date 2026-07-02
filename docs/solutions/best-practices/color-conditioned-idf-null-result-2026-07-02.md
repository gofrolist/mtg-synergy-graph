---
last_updated: 2026-07-02
module: scoring
title: Color-conditioned IDF probe declined — small-N key saturation cliff (null result)
tags:
  - scoring
  - idf
  - null-result
  - audit-gate
  - color-identity
  - plan-2026-07-02-001
problem_type: best_practice
resolution_type: reference
applies_when:
  - Considering conditioning the IDF denominator on the commander's color-identity-legal pool
  - Looking at "orthogonal directions to lift the recommendation model" and tempted to re-try population-conditioned IDF
  - Designing any per-commander-pool weighting scheme (the small-N saturation mechanism documented here is generic)
created: 2026-07-02
plan_ref: docs/plans/2026-07-02-001-feat-color-conditioned-idf-probe-plan.md
brainstorm_ref: docs/brainstorms/2026-07-02-color-conditioned-idf-requirements.md
---

# Color-conditioned IDF probe declined — small-N key saturation cliff

The color-identity-conditioned IDF probe (plan 2026-07-02-001) shipped
its infrastructure units (500-cmdr baseline fixture regeneration;
aggregate + identity-size slices in the per-commander NDCG report) and
**DECLINED** the scoring change at the Unit 4/5 gates. The flag-gated
implementation was committed (`f77e9b9`) and reverted (`d6bea5e`) —
the scoring path is bitwise back at baseline
(`--expect-identity` PASS, config_hash `d08d5800daea`).

This closes the **population axis** of the IDF family the way
`bm25-idf-null-result-2026-05-04.md` closed the curve-shape axis: the
current `1/log2(1+N)` over the color-unfiltered matched set has now
survived both a curve-shape probe and a denominator-population probe,
each with a fresh multiplier re-tune ruled out as the confound.

## What was tried

`N` for non-flat IDF keys counted over only the commander's
color-identity-legal pool (engine `page()` predicate parity, identity
union over the full commander set), with orphaned keys (in-pool N=0)
keeping their unconditioned weight. Flag-gated
(`_ENABLE_COLOR_CONDITIONED_IDF`), registered in
`ScoringConfigInputs` → hash `c8b42f82b30a`. Pre-flight kill-test on
per-key shrink had shown real re-ranking signal (inflation spread
1.2–1.9× mono/2c/3c) — the premise was sound; the effect was net
destructive.

## Result: DECLINE (all gates, all axes)

| Gate | Threshold | Measured |
|---|---|---|
| R7 per-commander cliff (500-cmdr) | any delta < −0.05 → DECLINE | **28 violations**, worst −0.4087 (Kodama of the West Tree); 8 commanders to live NDCG 0.0000 |
| R6 SHIP aggregate | ≥ +0.010 | **−0.0076**, bootstrap CI95 [−0.0112, −0.0045] — zero excluded, significantly negative |
| Gem guardrail | ≥ −0.01 | hidden_gem_hit_rate 0.8423 → 0.8260 (−0.016); forensics gem rate 0.751 → **0.440** |
| INVESTIGATE (gem-dominant) | gem ≥ +0.02, NDCG flat | not reached (gem regressed) |
| R8a re-sweep rescue | one `--optimize` on conditioned tensor | train +0.0005 (sub-noise), **held delta 0.0** — calibration is NOT the confound |

Identity-size slice of the 28 violations: mono 14, 2-color 11,
colorless 1, 3-color 1, 5-color 1 — **broad-based across mono/2c, not
colorless-specific**, which weakens the pre-identified small-pool
λ-blend fallback (it would dampen the extreme pools while leaving the
mono/2c majority of the damage in place).

## Failure mechanism: small-N key saturation

Conditioning only ever *raises* non-flat weights, and the `1/log2(1+n)`
curve is steepest exactly where pools bite: a key with 1–2 in-pool
matchers saturates at weight ~1.0 versus the healthy median 0.09–0.15.
Each such key single-handedly launches its (often obscure) matcher past
the whole labeled top-30 — e.g. Kodama of the West Tree's flag-ON
top-10 was led by "Genji Glove" at score 1.728. Kill-test measurements
had flagged 20–75% of keys in the n≤3 regime per commander; in
production that regime dominated outcomes.

The origin doc's named counter-hypothesis **fired**: OUTRANKED share
was unchanged (46.2% → 46.2%), `staple_only` miss sub-reason exploded
15 → 482, and the forensics plausibility gate collapsed — conditioning
amplified narrow-key displacers rather than rescuing the missed cards.
Per the ideation doc, the funded next lever for the displacement
pattern is **concave within-family aggregation** (idea #3-weak), not
more IDF work.

## What survives

- 500-cmdr fixture regenerated on baseline (Unit 1, commit `3f0dfaf`).
- Per-commander report aggregate summary + identity-size slices
  (Unit 3, commit `adac30f`) — standing instruments.
- The reverted implementation remains recoverable at `f77e9b9`
  (tag `pre-color-idf` marks the baseline before it).
- Process lesson: the pre-commit pytest hook stashes unstaged
  implementation edits but NOT untracked test files — committing while
  a sibling unit's untracked tests exist false-fails the hook
  (ImportError). Land implementation+tests together or sequence
  commits so untracked tests never orphan.

## Do not re-try unless

- A **sublinear or floored conditioning** variant is proposed (e.g.
  `n_eff = max(n_legal, floor)` with a swept floor, or λ-blend applied
  globally rather than small-pool-scoped) — i.e. a named delta that
  specifically neutralizes small-N saturation while keeping the
  population signal; AND
- the displacement-side lever (concave within-family aggregation) has
  been probed first — the forensics evidence points there.
