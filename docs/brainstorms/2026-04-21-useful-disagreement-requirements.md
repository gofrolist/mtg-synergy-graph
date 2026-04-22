---
date: 2026-04-21
topic: useful-disagreement
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 7)
status: draft (brainstorm 7 of 7)
depends_on:
  - 2026-04-21-unified-eval-harness-requirements.md (bench.py as host for the metric)
---

# Requirements: Useful-Disagreement Objective with NDCG Floor

## Problem Statement

The project's auto-memory records the intent three separate times: `feedback_edhrec_not_goal`, `feedback_edhrec_hivemind`, `feedback_edhrec_synergy_formula` all say the goal is "finding hidden gems from mechanics," with EDHREC serving only as a sanity check or tiebreaker. Yet the entire optimization loop today — `_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, every recent commit — is driven by NDCG@30 against EDHREC top-30. A rule that lifts NDCG purely by chasing popularity bias gets accepted even though it violates the stated design principle.

This is a live coherence gap between stated intent and revealed optimization preference. The goal of this brainstorm is not to replace NDCG, but to make the design principle measurable: track a complementary metric that explicitly rewards finding mechanically-plausible cards EDHREC has missed.

## Goals

1. Define a concrete, computable metric (`hidden_gem_hit_rate`) that captures "our scorer found a mechanically-plausible card that EDHREC top-30 didn't."
2. Track it on every audit run alongside NDCG@30. Two metrics, one report.
3. Keep NDCG@30 as the commit gate initially (secondary-metric framing — see 'Non-Goals' for why not gating on both).
4. Flag sudden drops in `hidden_gem_hit_rate` in audit output for human review — the existing guardrail already catches NDCG regressions; this makes the second axis visible.
5. Document an explicit escalation path to promote `hidden_gem_hit_rate` to a commit gate once the metric is validated.
6. Give rule-authoring decisions a second anchor: when a new rule lifts NDCG but drops `hidden_gem_hit_rate`, the verdict flips from "ship it" to "investigate."

## Non-Goals

- Replacing NDCG@30 with `hidden_gem_hit_rate` as the primary optimization target at MVP. That paradigm shift is risky without a proven plausibility gate and a validated metric.
- Blocking rule landings on `hidden_gem_hit_rate` regression. First cut is tracking-only.
- Redefining "hidden gem" based on community feedback / deck-building user studies / human ratings. The plausibility definition stays mechanical.
- Diversity metrics across commanders (pairwise top-30 Jaccard distance across same-color-identity commanders). Adjacent idea; revisit separately if `hidden_gem_hit_rate` lands successfully.
- Decreasing reliance on EDHREC as eval oracle — EDHREC top-30 is still the reference set against which "hidden" is measured. Memory note `feedback_edhrec_not_goal` says EDHREC is a sanity check; this survivor keeps that framing.

## Users and Scenarios

| Scenario | Before | After |
|---|---|---|
| Rule authored that lifts NDCG by 0.005 but concentrates on EDHREC top-10 staples | Accepted as POSITIVE | Accepted on NDCG gate; `hidden_gem_hit_rate` delta logged as ≈0. Dev sees the rule is "safe but not gem-finding" |
| Rule authored that lifts NDCG by 0.001 and lifts `hidden_gem_hit_rate` by 0.03 | Accepted as MARGINAL (NDCG delta too small) | Accepted on NDCG gate (not HARMFUL); hidden-gem delta surfaces the real value; commit message can call it out |
| Rule authored that lifts NDCG by 0.008 but drops `hidden_gem_hit_rate` by 0.05 | Accepted as POSITIVE | Accepted on NDCG gate; audit report prints a warning: "this rule shifted scoring toward EDHREC top-10 staples; review before merging." Dev decides. |
| Weekly rollup | Hard to answer "are we getting better at gem-finding?" | `bench.py audit --trend hidden_gems` plots the metric over the last N commits |

## Functional Requirements

### FR1 — `hidden_gem_hit_rate` metric definition

For each commander in the 100-cmdr golden set:

```
our_top_30(cmdr) = set of cards our scorer ranks in top-30 for this cmdr (color-legal, not-commander-self)
edhrec_top_30(cmdr) = set of cards EDHREC lists in top-30 "high synergy" for this cmdr

hidden_candidates(cmdr) = our_top_30(cmdr) \ edhrec_top_30(cmdr)
plausible_hidden(cmdr) = { c in hidden_candidates(cmdr) | mechanical_plausibility(cmdr, c) }
hidden_gem_hit_rate(cmdr) = |plausible_hidden(cmdr)| / 30
```

Aggregate metric = mean over 100 commanders.

### FR2 — Mechanical-plausibility gate (MVP definition)

At MVP, a card `c` is "mechanically plausible" for commander `cmdr` if:

```
plausibility(cmdr, c) = (N_rules_firing(cmdr, c) >= 2) OR (total_rule_contribution(cmdr, c) > median_contribution(cmdr))
```

Where:
- `N_rules_firing(cmdr, c)` = number of distinct complement rules firing a positive contribution for this pair (read from Survivor 1's tensor).
- `total_rule_contribution(cmdr, c)` = sum of IDF-weighted rule contributions for this pair.
- `median_contribution(cmdr)` = median `total_rule_contribution` across all candidates our scorer ranks non-zero for this commander.

Rationale: a card isn't a "gem" just because we rank it above random; it has to show real mechanical evidence. The OR clause catches high-contribution-from-one-rule cases (e.g., a counter doubler with a single massive rule hit).

### FR3 — `bench.py` integration

Survivor 1's `bench.py audit` output adds two lines per report:

```
aggregate ndcg@30       : 0.262341  (Δ +0.002105 vs pinned baseline)
hidden_gem_hit_rate     : 0.1467    (Δ -0.0034   vs pinned baseline)
```

A new `--trend hidden_gems` subcommand plots the metric over the last N commits as a sparkline or CSV. Used for "are we gem-finding over time" retrospectives.

### FR4 — Warning on drop (but no gate)

When `hidden_gem_hit_rate` drops by more than `_HIDDEN_GEM_WARN_THRESHOLD` (initial `0.02`) on a single change, `bench.py audit` prints a conspicuous warning line:

```
⚠ hidden_gem_hit_rate dropped 0.034 on this change (from 0.147 to 0.113).
  Inspect: `bench.py audit --inspect-gems` to see which gems were lost.
```

The commit proceeds. The warning surfaces the second-axis regression for the dev to weigh. Pairs with the existing HARMFUL-verdict warning from Survivor 1's pre-commit hook.

### FR5 — `bench.py audit --inspect-gems` diagnostic

New subcommand shows per-commander hidden-gem deltas:

```
commander            hidden_gems Δ   lost gems            gained gems
Animar               -2               Spellbinder, Kraj    —
Yuriko               +1               —                     Curtains' Call
Gitrog               0                —                     —
...
```

Reads from Survivor 1's persisted tensor. Used to diagnose "which commanders lost which gems" when the warning fires.

### FR6 — Documented escalation path

A section in the doc (and in the code comment near `_HIDDEN_GEM_WARN_THRESHOLD`) lays out the promotion criteria: after `hidden_gem_hit_rate` has been tracked for ≥ 20 commits AND humans have confirmed that its drops correlate with subjectively-bad recommendation changes AND no false-positive drops (legitimate refactor changes that the metric flagged spuriously), propose promoting it to a commit-gate alongside NDCG. Promotion is itself a brainstorm-scoped change.

## Success Criteria

1. **Metric is computable and stable.** On the current HEAD with no scoring changes, `bench.py audit` reports a consistent `hidden_gem_hit_rate` across three consecutive runs (determinism).
2. **Baseline established.** A specific value is recorded in the pinned reference fixture. First-run value is whatever it is; subsequent values are deltas against that pin.
3. **Regression discrimination.** At least one historical commit from the last month that was accepted by NDCG alone but concentrated its lift on EDHREC-top-10 staples is retrospectively flagged by `hidden_gem_hit_rate` as a zero-or-negative change. Demonstrates the metric catches a real pattern.
4. **Low false-positive rate.** Across a stratified sample of 10 recent accepted commits, no more than 1 triggers a spurious `hidden_gem_hit_rate` warning (false-positive rate ≤ 10%).
5. **Explainability.** For any commander, `bench.py audit --inspect-gems --commander <name>` enumerates which specific cards count as gained/lost hidden gems. Humans can sanity-check the metric against their intuition.

## Constraints

- Metric consumes only data we already have (our top-30, EDHREC top-30, rule-contribution tensor). No new data sources.
- Plausibility gate is explicitly mechanical — no learned classifier, no text-based heuristic.
- NDCG@30 stays the primary commit gate at MVP. Any temptation to flip it later goes through the escalation path in FR6, not a silent config change.
- Commander pool for aggregate is the same 100-commander golden set used for NDCG@30. No second set at MVP.

## Open Questions (For Planning Phase)

- Whether `mechanical_plausibility` should tighten after the Survivor 6 embedding lands (could add an embedding-cosine clause: "or cosine to commander-target > τ").
- Whether the metric should be weighted by commander popularity — currently gives equal weight to every commander. Likely OK at MVP.
- How to handle commanders without EDHREC data (a few of the 100). Current options: skip them entirely, treat all our top-30 as "hidden," or imputational proxy.
- Whether "hidden" should use EDHREC's "high synergy" list or "all top N" list — current answer is top-30 synergy to match the NDCG reference.
- How to render the trend plot — ASCII sparkline, Markdown line chart, or CSV only.
- Whether to add a companion "EDHREC-agreement delta" number to the report (the inverse metric) for completeness.

## Out of Scope for This Brainstorm

- Pairwise top-30 Jaccard diversity across same-color commanders.
- Moving to a 2,761-commander pool (Survivor 1 scope; revisit in sequence).
- Removing EDHREC data from the project. EDHREC stays as the reference oracle for both NDCG and this metric.
- Gating on `hidden_gem_hit_rate`. Explicitly non-goal at MVP; escalation path in FR6.

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 7.
- Prerequisite: `2026-04-21-unified-eval-harness-requirements.md` FR2 tensor (plausibility gate reads from it), FR3 bench.py CLI, FR4 histogram.
- Memory alignment: `memory/feedback_edhrec_not_goal.md`, `memory/feedback_edhrec_hivemind.md`, `memory/feedback_edhrec_synergy_formula.md` — this survivor is the direct operationalization of all three.
- Guardrail interaction: `memory/feedback_audit_every_change.md` — NDCG remains the primary gate; this metric is advisory at MVP.
- Potential integration: `2026-04-21-content-embeddings-requirements.md` — FR2's plausibility clause could expand to include embedding cosine once embeddings are proven.
