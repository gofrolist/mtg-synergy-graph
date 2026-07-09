---
last_updated: 2026-07-09
module: complement_rules
tags:
  - coverage
  - attack
  - evasion
  - keyword-discriminated
  - flat-credit
  - null-result
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any "attack-reward commander → creatures that attack" rule
    keyed on Attacks/AttackersDeclared triggers
  - Considering keyword-match (evasion, or any other single keyword axis) as
    a general fix for a commander-independent candidate flood
  - Reading the activation-poverty coverage census dead-zone cluster for
    Attacks/AttackersDeclared triggers (94-commander fresh cluster, PR #106)
    and tempted to re-open this gap with a broad keyword pool
  - Interpreting a coverage gate's in-cohort NDCG win — read the
    commander-independence caveat below before treating "keyword-discriminated"
    as sufficient de-flooding
---

# `attack_reward_evasion` rule: DECLINED at pre-registered gates

**Cycle:** spec `docs/superpowers/specs/2026-07-09-attack-reward-evasion-rule-design.md`,
the direct retry of
[[team-anthem-payoff-null-result-2026-07-08]] — that cycle's explicit lesson
was "the payoff must be discriminated, not a whole class... condition the
candidate on *which* keyword/buff [the commander] grants." This cycle
implemented exactly that retry lead for the sibling **attack-reward** dead-zone
cluster (94 commanders with a live `Attacks`/`AttackersDeclared`/
`AttackersDeclaredOneTarget` trigger earning zero) and gated on evasion as the
keyword discriminator. **Verdict: DECLINE.** The working tree stays at the
Task 1–4 hash-neutral state: flag `_ENABLE_ATTACK_REWARD_EVASION = False`, no
`scoring_weights.json` entry, the flag NOT in `ScoringConfigInputs`. The rule,
its flag-aware `RuleGate`, and the `attack_reward` cohort predicate remain
in-tree as standing infrastructure (like `team_anthem_payoff` and
`death_outlet_feeder`); config hash unchanged at `c770b664e626`.

**Does keyword-discrimination beat the flat flood? No.** This is the headline
result of the retry: narrowing the candidate side from "all creatures" to
"evasive creatures" (two IDF tiers, `evasion_hard` ~405 cards /
`evasion_soft` ~3,377 cards) did **not** fix the `team_anthem_payoff`
pathology. It is *smaller* than "all token producers," but it is still one
**commander-independent** class handed identically to every cohort member —
and the primary gate came back more negative than a coin flip, not merely
inside the noise band.

## What the rule was

Keys on a commander with a **team-benefiting attack-reward trigger**
(`_commander_has_team_attack_reward`, `complement_rules/combat.py`): either an
`Attacks`/`AttackersDeclared` trigger scoped to "creatures you control"
(handling the two different DB representations — `valid_filter` for
`Attacks`, `raw_line` `AttackingPlayer: 'You'` for `AttackersDeclared`), or a
self-only attack trigger paired with a team-scoped `PumpAll` (Agrus Kos).
Excludes tribal-subtype and Exalted commanders (the Rafiq displacement
identity from the `attack_payoffs` design). Emits `attack_reward_evasion`
complements for creatures carrying evasion, in two coarse IDF tiers:
`evasion_hard` (Shadow/Horsemanship/Skulk/Fear/Intimidate + self-unblockable
statics) and `evasion_soft` (Flying/Menace). Targets the 67-member
`attack_reward` cohort (60 buildable after the High-Synergy EDHREC filter) —
Agrus Kos, Aloy Savior of Meridian, Caesar Legion's Emperor, Linden the
Steadfast Queen, Inniaz the Gale Force, Miriam Herd Whisperer, and 55 others.

## Why it was surfaced

The activation-poverty census's dead-zone decomposition (94 commanders with a
live trigger port earning zero) split roughly evenly between `Attacks`/
`AttackersDeclared` (28+28+7) and `Phase` (28) shapes. Three existing combat
rules (`combat_enhancer`, `evasion`, `attack_payoffs`) were each read during
design and each deliberately excludes this population — `combat_enhancer`
requires an engine/value effect the plain `PumpAll` triggers lack, `evasion`
only fires on `DamageDone`-to-player triggers (not `Attacks`), and
`attack_payoffs` explicitly excludes commanders with their own attack trigger
(the Rafiq displacement finding). The gap was real: Agrus Kos surfaces only 15
candidates, all scoring 0.0.

## The measurement (flag on, multiplier 1.5 injected in-process, config hash
unaffected since the multiplier lives only in-process for measurement)

| Pre-registered gate | Threshold | Result | Verdict |
|---|---|---|---|
| 1. Primary — in-cohort NDCG@30 uplift | mean Δ **>** +0.0240 (cohort noise half-width, seed 17, mean 0.0758, n=60) | **−0.0445** | **FAIL — hard, negative** |
| 2. Anti-flood — differentiated top-30 contribution | both tiers present, not one flat class | Both tiers present, but **commander-independent**: identical 3,782-card pool (405 hard + 3,377 soft) to every cohort member — zero per-commander discrimination | FAIL (in spirit — see root cause) |
| 3. Guard — golden-500 partitioned no-regression | aggregate Δ within noise **and** no in-cohort cliff worse than noise band | Aggregate **−0.00058** (within ±0.0105, but only via 500-commander dilution); **0 out-of-cohort collateral** (gate confinement works); in-cohort cliffs Caesar −0.16, Reyav −0.12 (worse than the ±0.0240 band) | FAIL (cliffs) |
| 4. Guard — `--collinearity` vs `evasion`/`attack_payoffs`/`combat_enhancer` | not measured | Skipped — dispositive primary failure | not run |
| 5. Guard — `rule_quality_gate --rule attack_reward_evasion` | PASS | not measured | not run |
| 6. Guard — `hidden_gem_hit_rate` | Δ ≥ −0.02 | not measured | not run |

Per the spec's decision rule ("SHIP only if gate 1 clears noise AND gates 2–6
all pass"), gate 1 failing hard and negative is dispositive — the cycle
declines without spending measurement effort on guards 4–6. This is stated
honestly rather than backfilled: guards 4–6 are simply **not measured**.

**Salvage check (hard-tier only, soft flood removed — 405 cards, no `Flying`/
`Menace`): −0.0416** (1 gainer / 37 regressors / 22 flat; Aloy −0.17, Caesar
−0.16 still cliff). This confirms the failure is not a tunable weight or
tier-trim problem — even the premium rare-evasion-only pool regresses the
cohort. Full distribution at the shipped (untrimmed) cell: **5 gainers / 37
regressors / 18 flat**. Worst cliffs: Linden the Steadfast Queen −0.22, Aloy
Savior of Meridian −0.17, Inniaz the Gale Force −0.17, Caesar Legion's Emperor
−0.16. One notable gainer, **Miriam Herd Whisperer +0.24** — its EDHREC list
happens to be evasion-heavy, which is the exception that proves the rule: it
is not a general correlation, it is one commander whose archetype happens to
coincide with the discriminator.

## Why DECLINE — the flood survived discrimination

Two compounding reasons, in order of directness:

1. **The evasion discriminator is commander-independent.** Every
   attack-reward commander in the cohort wants "evasion" equally — Agrus Kos,
   Aloy, Caesar, and Miriam all receive the *identical* 3,782-card pool (405
   hard + 3,377 soft). The candidate side cannot differentiate *between*
   commanders any more than "all token producers" could for `team_anthem`.
   Narrowing the class from "all creatures" (~2,700+) to "evasive creatures"
   (~3,782 — coincidentally similar order of magnitude once soft evasion is
   included) reduces the flood's *size* but not its *shape*: it is still one
   flat per-tier IDF value shared by every cohort member, same pathology as
   [[team-anthem-payoff-null-result-2026-07-08]] and
   [[death-outlet-feeder-null-result-2026-07-07]] ("N candidates share one
   broad IDF key → identical flat contribution = whitelist-equivalent"). The
   3,377-card soft tier (Flying/Menace) dominates by sheer count and displaces
   each commander's real EDHREC Hi-Syn cards; even trimming to the 405-card
   hard-only tier does not save it (−0.0416), because the underlying pathology
   is not "too many cards," it is "the same cards for every commander."

2. **More fundamentally, evasion does not correlate with these commanders'
   real synergy lists.** Their honest EDHREC High-Synergy picks are about
   their specific color/type/archetype identity, not "has an evasion
   keyword." Crediting evasion does not merely fail to help — it *actively
   displaces* the correct cards, the same "reorders EDHREC-aligned cards down
   to make room for the flood" mechanism `team_anthem` exhibited (`hi_syn_loss
   = 0`, damage is via reordering, not removal). The one gainer, Miriam Herd
   Whisperer, is exactly the exception that demonstrates this: her own
   EDHREC list happens to already skew evasion-heavy, so the flood
   accidentally lines up with her real archetype instead of displacing it.

**The retry lead is therefore falsified for this cohort.** "Discriminate by
keyword" was the load-bearing hypothesis carried forward from
`team_anthem_payoff`'s DECLINE — the idea that conditioning the candidate on
*which* keyword the commander's payoff rewards would turn a flat class into
diverse, commander-specific synergy. It does not: the honest complement for
"attack-reward" genuinely **is** a broad class (evasive creatures, full stop),
and broad classes flood regardless of how the class boundary is drawn. Unlike
`team_anthem` (where the retry lead was untested), this cycle actually ran the
retry and it failed — a stronger, more conclusive null-result than its
predecessor.

## Standing infrastructure left in-tree (flag off, hash-neutral)

- `complement_rules/combat.py`: `_commander_has_team_attack_reward` (gate),
  `_find_attack_reward_evasion` (emitter), `_ENABLE_ATTACK_REWARD_EVASION =
  False`, `_ATTACK_REWARD_TRIGGER_EVENTS`.
- `penalties.py`: `_bulk_load_evasion_hard_cards` / `_bulk_load_evasion_soft_cards`
  + the two corresponding `CandidateCache` fields.
- `registry.py`: flag-aware coarse `RuleGate("attack_reward_evasion", …)` —
  reports no coverage while the flag is off, so `gap_report` /
  `rule_quality_gate` continue to see the attack-reward signature as
  unserved.
- `core.py`: emitter wired into `find_all_complements` (emits nothing while
  the flag is off).
- `bench/cohorts.py`: `attack_reward(conn)` predicate (67 members, 60
  buildable) + `coverage_report._COHORT_DISPATCH["attack_reward"]`.
- `tests/fixtures/golden_set_attack_reward.json` +
  `scripts/bootstrap_attack_reward_fixture.py` + the CLAUDE.md noise-band
  note (mean 0.0758, half-width 0.0240, seed 17).

Self-activates for a retry cycle if the flag flips; a retry must first find a
discriminator that is **commander-specific**, not merely a narrower flat
class — see lessons below.

## Lessons for the next coverage cycle

- **Narrowing the candidate class is not the same as discriminating between
  commanders.** A discriminator only breaks the flood if two different
  cohort members get *different* candidate sets (or at least different
  weightings) from it. "Which keyword the payoff rewards" sounded
  commander-specific in the `team_anthem` retry lead, but for attack-reward
  it collapses to one keyword axis (evasion) shared by the whole cohort —
  the discriminator varies by *rule design*, not by *commander*. A real fix
  needs a per-commander axis: e.g. color identity, existing archetype
  signal, or a second gate that only fires when the candidate's evasion type
  matches something else specific to that commander.
- **A partial-tier salvage check is worth running before declining on the
  full cell.** It took one extra measurement (hard-tier-only, −0.0416) to
  rule out "just trim the soft tail" as a fix, closing off the most obvious
  retry-of-the-retry before it could be proposed.
- **Aggregate golden-500 within-noise is, again, dilution, not safety.** The
  −0.00058 aggregate looks clean at 500 commanders but hides Caesar −0.16 and
  Reyav −0.12 — the exact "concentrated in-cohort cliffs hidden by a diluted
  aggregate" pattern from `team_anthem`. Guard 3 (partitioned by cohort
  membership) is doing real work catching this a second time; any future
  coverage cycle's guard table must keep this partition, not just the
  aggregate number.
- **The gate-confinement discipline (tribal/Exalted exclusion) worked
  perfectly again.** Zero out-of-cohort collateral regressors — the
  commander-side gate is airtight even though the candidate-side flood is
  not. This distinction (gate confinement vs. candidate discrimination) is
  worth keeping conceptually separate in future designs: getting the *first*
  right does not solve the *second*.
- **Two null-results on two different candidate-side floods (all token
  producers, all evasive creatures) now converge on the same conclusion:**
  when a payoff's honest mechanical complement is a broad card class, IDF
  weighting and keyword-narrowing do not manufacture per-commander
  discrimination that isn't there. The next coverage attempt on a
  dead-zone cluster should budget a cheap pre-check — does a sample of the
  cohort's EDHREC Hi-Syn lists actually contain members of the candidate
  class at materially different rates? — before investing in a full
  implementation cycle.
