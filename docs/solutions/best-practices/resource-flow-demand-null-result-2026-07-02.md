---
title: Resource-flow demand mechanism DECLINED at Stage 0 — NO_RULES miss mass is mostly mechanically unreachable
date: 2026-07-02
category: best-practices
module: demand_coverage
problem_type: null_result
component: scoring_engine
symptoms:
  - "NO_RULES forensics bucket at 43% of EDHREC-label misses; worst commanders have collapsed scored universes (Yawgmoth 190 activated candidates)"
  - "Commander demand ports (cost.sacrifice, cost.discard, cost.tap) look under-served by the rule layer"
  - "Hypothesis: a general commander-demand → candidate-supply mechanism over resource flows would recover the misses"
root_cause: premise_false
resolution_type: declined_with_evidence
severity: medium
related_components:
  - complement_rules
  - port_graph
  - bench
tags:
  - kill-test
  - null-result
  - no-rules-bucket
  - demand-supply
  - addressable-share
  - stage0
applies_when:
  - "considering new rules or mechanisms to recover NO_RULES forensics misses"
  - "proposing commander-cost demand rules (sacrifice/discard/tap/pay_life feeders)"
  - "estimating addressable share of any miss bucket before building"
plan_ref: docs/plans/2026-07-02-005-feat-resource-flow-demand-plan.md
---

# Resource-flow demand — DECLINED at Stage 0

## What was tested

Plan 2026-07-02-005 (origin brainstorm twice-reviewed the same day):
a general commander-demand → candidate-supply mechanism over five
resource flows (sacrifice fodder, discard fuel, untap capacity,
life, graveyard bodies), targeting the NO_RULES forensics bucket
(1,137 misses, 43% of all EDHREC-label misses — the largest OPEN
bucket after OUTRANKED was closed as justified divergence). Funding
was gated by a Stage 0 evidence pass with bars pinned blind, before
any measurement: cohort addressable share ≥ 0.25 AND fixture-wide
reach ≥ 100 labels.

## The kill-test design (all boundaries pinned before measurement)

- **Provisional pairing table** committed BEFORE the share
  computation existed (commit `b212bb8`), authored from registry
  gates + port-shape vocabulary only, no forensics miss lists
  consulted; each supplier pool bounded ≤ 2,000 cards; null-model
  comparison (same-size random pools, seed 17) required.
- **Three-way port classification**: unconsumed / consumed-with-
  material-yield / consumed-but-starved (< 1,000 activated
  candidates OR zero top-30 delivery), consumption = RULE_GATES
  minus CARD_LEVEL_RULES ∪ interpreter compiled gates.
- **IDF-burial criterion pinned** (delivered-but-zero-top-30 with
  pool ≥ 500); burial-only-reachable misses excluded from the
  numerator.
- **Cohort defined by rule** (< 1,000 activated candidates → 26
  commanders), not by the seven hand-picked worst names (three of
  which — Phenax, Slimefoot, Osgir — fell OUTSIDE the rule-defined
  cohort, vindicating the rule-based denominator).

## Result (run 2026-07-03T01:49Z, 100-commander canonical fixture)

| Bar | Pinned | Measured | Verdict |
|---|---|---|---|
| Cohort addressable share | ≥ 0.25 | **0.0828** (36/435; 1 burial-excluded) | FAIL |
| Fixture-wide label reach | ≥ 100 | **47** (of 1,137; null 15) | FAIL |
| Exceeds null model | required | 0.0828 vs 0.0184 (4.5×) | met |

Routing per the pinned table: **DECLINE**. The feeder-widening
reroute branch was unreachable (both bars must be met), and its
premise also failed: the yield diagnosis showed shipped feeders
already deliver (Araumi's `gy_fuel_feeder` places 29 of its 107
candidates in her top-30) — the missed labels are simply not
supply-shaped.

## Why it failed (mechanism)

The origin document named the rival hypothesis explicitly and Stage
0 confirmed it: **most NO_RULES miss mass is generic goodstuff /
deck-function inclusions whose correct mechanical score under this
architecture is zero.** 398 of 435 cohort misses are unreachable by
ANY of five well-narrowed demand→supply pairings. The flows carry
real signal (4.5× null), but the recoverable mass tops out at ~47
labels fixture-wide — an order of magnitude below what justifies a
mechanism build (new GATE_OPS leaf ops, seed grammar, overlap
governance against 7+ shipped feeders, kill-test cycle).

Honest caveat recorded at decision time: the `wrong_supply_cards`
feeder diagnosis is partially tautological (NO_RULES misses have
zero tensor rows by definition, so a delivering rule's candidates
can never intersect them); the load-bearing evidence is the
reachability counts, not the diagnosis labels.

## What this closes and what stays open

- **Closed**: uniform demand→supply resource-flow mechanisms for the
  NO_RULES bucket; commander-cost feeder expansion as an NDCG
  recovery lever (the demand ports are real but the labels aren't
  reachable through them). Together with the OUTRANKED closure
  (portfolio cycle: 0.0518 addressable) this means BOTH large miss
  buckets are now measured mostly-unaddressable under the
  mechanical-interaction architecture — the honest reading is that
  aggregate NDCG@30 against EDHREC labels is near its architectural
  ceiling, and further EDHREC-alignment pushes have steeply
  diminishing returns.
- **Open**: the hidden-gems axis (BM25 revisit measured +0.0407 gem
  rate, documented possible SHIP under gems-first criteria — the
  natural next cycle); granted-ability demand extraction (Phenax's
  AddAbility engine — an extraction gap, small); mechanics-native
  quality work that doesn't gate on EDHREC labels.

## Surviving infrastructure (all inert on the scoring path)

- `scripts/demand_coverage.py` + `src/mtg_synergy_graph/bench/demand_coverage.py`
  — standing Stage-0 instrument: three-way demand classification,
  feeder-yield diagnosis, addressable-share with null model; first
  production consumer of the `CARD_LEVEL_RULES` subtraction and
  `attributable_rules_for_port()` substrate. Re-run after major rule
  additions or data refreshes to re-measure under-served demand.
- `src/mtg_synergy_graph/data/resource_flows_seed.json` +
  `port_graph/resource_flows.py` — the pairing table and strict
  loader; deliberately absent from ScoringConfigInputs (committing
  or editing it never flips the config hash).
- Reports: `.audit/demand_coverage/report.{json,md}` (gitignored,
  regenerable in ~2 minutes).

## Prevention / method notes for future cycles

- Pin BOTH the thresholds AND every classifier boundary blind — this
  cycle pinned the starved floor, burial criterion, pool bound, and
  pairing table before measurement, which is why the DECLINE is
  trustworthy and cheap (Phase B never started).
- A relative share bar alone is insufficient — the ≥100-label
  absolute floor is what exposed the tiny total mass behind a
  seemingly-promising smoke result (Yawgmoth 19/29 reachable was the
  outlier, not the population).
- Define evidence cohorts by rule, not by hand-picked worst cases;
  three of seven named commanders fell outside the rule.
