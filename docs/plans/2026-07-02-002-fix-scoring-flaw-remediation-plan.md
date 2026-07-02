---
title: "fix: Scoring-flaw remediation — density calibration, EDHREC purity, coverage gaps"
type: fix
status: active
date: 2026-07-02
origin: docs/ideation/2026-06-10-synergy-accuracy-ideation.md
---

# fix: Scoring-flaw remediation — density calibration, EDHREC purity, coverage gaps

## Overview

A four-agent architecture review (this session, 2026-07-02) produced a ranked
flaw list explaining the two forensics-confirmed failure modes: OUTRANKED
(46.2% of misses — over-scored candidates displacing labeled cards) and
NO_RULES (41.4% — labeled cards no rule can reach). This plan remediates the
flaws in leverage order: a concrete bug fix and EDHREC-purity cleanup first,
then flag-gated calibration probes for the density family, then coverage
expansion for the miss engine. Every scoring-path unit carries pre-committed
SHIP/DECLINE gates in the plan-2026-07-02-001 style; a clean DECLINE with a
captured null-result doc counts as success for the probe units.

## Problem Frame

Aggregate NDCG@30 is 0.232 on the 100-cmdr fixture (`.audit/forensics.md`,
2026-07-02). The review found:

- **False-positive engine (A):** four flat-weight density rules bypass IDF
  (`tribal_density` 0.5 vs median genuine IDF match ~0.09–0.15); the tribal
  emitter has no payoff predicate; the Human/Warrior/Soldier skiplist guards
  only the vanilla fallback path, so token-producing commanders (Adeline)
  activate ~1,500-card Human pools through the primary path; the
  concentration dampener requires `len(syn_by_rule) >= 2` and so exempts
  exactly the single-family monoculture cards it exists to curb; flat
  overrides are outside `_RULE_QUALITY_MULTIPLIER` and therefore invisible
  to the optimizer.
- **EDHREC leakage (B):** `rank_bonus = 0.005 * max(0, 1 - rank/30000)` sits
  inside the ranked total (`universal_scorer.py`) — distinct from the sort
  tiebreak that the R8 ablation measured at +0.000000. This violates the
  EDHREC-clean inference claim and biases against unreleased/obscure cards.
- **Structural miss engine (C):** candidate-side `static.Continuous` ports
  (159 NO_RULES cards) are consumed only by tribal-lord overlap;
  `trigger.Phase` (56) and Forge's `effect.Effect` wrapper (27) project to
  UNKNOWN and are unmatchable; ChangeZone resonance requires bitwise-equal
  zone pairs and tutors produce zero attrs; generic ramp/draw/removal is
  architecturally unscoreable (deferred — design decision, see Scope).

**Baseline caveats (doc-review findings, 2026-07-02):**

- **Dual-total split.** Production ranking (`engine.page`) sorts by
  `to_legacy_buckets()["total"]`, which contains NO concentration
  dampener; the dampened `UniversalScore.score` is consumed only by the
  audit-fixture path. Fixture NDCG and live/forensics NDCG therefore rank
  by different formulas wherever the dampener fires. Unit 1 measures the
  divergence; Unit 4 must change a choke point shared by both totals.
- **Baseline figure.** This plan's headline 0.232 is the forensics
  canonical-denominator figure; CLAUDE.md's historical ~0.256 comes from a
  different measurement path. All plan deltas are read against the Unit 1
  baseline numbers, not the historical figure.
- **OUTRANKED ≠ all false positives.** The forensics R9 view shows a
  substantial justified-divergence share inside the OUTRANKED bucket
  (e.g., Meren 28/28 justified). Unit 1 records the unjustified share as a
  named baseline so Phase 2 headroom is quantified, not assumed.

## Requirements Trace

- R1. Fix the tribal skiplist bypass so token-of-own-type production alone
  cannot activate an overbroad tribe (review flaw A3).
- R2. Remove EDHREC signal from the inference path (`rank_bonus` term and
  `edhrec_rank` sort tiebreak), gated by an ablation measurement (B1).
- R3. Density-family calibration: payoff-qualified tribal emission (A2),
  pool-size-aware density weights (A1), concave within-family aggregation
  reaching single-rule candidates (A4), spell/tribal overlap dedup (A6).
- R4. Expose flat weight overrides to the optimizer sweep (A5).
- R5. Coverage expansion: classify `trigger.Phase`, unwrap `effect.Effect`
  (C3); zone-pair equivalence classes + tutor attr production (C4);
  candidate-side static/anthem consumption (C2).
- R6. Every scoring-path change passes `bench.py audit` with pre-committed
  gates (measurement surfaces: aggregate NDCG and per-commander cliffs on
  the 500-cmdr fixture; forensics secondaries and gem instruments on the
  100-cmdr canonical). Probe SHIP: aggregate ≥ +0.010 NDCG@30, OR the
  goal-aligned alternative — forensics secondaries materially improve
  (displacer monoculture share and unjustified-OUTRANKED share down) AND
  aggregate ≥ −0.010 AND the decontaminated gem sidecar (R9a) is
  non-degrading. DECLINE on any per-commander cliff < −0.05 or
  decontaminated-gem delta < −0.01. Bug fixes and purity removals use the
  asymmetric non-probe gate defined in Key Technical Decisions.
- R7. One scoring change in flight at a time; each unit lands or declines
  before the next starts, so audit attribution stays clean.
- R8. New rules whose targets sit outside the golden 100 pass
  `rule_quality_gate.py` Gates A (vacuum-fill) and B (flat-noise).
- R9. Cumulative floor: at every SHIP decision, deltas are also reported
  against the Unit 1 `pre-scoring-remediation` baseline (NDCG, gem,
  forensics bucket shares); cumulative NDCG vs that baseline must stay
  ≥ −0.015 unless forensics secondaries improved — per-unit tolerances
  cannot compound silently.
- R9a. Decontaminated gem sidecar: every flip evidence package includes a
  read-only recomputation of hidden_gem_hit_rate with flat-density rules
  excluded from the plausibility legs (no change to the committed B3 gate
  definition — FR6 stays honored). Gem-based DECLINE triggers in Units 4–6
  read the sidecar, because the committed gem metric counts monoculture
  flat-density cards as gems and would fire against the units' own success
  condition.

## Scope Boundaries

- No changes to the gem plausibility-gate definition (B3) in this plan —
  `feedback_hidden_gem_metric.md` / FR6 requires a separate brainstorm +
  plan cycle, and changing it simultaneously with the dampener (Unit 4)
  would confound the gem-rate reading.
- No generic deck-glue axis (C1: ramp/draw/removal quotas or lift
  normalization) — that is ideation #4's funded lever and needs its own
  design cycle.
- No node_kind hot-path matcher refactor (C5) — architecture-level change;
  this plan only expands coverage within the existing raw-key matcher.
- No IDF curve-shape or denominator-population changes — both axes closed
  by null results (`bm25-idf-null-result-2026-05-04.md`,
  `color-conditioned-idf-null-result-2026-07-02.md`). Unit 6's pool-size
  scaling is a per-rule weight schedule, not an IDF-family change, and must
  cite both nulls in its outcome doc.

### Deferred to Separate Tasks

- C1 generic-glue / lift-normalization axis: separate ce-brainstorm +
  ce-plan (ideation #4, FUNDED).
- C5 node_kind-based matching path: separate refactor plan after this
  plan's coverage units prove the vocabulary is worth routing through.
- B3 gem plausibility-gate hardening (exclude flat density rules from the
  ≥2-rules leg): separate brainstorm per FR6 escalation. The R9a sidecar in
  this plan is read-only evidence, not a gate-definition change.
- Counter-class compatibility relaxation (counter archetypes matching at
  class level): needs its own evidence base; cut from Unit 9 scope.
- PPMI-mined event-map expansion (ideation #6): complements Unit 8/9 but
  runs as its own audit-gated batch.

## Plan-Level Success Criteria

The plan is falsifiable against the owner's complaint, not only per-unit
gates. After Phase 3 (measured on the forensics instrument vs the Unit 1
baseline):

- Displacer monoculture: no commander's top-30 above ~70% single-family
  share among the current 87–100% cases.
- Unjustified-OUTRANKED share: down materially from the Unit 1 recorded
  baseline (target set in Unit 1 once the R9-justified share is known).
- NO_RULES share: down by at least the reach of Units 8–10 as measured in
  Unit 1's NO_RULES decomposition.
- Decontaminated gem rate: non-degrading vs baseline.
- Escalation rule: if ≥2 of Units 4–6 DECLINE, the OUTRANKED lever moves
  to the C1/lift-normalization design cycle rather than further
  calibration probes.

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/universal_scorer.py` — `_FLAT_COUNT_RULES`
  (~:490), `_compute_idf_basis` flat short-circuit (~:731),
  dampener `len(syn_by_rule) >= 2` (~:414), `rank_bonus` (~:965, ~:987),
  `ScoringConfigInputs` (~:243).
- `src/mtg_synergy_graph/complement_rules/density.py` — tribal emitter
  (~:669), `_VANILLA_TRIBAL_SKIPLIST` (~:575), spell_density subtype path
  (~:548).
- `src/mtg_synergy_graph/complement_rules/core.py` —
  `_commander_subtypes_from_ports` token gates (~:831–991), ChangeZone
  resonance zone equality (~:500), trigger-resonance same-event map (~:563).
- `src/mtg_synergy_graph/graph_engine.py` — `_effect_produced_attrs`
  battlefield-only ChangeZone (~:395), identity fallthrough (~:144).
- `src/mtg_synergy_graph/port_graph/projection.py` — UNKNOWN fallthrough
  (~:139); `src/mtg_synergy_graph/data/event_match_seed.json` — 20 trigger
  events.
- `src/mtg_synergy_graph/engine.py` — sort key with `edhrec_rank`
  tiebreak (~:413–419).
- Probe-plan template: `docs/plans/2026-07-02-001-feat-color-conditioned-idf-probe-plan.md`
  (baseline-tag → flag-gated OFF → measurement → flip+gates → conditional
  re-sweep → outcome paths). Flag-gate test template:
  `tests/test_pathway_flag_gate.py`.
- Density tests: `tests/test_density_rules.py` (vanilla anchor ~:998–1053,
  token Gate 5 ~:912–980). Scorer tests:
  `tests/test_universal_scorer_coverage.py`,
  `tests/bench/test_universal_scorer_identity.py`.

### Institutional Learnings

- `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md` — flat-noise
  pathology; Gates A/B mandatory for rules targeting outside the golden 100.
- `docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md`
  — R6/R7/gem gate numbers; small-N key saturation risk when moving rules
  from flat weights toward frequency-derived weights; explicitly funds
  concave within-family aggregation (Unit 4's mandate).
- `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md` — IDF
  curve-shape axis closed; NDCG and gem axes can diverge sharply.
- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  — identity-clean flag OFF; `ScoringConfigInputs` field only at flip time.
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
  — define the flip bar quantitatively before the sweep.
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md` — any
  `--optimize` runs use the 500-cmdr fixture.
- `docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`
  — verify new-rule targets appear on the evaluation surface before building.
- `docs/solutions/best-practices/sweep-writers-not-just-readers-when-changing-artifact-formats-2026-04-25.md`
  — if `scoring_weights.json` keys are restructured, sweep `scaffold_rule.py`'s
  regex patcher too.
- `docs/solutions/best-practices/rule-consolidation-null-result-2026-04-24.md`
  — run `--collinearity` after weight/aggregation changes.
- `docs/RULE_HISTORY.md` — tribal fallback added 2026-04-18 with known
  voltron regressions; `tap_type_feeder` precedent: fix density flooding by
  narrowing + lower weight, not deletion.

## Key Technical Decisions

- **Bug fix and purity removals are plain commits, probes are flag-gated.**
  A3 (Unit 2) and B1 (Unit 3) are defect/purity corrections measured by a
  before/after audit; revert is `git revert`. The calibration changes
  (Units 4–6) follow the house probe pattern: `_ENABLE_*` flag default-OFF,
  bitwise-inert (`--expect-identity` PASS), flip only with the evidence
  package.
- **Gate asymmetry for non-probe units.** Purity/bug units do not use the
  +0.010 SHIP bar (they are expected to *cost* a little inflated NDCG):
  SHIP unless (a) any 500-cmdr per-commander delta < −0.05 attributable to
  the change, or (b) aggregate NDCG delta < −0.010, or (c) gem delta
  < −0.01. Rationale: the metric itself was inflated by the flaw being
  removed; forensics secondaries (monoculture share, OUTRANKED share) are
  the positive evidence. **INVESTIGATE (non-probe)** is triggered when the
  gate legs conflict — aggregate NDCG in [−0.015, −0.010) or a gem-leg
  fail while forensics secondaries materially improve. It means: hold the
  change unlanded, run one focused forensics diagnosis pass (which
  commanders/families moved), then record an explicit SHIP-or-DECLINE
  decision with the diagnosis attached. It is not a third terminal state.
- **Goal-aligned probe SHIP alternative (R6) + decontaminated gem sidecar
  (R9a).** Two independent reviewers converged on the same defect: gating
  Units 4–6 on +0.010 NDCG-vs-EDHREC plus the contaminated gem metric
  makes the gates near-unsatisfiable by construction, because the correct
  fixes reduce agreement with EDHREC on tribal commanders and evict cards
  the current gem gate wrongly counts. The SHIP alternative and the
  sidecar (defined in Requirements Trace) are the response; the committed
  gem-gate definition is untouched.
- **Cumulative floor (R9).** Per-unit gates are measured against the most
  recent pin, which re-baselines after every SHIP; without a cumulative
  check, Units 2–3 could stack −0.018 silently and Unit 4 could "recover"
  against the deflated baseline. Every SHIP decision reports deltas vs the
  `pre-scoring-remediation` tag.
- **Dual-total reconciliation (Unit 4 precondition).** The concave
  aggregation must be implemented at a choke point consumed by BOTH totals
  (`UniversalScore.score` and `to_legacy_buckets()["total"]`), with the
  bench mirrors (`bench/optimize.py` fused total) updated in the same
  commit — otherwise the change moves audit NDCG while production
  rankings, `recommend.py`, and the forensics instrument stay frozen.
- **A3 fix is token-gate-scoped, not a blanket skiplist.** Blanket-applying
  `_VANILLA_TRIBAL_SKIPLIST` to the primary path would kill genuine
  Human-tribal commanders. The fix targets the admission route: a subtype
  admitted *only* because the commander produces a token of its own literal
  subtype (core.py token Gate 1) must additionally pass the skiplist or
  show a mechanical tribal reference (subtype in a `valid_filter` /
  `affected_scope`).
- **One scoring change in flight at a time (R7).** Sequential landing keeps
  `.audit/history.csv` attribution unambiguous and lets each probe's
  DECLINE revert cleanly to its own baseline tag.
- **Ordering: bug → purity → calibration → coverage.** The A3 bug is the
  cheapest confirmed defect. B1 lands second so every subsequent gate reads
  an EDHREC-clean score. Calibration (biggest OUTRANKED lever, pre-funded)
  precedes coverage (heavier: importer + re-import + re-pin cycles).
- **Re-pin discipline.** Units 2–3 shift scores without flipping
  `compute_config_hash` → on SHIP, re-pin both fixtures atomically in the
  landing commit. Units 4–6 flip the hash at their flip step (flag
  registration) → scratch-pin for tensor diagnostics, atomic re-pin of
  committed fixtures only on SHIP. Units 8–9 change seed JSON / importer
  output → re-import, `scripts/build_embeddings.py`, re-pin.

## Open Questions

### Resolved During Planning

- Should the tiebreak drop wait for its own measurement? No — R8 ablation
  already measured +0.000000 credit; it rides with Unit 3's audit run.
- Flag-gate the dampener change even though dampening is not in the config
  hash? Yes — identity-cleanliness while OFF is the review-safety property;
  the hash flip comes from the flag's `ScoringConfigInputs` registration at
  flip time, consistent with house pattern.
- Concavity vs payoff-tier first? Concavity (Unit 4) first: it is generic
  (also hits panharmonicon/cascade monocultures), pre-funded by the
  null-result doc, and its result de-risks how aggressive Units 5–6 need
  to be.

### Deferred to Implementation

- Exact concave transform for Unit 4 (per-family `sqrt`/`log`
  share-compression vs extending the existing dampener to
  `len(syn_by_rule) >= 1`) — pick after inspecting the tensor's family-share
  distribution; the unit pre-commits the gates, not the functional form.
- Payoff-predicate SQL shape for Unit 5 (port-join vs precomputed flag
  column) — decide against real query plans; must stay within the
  2-SQL-queries-per-rule budget.
- Which `trigger.Phase` sub-shapes are worth event-map rows in Unit 8 —
  driven by `--unknowns` ranking at implementation time.
- Whether `effect.Effect` unwrapping happens at extraction (importer) or
  projection (view) — depends on what the wrapper rows actually contain;
  extraction preferred if the inner Api is recoverable.

## Implementation Units

### Phase 1 — Baseline, bug fix, purity

- [x] **Unit 1: Baseline hygiene and branch setup** *(done 2026-07-02: tag `pre-scoring-remediation`; NDCG 0.236126; gem 0.8153 std / 0.7160 decontaminated; dual-total divergence 15/100 cmdrs, 46 cards; exploratory sweep skipped — flat keys not in grid until Unit 7)*

**Goal:** Clean baseline to measure every subsequent unit against.

**Requirements:** R6, R7

**Dependencies:** None

**Files:**
- Modify: `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md` (commit the outstanding 2026-05-22 re-sweep addendum as-is)

**Approach:**
- Create branch `fix/scoring-flaw-remediation` off current `main` (4e835c2).
- Commit the dirty solutions doc (docs-only commit).
- Verify `bench.py audit --expect-identity` PASS against the committed pin
  and that `tests/fixtures/golden_set_run_500.json` is fresh on this
  baseline (regenerated at 3f0dfaf; confirm config_hash match, do NOT
  regenerate if hashes agree).
- **Record named baseline numbers** (in the tag's annotation or a scratch
  note referenced by later units): aggregate NDCG (500-cmdr), forensics
  bucket shares, displacer monoculture shares for the 87–100% cases, the
  R9-justified vs unjustified split of the OUTRANKED bucket, a NO_RULES
  decomposition (shapes covered by Units 8–10 vs generic-glue/C1
  territory), and the decontaminated gem rate (R9a computation). These are
  the denominators for the cumulative floor (R9) and Plan-Level Success
  Criteria.
- **Measure the dual-total divergence**: count candidates per commander
  where the dampened `UniversalScore.score` and `to_legacy_buckets()
  ["total"]` orderings disagree in the top-30 — the baseline for Unit 4's
  reconciliation requirement.
- **Exploratory optimizer sweep** (design-time, no gate, no commit):
  one `--optimize` pass on current semantics with flat keys manually
  grid-included if cheap, to size whether the flat-weight problem is
  mere mistuning — its proposal magnitudes calibrate how aggressive
  Units 4–6 need to be. If infeasible without Unit 7's code, record that
  and skip.
- Tag `pre-scoring-remediation`.

**Test scenarios:**
- Test expectation: none — hygiene unit; verification is the identity audit
  and full suite green.

**Verification:**
- `--expect-identity` PASS; ~1230 tests green; tag exists; baseline
  numbers recorded and referenced by later units.

- [x] **Unit 2: A3 — tribal skiplist bypass fix (token-gate scoped)** *(SHIPPED 2026-07-02: cliff 0 violations on 500, agg −0.0003/−0.0001, gem +0.0007/−0.0005; lord payoff direction restored via `include_overbroad_tribes=True` after first pass tripped Adeline −0.0697; both fixtures re-pinned)*

**Goal:** A commander that merely produces tokens of its own literal
subtype no longer activates an overbroad tribe through the primary
subtype-extraction path.

**Requirements:** R1, R6

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (token Gate 1 in `_commander_subtypes_from_ports`)
- Modify: `src/mtg_synergy_graph/complement_rules/density.py` (if the skiplist constant moves to a shared location)
- Test: `tests/test_density_rules.py`

**Approach:**
- Narrow token Gate 1: a subtype admitted via own-type token production
  must also pass `_VANILLA_TRIBAL_SKIPLIST` OR appear as a mechanical
  tribal reference in the commander's ports (structured fields only:
  `valid_filter` / `affected_scope`).
- **Cover the second admission route** (doc-review, feasibility): the
  literal-relevance loop in `_commander_subtypes_from_ports` builds its
  haystack from `valid_filter + affected_scope + raw_line`, and Adeline's
  trigger `raw_line` contains the English TriggerDescription prose
  ("...create a 1/1 white Human creature token..."), so 'Human' enters
  independently of Gate 1. For skiplisted tribes, require structured-field
  evidence — `raw_line`/description prose alone is insufficient. Scope the
  narrowing to skiplisted tribes so non-skiplisted commanders keep current
  behavior.
- The skiplist constant must move to a shared location (`core.py` or a
  small shared module): `density.py` imports `core.py`, so `core.py`
  cannot import the constant from `density.py` — the "if" in the Files
  list is actually mandatory.
- Do NOT blanket-apply the skiplist to the whole primary path (protects
  genuine Human/Soldier tribal commanders whose ports reference the tribe).
- Audit with the non-probe gate (Key Technical Decisions): expect
  Adeline-class tribal floods to drop; watch RULE_HISTORY's known voltron
  regression commanders (Gorm, Zetalpa) for cliffs.

**Execution note:** Test-first — pin the Adeline reproduction (token
`w_1_1_human` → Human must NOT be admitted) before touching the gate.

**Patterns to follow:**
- Existing token Gate 5 tests in `tests/test_density_rules.py` (~:912–980).

**Test scenarios:**
- Happy path: commander with `TokenScript$ w_1_1_human` and literal subtype
  Human, no other Human reference → `tribal_density` does not emit Human
  complements.
- Happy path (raw_line route): commander whose only 'Human' evidence is
  TriggerDescription prose in `raw_line` (the Adeline shape) → Human not
  admitted; the reproduction test must exercise this route, not only
  Gate 1.
- Happy path: commander with a `valid_filter` mentioning Human (true
  Human-tribal) plus own-type tokens → Human still admitted.
- Edge case: non-skiplisted own-type token tribe (e.g. Squirrel via
  Chatterfang-style script) → still admitted (fix must not narrow beyond
  the skiplist tribes).
- Edge case: skiplisted tribe reachable via the vanilla fallback → fallback
  behavior unchanged (existing tests stay green).
- Integration: `bench.py audit` run — Adeline's top-30 tribal_density share
  drops materially from the 91.6% forensics baseline.

**Verification:**
- New tests green, existing vanilla-anchor and Gate-5 tests green; audit
  passes the non-probe gate; on SHIP, atomic re-pin of both fixtures +
  `docs/RULE_HISTORY.md` entry.

- [ ] **Unit 3: B1 — remove `rank_bonus` and the `edhrec_rank` sort tiebreak**

> **INVESTIGATE → DEFERRED until after Unit 6** (2026-07-02 two-step
> ablation, measured against the post-Unit-2 pin):
>
> - Step 1 (`rank_bonus` = 0 only): mean NDCG **−0.0395**, 26
>   per-commander cliffs < −0.05, gem 0.8160 → **0.8243** (+0.0083).
> - Step 2 (+ sort-tiebreak drop): identical numbers — the tiebreak's
>   marginal effect is ~0 even post-removal because `cmc` breaks ties
>   first. The entire cost is `rank_bonus`; the old R8 zero-credit
>   claim is confirmed valid for the tiebreak but was never evidence
>   about `rank_bonus`.
> - **Why deferred, not shipped**: forensics on the ablated code shows
>   the R9 justified-divergence pass-rate collapsing 0.8472 → 0.4654
>   (280 → 1,097 unjustified picks). Removing EDHREC ordering hands the
>   flat-weight exact-tie cohorts to the residual `(cmc, name)` sort —
>   effectively alphabetical — filling top-30s with arbitrary
>   single-rule density cards. `rank_bonus` is not only leakage; it is
>   currently MASKING the tie-density flaw Units 4–6 fix. Shipping now
>   degrades the goal-aligned axis (unjustified divergence) even though
>   gem rate rises.
> - **Re-run condition**: after Unit 6 lands/declines, repeat the
>   two-step ablation. Expected: calibration shrinks tie cohorts, the
>   NDCG cost shrinks toward the honest deflation, and unjustified
>   divergence stays flat. The honest EDHREC-free NDCG today is ~0.19
>   (vs 0.236 reported) — ~4.7 points of the headline metric are
>   EDHREC-sourced ordering, which recalibrates expectations for every
>   Phase 2 gate (their +/− deltas ride on a partially-inflated base).

**Goal:** EDHREC-clean inference path: no EDHREC-derived term in the score,
no EDHREC tiebreak in the sort key.

**Requirements:** R2, R6

**Dependencies:** Unit 2 landed or declined

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (drop `rank_bonus` from score assembly; keep the field or remove it from `UniversalScore` — prefer removal with explain-path sweep)
- Modify: `src/mtg_synergy_graph/engine.py` (drop `rank_lookup` from the sort key; deterministic residual order via `(cmc, name)`)
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (fused total folds `rank_bonus`; production-faithful sort key reconstruction)
- Modify: `src/mtg_synergy_graph/bench/forensics.py` (`production_sort_key` + the tiebreak self-check that raises on key divergence)
- Modify: `src/mtg_synergy_graph/bench/forensics_report.py` (production-key label)
- Test: `tests/test_universal_scorer_coverage.py`, `tests/bench/test_universal_scorer_identity.py`

**Approach:**
- Measure first, in TWO steps (doc-review, adversarial): (1) `rank_bonus`
  removal alone, (2) both removals. R8's +0.000000 tiebreak credit was
  measured *with `rank_bonus` present* — the bonus is a rank-monotone
  tie-splitter, so R8 does not license attributing the combined delta to
  `rank_bonus` alone. One extra audit run; record both deltas in the
  landing commit.
- Apply the non-probe gate. Positive evidence: gem-axis metrics should not
  degrade; unreleased/no-rank cards no longer carry a structural deficit.
- Sweep consumers: `to_legacy_buckets`, explain rendering, any test
  constructing `UniversalScore` with `rank_bonus` — AND the three bench
  mirrors above, in the same commit, or `--optimize` fidelity breaks and
  `--forensics` raises its tiebreak self-check against the re-pinned
  fixture.
- Decide the fate of `--forensics --ablate-tiebreak` (retire, or repoint
  at archived pins for historical measurement) as part of this unit.

**Test scenarios:**
- Happy path: two candidates identical except `edhrec_rank` → identical
  totals and stable name-ordered tie.
- Happy path: candidate with `edhrec_rank = NULL` (unreleased) → no score
  deficit vs an otherwise-identical ranked candidate.
- Error path: fixture rows missing the `edhrec_rank` column entirely →
  scorer does not raise.
- Integration: full audit run; `.audit/history.csv` row appended;
  aggregate delta within the non-probe gate.

**Verification:**
- Audit gate passes; on SHIP, atomic re-pin + CLAUDE.md note ("inference
  sort key is now `(-score, cmc, name)`") + `docs/RULE_HISTORY.md` entry.

### Phase 2 — Density-family calibration (OUTRANKED track)

- [x] **Unit 4: A4 — concave within-family aggregation (flag-gated probe)** *(DECLINED 2026-07-02: three variants measured — blanket 6 cliffs / flat-only 3 / flat-minus-tribal 1 (Rionya −0.0520) with agg ~0; R8a re-sweep held +0.00006 rules out calibration; structural finding: uniform haircuts cannot separate flood-as-noise from flood-as-archetype → mandate for Unit 5 tiering. Survives: dual-total choke point (flag OFF, inert), hash registration, 16 tests. See docs/solutions/best-practices/concave-family-agg-null-result-2026-07-02.md)*

**Goal:** Single-family monoculture candidates stop scaling linearly:
within-family contributions aggregate concavely (or the dampener reaches
single-rule candidates), compressing 87–100% single-family top-30s.

**Requirements:** R3, R6

**Dependencies:** Unit 3 landed or declined

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (concave aggregation at a choke point shared by `Score.score` AND `to_legacy_buckets()["total"]`; flag `_ENABLE_CONCAVE_FAMILY_AGG` default False)
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (fused-total mirror `_fast_total_and_contribs` must apply the same aggregation when the flag is on)
- Test: `tests/test_universal_scorer_concave_agg.py` (create)

**Approach:**
- **Dual-total reconciliation is a hard precondition** (doc-review P0,
  feasibility): production ranking sorts `to_legacy_buckets()["total"]`,
  which today has NO dampener; the dampened `Score.score` feeds only the
  audit-fixture path. Implement the concave aggregation in a single helper
  consumed by both totals (behind the flag), and update the optimize.py
  fused-total mirror in the same commit — otherwise forensics/production
  rankings cannot move and the unit's primary secondary evidence is
  physically unreachable. Add a flag-on test asserting the two totals
  apply the same aggregation.
- Flag-gated OFF, bitwise-inert; `ScoringConfigInputs` field added only at
  the flip step, per house pattern.
- Functional form chosen at implementation time from tensor family-share
  distribution (see Deferred). Constraint: must reach `len(syn_by_rule)==1`
  candidates — the current exemption is the flaw.
- Flip step: audit vs pin → bootstrap CI on aggregate → scratch-pin
  (`--repin --yes --fixture <scratch>`) → `--collinearity` + `--forensics`
  (displacer monoculture shares are the primary secondary) → probe gates
  per R6 (+0.010 SHIP or the goal-aligned alternative; cliff DECLINE;
  decontaminated-gem sidecar per R9a is the gem trigger), plus the R9
  cumulative-floor report.
- Conditional `--optimize` re-sweep (500-cmdr fixture) before any final
  DECLINE, to rule out stale multipliers as confound.
- Do not touch the gem plausibility gate in this unit (confound guard);
  the R9a sidecar is a read-only recomputation in the evidence package.

**Execution note:** Test-first with hand-computed aggregation pairs (the
color-IDF probe's Unit-2 pattern).

**Patterns to follow:**
- `tests/test_pathway_flag_gate.py` for flag-gate pinning;
  plan 2026-07-02-001 Units 2/4/5 for the flip evidence package.

**Test scenarios:**
- Happy path: flag OFF → bitwise-identical scores (`--expect-identity`).
- Happy path (flag ON, patched): candidate with one family × large
  contribution vs candidate with three families × equal total → the
  diversified candidate ranks higher.
- Edge case: single-rule candidate with tiny contribution → concavity does
  not amplify (monotone, bounded transform).
- Edge case: anti-synergy present → concavity applies to synergy side only,
  consistent with current dampener semantics.
- Integration: flag flip moves `compute_config_hash` and restores on
  unflip.

**Verification:**
- Flag-off identity PASS; flip evidence package complete; one of
  SHIP/DECLINE/INVESTIGATE declared with numbers; outcome paths as in the
  template (SHIP: re-pin + docs; DECLINE: null-result doc + revert to tag,
  keep tests).

- [x] **Unit 5: A2 — payoff-qualified tribal_density (flag-gated probe)** *(INVESTIGATE 2026-07-02, gem-dominant trigger fired: best variant (body 0.30, vanilla-anchor exemption) = 500-cmdr gem +0.0241, mean NDCG +0.0051, watch-list wins Marrow +0.254 / Chatterfang +0.078 / Lathril +0.024 / Edgar +0.019 — but 5 fuel-tribe cliffs (Nissa −0.107, Arasta −0.096, Rograkh −0.067, Camellia −0.060, Elenda −0.053). Sweep: 0.15→11 cliffs, 0.30→5 best, 0.40→gains lost. Fuel-tribe exemption rejected: gutted Marrow to 0.0. Resolution path: Unit 6 pool-scaled weights protect small fuel tribes — run the tier flag jointly in Unit 6's evidence package. Flag OFF in tree; tribal_body infra + weight entry + tests land now.)*

**Goal:** Tribal emission distinguishes payoff pieces (lord scopes, tribal
triggers/filters) from vanilla same-type bodies; vanilla bodies get a
reduced tier or require commander-side tribal payoff evidence.

**Requirements:** R3, R6, R8

**Dependencies:** Unit 4 landed or declined

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/density.py` (tribal emitter; flag `_ENABLE_TRIBAL_PAYOFF_TIER`)
- Modify: `src/mtg_synergy_graph/data/scoring_weights.json` (second-tier flat weight entry at flip time)
- Test: `tests/test_density_rules.py`

**Approach:**
- Two-tier emission: payoff tier (candidate has a port whose
  `valid_filter`/`affected_scope` references the tribe, or is a lord) keeps
  the current 0.5; body tier (mere type match) drops to a materially lower
  weight. SQL stays within the per-rule query budget (see Deferred).
- `rule_quality_gate.py --rule tribal_density` Gates A/B after the change
  (targets extend beyond the golden 100).
- Same flip evidence package and probe gates as Unit 4.

**Test scenarios:**
- Happy path: lord of the commander's tribe → payoff tier weight.
- Happy path: vanilla same-type creature → body tier weight (flag ON);
  unchanged 0.5 (flag OFF).
- Edge case: candidate that is a lord for a DIFFERENT tribe but shares the
  commander's type → body tier, not payoff.
- Edge case: changeling/all-subtypes candidates → deterministic tier
  assignment, no double emission.
- Integration: Krenko/Marrow-Gnawer forensics displacer share drops while
  their genuine payoffs (lords, tribal triggers) hold rank.

**Verification:**
- Flag-off identity PASS; Gates A/B PASS; probe gates evaluated; outcome
  paths per template.

- [x] **Unit 6: A1 + A6 — pool-size-aware density weights and spell/tribal dedup (flag-gated probe)** *(DECLINED 2026-07-02, three arms: pool alone 15 cliffs (Kess −0.233) / joint+tier 15 (gem +0.0297 best-ever but compounding cliffs) / pool(tribal_body)+tier 8 (Magda −0.149). ESCALATION RULE FIRED — ≥2 calibration DECLINEs: the OUTRANKED lever moves to the C1/lift-normalization design cycle; A6 dedup unmeasured, folded into C1. Full config table + structural finding: docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md. Infra survives flag-OFF incl. _POOL_SCALED_RULES scoping.)*

**Goal:** Flat density weights scale with the size of the pool they flood
(a 4,300-card tribe pays more than a 40-card tribe), and a candidate no
longer collects both `spell_density` (subtype path) and `tribal_density`
for the same tribe axis.

**Requirements:** R3, R6

**Dependencies:** Unit 5 landed or declined

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (`_compute_idf_basis` flat branch; flag `_ENABLE_POOL_SCALED_FLAT_WEIGHTS`)
- Modify: `src/mtg_synergy_graph/complement_rules/density.py` (spell_density subtype-path suppression when tribal_density emitted for the same candidate+tribe)
- Test: `tests/test_universal_scorer_coverage.py`, `tests/test_density_rules.py`

**Approach:**
- Pool scaling is keyed **per basis key, not per rule_id** (doc-review,
  adversarial + feasibility): `pool_N` = the emitted-candidate count for
  the specific `(rule_id, cmdr_event, cand_event, filter_group)` key —
  for tribal_density, `cand_event` is the tribe, so a commander with a
  40-card tribe and a 4,000-card tribe prices each tribe separately. This
  is what `_compute_idf_basis` already has in scope (`len(candidates)` per
  key); the wording "the rule's emitted-candidate count" would mislead
  toward a per-rule aggregate that breaks the unit's own happy-path test.
- The functional form (`min(flat_override, c / log2(1 + pool_N))` with a
  pool_N floor at ~30, the panharmonicon precedent) is directional, not
  pre-committed — like Unit 4, choose the schedule from tensor evidence at
  implementation time; the gates are what is pre-committed. Guard against
  the documented small-N saturation cliff in tests before flip.
- **Two flags, two measurements** (doc-review, feasibility): pool scaling
  (`_ENABLE_POOL_SCALED_FLAT_WEIGHTS`) and the spell/tribal dedup arm are
  mechanically unrelated with different failure modes — flag them
  separately and flip/measure sequentially (mirroring Unit 9's per-arm
  DECLINE structure) so a joint DECLINE is attributable. The density.py
  dedup suppression sits behind its own flag so flag-OFF stays
  bitwise-inert.
- This is a per-key weight schedule, not an IDF-family change; the outcome
  doc must cite both closed-axis nulls and explain the distinction.
- Dedup: emission-side suppression, not score-side subtraction (keeps the
  tensor interpretable).
- Same flip evidence package and probe gates (R6/R9/R9a); run
  `--collinearity` after.

**Test scenarios:**
- Happy path: two tribes, pool sizes 40 vs 4,000 → smaller pool retains a
  higher per-hit weight (flag ON).
- Edge case: pool_N below the floor → weight capped at the flat override,
  no saturation above it.
- Happy path (dedup): Edgar-style cast-tribal commander → a vanilla
  Vampire carries tribal_density only, not tribal + spell_density subtype.
- Edge case (dedup): candidate qualifying for spell_density via the
  instant/sorcery path (not subtype) → unaffected.
- Integration: flag flip moves config hash; forensics OUTRANKED
  tribal/spell family shares drop.

**Verification:**
- Flag-off identity PASS; probe gates evaluated; `--collinearity` healthy;
  outcome paths per template.

- [x] **Unit 7: A5 — expose flat overrides to the optimizer sweep** *(DONE 2026-07-02, tooling-only: flat keys honor `_RULE_QUALITY_MULTIPLIER` (multiplier-on-flat-value, exact with Unit 6 declined; bitwise passthrough with no entries — identity PASS); optimizer auto-adds flat rules at 1.0; smoke sweep clean on 500-cmdr; re-inflation warning in the reviewer-facing comment; 2112 tests green)*

**Goal:** The coordinate-ascent optimizer can propose changes to
`_FLAT_WEIGHT_OVERRIDES` values, ending the frozen-constant status of the
#1/#2 OUTRANKED contributors.

**Requirements:** R4

**Dependencies:** Units 4–6 resolved (sweep should see the surviving
aggregation semantics)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (sweep key set + proposal emission)
- Test: `tests/bench/test_optimize.py`

**Approach:**
- **Explicitly tooling-only groundwork** (doc-review, scope): this unit
  ships capability, not an outcome — it cannot fail an audit gate and
  makes no NDCG claim. Its acceptance bar is the fidelity invariant plus a
  clean exploratory sweep; any actual weight change it proposes goes
  through a human-reviewed `scoring_weights.json` edit with its own audit.
- Design-time only: no inference-path change, no audit gate. Proposals
  still land in `.audit/optimize_proposal.json` for human review; the
  committed `scoring_weights.json` is never auto-mutated.
- Preserve the cached-basis fidelity invariant
  (`tests/bench/test_optimize.py` ~:554–584), with strategy conditional on
  Unit 6's outcome (doc-review, feasibility): if Unit 6 DECLINEd,
  multiplier-on-flat-value is exact; if Unit 6 SHIPped, the flat weight is
  a `min(...)` expression, so either rebuild the basis per override
  candidate (the documented `IdfBasis` contract) or model
  `min(m·override, pool_term)` explicitly in the re-weighting shortcut AND
  extend the fidelity test with a binding-pool-term case.
- **Proposal-review warning** (doc-review, product-lens): the optimizer
  objective blends the critiqued NDCG proxy with the contaminated gem
  metric; proposals that raise flat-rule effective weights back toward
  pre-Unit-4/5/6 levels require forensics-secondary justification, not
  just objective improvement. Sequence the first real sweep after the R9a
  sidecar exists (it does, from Unit 4's evidence package).
- Respect `optimizer-fixture-size-2026-04-30.md`: 500-cmdr fixture only.

**Test scenarios:**
- Happy path: flat-rule key appears in the sweep grid and its proposal row
  round-trips through the JSON writer.
- Edge case: fidelity check — re-weighted flat key reproduces
  `score_all_universal` within the epsilon the existing invariant uses.
- Error path: proposal for a key absent from `scoring_weights.json` →
  rejected with a clear error, no silent write.

**Verification:**
- `--optimize --no-self-test --max-sweeps 1` runs clean on the 500-cmdr
  fixture with flat keys in the grid; fidelity tests green.

### Phase 3 — Coverage expansion (NO_RULES track)

- [x] **Unit 8: C3 — classify `trigger.Phase`, unwrap `effect.Effect`** *(HYGIENE ARM SHIPPED 2026-07-02, vocab v4: PHASE (2,305 rows) + INTERNAL (effect.Cleanup, 2,759 rows), UNKNOWN cards 15,338→13,777, embeddings rebuilt, identity PASS. Investigation findings reshape the rest: (a) Phase Execute payloads are already extracted as separate effect ports — event-map rows unneeded, the premise of "unmatchable upkeep engines" was wrong at the trigger level; (b) effect.Effect + granted-ability statics (Phenax's mill inside AddAbility) are one importer-level unwrapping family — deferred to the PPMI/importer batch with this evidence; (c) the remaining big UNKNOWN shapes (Destroy 1200, Dig 828, Charm 738) are C1 glue territory. No scoring-path change; no probe gates consumed.)*

**Goal:** The two largest UNKNOWN shapes become matchable: upkeep/end-step
trigger engines get a node_kind and event-map reach; Forge's generic
`effect.Effect` wrapper exposes its inner effect to the matcher.

**Requirements:** R5, R6

**Dependencies:** Phase 2 resolved

**Files:**
- Modify: `src/mtg_synergy_graph/port_graph/vocabulary.py` (PHASE node_kind; VOCAB_VERSION bump)
- Modify: `src/mtg_synergy_graph/port_graph/projection.py`
- Modify: `src/mtg_synergy_graph/ports.py` or importer extraction (effect.Effect unwrap — location per Deferred)
- Modify: `src/mtg_synergy_graph/data/event_match_seed.json` (rows for the phase-trigger shapes that survive `--unknowns` ranking)
- Test: `tests/test_ports_effect_unwrap.py` (create), projection/vocabulary tests

**Approach:**
- Start from `bench.py audit --unknowns` ranking on the refreshed DB;
  implement only the shapes with golden-set/500-fixture reach
  (`gap-report-impact-vs-golden-set-coverage` pre-check).
- Phase triggers match via their execute-effect payload (the trigger event
  alone carries no synergy semantics) — directional: the extraction should
  surface the inner effect class the same way other trigger ports do.
- **Re-baseline sequencing** (doc-review, coherence): (1) Phase 2 units
  land/decline against the pre-refresh fixtures; (2) re-import and rebuild
  embeddings on the surviving Phase 2 config; (3) re-pin both fixtures on
  the new port set — this pin is authoritative for Units 9–10; (4) Unit
  8's own audit gates are evaluated on a scratch pin built from the
  refreshed DB, compared against the pre-refresh committed pin, with the
  data-refresh component of the delta reported separately from the
  rule-coverage component. Phase 2 DECLINEs are final and are NOT
  re-measured after the refresh.
- Treat as an ordinary probe (doc-review, adversarial): enlarging match
  pools lowers existing rules' frequency-derived IDF weights, so existing
  candidates' scores can DECREASE and rankings shuffle both ways — full
  per-commander cliff analysis on the 500-cmdr fixture, plus a before/
  after check of IDF-weight shifts for the top-5 rules by contribution
  mass (from the tensor). The AlternateMode/Tergrid precedent is the
  cautionary case.
- Probe gates per R6/R9/R9a.

**Test scenarios:**
- Happy path: a card with an upkeep trigger producing Draw → projects to
  the new node_kind with the effect payload reachable by trigger_effect.
- Happy path: an `effect.Effect` wrapper with a recoverable inner Api →
  inner effect port emitted; wrapper without recoverable content → remains
  UNKNOWN (no fabrication).
- Edge case: VOCAB_VERSION bump propagates to the embedding config-hash
  discipline without breaking `--embedding-dedup`.
- Integration: `--unknowns` count for trigger.Phase / effect.Effect drops
  measurably; NO_RULES bucket share in `--forensics` decreases.

**Verification:**
- Re-import clean; audit gates evaluated; `--unknowns` delta recorded in
  the landing commit message; re-pin atomic.

- [ ] **Unit 9: C4 — zone-equivalence classes and tutor attrs**

**Goal:** Semantically-equivalent shapes match: recursion resonates across
zone-pair classes ({GY, Exile}→Battlefield, etc.) and tutors/
non-battlefield movers produce attrs. (Counter-class compatibility was cut
from this unit's scope — see Deferred to Separate Tasks; it appears in no
file, approach, or test below and needs its own evidence base.)

**Requirements:** R5, R6

**Dependencies:** Unit 8 (shares the re-pin cycle discipline)

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` (`_changezone_resonance_check` equivalence classes)
- Modify: `src/mtg_synergy_graph/graph_engine.py` (`_effect_produced_attrs` non-battlefield destinations)
- Modify: `src/mtg_synergy_graph/data/event_match_seed.json` (if class definitions live as seed data — preferred for hash discipline)
- Test: `tests/test_effect_resonance_zones.py` (extend or create)

**Approach:**
- Equivalence classes are data (seed JSON), not code branches, so future
  edits stay re-pin-disciplined and reviewable.
- **Hash-discipline registration is mandatory** (doc-review, feasibility):
  the config hash digests only the named sections of
  `event_match_seed.json` (`_seed_digest("event_match_seed.json",
  ("event_match_map", "cost_feeds_trigger"))` in
  `get_scoring_config_inputs`). A new zone-class section is invisible to
  the digest as written — register the new section name in the same commit
  that introduces it, plus a test asserting that editing a class row flips
  `compute_config_hash`.
- Tutor attrs: produce destination-zone-tagged attrs instead of dropping;
  consuming rules opt in (avoid suddenly matching every tutor to every ETB
  commander — that is a new false-positive engine if unguarded).
- Each relaxation is separately auditable: land zone classes, then tutor
  attrs, as distinct commits under one audit-gated unit; DECLINE
  independently if one arm cliffs.
- Existing scope-matching tests (Tergrid/Meren/Korvold suites) are the
  regression net — the relaxation must not reopen the player-scope holes
  those tests pin.

**Test scenarios:**
- Happy path: Meren (GY→BF) × Exile→BF recursion piece → resonates under
  the class; GY→Hand vs GY→BF → still distinct classes (retrieval ≠
  reanimation).
- Edge case: zone pair not in any class → exact-equality behavior
  preserved.
- Happy path: tutor (Library→Hand) feeding a commander with a cast/draw
  engine per the opt-in consumer → match; ETB-only commander → no match.
- Error path: malformed class row in seed JSON → interpreter/loader raises
  at init (existing drift-check pattern).
- Integration: audit + `--forensics`; NO_RULES ZONECHANGE share drops
  without an OUTRANKED spike.

**Verification:**
- Scope regression suites green; audit gates evaluated per arm; re-pin
  atomic with Unit 8's cycle or its own.

- [ ] **Unit 10: C2 — candidate-side static/anthem consumption**

**Goal:** Anthems, keyword granters, and protective statics score for
commanders whose strategy they serve (go-wide/token → anthem; voltron →
protection/buff), ending the 159-card static.Continuous NO_RULES block.

**Requirements:** R5, R6, R8

**Dependencies:** Unit 9

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/statics.py` (or extend `tokens.py` if cohesion favors it)
- Modify: `src/mtg_synergy_graph/complement_rules/registry.py`
- Modify: `src/mtg_synergy_graph/data/scoring_weights.json` (weight entries at flip)
- Test: `tests/test_static_payoff_rules.py` (create)

**Approach:**
- Gated the way axis-feeders are: the commander must provably supply the
  axis (token production / go-wide ports for anthems; voltron statics for
  protection), so this does not become a new flat-noise family — the
  ward_2_tribal lesson is the cautionary precedent.
- IDF-weighted (not flat): scope the candidate key by the static's
  affected-scope shape so specificity weighting has real granularity.
- `rule_quality_gate.py` Gates A/B mandatory (targets far outside golden
  100); `gap-report` reach pre-check before building.
- Probe gates at flip; flag-gate if emission volume is large.

**Test scenarios:**
- Happy path: token commander × global creature anthem → emits; voltron
  commander × +X/+X-your-creatures anthem → emits under voltron gate.
- Edge case: anthem scoped to a tribe the commander doesn't reference → no
  emission (scope must intersect commander evidence).
- Edge case: candidate static that buffs opponents' permanents or is a
  drawback static → no emission.
- Error path: static with NULL affected_scope → skipped, no crash (the
  etb_tapped_stax NULL-filter invariant precedent).
- Integration: Gates A/B PASS; forensics STATIC_BUFF NO_RULES share drops;
  no monoculture regression (Unit 4's concavity should already be live or
  declined-with-evidence).

**Verification:**
- Gates A/B PASS; audit gates evaluated; re-pin atomic;
  `docs/COMPLEMENT_RULES.md` + `docs/RULE_HISTORY.md` updated.

## System-Wide Impact

- **Interaction graph:** Units 2/5/6 change what `find_all_complements`
  emits → the tensor, forensics buckets, gem sets, and optimizer basis all
  shift; sequential landing (R7) keeps attribution clean.
- **Error propagation:** seed-JSON drift raises at interpreter init
  (existing behavior); Unit 9 adds class rows to that surface. Unit 8's
  importer change requires the re-import to precede any audit run or the
  DB/view drift raises.
- **State lifecycle risks:** re-pin timing — a hash-flipping unit whose
  fixtures are re-pinned before DECLINE poisons the baseline; the template's
  scratch-pin discipline is the guard. `.audit/history.csv` rows across
  these units span multiple (config, snapshot) boundaries — trend readers
  already mark boundaries.
- **API surface parity:** `UniversalScore` field removal (Unit 3) touches
  `to_legacy_buckets`, explain rendering, and any external consumer of the
  score dataclass; sweep writers and readers.
- **Integration coverage:** every scoring unit's final check is the audit
  run itself (live vs pin over 100/500 commanders) — unit tests alone
  cannot prove ranking-level effects.
- **Unchanged invariants:** DECLARATIVE_RULE_IDS routing, the legality
  filter, the pathway family, embeddings flag (OFF), gem-gate definition
  (B3 deferred), and both closed IDF axes stay untouched.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Purity removals (Unit 3) drop NDCG below the non-probe gate because the metric was inflated | Gate tolerates −0.010; forensics secondaries (gem axis, unjustified-divergence rate) are the positive evidence; INVESTIGATE path exists |
| Concavity (Unit 4) fixes monoculture but tanks tribal commanders whose EDHREC top-30 IS the tribe (Krenko) | R7 per-commander cliff gate on the 500-cmdr fixture catches it; functional form chosen from tensor evidence, not a priori |
| Payoff tier (Unit 5) reintroduces the vanilla-anchor regressions documented in RULE_HISTORY (Gorm, Zetalpa) | Those commanders named as explicit watch-list in the flip evidence package |
| Pool-size scaling (Unit 6) hits the small-N saturation cliff documented in the color-IDF null | Hard floor on pool_N; saturation check in tests before flip |
| Unit 8 re-import churn invalidates Phase 2 conclusions | Phase ordering: calibration lands before the data refresh; Unit 8 re-pins on the surviving config |
| Tutor-attr relaxation (Unit 9) becomes a new false-positive engine | Opt-in consumer design; separate commit arm; independent DECLINE |
| New statics family (Unit 10) repeats the ward_2_tribal flat-noise pathology | IDF-weighted (never flat), axis-gated, Gates A/B mandatory |
| Cumulative probe fatigue: several DECLINEs in a row | Each DECLINE ships a null-result doc + surviving tests/infra — the plan's definition of success is evidence, not green lights |

## Documentation / Operational Notes

- Each SHIP: `docs/RULE_HISTORY.md` entry + re-pin in the landing commit;
  CLAUDE.md updates for Unit 3 (sort key) and Unit 8 (vocab bump).
- Each DECLINE: null-result doc under `docs/solutions/best-practices/`
  citing the gate numbers (house convention).
- Pre-commit `bench-audit` hook fires advisorily on every one of these
  units — expected, not noise.
- Pre-commit pytest hook does not stash untracked test files — land
  implementation + tests in the same commit (color-IDF process gotcha).

## Sources & References

- **Origin:** this session's four-agent architecture review (scoring math,
  density rules, matching coverage, architecture/eval) +
  [docs/ideation/2026-06-10-synergy-accuracy-ideation.md](../ideation/2026-06-10-synergy-accuracy-ideation.md)
- Forensics baseline: `.audit/forensics.md` (2026-07-02, NDCG 0.232,
  OUTRANKED 46.2% / NO_RULES 41.4%)
- Template: [docs/plans/2026-07-02-001-feat-color-conditioned-idf-probe-plan.md](2026-07-02-001-feat-color-conditioned-idf-probe-plan.md)
- Gate provenance: [docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md](../solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md)
