---
title: "feat: Resource-flow demand probe — Stage 0 instrument + conditional mechanism"
type: feat
status: declined
date: 2026-07-02
origin: docs/brainstorms/2026-07-02-resource-flow-demand-requirements.md
---

# feat: Resource-flow demand probe — Stage 0 instrument + conditional mechanism

## DECISION — DECLINED at Stage 0 (2026-07-03, Unit 3 routing)

Both pinned funding bars failed on the full 100-commander run
(config_hash `34a9d110…`, run 2026-07-03T01:49Z, wall clock 120s):

- **Cohort addressable share 0.0828** (36 of 435 cohort NO_RULES
  misses reachable via healthy under-served demand pairings;
  1 burial-excluded) — bar was ≥ 0.25. Cohort by rule (<1,000
  activated candidates) = 26 commanders; of the seven named
  verification commanders, Phenax/Slimefoot/Osgir fall OUTSIDE the
  rule-defined cohort.
- **Fixture-wide reach 47 labels** (of 1,137 NO_RULES misses; null
  model 15) — floor was ≥ 100.

Routing per the pinned R2 table: share < 0.25 OR reach < 100 →
**DECLINE**. The feeder-widening reroute branch is not reachable
(both bars must be met first), and its premise also fails on the
evidence: the yield diagnosis shows the shipped feeders already
DELIVER (Araumi's `gy_fuel_feeder` puts 29 of its 107 candidates in
her top-30) — the misses simply are not supply-shaped.

**Adversarial sensitivity check (post-decision, ce-code-review
20260702-210134)**: review found the seed's self-recursion supplier
shape too strict (`valid_filter='Self'` misses the 191-card
empty-filter bucket incl. Reassembling Skeleton/Bloodghast;
Gravecrawler's MayPlay recursion has NO extracted port at all — an
extraction gap). Re-measured with a maximally generous diagnostic
flow (671-card filter-agnostic GY→BF pool via `--flows-seed`, pinned
seed untouched): share 0.0943, reach 57 — both bars still fail by
large margins. The DECLINE is robust to the defect; the correction's
upper bound covers ~1/10 of the gap to either bar.

**Interpretation**: the measured share exceeds the null model 4.5×
(0.0828 vs 0.0184), so the five flows carry real mechanical signal —
but the recoverable mass is an order of magnitude below the funding
bars. The correct reading is narrow: the **cost→supply resource-flow
frame is a weak lever** for this bucket. (An earlier draft here
over-read it as "most NO_RULES mass is generic goodstuff scored
correctly at zero" — that was RETRACTED 2026-07-03 after reading the
actual unreachable cards, which are on-theme archetype synergies the
engine scores zero: Slimefoot→all saprolings, Yawgmoth→undying/
aristocrat/-1-1, Araumi→reanimate, Gitrog/Borborygmos→lands. See
`docs/solutions/best-practices/resource-flow-demand-null-result-2026-07-02.md`
Correction. The bucket is an OPEN archetype-payoff-detection gap, not
a ceiling.) Honest caveat recorded: the `wrong_supply_cards` feeder
diagnosis is partially tautological (NO_RULES misses have zero
tensor rows by definition), so the load-bearing numbers are the
reachability counts, not the diagnosis labels.

Units 4–8 (Phase B) never ran; zero scoring-path changes; pins
untouched. Shipped floor outcome: the demand-coverage instrument
(`scripts/demand_coverage.py`) as a standing forensics companion,
plus the provisional pairing table. Null-result doc:
`docs/solutions/best-practices/resource-flow-demand-null-result-2026-07-02.md`.

## Overview

Kill-test-first probe cycle targeting the NO_RULES forensics bucket
(43.0% of EDHREC-label misses; the largest OPEN bucket). Phase A
builds a read-only demand-coverage instrument and a pre-pinned
provisional pairing table, and produces a mechanically checkable
funding/routing decision. Phase B (conditional on that decision)
builds ONE general commander-demand → candidate-supply rule family as
declarative rows over a committed seed, flag-gated default-OFF, and
adjudicates it with pre-pinned gates. A DECLINE at Stage 0 still
ships the instrument as a standing forensics companion.

## Problem Frame

See origin document for full evidence. Summary: NO_RULES misses have
ordinary, already-vocabularized ports and concentrate in commanders
whose scored universe has collapsed (Yawgmoth 190 activated
candidates vs Meren 19,727). Demand is **under-served** in three
verified shapes: genuinely unconsumed (Yawgmoth's sacrifice cost),
consumed-but-starved (Araumi/`gy_fuel_feeder`,
Borborygmos/`cost_payoff` — consumers exist, supply pools deliver
almost nothing), and invisible to cost ports (Phenax's granted
ability — scoped out). The rival hypothesis (unaddressable generic
goodstuff) must be ruled out by measurement before any mechanism is
built.

## Requirements Trace

(origin IDs preserved)

- R1/R1b-pin/R2 — Stage 0 instrument, provisional pairing table,
  pinned funding routing → Units 1–3.
- R3–R5 — seed artifact, one general mechanism, IDF-weighted,
  flag-gated → Units 4–5.
- R6/R6a — supply-pool narrowing grammar; overlap governance with
  existing feeders → Units 4–6.
- R7–R9 — 3-part kill-test gate, forensics before/after, audit
  discipline → Unit 7.
- Success criteria (mechanics-native secondary signals, ≥5×/≥0.8
  pins) → Unit 7 reporting.

## Scope Boundaries

Carried from origin: no ranking-layer transforms; no
vocabulary/DATA_GAP expansion; no per-archetype/per-commander rules;
no EDHREC at inference; OUTRANKED out of scope (empirical
addressable share 0.0518 — 24 of 463 OUTRANKED misses — measured by
the portfolio cycle, see
`docs/solutions/best-practices/portfolio-selection-null-result-2026-07-02.md`);
granted-ability demand extraction (Phenax) out of scope — Phenax is
the kill-test negative control.

### Deferred to Separate Tasks

- Granted-ability demand extraction (AddAbility raw_line parsing):
  future extraction cycle if this one SHIPs.
- BM25 gems-first revisit: documented competing lever, next
  gems-axis cycle (origin Key Decisions).

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/port_graph/interpreter.py` — declarative
  path is already two-sided: `gate_predicate` (commander ports,
  compiled to Python callables) + `candidate_port_predicate`
  (compiled SQL over `card_ports`). `commander_port_predicate`
  column exists but is unconsumed (reserved; this family would be
  its first consumer if per-demand-port emission is needed).
  `direction` is hardcoded `"synergy"` on this path.
- `src/mtg_synergy_graph/port_graph/vocabulary.py` — `GATE_OPS`
  (VOCAB_VERSION "4") lacks `cost_target`/`cost_subtype` leaf ops;
  the shipped Python demand gates for the five flows read exactly
  those columns (`_gy_fuel_feeder_gate`, `_cost_payoff_gate`,
  `_land_bounce_feeder_gate` in `complement_rules/registry.py`).
- `scripts/gap_report.py::_scan_universe` — the working precedent
  for the R1 consumption predicate: walks `RULE_GATES` per port AND
  reuses `RuleInterpreter._compiled[i].gate` closures (pure Python,
  DB-free) for declarative rules. Stage 0 copies this union and adds
  the `CARD_LEVEL_RULES` subtraction (new logic — Stage 0 is the
  first real consumer of `attributable_rules_for_port()` /
  `CARD_LEVEL_RULES`, which are currently dead API).
- `src/mtg_synergy_graph/bench/forensics.py::compute_forensics` —
  per-miss NO_RULES lists are ONLY available in-process
  (`entry.misses`, `bucket == BUCKET_NO_RULES`); the persisted JSON
  does not serialize them. `load_tensor_candidates(conn, commander,
  config_hash)` implements the activated-candidate floor with one
  SQL query, no live scoring.
- `src/mtg_synergy_graph/bench/portfolio_sim.py` — instrument
  template: subcommands, `_add_common_args`, exit codes 0/1/2/3
  (3 = self-check, do not trust reports), reports dir under
  `.audit/`, never appends `history.csv`, fixture loading with named
  errors, trap-list resolution.
- Seed discipline: `src/mtg_synergy_graph/data/*.json` +
  `port_graph/_paths.py::default_seed_path`, strict loaders
  (ValueError, frozenset-validated fields, `comment`/`_readme`
  hash-excluded), `tests/test_seed_files_tracked.py`
  `REQUIRED_TRACKED_FILES`, `_seed_digest(filename,
  functional_keys)` for `ScoringConfigInputs` participation.
- Flag pattern: `_ENABLE_EMBEDDING_CONTRIBUTION` /
  `_ENABLE_PATHWAY_RULES` / `_ENABLE_CONCAVE_FAMILY_AGG` — module
  flag + `ScoringConfigInputs` field + labeled `compute_config_hash`
  block + `tests/test_pathway_flag_gate.py`-style test.

### Institutional Learnings (constraints, most binding first)

- `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md` —
  a general demand→supply rule is the broadest rule shape ever
  proposed here (Ward:2 vacuum-fill risk profile at scale);
  `rule_quality_gate.py` PASS (Gate A coverage ≥3, Gate B top-30 CV
  ≥0.05) is a REQUIRED pre-flip gate; `weight_hint` is dead —
  weights go in `scoring_weights.json`.
- `docs/solutions/best-practices/lift-normalization-kill-test-null-result-2026-07-02.md`
  — demand must be kit/port-derived, never score-statistic-derived;
  run the offline simulation before any integration.
- `docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md`
  — tribe-as-fuel commanders (Magda, Nissa, Camellia, Elenda) are
  the named falsification population: vanilla fuel bodies carry no
  candidate-side supply evidence. Lift the trap list verbatim.
- `docs/solutions/best-practices/portfolio-selection-null-result-2026-07-02.md`
  — mechanism must be neither a classifier nor uniform decay;
  instrument architecture template; addressable-share-before-build
  discipline.
- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  — the 12-step checklist IF the Python-primitive fallback is taken:
  identity-clean landing, `ScoringConfigInputs` untouched until the
  flip decision, explain plumbing through `page()` AND `score_one()`.
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
  — quantitative flip bar pinned BEFORE the sweep; instrument stands
  alone on DECLINE.
- `docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md`
  — gate provenance (DECLINE on any 500-fixture cliff < −0.05);
  pre-commit stash gotcha: land implementation + tests in the same
  commit (hook stashes unstaged-but-tracked, not untracked tests).
- `docs/solutions/best-practices/rule-consolidation-null-result-2026-04-24.md`
  — interpretation matrix for `--collinearity` vs
  `--embedding-dedup`; a demand rule will inherently overlap density
  rules — use the matrix, don't eyeball.
- `docs/solutions/best-practices/extract-python-dict-to-json-sidecar-2026-04-25.md`
  + `sweep-writers-not-just-readers-on-source-of-truth-refactor-2026-04-25.md`
  — seed authoring: hash from loaded file, dead-key test, never
  conflate extraction with re-pin in one commit; grep `scripts/` for
  string-match writers before renaming structures.
- `docs/solutions/test-failures/walker-validator-config-hash-pins-2026-07-02.md`
  — any new config-hash pin test must be added to
  `scripts/_validate_rule.py`'s stage-1 exclusion list.
- `docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md`
  — Stage 0 tests must not assume `.audit/*` artifacts exist on a
  fresh checkout.
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`
  — R8a optimizer confound check runs on the 500-cmdr fixture only.

## Key Technical Decisions

- **Mechanism path: declarative rows + minimal GATE_OPS extension,
  Python primitive as fallback.** The interpreter is already
  two-sided; the gap is two missing leaf ops (`cost_target_eq`,
  `cost_subtype_part` — names indicative). Extending `GATE_OPS` is
  precedented (vocabulary.py docstring: bump `VOCAB_VERSION`, update
  both compilers, `--expect-identity`, re-pin) and keeps R4's
  "adding a resource is a data edit" literally true. IF
  implementation finds the supply-side narrowing (R6) inexpressible
  in the extended vocabulary (LIKE-based `filter_tag`/`tribe` is the
  current ceiling), fall back to ONE flag-gated Python primitive per
  the doc-16 checklist — the seed file and gates survive the switch
  unchanged. (see origin: Deferred to Planning, resolved here)
- **Stage 0's classification predicates are tensor+gates only — no
  NEW scoring path is built.** The consumption predicate reuses
  `gap_report._scan_universe`'s union (RULE_GATES ∪ interpreter
  compiled gates) minus `CARD_LEVEL_RULES`; the activated-candidate
  floor comes from `load_tensor_candidates`. The NO_RULES miss lists
  and the live top-30 (needed by the starved predicate's zero-top-30
  branch) come from ONE in-process `compute_forensics()` pass —
  which does run production ranking, so the full-fixture instrument
  run is minutes, not sub-second. The origin's fallback clause about
  treating declarative rules as non-consuming is DROPPED — per-port
  declarative attribution is free (research finding).
- **Provisional pairing table is NOT wired into scoring config in
  Phase A.** It is a committed artifact consumed only by the Stage 0
  instrument, so committing it must not flip `compute_config_hash`
  (identity-clean; doc-16 discipline). The `ScoringConfigInputs`
  digest field, hash block, and re-pin happen only in Phase B at
  flip time.
- **Authoring blindness is procedural and auditable**: the pairing
  table is authored from port-shape knowledge (registry gates, the
  five flows' definitions) in Unit 1, committed BEFORE Unit 2's
  share computation ever runs, and the commit boundary is the
  evidence of ordering (R1b-pin).
- **Gate numbers**: origin's R7 3-part gate + named-miss ≥3-of-5
  lists + funding bar (share ≥ 0.25 AND ≥ 100 labels fixture-wide) —
  all pinned in the origin, not re-derived here. Added from gate
  provenance: DECLINE on any 500-fixture cliff < −0.05;
  `rule_quality_gate.py` PASS required pre-flip. The 500-fixture
  noise band is re-derived at Unit 7 (a new rule family changes the
  score distribution; band reuse was flagged in origin review).

## Open Questions

### Resolved During Planning

- Interpreter two-sided capability: YES structurally; blocked only
  by two missing leaf ops → GATE_OPS extension chosen (above).
- Per-port declarative attribution cost: free (compiled gate
  closures); fallback clause dropped.
- Per-miss NO_RULES lists: in-process `compute_forensics()` only —
  the JSON artifact does not carry them.
- R6a sequencing vs audit baseline: Phase B lands flag-OFF and
  identity-clean; any SUPERSEDE decision on an existing feeder is
  executed as part of the same flip commit + single re-pin, so the
  kill-test adjudicates the combined change, never a half-state.

### Deferred to Implementation

- Exact leaf-op names/semantics for the GATE_OPS extension — decided
  against the real gate predicates during Unit 4.
- Whether `commander_port_predicate` (dormant column) is needed for
  per-demand-port emission/`--explain` — decide when wiring
  explainability in Unit 5.
- ~~Starved-yield diagnosis thresholds~~ — RESOLVED during plan
  review (the deferral reopened the threshold-fitting hole): the
  IDF-burial criterion is PINNED HERE, blind, not revisable after
  data is seen. Starvation is classified **IDF-burial** when the
  consuming rule's supply cards ARE present in the commander's
  tensor rows (the rule fires and delivers candidates) but ZERO of
  them reach the commander's top-30 AND the flow's supply pool size
  is ≥ 500 (large pool → low IDF is the burial mechanism).
  **Wrong-supply-cards** = the rule fires but its delivered
  candidates do not intersect the commander's NO_RULES miss list.
  **Pool-size** = everything else (rule fires, pool < 500, few
  candidates delivered). Unit 2 implements these predicates
  verbatim.

## Implementation Units

### Phase A — Stage 0 (unconditional)

- [x] **Unit 1: Provisional pairing table (pre-pinned, blind)**

**Goal:** Commit the five-flow demand→supply pairing table BEFORE any
share computation, satisfying R1b-pin's authoring-blindness and
anti-gaming bounds.

**Requirements:** R1b-pin, R3 (grammar preview).

**Dependencies:** None.

**Files:**
- Create: `src/mtg_synergy_graph/data/resource_flows_seed.json`
- Create: `src/mtg_synergy_graph/port_graph/resource_flows.py`
  (loader only — strict validation, no scoring wiring)
- Modify: `tests/test_seed_files_tracked.py` (add to
  `REQUIRED_TRACKED_FILES`)
- Test: `tests/test_resource_flows_seed.py`

**Approach:**
- Five flows (sacrifice fodder, discard fuel, untap capacity, life,
  graveyard bodies), each declaring consumer port shapes
  (commander-side) and supplier port shapes (candidate-side) plus a
  `comment` field. `_readme` documents provisional status and the
  ≤2,000-card pool bound.
- Loader mirrors `load_family_map` strictness: frozenset-validated
  keys, ValueError with pointer, `comment`/`_readme` excluded from
  any future digest.
- NO `ScoringConfigInputs` participation in this unit
  (identity-clean; verified by `--expect-identity` in Unit 2's
  verification).
- Authoring source: registry gate predicates and port-shape
  vocabulary ONLY — no forensics miss list is consulted (state this
  in the `_readme`).

**Patterns to follow:** `src/mtg_synergy_graph/data/family_map.json`
+ `portfolio.py::load_family_map`;
`extract-python-dict-to-json-sidecar-2026-04-25.md`.

**Test scenarios:**
- Happy path: loader returns five flows with consumer/supplier shape
  lists; every declared shape references valid `port_type` values.
- Error path: unknown top-level key → ValueError naming the key;
  missing `consumers` or `suppliers` on a flow → ValueError with
  dotted path; empty supplier list → ValueError.
- Edge case: `_readme` and `comment` fields ignored by the loader's
  functional view.
- Integration: file is git-tracked
  (`tests/test_seed_files_tracked.py` extension); `git ls-files`
  discipline per gitignore-negation learning.

**Verification:** Loader tests green; `bench.py audit
--expect-identity` still passes (no scoring-path change); the seed
commit lands before any Unit 2 share computation exists.

- [x] **Unit 2: Demand-coverage instrument**

**Goal:** The Stage 0 read-only instrument: three-way port
classification, feeder-yield diagnosis, addressable-share
measurement with null-model comparison, aggregate readout — reported
per commander, per port shape, and sliced by forensics bucket.

**Requirements:** R1, R1b-pin (consumption), R2 (inputs).

**Dependencies:** Unit 1.

**Files:**
- Create: `src/mtg_synergy_graph/bench/demand_coverage.py`
- Create: `scripts/demand_coverage.py` (thin shim, portfolio_sim
  style)
- Test: `tests/bench/test_demand_coverage.py`

**Approach:**
- Consumption predicate: `RULE_GATES` minus `CARD_LEVEL_RULES`,
  unioned with `RuleInterpreter` compiled gate closures
  (`gap_report._scan_universe` precedent — Stage 0 is the first
  production consumer of `CARD_LEVEL_RULES` subtraction; document
  that in the module docstring).
- Starved predicate (pinned in origin): gate fires AND
  (`load_tensor_candidates(conn, commander, config_hash)` count
  < 1,000 OR consuming rule contributes zero cards to that
  commander's top-30). Tensor staleness guarded via
  `compute_config_hash()` + populated-check; stale tensor → exit 2.
- NO_RULES miss lists via in-process `compute_forensics()`
  (`independent_check=False` acceptable for speed; document); per
  origin, cohort = commanders under the same <1,000 floor, the seven
  named commanders as qualitative verification set.
- Addressable share: for each cohort NO_RULES miss, does ANY
  provisional pairing connect an under-served commander demand port
  to a supplier-shaped port on the missed card? Pools capped at
  2,000 cards (a flow whose supplier pool exceeds the cap is
  reported as failing the bound and EXCLUDED — the "indiscriminate"
  DECLINE branch evidence). Null model: same-size random supply pool
  per flow (seeded RNG, fixed seed), share reported alongside.
- IDF-burial exclusion: misses reachable only through a
  consumed-port whose starvation diagnosis is IDF burial are counted
  separately and EXCLUDED from the funding numerator (origin R2).
- Aggregate readout: same reachability over ALL NO_RULES misses
  (non-gating; feeds the ≥100-label absolute floor).
- Reports: JSON + md to `.audit/demand_coverage/`; exit codes
  0/1/2/3 per portfolio_sim taxonomy; `--commander` filter for
  debugging; never touches `.audit/history.csv`.

**Execution note:** Test-first for the classification predicates
(consumption, starved, reachability) — they are the funding gate's
arithmetic and must be right before any full-fixture run.

**Patterns to follow:**
`src/mtg_synergy_graph/bench/portfolio_sim.py` (CLI, exit codes,
report writing, fixture loading); `scripts/gap_report.py::
_scan_universe` (gate union).

**Test scenarios:**
- Happy path: synthetic tmp_path DB with a commander carrying one
  unconsumed `cost.sacrifice` port + a candidate with a supplier
  shape → port classified unconsumed, miss counted addressable.
- Happy path: port consumed by a formal rule with healthy yield →
  consumed-with-material-yield; excluded from numerator.
- Edge: port consumed but commander activated-count below floor →
  consumed-but-starved, counted.
- Edge: rule in `CARD_LEVEL_RULES` fires on the port → does NOT
  count as consumption.
- Edge: declarative-rule gate fires on the port → DOES count as
  consumption (interpreter-gate union).
- Error path: stale tensor (config_hash mismatch) → exit 2 with
  actionable message; missing seed file → exit 2.
- Edge: supply pool over the 2,000 cap → flow excluded from
  numerator, reported as bound-failing.
- Edge: miss reachable ONLY via a burial-diagnosed port → excluded
  from the funding numerator; miss reachable via a burial-diagnosed
  AND a healthy under-served port → counted (the "only through"
  semantics).
- Edge: rule gate fires, activated-count ≥ floor, but the consuming
  rule contributes zero cards to the commander's top-30 →
  consumed-but-starved via the OR branch.
- Happy path: synthetic 2-commander fixture asserting the exact
  share ratio, cohort membership by the <1,000 rule (a commander
  above the floor is excluded from the denominator), and the
  ≥100-label fixture-wide floor arithmetic.
- Integration: null-model share computed with fixed seed is
  deterministic across runs, and the report carries the
  measured-vs-null exceedance verdict.
- Integration: full run on the real 100-commander fixture completes
  and writes both report files (marked slow if needed).
- Test-DB discipline: all DBs under `tmp_path` (CLAUDE.md
  convention; conftest guard).

**Verification:** Instrument runs end-to-end on the real fixture;
reports contain per-commander three-way classification, cohort +
aggregate shares, null-model comparison, yield diagnosis rows for
`gy_fuel_feeder`/`cost_payoff`; `--expect-identity` still passes.

- [x] **Unit 3: Stage 0 readout + routing decision**

**Goal:** Run the instrument, evaluate the pinned routing (DECLINE /
fund mechanism / feeder-widening reroute), and record the decision.

**Requirements:** R2.

**Dependencies:** Unit 2.

**Files:**
- Modify: this plan (DECISION block + checkboxes)
- Create (if DECLINE):
  `docs/solutions/best-practices/resource-flow-demand-null-result-2026-07-02.md`
- Modify: `docs/RULE_HISTORY.md` (dated entry either way)

**Approach:** Pure adjudication against the origin's pinned routing:
share ≥ 0.25 AND fixture-wide reach ≥ 100 labels, dominance analysis
(unconsumed vs starved; starved diagnosis), IDF-burial mass excluded.
On feeder-widening reroute: Phase B is NOT executed; the reroute
recommendation (which feeder, which pool widening) is recorded and a
fresh lightweight plan is opened for it — this plan closes with
status `declined` (mechanism) + shipped instrument.

**On a FUND routing, pin the named-miss lists HERE** (moved from
Unit 7 to close a gaming window): the ≥5-card list per gated-cohort
commander, drawn from the Unit 2 forensics NO_RULES output, is
committed in the same DECISION block — BEFORE any Unit 4/5 pool
authoring begins. Whoever finalizes the supply pools in Unit 5 must
never be able to draw the success predicate to fit the pools; the
commit boundary is the evidence of ordering, exactly as in Unit 1.

**Test scenarios:** Test expectation: none — decision/documentation
unit; the arithmetic it applies is tested in Unit 2.

**Verification:** A DECISION block exists in this plan quoting the
measured share, label reach, dominance, and the routing taken;
RULE_HISTORY entry added.

### Phase B — funded mechanism (conditional on Unit 3 = fund)

- [ ] **Unit 4: GATE_OPS extension for cost-demand predicates**

**Goal:** Extend the declarative vocabulary so the five flows'
demand gates are expressible as rows.

**Requirements:** R3, R4, R6 (narrowing grammar).

**Dependencies:** Unit 3 (fund routing).

**Files:**
- Modify: `src/mtg_synergy_graph/port_graph/vocabulary.py`
  (new leaf ops, `VOCAB_VERSION` bump)
- Modify: `src/mtg_synergy_graph/port_graph/rules_schema.py`
  (leaf validation),
  `src/mtg_synergy_graph/port_graph/interpreter.py`
  (both compilers: Python gate + SQL candidate)
- Test: `tests/test_rules_schema.py`, `tests/test_interpreter.py`
  (extend)

**Approach:** Add the minimal ops the real registry gates need
(`cost_target_eq`, `cost_subtype_part` — final names at
implementation against `_gy_fuel_feeder_gate`/`_cost_payoff_gate`
semantics; budget explicitly for a possible third op such as
`granted_keyword` — "undying" is keyword-borne and inexpressible in
the current grammar, and it sits inside the sacrifice-fodder
narrowing). Bump `VOCAB_VERSION`; run `--expect-identity` (no
behavior change while no row uses the new ops); re-pin only if the
hash flips per the vocabulary docstring contract. FALLBACK — with an
OPERATIONAL trigger, not implementer judgment: this unit's
acceptance criterion is that ALL FIVE flows' supplier-pool
predicates (including the recursion/token/undying narrowing) compile
in the extended grammar and pass a real-DB spot check (e.g.,
Gravecrawler/Reassembling Skeleton match sacrifice-fodder supply; a
vanilla creature does not). If that concrete test cannot be made to
pass, stop this unit and switch Units 5–6 to the doc-16
Python-primitive checklist — the seed and gates carry over. Do NOT
stretch LIKE-based `filter_tag`/`tribe` approximations to limp past
the criterion.

**Patterns to follow:** vocabulary.py docstring contract; existing
leaf-op compiler cases in `interpreter.py`.

**Test scenarios:**
- Happy path: a rule row using each new op compiles on both paths
  and matches a synthetic port with the target `cost_target`/
  `cost_subtype`.
- Error path: malformed op payload → ValueError with dotted path.
- Edge: old seed rows (no new ops) compile identically —
  `--expect-identity` bitwise gate.
- Integration: `seed_rules_db` round-trip with a new-op row; drift
  check still raises on seed↔DB mismatch.

**Verification:** All existing declarative tests green; identity
gate passes; new ops documented in the vocabulary docstring.

- [ ] **Unit 5: Resource-flow rules + flag-gated scoring wiring**

**Goal:** Promote the provisional table to final flow rules living in
`resource_flows_seed.json` (NEVER `rules_seed.json` — see hash
constraint below), teach the interpreter to load them only when the
flag is ON, and land identity-clean with the flag OFF.

**Requirements:** R3, R4, R5.

**Dependencies:** Unit 4.

**Hash constraint (verified in code, review pass):**
`compute_config_hash` hashes `rules_seed.json` rows and
`_RULE_QUALITY_MULTIPLIER` entries UNCONDITIONALLY — no flag can
guard them, and no flag-guarded hash pattern exists anywhere in the
codebase (the embedding precedent's fields are hashed
unconditionally; its docstring says flipping the field invalidates
the tensor). Therefore in this unit: NO edits to `rules_seed.json`,
NO edits to `scoring_weights.json`, NO `ScoringConfigInputs` change.
All hash-flipping edits consolidate into the Unit 7 flip commit
(this operationalizes the Key Technical Decision "digest field, hash
block, and re-pin happen only in Phase B at flip time").

**Files:**
- Modify: `src/mtg_synergy_graph/data/resource_flows_seed.json`
  (final pools per R6 narrowing; flow ROWS live here, one per flow)
- Modify: `src/mtg_synergy_graph/port_graph/resource_flows.py`
  (loader gains rule-row emission),
  `src/mtg_synergy_graph/complement_rules/core.py` (flag-gated
  loading site — see flag mechanism below),
  `src/mtg_synergy_graph/complement_rules/registry.py` ONLY if the
  flag-consult site lives there (note: `DECLARATIVE_RULE_IDS` is
  auto-derived from `rules_seed.json` at import — nothing to
  hand-edit; adding `RuleGate` entries for flow rule_ids would trip
  the `_DUAL_PATH_OVERLAP` guard — do NOT)
- Test: `tests/test_resource_flow_rules.py`, flag-gate test per
  `tests/test_pathway_flag_gate.py` pattern
- Modify: `scripts/_validate_rule.py` exclusion list IF any new
  config-hash pin test is added

**Approach:**
- **Flag mechanism (first flag-gated declarative rows — no
  precedent, so it is specced here):**
  `_ENABLE_RESOURCE_FLOW_RULES = False` gates whether the flow rows
  from `resource_flows_seed.json` are REGISTERED at all — flag OFF
  means they never enter the interpreter's compiled set, never
  appear in `DECLARATIVE_RULE_IDS`-adjacent routing, and never seed
  the DB `rules` table; bitwise identity is trivial by absence, and
  the seed↔DB drift check is unaffected. Flag ON (Unit 7 flip)
  loads them alongside `rules_seed.json` rows. The exact consult
  site (interpreter construction vs core.py block) is an
  implementation detail; the contract is registration-gating, not
  output-filtering or contribution-zeroing.
- Explain plumbing: recovered pairs must surface in `--explain`
  (through both `page()` and `score_one()`; decide
  `commander_port_predicate` use here).
- **`rule_quality_gate.py` protocol:** the gate evaluates LIVE
  scoring and has no flag facility — run it with
  `_ENABLE_RESOURCE_FLOW_RULES` patched ON (monkeypatch or
  uncommitted temporary edit, never committed). These runs are
  design-time measurements; the named-miss lists are already pinned
  at Unit 3 (see Unit 3), so no pin-ordering conflict. PASS
  required for every flow rule before Unit 7.
- Land implementation + tests in the SAME commit (pre-commit stash
  gotcha).

**Execution note:** Test-first for gate predicates and the flag
gate; characterization (`--expect-identity`) before and after every
commit in this unit.

**Patterns to follow:** `peer_tribal_keyword` migration (plan 003
Units 7–8) for declarative authoring;
`flag-gated-multi-port-rule-pattern-2026-04-23.md` for the flag +
identity-clean landing discipline.

**Test scenarios:**
- Happy path: flag ON (patched), Yawgmoth's `cost.sacrifice` +
  synthetic token-producer candidate → flow rule fires with correct
  rule_id and IDF weighting.
- Happy path: flag OFF (default) → zero contribution, bitwise
  identity with pre-unit scores.
- Edge: candidate in the commander's own set → excluded
  (`not_in_commander_set`).
- Edge: supply pool narrowing — a vanilla creature (no
  recursion/token/undying shape) does NOT match sacrifice-fodder
  supply (the tribe-as-fuel falsification population).
- Error path: seed row referencing an unknown flow/op → ValueError
  at interpreter init.
- Integration: `--explain` shows the flow firing for a real
  commander with flag patched ON.

**Verification:** Flag-OFF identity gate passes on the real DB; all
flow rules pass `rule_quality_gate.py`; no `history.csv` or pin
changes in this unit.

- [ ] **Unit 6: Overlap governance**

**Goal:** Execute R6a — per-flow SUPERSEDE / COEXIST-with-dedup /
SCOPE-AWAY decisions against the existing feeder family, with
overlap sidecars as evidence.

**Requirements:** R6a.

**Dependencies:** Unit 5.

**Files:**
- Modify: seed/rules files per decisions; possibly
  `src/mtg_synergy_graph/complement_rules/registry.py` (retired
  feeders) — exact set is an output of this unit
- Test: extend `tests/test_resource_flow_rules.py` with dedup
  scenarios
- Modify: this plan (decision table recorded per flow)

**Approach:** Run `bench.py audit --collinearity` and
`--embedding-dedup` with the flag patched ON against the feeder
family (`gy_fuel_feeder`, `cost_payoff`, `tap_type_feeder`,
`lifegain_feeder`, `life_total_feeder`, plus the
sacrifice/graveyard-side rules the sidecars implicate); interpret
via the rule-consolidation matrix. Any SUPERSEDE (feeder retirement)
is staged but executed only in the Unit 7 flip commit, so the
kill-test adjudicates the combined state and re-pin happens once.

**Test scenarios:**
- Integration: for each COEXIST flow, one test proving a
  (commander, candidate) pair matched by both the feeder and the
  flow rule is not double-credited beyond the documented dedup
  semantics.
- Edge: SCOPE-AWAY flow does not fire on the scoped-away shape.

**Verification:** Per-flow decision table in this plan; sidecar
outputs archived under `.audit/demand_coverage/`; no unexplained
VIF/dedup flags against the feeder family.

- [ ] **Unit 7: Kill-test, routing, and flip decision**

**Goal:** Adjudicate the mechanism against the origin's 3-part gate
with pre-pinned inputs; flip + single re-pin on SHIP only.

**Requirements:** R7, R8, R9; success criteria.

**Dependencies:** Units 5–6.

**Files:**
- Modify: this plan (pinned named-miss lists BEFORE flip; gate
  results; DECISION block)
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (flag flip on
  SHIP), pins via `bench.py audit --repin --yes` +
  `scripts/bootstrap_golden_set_500.py` (SHIP only)
- Create (if DECLINE):
  `docs/solutions/best-practices/resource-flow-demand-null-result-*.md`
- Modify: `docs/RULE_HISTORY.md`

**Gated cohort (per-commander NDCG deltas + named-miss recovery):**
Yawgmoth, Borborygmos Enraged, Araumi of the Dead Tide, Slimefoot,
Osgir, The Gitrog Monster. Phenax = negative control: reported, not
gated, must NOT move (movement indicates off-target firing). The
named-miss lists for these six were pinned at Unit 3's DECISION
block (gate: ≥3 of 5 enter top-30).

**Approach (ordered):**
1. Verify the Unit 3-pinned named-miss lists are committed and
   unmodified since the FUND decision (the gate checks membership
   against those lists only).
2. Re-derive the 500-fixture noise band with the current scoring
   config (`portfolio_sim.py bands` or equivalent) — band reuse was
   explicitly flagged in origin review; pin the fresh numbers here
   before flag-ON.
3. THE FLIP COMMIT consolidates every hash-flipping edit (see Unit 5
   hash constraint): flag → True, `ScoringConfigInputs` digest field
   + `compute_config_hash` block for `resource_flows_seed.json`,
   `scoring_weights.json` multiplier entries, `_RULE_TO_BUCKET`
   entries, Unit 6 SUPERSEDE retirements. The flag-ON audit runs
   against this state: 100-cmdr cohort gates (per-commander deltas;
   Phenax negative control), 500-cmdr aggregate non-regression +
   cliff scan (< −0.05 anywhere → DECLINE), gem gate aggregate-wide,
   trap set (calibration doc list incl. tribe-as-fuel), forensics
   before/after (NO_RULES share for the cohort), mechanics-native
   signals (≥5× universe growth, ≥0.8 explain coverage). Tensor
   regeneration under the new hash is an explicit step before any
   per-rule reads.
4. R8a optimizer confound pass on the 500 fixture before any
   DECLINE is finalized.
5. Route per canonical 5-outcome taxonomy; on SHIP: the flip commit
   lands with re-pin (both fixtures) — one re-pin total; on anything
   else: the flip commit is DISCARDED (never merged), the flag stays
   OFF, Phase B code remains dormant per repo convention
   (`_ENABLE_EMBEDDING_CONTRIBUTION` precedent), record and close.

**Test scenarios:**
- Integration: flag-gate test proves OFF-state bitwise identity
  after all Phase B code is merged (the standing guarantee while
  routing is pending).
- Test expectation for the adjudication itself: none — it is a
  measurement protocol; its arithmetic lives in bench instruments
  already tested.

**Verification:** DECISION block with all gate numbers; pins
untouched unless SHIP; null-result doc + RULE_HISTORY entry on
DECLINE; memory updated either way.

## System-Wide Impact

- **Interaction graph:** new declarative rows ride the existing
  interpreter path in `find_all_complements`; flag-OFF
  short-circuit keeps production untouched until flip. GATE_OPS
  extension touches both compilers — old rows must compile
  bitwise-identically.
- **Error propagation:** all new loaders raise ValueError with
  pointers (never assert); instrument uses exit-code taxonomy with
  exit 3 = do-not-trust.
- **State lifecycle risks:** tensor staleness between Stage 0 and
  Phase B (config_hash recorded in every report; stale → exit 2);
  provisional-seed → real-seed promotion must not leave two
  authorities (the provisional table becomes the seed or is
  explicitly superseded by it in the same commit).
- **API surface parity:** `--explain` must show flow firings in both
  `page()` and `score_one()` paths.
- **Integration coverage:** flag-gate bitwise identity test; dedup
  non-double-credit test; full-fixture instrument run.
- **Unchanged invariants:** pinned fixtures and `history.csv`
  untouched until a SHIP re-pin; rule_id single-path invariant;
  EDHREC absent from inference; no ranking-layer transforms.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| General demand rule = Ward:2 vacuum-fill at scale | `rule_quality_gate.py` PASS per flow pre-flip; supply-pool narrowing in seed; ≤2,000 pool bound carried from Stage 0 |
| Tribe-as-fuel supply pools carry no candidate evidence | Named falsification population in the trap set; sacrifice-fodder narrowing excludes vanilla bodies |
| Stage 0 numerator gaming via broad pools | Pools pre-pinned (Unit 1 commit precedes Unit 2), ≤2,000 bound, null-model comparison |
| `CARD_LEVEL_RULES` subtraction has no working precedent | Test-first predicates in Unit 2; document as first consumer |
| GATE_OPS extension can't express R6 narrowing | Explicit fallback to doc-16 Python primitive; seed survives the switch |
| 7-commander cohort NDCG is intrinsically noisy | Per-commander deltas reported; named-miss recovery is the load-bearing cohort gate, not the average |
| Band staleness after new rule family | Band re-derived at Unit 7 step 2 before flag-ON |
| Phase B built on a Stage 0 false positive | Funding requires share AND absolute floor AND dominance analysis; IDF-burial mass excluded |

## Sources & References

- **Origin document:**
  [docs/brainstorms/2026-07-02-resource-flow-demand-requirements.md](../brainstorms/2026-07-02-resource-flow-demand-requirements.md)
- Related code: `src/mtg_synergy_graph/port_graph/interpreter.py`,
  `src/mtg_synergy_graph/complement_rules/registry.py`,
  `src/mtg_synergy_graph/bench/forensics.py`,
  `src/mtg_synergy_graph/bench/portfolio_sim.py`,
  `scripts/gap_report.py`
- Related plans: 2026-07-02-001/002/003/004 (the same-day probe
  lineage this cycle inherits gates from)
- Institutional learnings: see Context & Research (12 docs)
