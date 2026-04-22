---
date: 2026-04-21
topic: unified-eval-harness
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 1)
status: draft (brainstorm 1 of 7)
---

# Requirements: Unified Eval Harness + Rule-Contribution Tensor

## Problem Statement

Every recent scoring-related commit is a manually-performed coordinate-descent step constrained by a ~10-minute audit cycle. The existing 1,343 LOC across four eval scripts (`_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, `weight_grid_search.py`) each re-scores the full 100-commander golden set from scratch, forcing every "is this change OK?" question to be a 10-minute wait. That latency is the single biggest blocker on rule-authoring throughput and on enforcing the "audit every scoring change" guardrail (`memory/feedback_audit_every_change.md`).

## Goals

1. Cut the per-change audit cycle from ~10 min to <30 s on a pinned reference fixture.
2. Persist per-(commander, candidate, rule) contributions so that every diagnostic currently requiring a re-score becomes a SQL query.
3. Consolidate the four eval scripts + `broad_set_track.py` into a single `scripts/bench.py` CLI.
4. Replace the binary TRIVIAL audit verdict with a rank-shuffle histogram that surfaces sub-top-30 movement.
5. Enable rule-pair collinearity (MI-VIF) as an automatic output of every audit run.
6. Make the NDCG-audit guardrail cheap enough to actually follow on every scoring change.

## Non-Goals

- Scaling the golden set to 2,761 EDHREC commanders (stays backlogged for a later iteration; infra must admit it without rework).
- Automatic coordinate-descent weight optimization (downstream follow-up; depends on this landing).
- CI / GitHub-Actions integration (local-only at MVP).
- Any change to what a complement rule *is* or how it scores — this is pure infrastructure around the existing scorer.
- UI changes to `recommend.py --explain` output.

## Users and Scenarios

| Scenario | Who | Expected experience |
|---|---|---|
| Add a new complement rule | Dev editing `complement_rules/` | Pre-commit hook runs `bench.py` in <30 s. Verdict printed. HARMFUL → warning banner; dev decides whether to proceed. |
| Tune a weight in `_RULE_QUALITY_MULTIPLIER` | Dev editing `universal_scorer.py` | Same: auto-run, verdict, decide. |
| Refactor a helper (no semantic change expected) | Dev | `bench.py` must report `Δ NDCG ≈ 0.0` against pinned fixture. Non-zero delta means refactor changed behavior → investigate. |
| Investigate why Rule X fires on 451 commanders but lifts only Prossh | Dev | `bench.py --rule token_etb_damage --inspect` runs a SQL query over the stored tensor. No re-score. |
| Check rule orthogonality before adding Rule Y | Dev | `bench.py --collinearity` outputs MI-VIF between every existing rule pair. Rules with VIF > 5 flagged. |
| Accept a positive change and make it the new baseline | Dev | `bench.py --repin` overwrites the pinned reference fixture. Recorded in git. |

## Functional Requirements

### FR1 — Rule-contribution tensor

A new SQLite table (schema to be designed in planning) persists a record for every (commander, candidate, rule) cell where a rule emits a non-zero contribution. The tensor is rebuilt whenever `bench.py --repin` runs. No incremental updates at MVP — full rebuild on re-pin only.

### FR2 — Pinned reference fixture

One committed file (working name: `tests/fixtures/golden_set_run.json`) holds the baseline aggregate NDCG@30, per-commander NDCG, per-commander hi_syn counts, and per-(cmdr, cand, rule) contributions. `bench.py` compares working-tree output to this file. `--repin` overwrites after explicit confirmation.

### FR3 — Unified `scripts/bench.py` CLI

Replaces the four existing scripts with subcommands:
- `bench.py audit` — default; runs full eval on 100-cmdr golden set against pinned baseline, prints verdict + aggregate delta.
- `bench.py audit --rule <rule_id>` — per-rule ablation (A/B with rule disabled); prints touched commander list + histogram.
- `bench.py audit --inspect <rule_id>` — reads tensor, lists every (commander, candidate) where this rule contributes, sorted by contribution magnitude.
- `bench.py audit --collinearity` — outputs MI-VIF between every pair of currently-registered rules.
- `bench.py audit --repin` — regenerates the pinned fixture from current working tree; requires `--yes` confirmation.
- `bench.py audit --format json|md` — output format selector. Default `md` for humans, `json` for tooling.

### FR4 — Rank-shuffle histogram verdict

Every audit run emits a per-commander histogram with buckets:
- `no_change` — ranks identical, no hi_syn gain/loss
- `rank_shuffle_within_top30` — top-30 identity unchanged, order changed
- `rank_shuffle_across_top30_boundary` — one or more cards crossed the top-30 line
- `hi_syn_gain` — net hi_syn count up
- `hi_syn_loss` — net hi_syn count down

Existing 5-verdict rubric (positive / MARGINAL / TRIVIAL / CONTENTIOUS / HARMFUL) is kept as a roll-up derived from the histogram, so existing workflows that consume verdicts keep working. TRIVIAL is redefined: only applies when all commanders are `no_change` (not when rank_shuffles net to zero).

### FR5 — Pre-commit hook

A new pre-commit hook runs `bench.py audit --format md --output .audit/last.md` when the staged diff touches any of:
- `src/mtg_synergy_graph/complement_rules/**/*.py`
- `src/mtg_synergy_graph/universal_scorer.py`
- `src/mtg_synergy_graph/graph_engine.py`

HARMFUL verdict prints a warning line to stderr with a one-liner summary ("aggregate NDCG dropped 0.012; 8 commanders regressed"). Commit proceeds unless the dev aborts. No hard block. Port-extraction files (`ports.py`, `importer.py`, `parser.py`) are intentionally excluded — their changes require a DB re-import before audit is meaningful, so they get their own workflow (out of scope).

### FR6 — MI-VIF collinearity report

The tensor structure makes rule-pair correlation a pairwise covariance computation across rule columns. `bench.py audit --collinearity` outputs VIF for each rule plus pairwise Pearson correlation for rules with VIF > 5. Used to flag candidate merges for the typed port-graph refactor (Survivor 2).

### FR7 — Pure-infra refactor mode

A flag `bench.py audit --expect-identity` asserts that every (commander, candidate) score is bitwise-identical to the pinned baseline. Non-zero delta fails the audit. Used when refactoring to land Survivor 2 (rules-as-data), where NDCG must be unchanged by definition.

## Success Criteria

1. **Latency.** `bench.py audit` completes in ≤ 30 s wall-clock on a pinned fixture, starting from cold SQLite on an M-class MacBook. `bench.py audit --rule X` and `--inspect X` complete in ≤ 2 s.
2. **Identity preservation.** On the existing 100-cmdr golden set with no scoring changes, `bench.py audit` against a freshly-regenerated baseline prints `Δ NDCG = 0.000000` and `histogram: 100 no_change, 0 others`. Deviation indicates a bug in the tensor extraction path.
3. **Verdict fidelity.** For the last 10 accepted rule commits, the new histogram-based verdict classifies each the same way the old 5-verdict rubric did (positive/MARGINAL/TRIVIAL/CONTENTIOUS/HARMFUL), plus adds the rank-shuffle detail.
4. **Hook adoption.** Pre-commit hook installs via `pre-commit install`; fires on the scoped paths; writes to `.audit/last.md`; HARMFUL verdict warning is visible in the normal commit terminal output.
5. **Guardrail enforceability.** The `memory/feedback_audit_every_change.md` guardrail becomes cheap to apply: every relevant commit has a measured NDCG delta attached in the commit or in `.audit/last.md`.

## Constraints

- No Python packages added unless strictly required for correctness. MI-VIF computation is a short numpy expression; avoid pulling scikit-learn just for VIF.
- Tensor size at MVP: 100 commanders × ~30k candidates × ~40 rules with sparsity ~5% → ~6M rows. SQLite handles this comfortably.
- Existing `--explain` output on `recommend.py` must continue to work. Preferably it reads from the persisted tensor when available, falls back to recompute when not.

## Open Questions (For Planning Phase)

- Exact tensor table schema (column per rule vs (rule_id, contribution) rows).
- Invalidation handshake when `_RULE_QUALITY_MULTIPLIER` changes — does the tensor need rebuild, or is weight multiplication applied at read time?
- Pre-commit hook: `pre-commit` framework vs bare git hook.
- `--inspect` output pagination or truncation for rules firing on 1,000+ commanders.

## Out of Scope for This Brainstorm

- Survivors 2–7 (each has its own requirements doc).
- Coordinate-descent weight optimizer built on top of this infra.
- Scaling eval to 2,761 commanders.
- CI integration.

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 1.
- Guardrail: `memory/feedback_audit_every_change.md` — this work is the instantiation of that rule.
- Sequencing: Survivor 2 (typed port-graph) depends on FR7 for refactor-identity verification.
