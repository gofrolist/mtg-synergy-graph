# `x_cost_scaler` rule — design spec

**Date:** 2026-07-09
**Status:** design approved, pre-implementation
**Predecessor:** `docs/solutions/best-practices/attack-reward-evasion-null-result-2026-07-09.md`
(the DECLINE whose sibling-segment retry this cycle implements) and
`docs/solutions/best-practices/team-anthem-payoff-null-result-2026-07-08.md`
(the flood pathology both cycles are disciplined against)
**Gap source:** `scripts/gap_report.py` proposal #2 (`scales_with.xPaid[*]`,
`x_cost_scaler` template)

## Motivation

`gap_report` ranks `scales_with.xPaid[*]` as its #2 unserved sub-cell: **98
legal legendary-creature commanders** carry a `scales_with.xPaid` port and **0%
get any rule activation**. Verified against the live DB: the cohort is a
coherent, real archetype — Zaxara, the Exemplary (the canonical X-spells
commander), Gadwick the Wizened, Verdeloth the Ancient, Rocco Cabaretti
Caterer, Chatterfang, Gix Yawgmoth Praetor, Old Stickfingers, The Goose Mother.
These commanders have an **X-cost ability**: some effect scales with the amount
of mana paid for `X`. Their mechanical want is unambiguous — **anything that
lets them pay a bigger `X`**.

Their honest complement is "big mana" — but crediting *every* mana source
flatly is the exact whitelist-equivalent flood that made `team_anthem_payoff`
and `attack_reward_evasion` DECLINE (a whole card class — here the **1,979**
`effect=Mana` producers — at flat per-class IDF). The discipline both prior
null-results teach: **discriminate to a bounded, mechanically-principled
subset**. For an X-cost port, two subsets are *true port-to-port economic
interactions* with the `xPaid` signature, not archetype affinity:

1. **Mana doublers** — a replacement effect that multiplies mana output
   directly increases the `X` you can pay. Port-verified: `replacement`
   `ProduceMana` whose `ReplaceWith ∈ {ProduceTwice, ProduceThrice}`.
2. **Generic cost reducers** — shaving the fixed part of a spell's cost frees
   mana for a larger `X`. Port-verified: a `ReduceCost` static that reduces
   `Type=Spell` by a numeric amount across a broad (non-self, non-tribe) card
   class.

Both differentiate (a bounded, tiered subset ranks above the generic mana
flood) and are near-flood-proof (the clean pools are tiny), which is what NDCG
rewards.

### Why this is a mechanical interaction, and evasion wasn't

The `attack_reward_evasion` DECLINE root cause was that evasion is a
*commander-independent card class* whose membership does not correlate with the
target commanders' EDHREC lists — "an evasive body that happens to fit a
go-wide deck" is archetype affinity, not a port-match. Here the relationship is
mechanical: a mana doubler or a generic cost reducer **changes the value of the
commander's own `X`-cost port**. That is a port-to-port economic interaction of
exactly the kind "the commander's ports ARE the query" is built to score. The
EDHREC-correlation premise evasion lacked is also strong: EDHREC X-spell /
Hydra decks are *defined by* ramp and mana doublers.

### Why existing rules leave this gap

Two adjacent rules were read during design; neither serves this population:

- **`_find_scales_with_density`** (`density.py`) credits *density* of
  scales-with matches — it does not fire a mana-economics complement for
  `xPaid` commanders, and the census confirms these 98 earn zero.
- **`_find_cost_reduction_targets`** (`cost_reduction_target`, `density.py`)
  fires the **inverse direction**: it triggers when the *commander itself* is a
  cost reducer (`static ReduceCost` with `Creature`/`Spell`) and credits
  **high-CMC creatures**. It never credits cost-reducer *cards* for an X-cost
  commander. Different direction, different pool — no redundancy. The
  collinearity gate (gate 4) still checks it.

## Scope decisions (resolved during brainstorm)

1. **Two clean mechanical tiers only** (`mana_double` + `cost_reduce_generic`).
   The broader tiers the gap sketch listed are **excluded**:
   - `x_cost_stacker` (other `scales_with.xPaid` cards, ~901) — two X-spells do
     not mechanically interact; that is "X-matters" archetype affinity, the
     evasion wall. Out.
   - all broad ramp / `effect=Mana` (1,979) — the flat flood. Out.
2. **The clean pools are thin, and that is accepted up front.** After removing
   mana-denial stax (Contamination, Damping Sphere, Infernal Darkness),
   color-warpers (Naked Singularity), and self/tribe-scoped cost reducers, the
   isolated pools are **3 doublers + 49 generic reducers ≈ 52 cards** across 98
   commanders. This caps achievable NDCG movement — the realistic outcome is
   **small-positive-or-null** — but it makes the rule near-flood-proof, the
   opposite failure mode of the two prior DECLINEs. This is the cleanest,
   most mechanically-principled, lowest-risk bet on the remaining dead-zone.
3. **New rule, shipped rules untouched.** No mutation of
   `_find_cost_reduction_targets` / `_find_scales_with_density` — a clean A/B
   and no change to live scores.

## Unit 1 — Commander gate

**Function:** `_commander_has_x_cost_ability(cmdr_ports) -> bool`
(location: `complement_rules/density.py`, beside the other density gates).

Pure `cmdr_ports` — no `conn`/`cmdr_set` needed (no tribal exclusion; X-cost
economics is orthogonal to creature type). Returns `True` iff some port has
`port_type == "scales_with"` and `event_class == "xPaid"`.

```python
def _commander_has_x_cost_ability(cmdr_ports: list[PortRow]) -> bool:
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() == "scales_with" and (
            p.get("event_class") or ""
        ).strip() == "xPaid":
            return True
    return False
```

**No Exalted/tribal-style exclusion.** Unlike `attack_reward_evasion`, the
candidates here (mana doublers, cost reducers) are non-creature economics
cards that do not compete for a tribal/voltron creature slot, so the
displacement risk that excluded Rafiq does not apply. Some cohort members do
carry a strong secondary axis (e.g. Chatterfang is primarily a
Squirrel/token commander whose `xPaid` is minor); the **partitioned
golden-500 no-regression gate (gate 3)** is the safety net — if such a
commander regresses, the plan narrows the gate. Calibrated by measurement,
not pre-tuned against hypotheticals.

## Unit 2 — Candidate discriminator

**Function:** `_find_x_cost_scaler(conn, cmdr_ports, cmdr_set,
candidate_cache=None) -> list[PortComplement]`.

Guarded by `if not _ENABLE_X_COST_SCALER: return []` first, then
`if not _commander_has_x_cost_ability(cmdr_ports): return []`.

Emits one `PortComplement` per distinct candidate card, in **two tiers**
(strong tier scanned first so dedup keeps the stronger credit):

- **`mana_double`** (high IDF, the real differentiator, **3 cards**):
  `port_type='replacement'`, `event_class='ProduceMana'`, and `raw_line`
  contains `'ReplaceWith': 'ProduceTwice'` **or** `'ReplaceWith':
  'ProduceThrice'`. (Mana Reflection, Nyxbloom Ancient, Virtue of Strength.)
  Mana-denial stax and color-warpers are excluded by construction — they
  carry `ProduceB`/`ProduceC`/`ProduceColorless`/single-color `ReplaceWith`,
  never `ProduceTwice`/`ProduceThrice`.
- **`cost_reduce_generic`** (lower IDF, **49 cards**): `event_class='ReduceCost'`
  where `raw_line` satisfies **all** of:
  - contains `'Type': 'Spell'` (a spell-cost reducer, not an ability-cost one), **and**
  - `'ValidCard'` is one of the **broad** values
    `{'Card', '', 'Card.nonCreature', 'Card.multicolor', 'Card.monocolored',
    'Instant,Sorcery'}` (reduces a broad class of *your* spells), **and**
  - `'Amount'` is a positive integer (`\d+`, ≥ 1 — excludes `'X'`/SVar
    amounts that don't shave a fixed generic cost), **and**
  - `'ValidCard'` is **not** `'Card.Self'` (self-cost reducers help only the
    reducer, not the commander's X-spells; 252 of 528 `ReduceCost` rows).

  Tribe/type-narrow reducers (`Wizard.YouCtrl`, `Dragon`, `Creature`, …) fall
  outside the broad `ValidCard` allow-list and are excluded.

Parsing uses module-level compiled regexes on `raw_line` (mirroring the
`attack_reward_evasion` `_VALID_ATTACKERS_RE` / `_CANTBLOCKBY_SELF_ATTACKER_RE`
pattern), e.g. `_REPLACE_WITH_RE`, `_REDUCECOST_TYPE_RE`,
`_REDUCECOST_VALIDCARD_RE`, `_REDUCECOST_AMOUNT_RE`.

Both tiers exclude `cmdr_set`. Candidates are **not** restricted to creature
cards (doublers/reducers are typically artifacts/enchantments/instants).

`PortComplement(rule_id="x_cost_scaler", direction="synergy", candidate=name,
cmdr_event="x_cost", cand_event="mana_double" | "cost_reduce_generic")`.

Dedup: a card qualifying for both tiers (unlikely) keeps `mana_double` — scan
the strong tier first, track emitted names, skip already-emitted in the soft
tier.

**`candidate_cache` integration:** both scans are commander-independent, so
when `candidate_cache` is provided the rule reads cached card-name projections
(`x_cost_mana_double_cards`, `x_cost_cost_reduce_cards`) instead of re-issuing
the scans per commander (mirrors `attack_reward_evasion`'s
`evasion_hard_cards` / `evasion_soft_cards`). The cache fields are populated
only when the flag is on (`frozenset()` otherwise), so a flag-off build does
zero extra work and stays hash-neutral. The plan adds the cache fields.

## Unit 3 — Wiring (flag-off, hash-neutral)

Mirrors the `attack_reward_evasion` template exactly:

- `_ENABLE_X_COST_SCALER: bool = False` in `density.py`.
- **Not** added to `ScoringConfigInputs` — the flag is invisible to
  `compute_config_hash`, so config hash stays `c770b664e626` and
  `bench.py audit --expect-identity` passes bitwise.
- Wired into `find_all_complements` (`core.py`) with `candidate_cache` — emits
  nothing while the flag is off.
- Flag-aware `RuleGate("x_cost_scaler", …)` in `registry.py` that reports **no**
  coverage while the flag is off (so `gap_report` / `rule_quality_gate`
  continue to see the `scales_with.xPaid` signature as unserved), exactly as
  the `attack_reward_evasion` gate does. The gate keys on a `scales_with`
  `xPaid` port.
- `scoring_weights.json` `_RULE_QUALITY_MULTIPLIER["x_cost_scaler"]` (proposed
  `1.5`) is **deferred to the ship task** — adding it flips the config hash, so
  it is injected in-process for the measurement and only committed on SHIP.
- `rule_id="x_cost_scaler"` stays on the **Python-helper path** — it is **not**
  added to `DECLARATIVE_RULE_IDS`.

## Unit 4 — Cohort predicate + fixture

- **`x_cost_scaler(conn) -> set[str]`** in `bench/cohorts.py`: legal
  legendary-creature commanders (reuse the shared `_LEGAL_LEGENDARY_CREATURE`
  WHERE fragment) that carry a `scales_with.xPaid` port. Mirrors
  `attack_reward(conn)` — single source of truth is the gate condition. A
  bulk single-query prefilter (all cohort candidates' `scales_with.xPaid`
  ports in one grouped query, no N+1), then the gate. Registered in
  `coverage_report._COHORT_DISPATCH["x_cost_scaler"]`.
- **Pinned cohort fixture** `tests/fixtures/golden_set_x_cost_scaler.json` +
  a `bootstrap_x_cost_scaler_fixture.py` script (thin wrapper over the
  parameterized `bootstrap_archetype_payoff_fixture.main(cohort_fn,
  output_path)`) and a bootstrap **noise band** measured at the flag-off
  baseline (`--per-commander-ndcg` instrument = `score_commander` top-30
  NDCG@30, seed 17, over the pinned cohort members). The band half-width is
  the gate-1 threshold; recompute after any data refresh.
- The fixture joins the no-DB freshness gate
  (`tests/bench/test_fixture_freshness.py`).

## Measurement & pre-registered gates

**The `team_anthem` lesson is binding: `earned_top30` is necessary but not
sufficient. The primary ship gate is in-cohort NDCG, not earned coverage.**

Run with the flag on and the `1.5` multiplier injected in-process:

| # | Gate | Threshold | Role |
|---|------|-----------|------|
| 1 | **Primary — in-cohort NDCG@30 uplift** (cohort fixture, `--per-commander-ndcg`) | mean Δ **>** cohort noise half-width (seed 17 bootstrap, measured pre-ship) | **Load-bearing** — the honest test the two priors failed |
| 2 | Anti-flood — the rule's own top-30 contribution is **differentiated** | both tiers present in credited top-30 cards across the cohort, not 30 equal-weight `cost_reduce_generic` cards; report tier mix + distinct contribution values | Guards the `n_synergy_buckets = 1` illusion |
| 3 | Guard — golden-500 no-regression, **partitioned by cohort membership** | aggregate NDCG@30 drop within noise half-width **and** no in-cohort cliff worse than the noise band | The dilution-masking lesson (a big cliff hiding inside a tiny aggregate) |
| 4 | Guard — `bench.py audit --collinearity` (inspect `x_cost_scaler` vs `cost_reduction_target` and the `scaling`/scales-with density rules) | not near-parallel (VIF / Pearson within the usual bounds) | The double-count retry-blocker |
| 5 | Guard — `rule_quality_gate --rule x_cost_scaler` | verdict PASS | Vacuum-fill / flat-noise pathology |
| 6 | Guard — `hidden_gem_hit_rate` | no crater (Δ ≥ −0.02) | Secondary eval axis |

**Decision rule:** SHIP (flip the flag, commit the `scoring_weights.json`
multiplier, re-pin all fixtures) **only if gate 1 clears noise AND gates 2–6
all pass.** Otherwise write a null-result doc under
`docs/solutions/best-practices/`, leave the rule flag-off as standing
infrastructure, and record the DECLINE in `docs/RULE_HISTORY.md`.

**The honest risk, stated up front:** the clean pool is only ~52 cards, so even
if mana doublers / generic reducers *do* sit in these commanders' EDHREC
Hi-Syn lists, the credited overlap may be too small to move gate 1 beyond
noise. If so the cycle DECLINEs cleanly — a valid, well-evidenced null-result
on a real, verified gap, not a failure. The upside case: the doublers
(Nyxbloom, Mana Reflection) are genuine X-deck staples, so a handful of
cohort commanders may see a real in-cohort NDCG lift that clears the thin band.

## Out of scope (YAGNI)

- `x_cost_stacker` (other `scales_with.xPaid` cards) and all broad ramp — see
  scope decision 1.
- Big-mana staples under *other* port signatures (Doubling Cube, Cabal Coffers,
  Nykthos, Mana Flare) — they lack a clean shared port signature; hunting them
  reintroduces isolation/flood risk (the rejected "broaden T1" option). A
  separate cycle if this one motivates it.
- Type-scoped cost reducers (`Wizard`, `Dragon`, `Creature`-only) — outside the
  broad `ValidCard` allow-list; revisit only if measurement motivates.
- `scoring_weights.json` multiplier tuning beyond the single `1.5` value — a
  sweep is a separate optimization.

## Files touched

- `src/mtg_synergy_graph/complement_rules/density.py` — gate + emitter + flag +
  regexes.
- `src/mtg_synergy_graph/complement_rules/core.py` — wiring.
- `src/mtg_synergy_graph/complement_rules/registry.py` — flag-aware `RuleGate`.
- `src/mtg_synergy_graph/penalties.py` — `CandidateCache` `x_cost_mana_double_cards`
  + `x_cost_cost_reduce_cards` fields.
- `src/mtg_synergy_graph/bench/cohorts.py` — `x_cost_scaler` predicate.
- `src/mtg_synergy_graph/bench/coverage_report.py` — cohort dispatch entry.
- `src/mtg_synergy_graph/data/scoring_weights.json` — multiplier (ship task only).
- `tests/fixtures/golden_set_x_cost_scaler.json` +
  `scripts/bootstrap_x_cost_scaler_fixture.py`.
- Tests: `tests/complement_rules/test_x_cost_scaler.py`,
  `tests/bench/test_x_cost_scaler_cohort.py`.
