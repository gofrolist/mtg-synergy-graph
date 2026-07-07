---
last_updated: 2026-07-07
module: complement_rules
tags:
  - outlet
  - sacrifice
  - death-payoff
  - cohort
  - kill-test
  - null-result
  - flat-credit
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any sacrifice-outlet / death-outlet feeder mechanism (commander with a ChangesZone-shaped death trigger paired with cost.sacrifice candidates)
  - Considering any per-class flat credit over a large (>1000-card) candidate class
  - Planning the next cohort cycle after subtype-supply — read this AND the head-flatness diagnostic (`.superpowers/sdd/diagnostic-head-flatness.md`) first
created: 2026-07-07
plan_ref: docs/plans/2026-07-07-002-feat-outlet-cohort-rank-bonus-sidecar-plan.md
---

# `death_outlet_feeder` rule: DECLINED at pre-registered gates

**Cycle:** plan `docs/plans/2026-07-07-002-feat-outlet-cohort-rank-bonus-sidecar-plan.md`
Part B (Tasks 2-8), the outlet-direction death-payoff cohort playbook, mirroring the
subtype-supply cycle (plan 2026-07-07-001). **Verdict: DECLINE** — the working tree
was reverted to the Task 6 hash-neutral state (flag `False`, no weights entry);
nothing was committed for Part B beyond the flag-off wiring already on `main` via
Task 6. Part A (the rank_bonus-ablated NDCG sidecar, Task 1) shipped separately and
remains standing instrumentation — see `docs/RULE_HISTORY.md` 2026-07-07.

## What was tested

A new formal `ComplementRule` `death_outlet_feeder` (`complement_rules/death_outlet.py`,
flag `_ENABLE_DEATH_OUTLET_FEEDER`, default `False`): commanders with a
`ChangesZone`/`ChangesZoneAll`-shaped death trigger that is not self-only and carries
no explicit `Sacrificed`/`SacrificedOnce` trigger port (the `outlet_direction_death_payoff`
cohort predicate, `bench/cohorts.py`; 135 commanders, 126 EDHREC-labeled, 5 golden-100
anchors: Judith, Marchesa, Meren, Titania, The Gitrog Monster) earn a synergy complement
against every candidate holding a `cost.sacrifice` port (1,923-1,996 legal cards
depending on legality-filter pass), with `cand_event` set to `free_outlet` /
`paid_outlet` / `self_sac` via the existing `_cost_filter_group` enrichment so IDF
could in principle differentiate outlet classes. Hypothesis: Judith×Viscera Seer,
Marchesa×Ashnod's Altar, Meren×Carrion Feeder — all zero-tensor-row misses today —
become reachable.

Gates were pre-registered in `.audit/outlet_cohort/PINNED_GATES.md` (Task 4, BEFORE
any rule code existed): O-noise (outlet-cohort ΔNDCG ≥ H_outlet = +0.0233), O-whitelist
(beat the flat-bonus comparator at matched cliffs ≤ 1 — PARTIAL band pre-registered
empty since no whitelist cell clears cliffs ≤ 1), O-500 (golden-500 no-regression),
O-100 (audit verdict), O-quality (rule_quality_gate + collinearity conjunction),
O-clean (rank_bonus-ablated cohort delta must also clear H_outlet).

## Sweep table (outlet fixture, n=126, vs baseline 0.1152 NDCG / 0.9616 gem, seed 17)

| multiplier | ΔNDCG | gem Δ | cliffs (<−0.05) |
|---|---|---|---|
| 0.75 (chosen) | −0.0113 | +0.0058 | 10 |
| 1.00 | −0.0141 | +0.0058 | 17 |
| 1.50 | −0.0220 | +0.0082 | 22 |
| 2.00 | −0.0255 | +0.0101 | 24 |
| 2.50 | −0.0328 | +0.0127 | 32 |

Monotone degradation — damage grows with the multiplier at every step; no cell was
NDCG-positive. Per the pre-registered fallback, the top-2 cells by ΔNDCG (0.75, 1.0)
were also run against golden-500, both noted as failing the binding cohort gates.

## Gate table at the chosen cell (multiplier 0.75, flag ON)

| Gate | Threshold | Measured | Result |
|---|---|---|---|
| O-noise (binding NDCG) | outlet ΔNDCG ≥ +0.0233 | **−0.0113** | **FAIL** |
| O-gem | outlet gem Δ ≥ −0.0104 | +0.0058 | PASS* |
| O-cliffs | per-commander cliffs ≤ 1 | **10** | **FAIL** |
| O-500 (NDCG) | golden-500 ΔNDCG ≥ −0.0136 | +0.0008 | PASS |
| O-500 (gem) | golden-500 gem Δ ≥ −0.0235 | −0.0005 | PASS |
| O-100 | audit verdict non-NEGATIVE, no gem warning | positive (Δ +38.01, gem Δ −0.0003), no warning | PASS |
| O-quality (gate CLI) | rule_quality_gate PASS | PASS (targets 146, cov 9.0, cv 0.180) | PASS |
| O-quality (collinearity) | no pair VIF > 5 AND \|r\| > 0.8 | max \|r\| = 0.723 (edict_feeder; VIF 8.98/5.98) | PASS† |
| O-clean (golden-100 leg) | ablated Δ vs HEAD-ablated not negative beyond noise | ablated 0.1904 vs 0.1911, Δ −0.0007 | PASS |
| O-clean (cohort leg, PINNED_GATES) | ablated cohort Δ ≥ +0.0233 | ablated 0.1005 vs flag-off-ablated 0.1074 → **−0.0069** | **FAIL** |

\* O-gem passes numerically, but the +0.0058 gem bump sits inside the whitelist
comparator's own +0.0042..+0.0058 range at negative NDCG — whitelist-signature, not
merit.

† `bench.py audit --collinearity` was tensor-blind pre-repin (`rules_examined: 0`).
Replicated with a scratch driver doing live in-process tensor capture over the same
126 outlet-fixture commanders, same Pearson/VIF math as `bench/collinearity.py`. All
pairs involving `death_outlet_feeder`: `cost_feeds_trigger` r=+0.434 (VIF 1.23),
`dies_drain` r=−0.550 (VIF 4.02), `edict_feeder` r=−0.723 (VIF 5.98 — flagged by
bench's OR-criterion, below the gate's AND-criterion), `gy_loader` r=−0.147
(VIF 1.23). `death_outlet_feeder`'s own VIF = 8.98. `sacrifice_cluster` /
`gy_fuel_feeder` never co-fired on this fixture; `graveyard_sac_value` is not a
rule_id anywhere in the codebase.

Three of three binding gates that mattered — O-noise, O-cliffs, O-clean (cohort
leg) — failed. O-noise and O-cliffs failed at **every** sweep cell, not just the
chosen one; the PARTIAL escalation band was pre-registered empty this cycle, so
there was no ambiguity to escalate to a human.

## Whitelist-equivalence finding

The disguised flat-bonus outlet whitelist (Task 5, measured BEFORE the rule
existed) was ALSO negative at every cell:

| bonus | ΔNDCG | cliffs | gem Δ |
|---|---|---|---|
| 0.10 | −0.0083 | 8 | +0.0042 |
| 0.25 | −0.0260 | 24 | +0.0058 |
| 0.50 | −0.0449 | 38 | +0.0053 |

The rule at its best operating point (0.75: −0.0113 / 10 cliffs / +0.0058 gem) sits
**between** the whitelist's 0.10 and 0.25 cells on every axis. It does not separate
from the disguised whitelist at matched side-effect budget — precisely the failure
mode the pre-registered O-whitelist gate existed to catch (subtype-supply's
predecessor cycle beat both whitelist variants outright; this rule did not).

## Root cause

The candidate side is a **per-class flat credit over a 1,996-card class**: every
legal card with a `cost.sacrifice` port receives one row at a single IDF value per
`filter_group` (0.1729 at multiplier 1.5, uniform within each of `free_outlet` /
`paid_outlet` / `self_sac`). The 3-way filter_group split does not produce
meaningful IDF separation at this population size — Viscera Seer and Ashnod's Altar
score identically to Akki Avalanchers and Army Ants. There is **zero per-candidate
discrimination** within the class.

This is the flatness-diagnostic leverage regime
(`.superpowers/sdd/diagnostic-head-flatness.md`) measured at rule scale rather than
at the density-rule scale the diagnostic originally found it in: any new term
touching a large, low-dispersion candidate class reshuffles the flat head
(median 48, up to 4,248 candidates within 5% of the #30 score) violently, because
the diagnostic's own numbers show **96.8% of near-tied adjacent head pairs are
ordered by micro-terms** (`cmc_bonus` + `rank_bonus`, ≤0.015 combined) that a flat
0.17-scale bump trivially overrides. The result is exactly what the diagnostic
predicted: mass promotion, not selective promotion, hence the 10-32 per-commander
cliffs.

It is also the same failure family as the deck-context flood DECLINE
(`deck-context-null-result-2026-07-06.md`): a "mean-of-IDF-sums" / flat-per-class
additive term without per-candidate specificity normalization is just another
density axis layered on top of an already-flat head. Both mechanisms independently
rediscovered that a per-class (not per-candidate) credit cannot rank *within* the
class it targets — it can only decide *whether* the class as a whole gets promoted,
which is a whitelist by another name.

## What this does NOT rule out

- A per-candidate-**discriminating** outlet signal — e.g. the embedding-similarity
  contribution (`embeddings/contribution.py`, flag-gated off; the head-flatness
  diagnostic's "lever 1": add real per-candidate dispersion rather than a flat
  bump) restricted to the sac-outlet class.
- A graded credit keyed on outlet *efficiency* rather than mere presence — free vs.
  costed activation, repeatability (untap effects, no-cost sac outlets vs. one-shot
  ones), or some other axis that actually separates Viscera Seer from Akki
  Avalanchers instead of collapsing them to the same `filter_group` bucket.
- Any future attempt at either of the above MUST pre-pin gates on the standing
  outlet fixture/bands/whitelist comparator described below before writing rule
  code — do not re-measure a fresh baseline; reuse `.audit/outlet_cohort/PINNED_GATES.md`.

## Measurement caveats

Three stock CLIs were tensor-blind pre-repin at the working-tree config_hash
(`--collinearity` reported `rules_examined: 0`; `--rule`/`--inspect` refused with
"no tensor rows"; `--forensics` exited 2 with the same message) — the standard
consequence of measuring an unpinned working-tree flip. All three were replicated
with scratch drivers calling the **same production library functions**
(`extract_live_ranking` via `engine.page()`, `edhrec_labels_for_commander`,
`compute_rank_bonus_ablation`, the same Pearson/VIF math as `bench/collinearity.py`)
rather than the stock CLI's persisted-tensor path. The O-clean cohort-leg baseline
(flag-off ablated NDCG) required a temporary working-tree flag revert, immediately
restored, since no pinned ablated-cohort baseline existed before this cycle.

## Standing infra (all REMAIN after the DECLINE)

- **Cohort predicate:** `outlet_direction_death_payoff(conn)` in `bench/cohorts.py`
  (135-commander cohort, separate from and not unioned into `_COHORT_PREDICATES` /
  the subtype-payoff cohort — deliberately, to avoid mutating the pinned
  archetype-payoff fixture).
- **Fixture:** `tests/fixtures/golden_set_outlet_payoff.json` (n=126 buildable),
  built by `scripts/bootstrap_outlet_payoff_fixture.py`; registered in
  `tests/bench/test_fixture_freshness.py`.
- **Bands:** `.audit/outlet_cohort/PINNED_GATES.md` / `.audit/outlet_cohort/bands/`
  (H_outlet = 0.0233, gem half-width 0.0104).
- **Whitelist comparator:** `outlet_whitelist_scores` in `bench/context_sim.py`
  (`--whitelist-baseline-outlet` CLI arm).
- **Rule code:** `complement_rules/death_outlet.py`, dispatch in `core.py`,
  registry gate, `_RULE_TO_BUCKET` entry — all present, flag `False`, hash-neutral
  (`bench.py audit --expect-identity` PASS). No `ScoringConfigInputs` field was
  ever registered (correctly — Part B never reached the SHIP path).
- **rank_bonus sidecar (Task 1, Part A):** `compute_rank_bonus_ablation` in
  `bench/forensics.py`, shipped independently as standing instrumentation this
  same cycle; used here to compute the O-clean gate.
- **Tests:** `tests/test_death_outlet.py`, `tests/bench/test_outlet_cohort.py`,
  `tests/bench/test_context_sim.py` (whitelist), `tests/bench/test_rank_bonus_ablation.py`
  — all green, all exercising flag-off / cohort-predicate / whitelist code paths
  that remain live.

A future cycle re-entering this space should read this doc, the head-flatness
diagnostic, and the deck-context null-result doc first, then propose a
per-candidate-discriminating mechanism (not a flat per-class credit) and pre-pin
gates on the standing fixture/bands/comparator above before writing any rule code.
