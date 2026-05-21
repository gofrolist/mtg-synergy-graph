---
title: BM25 IDF reformulation probe
type: feat
status: active
date: 2026-05-04
origin: docs/brainstorms/2026-05-04-bm25-idf-requirements.md
---

# BM25 IDF reformulation probe

## Overview

Replace the per-rule IDF formula in `src/mtg_synergy_graph/universal_scorer.py`
from `1/log2(1 + N)` to BM25-style saturation IDF
`log((N − df + 0.5) / (df + 0.5) + 1)`. Re-tune the
`_RULE_QUALITY_MULTIPLIER` table under the new IDF basis (so the
comparison is "current IDF + tuned weights" vs "BM25 IDF + freshly-tuned
weights"). Audit on the 500-cmdr fixture; ship if the bar is met.

This is a **probe**, not a feature: the goal is to learn whether the
saturation curve change is a real lever, with three possible outcomes
(SHIP / INVESTIGATE / DECLINE) defined by the origin requirements doc.

## Problem Frame

A 2026-05-04 session exhausted three direct improvement levers for
NDCG@30 on the 500-cmdr held subset (per-rule weight optimizer,
embedding contribution flip, walker rule-shipping queue). The
`docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
diagnosis identified "orthogonal directions" as the next class of
moves; the 2026-05-04 brainstorm picked BM25 IDF reformulation as the
cheapest probe.

If BM25 lifts NDCG@30 above the ship bar, the IDF saturation curve was
a real lever and weight-tuning has more headroom than the optimizer
sweeps suggested. If not, the next brainstorm picks from the
remaining four IDF alternatives or pivots to a higher-effort
orthogonal direction (anti-synergy rules, port-signature feature
expansion, commander-target redesign).

(See origin: `docs/brainstorms/2026-05-04-bm25-idf-requirements.md`.)

## Requirements Trace

- R1. Implement BM25 IDF as a direct replacement for `1/log2(1 + N)` in
  `src/mtg_synergy_graph/universal_scorer.py:702` (`_compute_idf_basis`).
- R2. Per-commander NDCG@30 reporting infrastructure for the
  prerequisite gate (any commander loses >0.05 → DECLINE).
- R3. Audit-gated swap: comparison via `bench.py audit` against the
  500-cmdr fixture; ship-bar evaluation per origin success criteria.
- R4. Resolve calibration-confound (origin Q1) — see Key Technical
  Decisions for the chosen approach.
- R5. Atomic re-pin of BOTH 500-cmdr AND 100-cmdr fixtures on SHIP
  outcome.
- R6. Rebuild `.audit/walker_outcomes.csv` baseline on SHIP (otherwise
  stale REJECT verdicts inherit from log2-IDF era).
- R7. Update `memory/feedback_edhrec_not_goal.md` with dated reframe
  note on SHIP, OR dated "declined" note on DECLINE.

## Scope Boundaries

- **Out of scope: alternative IDF formulations.** Smoothed-frequency,
  structural-overlap, per-rule-cluster, and rank-aware variants stay
  deferred per origin Out of Scope. If BM25 declines, those become
  candidate follow-on brainstorms.
- **Out of scope: hybrid IDF or per-commander overrides.** Per-commander
  overrides directly violate the project's "no per-commander or
  per-archetype rules" anti-goal. If BM25 lifts aggregate but creates
  tail regressions, the outcome is DECLINE — not hybrid.
- **Out of scope: per-archetype cohort analysis.** Per origin doc:
  `data/tags.db` does not contain commander→archetype labels (per
  `bench/optimize.py:153`). Adding them is its own work item.
- **Out of scope: BM25 with TF.** This work uses BM25's IDF half only
  (the project has no TF analog — per-(rule, candidate) firings are
  binary).
- **Out of scope: switchable config flag.** Per origin Out of Scope:
  BM25 is a direct formula replacement; reverting is `git revert` on
  the formula-change commit, not a flag flip.

### Deferred to Separate Tasks

- **Bootstrap-resample precision check infrastructure:** Origin doc
  downgraded to seed-vary (no new tooling). If the seed-vary spread
  reveals the +0.010 ship bar is in noise, a bootstrap module is its
  own follow-on plan.
- **IDF distribution histogram tooling:** Origin doc cut as
  preemptive infrastructure; per-commander gate catches catastrophic
  scale-shift impact. If the audit reveals subtle scale-shift issues
  not caught by the per-commander gate, this becomes its own plan.
- **Per-archetype audit gates:** See Out of Scope above. If BM25
  declines and the failure mode is systematic per-archetype skew, a
  follow-on brainstorm should design archetype-aware audit gates.

## Context & Research

### Relevant Code and Patterns

- **`src/mtg_synergy_graph/universal_scorer.py:702`** — current
  `1.0 / math.log2(1.0 + n)` lives inside `_compute_idf_basis()`.
  This is the single formula site. The wrapper
  `_compute_idf_weights()` (line 724) delegates to the basis function
  and does not need separate changes.
- **`src/mtg_synergy_graph/universal_scorer.py:693-702`** — `if rule_id
  in _FLAT_COUNT_RULES:` branch. Flat rules (`etb_self`, `evasion`,
  `scaling`, `spell_density`, `token_producer`, `tribal_density`)
  bypass the formula entirely. This branch must remain untouched —
  flat rules continue to bypass under BM25.
- **`src/mtg_synergy_graph/universal_scorer.py:701`** — panharmonicon
  special case `n = max(n, 30)`. A floor on `n` before applying the
  formula. Preserve under BM25 (semantics: prevents IDF blow-up for
  rare rules with very low candidate counts).
- **`src/mtg_synergy_graph/bench/optimize.py:630-666`** — `compute_ndcg`
  + `per_ndcg dict` patterns. Source for the new per-commander NDCG
  reporting handler (Unit 1).
- **`src/mtg_synergy_graph/bench/handlers.py:333`** — existing `--inspect`
  handler. Pattern to follow for the new `--per-commander-ndcg` flag.
- **`src/mtg_synergy_graph/bench/handlers.py:122-125`** —
  `expect-identity` assertion. Will fail by design when BM25 ships;
  re-pin via `bench.py audit --repin --yes` is required.
- **`src/mtg_synergy_graph/bench/cli.py:302`** — default fixture is the
  100-cmdr canonical (`tests/fixtures/golden_set_run.json`). Stage A
  pre-flight reads against this in pre-commit hooks.
- **`src/mtg_synergy_graph/bench/optimize.py:1477`** — `_CANONICAL_FIXTURE`
  references the 100-cmdr canonical.
- **`scripts/scaffold_rule.py`** — Stage A pre-flight integration
  (PR #39, just shipped). Reads against the canonical fixture state.
- **`data/scoring_weights.json`** — 53 `rule_quality_multiplier` + 6
  `flat_weight_overrides` entries calibrated against the current IDF.
  Re-tuned under BM25 in Unit 3.

### Institutional Learnings

- `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md` —
  origin diagnosis that "orthogonal directions" are the next move
  class.
- `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md` —
  500-cmdr fixture is the calibration baseline; 100-cmdr is too small
  for trustworthy gradient signal.
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md` —
  null-result discipline; BM25 must follow same pattern (document and
  decline explicitly if it doesn't clear the bar).

### External References

None. BM25 is a canonical IR formula; the brainstorm's workload-fit
disclosure already addresses the small-vocabulary / no-TF caveats.
External search would not change the implementation shape.

## Key Technical Decisions

- **Q1 (calibration confound) → option (c) re-tune under BM25.**
  Cleanest shipping path. The current 53 `rule_quality_multiplier`
  entries were tuned against `1/log2(1+N)` IDF; under BM25 their
  effective contributions shift. Re-tuning produces an apples-to-apples
  comparison ("current IDF + tuned weights" vs "BM25 IDF + tuned
  weights"). Compute cost is ~5-15 min per optimizer run (per
  2026-05-04 session evidence — implementer should re-confirm in Unit 3).
- **Pre-Unit-3 baseline check (NEW per plan-review pass 1):** Before
  running Unit 3, run the optimizer self-test against the CURRENT
  (log2) IDF to confirm baseline pass behavior. If it fails on
  current IDF too, the self-test instability is independent of the
  IDF formula and Unit 3's fallback chain is just churn — restructure
  Unit 3 to ship option (a) explicitly upfront with a documented
  "self-test cannot validate optimizer under either IDF" caveat,
  rather than pretend (c) is the path.
- **Q1 fallback (rare path) → SHIP forbidden on fallback.** If
  optimizer self-test fails under BM25 across 3 alternative
  `--self-test-seed` values (`7`, `17`, `137`), fall back to option
  (a) — reset multipliers to 1.0 in `data/scoring_weights.json`. **Under
  this fallback, Unit 4's SHIP outcome is FORBIDDEN regardless of
  point estimate.** Permitted outcomes are limited to INVESTIGATE-FOR-RETUNE
  (a new outcome — see Success Criteria) or DECLINE. This prevents
  shipping a production scoring config the plan itself flagged as
  "not directly shippable."
- **Q2 (per-commander NDCG reporting) → new flag.**
  `bench.py audit --inspect` does NOT surface per-commander NDCG@30
  diffs (verified in origin doc-review pass 2 — it's per-(rule,
  candidate) tensor rows scoped to one rule_id). Build a new
  `--per-commander-ndcg` flag that lifts the `compute_ndcg + per_ndcg`
  pattern from `bench/optimize.py:630-666` into a new reporting
  handler. Output sorted ascending by NDCG delta. The per-commander
  prerequisite gate (any commander >-0.05 → DECLINE) is unimplementable
  without this.
- **Outcome branching.** Five outcomes per Success Criteria below:
  SHIP, INVESTIGATE, INCONCLUSIVE (precision check failed),
  INVESTIGATE-FOR-RETUNE (Q1 fallback fired), DECLINE. Outcome
  handling (Unit 5) executes the appropriate branch based on Unit 4's
  verdict. The plan scopes all five paths. The base SHIP/INVESTIGATE/DECLINE
  trio comes from the brainstorm; INCONCLUSIVE and
  INVESTIGATE-FOR-RETUNE were added in plan-review pass 1 to close
  identified gaps (precision-routing collision and Q1 fallback escape
  hatch).
- **Atomic re-pin requires both fixtures.** The 100-cmdr canonical
  feeds Stage A pre-flight in pre-commit hooks; if only the 500-cmdr
  re-pins, Stage A inherits stale verdicts from the log2-IDF era and
  the autonomous walker may skip proposals that would now PASS.
  "Atomic" here means: both fixtures re-pinned in the SAME commit
  (no commit may exist where one fixture is BM25-pinned and the
  other is log2-pinned). The walker_outcomes.csv "rebuild" is
  separate operational housekeeping (gitignored, not part of the
  commit). **Pre-flight verification (NEW per plan-review pass 1):**
  Implementer must verify experimentally before the SHIP commit that
  two back-to-back `bench.py audit --repin --yes` invocations are
  idempotent — run on a scratch branch first, examine working-tree
  state after each, confirm both fixtures land in their expected
  states without one's state contaminating the other. If
  non-idempotent, escalate to extending `bench.py audit --repin` to
  accept multiple `--fixture` arguments in a single invocation.
- **Unit 1 is bundled but separable.** Unit 1 (per-commander NDCG
  reporting) is general-purpose audit infrastructure that survives
  this plan's outcome. It IS bundled here for sequencing simplicity
  — the BM25 probe needs it as a prerequisite. But it ships in its
  own commit (NOT bundled with the BM25 formula commit), and its
  output schema must be designed for general "compare two scoring
  configurations on per-commander NDCG" use, not specialized to
  BM25's needs. If a future audit task needs the same reporting,
  it should reuse Unit 1's flag without modification.

## Open Questions

### Resolved During Planning

- **Q1 (calibration confound):** Resolved → option (c) re-tune
  optimizer under BM25 (see Key Technical Decisions).
- **Q2 (per-commander NDCG reporting):** Resolved → new flag required
  (see Key Technical Decisions).

### Deferred to Implementation

- **Optimizer self-test stability under BM25:** The 2026-05-04 session
  observed self-test failures on alpha 0.3 and fine-grid optimizer
  runs (one specific rule —
  `etbreplacement_other_choosect_tribal` — couldn't be recovered).
  Whether BM25 changes which rule the self-test selects, and whether
  the new selection passes, is unknown until Unit 3 runs. Fallback
  pre-defined (3 alternative `--self-test-seed` values, then option
  (a) reset).
- **Whether BM25 produces large per-rule contribution shifts that
  trip Stage A pre-flight verdicts on prior scaffold proposals:**
  Unknown until Unit 5 runs. The atomic re-pin + walker_outcomes
  rebuild handles the consequences, not the prediction.
- **Final shape of the per-commander NDCG output (CSV vs Markdown vs
  JSON):** Implementer's call. Match existing audit output
  conventions (likely Markdown table for human review).
- **Whether a SHIP outcome warrants any change to `CLAUDE.md`'s
  scoring documentation.** Likely yes — IDF formula description in
  the "Algorithm" section would need update. Determine in Unit 5.

## Implementation Units

- [ ] **Unit 1: Per-commander NDCG@30 audit reporting**

**Goal:** Add a `--per-commander-ndcg` flag (or extend `--inspect`)
that emits per-commander NDCG@30 deltas across the held subset of the
audited fixture, sorted ascending. Required prerequisite for the
per-commander gate in Unit 4.

**Requirements:** R2

**Dependencies:** None — can run in parallel with Unit 2.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cli.py` (add CLI flag)
- Modify: `src/mtg_synergy_graph/bench/handlers.py` (new handler)
- Modify: `scripts/bench.py` (wire flag through)
- Test: `tests/test_bench_per_commander_ndcg.py` (new)

**Approach:**
- Reuse `compute_ndcg` (already public in `mtg_synergy_graph.validate`,
  imported at `bench/optimize.py:49`) and extract the per-commander
  loop pattern from `_score_split_with_weights()` at
  `bench/optimize.py:629-666` into a new handler in
  `bench/handlers.py`. The handler does not need to touch the
  optimizer's weight-patching context (no `patched_rule_quality_multiplier`)
  — it just iterates commanders in the held subset, scores each,
  computes per-commander NDCG vs the pinned reference, and emits the
  table.
- Emit per-commander NDCG@30 (current pin vs live scoring) sorted by
  delta ascending. Format: Markdown table for human review (matches
  `--inspect` convention).
- The handler reads pinned fixture + live scoring; doesn't mutate state.
- Output schema: `commander | pinned_ndcg | live_ndcg | delta` with N
  rows where N = held-subset size.
- The flag accepts the standard `--fixture PATH` argument so it can
  be run against either the 100-cmdr or 500-cmdr fixture.

**Patterns to follow:**
- `bench/handlers.py:333` — existing `--inspect` handler pattern.
- `bench/optimize.py:630-666` — `compute_ndcg + per_ndcg` lifting source.

**Test scenarios:**
- Happy path: 500-cmdr fixture, identity scoring (same as pin) →
  emits all-zero deltas across N rows.
- Happy path: synthetic perturbation in scoring → emits non-zero
  deltas in expected commanders.
- Edge case: empty held subset (e.g., train_ratio = 1.0) → emits
  empty table with informative message, not a crash.
- Edge case: held subset with single commander → emits one row.
- Integration: invoked via `bench.py audit --per-commander-ndcg
  --fixture tests/fixtures/golden_set_run_500.json` → produces output
  consumable by Unit 4's gate evaluation.

**Verification:**
- `uv run scripts/bench.py audit --per-commander-ndcg --fixture
  tests/fixtures/golden_set_run_500.json` produces a sorted-ascending
  Markdown table.
- New tests pass; full test suite stays green.
- No change to scoring or audit identity (this is a read-only
  reporting addition). `bench.py audit --expect-identity` continues
  to pass.

---

- [ ] **Unit 2: BM25 IDF formula in `_compute_idf_basis`**

**Goal:** Replace the live `1.0 / math.log2(1.0 + n)` with classic BM25
IDF `log((N − df + 0.5) / (df + 0.5) + 1)`. Update tests asserting
specific IDF values. Preserve the FLAT_COUNT_RULES bypass and the
panharmonicon `max(n, 30)` floor.

**Requirements:** R1

**Dependencies:** None — can run in parallel with Unit 1.

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (formula site
  at line 702 inside `_compute_idf_basis`)
- Modify: existing tests asserting specific IDF values (enumerate
  during execution; likely `tests/test_universal_scorer.py` and
  similar)
- Test: `tests/test_universal_scorer_bm25.py` (new) for BM25-specific
  invariants

**Approach:**
- Identify `N` for BM25's formula. Current `_compute_idf_basis` uses
  `n = len(candidates)` per (rule_id, cmdr_event, cand_event,
  filter_group) key — this is the per-key df, not a corpus-wide N.
  For BM25 to make semantic sense, **N must be the total candidate
  pool size for the commander** (cards considered across all rules
  for that commander), and df remains the per-key match count.
- **N-source decision (locked):** Use **strict N** = total distinct
  candidates considered for that commander (sum of distinct
  candidates across all (rule_id, cmdr_event, cand_event,
  filter_group) keys for the commander, deduplicated). This requires
  new plumbing into `_compute_idf_basis` (either pass total N as a
  parameter, or compute it from the candidates collection in the same
  scope). The previously-considered "approximate N = max(n_per_key)"
  is rejected because (a) it produces a per-commander scaling that
  conflicts with the no-per-commander-rules anti-goal, and (b) it's
  not BM25 — it's a different formula that happens to share BM25's
  log structure. A DECLINE outcome under approximate-N would not
  rule out BM25; it would rule out a different formula. Strict N
  preserves the probe's epistemic value.
- Preserve the `if rule_id in _FLAT_COUNT_RULES:` branch entirely —
  flat rules continue to bypass the formula.
- Preserve `n = max(n, 30)` panharmonicon floor before applying BM25.
- Direct replacement; no `_ENABLE_BM25_IDF` flag (per Out of Scope).

**Execution note:** Test-first for the BM25 formula computation.
Write `tests/test_universal_scorer_bm25.py` with known (df, N) →
expected IDF pairs FIRST, then implement. The formula is small and
math-verifiable; test-first catches arithmetic errors immediately.

**Patterns to follow:**
- `src/mtg_synergy_graph/universal_scorer.py:693-702` — the existing
  if/else branch structure that protects FLAT_COUNT_RULES.

**Test scenarios:**
- Happy path: BM25 IDF for (df=10, N=5000) → matches hand-computed
  value to 6 decimal places.
- Happy path: BM25 IDF for (df=2000, N=5000) → matches hand-computed
  value (tests the high-df / low-IDF tail).
- Edge case: BM25 IDF for (df=1, N=5000) → very large IDF (rare
  rule).
- Edge case: BM25 IDF for (df=N, N=5000) → IDF approaches 0 but stays
  ≥ 0 (validates the +1 inside the log).
- Edge case: BM25 IDF for (df=0, N=5000) → handle gracefully (likely
  doesn't occur in practice but protective).
- Invariant: BM25 IDF is non-negative for all valid (df, N) where
  0 ≤ df ≤ N.
- Invariant: FLAT_COUNT_RULES rules bypass — assert that scoring for
  `etb_self` (or another flat rule) produces identical contribution
  before and after the formula change.
- Invariant: Panharmonicon floor preserved — assert `n = max(n, 30)`
  applies before BM25.
- Cache discipline: After the formula swap, `IdfBasis` cache keys
  produce different values than before; existing cached entries from
  before the swap should not contaminate (verify via fresh
  `_compute_idf_basis` invocation).
- Integration: `bench.py audit --expect-identity` against current
  pin FAILS by design (scores are different); `bench.py audit
  --repin` regenerates the pinned fixture under BM25 (verify in Unit
  5, not here — Unit 2 is just the formula).

**Verification:**
- `tests/test_universal_scorer_bm25.py` passes.
- Existing IDF-value tests updated to new expected values; full test
  suite stays green.
- `bench.py audit --expect-identity` FAILS as expected against
  current pin (this is the design — re-pin happens in Unit 5).
- The 6 flat rules show identical contributions pre- and post-change
  (proves bypass is preserved).

---

- [ ] **Unit 3: Re-tune `_RULE_QUALITY_MULTIPLIER` under BM25
  (calibration-confound resolution)**

**Goal:** Run `bench.py audit --optimize` with BM25 IDF active to
re-tune the 53 multipliers in `data/scoring_weights.json`. Capture the
proposal as the new "BM25-tuned" weight set. Self-test must pass (or
fallback documented per Q1 fallback).

**Requirements:** R4

**Dependencies:** Unit 2 (BM25 formula must be live).

**Files:**
- Read: `src/mtg_synergy_graph/bench/optimize.py` (existing optimizer)
- Modify: `data/scoring_weights.json` (write proposal contents to
  this file as the new calibration; keep an inline comment
  documenting "tuned under BM25 IDF on 2026-05-04 via Unit 3")
- Read: `.audit/optimize_proposal.json` (proposal sink path)
- Read: `.audit/optimize_history.csv` (audit log)

**Approach:**
- Run: `uv run scripts/bench.py audit --optimize` against the 500-cmdr
  fixture. Defaults are correct: alpha=0.5, default grid, default
  seed=42.
- Inspect `.audit/optimize_proposal.json`. If self-test passed and
  the proposal looks reasonable (no clamp_max=5.0 runaways, sensible
  per-rule_quality_multiplier diffs), accept by writing the new
  multipliers into `data/scoring_weights.json`.
- If self-test fails: try `--self-test-seed 7`, then `--self-test-seed
  17`, then `--self-test-seed 137`. If any pass, accept that
  proposal.
- If all 3 self-test attempts fail: fall back to Q1 option (a) —
  reset all 53 multipliers to 1.0 in `data/scoring_weights.json`.
  Document in commit message that calibration-confound resolution
  fell back from option (c) to option (a) due to optimizer self-test
  instability under BM25, and the comparison in Unit 4 should be
  treated as scientific-only (not directly shippable; SHIP outcome
  routes to a follow-on optimizer-tuning task).
- This unit modifies `data/scoring_weights.json` even though Out of
  Scope normally excludes it — origin doc explicitly allows
  modifications "to the extent that resolving the calibration-confound
  (Q1) requires touching them."

**Patterns to follow:**
- `bench/optimize.py` existing optimizer invocation; CLI documented in
  `CLAUDE.md`.

**Test scenarios:**
- Test expectation: none — this unit is one-shot data generation
  (running the optimizer to produce a calibration). The optimizer
  itself has its own test suite. The output is reviewed manually; no
  new automated test is appropriate.
- Self-test pass/fail is verified by the optimizer itself; the unit's
  job is to invoke it and act on the outcome.

**Verification:**
- `data/scoring_weights.json` updated with new BM25-tuned multipliers
  (or reset to 1.0 if fallback fired).
- `.audit/optimize_history.csv` shows the run with passing self-test
  (or 3 failed seed attempts + fallback).
- Commit message clearly documents which path was taken.
- Full test suite passes after `data/scoring_weights.json` update.

---

- [ ] **Unit 4: Audit + evaluate against success criteria**

**Goal:** Run `bench.py audit --fixture tests/fixtures/golden_set_run_500.json`
under BM25 IDF + new multipliers (or reset multipliers if fallback
fired). Capture per-commander NDCG diff (via Unit 1 reporting) and
aggregate metrics. Apply success criteria gates. Produce verdict:
SHIP / INVESTIGATE / DECLINE.

**Requirements:** R3

**Dependencies:** Units 1, 2, 3.

**Files:**
- Read: `tests/fixtures/golden_set_run_500.json` (audit fixture)
- Read: existing pinned fixture state (current pin)
- Write: `.audit/last.md` (audit output)
- Write: working notes documenting the verdict

**Approach:**
- Step 1: Seed-vary precision check. Run audit under 3 train/held
  split seeds (17, 42, 137). Report mean NDCG@30 delta and spread
  (max - min across seeds).
- Step 2: Run `bench.py audit --per-commander-ndcg --fixture
  tests/fixtures/golden_set_run_500.json` (Unit 1 flag). Inspect the
  worst-regressing commanders.
- Step 3: Apply outcome-routing checks in this order. The first
  matching condition determines the outcome.
  - **Q1-fallback gate:** If Unit 3 fell back to option (a)
    (multipliers reset to 1.0), permitted outcomes are limited to
    **INVESTIGATE-FOR-RETUNE** (if numerics would otherwise route to
    SHIP) or **DECLINE** (if numerics route to DECLINE). SHIP is
    forbidden under fallback regardless of numerics.
  - **Per-commander prerequisite:** If ANY commander in the held
    subset shows NDCG@30 delta < -0.05 → **DECLINE**. Stop.
  - **Precision-inconclusive guard:** If seed-vary spread (Step 1)
    exceeds 0.010 → **INCONCLUSIVE**. Document the spread and
    point-estimate; do not revert; do not ship. Defer to a follow-on
    plan that gathers more seeds.
  - **Aggregate SHIP:** If NDCG@30 delta ≥ +0.010 AND
    hidden_gem_hit_rate delta ≥ -0.010 → **SHIP**.
  - **Aggregate INVESTIGATE:** If NDCG@30 delta in [+0.005, +0.010)
    AND gem within bound → **INVESTIGATE**.
  - **Aggregate DECLINE:** Else → **DECLINE**.
- Step 4: Document the verdict (which gate fired, exact deltas,
  worst-regressing commander, seed-spread) in working notes for use
  by Unit 5.

**Patterns to follow:**
- `CLAUDE.md` Common Commands section — `bench.py audit` invocation
  patterns.

**Test scenarios:**
- Test expectation: none — this unit is one-shot evaluation. The
  audit itself has its own test suite. The verdict is a manual
  decision based on the output; no new automated test is appropriate.

**Verification:**
- Audit runs cleanly under BM25 IDF + new multipliers, producing
  `.audit/last.md`.
- 3-seed precision spread documented.
- Per-commander NDCG diff captured and inspected.
- Verdict explicitly recorded (one of SHIP / INVESTIGATE / DECLINE)
  with exact numerical justification.

---

- [ ] **Unit 5: Outcome handling (conditional on Unit 4 verdict)**

**Goal:** Execute one of five branches per Unit 4 verdict: SHIP,
INVESTIGATE, INCONCLUSIVE, INVESTIGATE-FOR-RETUNE, or DECLINE. Each
branch leaves the repo and institutional record in a coherent state.

**Pre-Unit-2 baseline tag (NEW per plan-review pass 1):** Before
starting any code changes in Unit 2, create a git tag
`pre-bm25-baseline` on the current HEAD. The DECLINE and
INVESTIGATE branches reset to this tag rather than relying on
`git revert` (which can produce conflicts if intervening commits
touched the same files). This is a 5-second prerequisite that makes
the DECLINE/INVESTIGATE paths reliable.

**Requirements:** R5, R6, R7

**Dependencies:** Unit 4 verdict.

**Files (SHIP branch):**
- Modify: `tests/fixtures/golden_set_run_500.json` (re-pin via
  `bench.py audit --repin --yes --fixture
  tests/fixtures/golden_set_run_500.json`)
- Modify: `tests/fixtures/golden_set_run.json` (re-pin via
  `bench.py audit --repin --yes` — uses default 100-cmdr fixture)
- Modify: `.audit/walker_outcomes.csv` (rebuild — likely just delete
  and let it regenerate on next walker run, or add column noting
  IDF-formula-version transition)
- Modify: `docs/RULE_HISTORY.md` (single entry documenting BM25 IDF
  swap, date, audit deltas, multiplier-tuning approach)
- Modify: `memory/feedback_edhrec_not_goal.md` (dated reframe note
  per origin Product Decision section)
- Modify: `CLAUDE.md` (update Algorithm section IDF formula
  description if the new formula is named in CLAUDE.md)

**Files (DECLINE branch):**
- Revert: `data/scoring_weights.json` (restore pre-Unit-3 state via
  git revert of Unit 3 commit, OR reset multipliers if Unit 3 used
  fallback)
- Revert: `src/mtg_synergy_graph/universal_scorer.py` (revert Unit 2
  formula change)
- Revert: any test changes from Unit 2 that asserted BM25-specific
  values
- Modify: `memory/feedback_edhrec_not_goal.md` (dated "declined" note
  per origin Product Decision section)
- Create: `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`
  (null-result writeup following
  `infrastructure-without-scoring-activation-2026-04-24.md`
  template; document the precise deltas, the fallback path if any,
  and what's NOT ruled out — the four other IDF alternatives)

**Files (INVESTIGATE branch):**
- Same as DECLINE for code reverts. **Pinned fixture stays UNCHANGED**
  — current pin remains current; BM25 numbers do NOT become the
  new baseline. (Per origin doc: the +0.005 lift is NOT pre-paid
  progress for the next IDF-variant attempt.)
- Create: `docs/solutions/best-practices/bm25-idf-marginal-result-2026-05-04.md`
  (similar to null-result, but explicitly noting the +0.005 to
  +0.010 zone and the recommendation to attempt a different IDF
  variant in a new brainstorm — explicitly noting that the +0.005
  lift is NOT pre-paid progress for that next attempt)
- Modify: `memory/feedback_edhrec_not_goal.md` (dated "investigated,
  did not ship" note, scoped textually to BM25 work only)

**Files (INCONCLUSIVE branch — NEW):**
- Same as INVESTIGATE for code reverts (formula reverts; pinned
  fixture stays unchanged).
- Create: `docs/solutions/best-practices/bm25-idf-inconclusive-2026-05-04.md`
  (documents seed-spread > 0.010, point-estimate, and what would be
  needed to disambiguate — likely "a follow-on plan that runs more
  seeds OR scales the fixture beyond 500-cmdr").
- Modify: `memory/feedback_edhrec_not_goal.md` (dated "inconclusive
  precision; did not ship" note, scoped to BM25 work only).

**Files (INVESTIGATE-FOR-RETUNE branch — NEW):**
- Same as INVESTIGATE for code reverts.
- Create: `docs/solutions/best-practices/bm25-idf-fallback-retune-needed-2026-05-04.md`
  (documents that Q1 fallback fired, multipliers were reset to 1.0,
  numerics would have routed to SHIP, but production calibration
  cannot ship under reset multipliers; recommends a follow-on
  optimizer-tuning plan that re-tunes weights under BM25 IDF before
  shipping).
- Modify: `memory/feedback_edhrec_not_goal.md` (dated "fallback
  fired; needs follow-on retune" note, scoped to BM25 work only).

**Approach:**

**SHIP branch (~20 min):**
0. **Atomic re-pin verification (NEW per plan-review pass 1):** On a
   scratch branch (e.g., `tmp/bm25-repin-test`), run two back-to-back
   `bench.py audit --repin --yes` invocations (one per fixture).
   Inspect working-tree state after each. Confirm both fixtures land
   in their expected states without one's state contaminating the
   other (verify by re-running on a fresh clone). If non-idempotent,
   STOP — extend `bench.py audit --repin` to accept multiple
   `--fixture` arguments before continuing. Discard the scratch branch.
1. Stage A pre-flight verdict shift survey: before re-pinning the
   100-cmdr fixture, run `gap_report.py` and capture the current
   PASS/WARN/REJECT distribution.
2. Re-pin the 500-cmdr fixture: `uv run scripts/bench.py audit --repin
   --yes --fixture tests/fixtures/golden_set_run_500.json`.
3. Re-pin the 100-cmdr fixture: `uv run scripts/bench.py audit --repin
   --yes` (uses default fixture).
4. Re-run `gap_report.py`; document any PASS/WARN/REJECT verdict
   shifts in the commit message. (Heuristic: if >10% of prior PASS
   verdicts flip, flag in the commit message and consider follow-on
   Stage A re-evaluation as a separate task.)
5. Update `memory/feedback_edhrec_not_goal.md`: prepend or append a
   dated note acknowledging the metric reframe **scoped to BM25 work
   only**. Suggested text: "2026-05-04: BM25 IDF reformulation work
   shipped. **For BM25-IDF-related decisions only**, NDCG@30 was
   treated as primary metric and gem-rate non-regression was
   symmetric at -0.010. This scoped reframe does NOT extend to other
   scoring-axis decisions; future orthogonal-direction brainstorms
   must re-justify the primary metric independently. Prior framing
   ('EDHREC is sanity check only; goal is finding hidden gems from
   mechanics') remains the project-wide default."
6. Update `docs/RULE_HISTORY.md`: single entry documenting the
   change.
7. Update `CLAUDE.md` Algorithm section if the IDF formula is
   described there.
8. Commit: single conventional commit message: `feat(scoring): BM25
   IDF reformulation (probe shipped)`. The atomic re-pin is the both
   fixtures landing in this single commit.
9. **Operational housekeeping (separate from the commit):** Delete
   `.audit/walker_outcomes.csv` (gitignored, append-only; will
   regenerate on next walker run). Document in commit message that
   the historical baseline was rebuilt due to IDF transition; prior
   walker REJECT verdicts may not apply to BM25-era proposals.

**DECLINE branch (~10 min):**
1. Reset to `pre-bm25-baseline` tag (created in Unit 5 prerequisite):
   `git reset --hard pre-bm25-baseline` (on a feature branch only —
   never on main). This cleanly reverts Units 2-4 changes including
   formula, weights, and tests in one step. No conflict resolution
   needed.
2. **Preserve Unit 1**: Cherry-pick or rebase Unit 1's commit on top
   of `pre-bm25-baseline`. Unit 1 (per-commander NDCG reporting) is
   general-purpose audit infrastructure that survives the BM25
   decline.
3. Run full test suite to verify the resulting state is clean.
4. Run `bench.py audit --expect-identity` to verify production scoring
   matches pre-change state.
5. Append dated note to `memory/feedback_edhrec_not_goal.md`:
   "2026-05-04: BM25 IDF reformulation brainstorm proposed reversing
   this framing **for BM25-related work only**; the work declined
   due to [exact reason from Unit 4 verdict]; project-wide framing
   remains unchanged (gem-rate-primary)."
6. Write `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`
   following the `infrastructure-without-scoring-activation-2026-04-24.md`
   template. (Optional: if the only takeaway is "BM25 didn't move
   the needle," the memory note + commit message may be sufficient
   institutional record. Implementer judgment.)
7. Commit: `docs(scoring): BM25 IDF probe declined — null result`.

**INVESTIGATE branch (~10 min):**
1. Reset to `pre-bm25-baseline` tag (same as DECLINE step 1).
2. Cherry-pick Unit 1 (same as DECLINE step 2).
3. Run full test suite + `bench.py audit --expect-identity` (same as
   DECLINE).
4. Append dated note to `memory/feedback_edhrec_not_goal.md`:
   "2026-05-04: BM25 IDF reformulation showed marginal result
   (+0.005 to +0.010 NDCG@30) below ship bar **for BM25-related
   work only**; investigated, did not ship; pinned fixture
   unchanged so the +0.005 lift does NOT carry forward as pre-paid
   progress for the next IDF-variant attempt."
5. Write `docs/solutions/best-practices/bm25-idf-marginal-result-2026-05-04.md`
   noting precise deltas and recommending the next IDF variant for a
   fresh brainstorm.
6. Commit: `docs(scoring): BM25 IDF probe — marginal, not shipped`.

**INCONCLUSIVE branch (~10 min, NEW per plan-review pass 1):**
Fires when seed-vary precision check (Unit 4 step 1) returns spread
> 0.010 — result is too noisy to interpret regardless of point estimate.
1. Reset to `pre-bm25-baseline` tag (same as DECLINE step 1).
2. Cherry-pick Unit 1 (same as DECLINE step 2).
3. Run full test suite + `bench.py audit --expect-identity`.
4. Append dated note to `memory/feedback_edhrec_not_goal.md`:
   "2026-05-04: BM25 IDF reformulation produced inconclusive result
   (seed-spread > 0.010, point estimate [exact value]); precision
   too low to disambiguate; pinned fixture unchanged. **For BM25
   work only**, did not ship pending follow-on plan with more seeds
   or larger fixture."
5. Write `docs/solutions/best-practices/bm25-idf-inconclusive-2026-05-04.md`
   documenting the seed-spread, point-estimate, and what would be
   needed to disambiguate (e.g., "a follow-on plan that runs 10+
   seeds or scales the fixture to 1000-cmdr").
6. Commit: `docs(scoring): BM25 IDF probe — inconclusive, not shipped`.

**INVESTIGATE-FOR-RETUNE branch (~10 min, NEW per plan-review pass 1):**
Fires when Q1 fallback fired (Unit 3 reset multipliers to 1.0) AND
Unit 4 numerics would otherwise route to SHIP. Production scoring
cannot ship under reset multipliers; explicit "needs follow-on
retune" outcome instead of silent SHIP-with-bad-config.
1. Reset to `pre-bm25-baseline` tag (same as DECLINE step 1).
2. Cherry-pick Unit 1 (same as DECLINE step 2).
3. Run full test suite + `bench.py audit --expect-identity`.
4. Append dated note to `memory/feedback_edhrec_not_goal.md`:
   "2026-05-04: BM25 IDF reformulation showed promising numerics
   (+[exact NDCG] / [exact gem]) but optimizer self-test failed
   under BM25 (multipliers were reset to 1.0 as fallback); cannot
   ship untuned weights as production scoring. **For BM25 work
   only**, did not ship pending follow-on optimizer-tuning plan."
5. Write `docs/solutions/best-practices/bm25-idf-fallback-retune-needed-2026-05-04.md`
   documenting that Q1 fallback fired, the numerics that would have
   routed to SHIP, and the recommended follow-on plan
   ("optimizer-tuning under BM25 IDF, then re-evaluate ship gate").
6. Commit: `docs(scoring): BM25 IDF probe — fallback fired, retune
   needed`.

**Patterns to follow:**
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md` —
  null-result writeup template.
- `CLAUDE.md` Common Commands — re-pin invocation.

**Test scenarios:**
- Test expectation: none — this unit is one-shot operational work.
  Verification is by audit identity check (DECLINE/INVESTIGATE
  branches) or successful re-pin (SHIP branch).

**Verification:**
- SHIP: `bench.py audit --expect-identity` passes against the
  newly-pinned fixtures; `tests/` passes; commit lands cleanly.
  `gap_report.py` runs and any Stage A verdict shifts are documented
  in commit message. Atomic re-pin verification (Step 0) completed
  on scratch branch.
- DECLINE: `bench.py audit --expect-identity` passes against the
  unchanged pre-existing pin (post-reset to `pre-bm25-baseline`);
  `tests/` passes; Unit 1 cherry-picked and committed; null-result
  doc written (or memory note + commit message judged sufficient);
  memory updated with scoped reframe note.
- INVESTIGATE: same as DECLINE plus marginal-result doc written;
  memory updated; pinned fixture confirmed unchanged.
- INCONCLUSIVE: same as INVESTIGATE plus inconclusive doc documents
  seed-spread + disambiguation requirements.
- INVESTIGATE-FOR-RETUNE: same as INVESTIGATE plus fallback-retune
  doc documents the numerics that would have routed to SHIP and the
  follow-on optimizer-tuning task needed.

**Across all branches:** `pre-bm25-baseline` git tag must exist
(created in Unit 5 prerequisite). Unit 1 commits separately from
Unit 2 (per "Unit 1 is bundled but separable" in Key Technical
Decisions).

## System-Wide Impact

- **Interaction graph:** BM25 IDF change touches every scoring path
  that flows through `_compute_idf_basis`. That includes
  `score_all_universal`, `engine.SynergyEngine.page`, the audit
  harness, the optimizer, the embedding-contribution path (when
  flag-on, currently off), and the `--explain` renderer. None of
  these are mutated by Units 1-2 individually; the formula change
  propagates through them. Unit 3's optimizer re-tune is what
  exercises the propagation.
- **Error propagation:** No new error paths. BM25 formula is total
  (defined for all valid (df, N)). Self-test failure in Unit 3 is
  handled by fallback to option (a). Audit identity failure post-pin
  in Unit 5 SHIP branch indicates a re-pin bug, not a feature failure;
  block the commit.
- **State lifecycle risks:** `_RULE_QUALITY_MULTIPLIER` calibration
  is the central state risk. Unit 3's re-tune (or fallback reset)
  produces a new calibration set; if the optimizer self-test fails
  silently or produces a runaway proposal (clamp_max=5.0 cells), Unit
  3's "looks reasonable" gate must catch it before writing to
  `scoring_weights.json`. Walker_outcomes.csv rebuild risks losing
  historical preflight data; mitigated by gitignored append-only
  semantics — re-builds naturally on next walker run.
- **API surface parity:** No changes to `engine.SynergyEngine.page`
  contract. No changes to CLI surfaces other than the new
  `--per-commander-ndcg` flag. No changes to embedding contribution,
  Stage A pre-flight, or recommend.py user-facing surfaces.
- **Integration coverage:** Unit 4's audit is the integration test —
  it exercises the full scoring path under BM25 + new multipliers
  against the 500-cmdr fixture. Cross-layer scenarios (formula
  change + multiplier re-tune + per-commander reporting) are all
  exercised together.
- **Unchanged invariants:**
  - Anti-Goals: deterministic, no EDHREC at inference, rule-based,
    Forge DSL based, no learned weights at inference. BM25 IDF
    preserves all of these.
  - Stage A pre-flight (PR #39) library shape: pure functions,
    severity tiers, `walker_outcomes.csv` schema. Stage A code
    unchanged; only its baseline shifts (handled by atomic re-pin).
  - `bench.py audit --expect-identity` semantics unchanged; the gate
    correctly detects the BM25 swap as a score-drift event and
    requires re-pin.
  - `complement_rules/` rule logic unchanged. The 62-rule catalogue
    fires the same on the same inputs; only the IDF weight on each
    firing changes.
  - `port_graph/` substrate unchanged. Declarative rules in
    `data/rules_seed.json` continue to work identically.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Optimizer self-test fails under BM25 across all 3 alternative seeds | Pre-defined fallback to Q1 option (a) (reset multipliers to 1.0). Comparison becomes scientific-only; SHIP outcome routes to a follow-on optimizer-tuning task rather than direct ship. Documented in Unit 3. |
| BM25 lifts aggregate but causes a single tail-commander regression >0.05 | Per-commander prerequisite (Unit 4) routes to DECLINE. Simple, no hybrid escape per origin doc. If tail regression turns out to be the dominant failure mode, follow-on brainstorm reconsiders. |
| Re-pinning the 100-cmdr fixture shifts Stage A verdicts unpredictably | Unit 5 SHIP branch survey: capture pre-/post-pin gap_report distribution; document shifts in commit message. Walker_outcomes.csv rebuild prevents stale REJECT inheritance. |
| BM25 N-source: strict N requires new plumbing into `_compute_idf_basis` | Locked to strict N (see Unit 2 Approach). Implementer adds the plumbing — pass total candidate count or compute it from candidates collection in scope. Approximate-N option rejected (different formula; conflicts with no-per-commander-rules anti-goal). |
| Test coverage drift: existing tests assert specific IDF values | Unit 2 enumerates affected tests in advance; updates them in the same commit as the formula change. Test expectations must move atomically with the formula change. |
| Atomic re-pin lands inconsistently (e.g., 500 re-pinned but commit fails before 100 re-pin) | Single commit with both fixtures + walker_outcomes rebuild + memory update. If anything fails mid-flow, revert the partial state via git. |
| BM25 IDF scale shift breaks staple_bonus / anti-synergy / embedding scale calibration | Per-commander gate (>-0.05 → DECLINE) catches catastrophic per-commander impact. Subtle systematic skew not caught by per-commander bound is documented as a deferred risk; future audit gate work could add per-archetype cohort analysis if warranted. |
| Performance regression from BM25's slightly more expensive formula | Negligible per-call (one extra arithmetic op). IDF-basis cache pattern unchanged. Sanity-check with `bench.py audit` wall-time in Unit 4. |

## Documentation / Operational Notes

- `docs/RULE_HISTORY.md` gets a single entry on SHIP outcome.
- `CLAUDE.md` Algorithm section may need update on SHIP outcome
  (verify in Unit 5).
- `docs/solutions/best-practices/` gets a new entry on DECLINE or
  INVESTIGATE outcome (template:
  `infrastructure-without-scoring-activation-2026-04-24.md`).
- `memory/feedback_edhrec_not_goal.md` gets a dated note on EVERY
  outcome (SHIP, INVESTIGATE, or DECLINE) — this is mandatory per
  origin doc Product Decision section.
- No external-facing documentation changes (no public API change).
- No rollout plan needed — this is an audit-gated formula swap; the
  re-pin IS the rollout.
- Pre-commit hooks remain on the 100-cmdr canonical fixture; SHIP
  outcome's atomic re-pin keeps them aligned.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-04-bm25-idf-requirements.md](../brainstorms/2026-05-04-bm25-idf-requirements.md)
- **Saturation diagnosis:** `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
- **Embedding null result (template for our DECLINE/INVESTIGATE writeup):** `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
- **Fixture sizing rationale:** `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`
- **PR #39 (Stage A pre-flight, just shipped):** affects re-pin atomicity in Unit 5 SHIP branch
- **Current IDF formula:** `src/mtg_synergy_graph/universal_scorer.py:702` (`_compute_idf_basis`)
- **Optimizer + per_ndcg pattern source:** `src/mtg_synergy_graph/bench/optimize.py:630-666`
- **Audit handlers + --inspect pattern:** `src/mtg_synergy_graph/bench/handlers.py:333`
- **expect-identity gate semantics:** `src/mtg_synergy_graph/bench/handlers.py:122-125`
- **Project memory (NDCG-not-goal context):** `memory/feedback_edhrec_not_goal.md`
- **Project architecture overview:** `CLAUDE.md` Scoring Architecture section
