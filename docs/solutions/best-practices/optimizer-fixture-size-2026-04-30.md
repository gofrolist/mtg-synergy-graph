---
last_updated: 2026-04-30
module: bench/optimize
title: Optimizer fixture size — 100-commander golden set is too small for trustworthy gradient signal
tags: [optimizer, fixture, calibration, golden-set, validation, fr10]
problem_type: best_practice
symptoms:
  - Optimizer accepts 20+ rule moves with multiple slamming to clamp_max=5.0
  - "+1.1% train improvement" claims that don't generalize
  - Self-test (FR10) fails on tail rules with rule contribution too small for a 2x perturbation to register
  - 25 of 60+ rules show up as dead-keys (fire on zero fixture commanders)
resolution_type: pattern
applies_when:
  - Running ``bench.py audit --optimize`` against the 100-cmdr canonical fixture
  - Considering whether to apply an optimizer proposal
  - Designing a fixture for a new optimizer or any tail-sensitive eval
created: 2026-04-30
---

# Optimizer fixture size matters more than fixture composition

When ``bench.py audit --optimize`` was first run end-to-end against the 100-commander canonical golden set, three things broke at once:

1. **Self-test (FR10) failed every run.** The planted-perturbation gate could not recover the baseline for whichever rule the seed-shuffled selector picked first. With seed=0 it failed on ``etbreplacement_copy_dbcopy_optional_tribal``; with seed=42 on ``counter_axis_feeder``. Same failure mode at α=0.3 / 0.5 / 0.7. This is the FR10 contract working — the optimizer refusing to ship a proposal it can't validate against ground truth.

2. **Bypassing the self-test produced a wild proposal.** ``--no-self-test`` accepted 20 moves over 2 sweeps with **6 of them slamming to clamp_max=5.0** (``counter_axis_feeder``, ``landfall_enabler``, ``lifegain_feeder``, ``monarch_synergy``, ``populate_stack``, ``subject_zone_feeder``). The grid wanted to keep going past the clamp, which is a strong "the optimum is outside the search box" signal — i.e., the gradient is noise.

3. **Train delta and held delta disagreed.** Train +0.0111, held +0.0052 — half. Held nDCG IMPROVED MORE than train nDCG (+0.0103 vs +0.0089), which is the opposite of normal. That's the held set being too small (20 commanders) to be a reliable counterweight, not "the proposal generalizes."

## What the 500-cmdr fixture exposed

Built via ``scripts/bootstrap_golden_set_500.py`` (top 500 legendary creatures by ``edhrec_rank`` with at least one EDHREC ``High Synergy Cards`` row).

| Metric                 | 100-cmdr  | 500-cmdr  | What it tells us |
|------------------------|-----------|-----------|------------------|
| dead_keys              | 25        | 9         | -64% — most tail rules now fire |
| moves to clamp_max=5.0 | 6         | 0         | grid runaway eliminated |
| accepted moves         | 20        | 7         | optimizer wants smaller changes |
| train Δ                | +0.0111   | +0.0005   | 100-cmdr's "improvement" was noise |
| held Δ                 | +0.0052   | -0.0001   | held delta on 500 is statistically zero |
| n_iterations           | 2         | 1         | converged immediately |

The 500-cmdr fixture exposed the truth: the current ``_RULE_QUALITY_MULTIPLIER`` values are near-optimal for the composite objective at α=0.5. The 100-cmdr proposal's "improvement" was overfitting to noise in a fixture too narrow to disambiguate weight changes for tail rules.

## The structural reason

The composite objective at α=0.5 is ``α · mean_per_commander_nDCG@30 + (1-α) · hidden_gem_hit_rate``. Both terms have variance proportional to ``1/sqrt(n_commanders)``. With n=100 (80 train, 20 held), the noise floor is high enough that:

- Tail rules firing on <5 commanders look identical at any weight in [0.5, 2x] of baseline — flat region of the objective surface
- 80-cmdr train splits don't have enough variance to distinguish a 2x weight perturbation from baseline (the FR10 self-test failure mode)
- 20-cmdr held splits don't reliably penalize overfitting moves

Going to n=500 (400 train, 100 held) reduces noise by ``sqrt(5) ≈ 2.24x``, which is enough to:
- Push most "rule fires but flat" cases below the noise floor
- Give the held split enough power to flag overfitting (held Δ on 500 was -0.0001 vs +0.0052 on 100)
- Cut dead-keys by 64% — many tail rules fire on at least one commander somewhere in the top-500 popularity tail

## Pattern: fixture size > fixture composition (for this workload)

Initial intuition was that **stratified-by-archetype** selection (ensure each rule fires ≥ N times) would beat **top-N by EDHREC rank**. That intuition was wrong for this workload. The simple top-500 by popularity captured most archetypes naturally — only ~5 narrow tribal rules (firebending, training, party, mentor, prowess) are still dead keys at n=500, and those would require deliberate stratification or are genuinely too narrow to ever optimize.

For an optimizer or any eval that integrates over many rules, fixture **size** dominates fixture **composition** until you hit the structural tail. After that, stratification is the next lever — but you only need it once size has done the easy work.

## What to commit

- **``--optimize`` defaults to the 500-cmdr fixture** when ``--fixture`` is at the global default. Explicit ``--fixture`` is honored as-is. Other audit modes (``--repin``, ``--expect-identity``, drift check) continue to use the 100-cmdr canonical for fast pre-commit hooks.
- **Bootstrap script regenerates after data changes.** Run ``scripts/bootstrap_golden_set_500.py`` after every ``import_cardsfolder.py`` or scoring config change. Regenerable, not hand-curated.
- **The 100-cmdr fixture stays canonical for tests and audits.** Pre-commit hooks and ``bench.py audit`` need the small fast fixture. The 500 is for serious optimizer runs, not a replacement.

## Anti-pattern: judging proposals by train delta alone

The 100-cmdr proposal's +0.0111 train delta looked like a meaningful improvement. It wasn't. Without a held delta from a fixture large enough to be statistically meaningful, train deltas at the +0.5 to +1.5% level are noise. **Always check held delta on a fixture ≥ 5x the planned change magnitude before applying any optimizer proposal.**

## Related

- ``docs/solutions/best-practices/optimizer-perf-profile-2026-04-30.md`` — same module; methodology for measuring before chasing perf estimates. Same lesson, different axis: measure before trusting reviewer / optimizer claims.
- ``memory/feedback_audit_metric_too_coarse.md`` — the original observation that the audit histogram alone was too coarse; this note is its dual for the optimizer's gradient signal.
