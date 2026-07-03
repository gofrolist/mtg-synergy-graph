---
title: gy_fuel vocabulary expansion (Surveil / DigUntil) DECLINED at Stage 0 — corrected supply is optional-cantrip + combo noise
date: 2026-07-03
category: best-practices
module: complement_rules
problem_type: null_result
component: scoring_engine
symptoms:
  - "bench.py audit --unknowns shows large UNKNOWN port buckets (effect.Dig 828, effect.Mill 503, effect.Surveil 200, effect.DigUntil 149) that look like untapped synergy signal"
  - "Hypothesis: plug the card-selection / graveyard-fuel cluster into the existing gy_fuel axis to recover NO_RULES / DATA_GAP forensics misses"
root_cause: premise_false
resolution_type: declined_with_evidence
severity: medium
related_components:
  - port_graph
  - bench
tags:
  - kill-test
  - null-result
  - no-rules-bucket
  - vocabulary-expansion
  - gy-fuel
  - stage0
  - axis-feeder
applies_when:
  - "considering vocabulary expansion of bench.py audit --unknowns port shapes into an existing axis-feeder rule"
  - "proposing card-selection or self-mill supply rules (Surveil / DigUntil / Scry / Dig / Mill)"
  - "tempted to read a large UNKNOWN rank_weight as addressable synergy signal"
brainstorm_ref: docs/brainstorms/2026-07-03-gy-fuel-surveil-digmill-requirements.md
---

# gy_fuel vocabulary expansion — DECLINED at Stage 0

## What was tested

Origin `/ce-brainstorm` on "vocabulary expansion for the top UNKNOWN
port shapes." The lead candidate was the card-selection / graveyard-fuel
cluster (`effect.Dig` 828, `effect.Mill` 503, `effect.Scry` 385,
`effect.Surveil` 200, `effect.DigUntil` 149) plugged into the existing
`gy_fuel_feeder` axis (fires on ~24 `cost.exile_from_grave`,
`cost_target='any'` commanders — Araumi, Osgir, Aphemia…). Pressure-test
narrowed it to the one mechanically-honest subset: add `Surveil≥3` and
`DigUntil→Graveyard` self-mill supply tiers to `gy_fuel_feeder`. Gated by
a Stage-0 pre-flight candidate count before any scoring code.

## Two mechanical premises that did not survive inspection

1. **"UNKNOWN" ≠ "unconsumed."** `--unknowns` classifies the typed
   `port_nodes` view; the Python-helper rules query `card_ports`
   directly. `gy_fuel_feeder` **already consumes `effect.Mill`** (503) —
   it appears in the UNKNOWN list while being scored. Raw UNKNOWN
   `rank_weight` is "not-yet-in-typed-vocabulary," not addressable signal.

2. **The "cluster" bundles mechanically unlike shapes.** `Dig` (828) and
   `Scry` (385) are card selection — they never touch the graveyard;
   plugging the 828-card headline into a GY axis is a category error.
   Only `Mill` / `Surveil` / some `DigUntil` fill the yard.

## Stage-0 measurement result (corrected filters)

`ce-doc-review` (4 personas: coherence, feasibility, product-lens,
adversarial) surfaced two filter defects that the corrected pre-flight
then confirmed:

| Tier | Naive | Corrected | Why it collapses |
|---|---|---|---|
| Surveil ≥3 | 17 | 17 (**optional-fill**) | Surveil bins *any number incl. 0*; the `≥3` gate that made forced-Mill safe is decorative. The 17 are mostly selection cantrips (Connive, Otherworldly Gaze, Taigam's Scheming, Plan the Heist) — the flood shape the gate exists to block. |
| DigUntil→GY self | 13 | **4** | Opponent-mill hides in `Defined`/`ValidTgts:Player`/`IsCurse`, not the literal `Opponent` token (only 7/20 carry it). Corrected filter rejects 16/20 (Balustrade Spy, Mirko Vosk, Undercity Informer…) incl. Hermit Druid's `AILogic:DontMillSelf` combo. Survivors (Gamekeeper, Mirror-Mad Phantasm…) are combo/value, not incremental fuel. |

**Corrected net-new supply: ~21 cards**, dominated by optional cantrips
and combo pieces. Expected NDCG@30 movement near-zero-to-negative;
several survivors re-introduce the exact cantrip-flood / wrong-target
failure modes the rule's `≥3`/self-only filters exist to prevent.

## Why it was DECLINED before writing code

- The corrected, archetype-appropriate self-mill supply does not clear a
  plausible-movement Stage-0 bar.
- The kill-test gate was **structurally biased to DECLINE**: a hard
  no-regression bar over 24 gated commanders with near-zero upside gives
  24 chances to trip a noise-band regression and none to gain.
- Building would spend a full build + audit + re-pin cycle to confirm a
  null already visible from a 10-minute measurement.

## What this closes and what stays open

- **Closed:** the Surveil/DigUntil vocabulary-expansion-into-gy_fuel
  frame. Do NOT re-propose adding `--unknowns` card-selection/self-mill
  buckets to the gy_fuel axis without a corrected Stage-0 count first.
- **Reinforced (3rd independent confirmation this session):** axis-feeder
  *breadth* is the tapped-out lever (resource-flow demand DECLINE; the
  Slimefoot scoping — feeders see the payoff but the supply→payoff *link*
  is missing; now this). The open lever remains **archetype-payoff /
  subtype-link detection** — see
  `memory/project_no_rules_archetype_gap.md`. Cliff-prone; needs its own
  Stage-0-gated brainstorm.

## Method notes (what made the DECLINE cheap and trustworthy)

- Adversarial + feasibility personas caught two filter defects
  (Surveil optionality; DigUntil target-encoding) that biased the naive
  count *upward* — an adversarial matcher check against named canonical
  cards belongs in every Stage 0 (same lesson as the resource-flow cycle).
- A large `--unknowns rank_weight` is a vocabulary-coverage signal, not
  an addressable-synergy signal. Verify consumption on the `card_ports`
  path (not the typed view) before treating a bucket as untapped.
- Promote the pre-flight candidate count to a **blocking** Stage-0 gate,
  not a deferred "open question" — it is the highest-information,
  lowest-cost step and usually decides go/no-go.
