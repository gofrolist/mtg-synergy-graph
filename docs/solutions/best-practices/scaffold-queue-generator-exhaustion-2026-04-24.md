---
last_updated: 2026-04-24
module: complement_rules
title: Scaffold walker exhaustion — bottleneck shifts to generator catalog
tags:
  - scaffold_rule
  - forge_oracle
  - rule-authoring
  - null-result
  - generator-catalog
  - plan-002
problem_type: best_practice
resolution_type: reference
applies_when:
  - Running `scripts/scaffold_rule.py --walk N --apply` returns "Queue exhausted" with zero attempts.
  - `--show-template-stats` shows most or all templates BLOCKED.
  - The forge_oracle propose-rules output still shows dozens of eligible proposals.
created: 2026-04-24
sweep_ref: git log --oneline on 2026-04-24 after scripts/scaffold_rule.py --walk 5 --apply
---

# Scaffold walker exhaustion — bottleneck shifts to generator catalog

When the scaffold walker drains the queue without attempting any
proposal, the limit is no longer "which rule to add next" but "which
generator template can express a new rule." Recording this state is
load-bearing: without it, future engineers will re-run the walker
expecting output, conclude the pipeline is broken, and re-debug from
scratch.

## Context

Plan `docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md`
landed `forge_oracle` which produces a forge-signal-weighted proposal
queue via `scripts/forge_oracle.py propose-rules`. Plan
`docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md`
and its predecessors expanded `scripts/scaffold_rule.py` into a
walker that drains that queue, scaffolds one rule per iteration, and
audit-gates each with auto-revert on NDCG regression.

As of commit `ac38957` (forge_oracle PPMI bugfix) + `9677097` (VOCAB v3)
on 2026-04-24, the pipeline is operational end-to-end:

- `forge_oracle propose-rules` returns a ranked queue with real
  forge_signal variation (1.00–1.50).
- `scaffold_rule.py --show-template-stats` tracks per-template pass
  rates and blocks generators below a threshold.
- `--walk N --apply` auto-scaffolds, validates, and commits if NDCG
  holds.

Running `scripts/scaffold_rule.py --walk 5 --apply` produces:

```
Queue exhausted (no more eligible proposals).

========== Walk summary ==========
  passed:   0  (—)
  marginal: 0
  trivial:  0
  reverted: 0
  skipped:  0
```

Zero attempts, zero passes, zero reverts.

## Root cause

The walker in `_pick_top_proposal` filters the auditor's ranked queue
by three independent criteria; a proposal must pass all three to be
attempted:

1. **Generator registered** — a Python generator exists for the
   template (`_GENERATORS[proposal.template]`).
2. **Template not blocked** — `is_template_blocked(template)` returns
   `False`. A template is blocked when its pass rate drops below the
   block threshold after enough attempts. Prevents burning audit-gate
   cycles on generators with systematic flaws.
3. **`(template, rule_id)` not known-bad** — a specific signature
   hasn't been previously reverted. Prevents re-trying a doomed
   combination without explicit `--force`.

As of 2026-04-24, `scaffold_rule.py --show-template-stats` reports:

```
template                     pass  triv  marg   rev  skip  fresh   rate  status
axis_feeder                      6     0     0     7     0     13   0.47  ok
counter_removal_payoff           0     0     0     4     0      4   0.17  BLOCKED
creature_count_scaler            0     0     0     3     1      3   0.20  BLOCKED
peer_tribal_keyword             17    26     0    11     0     54   0.32  BLOCKED
replacement_stack                2     2     0     3     0      7   0.33  BLOCKED
x_cost_scaler                    0     0     0     2     0      2   0.25  BLOCKED
```

Six of seven templates are BLOCKED. The only unblocked template,
`axis_feeder`, has ~30 eligible proposals — but all of them use
qualifiers not in `_AXIS_FEEDER_TIERS` (`Other`, `tapped`, `blocking`,
`counters_GE`, `token`, `untapped`), so the generator raises
`ValueError` and the walker skips to the next proposal. With no
viable candidate on any template, the queue exhausts.

Critically, the remaining high-forge-signal proposals in the queue
(e.g., `keyword.Choose[*]`, `keyword.Doctor's[*]`,
`keyword.Horsemanship[*]`, `keyword.Living[*]`) are legitimately
flavor-only keywords where the forge signal picks up precon-deck
thematic grouping rather than mechanical synergy. The 11 previously-
reverted `peer_tribal_keyword` attempts in the attempt log almost
certainly include these flavor keywords — which is why the template
as a whole blocked.

## What the exhaustion means

The pipeline isn't broken. It is telling the truth: **relative to
the current generator catalog, there are no more viable rule-scaffold
additions in the forge_oracle-ranked queue**. The 62-rule catalogue
has absorbed every pattern that the seven registered generators can
express.

Next-step options, in descending order of effort:

### Option A: Extend `_AXIS_FEEDER_TIERS` with more qualifiers

Lowest effort. `scripts/scaffold_rule.py` has a per-qualifier tier
table `_AXIS_FEEDER_TIERS` that maps qualifiers (`attacking`,
`entered_tapped`, etc.) to a multiplier and gate class. Adding rows
for `Other`, `tapped`, `blocking`, `counters_GE`, `token`,
`untapped` would unblock ~30 axis_feeder proposals. Each new tier
needs the reviewer to pick a multiplier (typically 1.5–2.5) and
confirm the gate class makes mechanical sense for that qualifier
axis. One commit per tier entry; subsequent `--walk` runs will
attempt each unblocked proposal with audit-gate protection.

### Option B: Refine `peer_tribal_keyword` to filter flavor keywords

Medium effort. The template currently proposes a tribal rule for
every keyword with a small-enough card pool. In practice, flavor
keywords (Doctor's, Living, Horsemanship, Choose) yield narrow
card pools without mechanical synergy — precon decks *do* share
these cards, which the forge_signal amplifies, but the cards don't
mechanically synergize at scoring time.

A `_KEYWORD_FLAVOR_BLOCKLIST` or a card-pool-mechanical-strength
heuristic (e.g., "keyword must appear on a card with an activated
ability referencing its own mechanic") would cut the flavor
keywords off the proposal queue. Paired with an attempt-log reset
(`--force`), the remaining mechanically-sound keywords (e.g.,
Prowess, Ward:2) could then be attempted fresh. Needs careful
heuristic design — over-filtering would exclude legitimate
mechanical keywords like Exploit or Aftermath.

### Option C: Author new generator templates for unregistered signatures

Highest effort. The queue contains proposals like
`cost.add_counter[*]` and `trigger.Phase[*]` whose templates are
`needs_template` — no generator exists. Each new template requires
the ontology work of: (i) deciding what "counter-add cost" or
"phase-based trigger" should score against, (ii) writing a Python
generator emitting the helper + test + integration patches, (iii)
registering it in `_GENERATORS`, (iv) unblocking via `--walk
--apply` with fresh attempt history.

Only worthwhile if there's a clear card-pool-signal pattern that a
generator can programmatically capture. For one-off mechanics, a
hand-written rule is usually faster than a template.

### Option D: Accept the saturation and move on

Zero effort. The 62-rule catalogue already produces NDCG@30 ~0.256
on the 100-commander golden set. Additional rule scaffolding is
deeply subject to diminishing returns; further NDCG lift is more
likely to come from orthogonal directions (better IDF weighting,
new commander-target composition for embeddings, richer
port-signature features, etc.) than from extending the current
rule catalog.

## Guidance

1. **Before running `--walk`, check `--show-template-stats`.** If
   every template is BLOCKED, the walker will silently return zero
   attempts. Save the cycle; address the catalog first.

2. **Don't force-override template blocks without understanding
   why.** The block is earned — previous attempts genuinely
   regressed. Re-trying without refining the template, the gate, or
   the proposal source just replays the same failure mode. Only
   `--force` after you've changed the generator or the input.

3. **Document each catalog-expansion commit with the proposal list
   it unblocks.** E.g., "Extend _AXIS_FEEDER_TIERS with
   `untapped` qualifier → unblocks proposals #10 (scales_with.Valid[
   untapped]), #23 (effect.Pump[untapped])." Future engineers then
   know which queue rows shipped from which commit.

4. **Re-scan the proposal queue after every VOCAB bump, Forge corpus
   refresh, or IDF weight change.** These can reshuffle rankings
   and surface previously-low-priority cells into the top slots.
   The attempt log is the only thing that prevents the walker from
   re-attempting known-bad signatures — but the queue itself is
   always recomputed.

## Why this matters

Without this doc, the pipeline looks like a mystery. An engineer
expecting `--walk` to add rules sees no output, concludes "something
is wrong with forge_oracle or the walker," and spends a debugging
session on a system that is operating exactly as designed.

The compounding value: the next engineer starts from "the catalog is
the bottleneck, not the queue" and immediately targets Option A, B,
or D — not another round of pipeline debugging.

## Re-check trigger

Re-run the walker when ANY of the following changes:

- `_AXIS_FEEDER_TIERS` gains a new qualifier entry.
- `peer_tribal_keyword` generator is refined to filter flavor keywords.
- A new generator is registered in `_GENERATORS`.
- A reverted signature's template is meaningfully changed — use
  `--force` on the specific signature.
- The Forge corpus is refreshed (`data/forge/` pin update).
- `VOCAB_VERSION` bumps and `port_nodes` classifications change.
- IDF weights change materially (affects impact × forge_signal
  ranking).

If none of these changed, re-running the walker will return the same
exhaustion. Don't.

## References

- `scripts/scaffold_rule.py` — walker, generators, attempt log.
- `scripts/forge_oracle.py propose-rules` — proposal queue producer.
- `scripts/gap_report.py` — underlying `rank_gaps` used by propose-rules.
- Sibling learning (forge_signal fix unblocking the queue):
  [`ppmi-smoothing-on-sparse-vocabulary-2026-04-24.md`](ppmi-smoothing-on-sparse-vocabulary-2026-04-24.md)
- Sibling learning (infrastructure value without scoring activation):
  [`infrastructure-without-scoring-activation-2026-04-24.md`](infrastructure-without-scoring-activation-2026-04-24.md)
- Sibling learning (rule-consolidation baseline):
  [`rule-consolidation-null-result-2026-04-24.md`](rule-consolidation-null-result-2026-04-24.md)
