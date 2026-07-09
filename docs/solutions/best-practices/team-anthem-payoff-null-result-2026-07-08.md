---
last_updated: 2026-07-08
module: complement_rules
tags:
  - anthem
  - static-continuous
  - go-wide
  - token-producer
  - cohort
  - coverage
  - kill-test
  - null-result
  - flat-credit
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering any "commander's own passive static IS the payoff" rule (team anthem, self-voltron, keyword-granter) keyed on static.Continuous
  - Considering any rule whose candidate side is "all token producers" (or any large >1000-card class credited flatly)
  - Reading the coverage census poverty queue and tempted to close the ~114 static.Continuous dead-zone cohort with a go-wide rule
  - Interpreting a coverage `gate` earned_top30 win — read the n_synergy_buckets caveat below before treating +Δearned as merit
created: 2026-07-08
plan_ref: docs/superpowers/plans/2026-07-08-team-anthem-payoff-rule.md
spec_ref: docs/superpowers/specs/2026-07-08-team-anthem-payoff-rule-design.md
---

# `team_anthem_payoff` rule: DECLINED at pre-registered gates

**Cycle:** plan `docs/superpowers/plans/2026-07-08-team-anthem-payoff-rule.md`,
the first coverage-oriented rule gated on the activation-poverty instrument
(plan 2026-07-08 / PR #106). **Verdict: DECLINE** (human-chosen at the Task 5
decision point). The working tree stays at the Task 1–4 hash-neutral state:
flag `_ENABLE_TEAM_ANTHEM_PAYOFF = False`, no `scoring_weights.json` entry, the
flag NOT in `ScoringConfigInputs`. The rule, its flag-aware `RuleGate`, and the
`team_anthem` cohort predicate remain in-tree as standing infrastructure (like
`death_outlet_feeder`); config hash unchanged at `c770b664e626`.

## What the rule was

Keys on a commander's own **team-scoped `static.Continuous` anthem**
(`affected_scope` base ∈ {Creature, Permanent} carrying `YouCtrl`, granting a
positive `AddPower`/`AddToughness` or `AddKeyword`) and emits complements for
**creature-token producers** — two tiers, `token_doubler` (replacement
`CreateToken`) > `token_producer` (`effect.Token` with a P/T TokenScript).
Targets the biggest dead-zone cohort the census surfaced: ~114 passive-anthem
commanders no rule touched (25 of them fully dead in the golden universe). See
the spec for the motivation and the full gate boundary.

## Why it was surfaced

The activation-poverty census (`.audit/coverage/baseline.json`, 2928 legal
commanders) found **290 total dead zones** (`earned_top30 = 0` AND
`n_synergy_buckets = 0`). The biggest addressable cluster was
`STATIC_BUFF / static.Continuous` on 114 commanders. A triage confirmed no
existing anthem/voltron rule keys on a commander whose *own* static is the
payoff (they all assume the commander is an *active* engine — makes tokens,
triggers, scales). So the gap was real and large. The `team_anthem` cohort
predicate (155 legal members, 25 dead-at-baseline) named it.

## The measurement (flag on, multiplier 1.5 injected in-process)

Injecting the 1.5 multiplier in-process flips the config hash to `1da28677a3eb`
(the multiplier is a hash input — this is why the weight edit was deferred to
the ship task). The coverage delta stays valid because `run_gate` diffs live
`earned_top30` against the pinned baseline directly.

| Pre-registered gate | Result | By-the-letter |
|---|---|---|
| Primary: headroom-subset mean Δ`earned_top30` ≥ +5 | **+30.0** (all 25 dead → 30) | PASS |
| Guard A: golden-500 NDCG@30 drop within noise half-width | 0.095696 → 0.093416, **Δ −0.00228** vs half-width **±0.0105** | PASS (within noise) |
| Guard C: `rule_quality_gate --rule team_anthem_payoff` | neutral (+0.000 / +0.000), verdict PASS | PASS |
| Guard D: `hidden_gem_hit_rate` no crater | **+0.0291** (improved) | PASS |
| Guard B: `--collinearity` not near-parallel | **unmeasurable** (tensor pinned flag-off; would need a re-pin) | ⚠️ blocked |

**All measurable gates passed by the letter. It was DECLINED anyway** — the
gates measured the right things and the win was still bad. This is the key
lesson.

## Why DECLINE despite passing gates — the flood

1. **The coverage win is a flat flood.** `n_synergy_buckets = 1` for every
   dead commander: one rule (`team_anthem_payoff`) blankets all 30 top slots
   with token producers. The `earned_top30` 0→30 jump is **monotone, not
   diverse**. This is exactly the pathology the activation-poverty plan's own
   final review pre-warned: `earned_top30` (bucket-presence) counts "one rule
   flooding 30 cards" as fully covered. The `+30` primary "pass" is that
   illusion.

2. **The NDCG pass is dilution, and the harm lands on the rule's own targets.**
   The −0.00228 aggregate is within noise only because it is averaged across
   500 commanders. **All 24 regressors are cohort members** (zero collateral —
   the commander-side gate confinement works), and several take real hits:
   Bruenor Battlehammer **−0.18**, Surrak Dragonclaw **−0.18**, Iroas **−0.12**,
   Avacyn **−0.11**, Sephara **−0.11**. `hi_syn_loss = 0` — the rule does not
   *remove* EDHREC-synergy cards, it *reorders them down* to make room for the
   token flood. So the rule degrades EDHREC alignment for exactly the
   commanders it targets.

3. **Root cause is inherent to the design, not the weight.** "Team anthem →
   all ~2700 token producers, flat per-class rule credit" cannot discriminate
   *which* producer fits *this* anthem. The two-tier IDF surfaces premium
   producers first (Academy Manufactor, Doubling Season, Xorn — so it is not
   pure noise like `death_outlet_feeder`), but the tail is undifferentiated. A
   lower multiplier reduces the NDCG cliffs but still ships a monotone flood;
   there is no weight that turns a flat class into diverse synergy. Same shape
   as [[death-outlet-feeder-null-result-2026-07-07]] ("1,996 outlets share one
   broad IDF key → identical flat contribution = whitelist-equivalent").

## Lessons for the next coverage cycle

- **`earned_top30` is necessary but not sufficient.** A `+Δearned` win driven
  by `n_synergy_buckets = 1` is a flood, not coverage. Any future coverage-rule
  gate must read the diversity number alongside the bucket-presence delta —
  weight by distinct rules firing, or require the added cards to be a
  *discriminated* subset, not a whole card class.
- **A within-noise aggregate NDCG can still hide concentrated in-cohort
  cliffs.** Always partition the regression by cohort membership; an aggregate
  diluted across 500 commanders masks −0.18 cliffs on the ~25 the rule touches.
- **"Commander's own passive static IS the payoff" is a real, large gap — but
  the payoff must be discriminated, not a whole class.** A future attempt
  should condition the candidate on *which* keyword/buff the anthem grants
  (evasion-anthem → evasive bodies; indestructible-anthem → high-value
  permanents) so the credit varies per candidate, rather than crediting every
  token producer equally. The keyword-matched payoff was explicitly deferred in
  this spec; this null-result is the evidence it was the load-bearing part.
- **The instrument worked.** The census surfaced the cohort *and* the gate
  honestly exposed the flood (`n_synergy_buckets = 1`). The coverage pivot did
  not re-run the July DECLINE loop — it produced a clean, well-evidenced
  null-result on a real gap.

## Post-DECLINE code review — correctness fixes applied (2026-07-09)

A high-effort review of the standing infra (`/code-review` on PR #107) found
correctness bugs that were dormant behind the flag but confounded the
measurement and would have shipped wrong scoring on any retry. Fixed in-tree
(still flag-off, hash-neutral, `--expect-identity` PASS):

- **Gate was over-permissive.** `base = alt.split('.')[0].split('+')[0]`
  rejected subtype-*first* scopes (`Goblin.YouCtrl`) but not a subtype/condition
  folded in as a `+`-qualifier after a `Creature`/`Permanent` base — Admiral
  Beckett Brass (`Creature.Pirate+Other+YouCtrl`, a Pirate lord) and Abzan
  Falconer (`Creature.YouCtrl+counters_GE1_P1P1`, conditional) wrongly
  qualified. Fixed with a benign-qualifier allowlist (`{YouCtrl, Other}`); every
  other qualifier now excludes. **The `team_anthem` cohort dropped from 155 to
  54** — so the "155 members / 25 dead" figures in the measurement above were
  computed on a *polluted* cohort (they over-counted restricted lords /
  conditional / color-restricted anthems).
- **Doubler tier scored harmful/irrelevant cards as positive.** Filtered to
  own-board creature-token *multipliers* (`ReplaceWith ∈ {DoubleToken,
  TripleToken}`, not opponent-scoped): drops Halving Season (`HalveToken` —
  inverted polarity), Academy Manufactor / Xorn (non-creature `TokenReplace`),
  Bloodspatter (opponent). Notably Academy Manufactor — cited above as a
  "premium producer" surfaced for Avacyn/Iroas — makes NO creature bodies, so
  the flood was even dirtier than the measurement characterized.
- **Producer tier credited opponent-owned tokens.** Now excludes
  `TokenOwner` = `*Opponent*` / `Targeted*` (Akroan Horse, Forbidden Orchard,
  the Hunted cycle).
- Also applied: `candidate_cache` for the producer tier (matches the
  `subtype_supply` batch pattern), a shared `_parse_scope_alt` helper (removes
  the copy-paste with `_find_anthem_payoffs`, verified byte-identical via
  `--expect-identity`), and the gate helper renamed to
  `_commander_has_team_anthem_static` (returns `bool`).

**The DECLINE verdict is UNCHANGED.** These fixes make the flood *cleaner*, not
*discriminated* — the rule still credits ~2120 creature-token producers at flat
per-tier IDF (`n_synergy_buckets = 1` per commander). The root cause is
untouched.

### Retry-blockers still open (deferred — design decisions, not bugs)

Two cross-rule double-counts were found but NOT fixed, because de-conflicting
them is a redesign decision the retry must make, not a mechanical patch:

- **Overlap with `token_producer` (`_find_static_strategy`, tokens.py).** That
  already-wired rule emits `token_producer` for `Creature.YouCtrl` pump anthems;
  team_anthem_payoff double-credits the same candidates (live: Bruenor
  Battlehammer earns BOTH on 2130 candidates). Bruenor was the worst NDCG
  regressor (−0.18) in the measurement — largely double-count stacking, not new
  coverage. The rule's only non-redundant value is the `AddKeyword` / `Permanent`
  cases (Avacyn indestructible, Iroas menace) that `_find_static_strategy`'s
  `AddPower`-only branch misses. A retry must either narrow to the non-overlapping
  cases or subsume `_find_static_strategy`.
- **Overlap with `anthem_payoff` for dual-role pairs.** A commander that both
  hosts a team anthem and makes tokens, paired with a candidate that is both an
  anthem and a token producer, scores under both rule_ids. Run
  `bench.py audit --collinearity` on the pair before any flag flip.

## Standing infrastructure left in-tree (flag off, hash-neutral)

- `complement_rules/statics.py`: `_commander_has_team_anthem_static` (gate),
  `_find_team_anthem_payoffs` (emitter), `_ENABLE_TEAM_ANTHEM_PAYOFF = False`.
- `complement_rules/registry.py`: flag-aware `RuleGate("team_anthem_payoff", …)`
  — reports NO coverage while the flag is off, so `gap_report` /
  `rule_quality_gate` see the static.Continuous signature as still-unserved.
- `complement_rules/core.py`: emitter wired into `find_all_complements`
  (emits nothing while the flag is off).
- `bench/cohorts.py`: `team_anthem(conn)` predicate (155 members) +
  `coverage_report._COHORT_DISPATCH["team_anthem"]`.

Self-activates for a retry cycle if the flag flips; a retry should first
redesign the candidate side to be keyword-discriminated (see lessons above).
