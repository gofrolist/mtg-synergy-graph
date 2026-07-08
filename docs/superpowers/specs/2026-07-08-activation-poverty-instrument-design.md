# Activation-Poverty Instrument — Design

**Date:** 2026-07-08
**Status:** Approved (brainstorm), pending implementation plan
**Author:** brainstormed with Claude Code

## Motivation

The model has hit a well-defended plateau against the EDHREC-NDCG
objective. Golden-set NDCG@30 has been flat at **0.2364** since ~2026-07-07,
and every July experiment was DECLINED (see the July null-result docs in
`docs/solutions/best-practices/`). The 2026-07-08
`per-candidate-discrimination` null-result names the ceiling: the scorer
gives flat, class-uniform IDF credit, and no mechanical or
design-time-embedding signal beats production out-of-sample. Pushing
golden-NDCG further is empirically dead.

The chosen goal is different: **a better product for MORE commanders** —
coverage, not aggregate ranking. Golden-set NDCG is the wrong yardstick
for this: it only sees 100 commanders and rewards agreeing with EDHREC.
The `--forensics` report shows the real problem — **NO_RULES = 42.4%** of
misses, and a cluster of commanders the engine has *nothing* mechanical to
say about (Xenagos: 5 tensor candidates; Borborygmos: 160; Yawgmoth: 190;
Phenax: fires zero rules and returns Walls at a flat 0.5 staple bonus).

This spec delivers the **instrument** that makes coverage measurable, so
that coverage-oriented rule work can be gated on a per-commander coverage
metric instead of golden NDCG (which would re-run the July DECLINE loop).

### What this spec is NOT

- **Not** a scoring change. Zero scoring-path mutations; pure read-side
  instrument, like `demand_coverage.py` / `portfolio_sim.py`.
- **Not** the toughness-payoff rule. The rule that will serve Phenax and
  the toughness-matters cohort is a **follow-on spec**, gated on this
  instrument. This spec delivers only the instrument + baseline + the
  cohort definition it will use.

## Scope decomposition

The mill coverage push narrowed through brainstorm to a sharper, verified
target:

1. The mill **supply** side is already served (`gy_fuel_feeder` consumes
   `effect.Mill`; adding mill cards as graveyard fuel was DECLINED
   2026-07-03 — `gy-fuel-vocab-expansion-null-result`). Do not revisit.
2. The Phenax **payoff** side is genuinely empty. Phenax's real mechanical
   hook is not "mill" — it is **toughness-scaling**: its payoff SVar is
   `Count$CardToughness` (`{T}: target player mills X, X = this creature's
   toughness`). The cohort of legendary creatures whose payoff scales off
   toughness is ~8 exact (`scales_with` `CardToughness`: Phenax, Tanazir
   Quandrix, Arwen Weaver of Hope, Vhal, Betor, Orysa, The Pride of Hull
   Clade) plus the toughness-as-combat / walls-draw set (Doran, Arcades,
   High Alert) → **~12 commanders**. A real cohort, not a whitelist.

This instrument is a prerequisite for measuring that follow-on rule. It is
also reusable for every future coverage archetype (lands-matter,
power-doubling, etc.), so it is worth building as standing infra.

## The metric — activation poverty, per commander

Computed from a single live `engine.page()` pass per commander:

| Metric | Definition | Phenax today |
|---|---|---|
| `earned_top30` (**primary**) | of the surfaced top-30, count that earned **any** rule contribution (`total_score` > staple bonus + `rank_bonus` tiebreak) | **0 / 30** |
| `n_rules_firing` | distinct complement rules that fire for the commander | ~0 |
| `n_scored_cands` | candidates in the whole pool with earned synergy > 0 | ~0 |

`earned_top30` is the headline: it answers *"is the product telling this
commander's pilot anything real, or handing back cmc-sorted staples?"*
Phenax scores 0.0 today (Walls surface by accident via the flat staple
bonus + tiebreak, with zero rule credit).

**"Earned" definition.** A candidate's score is *earned* iff
`total_score − staple_bonus − rank_bonus > 0`. The staple-bonus and
`rank_bonus` components are subtracted using the **same** helper the
forensics ablation uses (`universal_scorer.rank_bonus_for_rank` and the
staple-bonus source), so the definition cannot drift from the forensics
report's EDHREC-ablation.

A coverage rule **succeeds** when it lifts the cohort's mean
`earned_top30` with **no regression** to the control population.

## Architecture — three units

### Unit 1 — `src/mtg_synergy_graph/bench/coverage.py` (metric core)

Pure function: given an `engine.page()` result for one commander, return
`{earned_top30, n_rules_firing, n_scored_cands}`. No DB, no I/O — takes the
already-computed page rows. Reuses the forensics ablation helper for the
staple/`rank_bonus` subtraction so "earned" is defined in exactly one
place.

- **What it does:** turns a scored page into three coverage scalars.
- **Interface:** `compute_coverage(page_rows, *, staple_bonus, rank_bonus_fn) -> CoverageMetrics`.
- **Depends on:** the forensics ablation helper; nothing else.

### Unit 2 — `toughness_payoff` cohort predicate in `src/mtg_synergy_graph/bench/cohorts.py`

Follows the existing `subtype_death_payoff` template. Selects the ~12
toughness-payoff commanders: the `scales_with` `CardToughness` set
(precise SQL: `card_ports.event_class = 'CardToughness'` or
`scaling_expression LIKE '%CardToughness%'`) UNION an explicit small set
for the toughness-as-combat-damage / defender-draw commanders (Doran,
Arcades, High Alert) that express toughness through a different port shape.
Must **exclude** the ~300 cards that merely reference "Toughness" in a buff
or P/T line (the naive `raw_line LIKE '%Toughness%'` match is noise).

- **What it does:** names the cohort a future rule will key on, for lift
  measurement.
- **Interface:** a predicate registered alongside `subtype_death_payoff`.
- **Depends on:** `card_ports` / `cards` schema.

### Unit 3 — `scripts/coverage_report.py` (CLI, mirrors `demand_coverage.py`)

Three modes:

- **`census`** — full ~2,000-legal-commander run (legendary creatures
  passing `legal_cards()`); computes the metric per commander; writes
  `.audit/coverage/baseline.json`, config_hash-stamped. This is the
  ~1-hour job, run occasionally.
- **`queue`** — reads the census baseline; prints commanders ranked
  ascending by `earned_top30`. This is the coverage-oriented successor to
  `gap_report` (which ranks by EDHREC-NDCG impact and is exhausted).
- **`gate --cohort <name>`** — runs the named cohort (~12) + a **stratified
  200-commander control sample** (stable seed); prints cohort mean
  Δ`earned_top30` vs the pinned baseline and the **full control Δ
  distribution** (so a silent regression outside the cohort cannot hide).
  ~6-minute run — cheap enough to run on every rule change.

Reports to `.audit/coverage/`. Zero scoring-path changes. Joins the no-DB
freshness gate (`tests/bench/test_fixture_freshness.py` pattern) via the
config_hash stamp.

## Cost model (why sampled gate + periodic census)

`engine.page()` ≈ 1.8s/commander. A full ~2,000-commander census ≈ **1
hour** — too slow to run per rule change, so a full-census gate would rot.
Chosen approach (**A** in brainstorm):

- **Census** (the 1-hour job) runs occasionally → the pinned baseline +
  the standing poverty queue.
- **Gate** runs only cohort (~12) + stratified 200-control ≈ **6 min** →
  cheap per-rule validation.

Rejected alternatives: **B** full census every run (~1hr, nobody runs it,
rots); **C** read from the persisted tensor (fixture-scoped to 100/500
commanders — structurally cannot see the universe, which is the whole
point).

## Baseline & lift measurement

- The `census` snapshot **is** the pinned baseline, config_hash-stamped —
  a scoring change (rule add, weight edit) flips the hash and forces a
  re-census.
- A rule's effect = **cohort mean Δ`earned_top30` vs the pinned baseline**.
- The stratified 200-control sample's Δ distribution is the no-regression
  guard.
- Because `earned_top30` is a deterministic count ratio (not a noisy NDCG
  estimate), no bootstrap band is needed: the gate is **cohort Δ
  meaningfully positive AND control Δ ≈ 0**. The full control Δ
  distribution is reported so any silent regression surfaces.
- The exact numeric bar for "meaningfully positive" is deliberately **not**
  fixed in this spec — it is set by the follow-on rule spec **after** the
  first census exists, because the threshold depends on the observed
  `earned_top30` distribution (which cannot be known before the census is
  run). This instrument's job is to produce that distribution; the rule
  spec's job is to pre-register a bar against it.

## Testing

- **Unit 1:** synthetic `page()` results — all-staple → `earned_top30 = 0`;
  a mix of earned + staple rows → correct count; boundary at exactly the
  staple + `rank_bonus` threshold.
- **Unit 2:** the predicate returns the known ~12 cohort members and
  excludes buff / P-T-noise cards (assert Phenax, Tanazir, Arwen present;
  assert a pure-buff "Toughness" card absent).
- **Unit 3:** config_hash stamp round-trips; `queue` orders ascending by
  `earned_top30`; `gate` computes cohort Δ against a fixture baseline.
- **No golden-NDCG assertions anywhere** — that is the point of this
  direction.

## Success criteria for the instrument itself

1. `census` produces a per-commander `earned_top30` for the full legal
   commander universe, and Phenax lands near the bottom of the `queue`
   (validates the metric captures the known-poor case).
2. `gate` runs in ≤ ~6 min and reports cohort Δ + control Δ distribution.
3. Zero scoring-path changes: `bench.py audit --expect-identity` stays
   bitwise-identical before/after this work.
4. The `toughness_payoff` cohort predicate returns the expected ~12
   commanders.

## Follow-on (out of scope here)

A separate spec adds the **toughness-payoff complement rule** (commander
scales a payoff off toughness → high-toughness defensive bodies,
~2,443-card pool). It will be validated by this instrument's `gate` mode,
not by golden NDCG. The flat-IDF ceiling still applies to the ~2,443-card
pool, but for the coverage goal that is acceptable — the flat credit moves
onto the *correct* class (Phenax goes from "Walls by accident" to
"toughness-matters pool on purpose").
