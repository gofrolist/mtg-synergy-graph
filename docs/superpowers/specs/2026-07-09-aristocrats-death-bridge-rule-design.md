# `aristocrats_death_bridge` rule — design spec

**Date:** 2026-07-09
**Status:** design approved; awaiting spec review → implementation plan
**Baseline config_hash:** `c770b664e626` (must remain unchanged — this is a
flag-off, hash-neutral cycle)

## 1. Motivation — why this rule, and why it is different

The last three rule cycles (`team_anthem_payoff`, `attack_reward_evasion`,
`x_cost_scaler`) all DECLINED for the same reason —
**commander-independence**: each credited a fixed candidate pool identically to
every cohort member, flooding the top-30 with generic bodies that displace the
real (rule-disconnected) EDHREC picks.

A 2026-07-09 investigation then measured whether the golden-set ceiling could be
raised by **calibration** instead of new rules. Two levers came back negative
(see `docs/solutions/best-practices/calibration-levers-exhausted-2026-07-09.md`):

- **Weight retuning** (`bench.py audit --optimize`): converged in one sweep,
  train Δ +0.00098 but **held-out Δ −0.00033** — overfit, not shippable. The
  per-rule multipliers are already at a local optimum.
- **Staple-bonus reform**: empirically, pure-staples occupy only **16 of 3000**
  golden top-30 slots (0.53%), all 16 on one commander (Xenagos). The staple
  bonus does not flood; reforming it recovers nothing.

The forensics taxonomy shows **~88% of misses** are "no mechanical rule connects
the commander to the EDHREC card" (OUTRANKED `staple_only` 46% + NO_RULES 42%).
The `demand_coverage` instrument (plan 2026-07-02-005) measured the general
commander-dependent demand→supply framing at **4.7% addressable share** (below
the 0.25 funding bar) — but the remaining signal is **extremely concentrated**:
Yawgmoth alone holds 19 of 52 reachable labels, with Araumi (7), Ghave (5),
Prossh (4) behind him.

Inspecting Yawgmoth's 19+ missed EDHREC cards directly reveals a **tight,
EDHREC-matching class the demand_coverage instrument mis-modeled**: the
**aristocrats death-value engine** —

- **death payoffs**: Blood Artist, Zulaport Cutthroat, Bastion of Remembrance,
  Nest of Scarabs, Deathgreeter, Vengeful Bloodwitch, Ayara;
- **recursive fodder**: Gravecrawler, Reassembling Skeleton, Nether Traitor,
  Butcher Ghoul (undying), Geralf's Messenger, Forsaken Miner;
- **value-on-death**: Pitiless Plunderer, Pawn of Ulamog, Sifter of Skulls,
  Skullclamp, Culling the Weak.

The demand_coverage `sacrifice_fodder` flow keyed supply on **token production**,
which misses nearly all of these — hence its `pool_size`-starved diagnosis.

**The mechanical root cause:** the engine's event substrate bridges `sacrifice →
Sacrificed` (Korvold: the sacrificed card itself triggers) but has **no
`sacrifice → ChangesZone(bf→grave)` equivalence** (the strings `"Dies"` /
`"Death"` appear 0× in `event_match_seed.json`). So when an aristocrats commander
sacrifices a creature, the engine never registers that *a creature died*, and
every death-payoff (which triggers on `ChangesZone` Battlefield→Graveyard) stays
**unranked — unconnected, not merely under-ranked.** This rule supplies that
missing bridge as a **dedicated, flag-gated, own-pool complement** — the safest
of the three implementation options considered (it does not perturb the shared
event-match substrate, so measurement is clean and out-of-cohort collateral is
zero by construction).

This is commander-dependent in the way the three DECLINES were not: the gate
fires only for commanders whose own ports establish a death/sacrifice engine,
and credit flows only to candidates with a genuine death-value trigger or
self-recursion — a specific, IDF-weighted mechanical class, not a flat pool of
generic bodies.

## 2. Cohort and commander gate

**Cohort predicate** (`bench/cohorts.py::aristocrats`): a legal legendary
creature (`LEGAL_LEGENDARY_CREATURE_WHERE`) carrying **either**

- **sacrifice outlet** — a `cost` port with `event_class='sacrifice'`,
  `cost_subtype LIKE '%Creature%'`, and `cost_target != 'self'` (sacrifice a
  creature other than itself); **or**
- **death-trigger payoff** — a `trigger` port with `event_class='ChangesZone'`,
  `zone_origin` Battlefield, `zone_destination` Graveyard.

Measured cohort size: **292** legal legendary creatures; **13 of the golden-100**
(Ghave, Jan Jansen, Judith, Marchesa, Meren, Mondrak, Omnath (Rage), Prossh,
Slimefoot, The Locust God, Titania, Wilhelt, Yawgmoth). The commander gate helper
`_commander_is_aristocrats(cmdr_ports)` is a pure predicate over the commander's
own ports mirroring this SQL.

## 3. Candidate supply — two IDF tiers

Emitted strong-first with a `seen`-set dedup (one complement per candidate),
`rule_id="aristocrats_death_bridge"`, `cmdr_event="death_engine"`,
`cand_event ∈ {"death_payoff", "recursive_fodder"}`. Candidates in the
commander set are excluded.

### Tier 1 — `death_payoff` (strong)

A candidate that **triggers on a creature dying and executes a value effect**:

- a `trigger` port with `event_class='ChangesZone'` (zone bf→grave) OR
  `event_class ∈ {'Sacrificed','Dies'}`, **whose executed effect is a value
  payoff** — the trigger's `execute_ref` / `sub_ability_ref` resolves to an
  effect port in `{LoseLife, GainLife, DealDamage, DamageAll, PutCounter,
  Draw, Token, Mana}`;
- **scoped to your/any creatures dying** — exclude opponent-scoped triggers
  (`Activator`/`ValidCard` restricted to an opponent, e.g. Accursed-Witch shape).

**Anti-flood note (primary implementation risk):** the loose upper bound
("card has a death trigger AND *some* value effect port anywhere") is ~881
cards — a flood. The rule MUST require the value effect to be the death
trigger's **own execution** (joined via `execute_ref`/`sub_ability_ref`), not
merely co-present on the card. Whether that tightening + color-identity
confinement + IDF weighting scopes below the cohort noise band is exactly what
the primary gate (§6) decides. If the join cannot be made reliably from the port
schema, that is a DECLINE-worthy finding to surface, not to paper over with a
looser predicate.

### Tier 2 — `recursive_fodder` (support)

A creature that **returns itself to be sacrificed again**:

- `granted_keyword ∈ {'Undying','Persist'}`; OR
- an `effect` port `event_class='ChangeZone'` with `zone_origin` Graveyard,
  `zone_destination` Battlefield (self-recursion), restricted to `Creature`
  card types.

Loose upper bound ~416 cards. Cast-from-graveyard bodies (Gravecrawler) that do
not surface as a grave→bf `ChangeZone` effect port are a known gap; the
implementation should check whether a distinct port shape captures them and,
if not, document the omission rather than widening the predicate.

## 4. Flag-off, hash-neutral wiring

- `_ENABLE_ARISTOCRATS_DEATH_BRIDGE = False` (module-level, in the file that
  hosts the emitter — `complement_rules/death.py` or an existing aristocrats-
  adjacent module; the plan fixes the exact home).
- The flag is **NOT** added to `ScoringConfigInputs` / `compute_config_hash` —
  `config_hash` stays `c770b664e626`; `bench.py audit --expect-identity` must
  PASS at every task boundary.
- `rule_id="aristocrats_death_bridge"` is **NOT** in `DECLARATIVE_RULE_IDS`
  (Python-helper path only; the `_DUAL_PATH_OVERLAP` guard would raise
  otherwise).
- A flag-aware `RuleGate("aristocrats_death_bridge", _gate)` in `registry.py`
  returns `False` when the flag is off (call-time `from . import` to read the
  live flag).
- Two `CandidateCache` frozenset fields (the tier-1 and tier-2 pools), **defaulted
  `= frozenset()` and placed at the END of the frozen dataclass body**; loaded
  via `_bulk_load_*(conn) if _flag_enabled() else frozenset()` so a flag-off
  build does **zero** extra scans.
- The deferred `_RULE_QUALITY_MULTIPLIER` entry (proposed `1.5`) is committed to
  `scoring_weights.json` **only at the SHIP flip**, never before.

## 5. Cohort fixture, coverage dispatch, noise band

- `aristocrats` registered in `bench/coverage_report.py::_COHORT_DISPATCH`.
- `scripts/bootstrap_aristocrats_fixture.py` — thin wrapper over the
  parameterized `bootstrap_archetype_payoff_fixture.main(cohort_fn,
  output_path)`; snapshots `cohort_members` for pin-reproducible slicing. Use
  THIS after a cardsfolder refresh, not `--repin` (which preserves the old
  snapshot).
- `tests/fixtures/golden_set_aristocrats.json` — pinned cohort fixture at
  `config_hash c770b664e626`; added to `_COMMITTED_GOLDEN_FIXTURES`
  (`tests/bench/test_fixture_freshness.py`).
- **Noise band** — bootstrap band of the `score_commander` top-30 NDCG@30
  instrument (seed 17) over the pinned cohort members, measured with the rule
  **flag-OFF** (the pre-ship baseline band). The half-width is the primary-gate
  threshold. Recompute after any data refresh or re-pin via
  `bootstrap_band()` over `compute_per_commander_ndcg_rows()` restricted to
  `pinned.cohort_members`. The measured band value goes into CLAUDE.md's
  fixture block at pin time.

## 6. Pre-registered measurement gates

Measured with the flag ON and the `1.5` multiplier injected **in-process**
(the injection flips the in-process hash only; the on-disk hash and all pins
stay valid at `c770b664e626`).

| # | Gate | Threshold |
|---|------|-----------|
| 1 | **Primary** — in-cohort NDCG@30 uplift | mean Δ **>** cohort noise half-width (§5) |
| 2 | **Anti-flood** — tier mix / discrimination | credit is a specific death-value class, not a flat pool; report per-commander tier composition and top-30 contribution spread |
| 3 | **Guard** — golden-500 partitioned no-regression | aggregate within noise; out-of-cohort collateral **= 0 by construction** (flag-aware gate fires only on aristocrats ports) |
| 4 | **Guard** — `--collinearity` | not near-collinear with `cost_feeds_trigger`, the resonance family, `token_etb_damage`, or existing sacrifice rules |
| 5 | **Guard** — `rule_quality_gate --rule aristocrats_death_bridge` | PASS (no vacuum-fill / flat-noise pathology) |
| 6 | **Guard** — `hidden_gem_hit_rate` | Δ ≥ −0.02 |
| 7 | **Whitelist-equivalence** | the rule is NOT a disguised whitelist — a whitelist keyed on the exact cohort scores maximally by construction; show the death-value class generalizes beyond the pinned cohort members and does not merely re-rank the selection predicate |

**Decision rule:** SHIP only if gate 1 clears the noise half-width **AND** gates
2–7 all pass. On SHIP: flip the flag on, commit the `1.5` multiplier to
`scoring_weights.json`, re-pin all fixtures, add the RULE_HISTORY SHIP entry.
Otherwise: leave the flag off (standing infrastructure), write a null-result doc
(`docs/solutions/best-practices/aristocrats-death-bridge-null-result-2026-07-09.md`)
and a RULE_HISTORY DECLINE entry. Guards not reached because of a dispositive
primary failure are honestly recorded as **not measured**, never backfilled.

## 7. Implementation units (for the plan)

1. **Commander gate** — `_commander_is_aristocrats` + tests.
2. **Candidate emitter** — `_find_aristocrats_death_bridge` two-tier, with the
   `execute_ref`-joined value-payoff scoping and opponent-scope exclusion;
   `CandidateCache` fields + skip-when-off loaders in `penalties.py`; tests
   including the flood-scoping regression cases (a bare `ChangesZone` trigger
   with no value execution must be EXCLUDED; an opponent-scoped death trigger
   must be EXCLUDED).
3. **Flag-off wiring** — `core.py` wire-in, `registry.py` flag-aware `RuleGate`,
   deferred `scoring_weights.json` entry; `--expect-identity` PASS.
4. **Cohort + fixture + band** — `bench/cohorts.py::aristocrats`,
   `coverage_report` dispatch, `bootstrap_aristocrats_fixture.py`, pinned
   fixture, freshness-gate registration, measured noise band.
5. **Measurement / gate** — run gates 1–7; produce the verdict.
6. **SHIP path** (mutually exclusive with 7) — flip flag, commit multiplier,
   re-pin, RULE_HISTORY SHIP.
7. **DECLINE path** (mutually exclusive with 6) — null-result doc, RULE_HISTORY
   DECLINE; working tree stays flag-off hash-neutral.

## 8. Global constraints (binding)

- config_hash stays `c770b664e626` through Tasks 1–5; `--expect-identity` PASS.
- Tests never pass a literal project-relative DB path to `open_db()` — use the
  `LIVE_DB = Path(__file__).resolve().parents[N] / "data" / "synergy.db"` +
  skipif pattern; use `tmp_path` for constructed DBs.
- SQL fragment interpolation guarded by `_VALID_*`/frozenset + `ValueError`
  (never `assert`); `# noqa: S608` acceptable only for module-constant
  concatenation with no bound-value interpolation.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context)
  <noreply@anthropic.com>` + the `Claude-Session` line. PR body ends with the
  Claude Code generation footer + session link.
