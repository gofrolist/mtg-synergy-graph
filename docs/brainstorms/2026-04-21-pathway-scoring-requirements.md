---
date: 2026-04-21
topic: pathway-scoring
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 5)
status: draft (brainstorm 5 of 7)
depends_on:
  - 2026-04-21-unified-eval-harness-requirements.md (audit gate)
  - optional: 2026-04-21-typed-port-graph-requirements.md (canonical nodes make path pattern matching cleaner; not strictly required)
---

# Requirements: Depth-2 Self-Bridging Path Scoring

## Problem Statement

The current complement-rule system matches at depth-1: one commander port ↔ one candidate port. Engine-grade commanders (Korvold, Muldrotha, Meren, Teysa) gain most of their value from *cascades* — a single card contributes multiple ports that form a chain the commander's triggers can exploit in sequence.

Example: Korvold's trigger = "when you sacrifice a permanent, draw a card." A candidate like Bloodghast has two ports: landfall ETB + return-from-graveyard on landfall. These two ports form a self-bridging chain: Bloodghast enters, gets sacrificed (triggering Korvold), returns next turn, gets sacrificed again. Current rules see these as two independent port-matches; the *loop* structure that makes Bloodghast specifically valuable for Korvold is invisible to the scorer.

The dark-gap `scales_with.Valid[*]` at 34% coverage is largely relational/referential ports — exactly what path-based matching catches.

## Goals

1. Identify candidates whose internal port-chain forms a depth-2 loop that reinforces two distinct commander-port matches.
2. Add a new complement rule family `self_bridging_cascade` that fires on such candidates.
3. Stay within a single candidate card at depth-2 — no cross-card multi-hop combinatorics in this survivor.
4. Produce explainable recommendations — `--explain` output must show the path discovered.
5. Preserve the audit guardrail: the new rule family is rolled out behind a feature flag and audited independently.

## Non-Goals

- Multi-card paths (commander → card A → card B → commander). Defers combinatorial complexity to a later survivor once depth-2 proves useful.
- Path depth ≥ 3. Defers to a later phase.
- Bridge-card detection (lifting cards that enable paths between *other* candidates). That's a richer model; revisit only if depth-2 lands positively.
- Replacing `cost_feeds_trigger`. This survivor extends it, doesn't supplant it. The new rule family is additive.
- Deck-so-far / evolving-query style multi-card scoring (that's a separate UX feature, not a model improvement here).

## Users and Scenarios

| Scenario | Before | After |
|---|---|---|
| Korvold page shows Bloodghast at rank 22 | Bloodghast has one direct-match port (creature-ETB) | `self_bridging_cascade` detects ETB→return-from-GY loop that self-feeds Korvold's sacrifice trigger. Bloodghast rises toward top-10. |
| Muldrotha page misses cards with "returns from graveyard" self-loops | Current rules only match depth-1 grave-return → cast-from-grave | New rule detects 2-port candidates whose internal chain loops through GY, lifting e.g. Gravecrawler, Nether Traitor. |
| Teysa + Aristocrats card | Single `DIES → payoff` match per rule | Depth-2 match detects a candidate whose ETB *and* DIES ports both chain to Teysa's token-doubling trigger; scored higher. |
| Explainability | `--explain` lists rule hits | `--explain` adds a `path:` line per self-bridging match: `Bloodghast: ETB (feeds Korvold sacrifice) → return-from-grave (re-enters → feeds again)`. |

## Functional Requirements

### FR1 — Gate condition

`self_bridging_cascade` rule fires on a candidate if and only if:
1. Commander has ≥ 2 distinct port-shape matches into the candidate's port bag (any two of commander's ports each match at least one of the candidate's ports).
2. The candidate has an internal port-graph edge between at least one of the matched ports and another port on the same card that either (a) matches a different commander port or (b) feeds back into the first matched port via a standard equivalence (from `EVENT_MATCH_MAP` / `COST_FEEDS_TRIGGER`).

Concretely: for each candidate, enumerate the set `M` of its ports that match *any* commander port. The rule fires when `|M| ≥ 2` AND there exists a port-graph path of length ≤ 2 between two ports in `M` through the candidate's own port bag (including `EVENT_MATCH_MAP` equivalences).

### FR2 — Scoring model

The rule produces a single `PortComplement` per firing with:
- `rule_id = "self_bridging_cascade"`
- `cmdr_event` = combined label of the two matched commander ports (deterministic)
- `cand_event = "self_bridging"`
- `filter_group = "depth_2"`
- A single IDF contribution — no multiplicative bonus on existing rule matches (that would double-count).

The contribution is *additive*: the candidate still receives its base per-rule contributions for the depth-1 matches; the pathway match is a separate third contribution. IDF is computed over the population of candidates for which the rule fires (same mechanism as all other rules).

### FR3 — Port-graph walker

A utility `_walk_self_paths(candidate_ports, commander_ports)` computes the pathway structure:
1. Intersect candidate's ports against any commander port → set `M`.
2. For each pair `(p1, p2) ∈ M × M, p1 ≠ p2`, check whether the candidate's port-graph has an edge `p1 → p2` via:
   - A shared `valid_filter` reference (e.g., port A's target matches port B's type).
   - An `EVENT_MATCH_MAP` entry (A produces a zone change that feeds B's trigger).
   - A `COST_FEEDS_TRIGGER` chain local to the candidate.
3. Return the first path found; terminate early once the rule has fired once per candidate. Path-length cap is 2 by construction.

### FR4 — Feature flag + audit gate

Ship behind a single config constant `_ENABLE_PATHWAY_RULES = False` (default off). Enable via audit:
1. Flip to `True`.
2. Run `bench.py audit` against pinned baseline.
3. Expect positive verdict on commanders named in FR1 examples (Korvold, Muldrotha, Meren, Teysa); expect near-zero impact on commanders without multi-port synergy.
4. If CONTENTIOUS: investigate losers; either tune gate and retry, or revert.
5. If HARMFUL: revert. Do not ship.

### FR5 — Explainability

`recommend.py --explain` output gains a per-contribution `path:` line when the `self_bridging_cascade` rule fires. Path is rendered as:

```
  self_bridging_cascade: ETB(Bloodghast) ⇌ Landfall(Bloodghast)
    (Korvold's sacrifice trigger consumes ETB; Landfall returns Bloodghast to battlefield)
```

### FR6 — No expansion to cross-card paths

Any logic that would walk across multiple candidate cards is explicitly excluded in this survivor. A future survivor (documented as follow-up in this doc's closing section) can explore bridge-card scoring; that work depends on first demonstrating depth-2 single-card paths are a net-positive signal.

## Success Criteria

1. **Landing verdict.** `bench.py audit` on the 100-commander golden set with the feature flag on reports ≥ positive verdict: aggregate NDCG up, no commander regressed beyond CONTENTIOUS threshold.
2. **Target-commander uplift.** Korvold, Muldrotha, Meren, Teysa, and at least 4 more cascade-shaped commanders identified during audit each show ≥ 1 hi-syn gain in their top-30.
3. **Non-target-commander impact ≤ noise.** Commanders with fewer than 2 relevant ports (e.g., voltron commanders like Uril, Rafiq) show no measurable NDCG movement.
4. **Explainability landing.** `recommend.py --commander Korvold, Fae-Cursed King --top 10 --explain` includes at least one `path:` line showing a legible 2-port self-bridging chain.
5. **Runtime impact.** Per-candidate scoring runtime increases by ≤ 10% — the port-graph walker is bounded by `|M|²` per candidate, and `|M|` is typically ≤ 5.

## Constraints

- No unbounded-length path search. Hard cap at 2.
- No new port extraction — works entirely off the existing `card_ports` + `port_attributes` tables.
- No multi-card fan-out. The walker operates on one candidate at a time.
- IDF denominator is the pool of candidates for which the rule fires — same bucketing mechanism as all other rules.

## Open Questions (For Planning Phase)

- Exact definition of "port-graph edge" within a single card — which columns of `card_ports` + `port_attributes` are joined to infer internal port dependencies?
- Handling of multi-word cascades (3 ports on a card, any 2 of which form a self-bridge) — does the rule fire once or per-pair?
- Whether the walker should leverage Survivor 2's `port_nodes` view if it's landed; or use raw `(port_type, event_class, valid_filter)` tuples until then.
- Whether the filter_group `"depth_2"` should split into subtypes (`"ETB-DIES"`, `"ETB-LTB"`, etc.) for finer IDF bucketing, or stay unified.
- Cross-interaction with `cost_feeds_trigger`: if commander's port feeds candidate's port A via cost_feeds_trigger, does that count toward the depth-2 match, or is it separate?

## Out of Scope for This Brainstorm

- Multi-card pathway scoring (bridge cards).
- Path depth ≥ 3.
- Hypergraph / n-card enabler patterns.
- Integration with deck-so-far (evolving query) — separate feature.

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 5.
- Prerequisite: `2026-04-21-unified-eval-harness-requirements.md` FR3 / FR4 (audit gate, histogram verdict).
- Synergy: `2026-04-21-typed-port-graph-requirements.md` — if canonical nodes land first, FR3's walker operates on a much cleaner graph structure (e.g., `ETB → ZONECHANGE` matches become first-class edges, not inferred from `EVENT_MATCH_MAP` strings).
- Memory alignment: `memory/feedback_general_not_specific.md` — this is a general mechanism that catches cascade commanders without per-commander rules.
- Memory alignment: `memory/project_reanimator_hisyn_gap.md` — reanimator hi-syn is commander-specific curation; depth-2 self-bridging is a partial structural answer to that gap.
