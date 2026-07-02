---
last_updated: 2026-07-02
module: scoring
title: Calibration track declined — flood-as-archetype is irreducible at the weight layer (null result)
tags:
  - scoring
  - calibration
  - monoculture
  - null-result
  - audit-gate
  - escalation
  - plan-2026-07-02-002
problem_type: best_practice
resolution_type: reference
applies_when:
  - Considering ANY uniform demotion of flat/density family weights (haircuts, tiers, pool scaling, or combinations)
  - Starting the C1 lift-normalization / generic-glue design cycle (this doc is its evidence base)
  - Re-running the Unit 3 rank_bonus ablation (its re-run condition assumed shipped calibration)
created: 2026-07-02
plan_ref: docs/plans/2026-07-02-002-fix-scoring-flaw-remediation-plan.md
---

# Calibration track declined — flood-as-archetype is irreducible at the weight layer

Plan 2026-07-02-002 Units 4–6 probed three mechanisms against the
monoculture/OUTRANKED flaw (46.2% of misses). Every configuration was
measured on the 500-cmdr fixture against pre-committed gates
(SHIP ≥ +0.010 or goal-aligned alternative; DECLINE on any
per-commander cliff < −0.05). None ships; the plan's escalation rule
(≥2 calibration DECLINEs → the OUTRANKED lever moves to the C1
lift-normalization design cycle) has fired.

## The full configuration table (500-cmdr fixture, post-Unit-2 baseline)

| Configuration | mean NDCG Δ | gem Δ | cliffs < −0.05 | worst |
|---|---:|---:|---:|---|
| U4-A concave blanket | +0.0005 | +0.0007 | 6 | Kodama −0.194 |
| U4-B concave flat-only | +0.0004 | ~0 | 3 | Edgar −0.103 |
| U4-C concave flat-minus-tribal | −0.0003 | +0.0013 | 1 | Rionya −0.052 |
| U5 tier, body 0.15 | +0.0043 | — | 11 | Magda −0.165 |
| **U5 tier, body 0.30** | **+0.0051** | **+0.0241** | **5** | Nissa −0.107 |
| U5 tier 0.30 + fuel exemption | +0.0017 | +0.0061 | 3 | (gains gutted: Marrow +0.254→0) |
| U5 tier, body 0.40 | +0.0044 | — | 4 | (gains lost: Chatterfang −0.003) |
| U6 pool-scaling alone | −0.0008 | +0.0139 | 15 | Kess −0.233 |
| U6 pool + tier joint | +0.0036 | +0.0297 | 15 | Kess −0.233 |
| U6 pool(tribal_body) + tier | +0.0049 | +0.0185 | 8 | Magda −0.149 |

## The structural finding (three probes, one mechanism)

Every configuration's cliffs are commanders whose EDHREC top-30 IS the
flooding family, in one of three sub-shapes:

1. **Tribal-as-archetype** (Edgar, Lathril, Hakbal): the tribe's
   payoff-dense small pools reward the tier, but any demotion of the
   remaining bodies still costs mid-size tribes.
2. **Tribe-as-fuel** (Magda's Dwarves, Nissa's mana-Elves, Camellia's
   sac-Squirrels, Elenda's token-Vampires): vanilla bodies feed the
   commander's engine — they are payoffs with no candidate-side
   payoff evidence. A commander-filter exemption was measured and
   REJECTED: it also exempts Marrow-Gnawer-class commanders where the
   tier delivered its biggest win (+0.2544), zeroing the gains.
3. **Spell-as-archetype** (Kess, Sythis, Rionya): identical shape on
   spell_density — the cheap-spell pool IS the deck.

The information needed to separate flood-as-noise from
flood-as-archetype is not in the weight layer (per-hit constants,
family shares, pool sizes). It is a DEMAND question — does this
commander's strategy consume this family? — which is what the C1
lift-normalization axis (score minus expected-baseline panel) can
express and per-family weights cannot: a lift baseline subtracts
"what this candidate scores for everyone" instead of guessing a
per-family discount, so archetype commanders keep their floods
(high commander-specific lift) while noise floods sink.

## What the track DID prove (keep this)

- The tier + tribal_body machinery works: Marrow-Gnawer +0.2544,
  Chatterfang +0.0775, Lathril +0.0238, Edgar +0.0188, gem +0.0241 —
  the payoff/body distinction is real signal, blocked only by the
  fuel-tribe minority.
- Gem rate improved in EVERY configuration (+0.0007…+0.0297) — the
  goal axis consistently rewards flood demotion; NDCG-vs-EDHREC cliffs
  are the blocker.
- Unit 3's finding compounds this: rank_bonus currently masks
  tie-density (its removal collapses R9 justified-divergence 0.85 →
  0.47). The calibration that would unmask it safely lives behind C1.

## Surviving infrastructure (all flag-OFF, bitwise-inert)

- `_syn_concentration_factor` dual-total choke point (Unit 4).
- `_ENABLE_TRIBAL_PAYOFF_TIER` + `tribal_body` rule_id + weight entry
  (0.30) + vanilla-anchor exemption (Unit 5).
- `_ENABLE_POOL_SCALED_FLAT_WEIGHTS` + `_POOL_SCALE_FLOOR` (30) +
  `_POOL_SCALED_RULES` scoping (Unit 6).
- All three flags registered in `ScoringConfigInputs` +
  `compute_config_hash`; full flag-gate test suites.
- A C1-cycle SHIP decision can re-flip the tier alongside the lift
  baseline — the joint evidence package pattern is established.

## Deferred consequences

- Unit 3 (rank_bonus removal) stays deferred: its re-run condition
  assumed shipped calibration; it now re-runs after the C1 lever.
- A6 spell/tribal dedup (mechanically separate, small) was not
  measured — the escalation stops calibration probes; fold it into
  the C1 cycle if still relevant.
