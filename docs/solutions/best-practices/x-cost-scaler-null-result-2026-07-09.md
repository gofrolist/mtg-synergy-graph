---
last_updated: 2026-07-09
module: complement_rules
tags:
  - coverage
  - x-cost
  - xpaid
  - mana-economics
  - mechanical-discrimination
  - commander-independence
  - flat-credit
  - null-result
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any "X-cost commander → mana doublers / cost reducers / ramp"
    rule keyed on a scales_with.xPaid commander port
  - Considering a MECHANICALLY-principled complement (a real port-to-port
    interaction, not archetype affinity) and assuming that alone de-floods —
    read the commander-independence caveat below
  - Reading gap_report's scales_with.xPaid[*] proposal (#2) and tempted to
    serve it with a mana-economics candidate pool
  - Interpreting a coverage gate's in-cohort NDCG result where a MINORITY of
    the cohort gains and the mean is dragged negative by displacement
---

# `x_cost_scaler` rule: DECLINED at pre-registered gates

**Cycle:** spec `docs/superpowers/specs/2026-07-09-x-cost-scaler-rule-design.md`,
plan `docs/superpowers/plans/2026-07-09-x-cost-scaler-rule.md`. Surfaced by
`gap_report` proposal #2 (`scales_with.xPaid[*]`, 98 legal legendary commanders
at 0% rule activation). The *third* consecutive dead-zone-payoff cycle to
DECLINE, after [[team-anthem-payoff-null-result-2026-07-08]] (flat per-creature
flood) and [[attack-reward-evasion-null-result-2026-07-09]] (keyword-discriminated
flood). **Verdict: DECLINE.** The working tree stays at the Task 1–4
hash-neutral state: flag `_ENABLE_X_COST_SCALER = False`, no `scoring_weights.json`
entry, the flag NOT in `ScoringConfigInputs`. The rule, its flag-aware
`RuleGate`, and the `x_cost_scaler` cohort predicate + fixture remain in-tree as
standing infrastructure; config hash unchanged at `c770b664e626`.

**Does a MECHANICALLY-principled discriminator beat the flat flood? No — and
this is the sharpest lesson of the three cycles.** This cycle was explicitly
designed to escape the [[attack-reward-evasion-null-result-2026-07-09]] failure
mode. Evasion was diagnosed there as *archetype affinity* — "an evasive body
that happens to fit a go-wide deck" — a candidate class whose membership does
not correlate with the target commanders' EDHREC lists. So this cycle chose a
complement that is a **true port-to-port economic interaction**: a mana doubler
or a generic cost reducer literally *changes the value of the commander's own
`xPaid` port* by letting it pay a bigger `X`. The clean pools were also tiny
(3 doublers + 49 reducers = 52 cards), the opposite of a flood by size. It
still DECLINED, more negative than the noise band. Mechanical validity of the
complement is **necessary but not sufficient**. The binding property all three
DECLINES share is **commander-independence**, explained below.

## What the rule was

Keys on a commander with an X-cost ability (`_commander_has_x_cost_ability`,
`complement_rules/density.py`): a pure predicate, True iff any port is
`port_type='scales_with'` with `event_class='xPaid'`. No tribal/Exalted
exclusion (X-cost economics is orthogonal to creature type). Emits
`x_cost_scaler` complements in two IDF tiers, scanned strong-first with dedup:

- **`mana_double`** (3 cards): `replacement.ProduceMana` whose `ReplaceWith ∈
  {ProduceTwice, ProduceThrice}` — Mana Reflection, Nyxbloom Ancient, Virtue of
  Strength. (The gap sketch's "pool 11" was mostly mana-*denial* stax —
  Contamination, Damping Sphere — and color-warpers, excluded by the
  `ReplaceWith` filter.)
- **`cost_reduce_generic`** (49 cards): `ReduceCost` with `Type=Spell`, a broad
  `ValidCard` (not `Card.Self`, not tribe/type-narrow), numeric `Amount≥1` —
  Baral, Goblin Electromancer, Helm of Awakening, Arcane Melee, Jace's Sanctum.

Targets the 98-member `x_cost_scaler` cohort (87 buildable after the
High-Synergy EDHREC filter) — Zaxara the Exemplary, Gadwick the Wizened,
Verdeloth the Ancient, Rocco Cabaretti Caterer, Chatterfang, Gix, Polukranos,
and 80 others.

## The measurement (flag on, multiplier 1.5 injected in-process; the injection
flips the in-process config hash to `35deff44aa21`, so the on-disk hash and all
pins stay valid at `c770b664e626`)

| Pre-registered gate | Threshold | Result | Verdict |
|---|---|---|---|
| 1. Primary — in-cohort NDCG@30 uplift | mean Δ **>** +0.0261 (cohort noise half-width, seed 17, mean 0.0894, n=87) | **−0.025** (11 gainers / 61 regressors / 15 flat) | **FAIL — negative** |
| 2. Anti-flood — differentiated top-30 contribution | per-commander discrimination, not one flat class | **FAIL**: every cohort member gets the **identical 52-card pool** (3 `mana_double` + 49 `cost_reduce_generic`). Zaxara, Marath, Greel, Verdeloth all receive the same 52 credits — zero per-commander discrimination | FAIL |
| 3. Guard — golden-500 partitioned no-regression | aggregate within noise, no out-of-cohort collateral | **0 out-of-cohort collateral by construction** (the flag-aware gate fires only on `scales_with.xPaid` ports; non-cohort commanders lack them). In-cohort cliffs Greel −0.245, Marath −0.183, Mimeoplasm −0.170 | not the failing gate (confinement holds) |
| 4. Guard — `--collinearity` vs `cost_reduction_target`/`mana_doubler`/`scaling` | not measured | Skipped — dispositive primary failure | not run |
| 5. Guard — `rule_quality_gate --rule x_cost_scaler` | PASS | not measured | not run |
| 6. Guard — `hidden_gem_hit_rate` | Δ ≥ −0.02 | not measured | not run |

Per the spec's decision rule ("SHIP only if gate 1 clears noise AND gates 2–6
all pass"), gate 1 failing negative is dispositive; guards 4–6 are honestly
**not measured** rather than backfilled.

**Salvage check (doubler-only, the 49 reducers dropped — 3 cards, the tiniest,
highest-signal, most-mechanically-valid tier):** in-cohort mean Δ **−0.0121**
(8 gainers / 63 regressors / 16 flat) — **still negative, still fails**. Even
three near-perfect X-deck staples, credited identically to all 87 commanders,
displace more real EDHREC picks than they add. Pool size is not the lever.

## Root cause — commander-independence (the property all three DECLINES share)

The rule credits a **fixed pool identically to every commander** that carries
the `xPaid` port. It cannot tell *which* X-cost commander actually wants ramp.
The measurement splits the cohort cleanly along this axis:

- **The 11 gainers are the true, ramp-hungry X-decks** whose EDHREC lists *are*
  full of doublers/reducers: Polukranos +0.141, Mutagen Man +0.041, Myra +0.024,
  **Zaxara the Exemplary** +0.020, Verdeloth +0.018, Verazol +0.008. For these,
  the mechanical interaction is real *and* matches EDHREC — the rule works.
- **The 61 regressors are commanders whose `xPaid` is incidental** — one minor
  X-ability on a card whose EDHREC identity is something else entirely: Greel
  −0.245 (a discard/reanimator), Marath −0.183 (a +1/+1-counter/token
  commander), Mimeoplasm −0.170 (a graveyard toolbox), Alquist Proft −0.122.
  Crediting them the same 52 ramp cards pushes their real EDHREC staples out of
  the top-30.

Because the cohort contains **more incidental-`xPaid` commanders than
dedicated-ramp commanders**, the mean is negative regardless of pool size or
mechanical validity. A mechanically-valid complement that is
commander-independent still floods — it just floods *correctly for the
minority and wrongly for the majority*, netting negative.

**The unifying lesson across `team_anthem_payoff`, `attack_reward_evasion`, and
`x_cost_scaler`:** all three narrowed the candidate side (token producers →
evasive creatures → mana economics) but left it **commander-independent** — a
single class handed to every cohort member. De-flooding requires the complement
to **vary with the specific commander** (which keyword *this* commander grants,
which subtype *this* commander pays, which mana color *this* commander's `X`
consumes), not merely to be a smaller or more mechanically-valid fixed class.
Necessary-and-jointly-sufficient de-flooding is *bounded* **AND**
*commander-dependent*. This cycle proves that mechanical-validity alone (the one
axis evasion lacked) does not substitute for commander-dependence.

## The retry lead (if this gap is re-opened)

The 11 gainers show the signal is real for dedicated X-decks. A commander-
**dependent** gate would keep only cohort members whose `xPaid` is a *primary*
payoff (e.g. the card's headline ability, or `xPaid` on a low-CMC body whose
identity is the X-spell), dropping the incidental-`xPaid` majority. That is a
*gate-narrowing* design (mechanically detecting "X is this commander's point"),
not a candidate-side change — and it must clear a whitelist-equivalence check,
since selecting the exact commanders that already gain is a disguised whitelist.
Not attempted here; noted for a future cycle.

## What stays in-tree

Standing, hash-neutral infrastructure (flag off): the gate
`_commander_has_x_cost_ability`, the emitter `_find_x_cost_scaler`, the two
`CandidateCache` mana-economics pools (skip-loaded when the flag is off), the
flag-aware `RuleGate("x_cost_scaler", …)`, the `x_cost_scaler` cohort predicate,
the `bootstrap_x_cost_scaler_fixture.py` + pinned fixture, and the noise band.
Zero scoring-path change; `--expect-identity` PASS; config hash `c770b664e626`.
