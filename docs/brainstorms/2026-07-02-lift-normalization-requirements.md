---
date: 2026-07-02
topic: lift-normalization
reviewed: 2026-07-02 (4-persona doc review; 19 findings integrated)
---

# C1 Lift Normalization — score minus expected-baseline panel

## Problem Frame

Plan 2026-07-02-002 measured the weight layer dead: ~13 configurations
of flood demotion (concave haircuts, payoff/body tiers, pool scaling,
an anthem support family) all failed the −0.05 per-commander cliff
gate on one mechanism — the cliffed commanders are those whose
archetype IS the flooding family (Edgar's Vampires, Magda's
fuel-Dwarves, Kess's cheap spells, Myrel's Soldiers). Whether a flood
is noise or the archetype is a commander-DEMAND question that per-hit
constants, family shares, and pool sizes cannot express
(docs/solutions/best-practices/calibration-track-null-result-2026-07-02.md).

Lift normalization expresses it: rank by
`lift(cmdr, card) = score(cmdr, card) − λ·panel_mean(card)`, where
`panel_mean(card)` is the card's mean score over a fixed commander
panel computed by our own scorer at build time — the mechanics-only
rebuild of EDHREC's `synergy = deck% − baseline%` move (ideation #4,
FUNDED). It is the only lever class the null-result evidence still
funds for the OUTRANKED bucket (45.6% of misses).

**The load-bearing hypothesis (adversarial review):** lift only helps
if the cards doing the displacing have HIGHER panel_mean than the
labeled cards they displace. If displacers are commander-specific
floods (low panel_mean — "most panel commanders give Edgar's Vampires
nothing"), subtraction shifts the whole cohort by ~a constant, changes
nothing within it, and taxes staples instead. This is checkable for a
few minutes' compute BEFORE any scoring integration — the kill-test
(R0) exists to kill the cycle cheaply if the hypothesis is false.

Three riders inherit into this cycle: the Unit 3 `rank_bonus` purity
removal (currently masks tie density; re-runs conditionally after the
lift outcome), the flag-OFF tier/anthem probe infrastructure (their
cliffs were flood-vs-archetype cases lift is designed to absorb), and
the `handle_repin` gem-staleness fix (must land first — this cycle's
gem gates need fresh values).

## Requirements

**Kill-test (before any scoring integration)**
- R0. Build a throwaway panel artifact and verify, on the Unit 1
  forensics baseline cards: mean panel_mean(known monoculture
  displacers) > mean panel_mean(the OUTRANKED labels they displaced),
  plus an overall spread readout. Run against at least TWO panel
  compositions (e.g., EDHREC-top-N vs archetype/identity-stratified) —
  composition modulates which archetypes get taxed and is load-bearing,
  not a nicety. If the inequality fails under both compositions, the
  cycle DECLINEs here: write the null-result doc and stop. (Kill-test
  discipline: color-IDF probe precedent.)

**Baseline panel**
- R1. `panel_mean(card)` is precomputed at build time over a fixed,
  deterministic, committed panel of commanders (~200, color- and
  archetype-diverse). Panel scores are BASE scores: the panel-build
  path forces `_ENABLE_LIFT_NORMALIZATION` OFF (no self-reference
  across rebuilds) and uses the SYNERGY-DERIVED portion only —
  side-channel bonuses (rank_bonus, cmc_bonus, circuit, staple) are
  excluded, which both removes the residual EDHREC term (rank_bonus
  still exists until R9) and keeps panel_mean a measure of generic
  mechanical goodness. Panel *selection* may use EDHREC rank as a
  design-time input (embeddings commander-target precedent, "OFFLINE
  ONLY"), frozen into a committed artifact.
- R2. Panel composition rule is chosen in planning under these
  constraints: deterministic, committed, reproducible from a fresh DB,
  re-derived (not hand-listed) under Forge refreshes, and validated by
  the R0 two-composition sensitivity check. Leave-one-out semantics
  (does a panel commander's own contribution count toward baselines it
  is scored against) is decided explicitly, not by accident.
- R3. panel_mean values live in the synergy DB under full config-hash
  discipline (panel-content digest + lift flag join
  `ScoringConfigInputs`/`compute_config_hash`; embeddings hybrid-hash
  precedent for the chicken-and-egg placement). Cards absent from the
  artifact fall back to the GLOBAL MEDIAN panel_mean (neutral — the
  λ·0 fallback would hand new cards the maximally favorable baseline),
  and the audit emits a panel-coverage fraction with a warning below
  99%: the fallback is a partial-refresh bridge, never a steady state.

**Scoring integration**
- R4. Flag-gated per the house probe pattern: `_ENABLE_LIFT_NORMALIZATION`
  default OFF, bitwise-inert, `ScoringConfigInputs` registration at the
  flip step. λ is a committed scalar; the sweep grid is
  {0.25, 0.5, 0.75, 1.0} (λ=0 is the pinned baseline control).
- R5. The lift term arrives as a new per-candidate `UniversalScore`
  field injected at construction in `score_from_complements` (the
  `embedding_contribution`/`rank_bonus` precedent — `UniversalScore`
  has no card identity or DB access, so panel_mean must be supplied
  there), and is subtracted consistently at all three total-assembly
  sites: `UniversalScore.score`, `to_legacy_buckets()["total"]`, and
  the optimizer's fused mirror — the three-site fidelity contract Unit
  4 established, verified by the fidelity test + `--expect-identity`.
- R6. Optimizer semantics are explicit: `--optimize` runs with
  panel_mean FROZEN at the baseline config (a per-grid-cell panel
  rebuild is infeasible), and the plan documents this approximation.
  The recurring cost — every scoring-config change invalidates the
  panel, so a ~200-commander re-score precedes audits while the flag
  is ON — is stated as an accepted workflow tax.

**Evidence package (bounded matrix + kill-order)**
- R7. Sequential narrowing, ceiling ≤7 full 500-cmdr batteries:
  (1) λ sweep runs on the LIFT-ALONE arm only (4 cells) and selects
  one committed λ; (2) if lift-alone blanket-DECLINEs at every λ, the
  cycle ends there (joint arms are wasted budget); (3) otherwise the
  `lift+tier` arm, the `lift+tier+anthem` arm (re-wired exactly as
  landed — the anthem key redesign is out of scope this cycle), and
  ONE staple-bonus-ablated arm at the winning λ run once each. Every
  λ cell and arm also emits a tie-density / top-30 score-spread
  sidecar (subtraction can compress spread and enlarge the tiebreak
  region — the Unit-3 alphabet effect).
- R8. Watch-lists, named BEFORE the sweep: (a) the inherited
  demotion-cliff commanders (Edgar, Magda, Nissa, Kess, Myrel, Kodama,
  Rionya, Camellia, Elenda, Arasta); (b) a NEW stratum — the N
  commanders whose EDHREC top-30 has the highest mean panel_mean
  (computable once the artifact exists): these are who lift taxes
  hardest and where its new cliffs will appear.

**Riders (ordered)**
- R9. FIRST, before any measurement: fix `handle_repin` to pass
  `edhrec_conn` (the `build_fixture` call at bench/handlers.py:104;
  the :739 comment anticipates it), failing HARD (exit 2, actionable
  hint) when the EDHREC DB is missing — a gem-less pin silently
  reintroduces the staleness this fixes. Refresh both pins' gem
  legacy; historical gem figures (e.g., the tier arm's +0.024) were
  measured against stale pins and re-base here.
- R10. AFTER the lift outcome: re-run the Unit 3 two-step `rank_bonus`
  ablation, but only if R7's tie-density sidecar shows tie density
  actually decreased under the chosen arm; ship the purity removal if
  unjustified divergence stays flat. If the sidecar shows no
  improvement, record that and leave Unit 3 deferred with its
  evidence updated.

**Gates**
- R11. House gates per arm: DECLINE on any 500-cmdr per-commander
  cliff < −0.05; SHIP at aggregate ≥ +0.010 NDCG@30 OR the
  goal-aligned alternative (unjustified-OUTRANKED share and displacer
  monoculture down, aggregate ≥ −0.010, fresh gem non-degrading);
  cumulative floor vs a new pre-lift baseline tag. A clean DECLINE
  with a null-result doc extends the calibration evidence and counts
  as success.

## Success Criteria

- Primary: an arm exists where no watch-list commander (either
  stratum) cliffs while displacer monoculture share and
  unjustified-OUTRANKED share drop from the Unit 1 baselines
  (monoculture ≥85% cases: 15 commanders; unjustified divergent
  picks: 280/1,833), with fresh (post-R9) gem non-degrading.
- Optional upside, not load-bearing: the `lift+tier` arm re-lands the
  tier's gem gain (previously +0.024 against stale pins; re-based
  value from R9 is the real target).
- If R0 fails or all arms DECLINE: the null-result doc states exactly
  what information the lift baseline lacked — closing the calibration
  family entirely and re-pointing at the role/quota portfolio sibling
  with evidence.

## Scope Boundaries

- No gem plausibility-gate (B3) definition change — own FR6 cycle;
  R9 refreshes stale VALUES only.
- No node_kind matcher refactor (C5), no importer granted-ability
  unwrapping, no PPMI event-map batch, no anthem key redesign.
- No per-deck role quotas / portfolio re-ranking (ideation #3-strong
  stays defunded).
- Panel is commander-side only; no per-card popularity or EDHREC data
  enters panel_mean VALUES (selection is design-time only, R1).

## Key Decisions

- **Subtractive lift, not z-score/percentile**: simplest, matches
  EDHREC's formula shape, keeps explain-path interpretability; z-score
  is the named fallback probe if subtraction cliffs on low-variance
  cards; percentile rejected (destroys magnitude the tiebreak-poor
  sort needs).
- **Kill-test before integration (R0)**: the adversarial review's
  Edgar arithmetic shows the core hypothesis may be false; a
  throwaway-panel check on known forensics cards costs minutes and
  can save the whole cycle.
- **Synergy-only panel scores (R1)**: excludes the side-channel
  bonuses — removes the rank_bonus purity contradiction during the
  measurement window and makes panel_mean a mechanics measure.
- **Median fallback, not zero (R3)**: λ·0 would systematically float
  every new card above the deflated field after partial refreshes.
- **Bounded matrix (R7)**: without the sequential-narrowing rule the
  package reads as a 24-battery cross — larger than the entire
  13-configuration campaign that just declined.
- **Riders bundled but not load-bearing**: tier/anthem re-flips and
  the Unit 3 ablation are incremental arms on infrastructure this
  cycle builds anyway; success is defined by lift-alone outcomes.

## Dependencies / Assumptions

- Builds on branch `fix/scoring-flaw-remediation` (PR #85) or main
  after merge: dual-total three-site fidelity contract, flag/hash/test
  conventions, flag-OFF tier/anthem/pool machinery.
- Panel precompute ≈ one 200-cmdr scoring pass (minutes; verified
  order-of-magnitude by this session's 500-cmdr batteries). Grows if
  the denominator decision (below) forces per-identity re-scoring.
- EDHREC-data refreshes change a rank-derived panel composition →
  panel digest flips → repin churn; accepted under hash discipline,
  frequency is a planning consideration.

## Outstanding Questions

### Resolve Before Planning
- (none — R0 resolves the go/no-go empirically)

### Deferred to Planning
- [Affects R1/R2][Technical] panel_mean denominator semantics:
  (a) cards not returned by `score_all_universal` for a panel
  commander count as 0 vs excluded; (b) whether the mean conditions on
  color-identity legality (per-card denominator = legal panel
  commanders) or averages all — EDHREC's baseline is color-conditioned
  and the color-IDF DECLINE (2026-07-02) is prior evidence on this
  axis, from the opposite direction.
- [Affects R3][Technical] Storage shape (table vs sidecar) and the
  panel digest's exact placement in the hash inputs (embeddings
  hybrid-hash precedent).
- [Affects R3][Technical] Artifact keying: oracle_id (repo convention)
  vs card name (the scorer's internal key) — one explicit mapping.
- [Affects R5][Technical] Whether λ·panel_mean subtracts pre-dampener
  or on the assembled total, under the constraint that panel_mean
  arrives at `UniversalScore` construction; verify with hand-computed
  pairs. Also: whether staple-only injected candidates (score = 0.01 +
  bonuses, no complements) are exempt — subtracting a full panel_mean
  drives them sharply negative, and R7's staple-ablated arm already
  probes this channel.
- [Affects R9/R10][Technical] R10's rank_bonus removal flips the
  config hash → panel rebuild + repin; budget the second build or run
  the ablation against stale-panel scores with that caveat recorded.

## Next Steps
-> /ce-plan for structured implementation planning
