# Requirements — gy_fuel_feeder extension: Surveil≥3 + DigUntil-to-graveyard self-mill

- **Date:** 2026-07-03
- **Status:** declined — DECLINED at Stage 0 (user-accepted 2026-07-03). Null recorded in `docs/solutions/best-practices/gy-fuel-vocab-expansion-null-result-2026-07-03.md`. No scoring code written.
- **Scope:** Lightweight (narrow, well-bounded extension of one existing gated rule)
- **Origin:** `/ce-brainstorm` on "vocabulary expansion for top UNKNOWN port shapes."
  Pressure-tested down from the original broad framing to the one
  mechanically-honest, low-regression subset (user selected "Narrow
  gy_fuel extension"). See Non-Goals for what was cut and why.

## Stage-0 measurement result (2026-07-03) — recommend DECLINE-before-build

The doc's own Open Question (pre-flight candidate count) was promoted to
a **blocking Stage-0 gate (R0)** after `ce-doc-review` (4 personas). Run
with the *corrected* filters the review surfaced, the pool does not
survive as archetype-appropriate self-mill fuel:

| Tier | Naive count | Corrected count | Why it shrinks |
|---|---|---|---|
| Surveil ≥3 | 17 | 17 (but **optional-fill**) | Surveil lets you bin *any number incl. 0* — the ≥3 gate is decorative; the 17 are mostly card-selection cantrips (Connive, Otherworldly Gaze, Taigam's Scheming, Plan the Heist) — the exact flood shape ≥3 was meant to block |
| DigUntil→GY self | 13 | **4** | Target-encoding-aware filter (`ValidTgts:Player` / `IsCurse` / `Defined∈{TargetedController, TriggeredTarget, Player.Opponent…}` / `AILogic:DontMillSelf`) rejects 16/20 as opponent-mill or combo (Balustrade Spy, Mirko Vosk, Undercity Informer, Hermit Druid) |

**Corrected net-new supply: ~21 cards**, of which the DigUntil
contribution (4) is combo/value (Gamekeeper, Mirror-Mad Phantasm), not
incremental fuel, and the Surveil 17 are optional cantrips that a player
surveils to *keep* good cards, not to fill the yard. Two mechanical
holes in the original premise (Surveil optionality; DigUntil target
encoding) mean the corrected pool re-introduces the exact cantrip-flood
and wrong-target failure modes R2 exists to prevent. Combined with a
24-commander hard-zero-regression bar over a near-zero-EV pool (24
chances to trip a noise-band regression, no upside), the kill-test is
structurally biased to DECLINE for reasons unrelated to the idea's
merit.

**Recommendation:** DECLINE before writing scoring code; record the null.
This is a successful cycle outcome — the cheap Stage-0 measurement did
exactly its job. The requirements below are retained as the corrected
record and as the spec *if* the user chooses to override and build anyway.

## Problem

The `gy_fuel_feeder` rule
(`src/mtg_synergy_graph/complement_rules/utility/resource_feeders.py`)
fires on commanders whose signature ability pays by **exiling cards
from any graveyard** (`cost.exile_from_grave`, `cost_target='any'` —
~24 commanders: Araumi, Osgir, Aphemia, Gorex, Kethis, Varina …). Their
archetype-defining payoff is **self-mill**: more cards in your graveyard
→ more activations. The rule currently feeds them exactly one candidate
shape — `effect.Mill` with `Defined:'You'` and `NumCards≥3`.

Two other Forge effect shapes **also fill your own graveyard** but are
not yet consumed by this rule (they surface in `bench.py audit
--unknowns` as `effect.Surveil` / `effect.DigUntil`):

- **Surveil N** — look at top N, put any number into *your* graveyard.
  Inherently self-graveyard-filling.
- **DigUntil … RevealedDestination='Graveyard'** — reveal from library
  until a condition, revealed cards go to *a* graveyard (the "mill until
  X" shape).

## Corrected mechanical scope (verified against `data/synergy.db`)

The original framing ("card-selection / graveyard-fuel cluster: Dig 828,
Mill 503, Scry 385, Surveil 200, DigUntil 149") does not survive
inspection. Corrections, with counts:

| Shape | Raw count | Genuinely self-GY-filling | Reason |
|---|---|---|---|
| `effect.Mill` (Defined:You, N≥3) | 503 | already consumed | in the rule today |
| `effect.Surveil`, Amount≥3 or X/Y/Z | 200 | **17** | 179 Surveil only 1–2 → below the anti-cantrip-flood threshold the rule already enforces |
| `effect.DigUntil`, RevealedDestination=Graveyard | 149 | **~10–20** | 129/149 revealed→Library (tutors) or Exile; some of the 20 GY-variants are opponent-mill, not self |
| `effect.Dig`, `effect.Scry` | 828 / 385 | **0** | card selection / impulse — do **not** touch the graveyard |

**Net addressable candidate pool: ~25–35 cards.** Not the ~1,300-card
headline. This is the load-bearing number for sizing expectations.

## Requirements

**R0 — Stage-0 pre-flight is a BLOCKING gate (done 2026-07-03).** Before
any code, count per-commander net-new scored-eligible candidates after
the *corrected* self-only filters. If the median gated commander gains
<1, DECLINE before writing scoring code. **Status: run — corrected
supply is ~21 cards (17 optional-fill Surveil + 4 combo/value DigUntil);
recommendation DECLINE.** R1–R5 are contingent on R0 clearing, which it
did not.

**R1 — Add two candidate tiers to `gy_fuel_feeder`, same commander gate.**
The commander gate is unchanged (`cost.exile_from_grave`,
`cost_target='any'`). Add:
- `gy_fuel_surveil`: `effect.Surveil` (count in the `Amount` field, NOT
  `NumCards` — the shared `_NUM_CARDS_RE` matches 0 Surveil rows; a
  distinct `Amount` regex is required). **Caveat (unresolved):** Surveil
  is *optional* — the player may bin 0 cards — so `Amount≥3` does NOT
  encode "fills GY by ≥3" the way forced Mill does. The Mill anti-flood
  guarantee does not transfer; a stronger self-GY signal (e.g. an on-card
  graveyard payoff) must be co-required, or the tier admits pure
  selection cantrips.
- `gy_fuel_dig_gy`: `effect.DigUntil` with `RevealedDestination='Graveyard'`.
  Self-targeting must be enforced *positively* — reject `ValidTgts:Player`,
  `IsCurse:True`, `AILogic:DontMillSelf`, and `Defined ∈ {TargetedController,
  TriggeredTarget, TriggeredDefendingPlayer, TriggeredPlayer,
  Player.Opponent, Player.EnchantedBy, EachOpponent}`. A `NOT LIKE
  '%Opponent%'` filter is insufficient (only 7/20 opponent-mills carry
  the literal token). Corrected survivor set: **4 cards**.

**R2 — Preserve the anti-flood threshold.** The ≥3 / self-only filters
are the encoded fix for the −0.093 Osgir / −0.436 Ultimecia cantrip-flood
regression (see rule docstring). **But the analogy is forced-mill-only:**
because Surveil is optional (R1 caveat), the ≥3 threshold does not
re-derive the anti-flood guarantee for the Surveil tier — this is the
core reason the Stage-0 recommendation is DECLINE.

**R3 — Dedup against the existing self-mill tier.** `effect.Surveil` and
`effect.DigUntil` are disjoint event classes from `effect.Mill`, so the
name-overlap with `gy_fuel_self_mill` is measured at **0** — R3 is a
defensive no-op here, not load-bearing. Keep the `seen`-set tier pattern
(`lifegain_feeder` / `land_bounce_feeder`) for hygiene only.

**R4 — Kill-test gate, bars pinned BLIND before measurement** (only
reached if R0 clears — it did not). Per
`memory/feedback_audit_every_change.md`, this is a scoring-path change
and must pass `bench.py audit`. Pin the routing table before running:
- **SHIP** if aggregate NDCG@30 delta **> 0** (strictly positive, to
  match the INCONCLUSIVE clause) AND ≥1 gated commander shows a positive
  per-commander delta with no commander regressing beyond noise.
- **DECLINE** if aggregate delta < 0 OR any gated commander regresses
  materially (the cantrip-flood failure mode).
- **INCONCLUSIVE → DECLINE** if the delta is indistinguishable from zero.
- **Scope the audit to the commanders that actually gain net-new
  candidates**, not all ~24 — a hard zero-regression bar over 24
  commanders with near-zero upside is structurally biased to trip a
  noise-band regression and auto-DECLINE for reasons unrelated to merit.
- **On SHIP, re-pin:** a Python-helper edit to `resource_feeders.py` does
  NOT flip `compute_config_hash`, so refresh the baseline with
  `uv run scripts/bench.py audit --repin --yes` or every later audit
  reports the shipped change as a live-vs-stale-pin delta.

**R5 — Rule quality gate.** Run `scripts/rule_quality_gate.py --rule
gy_fuel_feeder` after the change to confirm neither new tier exhibits
vacuum-fill / flat-noise pathology.

## Success criteria

- The extension either SHIPs with a measurable, regression-free NDCG
  gain on the gy_fuel commander cohort, or is cleanly DECLINED with the
  null recorded. Both are successful cycle outcomes.
- **Regression bar (reconciled with R4):** no gated commander regresses
  *beyond the bench.py audit noise band*. This is the operational
  threshold; the earlier "zero regression, hard bar" phrasing was
  absolute and contradicted R4's "materially / beyond noise" — R4's
  noise-relative bar governs.
- No broadening of the commander gate — the change adds *supply* shapes
  to an already-narrow archetype, it does not widen *demand*.

## Non-Goals (explicitly cut during pressure-test)

- **`effect.Dig` (828) and `effect.Scry` (385)** — card selection, not
  graveyard fuel. Generic goodstuff; the density/quality gates and prior
  reverts show this shape regresses. Out.
- **`static.ReduceCost` (511)** — universal cost reduction wanted by
  every spell deck = definitional flat-noise axis. Out. (`AlternativeCost`,
  147, is narrower and could be a *separate* future brainstorm, not this
  one.)
- **Broad vocabulary / typed-port-graph migration.** This is a Python-
  helper rule edit. Adding these shapes to the typed `NODE_KINDS`
  vocabulary is orthogonal infra and not required to consume them (the
  rule reads `card_ports` directly). Out of scope.
- **Archetype-payoff / subtype-link detection** (the larger open lever
  from this session's forensics). Deliberately deferred to its own
  Stage-0-gated brainstorm — it is cliff-prone tribal territory and must
  not be smuggled in here. See
  `memory/project_no_rules_archetype_gap.md`.

## Open questions for planning

- **Is the pool large enough to register?** Consider a pre-flight count:
  for each of the ~24 gated commanders, how many net-new candidates do
  the two tiers add after dedup? If several commanders gain 0 net
  candidates, the expected NDCG movement is ~0 and the honest call may
  be to DECLINE before writing code. A 10-minute measurement de-risks
  the whole build.
- **Surveil opponent-targeting:** Surveil is always self (you surveil
  your own library), so no opponent filter is needed for the Surveil
  tier — confirm during implementation. DigUntil *does* need the
  opponent-mill exclusion.
