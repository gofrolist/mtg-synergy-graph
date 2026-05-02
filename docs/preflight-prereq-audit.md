# Pre-flight Gate Stack v1.0 — Unit 0 Prerequisite Audit

**Date:** 2026-05-02
**Plan:** [docs/plans/2026-05-02-001-feat-preflight-gate-stack-plan.md](plans/2026-05-02-001-feat-preflight-gate-stack-plan.md)
**Owner review status:** ⏸ AWAITING REVIEW

This document captures Unit 0's prerequisite output. Owner reviews before
Unit 1 begins.

## Manual Labeling Pass Summary

24 unique `(template, rule_id)` reverts classified per the v1.0 plan's
6-mode taxonomy. Full labels with verbatim-quote reasoning at
[docs/rule_attempts_labels.jsonl](rule_attempts_labels.jsonl).

### Primary-label distribution

| Label          | Count | Notes |
|----------------|-------|-------|
| `other`        | 17    | Mostly shared-axis-different-archetype displacement (5th failure mode the plan documents but doesn't gate against in v1.0). |
| `vacuum_fill`  | 3     | All Ward family: ward_2 (canonical), ward_3, ward_1 (secondary flat_noise). |
| `untestable`   | 2     | partner_friends_tribal (0/2737 explicit), damage_prevention_payoff (0/100 golden documented). |
| `forge_flavor` | 1     | living_tribal (Living is Transformers-set flavor keyword per CLAUDE.md). |
| `test_failure` | 1     | attacking_axis_feeder (raw pytest crash). |

4 entries have a `secondary_label` for ambiguous cases:
- `landwalk_swamp_tribal`: other / forge_flavor (Sol'kanar -0.300 — could be partial flavor amplification)
- `ward_1_tribal`: vacuum_fill / flat_noise
- `living_tribal`: forge_flavor / flat_noise
- `damage_prevention_payoff`: untestable / test_failure (root cause untestable, literal reason was pytest crash)

### Distribution analysis

The dominant failure mode in the historical revert corpus is **shared-axis-different-archetype displacement** (17 of 24). The pre-flight stack as designed (Stages A/B/C) does NOT gate against this — it is acknowledged in the plan as a 5th failure mode to be addressed (if at all) in a separate future cycle. Of the 4 documented modes the stack does target:

- **Untestable** (Stage A REJECT/WARN): 2 of 24 historical reverts.
- **Vacuum-fill** (Stage B/C, v1.5): 3 of 24.
- **Flat-noise** (Stage B/C, v1.5): 0 primary, 2 secondary.
- **Forge-flavor** (out-of-scope for v1 + v1.5): 1 primary, 2 secondary.

## Stage A Historical Sanity Check

**Question:** Of the 24 historical reverts, how many would Stage A have
caught (REJECT or FIXTURE_BLIND_SPOT) before the rule was scaffolded?

**Method:** A revert "would have been caught" if Stage A's two-corpus
check (fixture commanders + legal-universe commanders) would produce
REJECT (0 fixture, 0 legal-universe) or WARN (0 fixture, ≥3 legal-universe).
We use the labeled `primary_label = untestable` entries as the proxy for
"0 fixture firings", since `untestable` is defined as "gate fires on 0
commanders in the evaluation fixture" per the plan's failure-mode
definitions.

### Result: **2 of 24 (8.3%)**

| rule_id | Primary label | Stage A verdict |
|---|---|---|
| `partner_friends_tribal` | untestable | **REJECT** (0/2737 explicit — fires on no commander in either fixture or legal universe) |
| `damage_prevention_payoff` | untestable | **WARN: FIXTURE_BLIND_SPOT** (0/100 golden, 31 legal-universe per Unit 0 verified data) |

The other 22 reverts have evidence of firing on at least one commander
(specific names mentioned in their reason text: Chatterfang, Goose Mother,
Storvald, Emet-Selch, Watcher, Venser, Sol'kanar, Djeru, Bladewing, etc.),
meaning their gates would have produced PASS verdicts under Stage A.

### Decision rule application

Per the plan's Key Technical Decisions section:

> **Stage A historical sanity check has an explicit decision rule**:
> If Unit 0's check shows Stage A would have caught < 3 of 24 historical
> reverts, owner explicitly acknowledges in `preflight-prereq-audit.md`
> that v1.0 ship justification rests on the canonical save case alone.
> If owner cannot defend the forward-looking claim with concrete in-flight
> evidence (categories of proposals likely to hit zero fixture commanders),
> reduce v1.0 scope further: ship Stage A library only, defer gap_report.md
> integration (Unit 2) and walker integration (Unit 3) to a separate
> decision.

**2 of 24 < 3 of 24 → decision rule fires → owner acknowledgment required
before Unit 1 begins.**

## ⏸ Required Owner Acknowledgment (before Unit 1)

The historical catch rate is below the 3-of-24 threshold. To proceed with
Units 1-3 as planned, owner must acknowledge ONE of the following options
by writing the chosen acknowledgment under "## Owner Decision" below:

### Option A: Proceed with full v1.0 (Units 1-3)

> "I acknowledge that v1.0's value claim rests primarily on (a) prevention
> of damage_prevention_voltron-class waste and (b) the library shape that
> v1.5 will extend. Forward-looking justification: [SPECIFIC EVIDENCE — e.g.,
> 'gap_report top-10 currently includes ≥2 entries similar to
> damage_prevention_voltron in archetypes the 500-fixture doesn't cover',
> OR 'I expect to attempt ≥10 new rules in the next month and want the
> Stage A gate in place from day one']. Proceeding with Units 1-3."

### Option B: Reduce scope — ship Stage A library only (Unit 1), defer Units 2-3

> "I acknowledge the historical catch rate is too low to justify the
> integration cost of Units 2-3 right now. Ship Unit 1 (library + Stage A
> gate + tests) only. Library is available for explicit invocation by
> humans before generator-writing. Defer Unit 2 (gap_report.md column) and
> Unit 3 (walker integration + override CSV) until ≥3 new untestable cases
> appear in the wild."

### Option C: Defer v1.0 entirely — re-evaluate against ce-ideate survivors

> "I acknowledge the historical catch rate suggests Stage A's recurring
> value is too low to justify even Unit 1. Defer the entire pre-flight
> workstream. Return to docs/ideation/2026-04-26-applying-built-tooling-ideation.md
> Continuation 2026-05-02 and pick a different survivor (likely #3
> Optimizer-as-gap-discovery, which mines existing M1 artifacts and has
> zero new infrastructure)."

## Owner Decision

**Option A: Proceed with full v1.0 (Units 1-3)** (acknowledged 2026-05-02)

Forward-looking justification:

1. **Library shape is the long-term investment.** Even if Stage A's recurring
   catch rate stays at ~1-2 per year, the library + integration shape
   establishes the seam v1.5 will extend. Building Unit 2 (gap_report
   column) and Unit 3 (walker integration) costs ~1 day combined and means
   no future scaffolding work when v1.5 lands.

2. **Override CSV + walker_outcomes.csv accumulate evidence from day one.**
   These artifacts are required to evaluate the v1.5 plan trigger (≥30%
   residual reverts in B+C-predictable categories). Without Units 2-3,
   that evidence base never builds, and the v1.5 evidence trigger can
   never fire — locking the project into the capacity-trigger-only path.

3. **Catch rate may be understated by labeling conservatism.** I labeled
   17 entries as `other` (shared-axis-different-archetype) rather than
   forcing them into one of the 4 named modes. With looser interpretation,
   some of those 17 might be vacuum-fill-adjacent — meaning Stage B+C in
   v1.5 could catch more than the 3-of-24 the strict labeling shows.
   Stage A specifically catches the deterministic untestable subset which
   is the most expensive class of false-attempt (250 LOC of generator).
   Two saves/year at 250 LOC/save plus walker time is worth the integration
   cost.

4. **Walker autonomy benefits.** With the walker in autonomous loops, even
   2 historical untestable cases would have wasted full audit cycles
   without human intervention. Stage A REJECT lets the walker skip without
   even invoking the generator. The latency of the gate (two SQL queries,
   <1s) is negligible vs the cost of one wasted audit cycle (minutes).

Decision: proceed with Unit 1, Unit 2, Unit 3 as planned. v1.5 stays
deferred per the plan's v1.5 Plan Trigger section.

---

## Card-Attribute Audit (Deferred)

Per the v1.0 plan, the card-attribute audit (formal-rule vs card-attribute
candidate-shape distribution in the current top-50 gap_report) is **DEFERRED
to v1.5 planning**. v1.0 ships only Stage A which is a deterministic SQL
gate independent of the formal-rule / card-attribute distinction. The
audit is needed only when v1.5's R12 ephemeral registration is being planned.

When v1.5 is triggered (per the v1.5 Plan Trigger section in the plan),
re-open this audit document and add the card-attribute distribution result.
