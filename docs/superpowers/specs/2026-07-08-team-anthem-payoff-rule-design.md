# Team-Anthem Payoff Rule — Design

**Date:** 2026-07-08
**Status:** Approved (brainstorm), pending implementation plan
**Author:** brainstormed with Claude Code

## Motivation

This is the first **coverage-oriented rule** gated on the activation-poverty
instrument (`docs/superpowers/specs/2026-07-08-activation-poverty-instrument-design.md`).
The census it produced (`.audit/coverage/baseline.json`, 2928 legal
commanders) surfaced the real frontier:

- **2319 / 2928 (79%)** commanders are already fully covered
  (`earned_top30 = 30`).
- **290 are total dead zones** — `earned_top30 = 0` *and*
  `n_synergy_buckets = 0`, i.e. **zero rules fire for them at all**.
  Only 5 of those are genuinely vanilla (no ports); **285 have real
  ports no rule keys on.**

Clustering the 285 by port shape names the single biggest addressable
cohort: **`STATIC_BUFF / static.Continuous` on 114 dead commanders** —
commanders whose identity is a *passive* static ability. This dwarfs the
~12-commander toughness-payoff cohort the instrument spec assumed would be
the first customer, which is exactly why the instrument exists ("let the
queue name the under-served commanders," not our prior).

### Root cause: a missing rule *direction*

A triage of four representative dead commanders confirmed these are **not
mis-gated existing rules** — they need a genuinely new rule direction. Every
current anthem/voltron/equipment rule keys on the commander as an *active*
engine:

| Commander | Own port(s) | Why nothing fires |
|-----------|-------------|-------------------|
| Avacyn, Angel of Hope | `static.Continuous` grants Indestructible to `Permanent.Other+YouCtrl` | `anthem_payoff` requires the *commander* to make tokens; no rule keys on "commander grants a team keyword." |
| Iroas, God of Victory | team combat static + `replacement.DamageDone` | Passive team anthem, no trigger/effect/token port to hook. |
| Ascendant Evincar | `static.Continuous` on `Creature.Black+Other` (+1/+1) | Color-tribal anthem; `lord` keys on creature *subtypes*, not colors. |
| Balan, Wandering Knight | self `AddKeyword: Double Strike` when equipped + `effect.Attach` | Self-voltron; `_voltron_gate` fires only on {Hexproof, Exalted, Shroud, Trample} keyword ports. |

The existing rules assume the commander *does* something (makes tokens,
triggers, scales). This cohort is a **passive** engine: the commander's
static ability **is** the payoff. No rule in the catalogue keys on that.

## Scope

This spec delivers **only sub-shape (1): the team anthem.** The self-voltron
(Balan) and any remaining color-tribal handling are explicitly out of scope
— deferred to follow-on specs, decided with real numbers from this rule in
hand. This follows the repo's small-increment discipline: the last ~10 broad
scoring experiments were DECLINED; subtype-supply (2026-07-07) shipped
precisely because it was a single, hard-gated increment.

Note: color-*qualified* team anthems (Ascendant Evincar's `Creature.Black`)
fold into this rule **for free**, because the gate keys on the affected
**base type** (`Creature`/`Permanent`), not the qualifier. Only creature-
*subtype* anthems stay out (that is `lord`'s territory).

### What this spec is NOT

- **Not** self-voltron (Balan / `Card.Self` statics) — follow-on.
- **Not** a keyword-matched payoff — the payoff is board-width (token
  producers), independent of *which* keyword the anthem grants. Keyword-
  aware refinement is future work.
- **Not** a broad "go-wide" pool — token producers only, deliberately tight.

## Architecture

One new complement rule, `team_anthem_payoff`, flag-gated OFF by default and
hash-neutral until it clears its pre-registered gates.

### Unit 1 — Commander-side gate

A commander static qualifies when **all** hold:

- `port_type = 'static'` AND `event_class = 'Continuous'` AND
  `affected_scope` non-empty.
- affected_scope **base type ∈ {Creature, Permanent}** (split on `.` and
  `+`, take the base) with a `YouCtrl` controller scope on the matched
  alternative (own board).
- raw_line grants a benefit: positive `AddPower`/`AddToughness` **or**
  `AddKeyword` (reuse the `_ANTHEM_BUFF_KEYS` set from `_find_anthem_payoffs`).

**Exclusions** (each prevents a known failure mode):

- Negative `AddPower`/`AddToughness` (drawback statics) — reuse the
  `re.search(r"'Add(?:Power|Toughness)':\s*'-", raw)` guard.
- No `YouCtrl` on the matched scope (symmetric or opponent-facing anthems).
- `Card.Self`-only statics (that is the voltron sub-shape — out of scope).
- Creature-*subtype* bases (Goblin/Elf/…): the base must be exactly
  `Creature` or `Permanent`. This is the double-counting boundary with
  `lord`.

This gate is the mirror boundary of `anthem_payoff` (which requires the
commander to make tokens and matches anthem *candidates*), so no
(commander_port, candidate_port) pair is scored by both rules.

### Unit 2 — Candidate-side payoff (token producers only)

Two coarse tiers, dedup per candidate with highest tier winning (mirrors the
shipped subtype-supply producer/body structure; coarse buckets keep IDF keys
away from the high-cardinality granularity trap the `anthem_payoff` docstring
warns about):

- **`token_doubler`** (strong) — replacement ports that multiply token
  creation (Doubling Season, Parallel Lives, Anointed Procession). Small,
  unambiguous, high-signal pool.
- **`token_producer`** (base) — `effect.Token` making a **creature** token
  (filtered via the `token_subtype` / `token_color` `port_attributes` rows to
  exclude Treasure / Clue / Food / Blood / etc.), plus populate effects.

### Unit 3 — Wiring & flag

- New helper `_find_team_anthem_payoffs(conn, cmdr_ports, cmdr_set)` in
  `complement_rules/statics.py`, adjacent to `_find_anthem_payoffs`.
- `RuleGate("team_anthem_payoff", _team_anthem_payoff_gate)` in
  `registry.py`; `rule_id = "team_anthem_payoff"`.
- **Flag `_ENABLE_TEAM_ANTHEM_PAYOFF`, default `False`, hash-neutral** —
  the death_outlet / subtype-supply pattern. While off, the rule and its
  registry gate report NO coverage so `gap_report` / `demand_coverage` /
  `rule_quality_gate` see the truth. Self-activates when the flag flips.
- Multiplier seeded at **1.5** (subtype-supply producer tier) in
  `src/mtg_synergy_graph/data/scoring_weights.json`, tunable.

### Unit 4 — Cohort predicate

- New predicate `team_anthem(conn)` in `bench/cohorts.py`, expressing exactly
  the Unit 1 gate over the legal-commander universe.
- Wired into `coverage_report.py`'s `_COHORT_DISPATCH` as `"team_anthem"`.

## Data flow

At inference (flag on): `find_all_complements` → `_find_team_anthem_payoffs`
fires per qualifying commander static → emits `PortComplement(rule_id=
"team_anthem_payoff", cand_event ∈ {token_doubler, token_producer})` for each
token-producing candidate → IDF-weighted and summed by `universal_scorer`
alongside every other rule. Zero change to the scoring algorithm; one more
rule in the registry.

## Pre-registered gates (decide ship/decline BEFORE seeing results)

Implement behind the flag, run the full battery, then flip the flag on **only
if all gates pass**:

1. **Primary — coverage lift.** `coverage_report.py gate --cohort team_anthem`:
   cohort mean `Δearned_top30 ≥ +5` AND stratified-control mean
   `Δearned_top30 ≥ 0` (no collateral damage to non-cohort commanders).
2. **Guard A — no golden regression.** `bench.py audit` on the golden-500
   fixture: NDCG@30 drop within the fixture's bootstrap noise half-width.
3. **Guard B — not collinear.** `bench.py audit --collinearity`: the new rule
   is not near-parallel to `scaling` / `anthem_payoff`.
4. **Guard C — quality gate.** `rule_quality_gate.py --rule team_anthem_payoff`
   passes the vacuum-fill / flat-noise pathology check.
5. **Guard D — gems intact.** `hidden_gem_hit_rate` does not crater on the
   golden audit.

A DECLINE at any gate is recorded as a null-result doc under
`docs/solutions/best-practices/`, the flag stays off, and the rule remains in
the tree hash-neutral (like `death_outlet_feeder`).

## Testing (TDD)

- **Gate unit tests:** Avacyn, Iroas, Ascendant Evincar qualify; a creature-
  subtype anthem (e.g. a Goblin lord), a `Card.Self`-only static, a symmetric
  (no-`YouCtrl`) anthem, and a drawback (`AddPower: '-1'`) static are all
  rejected.
- **Candidate-tier tests:** a creature-token producer fires (`token_producer`);
  a Treasure-only maker does not fire; Doubling Season resolves to
  `token_doubler`.
- **Dedup test:** a card matching both tiers gets exactly one complement, at
  the strong tier.
- **No-regression assertion:** golden-set NDCG unchanged while the flag is
  off (hash-neutral proof).

## Risks & mitigations

- **Flat-noise flood** (the ward_2_tribal lesson) — mitigated by the tight
  token-producer-only pool, coarse two-tier IDF keying, flag-off-first
  measurement, and Guards A/C/D.
- **Double-counting with `lord` / `anthem_payoff`** — mitigated by the
  subtype-vs-base boundary (Unit 1) and the reverse-direction candidate pool
  (Unit 2); verified by Guard B.
- **Whitelist-equivalence illusion** — the cohort is selected by the same
  predicate the rule keys on, so cohort lift alone is necessary-but-not-
  sufficient. The control arm of the primary gate plus Guard A (whole-fixture
  no-regression) are the generalization check.
