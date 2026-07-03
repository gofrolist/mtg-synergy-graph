---
date: 2026-07-02
topic: resource-flow-demand
---

# Resource-Flow Demand — closing the commander-demand connection gap (NO_RULES bucket)

## Problem Frame

Divergence forensics attributes 43.0% of all EDHREC-label misses
(1,137 of 2,646, run 2026-07-02T17:52) to `NO_RULES`: the missed card
has well-formed, already-vocabularized port data, but no rule connects
it to the commander. This is the largest OPEN (addressable) miss
bucket — OUTRANKED is larger at 45.6% but closed as justified
divergence — and the last open ranking lever after all three
ranking-layer transform families
were declined on 2026-07-02 (plans 002/003/004) and the walker/
template route was exhausted the same day (three April blocks
confirmed on merit).

Two facts reframe what `NO_RULES` means:

1. **The missed cards are ordinary.** The frequency-ranked port
   shapes across all NO_RULES cards are the most common shapes in the
   game — `static.Continuous` (164 cards), `effect.ChangeZone` (164),
   ETB triggers (137), `cost.tap` (133), `effect.Draw` (129),
   `effect.Mana` (97). UNKNOWN shapes appear only at the tail
   (31 cards). Candidate-side vocabulary is NOT the gap.

2. **The misses concentrate in commanders whose scored universe has
   collapsed.** Yawgmoth, Thran Physician: 29/30 misses NO_RULES and
   only 190 candidates receive any rule activation (Meren, a
   comparable archetype: 19,727). Borborygmos Enraged: 28 misses,
   160 candidates. Araumi: 26, 257. Phenax: 19, 1,692. For Yawgmoth
   the live engine returns a 49-candidate scored universe with "No
   mechanical synergy detected" on 9 of the top 10.

The mechanism (verified against `card_ports` and the rule registry,
review pass 2026-07-02) is **under-served demand**, in three distinct
shapes across the cohort:

- **Genuinely unconsumed** (Yawgmoth): port extraction is complete —
  `cost.sacrifice`, `cost.discard`, `cost.pay_life`,
  `effect.PutCounter[M1M1]`, `effect.Draw`, `effect.Proliferate` all
  present — and no registered rule keys off the sacrifice-cost
  demand. `cost_feeds_trigger` points the other way (candidate's
  cost feeds the commander's trigger).
- **Consumed but starved** (Araumi, Borborygmos, Osgir): per-axis
  feeders DO consume these demand ports — `gy_fuel_feeder`'s
  docstring names Araumi; `cost_payoff`'s names Borborygmos — yet
  both commanders still post 26–28 NO_RULES misses on 160–257
  activated candidates. The consumer exists; its supply pool
  delivers almost nothing. Why (pool size, IDF burial, or wrong
  supply cards) is a Stage 0 question, and the answer decides
  whether the fix is a new mechanism or wider pools on existing
  feeders.
- **Demand invisible to cost ports** (Phenax): the tap-cost engine
  lives inside a granted-ability `raw_line`
  (`static.Continuous` AddAbility), not a commander `cost.*` port. A
  consumer-shape mechanism cannot see it without an extraction
  extension — separately scoped (see Scope Boundaries).

**Load-bearing falsifiable hypothesis**: a general commander-demand →
candidate-supply mechanism over resource flows will lift NDCG@30 and
label connectivity on the low-coverage commander cohort without
regressing the aggregate, because the cohort's demand signal is
under-served — either unconsumed, or consumed by narrow per-axis
feeders whose supply pools deliver almost nothing. The rival
hypothesis Stage 0 must rule out: much of the NO_RULES mass is
generic goodstuff (draw, ramp, utility) whose correct mechanical
score under this architecture IS zero — justified divergence, not a
recoverable lever. If Stage 0 shows the under-served demand mass is
small, the addressable share is below the funding bar (R2), or the
supply pools are so broad that matching is indiscriminate, the
premise is false and the cycle DECLINES before any scoring-path
change.

## Requirements

**Stage 0 — evidence before mechanism (C-as-gate)**

- R1. Build a demand-coverage measurement over the existing tables:
  for every commander in the 100-commander canonical fixture,
  enumerate commander ports and classify each THREE ways —
  unconsumed (no registered rule keys off it),
  consumed-with-material-yield, or consumed-but-starved. Both
  classifier boundaries are pinned HERE, blind to Stage 0's numbers,
  with the same discipline as R2's threshold:
  - **Consumption predicate**: `RULE_GATES` in
    `src/mtg_synergy_graph/complement_rules/registry.py` MINUS
    `CARD_LEVEL_RULES` (whose firing "tells us nothing about
    per-port coverage" per the registry's own docstring — the
    existing auditor subtracts them), UNIONED with the interpreter's
    gate predicates for `DECLARATIVE_RULE_IDS` (per-port interpreter
    attribution is a documented deferred item — if too costly for
    Stage 0, declarative rules are recorded as non-consuming and the
    resulting over-count of "unconsumed" is bounded and reported).
    Substrate: `attributable_rules_for_port()`.
  - **Starved predicate** (absolute, archetype-free, not revisable
    after data is seen): a demand port is consumed-but-starved when
    a rule's gate fires for the commander AND (the commander's total
    activated-candidate universe is < 1,000 — an order of magnitude
    below fixture norms — OR the consuming rule contributes zero
    cards to that commander's top-30).
  Report under-served demand mass per commander and per port shape,
  cross-referenced against that commander's NO_RULES miss count.
  Stage 0 additionally includes: (a) a yield diagnosis for the
  cohort's existing feeders — why `gy_fuel_feeder` (Araumi, Osgir)
  and `cost_payoff` (Borborygmos) deliver 160–257 activated
  candidates: pool size, IDF burial, or supply cards that are not
  the labeled misses; and (b) an addressable-share measurement
  against a PROVISIONAL pairing table (see R1b-pin below); and (c) a
  non-gating aggregate readout: the share of ALL 1,137 NO_RULES
  misses (not just the cohort's) reachable by the provisional
  pairings — this tests whether a general mechanism addresses the
  bucket that motivated the cycle or only its worst exemplars.
  Read-only; no scoring-path changes.
- R1b-pin. The demand→supply pairing table used for the
  addressable-share measurement is a provisional draft covering ONLY
  the evident five flows, written and committed BEFORE the share is
  computed and before any cohort miss list is consulted for pool
  design. Anti-gaming bounds, pinned now: each measurement supply
  pool must be ≤ 2,000 cards (vs ~32k total), and the readout must
  report a null-model comparison (same-size random supply pool per
  flow) — measured share must exceed the null-model share.
  A share achieved only by pools failing these bounds counts as the
  "indiscriminate matching" DECLINE branch. Stage 0's readout may
  justify ADDING flows for the mechanism build, but the funding
  computation uses the pre-pinned draft only.
- R2. Stage 0 produces a mechanically checkable ROUTING decision.
  "Addressable share" = (cohort NO_RULES misses reachable by a
  provisional pairing whose commander-side demand port is classified
  unconsumed or consumed-but-starved per R1) / (total cohort
  NO_RULES misses). Demand mass whose starvation is diagnosed as IDF
  burial is EXCLUDED from the numerator — a new IDF-weighted rule
  over the same pools inherits the same burial, and re-weighting is
  a dead lever per Scope Boundaries. The cohort denominator is
  defined by rule, not by name: all fixture commanders whose
  activated-candidate count is below the same < 1,000 floor used in
  the starved predicate (the seven named commanders are the
  qualitative verification set, not the definition). Routing,
  pinned now — funding requires BOTH the share bar AND an absolute
  floor (≥ 100 NO_RULES labels reachable fixture-wide per the R1(c)
  aggregate readout), so the build cost is anchored against total
  recoverable mass, not just cohort proportion:
  - share < 0.25 OR fixture-wide reach < 100 labels → **DECLINE**,
    record the null result.
  - both bars met AND (unconsumed mass dominant OR starved-diagnosis
    = wrong-supply-cards) → **fund the mechanism build** (R3–R6a).
  - both bars met AND starved mass dominant AND diagnosis =
    pool-width on existing feeders → **route to a feeder-widening
    cycle instead** (data edits to shipped gates, same kill-test
    discipline) — the cheaper fix the diagnosis itself identifies;
    do not build the new mechanism on evidence that doesn't demand
    it.
  Honesty note on the precedent: the portfolio cycle's
  addressable-share readout passed its bar (0.630) and the cycle was
  still DECLINED at the sweep — share is a NECESSARY funding
  condition (cheap kill when absent), not a predictor of kill-test
  survival.

**The mechanism — one general resource-flow family (B)**

- R3. Resource flows are declared as a committed data artifact, not
  Python code: a seed file declaring, per resource (the evident five:
  sacrifice fodder, discard fuel, untap capacity, life, graveyard
  bodies; others only as Stage 0 justifies), which port shapes
  CONSUME it (commander-side demand) and
  which port shapes SUPPLY it (candidate-side). Follows the existing
  seed-artifact discipline (committed JSON, strict loader,
  `ValueError` on drift, hash-flipping functional edits).
- R4. ONE general interpreter mechanism consumes the seed: any
  commander port matching a declared consumer shape generates demand;
  any candidate port matching the paired supplier shape satisfies it.
  No per-archetype or per-commander logic
  (`memory/feedback_no_individual_rules.md`); adding a resource is a
  data edit, not a new rule.
- R5. Matches are IDF-weighted like every other rule (specificity
  discounts broad supply pools) and flag-gated default-OFF behind the
  established `_ENABLE_*` + `ScoringConfigInputs` pattern until the
  kill-test passes.
- R6. Prior-art cautions are design inputs, not afterthoughts:
  cost-side rules have reverted before when supply pools were broad
  (`counter_removal_payoff` HARMFUL ×4; broad `gy_retrieval` reverted
  2026-04-18; `Other`-qualifier axis rules HARMFUL). The seed grammar
  must support per-flow supply-pool narrowing (e.g. sacrifice fodder
  = recursion/token/undying shapes, not "any creature"), and the
  kill-test must include the flood/trap commanders from the portfolio
  cycle's trap set.
- R6a. Overlap governance with the existing feeder family: every
  resource R3 declares already has at least one shipped per-resource
  consumer (sacrifice → sacrifice_cluster/edict_feeder; discard →
  cost_payoff; untap → tap_type_feeder; life → lifegain_feeder/
  life_total_feeder; graveyard bodies → gy_fuel_feeder/
  graveyard_play). The plan must state, per declared resource,
  whether the general mechanism SUPERSEDES the existing feeder,
  COEXISTS with an explicit dedup key, or is SCOPED AWAY from that
  flow — and the kill-test must include a rule-overlap sidecar
  (`bench.py audit --collinearity` and `--embedding-dedup`) between
  the new mechanism and the feeder family, so a pass/fail cannot be
  a double-counting artifact. Note the repo invariant: a rule_id
  lives in exactly one of the Python-helper vs declarative paths.

**Kill-test and acceptance (probe-cycle discipline)**

- R7. The cycle runs kill-test-first with the canonical 5-outcome
  routing (SHIP / INVESTIGATE / INVESTIGATE-FOR-RETUNE /
  INCONCLUSIVE / DECLINE, per
  `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`),
  gates pinned before the flag flips, as a 3-part gate matched to the
  hypothesis (which promises cohort lift WITHOUT aggregate
  regression, not aggregate improvement):
  1. **Aggregate non-regression**: no NDCG@30 drop beyond the noise
     band on the 500-commander fixture, and NO cliff on trap
     commanders.
  2. **Cohort lift with named-miss recovery** (measured on the
     100-commander canonical fixture): NDCG improvement on the gated
     cohort — the six mechanism-reachable commanders (Yawgmoth,
     Borborygmos, Araumi, Slimefoot, Osgir, The Gitrog Monster),
     reported per-commander so a 6-way lift is not averaged into an
     INCONCLUSIVE. Phenax is the NEGATIVE CONTROL, reported but not
     gated: his demand is scoped out, so his numbers should NOT
     move — movement indicates the mechanism fires on something
     other than the declared demand shapes. Named-miss recovery is
     checked against pinned card lists, never seed-pool membership
     (the seed defining its own success predicate would be
     circular): the plan enumerates, per gated commander, an
     explicit ≥5-card list drawn from the current forensics
     NO_RULES miss output embodying that commander's mechanism
     story (land recursion for Borborygmos, sacrifice fodder for
     Yawgmoth, graveyard bodies for Araumi, …), committed before the
     flag flips; the gate passes only if ≥3 of the named cards per
     commander enter the top-30. Cohort lift alone is weakly
     falsifying: any rule that fires lifts a collapsed 49-candidate
     universe.
  3. **Gem gate with a displacement stance**: hidden_gem_hit_rate
     non-regressed aggregate-wide; on the cohort specifically,
     recovered EDHREC labels displacing implausible filler from
     near-empty top-30s is a win, not a regression — the cohort gem
     rate is reported but does not gate.
- R8. Forensics before/after: the NO_RULES bucket share must drop for
  the cohort; misses may move to NEAR_MISS/OUTRANKED (that is
  progress), but a shrinking NO_RULES share with flat NDCG (change
  within the noise band) is INVESTIGATE, not SHIP.
- R9. Every scoring-path change is audit-gated per
  `memory/feedback_audit_every_change.md`; re-pin only on SHIP.

## Success Criteria

- Stage 0 readout exists and the funding decision it produced is
  recorded (either direction — a DECLINE with evidence is a valid
  outcome).
- If funded and shipped: the R7 3-part gate passes (aggregate
  non-regression, cohort lift with named-miss recovery, gems
  non-regressed aggregate-wide), NO_RULES share reduction for the
  cohort, zero trap-commander cliffs.
- Mechanics-native secondary signals (measurable without EDHREC,
  per the stated product goal of working for any commander without
  playtesting data), with routing magnitudes pinned now — the same
  no-threshold-fitting discipline as R2: scored-universe growth
  ≥ 5× for collapsed-universe gated-cohort members (Yawgmoth 49 →
  ≥ 245) AND cohort top-30 `--explain` coverage ≥ 0.8. Meeting BOTH
  with flat EDHREC-relative metrics routes to INVESTIGATE, not
  DECLINE; anything less is reported but does not affect routing.
- Durable either way: the demand-coverage instrument (R1) remains a
  standing forensics companion, and the resource-flow seed grammar
  (if built) makes future resource additions data edits.

## Scope Boundaries

- NO ranking-layer transforms — weights, baselines, and list-assembly
  selection are measured dead
  (`memory/project_flood_as_archetype_irreducible.md`).
- NO vocabulary/DATA_GAP expansion in this cycle — it addresses only
  4.2% of miss mass; revisit separately if Stage 0 implicates
  UNKNOWN shapes for specific cohort commanders.
- NO per-archetype, per-commander, or per-card rules.
- Granted-ability demand extraction (Phenax's tap engine lives in an
  AddAbility `raw_line`, not a commander `cost.*` port) is OUT of
  this cycle's mechanism scope — recorded as a separate
  extraction-gap candidate. Phenax stays in the measurement cohort;
  the named-miss recovery gate keeps his numbers honest.
- NO EDHREC signal at inference (design-time evaluation only).
- OUTRANKED misses (45.6%) stay out of scope — closed as justified
  divergence or future candidate-evidence work.

## Key Decisions

- **Target = commander-demand gap, not candidate vocabulary**: the
  original vocabulary-expansion framing was invalidated by evidence —
  NO_RULES cards carry ordinary, known port shapes; the failure is
  connection, not classification. (User-selected 2026-07-02.)
- **Approach = one general data-driven mechanism (B) with an
  evidence Stage 0 (C) as its funding gate**: maximally general per
  standing feedback, one mechanism to audit instead of N rules, and
  the cycle can die cheaply at Stage 0 if the premise is wrong.
  Fallback shape if the general map proves too coarse: per-resource
  rule family (A) for only the flows Stage 0 shows are load-bearing.
  (User-selected 2026-07-02.)
- **Kill-test-first is the default cycle shape**: this repo's last
  four probe cycles all benefited from pre-pinned gates and cheap
  exits; this cycle inherits that discipline including the trap set.
- **Stage 0 is itself the first implementation unit**, with its own
  timebox: its three measurements share one substrate (persisted
  tensor + forensics miss lists + RULE_GATES), and
  `scripts/portfolio_sim.py` is the working precedent for an
  instrument of exactly this size. A DECLINE at Stage 0 still ships
  the demand-coverage instrument as a standing forensics companion —
  the cycle's floor outcome is a durable instrument, not nothing.
- **Weighed alternative — BM25 revisit**: the declined BM25-IDF probe
  measured +0.0407 hidden_gem_hit_rate on the 500-commander fixture
  and is documented as a possible SHIP under gems-first success
  criteria. This cycle was chosen over it because it targets a
  coverage hole BM25 cannot touch (commanders whose scored universe
  has collapsed see no benefit from re-weighting scores they never
  receive). BM25 remains the natural next gems-axis cycle.

## Dependencies / Assumptions

- Port extraction adequacy per cohort member (review pass
  2026-07-02): Yawgmoth VERIFIED complete and unconsumed; Araumi and
  Borborygmos VERIFIED consumed (gy_fuel_feeder / cost_payoff) but
  starved; Phenax VERIFIED unreachable via cost ports (granted
  ability). The remaining cohort members (Slimefoot, Osgir, Gitrog)
  are classified wholesale by Stage 0 (R1).
- Forensics instrument (`--forensics`) and the 500-commander fixture
  are the measurement substrate; both exist and ran on 2026-07-02.
- Fixture hosting (verified 2026-07-02): only Yawgmoth and The Gitrog
  Monster of the seven named cohort commanders appear in
  `tests/fixtures/golden_set_run_500.json`; all seven appear in the
  100-commander canonical fixture, which is also where `--forensics`
  runs. Therefore the cohort NDCG gate and the R8 before/after
  forensics comparison run on the 100-commander canonical fixture;
  the 500-commander fixture hosts the aggregate noise-band gate.
- The noise bands from `scripts/portfolio_sim.py bands` (NDCG ±0.0136
  on the 500 fixture) are reusable as the aggregate gate reference;
  the plan may re-derive them if the fixture changes.

## Outstanding Questions

### Resolve Before Planning

(none — both product decisions were resolved in-session)

### Deferred to Planning

- [Affects R3][Needs research] The initial resource taxonomy: which
  flows beyond the evident five (sacrifice fodder, discard fuel,
  untap capacity, life, graveyard bodies) does the Stage 0 readout
  justify, and what exact port shapes define each supply pool?
- [Affects R4][Technical] Whether the interpreter mechanism lives on
  the declarative `rules` table path or as one Python primitive
  consuming the seed — planning decides after reading
  `port_graph/interpreter.py` capabilities.
- [Affects R7][Technical] Band derivation and trap-list confirmation
  for the kill-test gates (the funding threshold itself is already
  pinned at ≥0.25 addressable share in R2; the noise-band reuse
  assumption from portfolio_sim should be re-argued or re-derived
  since a new rule family changes the score distribution itself).
- [Affects R6][Technical] How per-flow supply-pool narrowing is
  expressed in the seed grammar without reintroducing per-archetype
  logic.

## Next Steps

-> /ce-plan for structured implementation planning
