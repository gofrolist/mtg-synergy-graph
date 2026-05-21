---
last_updated: 2026-05-04
type: requirements
title: BM25 IDF reformulation for NDCG@30 lift
tags:
  - scoring
  - idf
  - universal-scorer
  - audit-gated
  - 500-cmdr-fixture
status: open
plan_ref: TBD (ce-plan to follow)
session_ref: 2026-05-04 brainstorm — orthogonal-directions to lift recommendation model
priors:
  - docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md
  - docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md
  - docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md
---

# BM25 IDF reformulation for NDCG@30 lift

## Problem Frame

Three direct improvement levers for NDCG@30 / hidden_gem_hit_rate were
proven exhausted in a single session on 2026-05-04:

1. **Per-rule weight optimizer** — 4 sweeps with varied alpha/grid/seed
   on the 500-cmdr fixture all produced sub-noise held-subset NDCG deltas
   (+0.000175 to +0.000343), zero gem_rate movement.
2. **Embedding contribution flip** — re-sweep on 500-cmdr (10 cells) was
   uniformly negative (−0.0012 to −0.0101 vs flag-off). The prior
   30-cmdr +0.0067 was sampling noise.
3. **Walker rule-shipping with v1.0 Stage A pre-flight** — 0 attempted,
   0 shipped. Queue exhaustion exactly as predicted by
   `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`.

The 2026-04-24 saturation diagnosis explicitly named "orthogonal
directions": **better IDF weighting**, new commander-target composition
for embeddings, and richer port-signature features.

This document scopes the **first orthogonal direction probe** to attempt:
**reformulating the per-rule IDF in `src/mtg_synergy_graph/universal_scorer.py`
from the current `1/log2(1 + N)` to a BM25-style saturation IDF**.

**Framing as "probe" not "first improvement attempt":** BM25 was chosen
as the opening move because it is the cheapest of the 5 viable
orthogonal directions, not because EV analysis ranks it highest. A null
result from BM25 is informative (it tells us the BM25-style IDF curve
is not the lever) but does NOT exhaust the IDF axis as a whole. Four
alternative IDF formulations remain explicitly viable post-DECLINE
(smoothed-frequency, structural-overlap, per-rule-cluster, rank-aware).

**Why BM25 specifically (workload-fit honest disclosure):** Classical
BM25 IDF was developed for natural-language information retrieval where
(a) vocabulary is large (10k+ terms), (b) per-document term frequency
matters and is multiplied by IDF (the "TF" half of BM25), and (c) df
distributions follow heavy-tailed Zipfian patterns. This workload is
different: (a) ~tens of rule_id-keyed firings (small vocabulary), (b)
per-(rule, candidate) firing is essentially binary — there is no TF
analog, so we use BM25's IDF half ONLY, (c) df distributions are shaped
by mechanical port frequency and are likely less Zipfian than NL.

**This means:** stripping TF from BM25 and applying its IDF to a small
near-binary signal may behave very differently from how the IR
literature describes BM25's gains over log2-IDF. The plan's audit will
reveal whether the saturation curve change improves ranking for THIS
workload — not whether BM25 in general is "better." A DECLINE outcome
specifically rules out the BM25-IDF saturation curve, not the IDF axis.

**Why this probe first (not anti-synergy or port-signature features):**
- IDF reformulation is a single file change with audit-gated revert.
- The current IDF formula is the central scoring knob; if there's lift
  available without expanding what the system can model mechanically,
  IDF is where it lives.
- Anti-synergy rules and richer port-signature features both expand
  the system's mechanical surface area; they are higher-EV for the
  hidden_gem axis but require new templates, vocabulary bumps, or
  mental model shifts. They're explicitly in the queue (see Out of
  Scope), but BM25 is the right "is the cheap axis dead?" probe to
  resolve before committing to the more expensive directions.

If BM25 lifts NDCG@30 above the ship bar, the IDF saturation curve was
a real lever and weight-tuning has more headroom than the optimizer
sweeps suggested. If it doesn't, the cheap probe completed and the
next brainstorm picks from the higher-effort orthogonal directions
with sharper EV expectations.

## Explicit Product Decision: NDCG@30 as primary metric (for this work)

The owner explicitly chose NDCG@30 as the primary metric for BM25 IDF
work during the 2026-05-04 ce-brainstorm session. This contrasts with
`memory/feedback_edhrec_not_goal.md` ("EDHREC is sanity check only;
goal is finding hidden gems from mechanics"). This brainstorm
acknowledges the contrast openly:

- **What the reframe accepts:** NDCG@30 measures EDHREC top-30
  similarity by construction. Optimizing for it points the scoring
  system toward EDHREC convergence — opposite to the original "find
  gems EDHREC misses" positioning.
- **What the reframe expects in return:** A measurable, communicable
  optimization target that can be moved or proven unmovable in a
  single session.
- **Per-change ship bar is symmetric** (NDCG +0.010 / gem −0.010, see
  Success Criteria) so this work does not silently ratchet gem rate
  down.
- **Scope of the reframe:** This decision applies to BM25 IDF
  specifically. Future orthogonal-direction brainstorms must
  re-justify the primary metric (do not assume NDCG@30 carries
  forward).

**Program-level constraints are out of scope for this brainstorm.**
A cumulative gem-rate budget across the orthogonal-directions program
was considered and rejected as scope creep — that belongs in a
separate program-governance doc, not in a brainstorm for one work
item. If the owner wants to track cumulative erosion, that needs its
own decision artifact.

**Memory follow-through:** As part of the SHIP commit (if BM25 ships),
update `memory/feedback_edhrec_not_goal.md` with a dated note
recording the reframe and its scope. If BM25 declines, append a dated
note recording "on 2026-05-04 a brainstorm proposed reversing this
framing for BM25 work; the work declined, framing remains unchanged."
Either way the memory record stays current.

## Goals

- **G1.** Implement BM25-style IDF as an alternative to current
  `1/log2(1 + N)` in `src/mtg_synergy_graph/universal_scorer.py`.
- **G2.** Audit-gate the swap: compute scoring under both formulas,
  measure NDCG@30 and hidden_gem_hit_rate deltas on the 500-cmdr held
  set against the current pinned fixture.
- **G3.** Ship BM25 if it clears the success bar; revert and document
  null result if it doesn't.

## Anti-Goals (Carryover, Not Up for Re-Litigation)

- No EDHREC at inference. (BM25 IDF computes from candidate pool
  statistics, no EDHREC data.)
- No per-commander or per-archetype rules. (BM25 is a per-rule formula
  applied universally.)
- No training, no learned weights at inference. (BM25 has no
  hyperparameters to tune in its IDF-only form.)
- Forge DSL based, deterministic. (BM25 is deterministic given pool
  statistics.)
- `bench.py audit --expect-identity` will FAIL on this change by design
  (BM25 changes scores). Re-pin via `bench.py audit --repin --yes` is
  expected and required if BM25 ships.

## Success Criteria

**Threshold derivation (judgment-call disclosure):** The +0.010 NDCG
bar is operationally chosen, not statistically derived. It's roughly
5× today's optimizer noise floor for per-rule multiplier sweeps;
structural IDF reformulation may have different noise characteristics.
The plan should seed-vary the train/held split (~3 seeds) and report
NDCG delta variance as a sanity check. If the spread exceeds the
ship-bar margin, treat the result as inconclusive (a 4th outcome
beyond SHIP/INVESTIGATE/DECLINE) and document accordingly. Bootstrap
resampling is NOT required — seed-varying is enough for a probe-level
precision check.

### Three outcomes (evaluated in sequence)

1. **Per-commander regression check (prerequisite, simple):**
   - If ANY commander in the 500-cmdr held subset loses more than
     0.05 NDCG@30 individually → **DECLINE** (regardless of aggregate
     delta). The probe is rejected; tail-impact is too high to
     ship. A separate brainstorm could reconsider with a hybrid or
     per-rule-fallback approach, but that's not authorized here.
   - Otherwise continue to aggregate check.
   - Per-commander reporting via existing `bench.py audit --inspect`
     if it surfaces NDCG diffs, or new reporting flag if not (see
     Open Q2).

2. **Aggregate metric check (after per-commander passes):**
   - **SHIP** if BOTH:
     - NDCG@30 delta ≥ +0.010 vs current pin
     - hidden_gem_hit_rate delta ≥ −0.010 vs current pin (symmetric)
   - **INVESTIGATE** if NDCG@30 delta is in [+0.005, +0.010) AND gem
     stays within bound. Document the result, do NOT ship. The follow-on
     decision (try a different IDF variant, accept saturation, etc.)
     requires a NEW brainstorm. Do not let INVESTIGATE become implicit
     authorization to swap IDF variants in-flight. Importantly: the
     +0.005 lift is NOT pre-paid progress — the next IDF-variant
     attempt must independently clear +0.010 from the (unchanged)
     current pin.
   - **DECLINE** if NDCG@30 delta < +0.005, OR gem regression beyond
     bound. The DECLINE conclusion specifically rejects BM25-style
     IDF for this workload. It does NOT rule out other IDF
     alternatives (smoothed-frequency, structural-overlap,
     per-rule-cluster, rank-aware) — those remain valid follow-on
     directions and any of them could move the IDF lever where BM25
     could not.

### Sanity check (deferred to plan-time discretion)

If the planner has capacity, sample 5-10 commanders BEFORE running
the audit (lock the names into the plan doc as a pre-commit) and
produce side-by-side top-30 diffs after the audit. This is a
qualitative gut-check, not a hard gate — owner reviews whether the
new lists pass intuition. Useful but not required to ship.

## Per-Commander Regression Bound (consolidated into Success Criteria check #1)

The per-commander bound semantics live in Success Criteria check #1
above. Simple form: ANY commander loses more than 0.05 NDCG@30 →
DECLINE.

**Why 0.05 (caveat):** The bound is operationally chosen, not
derived. A 0.05 NDCG@30 swing on a single commander roughly
corresponds to "1-2 cards swap into/out of the top 30" or "noticeable
top-3 reordering." If the natural per-commander NDCG volatility
across audit reruns is observed to be ≥ 0.05, the bound is toothless
and must tighten — but that calibration is a future-work item, not a
prerequisite for THIS probe.

**No per-archetype cohort analysis.** A per-archetype check was
considered but dropped: `data/tags.db` does not contain commander→
archetype labels (per `bench/optimize.py:153`), and adding them is
its own work item that exceeds the scope of this probe. If
systematic per-archetype skew turns out to be the dominant failure
mode under BM25, that becomes the seed of a follow-on brainstorm
("design archetype-aware audit gates"), not in-scope here.

## Scope Boundaries

**In scope:**
- Replace the IDF formula in `_compute_idf_basis()` at
  `src/mtg_synergy_graph/universal_scorer.py:702`. The wrapper
  `_compute_idf_weights()` delegates to the basis function and does
  not need separate changes.
- Update tests that assert specific IDF values for known cases (these
  will need new expected values).
- **Seed-vary precision check (lightweight):** Run the audit under
  3 train/held split seeds (e.g., 17, 42, 137); report mean and
  spread of NDCG@30 delta. If spread exceeds the ship-bar margin
  (~0.010), document the result as inconclusive. No bootstrap-resample
  module required.
- **FLAT_COUNT_RULES sanity check:** Confirm `_FLAT_WEIGHT_OVERRIDES`
  rules continue to bypass IDF as expected (the existing if/else
  branch in `_compute_idf_basis` does this; verify the branch is
  preserved post-change). The 6 flat rules — `etb_self`, `evasion`,
  `scaling`, `spell_density`, `token_producer`, `tribal_density` —
  should not see IDF formula effects. No new tooling needed.
- Resolve the calibration-confound (Open Q1): pick (a)/(b)/(c).
- Run `bench.py audit` against current pin under chosen approach;
  produce per-commander diff (extending `--inspect` if needed);
  decide ship/no-ship per success criteria.
- If shipping: re-pin BOTH the 500-cmdr and 100-cmdr fixtures
  atomically (see Risks for why both are required); `bench.py audit
  --repin --yes` for each; document any Stage A pre-flight verdict
  shifts; rebuild `walker_outcomes.csv` baseline (see Risks); commit;
  update `docs/RULE_HISTORY.md` with the IDF change as a single
  entry; update `memory/feedback_edhrec_not_goal.md` with the dated
  reframe note (per Product Decision section).

**Out of scope:**
- Other IDF alternatives (smoothed-frequency, structural-overlap,
  per-rule-cluster, rank-aware). These are explicitly DEFERRED to
  follow-on brainstorms IF BM25 declines.
- Commander-target composition redesign. Separate orthogonal direction.
- Port-signature feature richness (vocabulary expansion). Separate
  orthogonal direction.
- Anti-synergy rule system. Separate orthogonal direction.
- Multi-card combo scoring. Separate orthogonal direction.
- Tuning BM25's optional smoothing constants beyond defaults. **Locked
  scope decision:** use pure classic BM25 IDF formula
  `log((N − df + 0.5) / (df + 0.5) + 1)`. Variants are a separate
  decision. (Replaces former "Open Question 1" which was self-answered.)
- Changes to `_RULE_QUALITY_MULTIPLIER` or `_FLAT_WEIGHT_OVERRIDES`
  in `data/scoring_weights.json` — EXCEPT to the extent that
  resolving the calibration-confound (Q1) requires touching them.
  See Open Q1 for the three options and which ones modify weights.
- Permanent feature flag for the IDF formula. **Locked scope
  decision:** implement BM25 directly with no `_ENABLE_*` switch.
  Reverting is `git revert` or a one-line change regardless. The
  flag adds a permanent code path that serves no consumer post-ship.

## Carryover Constraints

- Library shape: pure-function gates, severity tiers (PASS/WARN/REJECT),
  audit-gated changes via `bench.py audit + repin` discipline.
- 500-cmdr fixture is the calibration baseline; 100-cmdr is too small
  per `optimizer-fixture-size-2026-04-30.md`.
- The 100-cmdr canonical fixture stays for `bench.py audit` pre-commit
  hooks; the 500-cmdr is for this NDCG@30 evaluation.

## Implementation Hints (for ce-plan)

These are NOT design decisions — they're observations from the brainstorm
that the planner should verify:

- **Current IDF location:** The live formula `1.0 / math.log2(1.0 + n)`
  is materialized in `_compute_idf_basis()` at
  `src/mtg_synergy_graph/universal_scorer.py:702`. Both this function
  AND the legacy `_compute_idf_weights()` wrapper must change together;
  the formula switch must happen at basis-build time so cached
  `IdfBasis` instances reflect the chosen formula. `_idf_weights_from_basis()`
  (line 709) remains the multiplier-application step and should NOT
  contain the formula change. Per-commander basis cache is the hot path
  for both audit and `--explain`; preserve the cache pattern.
- **BM25 IDF formula:** `log((N - df + 0.5) / (df + 0.5) + 1)` where
  `N` = total candidate pool size (need to verify universal_scorer has
  this), `df` = candidate frequency for the (rule_id, cmdr_event,
  cand_event, filter_group) key (current `N` in the formula).
- **No switchable config constant.** Scope locks BM25 as a direct
  formula replacement (see Out of Scope). Reverting is `git revert`
  on the formula-change commit, not a flag flip.
- **Audit invocation:** `uv run scripts/bench.py audit` with the
  500-cmdr fixture. Per-commander breakdown via `--inspect` or similar.
- **Score-tensor parity:** `bench.py audit --expect-identity` will fail.
  Re-pin via `--repin --yes` is required if shipping.

## Risks

| Risk | Mitigation |
|------|------------|
| BM25 lifts aggregate but causes 1+ tail-commander regression >0.05 | Per-commander check #1 routes ANY violation to DECLINE. Simple, no hybrid escape. If tail regression turns out to be the dominant failure mode, follow-on brainstorm reconsiders with hybrid/per-rule-fallback design. |
| BM25 lifts gem-rate but doesn't move NDCG | Symmetric ship bar (gem ≥−0.010 to match NDCG ≥+0.010) means a gem-positive / NDCG-flat result fails the SHIP gate; document as learnings outcome and feed into next brainstorm's direction selection. |
| BM25 introduces scoring drift in tests | Tests asserting specific IDF values need new expected values. Plan should enumerate affected tests in advance and update them in the same commit as the formula change. |
| Rule weight overrides calibrated to current IDF become miscalibrated under BM25 | `data/scoring_weights.json` contains **53 `rule_quality_multiplier` entries and 6 `flat_weight_overrides` entries** (verified 2026-05-04). The calibration-confound is real; resolved per Open Q1's three options (a)/(b)/(c). Plan picks one with rationale before evaluating ship bar. |
| BM25 IDF scale shift breaks staple_bonus / anti-synergy / embedding scale calibration | Under log2: high-df rule (df=2000, N=5000) → IDF ≈ 0.091. Under BM25: → IDF ≈ 0.916 (~10× scale shift). Other scoring components (staple_bonus, anti-synergy, embedding contribution) were calibrated assuming log2 IDF magnitudes. Mitigation: the per-commander regression check (≤0.05 cliff) catches if scale shift causes catastrophic per-commander drops. If aggregate looks fine but the qualitative sanity check (deferred to plan discretion) reveals weird behavior, treat as evidence of scale-shift impact and DECLINE. Building IDF-distribution-histogram tooling preemptively is out of scope. |
| BM25 IDF yields zero or near-zero for very common rules | The "+1" inside the log keeps BM25 IDF non-negative. Asymptotic-zero behavior for high-df rules is a different shape than log2; tail-commander regression check catches if it bites a real commander. |
| Re-pin breaks Stage A pre-flight (PR #39 just shipped) | Stage A reads against the 100-cmdr canonical fixture in pre-commit hooks. **Plan MUST re-pin BOTH the 500-cmdr and 100-cmdr fixtures atomically as part of the BM25 ship commit, AND rebuild `walker_outcomes.csv` baseline.** Without rebuilding, the autonomous walker may inherit stale REJECT decisions from the log2-IDF era. Document any Stage A verdict shifts on prior scaffold proposals in the commit message. |
| Performance regression from BM25's formula | Negligible per-call (one extra arithmetic op); IDF-basis cache pattern unchanged. Sanity-check with `bench.py audit` wall-time. |

## Open Questions for ce-plan

1. **Calibration-confound resolution (NEW, blocking — surfaced by doc-review):**
   The 53 `rule_quality_multiplier` and 6 `flat_weight_overrides`
   entries in `data/scoring_weights.json` were tuned against the
   current IDF basis. The plan must pick ONE of:
   (a) **Reset multipliers to 1.0** in the v1-vs-BM25 A/B to isolate the
       IDF effect cleanly. Cleanest comparison; but the BM25 result is
       not directly shippable as the new production weights.
   (b) **Keep current multipliers**, accept that BM25 evaluation
       conflates IDF change + multiplier mismatch. Risk: a positive BM25
       result could be partial-credit to multiplier rescaling.
   (c) **Re-run the optimizer under BM25** before the ship/decline call,
       so the comparison is "current IDF + tuned multipliers" vs "BM25
       IDF + freshly-tuned multipliers." Most realistic shipping path
       but doubles compute cost and may exceed session capacity.
   Recommendation: (c) is the cleanest shipping decision; (a) is the
   cleanest scientific A/B. The plan should pick one with rationale.

2. **Per-commander reporting format**: The success criteria require
   per-commander analysis. Plan should specify whether
   `bench.py audit --inspect` already produces per-commander NDCG@30
   diffs across the held subset of the 500-cmdr fixture, or whether a new reporting flag is
   needed. (`--inspect-gems` exists for hidden-gem diffs but not NDCG.)

## References

- Origin doc (saturation diagnosis): `docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
- Sibling null result (embeddings): `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md`
- Fixture sizing lesson: `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`
- Current IDF location: `src/mtg_synergy_graph/universal_scorer.py:319-323`, `:673`, `:709`, `:724`
- Universal Port Matcher architecture: `CLAUDE.md` (Scoring Architecture section)
- Memory: `memory/feedback_edhrec_not_goal.md` (NOW EXPLICITLY REVERSED for this brainstorm; see Product Decision section above)
- Memory: `memory/feedback_audit_every_change.md` (this work IS gated by audit per the standing guardrail)
