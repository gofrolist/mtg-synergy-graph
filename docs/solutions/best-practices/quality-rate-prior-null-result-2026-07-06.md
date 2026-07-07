---
last_updated: 2026-07-06
module: universal_scorer
tags:
  - quality-prior
  - magnitude
  - rate
  - effect-per-mana
  - outranked
  - kill-test
  - null-result
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any card-quality / power-level / rate prior on the OUTRANKED bucket (multiplicative or additive, any deterministic proxy)
  - Considering using card_ports.amount magnitudes as a scoring signal
  - Looking for an OUTRANKED lever after the lift-normalization and portfolio-selection DECLINEs — all three lever classes are now measured; read this before proposing a fourth
created: 2026-07-06
plan_ref: docs/plans/2026-07-06-001-feat-structural-gap-remediation-plan.md
---

# Magnitude quality prior (effect-per-mana rate multiplier): DECLINED at the golden-100 screen

**Cycle:** plan `docs/plans/2026-07-06-001-feat-structural-gap-remediation-plan.md`
Phase C — the third OUTRANKED lever class, tried with an explicit
expected-loss framing after reweighting (plans 2026-07-02-002/003) and
re-ranking (plan 2026-07-02-004) were DECLINED.
**Verdict: DECLINE at the Stage-1 screen; the golden-500 stage was never
reachable.** Zero scoring-path changes; `--expect-identity` PASS throughout.

## What was tested

A bounded multiplicative prior on the production total:
`total × (1 + q·tanh(rate/r0))`, with `rate` a deterministic effect-per-mana
signal from data already in synergy.db: `card_ports.amount` magnitudes
(effect/static ports; X/Y/Z→2.5, All→4.0, numeric clamped [0,6]), an
engine-shape marker (any trigger/cost port → weight 1.0, pure one-shots 0.5),
divided by max(cmc, 1). No EDHREC, no popularity, no curation.

Instrument: `scripts/quality_sim.py` (`bench/quality_sim.py`), wrapping
`portfolio_sim.build_commander_sim` with a q=0 bitwise self-check per
commander. Gates pinned before the sweep: H_500q=0.0136 (q=0 NDCG band,
identical to both sibling instruments' page-based bands), G_500q=0.0355
(matches the portfolio_sim gem-band precedent).

## Results (grid 3×3: q ∈ {0.05, 0.1, 0.2} × r0 ∈ {1.0, 2.0, 4.0}, golden-100)

ALL cells negative: mean ΔNDCG@30 −0.0228 (best, q=0.05/r0=4) to −0.0430;
**17–35 cliffs (<−0.05) per 100 commanders** vs the zero-cliff requirement;
traps cliff hard (Kess −0.145..−0.42, Edgar −0.10..−0.15). Gems roughly flat
at q≤0.1, negative at q=0.2. The Phase C gate (golden-500 Δ ≥ +0.0136 AND
zero cliffs AND gems within −0.0355 AND traps ≥ −0.05) is unreachable.

## Interpretation

The rate proxy is anti-correlated with EDHREC agreement at the list head:
high-amount repeatable engines are exactly the mechanically-loud cards the
scorer already over-ranks, so multiplying by rate amplifies the flood rather
than lifting outranked labeled cards. Kess (−0.42) is the archetype case:
her spell-density flood is full of high-rate cards. The head of the ranking
is also extremely flat (golden-set #30 ≈ #100 in total), so even a bounded
multiplier reshuffles the top-30 violently — the same head-instability that
killed the deck-context additive term the same day
(deck-context-null-result-2026-07-06.md).

**OUTRANKED lever-class scoreboard (all measured dead):**
1. Reweighting existing contributions — lift normalization (plans 002/003).
2. List-level re-ranking — portfolio selection (plan 004).
3. New-information pointwise prior — magnitude rate (this cycle).

**Signal-crudeness caveat (PR #101 review, 2026-07-07):** the engine-shape
marker is near-vacuous — 67.5% of amount-bearing cards carry a trigger/cost
port and get the 1.0 engine weight, and ~half of all trigger ports are
one-shot ChangesZone (ETB/dies) shapes. What this cycle measured dead is
therefore "printed-amount rate with a crude repeatability split," not a
well-formed engine-vs-one-shot signal; a chain-aware repeatability
classification would be a materially different (untested) signal.

What this null result does NOT rule out: a quality signal used as a
*tiebreak-scale* term (≪ the ~0.006 gap between adjacent head ranks) rather
than a multiplier — untested, expected value low; and simulation-derived
quality (Forge goldfish oracle, deferred in the plan) — a categorically
better-calibrated signal than printed amounts. Any future attempt must
pre-pin gates on `scripts/quality_sim.py`, which ships as standing infra.
