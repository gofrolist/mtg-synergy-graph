---
last_updated: 2026-07-09
module: complement_rules
tags:
  - coverage
  - aristocrats
  - sacrifice
  - death-trigger
  - commander-independence
  - pool-independence
  - flood
  - null-result
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any "sacrifice/aristocrats commander → death-payoffs / recursive
    fodder" rule keyed on a sac-outlet or death-trigger commander port
  - Tempted to believe a TIGHT, mechanically-principled, EDHREC-matching
    candidate pool escapes the flood — read the pool-independence section
  - Reading gap_report / demand_coverage's sacrifice_fodder or graveyard_bodies
    demand and tempted to serve it with a death-value candidate pool
  - Designing a rule whose GATE is commander-dependent but whose CANDIDATE POOL
    is a single fixed class handed to every gated commander
---

# `aristocrats_death_bridge` rule: DECLINED at pre-registered gates

**Cycle:** spec `docs/superpowers/specs/2026-07-09-aristocrats-death-bridge-rule-design.md`,
plan `docs/superpowers/plans/2026-07-09-aristocrats-death-bridge-rule.md`. This
was the deliberate, best-grounded retry after
[[calibration-levers-exhausted-2026-07-09]] ruled out weight-tuning and
staple-reform and pointed at commander-dependent coverage as the only lever with
headroom. **Verdict: DECLINE.** The working tree stays flag-off, hash-neutral:
`_ENABLE_ARISTOCRATS_DEATH_BRIDGE = False`, no `scoring_weights.json` entry, the
flag NOT in `ScoringConfigInputs`; config hash unchanged at `c770b664e626`;
`--expect-identity` PASS. The rule, its flag-aware `RuleGate`, the two
`CandidateCache` pools, the `aristocrats` cohort predicate + fixture + band
remain in-tree as standing infrastructure.

**This is the fourth consecutive dead-zone-payoff DECLINE**, after
[[team-anthem-payoff-null-result-2026-07-08]] (flat per-creature flood),
[[attack-reward-evasion-null-result-2026-07-09]] (keyword-discriminated flood),
and [[x-cost-scaler-null-result-2026-07-09]] (mana-economics flood). It is the
**closest** (in-cohort mean ΔNDCG@30 = **−0.0110** vs the prior cycle's −0.025)
and the most carefully built — and its failure sharpens the unifying lesson into
its final, likely-terminal form.

## What the rule was (the strongest version we could build)

Every prior critique was addressed:

- **Commander-dependent GATE** (unlike the flat cycles): fired only for
  `_commander_is_aristocrats` — a legal legendary creature with a creature
  sacrifice outlet (`cost`/`sacrifice`, non-self) OR a death-trigger payoff
  (`trigger`/`ChangesZone` Battlefield→Graveyard). 291 commanders.
- **A genuine mechanical bridge**: the engine substrate bridges `sacrifice →
  Sacrificed` but had no `sacrifice → ChangesZone(bf→grave)` equivalence, so
  aristocrats death-payoffs (Blood Artist, Zulaport) sat *unranked* for
  sac-outlet commanders. The rule supplied that missing bridge.
- **A TIGHT, EDHREC-matching pool** — not a flood by size. Two IDF tiers:
  `death_payoff` (**218** cards: a death trigger whose own `execute_ref`
  resolves to a value effect, watching a *creature* dying broader than self,
  opponent-excluded — Blood Artist, Bastion, Pitiless Plunderer) and
  `recursive_fodder` (**131** cards: Undying/Persist + self-returning bodies —
  Gravecrawler-likes, Reassembling Skeleton). Validated to include every
  canonical payoff and exclude one-shots, ramp, and reanimators.

Inspecting Yawgmoth's missed EDHREC cards had confirmed the pool *is* where his
picks live. Everything a mechanical scorer could ask for was true.

## The measurement (flag on, 1.5 multiplier injected in-process → in-process
hash flips; on-disk hash + pins stay `c770b664e626`)

| Pre-registered gate | Threshold | Result | Verdict |
|---|---|---|---|
| 1. Primary — in-cohort NDCG@30 uplift | mean Δ **>** +0.0130 (cohort noise half-width, seed 17, mean 0.0688, n=274) | **−0.0110** (80 gainers / 110 regressors / 84 flat) | **FAIL — negative** |
| 2. Anti-flood — differentiated top-30 | per-commander discrimination | **FAIL**: 13 cohort members' entire top-30 flooded with death cards → live NDCG **0.000** (Caesar, Cleopatra, Elder Arthur Maxson, The Ever-Changing 'Dane, Reyhan, Varolz, Torsten, …) | FAIL |
| 3. Guard — golden-500 no-regression | no out-of-cohort collateral | 0 collateral by construction (flag-aware gate fires only on aristocrats ports) | not the failing gate |
| 4–6. Guards — collinearity / rule_quality / hidden_gem | — | not measured (dispositive primary failure) | not run |

Per the spec's decision rule (SHIP only if gate 1 clears noise AND 2–6 pass),
gate 1 failing negative is dispositive; guards 4–6 are honestly **not measured**.

**The gainers are real.** The top gainers are exactly the true aristocrats whose
EDHREC identity *is* the death engine: Jerren +0.136, Judith +0.087, Agent Venom
+0.084, Athreos +0.080, Erebos +0.074, Elenda +0.065, Edgar +0.063, Dina +0.061.
For these ~80 commanders the bridge works beautifully. But 110 regress.

## Root cause — pool-independence (the lesson's final form)

A diagnostic split by gate reason:

- **has a death-trigger payoff** (n=200, the "truest" aristocrats): mean Δ
  **−0.0078** — still negative.
- **sac-outlet-only** (n=74): mean Δ **−0.0197** — worse.

Narrowing the gate to death-trigger commanders *helps* (−0.0078 vs −0.0197) but
does not cross the bar, and the 13 flood-to-zero casualties span **both** subsets
(Cleopatra/Reyhan/Torsten have death triggers; Caesar/Varolz do not). The flood
is **not separable by any mechanical gate property.**

The first three DECLINES were diagnosed as *commander-independence*: a fixed pool
credited identically to every cohort member. This cycle fixed that **at the
gate** — only aristocrats fire the rule — and still failed. The residual, and the
terminal form of the lesson, is **pool-independence**:

> The GATE is commander-dependent (which commanders fire the rule), but the
> POOL is not (every gated commander gets the SAME 218+131 death-engine cards,
> modulo color identity). Reyhan (a +1/+1-counter deck that happens to sacrifice)
> and Yawgmoth (a pure aristocrats engine) both receive the identical death-card
> flood — correct for Yawgmoth, top-30-destroying for Reyhan.

De-flooding requires the **pool itself** to vary per commander: *which specific
death-payoffs does THIS commander's deck actually want.* But that discrimination
is not a mechanical property of the cards — it is EDHREC popularity /
deck-identity, precisely the non-mechanical signal the scorer refuses to use by
design (`memory/feedback_edhrec_not_goal`, the `rank_bonus` ablation). A pure
port-to-port scorer can make the GATE commander-dependent but cannot make the
POOL commander-dependent without importing popularity.

**Consequence for future cycles:** the dead-zone-payoff class (go-wide / anthem /
aristocrats / X-cost — commanders whose complement is "a wide class of enablers")
appears **fundamentally unreachable** by a pure mechanical scorer at aggregate
NDCG. Four cycles — flat, keyword-discriminated, mana-economic, and
aristocrats-bridged — converge on the same wall. This likely **closes the
new-rule line** for these commanders. Effort is better spent on commanders whose
EDHREC complement is a *specific mechanical partner* (a named combo, a
subtype-supply match — the shape of the 2026-07-07 subtype-supply rule that
DID ship) rather than a broad enabler class.

## The retry lead (if ever re-opened)

The only path that could work is a pool that varies per commander by a
*mechanical* key the commander itself supplies — e.g. a death payoff that
produces the *specific* token/counter/color the commander's own ability
consumes, joined per-commander. That is a much narrower "commander's death
trigger feeds commander's own second ability" circuit, not a broad death-engine
pool. Not attempted; likely a handful of commanders at best. It must clear a
whitelist-equivalence check.

## Post-review correction (2026-07-09, PR #110 high-effort code review)

A recall-biased 8-angle review found the **candidate-pool loaders carried the
same exact-match-zone bug** that the Task 4 review had already fixed in the
cohort predicate (commit 0538bbe) — the fix was applied to `cohorts.py` but not
to the two `penalties.py` loaders. Both `_bulk_load_aristocrats_death_payoff_cards`
(`zone_destination = 'Graveyard'`) and `_bulk_load_aristocrats_recursive_fodder_cards`
(`zone_origin = 'Graveyard'`) used exact-equality, silently dropping comma-list
zone values ('Graveyard,Exile'): tier-1 228→**229** (added Reyhan, Syr Vondam),
tier-2 138→**150** (added Bramble Familiar, Bruna, Danitha, +9). The review also
found the opponent-exclusion only tested the `OppCtrl` substring, admitting
`OppOwn`-scoped triggers (Grim Feast, Patron of the Nezumi); it was widened to
the canonical opponent markers (`OppOwn`/`Player.Opponent`/`+Opp`) with a
your/any rescue. Both loaders now use `instr()` (mirroring the cohort), and
comma-list-zone + `OppOwn`-exclusion + cross-tier-dedup regression tests were
added. **Re-measured on the corrected 229/150 pools the DECLINE holds — in-cohort
mean ΔNDCG@30 = −0.0111** (was −0.0110; 79 gain / 111 reg / 84 flat). The +14
pool cards leave the aggregate unmoved, which *reinforces* the pool-independence
root cause: a fixed pool floods regardless of its exact contents. Hash-neutral
throughout (`c770b664e626`, `--expect-identity` PASS — the loaders are flag-off
candidate-side).

## What stays in-tree

Standing, hash-neutral infrastructure (flag off): `_commander_is_aristocrats`,
`_find_aristocrats_death_bridge`, the two `CandidateCache` pools (skip-loaded
when off), the flag-aware `RuleGate`, the `aristocrats` cohort predicate (SQL
verified set-equal to the Python gate via a drift-guard test), the
`bootstrap_aristocrats_fixture.py` + pinned fixture, and the noise band
(half_width 0.0130). Zero scoring-path change; `--expect-identity` PASS; config
hash `c770b664e626`.
