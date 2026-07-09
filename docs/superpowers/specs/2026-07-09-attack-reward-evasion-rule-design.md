# `attack_reward_evasion` rule — design spec

**Date:** 2026-07-09
**Status:** design approved, pre-implementation
**Predecessor:** `docs/solutions/best-practices/team-anthem-payoff-null-result-2026-07-08.md`
(the DECLINE whose retry lead this cycle implements)
**Coverage instrument:** activation-poverty census (`.audit/coverage/baseline.json`,
PR #106)

## Motivation

The activation-poverty census scored all 2,928 legal commanders and found **290
dead zones** (`earned_top30 = 0`). Decomposed by port shape, the single largest
*fresh* cluster is **94 commanders that have a trigger port but earn zero** —
the scoring engine's home turf failing. Profiling those 94 triggers, the two
dominant shapes are `Attacks` (28) and `Phase` (28); `AttackersDeclared` adds 7
more. The representative case — Agrus Kos, Wojek Veteran (*"whenever Agrus Kos
attacks, attacking red creatures get +2/+0 and attacking white creatures get
+0/+2"*) — surfaces only 15 candidates, all scoring 0.0.

These are **attack-reward** commanders: their payoff rewards attacking with a
board of creatures. Their honest complement is "creatures that attack" — but
crediting *every* creature flatly is the exact whitelist-equivalent flood that
made `team_anthem_payoff` DECLINE (a whole card class at flat per-class IDF,
`n_synergy_buckets = 1`). The retry lead from that null-result: **discriminate**
— credit a bounded subset whose keyword matches what the payoff rewards. For an
attack-reward trigger, the discriminator is **evasion**: evasive creatures
attack and connect reliably, so they trigger/benefit the payoff where a
groundling stalls behind blockers. This both un-floods (a bounded, tiered subset
instead of the whole creature class) and differentiates (evasion carriers rank
above vanilla bodies) — which is what NDCG rewards.

### Why existing combat rules leave this gap

Three combat rules already exist and were read during design; each deliberately
excludes this population:

- **`combat_enhancer`** (`_find_combat_enhancers`) fires for `Attacks Card.Self`
  triggers **only if** they carry an engine/value effect (AddPhase, Dig, Play,
  Mana, Token, …). Agrus Kos's trigger is a `PumpAll` — not on that list — so it
  is skipped. Candidate side is extra-combat + double-strike, not evasion.
- **`evasion`** (`_find_evasion_complements`) fires **only** for `DamageDone`
  combat-damage-to-player triggers (Yuriko, Saskia, Derevi) and credits **only
  self-unblockable `CantBlockBy` statics** — a pool of a few dozen. It does not
  look at `Attacks` triggers at all, and never credits printed evasion keywords
  (flying/menace/…).
- **`attack_payoffs`** (`_find_attack_payoffs`) is gated to **Isshin-style
  Panharmonicon statics only**. Its `_has_creature_attack_trigger` docstring
  states that commanders with *their own* Attacks trigger (Edgar Markov, Rafiq,
  Adeline) were **deliberately excluded** because stacking a generic
  attack-creature pool displaced their tribal/voltron EDHREC picks (Rafiq NDCG
  0.16 → 0.02). It notes Isshin is safe because he has no trigger of his own, so
  the pool is *"additive rather than displacing."*

That last point is the design key. The displacement risk that excluded
Rafiq/Edgar **does not exist for the dead-zone commanders** — they have no
tribal/voltron axis to displace. The activation-poverty condition *is* the
"additive rather than displacing" condition. So the principled gate is: an
attack-reward trigger **with no tribal subtype and not Exalted** — exactly the
commanders the old rules left dead.

## Scope decisions (resolved during brainstorm)

1. **Reward-triggers only, not grant-anthems.** A static `AddKeyword: Flying`
   anthem has *no* clean keyword discriminator — granting flying to a creature
   that already flies is redundant, so a flying-granter does not specifically
   want flyers. Its only discriminators are power-threshold / card-quality,
   which prior kill-tests (`quality_sim`) already declined. The reward-trigger
   case is the one where keyword-match is correct. The static grant-anthem
   population stays with the DECLINED `team_anthem_payoff` infra.
2. **New rule, shipped rules untouched** (brainstorm Approach A). No mutation of
   `_find_evasion_complements` / `_find_combat_enhancers` — a clean A/B and no
   change to live Yuriko/Saskia/Isshin scores.
3. **Evasion discriminator, two coarse IDF tiers.** Not a multi-axis pool
   (Approach C) — that re-introduces the `attack_payoffs` double-count and the
   displacement risk. Narrowest test of the discrimination hypothesis.
4. **Beneficiary must be the team.** Evasion on *other* creatures only helps
   when the trigger benefits the board, not the commander alone. Two acceptance
   paths (below) encode this.

## Unit 1 — Commander gate

**Function:** `_commander_has_team_attack_reward(cmdr_ports, conn, cmdr_set) -> bool`
(location: `complement_rules/combat.py`, beside the other combat gates).

Returns `True` iff **both** a trigger condition and both exclusions hold.

**Trigger condition** — some trigger port with
`event_class ∈ {"Attacks", "AttackersDeclared", "AttackersDeclaredOneTarget"}`
satisfies **either** acceptance path:

- **Path (a) — team-scope trigger.** The trigger fires on *your* attacking
  board. The scope is stored in **two different places** depending on event
  class (verified against the DB — this distinction is load-bearing; a
  `valid_filter`-only check silently misses every `AttackersDeclared`
  commander, including the census-dead Aloy and Caesar):
  - **`Attacks`** — scope is in `valid_filter`: accept when it contains
    `"YouCtrl"` **and** is not `_trigger_only_matches_self(valid_filter)`
    (e.g. `Creature.YouCtrl`, `Creature.Other+YouCtrl`).
  - **`AttackersDeclared` / `AttackersDeclaredOneTarget`** — `valid_filter`
    is **empty**; the scope lives in `raw_line` as `ValidAttackers` +
    `AttackingPlayer`. Accept when `raw_line` contains
    `'AttackingPlayer': 'You'` (Aloy: `ValidAttackers:
    'Creature.Artifact+YouCtrl'`, `AttackingPlayer: 'You'`; Caesar:
    `AttackingPlayer: 'You'`). ("Whenever one or more creatures you control
    attack …")
- **Path (b) — self-attack + team-pump.** The trigger is self-only
  (`_trigger_only_matches_self(valid_filter)` is `True`, e.g. `Card.Self`)
  **and** the commander has an `effect` port with `event_class == "PumpAll"`
  whose `valid_filter` contains the token `"attacking"` and is not Self-only.
  (Agrus Kos: `PumpAll` with `valid_filter = 'Creature.attacking+Red'`.
  Verified: `PumpAllInstances` does not exist in the data — only `PumpAll`
  (896 ports) and single-target `Pump` (4,475, deliberately excluded — a
  single-target pump is a voltron signal, not a board payoff); the
  `attacking` token is carried on `valid_filter`, not `affected_scope`.)

**Exclusion 1 — not tribal.** `_commander_subtypes_from_ports(conn, cmdr_set,
cmdr_ports)` is empty. A commander with a creature-subtype identity is served by
the tribal/lord rules; a subtype-named attack trigger ("whenever a Pirate
attacks") is caught here and routed to those rules.

**Exclusion 2 — not Exalted (attack-alone identity).** The commander has no
`port_type='keyword'` port with `event_class == "Exalted"`. Exalted rewards
attacking with a *single* creature — the exact opposite of a wide evasive board,
and the identity of the canonical displacement casualty (Rafiq, whose NDCG
collapsed 0.16 → 0.02 when a generic attacker pool was stacked on him). This is
the precise, correct exclusion; a fuzzy "is-voltron" detector is deliberately
avoided.

Note the two acceptance paths already exclude most self-buff voltron commanders
by construction: a Wyleth-style *"whenever this attacks, draw for each
aura/equipment"* trigger is self-only (fails Path a) and has no team `PumpAll`
(fails Path b). The **partitioned golden-500 no-regression gate (gate 3 below)**
is the safety net for any remaining competing-axis commander the port gate
admits: if a non-tribal, non-Exalted commander with a strong non-evasion axis
regresses, the plan narrows the gate (e.g. also excluding commanders carrying
equipment/aura self-attachment ports). The gate is calibrated by measurement,
not pre-tuned against hypotheticals.

The gate takes `conn` and `cmdr_set` (unlike the pure-`cmdr_ports` gates)
because the tribal-subtype check needs them; this matches
`_find_evasion_complements`, which calls `_commander_subtypes_from_ports` the
same way.

## Unit 2 — Candidate discriminator

**Function:** `_find_attack_reward_evasion(conn, cmdr_ports, cmdr_set,
candidate_cache=None) -> list[PortComplement]`.

Guarded by `if not _ENABLE_ATTACK_REWARD_EVASION: return []` first, then
`if not _commander_has_team_attack_reward(...): return []`.

Emits one `PortComplement` per distinct candidate creature carrying evasion,
in **two tiers** (strong tier scanned first so dedup keeps the stronger credit):

- **`evasion_hard`** (high IDF, the real differentiator, ~140 cards):
  - `port_type='keyword'` with `granted_keyword ∈ {Shadow, Horsemanship,
    Skulk, Fear, Intimidate}`, **plus**
  - `port_type='static'` `event_class='CantBlockBy'` self-unblockable
    (`raw_line LIKE '%Creature.Self%' OR '%Card.Self%'`) — the pool the shipped
    `evasion` rule uses, reused here as a hard-evasion tier.
- **`evasion_soft`** (low IDF, ~3,400 cards): `port_type='keyword'` with
  `granted_keyword ∈ {Flying, Menace}`.

Both tiers restrict candidates to creature cards
(`JOIN cards c ON c.name = p.card_name WHERE c.card_types LIKE '%Creature%'`) and
exclude `cmdr_set`. Trample is **excluded** in v1: it is a damage-through
keyword, not a connect-reliably one, and is irrelevant to attack-*count*
triggers; revisit only if the measurement motivates it.

`PortComplement(rule_id="attack_reward_evasion", direction="synergy",
candidate=name, cmdr_event="attack_reward", cand_event="evasion_hard" |
"evasion_soft")`.

**`candidate_cache` integration:** the keyword scan is commander-independent, so
when `candidate_cache` is provided the rule reads a cached evasion-carrier
projection instead of re-issuing the scan per commander (mirrors
`_find_team_anthem_payoffs` / `_find_subtype_supply_complements`). The plan adds
the cache field.

## Unit 3 — Wiring (flag-off, hash-neutral)

Mirrors the `team_anthem_payoff` template exactly:

- `_ENABLE_ATTACK_REWARD_EVASION: bool = False` in `combat.py`.
- **Not** added to `ScoringConfigInputs` — the flag is invisible to
  `compute_config_hash`, so config hash stays `c770b664e626` and
  `bench.py audit --expect-identity` passes bitwise.
- Wired into `find_all_complements` (`core.py`) with `candidate_cache` — emits
  nothing while the flag is off.
- Flag-aware `RuleGate("attack_reward_evasion", …)` in `registry.py` that
  reports **no** coverage while the flag is off (so `gap_report` /
  `rule_quality_gate` continue to see the attack-reward signature as unserved),
  exactly as the `team_anthem_payoff` gate does.
- `scoring_weights.json` multiplier (proposed `1.5`, matching `attack_payoffs`)
  is **deferred to the ship task** — adding it flips the config hash, so it is
  injected in-process for the measurement and only committed on SHIP.

## Unit 4 — Cohort predicate + fixture

- **`attack_reward(conn) -> set[str]`** in `bench/cohorts.py`: legal
  legendary-creature commanders satisfying `_commander_has_team_attack_reward`.
  Mirrors `team_anthem(conn)` — single source of truth is the gate helper.
  Registered in `coverage_report._COHORT_DISPATCH["attack_reward"]`.
- **Pinned cohort fixture** `tests/fixtures/golden_set_attack_reward.json` +
  a `bootstrap_attack_reward_fixture.py` script and a bootstrap **noise band**
  (`portfolio_sim bands` / the `--per-commander-ndcg` instrument, seed 17),
  mirroring `archetype_payoff`. The band is the gate threshold; recompute it
  after any data refresh.
- The fixture joins the no-DB freshness gate
  (`tests/bench/test_fixture_freshness.py`).

## Measurement & pre-registered gates

**The `team_anthem` lesson is binding: `earned_top30` is necessary but not
sufficient.** `team_anthem` passed the coverage delta (+30) and still shipped a
flat flood (`n_synergy_buckets = 1`, in-cohort NDCG cliffs). So the **primary
ship gate here is in-cohort NDCG, not earned coverage.**

Run with the flag on and the `1.5` multiplier injected in-process:

| # | Gate | Threshold | Role |
|---|------|-----------|------|
| 1 | **Primary — in-cohort NDCG@30 uplift** (cohort fixture, `--per-commander-ndcg`) | mean Δ **>** cohort noise half-width (seed 17 bootstrap) | **Load-bearing** — the honest test `team_anthem` failed |
| 2 | Anti-flood — the rule's own top-30 contribution is **differentiated** | both tiers present in credited top-30 cards across the cohort, not 30 equal-weight `evasion_soft` cards; report the tier mix and distinct contribution values | Guards the `n_synergy_buckets = 1` illusion |
| 3 | Guard — golden-500 no-regression, **partitioned by cohort membership** | aggregate NDCG@30 drop within noise half-width **and** no in-cohort cliff worse than the noise band | The dilution-masking lesson (a −0.18 cliff hid inside a −0.002 aggregate last time) |
| 4 | Guard — `bench.py audit --collinearity` vs `evasion`, `attack_payoffs`, `combat_enhancer` | not near-parallel (VIF / Pearson within the usual bounds) | The double-count retry-blocker |
| 5 | Guard — `rule_quality_gate --rule attack_reward_evasion` | verdict PASS | Vacuum-fill / flat-noise pathology |
| 6 | Guard — `hidden_gem_hit_rate` | no crater (Δ ≥ −0.02) | Secondary eval axis |

**Decision rule:** SHIP (flip the flag, commit the `scoring_weights.json`
multiplier, re-pin all fixtures) **only if gate 1 clears noise AND gates 2–6 all
pass.** Otherwise write a null-result doc under
`docs/solutions/best-practices/`, leave the rule flag-off as standing
infrastructure (like `team_anthem_payoff`), and record the DECLINE in
`docs/RULE_HISTORY.md`.

**The honest risk, stated up front:** evasion may simply not correlate with
these commanders' EDHREC Hi-Syn lists. If so, gate 1 does not move and the
cycle DECLINEs cleanly — a valid, well-evidenced null-result on a real gap, not
a failure.

## Out of scope (YAGNI)

- The `Phase`-trigger dead bucket (28) — self-firing engines whose complement
  reinforces their *output*; a different mechanism, a separate cycle.
- Grant-anthems / the static `AddKeyword` population — no clean discriminator
  (see scope decision 1); stays with `team_anthem_payoff`.
- Trample and other damage-through keywords — see Unit 2.
- `scoring_weights.json` multiplier tuning beyond the single `1.5` value —
  a sweep is a separate optimization, not this cycle.
- Reusing the `attack_payoffs` pool or a power-threshold axis (Approach C).

## Files touched

- `src/mtg_synergy_graph/complement_rules/combat.py` — gate + emitter + flag.
- `src/mtg_synergy_graph/complement_rules/core.py` — wiring.
- `src/mtg_synergy_graph/complement_rules/registry.py` — flag-aware `RuleGate`.
- `src/mtg_synergy_graph/penalties.py` — `CandidateCache` evasion-carrier field.
- `src/mtg_synergy_graph/bench/cohorts.py` — `attack_reward` predicate.
- `src/mtg_synergy_graph/bench/coverage_report.py` — cohort dispatch entry.
- `src/mtg_synergy_graph/data/scoring_weights.json` — multiplier (ship task only).
- `tests/fixtures/golden_set_attack_reward.json` + `scripts/bootstrap_attack_reward_fixture.py`.
- Tests: `tests/complement_rules/test_attack_reward_evasion.py`,
  `tests/bench/test_attack_reward_cohort.py`.
