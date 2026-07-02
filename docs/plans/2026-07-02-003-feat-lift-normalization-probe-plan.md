---
title: "feat: Lift-normalization probe — score minus expected-baseline panel (C1)"
type: feat
status: active
date: 2026-07-02
origin: docs/brainstorms/2026-07-02-lift-normalization-requirements.md
---

# feat: Lift-normalization probe — score minus expected-baseline panel (C1)

## Overview

The C1 cycle mandated by plan 2026-07-02-002's escalation rule. Rank by
`lift(cmdr, card) = score(cmdr, card) − λ·panel_mean(card)` where
`panel_mean` is precomputed by our own scorer over a fixed committed
commander panel. This is a probe, not a feature: the R0 kill-test can
end the cycle before any scoring integration, and a clean DECLINE with
a null-result doc counts as success. Template: the color-IDF probe
(docs/plans/2026-07-02-001) — baseline tag, flag-gated OFF units, flip
evidence package with pre-committed gates, explicit outcome paths.

## Problem Frame

Carried from the origin doc (see origin): ~13 weight-layer
configurations against flood monoculture all failed the −0.05
per-commander cliff gate on flood-vs-archetype displacement; gem
improved in every one. Whether a flood is noise or the archetype is a
commander-demand question only a baseline subtraction can express.
**Load-bearing hypothesis:** displacing cards have HIGHER panel_mean
than the labels they displace — checkable in minutes on the known
forensics cards before any integration work (R0).

## Requirements Trace

Origin R0–R11 map to units as:
- R0 kill-test → Unit 2 (with DECLINE-and-stop path)
- R1–R3 panel semantics/storage → Unit 3
- R4 flag-gating → Unit 4; R4 λ-grid sweep → Unit 5; R5 three-site
  integration → Unit 4; R6 frozen-panel optimizer semantics → Unit 4
  (workflow-tax docs → Unit 7)
- R7 bounded matrix + kill-order, R8 two watch-list strata → Unit 5
- R9 repin fix FIRST → Unit 1
- R10 conditional rank_bonus ablation → Unit 6
- R11 gates → Units 5/6; outcome handling → Unit 7

## Scope Boundaries

Carried verbatim from origin: no B3 gate-definition change (own FR6
cycle) — Unit 1 (R9) refreshes stale gem legacy VALUES only; no C5 matcher refactor, importer
unwrapping, PPMI batch, or anthem key redesign; no role/quota
portfolio re-ranking; no EDHREC data in panel VALUES (selection is
design-time only).

### Deferred to Separate Tasks

- Role/quota portfolio selection (C1's sibling): only if this probe
  and its fallback both DECLINE — the null-result doc re-points there.
- B3 gem plausibility-gate hardening: own FR6 cycle.

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/universal_scorer.py` — per-candidate field
  injection precedent (`embedding_contribution`, `rank_bonus`
  construction in `score_from_complements`); three total-assembly
  sites (`UniversalScore.score`, `to_legacy_buckets`, and
  `bench/optimize.py::_fast_total_and_contribs`) with the Unit-4
  fidelity contract; `ScoringConfigInputs` + `_seed_digest`;
  `_FLAT_COUNT_RULES` (synergy-only definition boundary).
- `src/mtg_synergy_graph/bench/handlers.py` — `handle_repin`'s
  `build_fixture` call (≈:104) lacks `edhrec_conn`; the ≈:739 comment
  anticipates the plumbing; `handle_inspect_gems` (≈:748–781) is the
  hard-fail pattern for a missing EDHREC DB. `build_fixture` accepts
  `edhrec_conn` (`bench/fixture.py` ≈:286).
- `scripts/build_embeddings.py` + `src/mtg_synergy_graph/embeddings/config.py`
  — the build-artifact + hybrid-hash discipline the panel mirrors
  (incl. the chicken-and-egg digest placement).
- Probe mechanics: `docs/plans/2026-07-02-001-feat-color-conditioned-idf-probe-plan.md`
  (template), `tests/test_pathway_flag_gate.py` (flag-gate tests),
  `tests/bench/test_optimize.py` (fused-total fidelity invariant).
- Watch-list provenance + baseline numbers: `pre-scoring-remediation`
  tag annotation and `.audit/forensics.md`.

### Institutional Learnings

- `docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md`
  — the mandate and the four flood sub-shapes; gates provenance.
- `docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md`
  — kill-test-before-integration discipline; R6/R7 gate numbers; the
  color-conditioning axis is measured territory (informs the
  denominator decision, from the opposite direction).
- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  — identity-clean OFF; `ScoringConfigInputs` field at flip only.
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
  — flip bar defined before the sweep.
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`
  — 500-cmdr fixture for any `--optimize` runs.

### External References

- None needed — grounding is local (origin doc reviewed by 4 personas
  this session; integration sites verified by the feasibility
  reviewer against the working tree).

## Key Technical Decisions

Carried from origin (see origin for rationale): subtractive lift with
z-score as named fallback; synergy-only base-score panels (lift flag
forced OFF at build; side-channel bonuses excluded); global-median
fallback + coverage warning; frozen-panel optimizer semantics;
sequential-narrowing matrix with a ≤7-battery ceiling; riders bundled
but not load-bearing. Plan-level additions:

- **Panel artifact = committed JSON + DB table**: the commander list
  (selection output) is committed like `event_match_seed.json` so
  inference reproducibility is reviewable; the per-card `panel_mean`
  values live in a `card_panel_mean` DB table rebuilt by a script
  (embeddings precedent) — values are DB-derived, not committed.
- **Panel keyed by card name internally, oracle_id in the committed
  artifact header for provenance**: the scorer is name-keyed
  end-to-end; converting at one boundary avoids a cross-cutting
  rename. (Resolves the origin keying question.)
- **Baseline tag `pre-lift-normalization`** (carried from origin R11;
  plan detail): created after Unit 1's re-pin — the R9 gem refresh
  changes pinned gem values, so cumulative-floor comparisons for this
  cycle read from the new tag (evaluated on the winning arm at
  decision time), and plan-002-era gem figures are explicitly
  re-based.
- **`synergy_total` accessor defines the panel value** (feasibility
  review): no synergy-only score exists anywhere in the stack —
  `UniversalScore.score` folds all bonuses, and `to_legacy_buckets`
  gives circuit/cmc/rank/multi-rule/pair no named bucket. Unit 3 adds
  a `synergy_total` accessor with a pinned definition: syn − anti +
  multi-rule bonus + pair bonus, dampener applied — IN;
  staple/circuit/cmc/rank/embedding — OUT (builder also forces
  `_ENABLE_EMBEDDING_CONTRIBUTION` off).
- **λ lives in `scoring_weights.json`** (repo tuning convention:
  value/comment discipline, hash via the existing weights path), not
  a module constant — Unit 7's optimizer exposure conventions apply
  later if λ ever needs sweeping by machine.
- **`lift_penalty` gets a named bucket** in `to_legacy_buckets`
  (`"lift"`), preserving the named-bucket-sum invariant the explain
  path and forensics consume (`embedding`/`concentration_dampen`
  precedent).
- **The missing-panel raise lives in the `lift_panel` loader**,
  triggered on first access with the flag ON — covering SynergyEngine
  AND the direct bench callers (fixture/forensics/optimizer bypass
  engine init). This is a deliberate exception to the R-004
  never-raise convention in the scoring init path: a median-only run
  is silently wrong, not gracefully degraded.

## Open Questions

### Resolved During Planning

- Artifact keying: name-keyed internally, oracle_id provenance header
  (above).
- Where the lift subtracts: on the assembled total as a distinct
  field (like `embedding_contribution`), NOT inside the synergy sum —
  keeps dampener/anti-synergy semantics untouched and the field
  visible in explain output. Hand-computed pairs pin it (Unit 4).

### Deferred to Implementation

- panel_mean denominator semantics (absent-card = 0 vs excluded;
  color-legality conditioning) — Unit 2 measures BOTH under the
  two-composition kill-test and picks with numbers; the color-IDF
  null is cited in the decision record either way.
- Leave-one-out semantics — decided in Unit 2 from the same readout.
- Staple-only candidate exemption — decided in Unit 4 with a
  hand-computed check (subtracting full panel_mean from a 0.01-bonus
  candidate drives it sharply negative; the staple-ablated arm probes
  the channel regardless).
- Whether the R10 ablation runs against stale-panel scores or budgets
  a second panel build — decided at Unit 6 from wall-clock reality.

## High-Level Technical Design

> *Directional guidance for review, not implementation specification.*

```
build time (scripts/build_lift_panel.py, lift flag forced OFF):
  panel = committed selection artifact (~200 commanders)
  for cmdr in panel:  score_all_universal(...)  -> synergy-only totals
  card_panel_mean[card] = mean over panel (denominator semantics per Unit 2)
  store: card_panel_mean table + global_median + panel_digest

inference (flag ON):
  score_from_complements: us = UniversalScore(..., lift_penalty =
      λ * panel_mean.get(card, global_median))
  all three total sites: total -= lift_penalty
  audit surfaces: panel coverage %, tie-density/spread sidecar
```

## Implementation Units

- [ ] **Unit 1: R9 rider — repin gem plumbing + fresh baseline tag**

**Goal:** `--repin --yes` refreshes gem legacy; both fixtures re-pinned
with fresh gem values; new baseline tag for this cycle's gates.

**Requirements:** origin R9

**Dependencies:** None (lands on the remediation branch or main after
PR #85)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/handlers.py` (`handle_repin`)
- Test: `tests/bench/test_repin_gem_refresh.py` (create)

**Approach:**
- Pass `edhrec_conn` into the `build_fixture` call (≈:104), opening
  `args.edhrec_db` as `handle_inspect_gems` does — PLUS a pre-flight
  validation probe (one direct query against `edhrec_card_synergy`)
  BEFORE `build_fixture`, exiting 2 on failure. The inspect_gems
  DatabaseError branch alone is NOT sufficient: `_edhrec_top_30`
  swallows per-commander DatabaseErrors by design, so an empty-file
  DB would silently produce the exact gem-less pin this unit exists
  to prevent (feasibility review).
- Re-pin per fixture: `--repin --yes` for the 100-cmdr pin; the
  500-cmdr fixture regenerates via `scripts/bootstrap_golden_set_500.py`,
  which ALREADY plumbs `edhrec_conn` — verify whether its pinned gems
  are in fact fresh before assuming staleness there. Note sequential
  re-pins are last-writer-wins on the persisted tensor rows at the
  shared config hash.
- Record fresh gem values WITH per-commander gem-key coverage (slug
  mismatches degrade per commander; do not assume 100%); tag
  `pre-lift-normalization` with the baseline numbers (NDCG, fresh
  gem + coverage, forensics buckets, monoculture shares,
  unjustified-OUTRANKED split).

**Patterns to follow:** `handle_inspect_gems` EDHREC-DB error path;
plan-002 Unit 1 baseline-tag discipline.

**Test scenarios:**
- Happy path: repin against a tmp fixture + tmp EDHREC DB → gem keys
  present in the written fixture and match a direct
  `hidden_gem_hit_rate_for_commander` computation.
- Error path: missing EDHREC DB path → exit 2, no fixture write, hint
  mentions `--edhrec-db`.
- Error path: corrupt EDHREC DB (empty file) → exit 2, no partial pin.
- Edge case: fixture whose old legacy lacks gem keys entirely → keys
  added, not crashed.
- Integration: after repin, `bench.py audit` gem line shows a live Δ
  (not `Δ —`) against the fresh pin.

**Verification:** suite green; both committed fixtures carry fresh gem
values; tag exists with numbers; `--expect-identity` PASS.

- [ ] **Unit 2: R0 kill-test — throwaway panel + displacer inequality**

**Goal:** Decide go/no-go for the whole cycle in minutes of compute:
does `mean panel_mean(displacers) > mean panel_mean(displaced labels)`
hold on the known forensics cards?

**Requirements:** origin R0, R2 (composition sensitivity), and the two
deferred denominator/leave-one-out questions

**Dependencies:** Unit 1 (fresh baselines)

**Files:**
- Create: scratch script (session scratchpad; promoted to
  `scripts/` only if the cycle proceeds and the readout is worth
  keeping as an instrument)

**Approach:**
- Build throwaway panel_mean under TWO compositions (EDHREC-top-N
  with per-identity caps; identity-stratified) × the denominator
  variants (absent=0 vs excluded; all-panel vs color-legal
  denominator) × leave-one-out on/off — cheap once scores are cached
  per panel commander.
- Evaluate the displacer inequality on the Unit-1-tagged forensics
  sets (monoculture displacer cards vs the OUTRANKED labels they
  displaced), plus the spread readout.
- **DECLINE-and-stop path:** inequality fails under both compositions
  → write the null-result doc (what information the baseline lacked),
  update RULE_HISTORY + plan status, and the cycle ends here — Units
  3–7 never run. Otherwise: record the winning composition/
  denominator/leave-one-out choice WITH numbers as the Unit 3 spec.

**Execution note:** Kill-test discipline — no scoring-path code until
this unit's numbers are recorded.

**Test scenarios:**
- Test expectation: none — design-time diagnostic script; its output
  numbers are the deliverable and get recorded in the plan/decision
  record. (Determinism of the promoted builder is tested in Unit 3.)

**Verification:** a written go/no-go decision with the inequality
numbers for every measured variant; on GO, the frozen Unit-3 spec.

- [ ] **Unit 3: Panel builder + storage (GO path only)**

**Goal:** Deterministic committed panel + `card_panel_mean` storage
with full hash discipline.

**Requirements:** origin R1, R2, R3

**Dependencies:** Unit 2 GO

**Files:**
- Create: `scripts/build_lift_panel.py`
- Create: `src/mtg_synergy_graph/data/lift_panel_seed.json` (committed
  selection artifact: commander list + selection-rule provenance +
  oracle_id header)
- Modify: `src/mtg_synergy_graph/db.py` or importer schema site
  (`card_panel_mean` table + config KV rows)
- Create: `src/mtg_synergy_graph/lift_panel.py` (loader: panel_mean
  map + global median + digest, cached)
- Test: `tests/test_lift_panel_build.py` (create)

**Approach:**
- Panel scores are BASE synergy-only scores via the new
  `synergy_total` accessor (definition pinned in Key Technical
  Decisions); builder forces `_ENABLE_LIFT_NORMALIZATION` and
  `_ENABLE_EMBEDDING_CONTRIBUTION` OFF.
- Builder constructs ONE shared `CandidateCache` and passes it per
  panel commander (the SynergyEngine batch precedent) — the fixture
  pattern without it re-issues full-table scans per commander, and
  the rebuild tax recurs on every config change while the flag is ON.
- The committed selection artifact joins the config hash only via the
  panel digest at the Unit 5 flip — editing the list while the flag
  is OFF changes no hash (window accepted and stated).
- Global median = median of the stored per-card `panel_mean` values
  (the fallback constant, stored alongside them).
- Store per-card mean + the global median (the fallback value) + a
  panel-content digest; digest joins the hash inputs at Unit 5's flip
  (embeddings hybrid-hash precedent for placement).
- Coverage fraction computed at load; surfaced by the audit (Unit 4).

**Patterns to follow:** `scripts/build_embeddings.py` build+hash
shape; `event_match_seed.json` committed-artifact conventions.

**Test scenarios:**
- Happy path: two builds on the same DB → identical values + digest
  (determinism).
- Happy path: panel scores contain no rank_bonus component — a card
  whose only differentiator is `edhrec_rank` gets identical
  panel_mean to its twin (synergy-only pin).
- Happy path: `synergy_total` equals a hand-computed
  syn − anti (+ multi-rule + pair, dampened) figure on a synthetic
  UniversalScore — the in/out list pinned exactly.
- Edge case: recursion guard — builder run while the lift flag is
  globally True still produces base scores (flag forced OFF inside).
- Edge case: card absent from every panel commander's results →
  handled per the Unit-2 denominator decision; global median present
  and equal to a hand-computed value on a small fixture DB.
- Error path: malformed committed selection artifact → loader raises
  at init (seed drift-check pattern).
- Integration: rebuilding after a scoring-weight edit changes values
  and the digest (staleness propagates).

**Verification:** builder deterministic; loader values match direct
computation on a tiny fixture DB; suite green.

- [ ] **Unit 4: Scoring integration, flag-gated OFF**

**Goal:** `lift_penalty` per-candidate field wired at all three
total-assembly sites, bitwise-inert while OFF.

**Requirements:** origin R4, R5, R6 (frozen-panel semantics), R3
(fallback + coverage surfacing)

**Dependencies:** Unit 3

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (flag;
  `UniversalScore.lift_penalty` field; injection in
  `score_from_complements`; subtraction in `score` and
  `to_legacy_buckets`)
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (fused-mirror
  subtraction; panel FROZEN at baseline config during sweeps —
  document the approximation in the docstring)
- Modify: audit report surface (panel-coverage fraction line)
- Test: `tests/test_universal_scorer_lift.py` (create)

**Approach:**
- Field injected at construction (the `embedding_contribution`
  precedent — `UniversalScore` has no card identity) at BOTH
  construction sites in `score_from_complements` — the main results
  loop AND the staple-only fallback loop; the staple-only exemption
  decision governs the second site explicitly, not by omission.
  Subtraction on the assembled total at all three sites, with a named
  `"lift"` bucket in `to_legacy_buckets` (invariant test extended).
- `lift_penalty = λ·panel_mean.get(name, global_median)` — the median
  fallback, never λ·0.
- Staple-only candidate decision made here with a hand-computed check
  and pinned by a test either way.
- `ScoringConfigInputs` registration deliberately NOT here (flip-time
  convention).

**Execution note:** Test-first with hand-computed pairs (the
color-IDF/concave probes' pattern).

**Test scenarios:**
- Happy path: flag OFF → `--expect-identity` PASS; field is 0.0
  everywhere.
- Happy path (flag ON, patched): hand-computed pair — candidate with
  panel_mean 0.4 at λ=0.5 loses exactly 0.2 from all three totals;
  three-site agreement asserted.
- Edge case: card missing from panel map → global-median penalty
  (never zero, never KeyError).
- Edge case: staple-only candidate → pinned per the decision made in
  this unit.
- Edge case: negative lift allowed (score < penalty) — ordering
  semantics still deterministic.
- Error path: flag ON with no panel artifact present → the
  `lift_panel` loader raises on first access with a rebuild hint (a
  deliberate R-004 exception; covers engine AND direct bench
  callers), never a silent all-median run.
- Integration: optimizer fused mirror equals `to_legacy_buckets`
  total with the flag ON (fidelity invariant extended); optimizer
  sweep with flag ON does not attempt a panel rebuild (frozen
  semantics pinned).

**Verification:** suite green; identity PASS flag-off; fidelity test
extended and green flag-on.

- [ ] **Unit 5: Flip + bounded evidence matrix (≤7 batteries)**

**Goal:** Measured SHIP/DECLINE/INVESTIGATE decision under origin R7,
R8, R11.

**Requirements:** origin R4 (λ grid), R7 (kill-order), R8 (two
watch-list strata), R11 (gates)

**Dependencies:** Unit 4

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py`
  (`ScoringConfigInputs` + flag + λ + panel digest),
  `src/mtg_synergy_graph/bench/tensor.py` (hash segments)
- Modify: `tests/test_pathway_flag_gate.py` (_fields pin),
  `tests/test_scoring_weights.py` (production-hash pin),
  `tests/test_universal_scorer_lift.py` (hash-flip test)
- Test artifacts: per-cell sidecar outputs recorded in the decision
  record

**Approach:**
- Compute watch-list stratum (b) — the 20 commanders whose EDHREC
  top-30 has the highest mean panel_mean — BEFORE the sweep; name
  both strata in the decision record.
- Kill-order per origin R7: λ ∈ {0.25, 0.5, 0.75, 1.0} on lift-alone
  (4 batteries, each with tie-density/top-30-spread sidecar +
  watch-list extraction); blanket DECLINE at every λ ends the cycle;
  otherwise tier arm, tier+anthem arm (as-landed key design), and one
  staple-ablated arm at the winning λ (3 batteries). Ceiling: 7.
- Gates per arm: cliff < −0.05 on 500 → DECLINE; SHIP ≥ +0.010 agg or
  the goal-aligned alternative (unjustified-OUTRANKED + monoculture
  down, agg ≥ −0.010, fresh gem non-degrading); cumulative floor vs
  `pre-lift-normalization`.
- Outcome paths per template: SHIP → atomic re-pin (both fixtures) +
  RULE_HISTORY + CLAUDE.md; DECLINE → null-result doc + flag stays
  OFF + revert scoring integration only if it carries maintenance
  cost (the inert field may stay, per the plan-002 surviving-infra
  convention); INVESTIGATE (gem-dominant: fresh gem ≥ +0.02
  with aggregate NDCG in (−0.010, +0.010) — the house trigger) →
  hold, stub decision record, no re-pin.

**Test scenarios:**
- Happy path: hash flips with the flag and with a λ change and with a
  panel-digest change; restores after.
- Integration: the batteries themselves (audit + per-commander NDCG +
  forensics + fresh-gem readings per cell) — recorded, not unit-tested.

**Verification:** decision record with per-cell numbers for every
battery, both watch-list strata, and one declared outcome with the
gate arithmetic shown.

- [ ] **Unit 6: R10 rider — conditional rank_bonus ablation**

**Goal:** Re-run the deferred Unit-3 (plan 002) purity removal iff the
lift outcome improved tie density.

**Requirements:** origin R10

**Dependencies:** Unit 5 outcome declared

**Files:**
- Modify (SHIP path): `src/mtg_synergy_graph/universal_scorer.py`,
  `src/mtg_synergy_graph/engine.py`, and the bench mirrors
  (`bench/optimize.py`, `bench/forensics.py`,
  `bench/forensics_report.py`) per plan-002 Unit 3's file list
- Test: `tests/test_universal_scorer_coverage.py` extensions

**Approach:**
- Gate on the Unit 5 tie-density sidecar: no improvement → record and
  leave deferred (updated evidence), no code change.
- Otherwise: two-step ablation (rank_bonus alone, then + sort
  tiebreak), non-probe gates, decide with the panel-rebuild question
  (stale-panel caveat vs second build) resolved from wall-clock
  reality and recorded.

**Test scenarios:**
- (SHIP path only) the plan-002 Unit 3 scenarios carry over: identical
  candidates ± edhrec_rank → identical totals; NULL-rank cards carry
  no deficit; missing column tolerated; bench mirrors swept in the
  same commit (forensics tiebreak self-check green).

**Verification:** either a recorded no-run decision with sidecar
numbers, or the removal landed under its gates with re-pin.

- [ ] **Unit 7: Outcome handling and docs**

**Goal:** Every terminal state leaves the standard paper trail.

**Requirements:** origin R11 (success definition), Success Criteria

**Dependencies:** Units 5–6 resolved

**Files:**
- Modify: `docs/RULE_HISTORY.md`, `CLAUDE.md` (new commands/flags if
  shipped), this plan's checkboxes/frontmatter
- Create (DECLINE path): `docs/solutions/best-practices/`
  null-result doc naming exactly what information the lift baseline
  lacked

**Test scenarios:**
- Test expectation: none — documentation unit.

**Verification:** plan status updated; memory index updated; the
escalation target (role/quota sibling) is either funded with evidence
or explicitly not needed.

## System-Wide Impact

- **Interaction graph:** the lift field touches every ranked total —
  audit fixtures, forensics live pass, optimizer basis, explain
  rendering. Sequential landing (one scoring change in flight)
  continues to apply.
- **Error propagation:** malformed panel artifact raises at loader
  init (drift-check pattern); missing panel with flag ON is a hard
  error at init, not a silent all-median run — pinned by a Unit 4
  error-path test.
- **State lifecycle risks:** panel staleness is the new hazard class —
  hash discipline (digest in `ScoringConfigInputs`) + the coverage
  warning are the guards; every scoring-config change while flag ON
  costs a panel rebuild (accepted workflow tax, documented).
- **API surface parity:** `UniversalScore` gains a field — sweep
  `to_legacy_buckets` named-bucket invariant and explain rendering.
- **Integration coverage:** the batteries are the integration proof;
  unit tests cannot show ranking-level effects.
- **Unchanged invariants:** rule set, IDF form, dampener semantics,
  gem-gate definition, legality filter, embeddings flag.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| R0 kill-test fails → cycle over before integration | That is the designed cheap exit; the null-result doc closes the calibration family with evidence |
| Staple-heavy commanders cliff (the new failure population) | Watch-list stratum (b) named before the sweep; λ grid includes low values |
| Panel-composition choice silently decides which archetypes get taxed | Two-composition kill-test + explicit leave-one-out decision with numbers (Unit 2) |
| Lift compresses score spread → tiebreak region grows (alphabet effect) | Tie-density/spread sidecar on every λ cell; Unit 6 conditioned on it |
| Panel rebuild tax makes future probes expensive | Frozen-panel optimizer semantics; tax documented; digest keeps correctness |
| EDHREC-data refresh churns a rank-derived panel selection | Committed selection artifact changes only when deliberately rebuilt; churn frequency noted in docs |
| Evidence matrix creep | Hard ceiling ≤7 batteries; kill-order; anthem key redesign explicitly out of scope |

## Documentation / Operational Notes

- SHIP: CLAUDE.md gains the panel-build command + rebuild-after-refresh
  step (next to `build_embeddings.py`); RULE_HISTORY entry with the
  full matrix table.
- DECLINE at any stage: null-result doc; the surviving-infra
  convention from plan 002 applies.
- The `pre-lift-normalization` tag annotation is the cycle's baseline
  ledger (fresh gem values re-base all plan-002-era gem figures).

## Sources & References

- **Origin document:** [docs/brainstorms/2026-07-02-lift-normalization-requirements.md](../brainstorms/2026-07-02-lift-normalization-requirements.md)
- Mandate: [docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md](../solutions/best-practices/calibration-track-null-result-2026-07-02.md)
- Template: [docs/plans/2026-07-02-001-feat-color-conditioned-idf-probe-plan.md](2026-07-02-001-feat-color-conditioned-idf-probe-plan.md)
- Predecessor: [docs/plans/2026-07-02-002-fix-scoring-flaw-remediation-plan.md](2026-07-02-002-fix-scoring-flaw-remediation-plan.md), PR #85
