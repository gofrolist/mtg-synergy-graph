---
date: 2026-04-26
topic: tensor-weight-optimizer
seed: docs/ideation/2026-04-26-applying-built-tooling-ideation.md (Survivor 1)
status: draft (foothold M1)
---

# Requirements: Tensor-Driven Weight Optimizer (Foothold)

## Problem Statement

Manual editing of `data/scoring_weights.json` is the only active lever for moving NDCG@30, and the documented blocker is that "manual weight tuning" is the entire optimization loop. The persisted per-(commander, candidate, rule) contribution tensor produced by `bench.py audit` is a useful filter but does NOT support O(1) re-score by scalar dot-product: per `src/mtg_synergy_graph/bench/rule_ops.py:8-18`, contributions are stored pre-dampening, so the sum across rules diverges from `UniversalScore.score()` whenever one rule contributes >70% of a candidate's synergy total — exactly the multi-rule cards relevant for hidden gems. The optimizer therefore uses the tensor as a cheap top-K filter (per commander) and calls `score_all_universal()` on those K candidates to compute its objective at production fidelity.

The deprecated `scripts/weight_grid_search.py` proves the per-(cmdr, comps, labels) caching pattern works (~100x speedup vs naive), but it grid-searches 4 hand-picked dimensions against mean per-commander nDCG with no held-out and no gem-axis term. The 53 keys in `_RULE_QUALITY_MULTIPLIER` × the small 100-cmdr golden set demand a smarter search, a held-out validation gate, and an objective that doesn't drift toward EDHREC hivemind alignment (memory: `feedback_edhrec_not_goal.md`).

## Goals

1. Replace hand-edits to `_RULE_QUALITY_MULTIPLIER` with a Coordinate Ascent optimizer that uses the persisted tensor as a cheap top-K filter and live-re-scores those K candidates per commander to keep the objective production-faithful (no scalar-tensor fidelity gap).
2. Use a **composite objective** `α · mean_per_commander_nDCG@30 + (1-α) · hidden_gem_hit_rate`, default `α = 0.5`. Both terms live in [0, 1]; the blend pushes both axes simultaneously rather than treating gem as a floor anchored at the current value.
3. Validate against a held-out 80/20 split (fixed seed) — train objective must improve AND held-out objective must not degrade by more than `ε = 0.005` for a step to be accepted.
4. Output a candidate `data/scoring_weights.json` diff for human review — the optimizer NEVER writes weights to disk autonomously.
5. Be cheap enough to run iteratively (target: full sweep over 53 keys in <3 min using hybrid tensor-filter + live-re-score; planning to benchmark before locking).

## Non-Goals (deferred to M2 / M3)

- SPSA, LambdaMART, or any non-Coordinate-Ascent optimizer (M3).
- Tuning `_FLAT_WEIGHT_OVERRIDES` or any per-density-bucket extension (M2).
- Wiring `weight_hint` into the loop as a Bayesian prior — confirmed unconsumed in `src/mtg_synergy_graph/universal_scorer.py`; stays a dead field for this MVP (M2).
- SNIPS / IPS reweighting per commander (M2 or M3).
- Hard Lagrangian-barrier formulation of any axis (M3 if the composite blend in FR2 proves insufficient).
- Auto-revert / CI gating on regression — human reviews diff and runs `bench.py audit --repin --yes` manually.
- Full Survivor #2 (writer-side audit gate compiler with `--writer-trace` UX). M1 does NOT depend on the full UX; it depends only on the **predicate-compile prerequisite** (FR0 below) so declarative-rule attribution is correct in the optimizer's tensor reads. Full `--writer-trace` and per-rule auditor inspection deferred to a separate M.
- Cross-validation beyond a single 80/20 split (5-fold or LOO are M3 if overfitting symptoms appear).
- Optimizing comments / metadata in `scoring_weights.json` — only `value` fields move; `comment` fields are preserved verbatim.
- New tensor schema — reuses the existing persisted tensor as-is.

## Users and Scenarios

| Scenario | Who | Expected experience |
|---|---|---|
| Try to find better weights without hand-editing | Dev | `bench.py audit --optimize` runs against current `data/scoring_weights.json`, prints a per-rule diff with train/held-out composite-objective deltas plus per-axis nDCG and gem deltas, and writes `.audit/optimize_proposal.json` for inspection. No file mutated. |
| Accept the proposed diff | Dev | Dev manually copies values into `data/scoring_weights.json`, then runs `bench.py audit --repin --yes` per existing discipline. (No `--apply-proposal` shortcut in M1 — FR6 makes "optimizer never mutates `scoring_weights.json`" load-bearing.) |
| Reject the proposed diff | Dev | No-op. The proposal sits in `.audit/optimize_proposal.json` and is overwritten on the next `--optimize` run. |
| Investigate convergence | Dev | `.audit/optimize_history.csv` appended one row per attempted step (see FR7 for full schema). Standard SQL/CSV introspection. |
| Re-run optimizer later | Dev | `--optimize` resumes from current `data/scoring_weights.json` (NOT from scratch). Each run is incremental. |

## Functional Requirements

### FR0 — Prerequisite: declarative-rule predicate compilation for the auditor

Before M1 can ship, declarative rules in `DECLARATIVE_RULE_IDS` (currently the migrated `peer_tribal_keyword` family, ~16 rules) must have their `rules_seed.json` JSON predicates compiled into the auditor's per-port gate path so the persisted tensor's per-rule attribution is correct for them. Without this, the optimizer's hybrid scoring (FR1) would re-score using correct contributions for declarative rules but the tensor's top-K filter would mis-rank candidates, producing a mismatch between filter and re-score that would either blow up the cost (filter passes too many candidates) or silently drop hits (filter passes too few).

Scope: only the **auditor-side predicate compilation** — the `RuleInterpreter` already returns SQL fragments + Python callables; this hooks them into the tensor build path so declarative rules emit `port_nodes`-attributed contribution rows like Python rules do. No `--writer-trace` UX, no `bench.py audit --inspect RULE_ID` improvements (those are full Survivor #2, deferred).

This is the only part of Survivor #2 that M1 depends on. Estimated 1 day.

### FR1 — Coordinate Ascent over `_RULE_QUALITY_MULTIPLIER`

The optimizer iterates over **all 53 keys in `_RULE_QUALITY_MULTIPLIER`** (the dict already includes both Python-helper and declarative rules; the FR0 prerequisite ensures declarative-rule attribution in the persisted tensor matches Python-helper attribution, so all keys are optimized on equal footing). Iteration order is alphabetical by `rule_id`.

For each key, the optimizer sweeps a small **multiplicative** grid of perturbations applied to the **current value** of that key: `[0.5×, 0.75×, 1.25×, 1.5×, 2.0×]` (configurable). For example, a key currently at 1.5 is evaluated at `[0.75, 1.125, 1.875, 2.25, 3.0]`. The perturbation that maximizes the **composite objective** (FR2) on the training split is provisionally accepted; if it also passes the held-out gate (FR4), it is committed in-memory and the next key sweep starts from that updated value. All proposed values are clamped to `[0.01, 5.0]` (see FR4b).

**Per-evaluation scoring (hybrid filter + live re-score):**
1. For each commander in the relevant split, the persisted tensor produces a top-K candidate shortlist (default `K = 200`, configurable). The shortlist uses scalar-tensor sums with the proposed weight applied — this is fast and approximate.
2. `score_all_universal()` is called on those K candidates with the proposed weight vector to produce production-faithful scores including dampening, multi-rule bonus, pair bonus, anti-synergy, and embedding contribution.
3. The top-30 from the live-re-scored K are used to compute mean per-commander nDCG and `hidden_gem_hit_rate` for the composite objective.

K is sized so that production-faithful top-30 is always a subset of the tensor-filtered top-200 in steady state. Planning to validate this empirically: if the tensor-filter top-K misses any production-true top-30 candidate on the baseline config, increase K. (At baseline K=200 vs ~10k-card pools, the tensor-filter recall on top-30 is expected to be ≥99%; target validates that.)

If `_RULE_QUALITY_MULTIPLIER` contains a key for a `rule_id` that is not registered in either the Python-helper registry or `DECLARATIVE_RULE_IDS`, the optimizer logs a stderr warning naming the dead key and skips it (this signals a stale entry in `data/scoring_weights.json`; should not occur in steady state).

### FR2 — Composite α-blended objective

The objective per evaluation is:

```
objective(weights) = α · mean_per_commander_nDCG@30 + (1 - α) · hidden_gem_hit_rate
```

Defaults: `α = 0.5`. Both terms live in [0, 1] so the blend is meaningful without further normalization. Configurable via a single constant.

- **`mean_per_commander_nDCG@30`** uses EDHREC graded labels (hi_syn = 3.0, on_page = 1.0, others = 0) per commander; per-commander averaging avoids the aggregate-raw-DCG coverage bias (commanders with more EDHREC labels would otherwise dominate). The Saito & Joachims 2023 OPE caveat does not bite here — we are tuning weights in-policy against fixed labels, not comparing two ranking policies via off-policy estimation.
- **`hidden_gem_hit_rate`** is computed via the existing `src/mtg_synergy_graph/bench/hidden_gems.py` module on the same evaluation set.

The composite shape replaces the prior soft-floor `λ` formulation — gem is a co-equal axis, not a constraint. A weight move that gains nDCG at the cost of gem is rejected proportionally rather than only when gem crosses an absolute floor. Eliminates the unbacked `λ = 100` calibration.

### FR3 — Per-axis monotonicity tracking

While the optimizer maximizes the composite, the proposal output (FR6) reports BOTH per-axis values (`mean_ndcg_train/held`, `gem_train/held`) so the human reviewer can detect cases where the composite improved but one axis regressed. No automated rejection on per-axis regression alone — the composite is authoritative — but the proposal flags any per-axis regression > 0.005 as a stderr warning.

### FR4 — Held-out validation

The 100-commander golden set is split **once** into 80 train + 20 held-out commanders using a fixed seed. The split is **stratified by archetype tag** (tribal / graveyard / voltron / spell-density / lifegain / token / counter / aristocrats / unbucketed) rather than color identity, because color identity is a weak proxy for which rules fire. Stratification at split time costs ~0 (one extra sort) and prevents the seed-=-42-by-fiat skew flagged in review.

A step is accepted iff:

- Train **composite objective** strictly improves, AND
- Held-out **composite objective** does NOT degrade by more than `ε = 0.005` (calibrated to ~1σ noise on a 20-commander sample of a metric in [0,1]).

If either condition fails, the step is rejected and the previous value is restored.

Per-axis held-out values (mean nDCG, gem rate) are logged regardless. Cumulative held-out objective drift is checked at sweep boundaries: if cumulative held-out delta from baseline drops below −0.005 at the end of any sweep, the optimizer reverts that sweep's accepted steps and terminates with `partial_sweep: true` in the proposal.

Per-color-identity bucket DCG reporting is **dropped from M1**: at n=20 the per-bucket sample sizes (~4-7 commanders) are pure noise. If skew diagnostics become necessary, defer to M2.

### FR4b — Weight clamp range

All proposed `value` fields after a coordinate step are clamped to `[0.01, 5.0]`. Values below 0.01 round to 0.01; values above 5.0 round to 5.0. This prevents runaway weight drift on a single sweep. The clamp range is configurable via a constant, not a CLI flag.

### FR4c — Tensor-staleness precondition

The optimizer **refuses to run** if `compute_config_hash(get_scoring_config_inputs())` does not match the hash recorded in the persisted tensor's `rule_contributions_config` row. Error message names the mismatch and instructs the user to run `bench.py audit --repin --yes` first. The optimizer never auto-rebuilds the tensor.

### FR5 — Termination

Optimizer terminates on whichever of the following fires first:

- **Convergence**: a full sweep over all 53 keys completes with zero accepted steps. The optimizer always completes the in-progress sweep before declaring convergence — early exit on partial sweeps is forbidden so the final state is consistent across keys.
- **Sweep cap**: 5 full sweeps completed (whether or not all accepted steps).
- **Wall-clock**: 5 minutes elapsed (hard self-abort, emits best-so-far proposal even if mid-sweep — proposal output flags `partial_sweep: true` so the reviewer can choose to discard).

### FR6 — Output: candidate diff, not in-place mutation

The optimizer writes a single artifact: `.audit/optimize_proposal.json` with:

- `baseline_config_hash` — `compute_config_hash` of input weights
- `proposed_config_hash` — same after proposed deltas
- `per_rule_diffs` — list of `{rule_id, old_value, new_value, composite_delta_train, composite_delta_held, ndcg_delta_train, ndcg_delta_held, gem_delta_train, gem_delta_held, accepted_iteration}`
- `aggregate_train_composite_delta` — composite objective delta on train split
- `aggregate_held_composite_delta` — composite objective delta on held-out split
- `train_ndcg`, `held_ndcg` — per-axis mean per-commander nDCG@30
- `gem_rate_train`, `gem_rate_held` — per-axis hidden_gem_hit_rate
- `n_iterations`, `n_steps_accepted`, `n_steps_rejected`
- `partial_sweep` — boolean; true if wall-clock cap aborted mid-sweep
- `dead_keys` — list of `_RULE_QUALITY_MULTIPLIER` keys whose `rule_id` is unreachable in scoring (empty in steady state; non-empty signals stale `data/scoring_weights.json`)

**Comment preservation contract.** The proposal's `per_rule_diffs` only carries `value` deltas. The `comment` fields in `data/scoring_weights.json` are never read or emitted by the optimizer, so a human applying the diff cannot accidentally clobber them. (Per CLAUDE.md, `comment` edits don't flip `compute_config_hash`.)

Stderr prints a human-readable summary table sorted by `|composite_delta_train|` descending. **The optimizer never mutates `data/scoring_weights.json`.** Application is human-driven.

### FR7 — Convergence log

Append-only `.audit/optimize_history.csv` records every step (accepted or rejected) for post-hoc analysis. Schema:

```
timestamp, run_id, sweep_n, rule_id, old_value, new_value,
train_composite, held_composite, train_ndcg, held_ndcg, train_gem, held_gem,
accepted, reject_reason
```

Gitignored (matches `.audit/history.csv` precedent). Regenerable.

### FR8 — CLI integration

New flag on the existing `bench.py audit` subcommand:

```
uv run scripts/bench.py audit --optimize                   # run with current weights as starting point
uv run scripts/bench.py audit --optimize --max-sweeps 3    # cap iterations
uv run scripts/bench.py audit --optimize --seed 42         # override split seed (non-default = experimental)
```

Lives in `src/mtg_synergy_graph/bench/optimize.py` per the existing handler pattern. No changes to `audit` semantics when `--optimize` is absent.

### FR9 — Identity preservation when no improvement found

If the optimizer terminates with zero accepted steps, `.audit/optimize_proposal.json` still writes (with `n_steps_accepted: 0`) so post-hoc tooling can distinguish "ran and found nothing" from "didn't run." Stderr prints a one-line "no improvement found" notice.

### FR10 — Planted-perturbation self-test (calibration)

Before each `--optimize` run produces its proposal, the optimizer runs a self-test: pick a randomly-chosen `rule_id`, set its weight to `1.5×` its baseline value, run a single sweep, and assert the optimizer recovers a value within ±10% of the original. If the test fails, the optimizer aborts with a diagnostic about which gate (train, held-out, ε) prevented recovery. Self-test seed is independent of the train/held split seed so it does not contaminate the held-out.

This addresses the "no improvement found" unfalsifiability concern from review: if the gates can't accept a known-good move, "no improvement found" tells us the gates are mis-calibrated, not that weights are optimal.

The self-test is opt-out via `--no-self-test` (for cycles where calibration has already been validated within the current sweep grid).

## Success Criteria

1. **Functional.** `bench.py audit --optimize` runs to completion in <3 min on the current 100-cmdr golden set with the current 62-rule catalogue (hybrid scoring with K=200 top-K filter). If the budget is missed, planning either tightens K or reverts to a smaller train split — but production fidelity is the invariant, not wall-clock.
2. **Quality.** First run from current `data/scoring_weights.json` either (a) produces a proposed diff with train composite ↑ ≥ 0.005 and held-out composite ↑ ≥ 0, OR (b) reports "no improvement found" AND passes the planted-perturbation self-test (FR10). Without (b)'s perturbation guard, "no improvement found" is unfalsifiable — the test distinguishes "weights near optimal" from "gates too strict."
3. **Trust.** When applied via `--repin --yes`, the resulting `bench.py audit` verdict on the full 100-cmdr set is POSITIVE or NEUTRAL (not NEGATIVE) for at least one of the first 3 attempted optimization rounds. Negative outcome on 3 consecutive rounds → escalate; with hybrid scoring, the most likely cause is (1) overfitting on 80-cmdr train split or (2) tensor-filter top-K under-sized.
4. **Observability.** `.audit/optimize_history.csv` accumulates ≥ 1 row per attempted step so we can post-hoc inspect convergence behavior and identify pathological rules (always-rejected, always-accepted-then-rejected, etc.). Per-axis (mean nDCG, gem rate) values are logged alongside composite for axis-by-axis post-hoc inspection.

## Open Questions Resolved in This Brainstorm

| # | Question | Decision |
|---|---|---|
| 1 | SPSA vs Coordinate Ascent vs LambdaMART | **Coordinate Ascent** (M1 only); SPSA/LambdaMART deferred to M3. |
| 2 | Held-out split design | **Stratified 80/20** by archetype tag, fixed seed. Stratification is free at split time and prevents archetype skew. |
| 3 | Lagrangian-floor vs composite objective | **Composite α-blended objective.** `α · mean_per_commander_nDCG + (1-α) · gem_rate`, α=0.5 default. Replaces both the soft-floor `λ` and the hard-barrier formulations. |
| 4 | `weight_hint` priors source | **Out of MVP.** Field is unconsumed by `universal_scorer.py`. M2 will populate from baseline values and tune `tau`. |
| 5 | SNIPS propensity estimation | **Out of MVP.** M2 or M3. |
| 6 | Per-density-bucket joint vs phased | **Out of MVP.** `_FLAT_WEIGHT_OVERRIDES` not optimized in M1; M2 phase. |
| 7 | Tensor scoring fidelity | **Hybrid: tensor top-K filter + live re-score.** K=200 default. Production-faithful objective; tensor used for cheap filter only. |
| 8 | Sequencing vs Survivor #2 (writer-side gate compiler) | **Compromise.** FR0 lands the auditor-side predicate compilation prerequisite (~1 day). Full Survivor #2 (writer-trace UX) deferred. M1 covers the full 62-rule catalogue. |
| 9 | Re-pin discipline | **Human-driven, NOT auto.** Optimizer emits `.audit/optimize_proposal.json` only; human runs `--repin --yes` after manually applying the diff. |
| 10 | Falsification of "no improvement found" | **Planted-perturbation self-test (FR10).** Aborts with diagnostic if known-good move is unrecoverable; distinguishes "near optimum" from "gates mis-calibrated." |

## Open Questions for Planning

1. **Search grid coarseness.** Multiplicative grid `[0.5×, 0.75×, 1.25×, 1.5×, 2.0×]` is a starting guess (the multiplicative-vs-additive scaling question is now resolved in FR1: multiplicative). Planning should empirically sweep one or two alternative grids (e.g., `[0.6×, 0.8×, 1.25×, 1.66×]`) on a sandbox run and lock in whichever the empirical history of past weight tunings best matches. The deprecated `scripts/weight_grid_search.py` already has the caching plumbing for this benchmark.
2. **`α` calibration.** `α = 0.5` is the starting default for the FR2 composite objective. Planning should validate empirically: a sweep over `α ∈ {0.3, 0.5, 0.7}` on a sandbox run reveals which α pushes the gem axis without sacrificing nDCG more than expected. Lock the M1 default after that sweep.
3. **Non-mutating `compute_config_hash` for proposed configs.** FR6 emits `proposed_config_hash`. Current `compute_config_hash()` in `src/mtg_synergy_graph/bench/tensor.py` reads `get_scoring_config_inputs()` with no argument. Planning needs a `compute_config_hash_for(scoring_config_inputs)` variant or a transient in-memory patch/restore. Single-threaded `bench.py` makes the patch/restore safe; planning to pick.

## References

- Seed: `docs/ideation/2026-04-26-applying-built-tooling-ideation.md` Survivor #1 (and FR0 lifts the auditor-prereq from Survivor #2).
- Prior weight-tuning attempt: `scripts/weight_grid_search.py` (deprecated; pattern validates the per-(cmdr, comps) caching but uses a stripped-down scorer that omits dampening — M1 uses the hybrid tensor+live-rescore path instead).
- Tensor producer + pre-dampening contract: `src/mtg_synergy_graph/bench/rule_ops.py:8-18`, `src/mtg_synergy_graph/universal_scorer.py:828-858`. Concentration dampening at `universal_scorer.py:329-379`.
- `compute_config_hash`: `src/mtg_synergy_graph/bench/tensor.py:38-95`.
- Hidden-gem metric: `src/mtg_synergy_graph/bench/hidden_gems.py`, `--inspect-gems` plumbing.
- Existing per-commander nDCG: `src/mtg_synergy_graph/validate.py::compute_ndcg` (the M1 objective composite reuses this for the nDCG term).
- Memory anchors: `memory/feedback_edhrec_not_goal.md`, `memory/feedback_edhrec_hivemind.md`, `memory/feedback_hidden_gem_metric.md`, `memory/feedback_audit_every_change.md`, `memory/feedback_audit_metric_too_coarse.md` (corroborates aggregate-verdict-too-coarse risk addressed by per-axis logging in FR3/FR6).
- Writer-side blind spot context: `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md`, commit `6fa552f`.
- Saito & Joachims 2023 ("On (Normalised) DCG as an Off-Policy Evaluation Metric", arXiv:2307.15053) — cited in problem statement; explicitly NOT used to justify raw-DCG-aggregate as the M1 objective (caveat applies to OPE, not in-policy weight tuning).
