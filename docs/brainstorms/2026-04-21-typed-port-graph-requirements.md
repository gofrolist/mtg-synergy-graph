---
date: 2026-04-21
topic: typed-port-graph
seed: docs/ideation/2026-04-21-recommendation-model-ideation.md (Survivor 2)
status: draft (brainstorm 2 of 7)
depends_on: 2026-04-21-unified-eval-harness-requirements.md (FR7 for refactor-identity verification)
---

# Requirements: Typed Port-Graph + Rules-as-Data

## Problem Statement

The current rule corpus is ~2,500 LOC of Python across 40+ hand-authored complement rules. Each rule re-derives its own port-matching logic; adding a new mechanic means writing, registering, and auditing a new Python helper. The hand-written `EVENT_MATCH_MAP` and `COST_FEEDS_TRIGGER` dictionaries in `graph_engine.py` encode port-equivalence knowledge only in Python — unavailable to SQL, to analysis tooling, or to future rule-mining. As a result, new mechanics on unreleased cards get zero coverage until a human writes a dedicated rule, directly conflicting with the "universal / handles unreleased cards" goal.

Recent memory notes emphasize this gap: `feedback_no_individual_rules` says "don't add game rules one-by-one; need general mechanical understanding." The structural response is a typed port-graph — a small canonical vocabulary of event nodes on top of which rules become declarative data rather than imperative code.

## Goals

1. Project every extracted port into a small canonical vocabulary of event nodes (`ETB`, `DIES`, `CAST`, `PAYMANA`, `TAP`, `COUNTER_PLACED`, `SACRIFICE`, `DISCARD`, `DRAW`, `DAMAGE`, `ZONECHANGE`, `STATIC_BUFF`, …) with typed attribute columns.
2. Promote `EVENT_MATCH_MAP` and `COST_FEEDS_TRIGGER` from inline Python dicts into a materialized SQL view.
3. Convert ~30 of 40 complement rules from Python helpers into rows of a `rules` table consumed by a single interpreter.
4. Collapse all 10 `*_feeder` helpers in `axis_feeders.py` + `resource_feeders.py` into one `scales_with_axis` engine plus one data row per axis.
5. Keep ~10 genuinely imperative rules (voltron, monarch_synergy, wheel_synergy, flicker_synergy, creatures_as_lands_landfall, panharmonicon-style specializations) as Python escape hatch.
6. Make every future rule a data row, not a PR full of generated Python.
7. Provide graceful, observable handling of novel Forge port shapes so future card releases get zero-cost incremental coverage.

## Non-Goals

- Rewriting imperative rules. The ~10 remaining Python rules stay.
- ML or learned rule weights — rule weights and multipliers remain hand-set (unless Survivor 1's downstream coordinate-descent is built later).
- Mining new rules from data — that's Survivor 4. This brainstorm is purely about the substrate that makes mining tractable.
- Changing scoring semantics. Post-refactor, `score_all_universal` must produce bitwise-identical output on the golden set (verified via Survivor 1 FR7 `bench.py audit --expect-identity`).
- New port extractions from Forge. The existing `ports.py` output is the input to the projection; this brainstorm does not extend `ports.py`.

## Users and Scenarios

| Scenario | Who | Expected experience |
|---|---|---|
| Add a new axis-feeder (e.g., a new counter type) | Dev | One row added to the `rules` table. No new Python file. `scaffold_rule.py` emits a row, not code. |
| Tune a gate predicate for an existing rule | Dev | UPDATE a row. Audit runs on save. |
| Understand why Rule X fires | Dev | SELECT the row; the predicates read like a declarative spec. Compare to scoring output via Survivor 1's tensor. |
| Forge releases a new mechanic with new ApiType | Forge-release integrator | Port extraction runs, unknown port shapes are classified UNKNOWN with raw attributes preserved. `bench.py audit --unknowns` surfaces them in the next audit. Human decides whether to add a canonical node mapping. |
| Merge two collinear rules | Dev | Delete one row, merge predicates into the other. No code deletion. |
| Add a new canonical node kind | Dev | One migration file adds the kind to the canonical vocab; existing rules unaffected; UNKNOWN ports whose raw shape matches the new kind get re-classified on next `bench.py audit --repin`. |

## Functional Requirements

### FR1 — Canonical node vocabulary

Define a closed, versioned set of node kinds (initial cut: `ETB`, `LTB`, `DIES`, `CAST`, `RESOLVE`, `ATTACK`, `BLOCK`, `PAYMANA`, `TAP`, `UNTAP`, `COUNTER_PLACED`, `COUNTER_REMOVED`, `SACRIFICE`, `DISCARD`, `DRAW`, `DAMAGE`, `LIFE_CHANGE`, `ZONECHANGE`, `STATIC_BUFF`, `STATIC_REPLACEMENT`, `SCALES_WITH`). Exact vocabulary is an implementation decision for planning; must be narrow enough to be usable and wide enough to cover the ~30 declaratized rules without semantic squishing.

### FR2 — Port projection

A new `port_nodes` SQLite view (or materialized table) projects every row in `card_ports` to:
- `(card_name, node_kind, subkind, valid_filter_bag, amount_expr, zone_origin, zone_destination, counter_type, tribe_token, color_token, …)`

Columns cover everything currently queried by any of the ~30 declaratizable rules. Unmapped port shapes project to `node_kind='UNKNOWN'` with `subkind` preserving the raw `(port_type, event_class)` so they remain queryable.

### FR3 — `EVENT_MATCH_MAP` / `COST_FEEDS_TRIGGER` as data

Both dicts become tables (`event_match_map`, `cost_feeds_trigger`) whose rows are `(from_node_kind, to_node_kind, match_quality)`. The Python constants become thin loaders. Adding an equivalence = INSERT a row. Used by the interpreter to resolve "does commander's ETB trigger accept candidate's creature-ETB static?" style matches via SQL, not Python.

### FR4 — `rules` table

Columns (exact schema for planning):
- `rule_id TEXT PRIMARY KEY`
- `family TEXT` — primitive / density / archetype / axis-feeder / specialized
- `gate_predicate JSON` — commander-port selection (supports AND / OR / NOT / attribute matches)
- `commander_port_predicate JSON` — what commander-port kind activates the rule
- `candidate_port_predicate JSON` — what candidate-port kind counts as a match
- `filter_group TEXT` — IDF bucketing key
- `cmdr_event TEXT` — canonical event label on commander side
- `cand_event TEXT` — canonical event label on candidate side
- `weight_hint FLOAT` — default weight / flat override
- `active INTEGER` — allows runtime A/B without code changes

### FR5 — Single interpreter

One `RuleInterpreter` class replaces the 30 per-rule Python helpers. It loads the `rules` table at process start, compiles each predicate JSON to a SQL fragment + Python guard where necessary, and exposes the same `find_all_complements()` interface the current registry provides. Keeps the interpreter hot-path identical in performance characteristics to hand-written helpers (no additional per-rule overhead).

### FR6 — Imperative-rule escape hatch

The ~10 rules that genuinely need imperative logic (voltron port-count accumulation, wheel_synergy 2-port conjunction, flicker_synergy multi-directional port checks, creatures_as_lands_landfall cross-port projection) continue to live under `complement_rules/` as Python. The registry composes declarative + imperative rules uniformly at the `find_all_complements()` interface. No declarative-vs-imperative distinction leaks to callers.

### FR7 — Unknown-port observability

`bench.py audit --unknowns` (new subcommand on Survivor 1's CLI) reads `port_nodes` and groups rows with `node_kind='UNKNOWN'` by `subkind`, emitting a Markdown table ranked by `(distinct_cards × sum_edhrec_rank_weight)`. Weekly workflow: run after each Forge cardsfolder refresh; human decides whether to add a canonical mapping for the top entries. Directly closes the "ValidCards fallback took weeks to surface" feedback-loop gap.

### FR8 — Migration strategy

Rule-by-rule, each migration is a single PR-equivalent change:
1. Pick a rule (start with `generated/cascade_tribal.py`; it's already machine-generated).
2. Rewrite as a row in `rules` table + migration fixture.
3. Delete the Python helper.
4. Run `bench.py audit --expect-identity` (Survivor 1 FR7). Bitwise-identical pre/post is the gate.
5. Commit.

No big-bang rewrite. No parallel shadow-scoring interpreter. Every commit during the refactor leaves the system shippable and auditable.

### FR9 — `scaffold_rule.py` emits rows

After FR8 completes for a rule family, `scripts/scaffold_rule.py` is updated to emit a `rules` table row + audit-fixture row instead of Python code. The gap_report → scaffold → audit workflow continues unchanged from the user's perspective, but the artifact is data, not code.

## Success Criteria

1. **Identity preservation.** After full migration, `bench.py audit --expect-identity` on the pinned fixture reports `Δ NDCG = 0.000000` and bitwise-identical per-(commander, candidate) scores. Any non-zero delta is a migration bug and must be investigated, not accepted.
2. **LOC reduction.** `src/mtg_synergy_graph/complement_rules/` shrinks from ~2,500 LOC to ≤ 400 LOC (target: only the ~10 imperative rules + the interpreter). The `rules` table + fixtures expand correspondingly.
3. **Author UX.** Adding a new axis-feeder is a single-row change with no new Python file and no import wiring. Audited end-to-end in ≤ 5 minutes from idea to pinned baseline.
4. **New-mechanic coverage latency.** From Forge cardsfolder refresh to "candidate rules for new node kinds surfaced in `bench.py audit --unknowns`" is automated — no human diagnostic work until a mapping decision is needed.
5. **No scoring regressions on imperative rules.** The ~10 rules that stay Python are not touched by this refactor and produce identical output.
6. **MI-VIF surfaces collinearity.** After migration, `bench.py audit --collinearity` (Survivor 1 FR6) can suggest concrete declarative-rule merges because the rule shape is now machine-comparable.

## Constraints

- The `rules` table + fixtures are committed to the repo and tracked in version control. Every rule change is a diff.
- No introspection of rule behavior at runtime via string/regex manipulation — the interpreter compiles predicates to typed SQL fragments at load time.
- The canonical vocabulary is versioned; a migration is required to add a node kind. Do not hotswap.
- Rule-by-rule migration must not change the order or priority of rule evaluation. Current ordering (registry iteration order) is preserved until the final migration, which may reorder only if audit identity holds.

## Open Questions (For Planning Phase)

- Exact schema for `gate_predicate` JSON (tree vs flat; supported operators; evaluator choice).
- Whether `port_nodes` is a SQLite view (cheap but recomputed) or a materialized table (requires invalidation on DB re-import).
- How the interpreter's compiled-SQL fragments cache across commanders — does each commander get a fresh compile or a shared prepared-statement pool?
- UNKNOWN-subkind classification: which raw fields to retain as projection attributes vs which to discard.
- Whether `card_hints` gets a second chance as a declarative rule row once the interpreter exists (out of scope for this brainstorm; revisit after FR8 completes).

## Out of Scope for This Brainstorm

- Survivors 3–7 each have their own requirements docs. Survivor 3 (BM25F/IDF reforms) may touch the interpreter; that brainstorm will cross-reference this doc.
- Survivor 4's BoosterDraftAI + precon mining becomes much easier on a typed substrate; this brainstorm treats that as a future payoff, not a requirement.
- Rule-mining automation (DRUM / Neural LP / PPMI).
- Deck-so-far evolving query (separate ideation cut).

## Related

- Seed idea: `docs/ideation/2026-04-21-recommendation-model-ideation.md` Survivor 2.
- Prerequisite: `docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md` FR7 (`bench.py audit --expect-identity`).
- Guardrail: `memory/feedback_audit_every_change.md` — every migration step is audit-gated by identity.
- Memory alignment: `memory/feedback_no_individual_rules.md` — "need general mechanical understanding, not individual rules" — this is the structural implementation of that principle.
