---
last_updated: 2026-07-09
module: universal_scorer
tags:
  - calibration
  - staple-bonus
  - weight-optimizer
  - forensics
  - outranked
  - staple-only
  - commander-independence
  - null-result
problem_type: null-result
resolution_type: reference
applies_when:
  - Tempted to improve golden-set NDCG by retuning _RULE_QUALITY_MULTIPLIER
    (bench.py audit --optimize) on the current rule set
  - Tempted to reform the staple bonus (heuristics.STAPLES / staple_bonus)
    to un-bury real EDHREC picks
  - Interpreting a forensics OUTRANKED `staple_only` miss and about to read
    it as "a staple card displaced the real pick"
  - Deciding between a calibration fix and a coverage (new-rule) cycle
---

# Both "tune what we already have" levers are exhausted — the headroom is coverage

After three consecutive commander-*independent* rule DECLINEs
([[team-anthem-payoff-null-result-2026-07-08]],
[[attack-reward-evasion-null-result-2026-07-09]],
[[x-cost-scaler-null-result-2026-07-09]]), a 2026-07-09 investigation asked
whether the golden-set ceiling could be raised by **calibration** instead of new
rules. Two candidate levers were measured; **both came back negative.** The
finding: there is no calibration shortcut — the ~88% of misses that are
"no rule connects the commander to the EDHREC card" can only be closed by
**coverage**, and (per the three DECLINEs) only by *commander-dependent*
coverage.

## Lever 1 — weight retuning (optimizer): no generalizable gain

`bench.py audit --optimize` (Coordinate Ascent over `_RULE_QUALITY_MULTIPLIER`,
500-cmdr fixture) converged in a single sweep:

| split | NDCG baseline | NDCG final | Δ |
|---|---|---|---|
| train (400) | 0.09665 | 0.09763 | +0.00098 |
| **held-out (100)** | 0.10022 | 0.09989 | **−0.00033** |

The train gain is negligible **and does not transfer** — held-out goes negative.
The 7 accepted steps (`cardpower_axis_feeder` 3.5→1.75, `cost_reduction_target`
0.5→0.25, `combat_enhancer` 0.7→1.05, …) are a textbook overfit; the proposal is
**not shippable** and `scoring_weights.json` was left untouched. **The existing
per-rule multipliers are already at a local optimum for this axis.** (The
self-test also aborts on the narrow `doctor_s_tribal` rule — expected: a
low-contribution tribal rule cannot detect a planted 2× perturbation on the
train split; run with `--no-self-test` to read the held delta, which is the
number that actually decides the question.)

## Lever 2 — staple-bonus reform: wrong mechanism, does not flood

**First, a correction that motivated the check:** a forensics OUTRANKED
`staple_only` sub-tag does **NOT** mean "a staple card displaced the real pick."
Per `bench/forensics.py`, it tags the **missed EDHREC card**: we ranked it 61+
because the persisted tensor holds **zero rule rows** for the (commander, card)
pair, so it scored only on flat non-rule channels. OUTRANKED-`staple_only` (46%)
and NO_RULES (42%) are therefore the **same disease** — no mechanical rule
connects the commander to the card — ~88% of all golden-100 misses.

The staple bonus itself is a flat `0.01` tiebreak on a ~36-card curated set
(`heuristics.STAPLES`, 6 colourless + 5×5 colour), explicitly "below any
mechanical synergy bucket." Empirical top-30 occupancy across all 100 golden
commanders (scratch probe over `score_all_universal`):

- **pure-staple slots (staple bonus, 0 rules): 16 of 3000 (0.53%)**, avg **0.16
  per commander**;
- **all 16 belong to one commander — Xenagos, God of Revels** — who has almost
  no rule coverage, so staples fill the vacuum. For 99/100 commanders,
  pure-staples occupy **zero** top-30 slots.

Reforming the staple bonus would move exactly one pathological commander.
**It is not the flood lever.**

## Consequence

The flood the three DECLINEs kept feeding is not a weight-calibration artifact
and not a staple-over-credit artifact — it is structural: broad,
commander-*independent* rule credit surfaces generic bodies that displace the
real (rule-disconnected) EDHREC picks. The only lever with real headroom is
**commander-dependent coverage** — credit that varies by a per-commander
attribute joined to a candidate attribute. The 2026-07-07 subtype-supply rule
(which passed its gate) is the existence proof; generalizing that join shape is
the live design direction. See [[tensor-single-owner-slot-2026-07-08]] for the
re-pin discipline any such cycle must follow.
