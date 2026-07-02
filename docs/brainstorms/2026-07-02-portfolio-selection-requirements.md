---
date: 2026-07-02
topic: portfolio-selection
---

# Role/Quota Portfolio Selection — Diminishing-Returns Top-30 Assembly

## Problem Frame

The top-30 recommendation list is assembled purely by pointwise score.
When one rule family dominates a commander's candidate pool (tribal
floods, spell floods), the family fills most of the 30 slots and
displaces broadly-good labeled cards. OUTRANKED is the largest miss
bucket in the latest forensics run (1,206/2,646 misses, 45.6%).

Two full probe cycles (plan 2026-07-02-002, ~13 weight-layer
configurations; plan 2026-07-02-003, lift normalization + z-score
fallback) measured the entire pointwise-transform space closed: every
per-card demotion moves whole cohorts together and cliffs whichever
commanders genuinely DEMAND the cohort (Edgar's vampires, Krenko's
goblins, Kess's spells). Whether a 90%-concentrated family is
pathology or the archetype is a commander-demand question that
per-card scoring cannot express — Marrow-Gnawer's top-30 at 93.5%
`tribal_density` is correct; the same concentration elsewhere is the
flood. See
docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md
and
docs/solutions/best-practices/lift-normalization-kill-test-null-result-2026-07-02.md.

The flood problem is a LIST-composition problem (30 slots, one
family), not a per-card scoring problem. This cycle moves the lever
to selection: treat top-30 assembly as a portfolio with
diminishing returns per family, so demand-adaptivity emerges from
score structure instead of a classifier.

Notably, hidden_gem_hit_rate improved in EVERY flood-demotion
configuration measured (+0.0007…+0.0297) — the goal axis
consistently rewards flood bounding; per-commander NDCG cliffs were
the sole blocker every time. Two honesty caveats on that framing:
(1) because the mechanism directly edits top-30 composition, gem
rate improves near-tautologically under ANY flood bounding — it is
a necessary but weakly-discriminating signal, which is why the gate
guards gem QUALITY and non-regression rather than requiring gem
improvement (R9/R9a — see Key Decisions); (2) the 45.6% bucket overstates
the reachable upside — recoverability of a missed label is
family-share ARITHMETIC, not family identity: the discount applies
to the family-attributable contribution, so a same-family miss with
a LOWER family share than the marginal flood members can cross above
them after the discount, while a miss dominated by the flood family
at similar-or-higher share cannot (within-family order is preserved
only when the discount applies to the whole score or shares are
equal). Forensics shows missed cards' dominant families are led by
spell_density (24.9%) and tribal_density (19.9%), so the addressable
share must be measured — empirically, not by family-identity
heuristics — before the sweep (R7b).

**Load-bearing hypothesis (falsifiable — the target of the R0
kill-test, the cycle's stage-zero pre-integration gate defined in
R7):** on
cohort-demand commanders, the non-family candidates behind the flood
score BELOW the flood members' discounted effective score (cohort
survives); on flooded commanders, the displaced labeled cards score
ABOVE the marginal flood members' discounted effective score (flood
sheds). R0 is designed to break this on the named trap population —
the fuel-tribe sub-shape (Magda's Dwarves, Nissa's mana-Elves) and
the archetype sub-shapes (Edgar, Krenko, Kess, Myrel) — not merely
to grid-search decay rates. If the hypothesis fails on any
sub-shape at every decay cell, the cycle DECLINES with that named
evidence.

## Requirements

**Selection mechanism**

- R1. Top-30 assembly becomes a greedy portfolio selection over full
  per-family contribution VECTORS (the faithful submodular/MMR
  adaptation, adopted in review pass 2 over dominant-family
  labeling): at each pick, a candidate's effective score is its
  non-family residual plus, for EACH family, that family's
  attributable contribution discounted by a decay function of the
  family's contribution mass already accumulated in the selected
  set. No hard caps; picks into an empty family are undiscounted;
  and a card carrying flood-family contribution under a different
  top family still pays the flood discount on that share — the
  multi-family leak dominant-labeling would allow is closed by
  construction.
- R2. Demand-adaptivity must be emergent, not classified: no
  commander-demand detector, no per-commander configuration. A
  cohort-demanding commander keeps its flood because even discounted
  family members outscore the alternatives; a flooded commander sheds
  marginal family members to the broadly-good cards behind them
  ("broadly-good" = candidates whose synergy contributions span
  multiple rule families rather than concentrating in the flood
  family — typically the labeled cards the flood currently
  displaces).
- R3. The decay schedule is a single tunable (plus the family map);
  the exact functional form (geometric vs harmonic vs other) and the
  swept parameter grid are planning decisions, but the mechanism must
  not introduce more than one new scoring-relevant constant beyond the
  family map itself.

**Family definition**

- R4. Family membership is defined by an explicit, committed
  `rule_id → family` mapping artifact covering every registered rule
  (declarative and Python-path). Unmapped rule_ids are a hard error,
  not a silent default — enforced when the portfolio flag is ON
  (flag-OFF runs must stay bitwise-identical per R8, so an unmapped
  rule cannot brick a flag-OFF engine load).
- R4a. The rule-authoring pipeline stays closed: `scaffold_rule.py`
  emits a family assignment for every new rule it lands,
  `rule_quality_gate.py` (or a registry test) asserts every
  registered rule_id has a family entry — so the R4 hard error can
  only fire on a malformed commit, never on routine walker output.
- R5. A card's per-family contribution vector is derived from the
  same per-rule contributions the tensor already records, grouped by
  the R4 mapping (a "rule group" is the set of rules mapped to one
  family). Selection needs no dominant-family label (see R1); where
  a single label is still needed for REPORTING (R9a strata, the
  coarse pre-planning bound), it is the family with the highest
  summed contribution for that (commander, card) pair — ties there
  are cosmetic, not scoring-relevant.
- R6. Forensics' `rule_family()` is only identity-by-rule_id plus
  three derivable naming families (`repl_*`, `*_tribal`, `*_feeder`)
  — it is a naming reference, NOT a reusable taxonomy (it keeps
  `lord`, `tribal_density`, and the peer-tribal `tribal` family
  separate). The committed map must therefore introduce new sibling
  merges beyond forensics: at minimum `lord` + `tribal_density` +
  `*_tribal` into one tribal family, and an equivalent audit of
  `spell_density`'s siblings (e.g. `spellcast_resonance`) — otherwise
  a flood split across sibling rules accrues discounts several times
  slower and escapes the mechanism. Each merge is documented in the
  mapping artifact.
- R6a. The R0 kill-test sweeps at least two map granularities
  (merged-siblings vs finer), so a DECLINE is attributable to the
  mechanism rather than to a map-granularity artifact.

**Gating and evaluation**

- R7. R0 kill-test before any integration ("R0" is the cycle's
  stage-zero pre-integration gate, a stage label — not a numbered
  requirement): an offline re-rank simulation (selection pass +
  re-rank + `compute_ndcg` against graded labels, ~3 min/100
  commanders, zero scoring-path changes) sweeps the decay grid and
  reports per-commander NDCG deltas, aggregate NDCG, and
  hidden_gem_hit_rate per cell. The method is documented in
  docs/solutions/best-practices/lift-normalization-kill-test-null-result-2026-07-02.md;
  the prior cycle's `r0_ndcg_sim.py` lived in an ephemeral session
  scratchpad and is NOT committed infrastructure — the harness must
  be rebuilt (and this time committed) as part of the kill-test unit,
  running on the authoritative gate instrument's candidate pool and
  ordering (the fixture path, see R12) so the preview is exact for
  the gate it predicts. If no cell passes the SHIP gate axes, the cycle DECLINES
  before Unit 1 of integration.
- R7a. The R0 sweep may run on the 100-cmdr golden set for speed, but
  every surviving grid cell is confirmed on the 500-cmdr fixture
  before the SHIP decision — the fuel-tribe trap population that
  killed plan 002 (Magda, Nissa, Camellia, Elenda) lives on the
  500-cmdr fixture, and the R0 report must break these commanders
  out explicitly. Because the traps are the most diagnostic
  population, they are ALSO scored as a mandatory sidecar at EVERY
  grid cell of the 100-cmdr sweep (a handful of extra commanders
  costs seconds per cell) — otherwise a full-sweep DECLINE would
  close the cycle without the fuel-tribe evidence the hypothesis
  block promises.
- R7b. The FIRST R0 readout, before any decay sweep, is an
  addressable-share diagnostic defined EMPIRICALLY (dominant-family
  identity under-counts — see Problem Frame): a miss is addressable
  if at some decay depth its effective score exceeds the marginal
  flood member's discounted effective score; the sim computes this
  directly from the tensor at negligible cost. If the aggregate
  addressable share is negligible, the cycle DECLINES before the
  sweep, and a DECLINE report must distinguish "mechanism wrong"
  from "nothing recoverable at the selection layer". A coarse
  family-identity approximation of this share is computed BEFORE
  planning from existing forensics data (see Resolve Before
  Planning).
- R8. The mechanism is flag-gated (default OFF) and registered in
  `ScoringConfigInputs` / `compute_config_hash`; flag-OFF behavior is
  bitwise-identical to current scoring (`--expect-identity` clean).
- R9. SHIP gate (strict, all three, evaluated on the 500-cmdr
  fixture per the optimizer-fixture-size guidance; the 100-cmdr
  canonical is a secondary sanity view): (a) zero per-commander
  NDCG@30 deltas < −0.05 vs baseline (strict less-than, matching
  `PER_COMMANDER_REGRESSION_THRESHOLD` in
  `bench/per_commander_ndcg.py` so the requirement and the committed
  instrument agree at the boundary); (b) aggregate NDCG IMPROVED
  above a numeric noise band — the cycle's payoff axis; (c)
  hidden_gem_hit_rate NOT REGRESSED below a numeric noise band (gem
  improvement is reported but not required — see Key Decisions:
  requiring it would auto-DECLINE a working mechanism). Both numeric
  bands are pinned during planning BEFORE the R0 sweep runs — the
  gate must be mechanically checkable, not judged after seeing
  results. Scoring is deterministic at fixed
  config (`--expect-identity`), so "run-to-run variance" does not
  exist: the gem band derives from `hidden_gem_hit_rate` movement
  across (config, snapshot) boundaries in `.audit/history.csv`; the
  NDCG band from `aggregate_ndcg_canonical` across boundary markers
  in `.audit/forensics_history.csv`, or — preferably — from
  per-commander bootstrap resampling on the 500-cmdr fixture
  (across-config effect sizes are intentional changes, not noise;
  bootstrap is the principled derivation).- R9a. Gem-QUALITY probe (because the rate axis is weakly
  discriminating, see Problem Frame): an automated full pass over
  every gained gem, asserting a predicate STRICTLY STRONGER than the
  existing gem plausibility gate (which every counted gem already
  passes by construction): contributions from ≥2 distinct rule
  FAMILIES, at least one outside the commander's flood family. The
  per-stratum pass rate is reported and the check is mechanical,
  consistent with R9's own discipline; a small sampled
  `--inspect-gems`-style human review of gained gems on the known
  flood commanders is advisory color, not a gate. The R0 report
  breaks gem delta out by flood vs non-flood commander strata: on
  flood commanders, gem rate is EXPECTED to locally drop (labeled
  replacements displace unlabeled flood members that counted as
  gems); the stratified readout diagnoses where gem movement
  originates rather than letting opposing movements net silently,
  and R9(c)'s non-regression band applies to the aggregate.
  Operational strata definition: a commander's FLOOD FAMILY is the
  family with the largest aggregate contribution share of its
  baseline (pinned) top-30; the flood STRATUM is commanders whose
  top family share exceeds a threshold pinned in planning.

**Measurement instrument**

- R12. The bench fixture/eval path today
  sorts raw `score_all_universal` output and computes gem membership
  from the raw top-30 — it never routes through `page()`, so an
  assembly-layer re-ranker is INVISIBLE to `bench.py audit`,
  `--inspect-gems`, and the history ledger as they stand. When the
  flag is ON, fixture building and audit evaluation must assemble
  the top-30 through the same selection layer as `page()` (and
  `--repin` pins the assembled ordering); flag-OFF fixture behavior
  is unchanged. Without this, R9 is unmeasurable by the instruments
  the repo's gating discipline uses — only the forensics live pass
  would see the effect. The fixture-path instrument is AUTHORITATIVE
  for both the R0 DECLINE and the SHIP decision (the R0 harness runs
  on its pool and ordering, keeping preview and gate on one
  instrument); `page()` remains the user-facing surface, whose live
  behavior the forensics pass verifies. Note the fixture pool is
  unfiltered with a name-only tiebreak while `page()`
  legality-filters and uses the 4-key tiebreak — this divergence
  predates the cycle and is why an authority declaration is needed.

**Explainability and audit**

- R10. `--explain` output must show when a card's list position was
  affected by the portfolio discount (family, pick index, discount
  applied), and whether a card entered the top-30 because a
  flood member was discounted below it.
- R11. Raw pointwise scores are unchanged; the discount applies at
  list assembly only. Audit tooling that reads persisted tensors
  continues to see undiscounted contributions.

## Success Criteria

- R0 kill-test produces a full decay-grid matrix with per-commander
  cliff counts, aggregate NDCG, and gem rate per cell — evidence
  either funds integration or closes the selection axis with a
  null-result doc (the designed cheap exit).
- On SHIP: aggregate NDCG improved above the pinned noise band with
  zero per-commander deltas < −0.05, hidden_gem_hit_rate within its
  noise band of the `pre-lift-normalization` baseline ledger
  (100-cmdr agg 0.8160, 500-cmdr 0.7123), and the R9a quality probe
  passing.
- Known flood cases (Kess spell flood, Myrel anthem displacement)
  show marginal family members replaced by labeled broadly-good
  cards, while known cohort-demand cases (Krenko, Edgar,
  Marrow-Gnawer, Magda's fuel-Dwarves) keep their cohorts.

## Scope Boundaries

- No commander-demand classifier of any kind (see R2) — that is the
  measured-dead axis.
- No hard per-family caps and no role-coverage minimums (approaches B
  and C, considered and rejected).
- No changes to rule set, IDF form, per-card scores, dampener
  semantics, gem-gate definition, legality filter, or the embeddings
  flag.
- Selection applies to the canonical top-30 recommendation surface
  only (the existing surface constant, not a new scoring-relevant
  constant under R3's budget). The full ranking remains a
  deterministic total order: the assembled 30 as prefix, all
  remaining cards in raw-score order in the tail — cards discounted
  out of the top-30 re-enter the tail at their raw-score positions,
  never dropped or duplicated. `page()` offset/limit windows and
  deep-window consumers (forensics' `limit=1_000_000` pass) read
  this same total order.
- Family map covers rule_ids only; no card-level or embedding-cluster
  family assignment in this cycle.

## Key Decisions

- **Approach A is an instance of established IR diversification, not
  a bespoke invention:** greedy selection with per-family diminishing
  returns is the standard diversity-aware re-ranking family (Maximal
  Marginal Relevance / submodular diversification), adapted to
  discrete rule-family membership instead of a continuous similarity
  measure. Known parameterizations and failure modes from that
  literature inform the decay-form grid in planning.
- **Diminishing returns over hard quotas (Approach A over B):** a
  hard cap gated by a demand classifier rebuilds the exact judgment
  plan 002 measured as inexpressible, and binary gates cliff whole
  commanders on classifier error. Decay lets the scores themselves
  answer the demand question.
- **Diminishing returns over role-coverage floors (A over C):** the
  eval labels are EDHREC synergy lists, not deck templates; forcing
  coverage of unrewarded families hurts both axes.
- **Explicit rule→family map over dominant-rule-only or embedding
  clusters:** auditable, hash-disciplined, matches existing forensics
  grouping; dominant-rule-only fragments floods across sibling rules
  (lord + tribal_density escape together); embedding clusters are
  unauditable and depend on flag-OFF infrastructure.
- **Vector formulation over dominant-family labeling (review pass
  2):** discounting each card's full per-family contribution vector
  by accumulated selected family mass is the faithful submodular
  adaptation; dominant-family labeling leaks floods carried through
  multi-family cards and imports a tie-instability question the
  vector form dissolves.
- **Honest gate — NDCG-recovery purpose, gems guarded (revised in
  review pass 2):** the mechanism recovers OUTRANKED misses, which
  are EDHREC-labeled by definition; on flood commanders it
  mechanically trades unlabeled gems for labeled cards, and no named
  pathway produces gem gains on non-flood commanders where the
  discount barely binds. Requiring strict gem improvement would
  auto-DECLINE a working mechanism. SHIP therefore requires
  aggregate NDCG improvement above the noise band with gems
  non-regressed and quality-guarded (R9a); gem improvement is
  reported, not required.
- **Discount at assembly, not in scores (R11):** keeps the audit
  tensor, optimizer basis, and every pointwise diagnostic unchanged;
  the mechanism is a re-ranker, which is also what makes the R0
  offline simulation an exact preview.

## Dependencies / Assumptions

- Per-rule, per-card contribution data sufficient to determine the
  dominant family is already persisted in the audit tensor and
  available on the live scoring path (assumption verified for the
  tensor; live-path availability at `page()` time is a planning
  check).
- The `pre-lift-normalization` git tag (created by the plan
  2026-07-02-003 rider re-pin, commit `92fba39`; its annotation
  carries the full baseline ledger — see
  docs/solutions/best-practices/lift-normalization-kill-test-null-result-2026-07-02.md
  § Artifacts) is the baseline for all comparisons; the quoted gem
  values (100-cmdr agg 0.8160, 500-cmdr 0.7123) are that re-pin's
  fresh fixture aggregates.
- Surviving plan-002 infrastructure (`tribal_body` tier, pool-scaled
  flat weights, concave concentration factor) stays flag-OFF and is
  NOT bundled into this cycle's SHIP decision.

## Outstanding Questions

### Resolve Before Planning

(none — the data check below was resolved 2026-07-02)

**Coarse addressable-share bound (RESOLVED 2026-07-02, funds the
cycle):** computed via a fresh in-process `compute_forensics()` pass
(same code path as `--forensics`) reusing `rule_family()` + the R6
tribal/spell sibling merges; dominant merged family per OUTRANKED
miss vs commander flood family from positive top-30 contribution
shares. Results:
- Aggregate: 750/1191 = **0.630** of OUTRANKED misses addressable
  (0.622 counting the 15 zero-tensor `staple_only` misses).
- Flood-stratum (63/100 commanders with top family share > 0.5):
  **0.493**; non-flood stratum: **0.815**.
- Per-commander distribution strongly bimodal: 24 commanders at
  exactly 0.0, 43 at exactly 1.0 (min 0 / median 0.926 / max 1).
  Lowest: Wort (flood=spell 0.784, 23 misses), Sythis (spell 0.534,
  22), Zur (value_engine 1.000, 22), Yarok (panharmonicon 1.000,
  20), Mirko Vosk (effect_resonance 0.748, 18).
- This is the pessimistic family-identity FLOOR; the 24
  zero-addressable commanders are reachable only through R7b's
  empirical share-arithmetic path, which this floor under-counts by
  design. ~750 recoverable misses is ample premise evidence.

### Deferred to Planning

- [Affects R1][Technical] Decay functional form and grid (geometric
  `d^(m-1)` vs harmonic `1/m` vs floor-clamped) — pick 2-3 forms for
  the R0 sweep rather than one. Note: floor-clamped carries two
  constants (rate + floor) and is admissible in the sweep as a
  diagnostic only — shipping it would exceed R3's one-constant
  budget and requires an explicit, documented R3 amendment at SHIP
  time.
- [Affects R1][Technical] Whether the discount applies to the full
  family-attributable contribution or only the flat/density share —
  the R0 sim can measure both variants cheaply.
- [Affects R4][Technical] Where the family map lives (new committed
  JSON next to `rules_seed.json` vs a registry attribute) and whether
  functional edits flip `compute_config_hash` like the other seed
  artifacts (they should).
- [Affects R7][Needs research] Whether the greedy selection needs a
  candidate-pool depth beyond the current page window (a discounted
  flood member's replacement may sit below the old top-30 cutoff) —
  measure the required look-ahead depth in the R0 sim.
- [Affects R10][Technical] Exact `--explain` rendering for
  discount-affected rows.
- [Affects R7][Technical] Where the committed R0 simulation harness
  lives (`scripts/` vs a bench sandbox) — it is now a two-cycle
  recurring instrument and must stop being a scratchpad artifact.
- [Affects R9][Technical] Derive the numeric NDCG noise band
  (preferably per-commander bootstrap resampling on the 500-cmdr
  fixture; fallback `aggregate_ndcg_canonical` boundary variance in
  `.audit/forensics_history.csv`) and the gem minimum-signal
  threshold (`hidden_gem_hit_rate` movement across (config,
  snapshot) boundaries in `.audit/history.csv`); pin both in the
  plan before the R0 sweep.
- [Affects R4a][Technical] Should the family map's merge discipline
  get a standing drift diagnostic (e.g. an embedding-dedup-style
  sibling detector flagging candidate merges after each walker
  batch)? R4a checks entry PRESENCE, not assignment correctness — a
  walker-assigned wrong family silently regrows the sibling-escape
  leak R6 closes.

## Alternatives Considered

- **B — Demand-adaptive hard quotas:** rejected; rebuilds the
  inexpressible demand classifier, binary cliff risk (Magda
  tribe-as-fuel is a known classifier trap).
- **C — Role-coverage minimums (inversion):** rejected; misaligned
  with EDHREC-list labels, requires a role taxonomy that doesn't
  exist.
- **Pointwise anything (haircuts, tiers, pool scaling, lift/z-score
  baselines):** measured closed across plans 002/003; do not
  re-propose.

## Next Steps

-> /ce-plan for structured implementation planning (R0 kill-test is
Unit 0 and the designed cheap exit).
