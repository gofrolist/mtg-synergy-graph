---
title: "feat: Color-identity-conditioned IDF denominator (probe)"
type: feat
status: active
date: 2026-07-02
origin: docs/brainstorms/2026-07-02-color-conditioned-idf-requirements.md
---

# feat: Color-identity-conditioned IDF denominator (probe)

## Overview

Flag-gated probe: count IDF's `N` over the commander's
color-identity-legal pool instead of the color-unfiltered matched
set, inside `_compute_idf_basis`. Evaluated under the pre-committed
SHIP / INVESTIGATE / INCONCLUSIVE / DECLINE protocol on the
regenerated 500-cmdr fixture, with a per-commander cliff gate and a
calibration re-sweep before any DECLINE. This is a **probe, not a
feature** — a clean DECLINE with a captured null-result doc is a
successful outcome (see origin doc).

## Problem Frame

The ranking pool is color-filtered after scoring
(`src/mtg_synergy_graph/engine.py::page()`), but IDF weights count
matched candidates across all colors — a mono-white commander's
"sacrifice cost" weight is diluted by black sac outlets it can never
play. Forensics (PR #75, 2026-07-02 run) puts OUTRANKED at 46.2% of
misses; the 2026-07-02 kill-test confirmed conditioning is NOT a
uniform rescale (per-key inflation spread 1.2–1.9× for mono/2c/3c;
5-color no-op; colorless flagged as acute cliff risk with 75% of
keys at n≤3). Full framing, counter-hypothesis, and evidence: see
origin document.

## Requirements Trace

From `docs/brainstorms/2026-07-02-color-conditioned-idf-requirements.md`:

- R1 (conjoined legality predicate, identity union over full
  `commander_set`) → Unit 2
- R2/R2a (denominator-only; orphaned keys keep global-N weight) → Unit 2
- R3/R4 (flat rules untouched; `cond_mult` + panharmonicon floor
  compose on conditioned N) → Unit 2
- R5 (deterministic, EDHREC-free at inference) → Unit 2 (pool derives
  from `cards` metadata only)
- R6 (four outcomes with triggers) → Units 4, 6
- R7 (cliff gate < −0.05 per commander on 500-cmdr) → Units 3, 4
- R8 (machine-emitted aggregate = mean of per-commander deltas) → Unit 3
- R8a (one `--optimize` re-sweep before DECLINE) → Unit 5
- R9 (audit-gated; SHIP re-pins) → Units 4, 6
- R10 (null-result doc on DECLINE, incl. counter-hypothesis
  verdict) → Unit 6
- R11 (500-cmdr fixture regenerated on baseline code FIRST) → Unit 1
- R12 (per-commander histogram sliced by identity size) → Unit 3
- R13 (flag registered in `ScoringConfigInputs` → hash flip) → Unit 4

## Scope Boundaries

- No pool restriction of complement search or scoring (denominator
  only). No λ-blend in v1 (pre-identified fallback, scoped to small
  pools, if colorless-driven cliffs fire). No IDF curve-shape
  changes. No changes to flat weights, staple bonus, anti-synergy,
  or the sort key. (See origin doc for rationale.)

### Deferred to Separate Tasks

- Pool restriction of complement search (coherence/perf follow-up
  if SHIP) — future plan.
- FR6 gem-primary metric escalation — its own brainstorm, triggered
  only by the INVESTIGATE(gem-dominant) outcome.
- `edhrec_rank` sort-tiebreaker removal (proven +0.000000) —
  separate trivial PR, do not couple to this probe's audit.

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/universal_scorer.py` — `_compute_idf_basis`
  (line ~711; the `1/log2(1+n)` site is ~740), `IdfBasis` docstring
  cache contract (~692), `score_from_complements` IDF call site
  (~858), staple-pip lookup (~909, `commander_set[0]`-only — NOT the
  pattern to copy), `ScoringConfigInputs` (~243) +
  `get_scoring_config_inputs()` (~322).
- `src/mtg_synergy_graph/penalties.py` — `CandidateCache` (~184),
  `candidate_rows` carries `name, color_identity, card_types,
  legal_commander` (+ back-compat `1 AS legal_commander` shim) —
  the legal pool derives from this cache with no new SQL on the
  cached path.
- `src/mtg_synergy_graph/engine.py` — `page()` legality filter
  (~364–383) and `_color_identity_union` (~117): the predicate and
  union semantics to replicate.
- `src/mtg_synergy_graph/bench/per_commander_ndcg.py` — cliff-gate
  report (`--per-commander-ndcg`, threshold −0.05 at ~38; pinned
  NDCG recomputed from `FixtureEntry.scores`, live from a fresh
  re-score).
- `src/mtg_synergy_graph/bench/optimize.py` — `idf_basis_cache`
  keyed by commander name (~1024), fill site `_score_split` (~646)
  must pass the same pool; `candidate_cache` already in scope
  (~1008).
- Flag pattern: `_ENABLE_EMBEDDING_CONTRIBUTION` in
  `src/mtg_synergy_graph/embeddings/contribution.py` (default-off
  module constant, mock-patch idiom, flip procedure in docstring).
- Sibling plan to mirror:
  `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md` (probe
  framing, baseline tag, outcome handling, optimizer fallback chain).

### Institutional Learnings

- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md`
  — flag defaults `False`; do NOT add the `ScoringConfigInputs`
  field until the flip-and-audit unit (adding early forces a
  premature re-pin); prove flag-off bitwise identity with
  `--expect-identity`; SHIP sequence = flip → `--repin --yes` →
  `--expect-identity` PASS → commit.
- `docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md`
  — flag-off audits can't catch a broken flag-on path; an
  end-to-end flag-ON test must exist before trusting flag-ON
  numbers.
- `docs/solutions/best-practices/optimizer-perf-profile-2026-04-30.md`
  — `--optimize` ≈ 3 min / 100 cmdr (≈ 15 min on 500); expect exit
  2 (stale tensor) after the hash flips until re-pin; use
  `--proposal-path` / history redirects to avoid polluting
  `.audit/`; optimizer's fast path replays the persisted
  tensor/basis — re-sweep is only valid AFTER re-pin under the
  conditioned config.
- `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md`
  — the sibling DECLINE; cliff clusters were tribal/graveyard; a
  gem-positive result was discarded under NDCG-primary framing
  (this probe's INVESTIGATE route exists because of that).
- `docs/solutions/best-practices/rule-consolidation-null-result-2026-04-24.md`
  — re-run `--collinearity` after IDF weight shifts (explicit
  trigger listed there).
- `docs/solutions/best-practices/ppmi-smoothing-on-sparse-vocabulary-2026-04-24.md`
  — sparse-count log-ratios degenerate on small pools; sanity-check
  IDF weight distributions per identity class before trusting
  aggregates.

## Key Technical Decisions

- **Flag-gated, default off** (`_ENABLE_COLOR_CONDITIONED_IDF =
  False` module constant in `universal_scorer.py`): unlike BM25's
  direct replacement, the flag gives an in-tree ablation lever and
  follows the embeddings precedent; config-hash registration is
  deferred to Unit 4 per the flag-gating learnings doc.
- **Legal pool from `CandidateCache`** on the cached path (columns
  already loaded); single SQL fallback replicating the `page()`
  predicate when `candidate_cache is None`. No new tables, no new
  queries on the hot path.
- **`_compute_idf_basis(complements, legal_pool=None)`** — pure
  function with an optional `frozenset[str]`; `None` reproduces
  today's behavior exactly (bitwise flag-off identity). Call-site
  seam (verified): the scorer's no-basis branch at
  `universal_scorer.py:858` routes through `_compute_idf_weights`,
  so restructure that else-branch to
  `_idf_weights_from_basis(_compute_idf_basis(complements,
  legal_pool=pool))`, deriving the pool ONLY when `idf_basis is
  None` — when the optimizer supplies a cached basis, the pool is
  already baked in at the fill site (`optimize.py` ~646). The public
  `_compute_idf_weights` wrapper stays pool-unaware (its other
  callers are tests exercising unconditioned behavior).
- **Flag-read idiom across modules**: `optimize.py` must read the
  flag at call time via module-attribute access
  (`universal_scorer._ENABLE_COLOR_CONDITIONED_IDF`) or a
  function-local import — never a top-level `from universal_scorer
  import _ENABLE_...`, which snapshots at import time and makes
  mock-patch flip one call site but not the other (the embeddings
  precedent uses a function-local import for exactly this reason,
  `universal_scorer.py` ~873).
- **Orphaned keys (in-pool N = 0) keep global-N weight** (origin
  R2a): no div-by-zero, no weight-1.0 fallback inflation, ranking
  unaffected by construction.
- **Baseline git tag before the scoring change** (`pre-color-idf`,
  mirroring `pre-bm25-baseline`): DECLINE recovery = reset to tag
  (Unit 1 is an ancestor and survives automatically) + cherry-pick
  Unit 3's commit(s).

## Open Questions

### Resolved During Planning

- Pool source: `CandidateCache.candidate_rows` (verified it carries
  the needed columns) with SQL fallback — no new plumbing layers.
- Basis signature: optional parameter on `_compute_idf_basis`
  (preserves the optimizer's cached-basis fidelity invariant,
  `tests/bench/test_optimize.py:554–584` must stay green).
- Optimizer cache key: commander-name key stays valid (pool is
  commander-deterministic); only the fill site changes.
- Commander pips for the pool: fetch `color_identity` for ALL names
  in `commander_set` and union (R1); hoist ahead of the IDF call —
  the existing staple lookup at ~909 stays untouched (it feeds
  STAPLES, not the pool).

### Deferred to Implementation

- Exact helper name/location for the pool derivation (scorer-local
  `_color_legal_pool(...)` vs reuse of engine logic) — pick at
  implementation; the predicate semantics are fixed by R1.
- Whether the identity-size slice table in the report needs its own
  flag or always renders — decide by output noise on the 100-cmdr
  default.
- Uncertainty estimate for the +0.010 gate — bootstrap SEM / CI
  over the 500 per-commander deltas during Unit 4 (scoring is
  deterministic, so identical-run jitter is 0; the relevant noise
  is sampling variance across commanders — BM25's "3-seed" analog
  varied optimizer splits, a different noise source).

## High-Level Technical Design

> *This illustrates the intended approach and is directional
> guidance for review, not implementation specification.*

```
score_from_complements(conn, commander_set, complements, cache,
                       idf_basis=None, ...):
    if idf_basis is not None:
        idf = _idf_weights_from_basis(idf_basis)  # optimizer path: pool
            # already baked into the cached basis at its fill site
    else:
        pool = None
        if _ENABLE_COLOR_CONDITIONED_IDF:
            pool = color_legal_pool(conn, commander_set, cache)
                # identity union over ALL commanders in the set,
                # predicate: pips ⊆ union ∧ types ok ∧ legal_commander≠0
        idf = _idf_weights_from_basis(
            _compute_idf_basis(complements, legal_pool=pool))

_compute_idf_basis(complements, legal_pool=None):
    for key, cands in freq.items():
        n_all = len(cands)
        n = n_all
        if legal_pool is not None:
            n_legal = len(cands & legal_pool)
            n = n_legal if n_legal > 0 else n_all   # R2a orphan policy
        # flat-rule bypass, cond_mult, panharmonicon max(n,30)
        # all unchanged, applied to the (possibly conditioned) n
```

Decision matrix (flag × pool availability):

| flag | candidate_cache | behavior |
|------|-----------------|----------|
| off  | any             | today's path, bitwise identical |
| on   | present         | pool from cache rows, no SQL |
| on   | None            | pool via one SQL query (page() predicate) |

## Implementation Units

- [ ] **Unit 1: Baseline commit + 500-cmdr fixture regeneration (R11)**

**Goal:** Clean baseline state: land the outstanding post-refresh
housekeeping, regenerate the stale 500-cmdr fixture on baseline
scoring code, and tag the baseline.

**Requirements:** R11

**Dependencies:** None

**Files:**
- Modify: `tests/fixtures/golden_set_run_500.json` (regenerated)
- Commit (already staged in working tree): `tests/fixtures/golden_set_run.json`,
  `docs/ideation/2026-06-10-synergy-accuracy-ideation.md`,
  `docs/brainstorms/2026-07-02-color-conditioned-idf-requirements.md`,
  this plan
- Tag: `pre-color-idf` after the baseline commit(s)

**Approach:**
- Commit the uncommitted 2026-07-02 re-pin + docs first (baseline
  integrity: the 100-cmdr pin and the tensor must correspond to
  baseline code before any probe diff exists).
- Regenerate via `scripts/bootstrap_golden_set_500.py` (~1–2 min);
  confirm the canonical 100 pin still passes `--expect-identity`
  so the regeneration provably ran on baseline scoring.
- The pre-commit advisory bench hook fires on fixture-adjacent
  edits — expected, advisory only.

**Test scenarios:**
- Test expectation: none — data regeneration + commits; correctness
  is proven by `--expect-identity` PASS and by the per-commander
  report showing ~0 deltas against the fresh fixture.

**Verification:**
- `bench.py audit --expect-identity` passes on the canonical pin.
- `bench.py audit --per-commander-ndcg --fixture
  tests/fixtures/golden_set_run_500.json` shows all deltas ≈ 0
  (live == freshly pinned baseline; checked via the worst-first
  sorted rows until Unit 3's aggregate line lands — note this
  primarily verifies cross-instrument consistency; the real
  baseline prover is the `--expect-identity` PASS above).
- `pre-color-idf` tag exists on the baseline commit.

- [ ] **Unit 2: Conditioned `_compute_idf_basis` + legal-pool derivation (R1–R5)**

**Goal:** The scoring change itself, flag-gated off, bitwise-inert
until flipped.

**Requirements:** R1, R2, R2a, R3, R4, R5

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py`
- Modify: `src/mtg_synergy_graph/bench/optimize.py` (basis fill
  site ~646 passes the pool under the same flag)
- Test: `tests/test_universal_scorer_color_idf.py` (new)

**Approach:**
- `_ENABLE_COLOR_CONDITIONED_IDF: bool = False` module constant,
  read at call time (mock-patch idiom), docstring documents the
  flip procedure — mirror `embeddings/contribution.py`. Do NOT
  register in `ScoringConfigInputs` yet (Unit 4).
- Pool derivation helper per Key Technical Decisions: identity
  union over the full `commander_set` (R1 — explicitly not the
  `commander_set[0]` staple-lookup pattern), predicate conjunction
  matching `page()`, cache-first with SQL fallback, returns
  `frozenset[str]`.
- `_compute_idf_basis` gains optional `legal_pool`; conditioned
  `n = |cands ∩ pool|` with R2a orphan fallback to `n_all`;
  flat-rule bypass, `cond_mult`, panharmonicon floor untouched in
  structure, floor applied to conditioned n (R4).
- Update the `IdfBasis` docstring cache-invalidation contract (it
  now also depends on the pool when provided).

**Execution note:** Test-first — hand-computed (n_all, n_legal) →
weight pairs, mirroring the BM25 plan's Unit 2 style.

**Patterns to follow:**
- `tests/test_universal_scorer_coverage.py` IDF section (in-memory
  `PortComplement` lists, no DB) and
  `tests/bench/test_universal_scorer_identity.py` (`open_db(tmp_path
  / "synergy.db")` — never literal `data/*.db`, enforced by
  conftest).

**Test scenarios:**
- Happy path: key with cands {A,B,C,D}, pool {A,B} → n=2, weight
  `1/log2(3)`; same key unconditioned → n=4.
- Happy path: flag off / `legal_pool=None` → output equals current
  `_compute_idf_weights` exactly (byte-identical dict).
- Edge case (R2a): key whose cands ∩ pool = ∅ → weight uses n_all;
  no ZeroDivisionError; key still present in `base_idf_non_flat`.
- Edge case: flat rule key with pool present → still routed through
  `flat_weights`, value unchanged.
- Edge case: `:cond` filter_group with pool → conditioned n THEN
  ×0.5 cond_mult (composition order preserved).
- Edge case: panharmonicon key with in-pool n=5 → floor lifts to 30
  (floor applies to conditioned n).
- Happy path (pool derivation): two-commander partner set with W
  and B identities → pool admits WB candidates (union, not first
  commander only).
- Edge case (pool derivation): colorless commander → pool contains
  only colorless-identity cards; `legal_commander=0` and
  NON_EDH_CARD_TYPES rows excluded.
- Integration: `score_all_universal` on a tmp DB with the flag
  mock-patched ON produces different (and deterministic) scores vs
  OFF for an in-color candidate whose key has out-of-color
  co-matchers — the end-to-end flag-ON test required by the
  verify-from-stored-config learning.
- Integration: optimizer fidelity invariant
  (`tests/bench/test_optimize.py:554–584`) stays green with the new
  signature.
- Integration (cross-module flag parity): a single mock-patch of
  the flag on `universal_scorer` makes BOTH the scorer path and the
  optimizer `_score_split` path produce conditioned bases — guards
  against the import-time-snapshot trap.
- Integration (R2a invariant): on a tmp fixture DB, the pool
  helper's output is a superset of (ideally equal to) the legal set
  `engine.page()` computes for the same commander, on BOTH the
  cache path and the SQL-fallback path — converts the "orphaned
  keys can't affect the ranking" claim from assertion to enforced
  invariant.

**Verification:**
- Full test suite green with flag off.
- `bench.py audit --expect-identity` PASSES with flag off (bitwise
  inertness proven).

- [ ] **Unit 3: Report plumbing — aggregate line + identity-size slices (R8, R12)**

**Goal:** Machine-emitted gate numbers: the SHIP aggregate and the
per-identity-class delta view, so Unit 4's verdict reads off a
report instead of hand-averaging 500 rows.

**Requirements:** R7 (report is the gate instrument), R8, R12

**Dependencies:** Unit 1 for verification only — implementation may
proceed in parallel with Unit 2 (survives any outcome, like BM25's
Unit 1)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/per_commander_ndcg.py`
- Test: `tests/bench/test_per_commander_ndcg.py` (extend existing;
  create if absent)

**Approach:**
- Append an aggregate summary block: mean pinned, mean live, mean
  delta, violation count vs `PER_COMMANDER_REGRESSION_THRESHOLD`.
- Add an identity-size slice table (colorless / mono / 2 / 3 / 4 /
  5-color): per-class mean delta + worst delta + violation count.
  Identity class from `cards.color_identity` pip count of the
  fixture commander. (`FixtureEntry.commander` is a single name —
  partner entries cannot occur under the current schema; partner
  union semantics live in Unit 2's pool derivation, where they are
  real.)
- Keep exit-code semantics unchanged (report, human-enforced gate —
  consistent with existing behavior; the R6 verdict is applied in
  Unit 4/6, not by exit code).

**Test scenarios:**
- Happy path: fixture of 3 synthetic commanders with known scores →
  aggregate line equals hand-computed mean of the three deltas.
- Happy path: commanders with W, WU, and empty (colorless)
  identities land in mono / 2-color / colorless slices respectively.
- Edge case: zero-label commander (NDCG 0 both sides) → included at
  delta 0, does not crash the slice table.
- Edge case: fixture commander missing from the `cards` table
  (post-refresh rename) → lands in an explicit "unknown" slice or
  raises clearly — never silently miscounted.
- Error path: fixture missing → existing usage-error behavior (exit
  2) unchanged.

**Verification:**
- Running the report against the Unit 1 fresh fixture prints
  aggregate ≈ 0 and slice rows for every identity class present in
  the 500 set (incl. 15 colorless).

- [ ] **Unit 4: Flip, register config hash, audit, gate evaluation (R6, R7, R13)**

**Goal:** Flip the flag on, make the config hash reflect it, produce
the full evidence package, and evaluate the R6 gates.

**Requirements:** R6, R7, R9, R13, R12 (evidence)

**Dependencies:** Units 1–3

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` (flag → True;
  `enable_color_conditioned_idf` field added to `ScoringConfigInputs`
  + `get_scoring_config_inputs()` with the standard comment citing
  this plan — the deferred R13 step, per the flag-gating learnings)
- Test: `tests/test_universal_scorer_color_idf.py` (extend —
  config-hash flip test, mirroring `tests/test_pathway_flag_gate.py`)
- Test: `tests/bench/test_optimize.py` (extend — exit-2 staleness
  assertion)

**Approach:**
- Registration order per learnings: field lands in the same unit as
  the flip, never earlier.
- Evidence package, in order (sequencing matters — the hash flip
  makes the persisted tensor stale, so tensor-reading steps come
  AFTER a scratch-pin tensor rebuild, and the committed baseline
  pins are never overwritten mid-unit):
  1. `bench.py audit` vs the committed 100-cmdr baseline pin
     (histogram verdict + gem rate) and
     `--per-commander-ndcg --fixture ...golden_set_run_500.json`
     (cliff gate + aggregate + identity slices) — these compare
     live-vs-pinned scores and do NOT need the tensor.
  2. Uncertainty estimate for the +0.010 gate: bootstrap SEM /
     confidence interval over the 500 per-commander deltas from
     step 1's report rows. (Scoring is bitwise-deterministic —
     `--expect-identity` exists because of it — so "two identical
     runs" would trivially read 0; the real uncertainty is sampling
     variance across commanders.)
  3. Scratch-pin tensor rebuild: copy the baseline 500 fixture to a
     scratch path and run `--repin --yes --fixture <scratch-path>`
     (verified: `handle_repin` honors `args.fixture`) — this
     persists the tensor at the conditioned hash while leaving
     `tests/fixtures/golden_set_run*.json` untouched.
  4. Tensor-reading diagnostics against the scratch pin:
     `--collinearity` (rule-consolidation learning; NOTE it returns
     a silently-empty report on a stale tensor — step 3 must
     precede it), IDF-distribution sanity per identity class
     (PPMI-smoothing learning; slice table + weight-histogram spot
     check for one mono and one colorless commander), and
     `--forensics` (OUTRANKED-share / counter-hypothesis read;
     hard-fails on a stale tensor — if it also validates the
     fixture's stored config hash, point it at the scratch pin).
- Evaluate R6: SHIP / DECLINE / INVESTIGATE(gem-dominant) /
  INCONCLUSIVE. Raw gate failure routes to Unit 5 before any final
  DECLINE.

**Test scenarios:**
- Happy path: `compute_config_hash` changes when the flag flips
  (unit test on `get_scoring_config_inputs` including the new
  field).
- Integration: with the field registered and flag on, `bench.py
  audit --optimize` against the stale tensor exits 2 (staleness
  detected, not silently wrong) — asserts the R13 wiring actually
  protects the optimizer path.

**Verification:**
- Evidence package complete and archived (`.audit/last.md`,
  per-commander report incl. slices, collinearity, forensics).
- An explicit R6 outcome is recorded with the numbers that
  triggered it.

- [ ] **Unit 5: Calibration re-sweep before DECLINE (R8a — conditional)**

**Goal:** If raw gates fail, distinguish "population axis is wrong"
from "surrounding constants were tuned for the old denominator."

**Requirements:** R8a

**Dependencies:** Unit 4 (only runs on raw gate failure)

**Files:**
- Modify: `src/mtg_synergy_graph/data/scoring_weights.json` (only if
  accepted proposal steps are applied)

**Approach:**
- Sequencing (from the optimizer learnings — the fast path replays
  the persisted tensor): the conditioned-hash tensor already exists
  from Unit 4 step 3's scratch pin; run `bench.py audit --optimize
  --fixture <scratch-path>` with `--proposal-path` redirected to a
  scratch location (~15 min). Committed baseline pins stay
  untouched until a SHIP verdict.
- BM25 fallback chain on self-test failure: seeds 7 → 17 → 137,
  then reset multipliers to 1.0; SHIP is forbidden under the
  fallback path.
- Apply accepted proposal steps manually (optimizer never
  auto-mutates), re-run the Unit 4 evidence package once, and take
  the R6 verdict as final.
- Applying weight edits flips the hash again → second re-pin before
  re-audit. Watch the sweep-writers learning if touching
  `scoring_weights.json` shape (values only — never structure).

**Test scenarios:**
- Test expectation: none — this unit runs existing instruments;
  correctness is carried by the optimizer's built-in self-test and
  the re-run evidence package.

**Verification:**
- Either: re-sweep improved the raw result and the final R6 verdict
  cites post-sweep numbers; or: the sweep's failure to rescue is
  recorded as part of the DECLINE evidence.

- [ ] **Unit 6: Outcome handling (R9, R10 + origin Success Criteria)**

**Goal:** Land exactly one outcome cleanly, with the repo left in a
coherent state whichever way the verdict went.

**Requirements:** R6, R9, R10

**Dependencies:** Unit 4 (and Unit 5 when triggered)

**Files (SHIP path):**
- Modify: `tests/fixtures/golden_set_run.json` +
  `tests/fixtures/golden_set_run_500.json` (atomic re-pin, one
  commit — BM25 Unit 5 pattern)
- Modify: `docs/RULE_HISTORY.md` (dated entry), `CLAUDE.md` (flag
  note in the scoring section)

**Files (DECLINE path):**
- Create: `docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-XX.md`
  (sibling of the BM25 null-result doc; must record the
  per-commander failure pattern, the identity-class slice view, the
  R8a re-sweep evidence, and whether the origin doc's named
  counter-hypothesis — displacer amplification — fired)
- Revert: reset to `pre-color-idf` tag (Unit 1 is an ancestor of
  the tag and survives automatically), cherry-pick Unit 3's
  commit(s) — the report infra survives any outcome

**Files (INVESTIGATE gem-dominant path):**
- Create: stub brainstorm
  `docs/brainstorms/2026-07-XX-gem-primary-metric-regime-requirements.md`
  seeded with the probe's numbers; probe branch parked (flag off,
  registered field retained), no re-pin.

**Approach:**
- SHIP sequence per the flag-gating learnings: flag on (already) →
  `--repin --yes` → `--expect-identity` PASS → single commit.
- Colorless-driven cliff DECLINE is class-specific per the origin
  doc's kill-test flag: record it as such and name the small-pool
  λ-blend as the pre-identified follow-up, not a full revert
  recommendation.

**Test scenarios:**
- Test expectation: none — process unit; artifacts verified by
  checklist below.

**Verification:**
- Exactly one R6 outcome recorded; working tree matches it (SHIP:
  re-pinned + docs updated; DECLINE: tag-reset + null-result doc
  committed + Units 1/3 preserved; INVESTIGATE: stub brainstorm
  exists, flag off).
- `.audit/history.csv` and `.audit/forensics_history.csv` show the
  probe's config-hash boundary (R13 working as intended).

## System-Wide Impact

- **Interaction graph:** IDF weights feed `score_from_complements`,
  the tensor sink (`_emit_tensor_rows`), the optimizer's cached
  basis, and `--explain` output. Flag-off inertness protects all of
  them until Unit 4.
- **Error propagation:** pool derivation must raise on missing
  commander rows (consistent with `page()`'s unknown-commander
  ValueError), never silently return an empty pool — an empty pool
  would orphan every key and silently reproduce unconditioned
  behavior (masking the probe).
- **State lifecycle risks:** the config-hash flip (Unit 4)
  invalidates the persisted tensor — expected and desired (R13);
  optimizer exits 2 until re-pin. No other persistent state.
- **API surface parity:** `score_one` on out-of-color cards shifts
  numerically where shared keys are conditioned (documented in
  origin R2); `--explain` IDF values change under the flag —
  acceptable, explanation text format unchanged.
- **Integration coverage:** the flag-ON end-to-end scoring test in
  Unit 2 is the mock-proof integration point; Unit 4's exit-2 test
  covers the tensor-staleness contract.
- **Unchanged invariants:** flat weights, staple bonus,
  anti-synergy slot, sort key, complement search, `page()`
  filtering, declarative-rule routing — all untouched (origin scope
  boundaries).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Colorless commanders cliff (75% of keys at n≤3, kill-test) | R7 gate observes them (15 in fixture); class-specific DECLINE reading + λ-blend fallback pre-identified (origin doc) |
| DECLINE measures stale calibration, not the axis | Unit 5 re-sweep pre-committed (R8a), correctly sequenced after re-pin |
| Flag-off path accidentally perturbed | Unit 2 bitwise-identity test + `--expect-identity` before flip |
| Fixture regenerated with probe active → vacuous cliff gate | Unit 1 runs on baseline commit, verified via `--expect-identity`, tagged |
| Optimizer sweeps old geometry | Re-pin before `--optimize` (learnings); exit-2 test in Unit 4 |
| +0.010 SHIP gate vs sampling variance | Bootstrap SEM / CI over the 500 per-commander deltas recorded before verdict (scoring is deterministic — run-to-run jitter is 0 by construction) |
| Stale tensor makes collinearity/forensics vacuous or failing mid-Unit-4 | Scratch-pin tensor rebuild (step 3) before any tensor-reading diagnostic; committed pins never overwritten pre-verdict |
| Hidden collinearity revealed by weight shifts | `--collinearity` in the Unit 4 evidence package |

## Documentation / Operational Notes

- `docs/RULE_HISTORY.md` entry on SHIP; null-result solution doc on
  DECLINE (R10); CLAUDE.md scoring-section note only on SHIP.
- Update the ideation doc
  (`docs/ideation/2026-06-10-synergy-accuracy-ideation.md`) idea #2
  status with the outcome, whichever it is.

## Sources & References

- **Origin document:** `docs/brainstorms/2026-07-02-color-conditioned-idf-requirements.md`
- Sibling plan: `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md`
- Forensics instrument: PR #75 (`bench.py audit --forensics`)
- Kill-test results: origin doc, Resolve Before Planning (RESOLVED)
- Related code: `src/mtg_synergy_graph/universal_scorer.py`,
  `src/mtg_synergy_graph/penalties.py`,
  `src/mtg_synergy_graph/bench/per_commander_ndcg.py`,
  `src/mtg_synergy_graph/bench/optimize.py`
