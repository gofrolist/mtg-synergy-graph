---
last_updated: 2026-07-02
module: scoring
title: Lift-normalization probe declined at the R0 kill-test — panel baseline points the wrong way on flood commanders (null result)
tags:
  - scoring
  - lift-normalization
  - panel-baseline
  - null-result
  - kill-test
  - flood-as-archetype
  - plan-2026-07-02-003
problem_type: best_practice
resolution_type: reference
applies_when:
  - Considering ranking by score(cmdr, card) − λ·panel_mean(card) or any expected-baseline subtraction (the C1 axis)
  - Considering z-score / variance normalization of the synergy score against a commander-panel distribution
  - Tempted to rebuild EDHREC's synergy = deck% − baseline% formula from mechanical scores instead of co-play data
  - Looking for the OUTRANKED lever after the calibration track's closure (calibration-track-null-result-2026-07-02.md)
created: 2026-07-02
plan_ref: docs/plans/2026-07-02-003-feat-lift-normalization-probe-plan.md
brainstorm_ref: docs/brainstorms/2026-07-02-lift-normalization-requirements.md
---

# Lift-normalization probe declined at the R0 kill-test

The C1 lift-normalization cycle (plan 2026-07-02-003) **DECLINED at
Unit 2** — the designed cheap exit. No scoring integration was built:
Units 3–7 never ran, no flag, no `UniversalScore` field, no panel
artifact. Total cost: one rider fix that ships regardless (the
`--repin` gem-refresh plumbing, commit `92fba39`) plus four scratchpad
scripts. The scoring path is untouched (`--expect-identity` PASS at
config `34a9d110579d`, tag `pre-lift-normalization`).

This closes the **baseline-subtraction axis** (ideation #4, FUNDED)
the way `calibration-track-null-result-2026-07-02.md` closed the
weight-layer axis: measured, at every strength, on the population it
was funded to fix.

## The hypothesis and how it failed

**Load-bearing hypothesis (R0):** cards that displace EDHREC labels on
monoculture commanders have HIGHER panel_mean (their score averaged
over a fixed commander panel) than the labels they displace — so
subtracting `λ·panel_mean` demotes displacers and recovers labels.

**Evaluation set:** the 23 monoculture commanders (≥85% single
displacer family) named in the `pre-lift-normalization` tag. Panel:
two compositions (EDHREC-top-200 with per-identity caps of 8;
identity-stratified round-robin, 200 each), four denominator variants
(all-panel vs color-legal × absent=0 vs excluded), leave-one-out
on/off — 16 variants. Panel values are synergy-only
(`score − staple − circuit − cmc − rank − embedding`, the planned
`synergy_total` pin), built with a shared `CandidateCache`.

Three readouts, in order of increasing decisiveness:

1. **Mean inequality (the R0 formulation): a wash.** Aggregate gap
   between displacer panel_mean and displaced panel_mean is ±0.01 on a
   0.18–0.20 scale, sign-unstable across denominator variants; holds
   for only 9–14 of 21 commanders. Compositions A and B give nearly
   identical numbers — composition is not the lever. The inequality
   INVERTS 2–3× on the tribal-flood sub-shape: Azusa 0.026 vs 0.071,
   Kess 0.019 vs 0.041, Chatterfang 0.013 vs 0.031, Marrow-Gnawer
   0.009 vs 0.019. It holds only on engine monocultures (Urza 0.048 vs
   0.023, Feather, Talrand, Sram, Vorel).

2. **Median-crossing recovery: superficially encouraging, one-sided.**
   Within each commander's (displacers ∪ displaced) pool, subtractive
   lift lifts 33/99 displaced labels above the displacer median (raw:
   7/99); z-score lifts 51/99. This readout ignores outflow — labels
   already in the top-30 that the transform pushes out — and outflow
   is what decides NDCG.

3. **Full NDCG@30 simulation (the gate metric): DECLINE at every λ.**
   Offline re-rank of all 100 golden-set commanders by
   `total − λ·panel_mean` (panel A, absent=0), production tiebreaks,
   all-sections graded labels:

   | λ | aggregate NDCG@30 | Δ vs raw 0.233623 | cliffs < −0.05 | gains > +0.05 | monoculture-23 mean Δ |
   |---|-------------------|-------------------|----------------|---------------|----------------------|
   | 0.25 | 0.220017 | −0.013606 | 14 | 1 | −0.0358 |
   | 0.50 | 0.214516 | −0.019107 | 19 | 2 | −0.0427 |
   | 0.75 | 0.208623 | −0.025000 | 20 | 1 | −0.0545 |
   | 1.00 | 0.204953 | −0.028669 | 23 | 2 | −0.0588 |

   Worst cliffs at λ=0.25: Bruvac −0.178, Kess −0.172, **Edgar Markov
   −0.128** (the adversarial review's "Edgar arithmetic" prediction,
   confirmed numerically), Ur-Dragon −0.119, Azusa −0.095, Kaalia
   −0.080, Krenko −0.072. The cliff population IS the flood-as-
   archetype population. Every gate in the plan fails at every λ:
   aggregate below the −0.010 goal-aligned floor, cliff gate fired
   14–23 times.

   **z-score fallback (R11), same harness:** aggregate 0.233623 →
   0.122122 (Δ −0.1115), 65 cliffs, monoculture mean Δ −0.1357.
   Catastrophic — dividing by panel_std amplifies narrow spikes
   (small-σ cards) exactly like the small-N IDF saturation cliff in
   `color-conditioned-idf-null-result-2026-07-02.md`, one level up.

## What information the lift baseline lacked

`panel_mean` measures how BROADLY a card's mechanics fire across
commanders. But the OUTRANKED failure on flood commanders is caused by
cards whose mechanics fire NARROWLY — tribal payoffs, single-engine
pieces — which have LOW panel_mean. Subtracting the baseline therefore
REWARDS the flood displacers relative to the broadly-good labels they
displace. The subtraction can only demote broadly-generic cards, and
the displaced labels on flood commanders ARE the broadly-good cards.

EDHREC's `synergy = deck% − baseline%` works because its numerator is
human co-play frequency — evidence that people actually run the card
with that commander. Our numerator is mechanical affinity, which
already overcounts narrow matches (the flood problem itself);
subtracting a mechanical baseline amplifies the overcount instead of
correcting it. A mechanics-only rebuild of the EDHREC formula is
structurally the same optimization as the hidden-gem axis — it
surfaces commander-specific obscura — and structurally opposed to
EDHREC-label NDCG on flood commanders. Same gem-up/NDCG-down tension
the calibration track died on, now measured from the baseline side.

Two secondary assumptions also failed:

- **"The lift taxes staples."** Staples are nearly rule-invisible, so
  their panel_mean is LOW (Sol Ring 0.0156, Arcane Signet 0.0133 vs
  displacer pool mean 0.0246, median 0.0215). The lift barely touches
  them.
- **"Panel composition decides who gets taxed."** Compositions A and B
  produced near-identical panel_means; the denominator variants moved
  magnitudes but never fixed the direction. The failure is in what
  panel statistics of a mechanical scorer CAN express, not in how the
  panel is chosen.

## Where the OUTRANKED lever goes next

Per the plan's deferred-tasks note: with both the probe and its named
fallback declined, the funded sibling is **role/quota portfolio
selection** — treat the top-30 as a portfolio with per-role quotas
instead of a pointwise-scored list. The flood problem is a
LIST-composition problem (30 slots, one family), not a per-card
scoring problem; every pointwise transform measured so far (weight
haircuts, tiers, pool scaling, baseline subtraction, variance
normalization) moves whole cohorts together and cliffs whichever
commanders genuinely demand the cohort. A quota operates on the list
directly and can cap a family without re-scoring it.

Methodology note for that cycle: the offline re-rank simulation used
here (scratchpad `r0_ndcg_sim.py` — panel/statistic pass + re-rank +
`compute_ndcg` against graded labels, ~3 minutes for 100 commanders)
is the cheap kill-test template. Run it BEFORE any integration; it
predicted every gate outcome without touching the scoring path.

## Artifacts

- Rider shipped: `--repin` gem-refresh plumbing + pre-flight EDHREC
  validation (commit `92fba39`); both fixtures re-pinned with fresh gem
  values (100-cmdr agg 0.8160, 500-cmdr 0.7123); tag
  `pre-lift-normalization` carries the full baseline ledger.
- Scratchpad (session-local, not committed): `r0_kill_test.py`
  (16-variant inequality readout), `r0_fallback_check.py`
  (median-crossing recovery), `r0_ndcg_sim.py` (λ sweep NDCG),
  `r0_zscore_sim.py` (fallback closure), with their reports.
- Units 3–7 of the plan: never run (no panel builder, no
  `lift_penalty` field, no flag, no hash change).
