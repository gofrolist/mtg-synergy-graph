---
title: Archetype-payoff subtype-link (producer→death-payoff) PARKED — IDF cannot damp broad-subtype floods; eval fixture is the real blocker
date: 2026-07-03
category: best-practices
module: complement_rules
problem_type: null_result
component: scoring_engine
symptoms:
  - "NO_RULES forensics bucket (~43%) dominated by on-theme archetype synergies scored zero (Slimefoot→Mycoloth Saproling producers)"
  - "Hypothesis: a token-producer → subtype-keyed-death-payoff link, IDF-weighted, recovers the misses without a per-archetype rule"
  - "Assumption: common subtypes (Zombie/Food/Treasure) self-damp via IDF because they are low-specificity"
root_cause: premise_false
resolution_type: parked_with_evidence
severity: medium
related_components:
  - universal_scorer
  - port_graph
  - bench
tags:
  - kill-test
  - null-result
  - no-rules-bucket
  - archetype-payoff
  - idf
  - flood
  - eval-fixture
  - stage0
applies_when:
  - "proposing any rule that fires on a broad candidate set and relies on IDF to prevent a top-30 flood"
  - "proposing subtype/tribal producer→payoff links"
  - "estimating whether a cohort-local win (few dozen commanders) is visible to the aggregate NDCG audit gate"
brainstorm_ref: docs/brainstorms/2026-07-03-archetype-payoff-subtype-link-requirements.md
---

# Archetype-payoff subtype link — PARKED at brainstorm (5-persona review)

## What was proposed

The primary open NO_RULES lever (surfaced 3× this session): connect a
token *producer* to a commander's subtype-keyed *death payoff* — e.g.
Slimefoot's `ChangesZone Saproling.YouCtrl → DamageAll` payoff ↔ any of
the 81 `token_subtype=Saproling` producers (Mycoloth, absent from his
top-200). Substrate is real: 2,721 producer cards (`port_attributes.
attr_kind='token_subtype'`), ~75 aristocrats-shaped (death-keyed)
demanding commanders. First cut scoped narrowest (aristocrats-only),
IDF-weighted, no per-subtype whitelist.

## Why it was PARKED (core premise arithmetically false)

The bet rested on "common subtype → low IDF → flood self-damps." IDF in
this engine is `1/log2(1+N)` (`universal_scorer.py:869`, N = distinct
candidates per `(rule_id, cmdr_event, cand_event, filter_group)` tuple).
Even with `filter_group=subtype` (best case, N = per-subtype supply):

| Subtype | N | IDF `1/log2(1+N)` |
|---|---|---|
| Egg / Cleric | 2 | 0.63 (≈0 coverage) |
| Sliver | 8 | 0.32 (≈0 coverage) |
| Saproling | 81 | **0.157** |
| Zombie | 145 | **0.139** |
| Treasure | 289 | **0.122** |

`log2` compresses N=46–289 into a **0.12–0.16 window**. IDF damps
*per-item weight*, but the flood is driven by *count*: 145 Zombie
producers × 0.139 each, against a commander scoring its archetype cards
at zero, cracks the top-30. **There is no N-range that is both
flood-safe AND high-coverage** — the only genuinely high-IDF subtypes
have ~2 producers. Any "principled specificity threshold" separating
Saproling (0.157) from Zombie (0.139) is a ~0.018-wide slice — i.e. a
raw supply cutoff, which IS the rarity whitelist
`feedback_no_individual_rules` forbids. Honest outcome space:
DECLINE-or-disguised-whitelist. Verified independently by two reviewers
against the code.

## Compounding review findings

- **Eval-set dilution:** only ~2–3 of the 75 demanding commanders sit in
  the golden-100 audit fixture with a subtype-keyed death trigger. A
  genuine cohort-local win dilutes below the fixture noise band, so the
  aggregate-NDCG SHIP gate is **structurally unable to register it**.
  (The Stage-0 gate would measure intra-cohort share; the SHIP gate is
  whole-fixture aggregate — different populations.)
- **Not even a direct link:** producing subtype-S tokens does not fire an
  S-death trigger — an intermediate *sacrifice outlet* is required. The
  edge is a depth-2 path (produce → sac outlet → death) with the middle
  node unmodeled; a direct match re-creates the pathway `valid_filter`
  channel already cut for flooding (2026-04-23).
- **Direction may be the minority half:** EDHREC aristocrats lists favor
  sac *outlets* and death-payoff *pieces* (Blood Artist), not token
  *producers*; the producer direction rests on one anecdote.
- **Overlap with `self_bridging_cascade`** (targets "aristocrats-chain
  token-makers") unmeasured — marginal addressable share may be smaller.

## What this closes and what stays open

- **Closed:** IDF-weighted broad-subtype-production as a flood-safe
  mechanism. Do NOT propose "IDF will damp the flood" for any rule whose
  candidate set exceeds ~40 members — the log2 curve is too flat.
- **Open — the real blocker the cycle surfaced:** the **eval fixture**.
  The golden-100/500 audit cannot see a win concentrated in a few-dozen-
  commander cohort. Rebuilding / over-sampling the audit fixture on the
  archetype-payoff cohort is the prerequisite for measuring ANY
  archetype-payoff variant, is clean infra with zero flood risk, and is
  the chosen next step. The archetype-payoff *lever* is NOT disproven —
  see `memory/project_no_rules_archetype_gap.md`.
- **Open (surviving reframes):** intrinsically-narrow depth-2 gate
  (extend `self_bridging_cascade` with a `token_subtype` channel);
  outlet/payoff-piece direction instead of producer.

## Method notes

- **Compute the IDF curve before betting on it.** A "low-specificity →
  low-weight" intuition is worthless without the actual `1/log2(1+N)`
  values; log2 flattens everything above N≈40 to a narrow band. This is a
  general property, not specific to subtypes.
- **Check gate ↔ ship-gate population match.** A Stage-0 intra-cohort
  metric that the aggregate SHIP gate can't see is a green-light to build
  something the audit will DECLINE on dilution, not merit. Fix the ruler
  before the rule.
- **A "general mechanism" can still be a per-archetype rule** if the only
  way to make it work is a supply/rarity cutoff hand-fit to the flood set.
