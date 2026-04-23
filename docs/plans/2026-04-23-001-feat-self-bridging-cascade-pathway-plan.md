---
title: "feat: Depth-2 self-bridging pathway scoring (self_bridging_cascade)"
type: feat
status: active
date: 2026-04-23
origin: docs/brainstorms/2026-04-21-pathway-scoring-requirements.md
---

# feat: Depth-2 self-bridging pathway scoring (`self_bridging_cascade`)

## Overview

Add a new complement-rule family, `self_bridging_cascade`, that fires when a
single candidate card's internal port-graph forms a length-≤2 loop reinforcing
two distinct commander-port matches. This catches cascade-shaped synergy that
the current depth-1 matcher is structurally blind to — e.g. Bloodghast's
ETB + landfall-self-return loop as viewed through Korvold's sacrifice trigger.

The rule ships behind `_ENABLE_PATHWAY_RULES = False`. Landing is gated on a
`bench.py audit` verdict against the 100-commander golden set (see origin FR4
+ CLAUDE.md `memory/feedback_audit_every_change.md`). Flag is intentionally
family-forward: it gates the `self_bridging_cascade` rule at MVP and a future
depth-2 pathway rule would share the same gate (no new flag per rule). When
the audit lands the flip (Unit 6), the flag is plumbed into
`ScoringConfigInputs` so config-hash invalidation is automatic; until then,
Units 1-5 add no fields to `ScoringConfigInputs` (preserving `bench.py audit
--expect-identity` at flag = False).

## Problem Frame

Engine-grade commanders (Korvold, Muldrotha, Meren, Teysa) derive most of their
value from *cascades* — a single card whose ports chain in a way the commander's
triggers repeatedly exploit. Current rules match at depth-1: one commander port
↔ one candidate port. The cascade structure is invisible; Bloodghast registers
as two independent single-port hits for Korvold, not as a loop.

The brainstorm's dark-gap observation (`scales_with.Valid[*]` at 34% coverage)
is largely relational/referential ports — exactly what a depth-2 walker catches
without adding per-commander curation (aligns with `memory/feedback_general_not_specific.md`).

See origin: [docs/brainstorms/2026-04-21-pathway-scoring-requirements.md](../brainstorms/2026-04-21-pathway-scoring-requirements.md).

## Requirements Trace

- R1 (origin FR1). Gate condition: `|M| ≥ 2` where M = candidate's ports matching *any* commander port, AND an internal length-≤2 edge exists between two ports in M via shared `valid_filter`, `EVENT_MATCH_MAP`, or `COST_FEEDS_TRIGGER`.
- R2 (origin FR2). Single additive `PortComplement` per firing. `rule_id="self_bridging_cascade"`, `cmdr_event` = deterministic label of the two matched commander-port events, `cand_event="self_bridging"`, `filter_group="depth_2"` (unified for this rule at MVP; see Key Technical Decisions). IDF is computed over candidates for which the rule fires (free via existing dedup-key mechanism).
- R3 (origin FR3). `_walk_self_paths(candidate_ports, commander_ports)` utility. Length cap = 2 by construction. Terminates early once the first path is found per candidate.
- R4 (origin FR4). Feature flag `_ENABLE_PATHWAY_RULES = False`. Flip, audit, land if verdict ≥ MARGINAL-to-POSITIVE; revert otherwise.
- R5 (origin FR5). `recommend.py --explain` emits a legible `path:` line per `self_bridging_cascade` firing (Event1(Card) ⇌ Event2(Card) + inline reason).
- R6 (origin FR6). No cross-card path-walking. Walker operates on one candidate at a time.

## Scope Boundaries

- No cross-card paths (commander → A → B → commander). Future survivor.
- No path depth ≥ 3. Future survivor.
- No bridge-card detection (candidate that enables paths between *other* candidates).
- No replacement of `cost_feeds_trigger` — this extends it additively.
- No deck-so-far / evolving-query UX.
- `self_bridging_cascade` rule_id must NOT appear in `data/rules_seed.json` or be added to `DECLARATIVE_RULE_IDS` — the depth-2 walk is inherently algorithmic and cannot be expressed as a single declarative SQL WHERE fragment.

### Deferred to Separate Tasks

- Cross-card bridge scoring: a future brainstorm after depth-2 demonstrates it is a net-positive signal.
- `filter_group` subtype split (e.g. `"depth_2__ETB-DIES"`): deferred to a follow-up only if MVP audit shows a clear sub-pattern that would benefit from separate IDF buckets. Start unified.

## Context & Research

### Relevant Code and Patterns

- **`PortComplement` dataclass:** `src/mtg_synergy_graph/complement_rules/core.py:678-688`. Frozen; dedup key is `(rule_id, cmdr_event, cand_event, filter_group)` — the IDF pool emerges automatically from this 4-tuple at `src/mtg_synergy_graph/universal_scorer.py:792-851`.
- **Rule registration site:** `_card_attr_complements()` closure in `src/mtg_synergy_graph/complement_rules/core.py:1222-1309`. A new rule adds one `out.extend(_find_self_bridging_cascade(...))` line. Multi-port-per-candidate precedent: see `_find_flicker_synergy` in `src/mtg_synergy_graph/complement_rules/utility/flicker.py:37-120` and `_find_artifact_recursion` in `src/mtg_synergy_graph/complement_rules/graveyard.py:306-385`.
- **Two-stage bulk-load pattern** (the closest precedent to a per-candidate port-graph walk): `_find_panharmonicon_complements` in `src/mtg_synergy_graph/complement_rules/panharmonicon.py:80-171` — first-stage SQL finds candidates, second-stage query loads all their effect ports into a `dict[card_name, effect_row]`, then Python-side filtering. Template for Unit 2.
- **`EVENT_MATCH_MAP` / `COST_FEEDS_TRIGGER`:** `src/mtg_synergy_graph/graph_engine.py:116-129` (PEP 562 lazy-loaded from `data/event_match_seed.json` via `src/mtg_synergy_graph/port_graph/event_maps.py`). `EVENT_MATCH_MAP[trigger_event][effect_event]` returns an `EventCheck: Callable[[PortRow, PortRow], bool]` predicate. `COST_FEEDS_TRIGGER[cost_event]` returns a frozenset of triggerable event classes.
- **Shared `valid_filter` equivalence:** `_changezone_type_set()` in `src/mtg_synergy_graph/complement_rules/core.py:435-484` is **ChangeZone-specific** — it returns `None` for any `valid_filter` whose head is not in `_CHANGEZONE_ALL_CARD_TYPES` / `_CHANGEZONE_PERMANENT_TYPES` / `Card` / `Permanent` or a ChangeZone runtime head. Reuse it for the ChangeZone↔ChangeZone path (covers the Bloodghast shape) but Unit 1 must add a broader type-set intersection primitive for non-ChangeZone pairs (DamageDone triggers, ability-activation costs, etc.). Do not treat this as a one-line reuse.
- **Scoring-config hash surface:** `ScoringConfigInputs` NamedTuple at `src/mtg_synergy_graph/universal_scorer.py:219-237` and `get_scoring_config_inputs()` at `:239-250`. Drives `bench.tensor.compute_config_hash()`; any new bool field here means flipping it invalidates stale tensor rows.
- **Explainability:** `SynergyEngine._render_explanation` at `src/mtg_synergy_graph/engine.py:442-465`. Currently receives `(card: str, scores: dict[str, float])` — the legacy bucket dict from `UniversalScore.to_legacy_buckets()` at `src/mtg_synergy_graph/universal_scorer.py:329-366`. The call site at `engine.py:380-410` builds `ranked` from `us.to_legacy_buckets()` and **discards the `UniversalScore` object** before invoking the renderer. To emit a `path:` line we must (a) restructure that sort/window loop to retain the `UniversalScore` alongside the bucket dict (e.g. carry a parallel `dict[str, UniversalScore]` or tuples), and (b) extend `_render_explanation` to accept it. This is a call-site rewiring, not a purely additive signature extension.
- **Rule-to-bucket mapping:** `_RULE_TO_BUCKET` at `src/mtg_synergy_graph/universal_scorer.py:66-154`. One-line add for the new rule.
- **Registry gate:** `src/mtg_synergy_graph/complement_rules/registry.py:701-763` (`_CARD_ATTR_GATES`) and `CARD_LEVEL_RULES` frozenset at `:774-801`. The new rule is card-level (depends on port *combinations* on the candidate) — include in `CARD_LEVEL_RULES` alongside precedents `flicker_synergy`, `flicker_payoff`, `untap_combo`, `cheat_cmc`.
- **Bench audit flow:** `src/mtg_synergy_graph/bench/audit.py:26-69` + `src/mtg_synergy_graph/bench/cli.py:94-220`. No CLI override for scoring constants — flag must be edited in source.
- **Test fixtures:** `tests/conftest.py:64-107` — session-scoped card fixtures loaded from `tests/fixtures/*.txt`. Existing: Korvold, Panharmonicon, Cathar's Crusade, Rhystic Study, Scute Swarm. **Missing:** Muldrotha, Meren, Teysa — must be added for target-commander coverage.

### Institutional Learnings

- `docs/solutions/` returned no relevant prior entries — first attempt at a port-graph traversal rule in the repo. Compound-worthy after landing.
- `CLAUDE.md` authoritative context:
  - `card_hints` deck_hint_match / deck_needs_fulfilled / buffed_by_match were reverted for being too broad and "diluting the mechanical-port signal." **Relevance:** a depth-2 walker that fires on too many candidates risks the same failure mode. Tight gate (≥2 matched ports *and* internal edge) and single-contribution-per-candidate are the structural guards.
  - `memory/feedback_audit_every_change.md` — every scoring-path change gated by NDCG audit; no exceptions. Unit 6 is the gate, not a nicety.
  - `memory/feedback_general_not_specific.md` — general mechanical rules beat per-archetype curation. The depth-2 walker *is* that general mechanism.
  - `memory/project_reanimator_hisyn_gap.md` — reanimator hi-syn is commander-specific curation; depth-2 is a structural partial answer.
- Prior flag-gated-rule precedent: `feat/idf-reforms-bm25f-conditional` branch (commits `5887dcb`, `97e4df2`, `a3254b4`) used `_IDF_METHOD = "legacy"` constant + `ScoringConfigInputs` field. Infrastructure landed but the *method flip* was reverted as HARMFUL. Same pattern here: infrastructure + flag are separable from the audit decision.

## Key Technical Decisions

- **Python-helper path, not declarative.** `RuleInterpreter` emits one `PortComplement` per matching candidate via a single SQL WHERE fragment. A depth-2 per-card graph walk needs algorithmic control flow. Stay on the `_find_*` pattern.
- **Single additive `PortComplement` per firing.** Candidate still receives its base depth-1 per-rule contributions; pathway match is a third contribution, not a multiplier. Avoids double-counting under IDF.
- **Unified `filter_group = "depth_2"`.** Don't shard IDF buckets by sub-pattern at MVP — small pools overfit. Split only if audit data justifies.
- **Deterministic `cmdr_event` label.** Lexicographic sort of the two matched commander-port event_classes, joined with `"+"` (e.g. `"ETB+LTB"`). Keeps the dedup key stable across runs and the 4-tuple IDF grouping well-formed.
- **Port_nodes canonical labels are cosmetic only.** Walker uses raw `(port_type, event_class, valid_filter)` tuples from `card_ports`. The `port_nodes.node_kind` column is used only for the human-readable `path:` rendering in `--explain`, not for path semantics. Keeps the walker free of a view dependency and matches the canonical-name-for-humans convention already used by `port_graph/vocabulary.py`.
- **Walker as a pure function on in-memory port tuples.** Enables test-first enumeration of all internal-edge combinations without DB scaffolding.
- **`ScoringConfigInputs` field added in Unit 6, not earlier.** `ScoringConfigInputs` is a NamedTuple; adding any field unconditionally shifts `compute_config_hash()` output (see `src/mtg_synergy_graph/bench/tensor.py:38`), invalidating the pinned tensor even for flag-off runs. Bundling the field add + flag flip + `--repin` into Unit 6 keeps Units 1-5 at `bench.py audit --expect-identity` parity. This matches the `_IDF_METHOD` precedent's *shape* (config-hash invalidation on flip) while deferring the hash-shift cost to the unit that actually re-pins the fixture.
- **Runtime impact accepted, bounded.** Walker is `O(|M|²)` per candidate with `|M| ≤ 5` typical. Bulk port re-query is one extra SQL call per `find_all_complements` invocation. Target: ≤ 10% overhead (origin Success Criterion 5).

## Open Questions

### Resolved During Planning

- **"Port-graph edge within a single card"** → Three channels, per FR3:
  1. Shared `valid_filter` type intersection — reuse `_changezone_type_set()` at `complement_rules/core.py:435-484` for the ChangeZone↔ChangeZone sub-case (which covers the Bloodghast shape). For non-ChangeZone ports, Unit 1 implements a small fallback type-set primitive that extracts the head type token from the `valid_filter` string. Edge exists iff the resulting type sets overlap.
  2. `EVENT_MATCH_MAP[port_a.event_class][port_b.event_class]` returns a truthy `EventCheck` and `check(port_a, port_b)` evaluates True.
  3. `COST_FEEDS_TRIGGER.get(cost_port.event_class, frozenset())` contains the other port's `event_class`.
  Channels (2) and (3) reuse existing canonical substrates; channel (1) introduces the one new primitive.

- **"Multi-port (≥3) cascades — fire once or per pair?"** → Fire ONCE per candidate. The walker terminates after the first `(p1, p2)` pair in `M × M` for which an internal edge is found. Per-pair firing inflates the IDF pool and double-counts under the single-contribution invariant.

- **"Use `port_nodes` view or raw tuples?"** → Walker operates on raw `(port_type, event_class, valid_filter)` tuples (from `card_ports`). The `port_nodes.node_kind` canonical label is used only for human-readable rendering in the `--explain path:` line.

- **"`filter_group` unified or subtyped?"** → Unified (`"depth_2"`) at MVP. Subtype split deferred.

- **"Cost_feeds_trigger cross-interaction?"** → A commander-port → candidate-port match through `COST_FEEDS_TRIGGER` counts equally toward `|M| ≥ 2`. Internal-edge detection on the candidate is independent of how each port matched the commander — `M` is defined by the match side, the edge check by the candidate-internal side.

### Deferred to Implementation

- **Exact SQL for first-stage candidate enumeration.** SQLite does not support `COUNT(DISTINCT (a, b))` over tuples; Unit 2 must use either `COUNT(DISTINCT port_type || '|' || event_class) >= 2` or a self-join variant. The `(port_type, event_class)` pair IN-list is also not SQLite-native — decide between a `(port_type || '|' || event_class) IN (...)` concatenation shape or a two-predicate disjunction during implementation. For Korvold (~20 ports) the bound-param count is well under SQLite's 999 limit, but the pool shape still needs profiling.
- **`CandidateCache.ports_by_card` helper — likely required, not speculative.** Korvold's Stage-1 candidate pool (≥2 ports matching any of ~20 commander shapes) is potentially thousands of candidates; Stage-2 bulk-loading every port of every candidate could touch 10-50k rows per page. The panharmonicon precedent scales only because its Stage-1 is gated by 1-4 event classes. Unit 2 should profile this and add `CandidateCache.ports_by_card` if Stage-2 dominates. Treat it as a probable addition, not a speculative one.
- **`_render_explanation` plumbing shape.** Two options: (a) restructure `engine.py:380-410` to retain `UniversalScore` alongside the bucket dict, and extend the renderer signature to accept it; (b) encode the path into `PortComplement.filter_group` as a structured string (e.g. `"depth_2|ETB⇌LTB"`) and decode in the renderer. Option (a) is cleaner and gives the path richer context but requires rewriting the sort/window loop. Pick during Unit 4; prefer (a) unless the call-site rewiring blocks progress.
- **Whether `"self_bridging_cascade"` maps to an existing bucket in `_RULE_TO_BUCKET` or a new bucket.** `"port_match"` is the most natural fit; a dedicated `"pathway"` bucket may help the `--explain` reader. Decide during Unit 4.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Data flow for a single commander page:

```
find_all_complements(cmdr, conn)
  ├── existing depth-1 helpers (unchanged) ──────────┐
  └── if _ENABLE_PATHWAY_RULES:                      │
        _find_self_bridging_cascade(conn, cmdr_ports, cmdr_set)
          │                                          │
          ├── Stage 1 (SQL): candidates with ≥2 ports matching any cmdr port
          │     SELECT card_name FROM card_ports
          │     WHERE (port_type, event_class) IN (...cmdr_port_shapes...)
          │     GROUP BY card_name HAVING COUNT(DISTINCT ...) >= 2
          │                                          │
          ├── Stage 2 (SQL bulk): all ports of those candidates
          │     ports_by_card: dict[str, list[PortRow]]
          │                                          │
          └── Stage 3 (Python): per candidate        │
                for cand in ports_by_card:           │
                  M = ports in cand_ports matching any cmdr_port
                  if |M| < 2: continue               │
                  path = _walk_self_paths(M)         │
                  if path is None: continue          │
                  emit PortComplement(               │
                    rule_id="self_bridging_cascade", │
                    cmdr_event=canonical_label(path.p1, path.p2),
                    cand_event="self_bridging",      │
                    filter_group="depth_2",          │
                    …)                               │
                                                     ▼
UniversalScore.score ──── IDF bucketing over 4-tuple dedup key (no change)
      │
      └── _render_explanation(card, UniversalScore) [extended]
            └── for c in score.complements where c.rule_id == "self_bridging_cascade":
                  emit "self_bridging_cascade: {kind_a}({card}) ⇌ {kind_b}({card})"

bench.py audit
  └── ScoringConfigInputs includes enable_pathway_rules → config_hash shifts
      when flag flips → persisted tensor invalidated → fresh audit
```

Internal-edge detection (`_walk_self_paths`) signature and logic shape:

```
def _walk_self_paths(M: Sequence[PortRow]) -> tuple[PortRow, PortRow, str] | None:
    # Returns (p1, p2, channel) on first path; None if no internal edge.
    # channel ∈ {"valid_filter", "event_match", "cost_feeds"} — used for
    # the --explain human-readable reason.
    for (p1, p2) in itertools.combinations(M, 2):
        if _shared_valid_filter(p1, p2): return (p1, p2, "valid_filter")
        if _event_match(p1, p2):         return (p1, p2, "event_match")
        if _cost_feeds(p1, p2):          return (p1, p2, "cost_feeds")
    return None
```

## Implementation Units

- [x] **Unit 1: `_walk_self_paths` pure-function walker**

**Goal:** A pure, DB-free utility that detects a length-≤2 internal edge between two ports of the same candidate via the three canonical channels.

**Requirements:** R3.

**Dependencies:** None.

**Files:**
- Create: `src/mtg_synergy_graph/complement_rules/pathway.py`
- Test: `tests/test_pathway_walker.py`

**Approach:**
- Implement `_walk_self_paths(M: Sequence[PortRow]) -> tuple[PortRow, PortRow, str] | None` returning the first found path and channel identifier.
- Three channel checks as small private helpers `_shared_valid_filter`, `_event_match`, `_cost_feeds`. Each returns `bool`.
- `_shared_valid_filter` delegates to existing `_changezone_type_set()` in `complement_rules/core.py` — import, do not duplicate.
- `_event_match` calls `match_event(p1, p2)` / reverse (bidirectional check) using `graph_engine.EVENT_MATCH_MAP`.
- `_cost_feeds` checks `COST_FEEDS_TRIGGER.get(cost.event_class, frozenset())` contains the other port's trigger event class.
- `itertools.combinations(M, 2)` iterates pairs deterministically (input is a list, caller responsible for stable order).

**Execution note:** Implement test-first. Enumerate the internal-edge channel combinations (3 positive channels × symmetry × 3-port-cascade-fires-once) as unit tests before implementation. Pure function, synthetic `PortRow` dicts, no DB — ideal TDD surface.

**Patterns to follow:**
- Synthetic `PortRow` dict construction: see `tests/conftest.py` `_make_db`, `_port`, `_add_port` helpers (adapt to in-memory dicts without requiring a DB).
- Channel helper style: `src/mtg_synergy_graph/complement_rules/core.py` private underscore helpers.

**Test scenarios:**
- Happy path: Two ETB + ZONECHANGE ports that share a `valid_filter` → returns `(p1, p2, "valid_filter")`.
- Happy path: Sacrifice-cost + DIES trigger on the same card → returns channel `"cost_feeds"`.
- Happy path: ChangeZone Battlefield→Graveyard + ETB trigger where `EVENT_MATCH_MAP` links them → returns channel `"event_match"`.
- Happy path: Bloodghast-shape — landfall ETB + return-from-grave-on-landfall — returns a path.
- Edge case: `|M| < 2` → returns `None`.
- Edge case: `|M| = 2` with no channel match → returns `None`.
- Edge case: `|M| = 3` with multiple viable paths → returns exactly ONE path, first found by deterministic iteration order.
- Edge case: Two identical ports on one card (rare but possible) → pair filter excludes reflexive pair.
- Error path: `PortRow` with missing `event_class` → reuses existing handling; no new exception.
- Integration: Walker called on real Bloodghast port tuples imported from the test fixture returns the expected `(ETB, return-from-grave)` channel identification.

**Verification:**
- All walker test scenarios pass.
- No DB access in the walker or its helpers (enforced by import-only of `graph_engine.EVENT_MATCH_MAP` + `COST_FEEDS_TRIGGER` dicts).
- `ruff` and `pyright` clean.

- [x] **Unit 2: `_find_self_bridging_cascade` helper (SQL + Python pipeline)**

**Goal:** Wrap the walker in the two-stage bulk-load pipeline that enumerates candidates, bulk-loads their ports, and emits one `PortComplement` per firing.

**Requirements:** R1, R2, R6.

**Dependencies:** Unit 1.

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/pathway.py` (extend with `_find_self_bridging_cascade`).
- Test: `tests/test_self_bridging_cascade.py`

**Approach:**
- Signature: `def _find_self_bridging_cascade(conn, cmdr_ports, cmdr_set) -> list[PortComplement]`. Matches existing `_find_*` convention.
- Early-return gate: if fewer than 2 commander ports, return `[]`.
- Stage 1 SQL: enumerate candidate card_names with ≥ 2 distinct ports matching any commander port shape. SQLite does not support `COUNT(DISTINCT (a, b))` — use `COUNT(DISTINCT port_type || '|' || event_class) >= 2` with `(port_type || '|' || event_class) IN (?, ?, ...)` from the commander's port-shape set. S608-clean via frozenset-guarded placeholder-string build (follow the pattern in `complement_rules/core.py`).
- Stage 2 SQL: bulk-fetch all ports of those candidates, grouped into `ports_by_card: dict[str, list[PortRow]]`. Single query per call, not per-candidate. If the Stage-1 pool is large enough that this dominates cost (see profiling step below), elevate this to `CandidateCache.ports_by_card` before committing.
- Stage 3 Python: for each candidate, compute `M = [p for p in cand_ports if any match against any commander port]`. If `|M| < 2` skip. Call `_walk_self_paths(M)`. On success, emit ONE `PortComplement`:
  - `rule_id = "self_bridging_cascade"`
  - `direction = "synergy"`
  - `candidate = card_name`
  - `cmdr_event` = lexicographically-sorted `"{event_a}+{event_b}"` from the two matched commander-port events (not the candidate's; this keeps IDF bucketing reflective of commander context)
  - `cand_event = "self_bridging"`
  - `filter_group = "depth_2"`
  - Standard `branch_kind = "root"` default
- Dedup per-rule: `seen: set[str]` of candidate card_names.
- Exclude candidates in `cmdr_set` and stax-excluded set (replicate the pattern at `core.py:1358-1359`).
- **Profiling step (required to verify Success Criterion 5 / Risk row 3):** after a working implementation, time `find_all_complements()` for a large-port commander (Korvold or similar) with and without the new helper. Record: Stage-1 SQL time, Stage-2 SQL time, row count returned by Stage-2, and percent-of-total-call time. Include these numbers in the unit's commit message. If total overhead > 10%, add `CandidateCache.ports_by_card` as part of this unit.

**Patterns to follow:**
- Two-stage template: `src/mtg_synergy_graph/complement_rules/panharmonicon.py:80-171`.
- Multi-port SQL self-join shape: `src/mtg_synergy_graph/complement_rules/utility/flicker.py:91-101` (`WHERE card_name IN (SELECT …)`).
- S608-safe placeholder building: `src/mtg_synergy_graph/complement_rules/core.py` existing query builders.
- `seen` set dedup: every existing `_find_*` helper.

**Test scenarios:**
- Happy path (Korvold + Bloodghast fixture — real parsed ports): helper returns exactly one `PortComplement` for Bloodghast with `rule_id="self_bridging_cascade"`, the deterministic `cmdr_event` label, `filter_group="depth_2"`.
- Happy path (synthetic in-memory ports — GY self-loop shape): a card with sacrifice-cost + DIES trigger wired to re-cast-from-grave fires the rule. No fixture needed.
- Happy path (synthetic in-memory ports — ETB + DIES aristocrats shape): one firing via the token-double chain.
- Edge case: commander with only 1 port → returns `[]`.
- Edge case: candidate with 3+ matching ports + multiple internal edges → fires exactly ONCE.
- Edge case: candidate in `cmdr_set` (same card as commander) → excluded.
- Edge case: stax-excluded candidate → excluded.
- Error path: empty `cmdr_ports` → `[]`.
- Integration: `find_all_complements()` with the flag ON (via `patch.object`) surfaces self-bridging contributions in `UniversalScore.complements`; dedup key uniqueness verified by counting distinct 4-tuples.
- Integration: IDF weight for the rule is computed from the pool of candidates that fire it (FR2) — verify via a synthetic 5-candidate fixture.

**Verification:**
- All helper test scenarios pass.
- `uv run pytest tests/test_self_bridging_cascade.py` runs under 2s.
- No `self_bridging_cascade` entry sneaks into `data/rules_seed.json` (regression guard in the registry unit).

- [x] **Unit 3: Flag gate and registry wiring**

**Goal:** Wire the new helper into the complement pipeline behind `_ENABLE_PATHWAY_RULES = False`, with registry attribution in place. **Do not** modify `ScoringConfigInputs` in this unit (see Unit 6 + Key Technical Decisions).

**Requirements:** R4 (flag gate portion; config-hash invalidation deferred to Unit 6).

**Dependencies:** Unit 2.

**Files:**
- Modify: `src/mtg_synergy_graph/complement_rules/pathway.py` (declare `_ENABLE_PATHWAY_RULES: bool = False` module constant).
- Modify: `src/mtg_synergy_graph/complement_rules/core.py` — add guarded `out.extend(_find_self_bridging_cascade(...))` line in `_card_attr_complements()` (~line 1222-1309).
- Modify: `src/mtg_synergy_graph/complement_rules/registry.py` — add `RuleGate("self_bridging_cascade", _self_bridging_cascade_gate)` to `_CARD_ATTR_GATES`; add `"self_bridging_cascade"` to `CARD_LEVEL_RULES` frozenset.
- Modify: `src/mtg_synergy_graph/universal_scorer.py` — add `"self_bridging_cascade"` entry in `_RULE_TO_BUCKET` (~line 66-154). **No change to `ScoringConfigInputs` in this unit.**
- Test: `tests/test_pathway_flag_gate.py`
- Test (update): `tests/test_registry_gates.py` if it exists, or co-locate in the new flag-gate test.

**Approach:**
- The flag lives in `pathway.py` for locality. Re-exported through nothing else; tests `patch.object(pathway, "_ENABLE_PATHWAY_RULES", True)` to toggle.
- The single dispatch line in `_card_attr_complements()`:
  - `if pathway._ENABLE_PATHWAY_RULES: out.extend(pathway._find_self_bridging_cascade(conn, cmdr_ports, cmdr_set))`
  - Place after the existing similar `_find_*` extensions, alphabetically ordered or by theme; match surrounding style.
- Registry gate: `_self_bridging_cascade_gate(port)` returns `True` for any port that could participate in a depth-2 match — the gate is intentionally permissive (the walker does the actual gating). Precedent: `flicker_synergy` gate in `registry.py`.
- `CARD_LEVEL_RULES` inclusion means the auditor **excludes** `self_bridging_cascade` firings from per-port attribution accounting (these rules are card-shape rules, not port-shape rules). Per the frozenset's docstring at `registry.py:774-801`, `CARD_LEVEL_RULES` is *subtracted* from the unregistered-rule fallback so multi-port conjunction rules don't bloat per-signature activation rates. Confirm this is the intended behavior: pathway firings contribute to scoring but not to port-shape coverage metrics.
- Explicit regression guard test: assert `"self_bridging_cascade"` is NOT in `DECLARATIVE_RULE_IDS`.

**Patterns to follow:**
- `CARD_LEVEL_RULES` precedents (two-port-required rules): `flicker_synergy`, `flicker_payoff`, `untap_combo`, `cheat_cmc` at `src/mtg_synergy_graph/complement_rules/registry.py:774-801`.
- Flag constant + `patch.object` test pattern: commit `5887dcb` on `feat/idf-reforms-bm25f-conditional` — `_IDF_METHOD` module-global addition. Mirror the constant-and-patch pattern; do NOT mirror the `ScoringConfigInputs` field addition here.

**Test scenarios:**
- Happy path: flag off (default) → `find_all_complements()` emits ZERO `self_bridging_cascade` complements on Korvold + Bloodghast.
- Happy path: flag on (via `patch.object`) → `find_all_complements()` emits ONE on same fixture.
- Edge case: `"self_bridging_cascade"` is NOT in `DECLARATIVE_RULE_IDS` (regression guard against accidental seed-JSON add).
- Edge case: registry `_CARD_ATTR_GATES` includes the rule; `CARD_LEVEL_RULES` includes the rule.
- Edge case: config hash is UNCHANGED by this unit (adding entries to `_RULE_TO_BUCKET` alone does not shift `compute_config_hash` output; `ScoringConfigInputs` unchanged).
- Integration: `UniversalScore.complements` contains the rule when flag is patched on; `UniversalScore.to_legacy_buckets()` reports the new rule's bucket non-zero.

**Verification:**
- `bench.py audit --expect-identity` passes after this unit (identical to current main — flag-off default + no `ScoringConfigInputs` change = unchanged config hash = no tensor re-pin needed).
- With flag patched on in tests, the 4-tuple dedup key works (no double-counted contributions).
- Registry unit tests pass.

- [x] **Unit 4: `--explain` path rendering**

**Goal:** `recommend.py --commander X --top N --explain` emits a legible `path:` line per `self_bridging_cascade` firing, naming the two canonical node kinds and the internal-edge channel reason.

**Requirements:** R5.

**Dependencies:** Unit 3.

**Files:**
- Modify: `src/mtg_synergy_graph/engine.py` — extend `_render_explanation` (line 442-465) to accept the `UniversalScore` or its complements list so it can filter by `rule_id`. **Also restructure the sort/window loop at `engine.py:380-410`** so that the `UniversalScore` instance is retained alongside the bucket dict produced by `to_legacy_buckets()` (current code discards `UniversalScore` before the renderer runs). Path rendering logic is inlined directly in `_render_explanation` — no separate exported helper.
- Test: `tests/test_explain_rendering.py`

**Approach:**
- Prefer Option (a) from the Deferred questions: thread `UniversalScore` into `_render_explanation`. Cleaner than encoding the path into `filter_group`.
- Call-site rewiring: the existing aggregation loop in `engine.py` materializes `ranked = sorted(...)` from buckets only. Change it to carry `(card, buckets, universal_score)` triples (or parallel dict). This is the single riskiest edit in the unit — walk through it carefully and verify existing tests still pass before adding the new `path:` line.
- Inline format (per FR5) directly in `_render_explanation`:
  ```
  self_bridging_cascade: {kind_a}({card}) ⇌ {kind_b}({card})
    (channel: {event_match|cost_feeds|valid_filter})
  ```
- `kind_a` / `kind_b` come from the `port_nodes.node_kind` canonical vocabulary (look up via `port_graph.projection` or a small local `(port_type, event_class) -> node_kind` mapping built at import). Fall back to raw `event_class` if no canonical node kind exists.
- Keep the reason line short and channel-typed — do not invent commander-specific flavor text.
- Renderer output preserves the existing `"        - "` indentation from `scripts/recommend.py:67-69`.

**Patterns to follow:**
- `engine.py:442-465` existing `_render_explanation` structure.
- `port_graph/vocabulary.py` for canonical `NODE_KINDS` labels.

**Test scenarios:**
- Happy path: Korvold + Bloodghast → explanation includes exactly one `self_bridging_cascade:` line with legible `ETB(Bloodghast) ⇌ ZONECHANGE(Bloodghast)` or equivalent.
- Happy path: no `self_bridging_cascade` firings → explanation omits the line (doesn't emit an empty placeholder).
- Edge case: multiple candidates firing the rule → one `path:` line per candidate's own explanation block.
- Edge case: an unknown `(port_type, event_class)` pair → fallback to raw `event_class` string, no crash.
- Error path: missing `candidate` field on the complement → graceful skip with a log warning; does not abort explanation rendering for other candidates.
- Integration: run `uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 10 --explain` end-to-end (flag on, fixture DB) and assert the output contains a `path:` line.

**Verification:**
- `--explain` output is byte-identical to pre-change when flag is off (no regression for users on the default path).
- `--explain` output with flag on passes the integration test pattern match.
- `_render_explanation` call-site update doesn't break other callers (`UniversalScore` threading is additive, not subtractive).

- [x] **Unit 5: Bloodghast fixture for depth-2 integration test**

**Goal:** Add the minimal fixture needed for Unit 2's real-fixture happy-path integration test. Korvold already exists; Bloodghast is the only new card needed to exercise the full SQL → walker → `PortComplement` pipeline against real parsed ports.

**Requirements:** R1 (one real-fixture integration path for Unit 2).

**Dependencies:** None (can proceed in parallel with Unit 1 or Unit 2).

**Files:**
- Create: `tests/fixtures/bloodghast.txt` (Forge card text)
- Modify: `tests/conftest.py` — add session-scoped `bloodghast` fixture mirroring `korvold` at line 90-92.

**Approach:**
- Source raw Forge card text from the vendored Forge cardsfolder bundle (`data/forge/` per `memory/reference_forge_java_engine.md`).
- Mirror the `korvold` fixture loader exactly — session-scoped, loads from `tests/fixtures/*.txt`.
- Muldrotha, Meren, Teysa fixtures are intentionally **not added** — Unit 6's audit runs against the full live DB (`data/synergy.db`), where those commanders already exist as real rows, so pytest fixtures are unnecessary for spot-checking them. Add them only if Unit 6 reveals a specific failure shape that benefits from an isolated pytest reproduction.

**Patterns to follow:**
- `tests/fixtures/korvold_fae_cursed_king.txt` for raw Forge text format.
- `tests/conftest.py:90-92` for loader registration.

**Test scenarios:**
- Test expectation: the fixture loader itself needs a lightweight presence test (card name "Bloodghast" correctly parsed, ≥2 ports). Add to the existing `_load`-style fixture test.
- Sanity: loader is session-scoped (no re-parse cost per test).

**Verification:**
- `uv run pytest tests/` still runs under ~2s for the non-integration suite.
- Unit 2's Korvold + Bloodghast integration test works without any further fixture additions.

- [x] **Unit 6: `ScoringConfigInputs` plumbing + audit gate + landing decision**

**Goal:** Add the flag to `ScoringConfigInputs` (so config-hash invalidation is automatic), flip `_ENABLE_PATHWAY_RULES = True` in the working tree, run `bench.py audit`, and decide whether to commit the flip or revert.

**Requirements:** R4 (audit gate + config-hash invalidation), Success Criteria 1-5 from origin.

**Dependencies:** Units 1-5 complete; all tests passing with flag-off identity.

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` — add `enable_pathway_rules: bool` field to `ScoringConfigInputs` NamedTuple (~line 219-237) and populate in `get_scoring_config_inputs()` (~line 239-250).
- Modify: `src/mtg_synergy_graph/complement_rules/pathway.py` — working-tree flip to `_ENABLE_PATHWAY_RULES = True` during audit; commit as `True` only if verdict supports landing.
- Modify: `tests/fixtures/golden_set_run.json` — re-pin via `uv run scripts/bench.py audit --repin --yes --edhrec-db data/tags.db` (**required even if the final committed flag is False**, because the `ScoringConfigInputs` field addition shifts `compute_config_hash`).
- Modify: `docs/RULE_HISTORY.md` — append dated entry with verdict + aggregate Δ NDCG + hidden_gem_hit_rate Δ + named target-commander deltas.
- Modify: `CLAUDE.md` — add `self_bridging_cascade` to the complement-rule catalogue (landed True) or note infrastructure-only landing (landed False).

**Approach:**
- Step 0 (pre-audit, in working tree): add the `enable_pathway_rules` field to `ScoringConfigInputs` and re-pin the fixture baseline with flag = False. This isolates the config-hash shift from the behavioral comparison. Verify `--expect-identity` passes post-re-pin with flag still False.
- Step 1 (working tree only — do NOT commit this flip yet): set `_ENABLE_PATHWAY_RULES = True` in `pathway.py`.
- Step 2: `uv run scripts/bench.py audit` → capture verdict, histogram, aggregate Δ.
- Step 3: `uv run scripts/bench.py audit --inspect self_bridging_cascade --limit 20` → top contribution rows, sanity-check they look like cascade-shape cards (Bloodghast, Gravecrawler, Nether Traitor, aristocrats-shape tokens).
- Step 4: spot-check named target commanders — Korvold, Muldrotha, Meren, Teysa — each should show ≥ 1 hi-syn gain in their top-30 (Success Criterion 2).
- Step 5: spot-check non-target commanders — Uril the Miststalker, Rafiq of the Many (voltron, `|M|` typically < 2) — should show no measurable NDCG movement (Success Criterion 3).
- Step 6: check `hidden_gem_hit_rate` delta via `bench.py audit --inspect-gems` and `--trend hidden_gems` — tracking-only per plan 003, but informative.
- Step 7: decision:

  | Verdict | Action |
  |---|---|
  | **Land flip = True:** aggregate Δ ≥ 0 OR positive on the four named target commanders with no commander worse than −0.05, *and* hidden_gem_hit_rate not worse than −0.02 | Keep flip, re-pin fixture baseline at flag = True, commit, update `RULE_HISTORY.md` and `CLAUDE.md`. |
  | **Revert flip (leave infrastructure):** HARMFUL or severe CONTENTIOUS verdict not resolvable with one tuning pass | Revert the flag to `False` in the committed change, keep the `ScoringConfigInputs` + walker + registry infrastructure, document the verdict in `RULE_HISTORY.md`. Matches BM25F landed-as-infrastructure precedent (commit `97e4df2`). |

  If the first audit is MARGINAL or CONTENTIOUS, one tuning pass is permitted (e.g. add a `_RULE_QUALITY_MULTIPLIER["self_bridging_cascade"]` entry in `universal_scorer.py:407-789` to dampen or amplify, or disable the `valid_filter` channel in the walker). Re-audit once; if still not landable, revert the flip.
- Step 8: compound-worthy after landing (either direction) — add a `docs/solutions/` entry describing the flag-gated multi-port rule pattern.

**Patterns to follow:**
- `ScoringConfigInputs` field-addition pattern: commit `5887dcb` on `feat/idf-reforms-bm25f-conditional` — `_IDF_METHOD` field addition at `universal_scorer.py:219-237`.
- BM25F landed-as-infrastructure precedent: commit `97e4df2`.
- `docs/RULE_HISTORY.md` existing dated-entry format.

**Test scenarios:**
- Test expectation: none -- this unit's deliverable is an audit decision plus the `ScoringConfigInputs` plumbing. The plumbing's correctness is verified by `--expect-identity` pre-flip and the audit run itself; no new pytest scenarios.

**Verification:**
- Post-re-pin, `bench.py audit --expect-identity` passes at flag = False.
- `bench.py audit` with flag = True produces a verdict file (`.audit/last.md`) with the new rule visible.
- Final committed state: either flag = True with re-pinned fixture at True baseline, OR flag = False with re-pinned fixture at False baseline. In both cases `--expect-identity` passes on the final committed tree.
- `docs/RULE_HISTORY.md` has a dated entry with the verdict and named target-commander deltas.

## System-Wide Impact

- **Interaction graph:** The new rule extends `find_all_complements` and `UniversalScore.complements` — both consumed by `SynergyEngine.page()`, `recommend.py`, and `bench.py` audit paths. `CandidateCache` hashing unaffected (same candidate set). `ScoringConfigInputs` hash surface gains a field.
- **Error propagation:** Walker is pure; no new IO failure modes. Stage 1/2 SQL failures propagate as `sqlite3.Error` — caught by existing error handling in `find_all_complements`.
- **State lifecycle risks:** None. Rule is stateless; walker is a pure function; no cache coupling.
- **API surface parity:**
  - `SynergyEngine.page()` continues to return the same `RecommendationPage` dataclass; only the `explanation` field gains an extra line when the rule fires.
  - `recommend.py --explain` output changes when flag is on; byte-identical when flag is off.
  - `bench.py audit` config-hash changes when the field is added to `ScoringConfigInputs` — expected, requires fixture re-pin in Unit 6 iff the flag flips.
- **Integration coverage:** Unit 2's integration tests exercise the full SQL → Python → `PortComplement` → `UniversalScore` → `to_legacy_buckets` chain. Unit 4 exercises `UniversalScore` → `_render_explanation` → `recommend.py` output.
- **Unchanged invariants:**
  - Default scoring behavior (flag = False) is byte-identical to pre-change — enforced by `bench.py audit --expect-identity` in Unit 3.
  - Existing complement rules and their IDF buckets are unchanged. The new rule contributes to a new 4-tuple key; it cannot re-weight existing keys.
  - No changes to the typed port-graph substrate (`port_nodes` view, declarative interpreter, `data/rules_seed.json`).
  - No changes to the importer or `data/synergy.db` schema.

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Walker fires too broadly — dilutes mechanical signal (same failure mode as reverted `deck_hint_match` / `deck_needs_fulfilled`) | Tight gate: `|M| ≥ 2` + internal edge required. Single additive contribution per candidate. Unit 6 audit is the final arbiter; keep flag False if broad. |
| Double-counting with existing depth-1 rules under IDF | Dedup key `(rule_id, cmdr_event, cand_event, filter_group)` — `self_bridging_cascade` has its own 4-tuple, cannot overlap. Verified in Unit 2 integration tests. |
| Runtime regression > 10% | Walker is `O(|M|²)` with `|M| ≤ 5`. Stage-2 bulk re-query is the real cost centre: for Korvold's ~20-port commander profile, the Stage-1 pool may be thousands of candidates and Stage-2 loads all their ports (10-50k rows). Unit 2 requires profiling the Stage-1 + Stage-2 split and adding `CandidateCache.ports_by_card` in that same unit if overhead exceeds the 10% target. Treat the cache as likely-required, not deferred. |
| `--explain` plumbing leaks `UniversalScore` into the public `Recommendation` dataclass surface | Keep the threading internal to `engine.py` — extend `_render_explanation` only, do not change the external `Recommendation` dataclass shape. |
| Flag flip invalidates the pinned audit fixture surprise | `ScoringConfigInputs` wiring makes this explicit via `compute_config_hash()`. Unit 6 includes re-pin step iff landing. |
| Target commanders (Muldrotha / Meren / Teysa) have no fixture → cannot validate Success Criterion 2 pre-audit | Unit 5 adds them as a parallel-track dependency. |
| Over-eager `port_nodes` reliance blocks walker on the typed port-graph substrate | Walker uses raw `(port_type, event_class, valid_filter)` tuples; `port_nodes` is cosmetic-only for `--explain`. No hard dependency on plan 002's interpreter. |
| Audit verdict is CONTENTIOUS and tuning can't recover it | Land infrastructure at flag = False (BM25F precedent). Document verdict; do not force-ship. |

## Documentation / Operational Notes

- **`CLAUDE.md`** — update the Complement Rules section to mention `self_bridging_cascade` iff the flag lands True. If the flag stays False, note it in `docs/RULE_HISTORY.md` only (infrastructure-landed, not active).
- **`docs/COMPLEMENT_RULES.md`** — add the new rule to the catalogue under a new "Pathway" section after the flag lands (either True or False — document what exists).
- **`docs/RULE_HISTORY.md`** — dated entry under 2026-04-23 with verdict, aggregate Δ, hidden_gem_hit_rate Δ, named target-commander deltas.
- **`docs/solutions/`** — after landing (either direction), add a `docs/solutions/` entry: first documented port-graph pathway rule + the "flag-gated multi-port rule" authoring pattern. Compound-worthy per `ce-compound` skill.
- **Pre-commit hook** — the advisory bench-audit hook already fires on `complement_rules/` edits per `CLAUDE.md`. No new hook needed.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-21-pathway-scoring-requirements.md](../brainstorms/2026-04-21-pathway-scoring-requirements.md)
- **Related plans:**
  - [docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md](2026-04-22-001-feat-unified-eval-harness-plan.md) (bench.py, the audit gate)
  - [docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md](2026-04-22-002-feat-typed-port-graph-substrate-plan.md) (port_nodes canonical labels)
  - [docs/plans/2026-04-22-003-feat-useful-disagreement-hidden-gem-metric-plan.md](2026-04-22-003-feat-useful-disagreement-hidden-gem-metric-plan.md) (second-axis metric)
- **Related code:**
  - `src/mtg_synergy_graph/complement_rules/core.py` (PortComplement, find_all_complements, _changezone_type_set)
  - `src/mtg_synergy_graph/complement_rules/panharmonicon.py` (two-stage template)
  - `src/mtg_synergy_graph/complement_rules/utility/flicker.py` (two-port self-join template)
  - `src/mtg_synergy_graph/graph_engine.py` (EVENT_MATCH_MAP, COST_FEEDS_TRIGGER)
  - `src/mtg_synergy_graph/universal_scorer.py` (ScoringConfigInputs, IDF bucketing, _RULE_TO_BUCKET)
  - `src/mtg_synergy_graph/engine.py` (_render_explanation, Recommendation)
  - `src/mtg_synergy_graph/bench/audit.py` + `bench/cli.py` (audit flow)
- **Prior flag-gate precedent (reference only, not landed on main):** commits `5887dcb` / `97e4df2` / `a3254b4` on `feat/idf-reforms-bm25f-conditional` — `ScoringConfigInputs` field addition pattern.
- **Memory alignment:**
  - `memory/feedback_general_not_specific.md`
  - `memory/feedback_audit_every_change.md`
  - `memory/feedback_audit_metric_too_coarse.md`
  - `memory/feedback_hidden_gem_metric.md`
  - `memory/project_reanimator_hisyn_gap.md`
