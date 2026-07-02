---
date: 2026-07-02
topic: color-conditioned-idf
type: requirements
title: Color-identity-conditioned IDF denominator (probe)
status: open
tags:
  - scoring
  - idf
  - universal-scorer
  - audit-gated
  - probe
ideation_ref: docs/ideation/2026-06-10-synergy-accuracy-ideation.md (idea #2, FUNDED 2026-07-02)
priors:
  - docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md
  - docs/brainstorms/2026-05-04-bm25-idf-requirements.md
  - docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md
  - docs/solutions/best-practices/read-sibling-solutions-before-improvement-levers-2026-05-05.md
---

# Color-identity-conditioned IDF denominator (probe)

## Problem Frame

IDF weights in `src/mtg_synergy_graph/universal_scorer.py` compute
`N` — the number of distinct candidates matching each
`(rule_id, cmdr_event, cand_event, filter_group)` tuple — over the
**color-unfiltered set of candidates that matched that key** in the
commander's complements (`_compute_idf_basis`, line ~711; the
complement search itself is not color-filtered). But the ranking
pool a commander is actually scored against is filtered to its
color-identity-legal subset *after* scoring (`engine.py::page()`,
lines ~364–383). The denominator therefore counts candidates the
commander can never play: a mono-white commander's "sacrifice cost"
IDF is diluted by every black sac outlet that matched the key. Note
the per-key effect size is bounded by that key's matched-candidate
count, not by the 32k universe — dilution is per-key.

Forensics evidence (2026-07-02 run, PR #75 instrument): OUTRANKED is
the largest miss bucket at 46.2% of 2,642 misses — rules fire on the
missed cards but they lose the ranking. Miscalibrated IDF weights are
a direct candidate cause. Conditioning the denominator on the
color-legal pool makes specificity mean "specific *within the pool
this commander chooses from*" — the same methodological move EDHREC
made (synergy conditioned on color-identity baseline).

**Distinctness from the declined BM25 probe:** BM25 changed the IDF
*curve shape* over the same population and cliffed (65/500 commanders
< −0.05). This probe changes the *population* under the unchanged
`1/log2(1+N)` curve. Different failure surface; same gate discipline.

**Named counter-hypothesis (tested, not assumed away):** the same
forensics run shows OUTRANKED missed cards' contributions are
dominated by *flat-density* families (spell_density 24.2% +
tribal_density 21.8%) which R3 leaves untouched, while
color-conditioning can only *raise* non-flat weights — potentially
amplifying the single-family displacers (Zur value_engine 100%,
Feather cost_reducer 97.5%) relative to the flat-carried missed
cards, i.e. widening the very bucket the probe targets. The SHIP
criterion "OUTRANKED share reduced" and the R10 null-result capture
are the tests of this hypothesis; if it wins, the funded follow-up
is concave within-family aggregation (ideation idea #3-weak), not
more IDF work.

## Requirements

**Scoring change**
- R1. `N` for non-flat IDF keys is counted over only candidates in
  the commander's color-identity-legal pool (same legality predicate
  as `engine.py::page()`, all three conditions conjoined: color
  identity ⊆ commander union ∧ non-empty `card_types` with no
  `NON_EDH_CARD_TYPES` entry ∧ `legal_commander != 0`), instead of
  the color-unfiltered matched set. The color union MUST be the
  identity union over the full `commander_set`
  (`_color_identity_union` semantics); the existing
  `commander_set[0]`-only staple-pip lookup in the scorer
  (`universal_scorer.py` ~line 910) is NOT the pattern to copy —
  partner pairs would get a first-partner-only pool.
- R2. Denominator-only: the complement search, scoring pool, tensor
  population, and `page()` filtering are unchanged. Out-of-color
  cards remain scoreable via `score_one` with the same formula;
  their numeric scores may shift where shared keys are conditioned.
  (Decided 2026-07-02 — see Key Decisions.)
- R2a. Orphaned-key policy (decided 2026-07-02): a key whose in-pool
  N is 0 (all matching candidates out-of-color) retains its
  unconditioned global-N weight. Such keys cannot affect the
  color-legal ranking (no legal candidate matches them); this
  avoids both the `1/log2(1+0)` division-by-zero and the implicit
  weight-1.0 fallback that would inflate out-of-color scores and
  corrupt tensor/ablation reads.
- R3. Flat-weight rules (`_FLAT_COUNT_RULES`, `_FLAT_WEIGHT_OVERRIDES`)
  are untouched; the change applies only to `base_idf_non_flat` keys.
- R4. Existing static dampening logic composes unchanged on top of
  the new N: `cond_mult`, and the panharmonicon `n = max(n, 30)`
  floor (the floor binds more often in small pools — accepted).
- R5. Behavior is deterministic and EDHREC-free at inference,
  consistent with the project constraint.

**Probe discipline (house pattern from the BM25 probe)**
- R6. Pre-committed outcomes with trigger conditions (decided
  2026-07-02; all NDCG/gem deltas on the regenerated 500-cmdr
  fixture):
  - **SHIP** — aggregate NDCG@30 ≥ +0.010, zero R7 cliff
    violations, and gem delta ≥ −0.01 (soft guardrail).
  - **DECLINE** — any R7 cliff violation, or aggregate NDCG@30
    ≤ −0.010, persisting after the R8a re-sweep step.
  - **INVESTIGATE** — (a) gem-dominant quadrant: gem delta ≥ +0.02
    while aggregate NDCG@30 is flat (−0.010, +0.010) → route to an
    FR6 gem-primary escalation brainstorm instead of discarding
    (the BM25 probe's +0.0407 gem result landed exactly here);
    (b) raw gate failure → R8a re-sweep before final verdict.
  - **INCONCLUSIVE** — all deltas within noise (|NDCG| < 0.010,
    |gem| < 0.02, no cliffs).
- R7. Hard cliff gate: any commander with NDCG@30 delta < −0.05 on
  the 500-cmdr fixture → DECLINE (reuses the per-commander NDCG
  reporting infrastructure shipped by plan 2026-05-04-001 Unit 1).
- R8. The SHIP aggregate is defined as the mean of the per-commander
  deltas emitted by the per-commander NDCG report
  (`src/mtg_synergy_graph/bench/per_commander_ndcg.py`) over the
  500-cmdr fixture; planning should add an aggregate summary line to
  that report so the SHIP number is machine-emitted rather than
  hand-averaged over 500 rows. `hidden_gem_hit_rate` participates
  only as the R6 soft guardrail / INVESTIGATE trigger — it is not
  a project-level commit gate (FR6 escalation policy unchanged).
- R8a. Calibration-confound control (decided 2026-07-02): flat
  weights, staple bonus, and `_RULE_QUALITY_MULTIPLIER` were tuned
  against global-N IDF, and conditioning only ever raises non-flat
  weights. If the raw result fails the R6 gates, run ONE
  `bench.py audit --optimize` multiplier re-sweep on top of the
  conditioned IDF before finalizing DECLINE (mirrors the BM25
  probe, which freshly tuned weights before its verdict). A DECLINE
  without this step measures stale calibration, not the population
  axis.
- R9. Every scoring-path change passes `bench.py audit` per
  `memory/feedback_audit_every_change.md`; a SHIP re-pins via
  `bench.py audit --repin --yes`.
- R10. On DECLINE, capture a null-result solution doc (sibling to
  `bm25-idf-null-result-2026-05-04.md`) including the per-commander
  failure pattern, so the population axis is marked probed.
- R13. The denominator mode is expressed as a registered scoring-
  config input (flag or version field added to
  `ScoringConfigInputs` / `get_scoring_config_inputs()`), per the
  house rule in `universal_scorer.py` ("if you tune score() with a
  new module-global dict or flag, add it here too"). This flips
  `compute_config_hash`, making tensor staleness detectable and
  giving `.audit/forensics_history.csv` a (config) boundary marker
  between pre- and post-probe rows.

**Evaluation prerequisites**
- R11. Regenerate the stale 500-cmdr fixture first
  (`scripts/bootstrap_golden_set_500.py`) — the current one predates
  the 2026-07-01 DB refresh (a1eae6e color_identity fix). This is
  independently mandated by CLAUDE.md after data refreshes.
  **Baseline-integrity precondition:** the fixture MUST be
  regenerated and pinned from baseline (pre-probe) scoring code —
  verified via `bench.py audit --expect-identity` or a clean
  pre-probe commit — BEFORE the denominator change is applied.
  Regenerating with the probe active would make pinned scores equal
  probe scores, collapsing every per-commander delta to ~0 and
  passing the R7 cliff gate vacuously.
- R12. Report the per-commander delta histogram sliced by color-
  identity size (mono / 2 / 3 / 4 / 5-color / colorless), since pool
  shrinkage — and therefore expected effect size — is a direct
  function of identity size (5-color ≈ no-op; colorless is the
  extreme case).

## Success Criteria

- Probe reaches exactly one pre-committed outcome with the audit
  evidence attached (`.audit/last.md` + per-commander report).
- SHIP: aggregate NDCG@30 ≥ +0.010 (500-cmdr), zero cliff-gate
  violations, gem delta ≥ −0.01 (per R6); re-pinned; forensics
  re-run shows OUTRANKED share reduced, reported alongside the
  per-commander delta histogram sliced by color-identity size
  (per R12) as part of the SHIP evidence package.
- DECLINE is also success if the null result is captured per R10
  (including the R8a re-sweep evidence and whether the named
  counter-hypothesis — displacer amplification — fired) — the
  point of a probe is a clean answer, not a forced ship.
- INVESTIGATE (gem-dominant) is also success: it routes the result
  into the FR6 gem-primary escalation rather than discarding it.

## Scope Boundaries

- No pool restriction of complement search or scoring (R2) — a
  possible follow-up if the probe SHIPs, as a coherence/perf change.
- No λ-blend between global and color IDF in v1 — that is the
  pre-identified fallback if the pure form DECLINEs on cliffs, not
  part of the probe.
- No IDF curve-shape changes (BM25 axis is closed per null-result doc).
- No changes to flat-weight density rules, staple bonus, anti-synergy,
  or the sort key.
- No partner/background multi-commander special-casing beyond what
  `_color_identity_union` already provides.

## Key Decisions

- **Denominator-only variant** (2026-07-02, user-selected): cleanest
  probe; isolates the population effect from pool-restriction effects;
  keeps `score_one`, tensor, and ablation semantics stable so the
  audit diff is attributable to exactly one mechanism.
- **NDCG-primary scoped to this probe only** (2026-07-02,
  user-selected): mirrors the BM25 probe's metric regime for
  comparability of the two IDF-axis results. Explicitly NOT a house
  default — the project-wide framing remains gem-primary with
  EDHREC as sanity check (`memory/feedback_edhrec_not_goal.md`);
  the gem-dominant INVESTIGATE route in R6 exists precisely so a
  gem-positive result is escalated per FR6 rather than discarded
  the way BM25's +0.0407 was.
- **Orphaned keys keep global-N weight** (2026-07-02,
  user-selected): see R2a — zero effect on the color-legal ranking,
  no div-by-zero, no fallback-1.0 inflation.
- **Re-sweep before verdict** (2026-07-02, user-selected): see R8a —
  a DECLINE must be attributable to the population axis, not to
  surrounding constants tuned for the old denominator.
- **500-cmdr fixture as probe basis** (house default per
  `optimizer-fixture-size-2026-04-30.md`); 100-cmdr canonical stays
  the commit-gate basis.

## Dependencies / Assumptions

- `IdfBasis` is already per-commander and cached per commander
  (verified); a per-color-identity-bucket cache (≤32 identity masks +
  colorless) is a planning-level optimization, not a requirement.
- Candidate names in `PortComplement.candidate` are joinable to the
  legality predicate data already bulk-loaded by
  `build_candidate_cache` / `build_penalty_context` (verified pattern
  exists in `engine.py`; exact plumbing is planning's choice).
- a1eae6e (Scryfall-sourced `color_identity`) makes the color data
  trustworthy; this probe should not land on a pre-fix DB.

## Outstanding Questions

### Resolve Before Planning
- ~~Per-key color-skew kill-test~~ **RESOLVED 2026-07-02 —
  GREEN-LIGHT with one flag.** Measured on the live DB across 8
  commanders spanning all identity classes (per-key
  `w_new/w_old = log2(1+N_all)/log2(1+N_legal)` over non-flat keys):

  | class | pool | keys | shrink p50 | orphan% | n≤3% | inflation p10/p50/p90 | spread |
  |-------|-----:|-----:|-----------:|--------:|-----:|----------------------|-------:|
  | mono-R (Krenko) | 7,272 | 3 | 0.22 | 0% | 0% | 1.10/1.42/2.09 | 1.91 |
  | mono-W (Adeline) | 7,316 | 4 | 0.50 | 0% | 25% | 1.21/1.40/1.48 | 1.23 |
  | mono-B (Tergrid) | 7,266 | 29 | 0.45 | 10.3% | 21% | 1.00/1.27/1.46 | 1.46 |
  | 2c-UB (Wilhelt) | 12,287 | 10 | 0.48 | 0% | 20% | 1.06/1.12/1.73 | 1.64 |
  | 3c-BRG (Korvold) | 17,834 | 23 | 0.63 | 4.3% | 22% | 1.04/1.12/1.46 | 1.40 |
  | 4c (Atraxa) | 23,923 | 6 | 0.67 | 0% | 33% | 1.02/1.07/1.29 | 1.27 |
  | 5c (Ur-Dragon) | 30,943 | 3 | 0.97 | 0% | 0% | 1.00/1.00/1.02 | 1.02 |
  | colorless (Kozilek) | 2,640 | 4 | 0.20 | 0% | 75% | 1.00/2.58/3.46 | 3.46 |

  Readings: (a) inflation is NOT a uniform rescale — spread 1.2–1.9
  for mono/2c/3c means real re-ranking signal; premise survives.
  (b) 5-color is a no-op as predicted (spread 1.02). (c) Orphaned
  keys exist in practice (Tergrid 10.3%) — R2a policy confirmed
  necessary. (d) **Flag: colorless commanders** are the acute cliff
  risk (75% of keys in the n≤3 regime, median inflation 2.58×); the
  R7 gate and R12 slice will observe them (15 colorless commanders
  in the 500-cmdr fixture), and a colorless-driven cliff DECLINE
  should be read as class-specific — the λ-blend fallback scoped to
  small pools is the pre-identified remedy, not a full revert.

### Deferred to Planning
- [Affects R1][Technical] Where to source the legal-candidate set
  inside `score_all_universal` with minimal plumbing: reuse
  `CandidateCache` rows vs a dedicated `legal_cards()`-shaped query;
  keep the IdfBasis cache-invalidation contract intact.
- [Affects R1][Technical] Whether `_compute_idf_basis` takes the
  legal set as a parameter (pure function, preferred for the
  optimizer's cached-basis path) or the filtering happens upstream
  on the complements sequence fed to frequency counting only.
- [Affects R7][Technical] Whether the optimizer's cached-basis path
  (`IdfBasis` reuse across grid cells) needs a rebuild trigger — the
  basis becomes commander-color-dependent, which it already is via
  complements, so likely no contract change; verify.
- [Affects R12][Technical] Slice thresholds for the identity-size
  histogram report format (how the R12 slices appear in the audit
  output) — the measurement itself moved to Resolve Before Planning.

## Next Steps

-> Kill-test passed (2026-07-02): `/ce-plan` for structured
implementation planning
