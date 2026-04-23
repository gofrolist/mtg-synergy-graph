---
title: Flag-gated multi-port complement-rule authoring pattern
date: 2026-04-23
category: best-practices
module: complement_rules
problem_type: best_practice
component: development_workflow
severity: medium
applies_when:
  - "Adding a complement rule that requires two distinct commander-port matches on the same candidate card (card-level, not per-port)"
  - "The rule logic is inherently algorithmic and cannot be expressed as a row in data/rules_seed.json"
  - "The rule must ship identity-clean (bench.py audit --expect-identity) and be gated behind a controlled audit flip"
related_components:
  - tooling
  - documentation
tags:
  - complement-rules
  - multi-port
  - flag-gated
  - audit-iterate
  - identity-clean
  - rule-authoring
  - card-level-matching
  - scoring-config
---

# Flag-gated multi-port complement-rule authoring pattern

## Context

The existing complement-rule pipeline is built around depth-1 junction rules:
`ComplementRule` tuples (`complement_rules/core.py:603-670`) declare a single
`cmdr_port_type` + `event_pairs` + `cand_port_type`, and the engine emits a
`PortComplement` for every (commander port, candidate port) pair that
satisfies the predicate. The `RuleInterpreter` (`port_graph/interpreter.py`)
extends this model declaratively: rows in `data/rules_seed.json` compile to a
SQL `WHERE` fragment plus a Python gate callable, one match per port pair.
Both paths are single-commander-port × single-candidate-port junction
machines.

Neither path can express a **cascade-shape rule**: a rule that fires when a
candidate possesses *two or more* ports that each independently match a
commander port AND those candidate ports also form an internal causal edge
(trigger→effect or cost→trigger on the same card). Cascade archetypes like
Korvold + Bloodghast, Muldrotha + Gravecrawler, and Teysa + aristocrats-chain
token-makers all have this shape — the candidate is valuable because its
internal port graph resonates with the commander on multiple levels
simultaneously, not just one.

Writing such a rule as a formal `ComplementRule` depth-1 tuple emits separate
complements for each matched port, treating the conjunction as coincidental
rather than structural. Writing it declaratively requires a lateral join or
correlated subquery that violates the single-fragment compilation contract.
Multi-port conjunction rules have to live on the Python-helper path.

Flag-gating matters here because `memory/feedback_audit_every_change.md`
mandates that every scoring-path change be gated by a `bench.py audit` NDCG
comparison against a pinned fixture (auto memory [claude]). A cascade rule
changes scoring outputs by definition. Without a flag, any work-in-progress
implementation would corrupt the baseline comparison the next time the
auditor runs. The flag keeps `--expect-identity` clean through every
infrastructure unit and flips only in the dedicated audit unit, at which
point the fixture is re-pinned.

This separates "the wiring is correct" from "the rule produces good signal"
so each can be verified independently. The separation was not possible under
the prior norm (session history): earlier rule additions like `untap_combo`
and `dies_drain` iterated pool + multiplier in a single working branch, with
audit regression as the feedback signal. The flag-gated pattern lets
infrastructure land across multiple PRs or units before any behavioral
change ships.

## Guidance

End-to-end checklist for authoring a new flag-gated multi-port rule. The
canonical reference implementation is `self_bridging_cascade` in
`src/mtg_synergy_graph/complement_rules/pathway.py`; cite specific
file:line references there when in doubt.

### 1. Location — Python-helper path, new module

Create `src/mtg_synergy_graph/complement_rules/<family>.py`. The module
owns: the feature flag, the pure walker, the SQL-backed helper, the
explain formatter, and any family-local utilities (type-set extractors,
memos). Do not touch `data/rules_seed.json`, and do not add the rule_id
to `DECLARATIVE_RULE_IDS`. A rule lives in exactly one of the two sides
(plan 002 FR6 escape-hatch).

### 2. Pure walker first (test-first)

Isolate the algorithmic core as a pure function on in-memory port
tuples before writing any SQL. For `self_bridging_cascade` this is:

```python
def _walk_self_paths(
    ports: Sequence[PortRow],
) -> tuple[PortRow, PortRow, str] | None:
    """Detect a length-<=2 internal edge between two of `ports`.
    Returns (p1, p2, channel) or None.
    """
```

The function takes the already-filtered `M` set (candidate ports that
match any commander port) and walks every pair looking for an internal
causal edge. No database dependency. Test exhaustively: empty input,
single-port input (must return None), a pair connected via each channel,
a pair with no internal edge (must return None), channel priority
ordering, orientation bidirectionality. Run suite, confirm green,
*then* write the SQL.

### 3. SQL pipeline shape

Follow the three-stage pattern established by
`_find_panharmonicon_complements`:

- **Stage 1 (narrow prefilter)**: Select candidate names whose ports
  contain at least 2 distinct commander-relevant `(port_type,
  event_class)` shapes. The concat-distinct trick works around SQLite's
  lack of tuple-DISTINCT:

  ```sql
  SELECT card_name FROM card_ports
  WHERE event_class IN (<relevant_events>)
    AND card_name NOT IN (<excluded>)
  GROUP BY card_name
  HAVING COUNT(DISTINCT port_type || '|' || event_class) >= 2
  ```

  Build `<relevant_events>` by expanding commander port events through
  `EVENT_MATCH_MAP` (forward + reverse) and `COST_FEEDS_TRIGGER`
  (cost→trigger + trigger→cost). Err broad — Stage-3 is authoritative.

- **Stage 2 (bulk port fetch)**: Fetch all ports for every Stage-1
  candidate in a single query. When `CandidateCache` is available, do a
  dict-slice over `candidate_cache.ports_by_card` (pre-loaded
  commander-independently, cached once per session). Otherwise fall
  back to a bulk SQL fetch. **Pre-authorize the `CandidateCache` path
  before the first real profiling run** — without it, broad commanders
  produce thousands-of-percent overhead because Stage-2 queries
  thousands of `card_ports` rows per page.

- **Stage 3 (Python walker)**: For each candidate, compute `M` using
  `_cand_port_matches_any_cmdr`. If `|M| < 2`, skip. Call the pure
  walker. On path found, emit one `PortComplement`.

Exclude both commander names (`cmdr_set`) and stax-blocked candidates
(`stax_excluded`) in the Stage-1 WHERE clause. Thread `stax_excluded`
through from the caller site (step 6).

### 4. Flag constant + `ScoringConfigInputs` plumbing

Declare the flag at module top-level, defaulting to `False` so every
infrastructure unit (steps 2-8) lands identity-clean before any
behavioral change ships:

```python
# src/mtg_synergy_graph/complement_rules/pathway.py
_ENABLE_PATHWAY_RULES: bool = False  # flipped to True in the audit unit
```

The canonical reference module has this constant at `True` today
because the audit unit has already landed. When authoring a new
rule family, start at `False` and flip only after the audit verdict
in step 9.

Add a corresponding bool field to `ScoringConfigInputs`
(`universal_scorer.py`):

```python
class ScoringConfigInputs(NamedTuple):
    rule_quality_multiplier: dict[str, float]
    flat_weight_overrides: dict[str, float]
    synergy_pairs: dict[frozenset[str], float]
    enable_pathway_rules: bool   # add this
```

Populate in `get_scoring_config_inputs()`:

```python
from .complement_rules import pathway  # local import: avoids cycle
return ScoringConfigInputs(
    ...,
    enable_pathway_rules=pathway._ENABLE_PATHWAY_RULES,
)
```

Extend `compute_config_hash()` in `bench/tensor.py`:

```python
h.update(b"|pathway:")
h.update(repr(cfg.enable_pathway_rules).encode("utf-8"))
```

A flag flip now shifts the config hash automatically. The pinned tensor
carries the old hash; any `bench.py audit` run refuses to compare stale
rows and demands `--repin --yes`. This is mechanical enforcement, not
trust-based.

**Timing note**: keep `ScoringConfigInputs` unchanged until the audit
unit. Adding the field mid-infrastructure forces a re-pin immediately,
which defeats the purpose of incremental identity-clean landings.

### 5. Registry wiring

In `src/mtg_synergy_graph/complement_rules/registry.py`:

```python
CARD_LEVEL_RULES: frozenset[str] = frozenset({
    ...,
    "self_bridging_cascade",   # requires two distinct commander ports
})
```

`CARD_LEVEL_RULES` was introduced in 2026-04-18/19 specifically to mark
rules whose firing requires a *conjunction* of two or more matches —
same structural reason for `flicker_synergy`, `untap_combo`, and
`cheat_cmc` (session history). The auditor subtracts these from
per-port attribution so the coverage metric stays honest.

- Do **not** add to `_CARD_ATTR_GATES` — multi-port rules are excluded
  from per-port attribution by design.
- Do **not** add to `data/rules_seed.json` or `DECLARATIVE_RULE_IDS` —
  the rule stays on the Python-helper path only.

In `universal_scorer.py`, add to `_RULE_TO_BUCKET`:

```python
"self_bridging_cascade": "port_match",
```

### 6. Dispatch site in `_card_attr_complements()`

Add one guarded block in `src/mtg_synergy_graph/complement_rules/core.py`
near the other `_find_*` helper calls:

```python
from . import pathway  # local import: avoids graph_engine at core load

if pathway._ENABLE_PATHWAY_RULES:
    out.extend(
        pathway._find_self_bridging_cascade(
            conn, cmdr_ports, cmdr_set, stax_excluded, candidate_cache
        )
    )
```

Use a local import to avoid circular module loading. **Thread
`stax_excluded` through** — the `stax_excluded` set is built once per
commander in `find_all_complements`; without threading it, stax pieces
fire the rule for commanders they actively hurt (e.g., `CantSacrifice`
statics firing for Korvold). This was one of three P1 findings in the
post-landing review pass (`docs/RULE_HISTORY.md` § 2026-04-23).

### 7. Load-bearing iteration order for `cmdr_event` IDF labels

The `cmdr_event` field on emitted `PortComplement`s encodes which
commander events matched the bridging pair. For
`self_bridging_cascade`, it is `"+".join(sorted([ev1, ev2]))` where
`ev1` and `ev2` come from `_cand_port_matches_any_cmdr`, which iterates
`cmdr_ports` in **source order** and returns the first match.

**This iteration order is load-bearing.** Any optimization that
reorders or prunes the `cmdr_ports` scan — even when semantically
equivalent for scoring purposes — changes `cmdr_event` labels, shifts
the IDF dedup 4-tuple `(rule_id, cmdr_event, cand_event,
filter_group)`, and breaks `bench.py audit --expect-identity`. A
commander event-class index was prototyped in the 2026-04-23 perf pass
and rejected for exactly this reason. Document the constraint at the
function:

```python
def _cand_port_matches_any_cmdr(
    cand_port: PortRow,
    cmdr_ports: Sequence[PortRow],
) -> PortRow | None:
    """Return the first commander port that matches cand_port, or None.

    Ordering is load-bearing: the cmdr_event label encodes the
    commander event of the FIRST match in cmdr_ports order, so all
    call sites must preserve the same iteration order to keep IDF
    buckets stable.
    """
```

The approved performance mitigation that does *not* change iteration
order is a full-shape memo:

```python
match_memo: dict[tuple[str, str, str, str, str, str], PortRow | None] = {}
for cp in cand_ports:
    key = (
        cp.get("port_type") or "",
        cp.get("event_class") or "",
        cp.get("valid_filter") or "",
        cp.get("zone_origin") or "",
        cp.get("zone_destination") or "",
        cp.get("counter_type") or "",
    )
    # Key on the full set of fields the channel predicates read.
    # A narrower key conflates ports with different zone_destinations
    # under zone_compatible, producing silent scoring drift that
    # --expect-identity catches but unit tests do not.
    if key in match_memo:
        cached_match = match_memo[key]
    else:
        cached_match = _cand_port_matches_any_cmdr(cp, cmdr_ports)
        match_memo[key] = cached_match
```

Many ports on different candidate cards share identical full shapes,
so the memo cuts wall time without touching traversal order. **Narrower
keys are not safe** — `EventCheck` predicates (e.g., `zone_compatible`,
`counter_compatible`) read zone/counter fields; omitting them from the
memo key silently conflates different matches.

### 8. Explain plumbing

Add a `path_info: str = ""` field to `PortComplement` (defaulted,
excluded from the 4-tuple dedup key so it is pure narrator metadata):

```python
@dataclass(frozen=True)
class PortComplement:
    ...
    path_info: str = ""   # narrator metadata, not in dedup key
```

Populate it in the Stage-3 loop:

```python
path_info = _format_path_info(p1, p2, channel)
# e.g. "trigger.Sacrificed <-> cost.sacrifice (channel: cost_feeds)"
```

Extend `_render_explanation` in `engine.py` to surface path lines with
per-card deduplication:

```python
if universal_score is not None:
    seen_paths: set[str] = set()
    for c in universal_score.complements:
        if c.rule_id != "self_bridging_cascade" or not c.path_info:
            continue
        if c.path_info in seen_paths:
            continue
        seen_paths.add(c.path_info)
        lines.append(f"self_bridging_cascade: {c.path_info}")
```

The `seen_paths` set is scoped per-call (`_render_explanation` is
called once per card with a per-card `UniversalScore`), so duplicate
suppression is within a single card's complements list — not across
candidates.

**Both `page()` and `score_one()` must thread `UniversalScore` to
`_render_explanation`.** Asymmetric plumbing — threading it through one
entry point but not the other — causes identical cards to produce
different explain output depending on which API was called. This was
the first P1 finding in the 2026-04-23 post-landing review. The
reroute in `page()` (retaining `UniversalScore` alongside buckets in
the sort/window loop) is a call-site rewire, not a purely additive
signature extension.

### 9. Audit-iterate landing pattern

Start with the broadest permissible gate, run `bench.py audit`, review
qualitatively, then narrow by channel or qualifier until the
qualitative review is clean. **Each narrowing should preserve the
primary success metric** (`hidden_gem_hit_rate` in this repo, per
`memory/feedback_hidden_gem_metric.md` (auto memory [claude])) while
cutting collateral.

The 2026-04-23 `self_bridging_cascade` run produced three variants in
sequence:

| variant | agg Δ | hi_syn_loss | hidden_gem | qualitative |
|---|---|---|---|---|
| v1: 3 channels (event_match + cost_feeds + valid_filter) | +463.7 | 0 | 0.8423 | voltron / proliferate equipment drift |
| v2: drop valid_filter | +377.4 | 0 | 0.8423 | wildcard `*` still over-fires on equipment |
| v3 LANDED: drop valid_filter + reject wildcard `*` | +209.3 | 0 | 0.8423 | clean |

**The aggregate delta decreased while `hidden_gem_hit_rate` stayed
flat at 0.8423** across all three variants. Same uplift over baseline
(0.7287 → 0.8423 = +0.1136), smaller aggregate = more precise firing.
The per-commander qualitative review reveals collateral the aggregate
metric cannot distinguish from signal (auto memory [claude]
`feedback_audit_metric_too_coarse.md`). Run both.

Prior audit-iterate experience (session history): `untap_combo` needed
three pool narrowings to reach net-positive (1× broad → 3× broad → 3×
with tap-cost + mana-effect conjunction). `dies_drain` reverted from
2.5× to 1.5× after a Wilhelt −0.10 NDCG regression. The pattern is
stable: **narrow until regressions vanish, don't chase aggregate gains
with broader pools.**

### 10. Fixture re-pin discipline

After the flag is flipped to `True` and all post-review findings are
fixed:

```
uv run scripts/bench.py audit --repin --yes --edhrec-db data/tags.db
uv run scripts/bench.py audit --expect-identity
```

`--expect-identity` must PASS before the commit lands. Every
`ScoringConfigInputs` field addition or flag flip shifts the config
hash — re-pinning is mechanically required, not optional.

### 11. Performance budget

Profile before claiming the rule is ready. For a broad commander
(Korvold: ~1336 firings, ~20 cmdr_ports) the 2026-04-23 measurements
were:

| phase | Korvold | Gitrog | Yawgmoth |
|---|---|---|---|
| pre-optimization | 345 ms (+2084%) | 91 ms (+203%) | 76 ms (+262%) |
| + `CandidateCache.ports_by_card` + shape memo | 128 ms | 56 ms | 36 ms |
| + `functools.cache` on `_type_token_set` + empty-filter short-circuit | 114 ms | 55 ms | 36 ms |

**Use absolute cost, not percentage, as the interactive-acceptability
metric.** +600% overhead on a 15 ms baseline is 115 ms total — fine
for `recommend.py`. The plan's 10% budget was aspirational for broad
commanders; the actual ceiling is set by the walker + `PortComplement`
construction cost per firing (~85 µs), which does not compress further
without a semantic change (audit re-run required).

Approved optimizations:

- `@functools.cache` on pure string helpers (hashable inputs, ~2k unique
  filter strings → 98% hit rate in production). **Caveat**: `functools.cache`
  holds references for the process lifetime. Apply only when the input
  space is bounded and small (here: `valid_filter` strings from the
  Forge cardsfolder). Do not copy this to helpers consuming user
  text, oracle strings, or any unbounded input — memory grows
  without eviction.
- Early short-circuit in `_valid_filter_edge` when either port lacks a
  `valid_filter` string.
- Full-shape memo on `_cand_port_matches_any_cmdr` (see step 7 for the
  key composition).
- `CandidateCache.ports_by_card` bulk pre-load (follow the
  `_bulk_load_*` pattern from `penalties.py`; session history shows
  this pattern was established in 2026-04-17 with `token_etb_damage_cards`).

Rejected optimizations:

- Commander event-class index — reordered `cmdr_ports` iteration,
  breaking `cmdr_event` label stability (see step 7).
- Narrower memo keys — conflated ports with different zone/counter
  fields that `EventCheck` predicates read.

### 12. Testing discipline

Four test categories, all required:

- **Walker tests**: synthetic `PortRow` dicts, no database. Cover all
  channel cases, early exits, empty/single-port inputs, ordering
  invariants, bidirectional match.
- **Helper tests**: `rules_db` in-memory fixture + manual
  `card_ports` inserts. Verify Stage-1 SQL, Stage-3 walker integration,
  `PortComplement` field values, `stax_excluded` suppression,
  Stage-1 concat-distinct boundary behavior.
- **Flag-gate tests**: `patch.object(<module>, "_ENABLE_<FLAG>",
  True/False)` over a production-schema DB (via `open_db`) to verify
  the dispatch site in `_card_attr_complements()` gates correctly.
  Assert registry invariants: rule in `CARD_LEVEL_RULES`, NOT in
  `DECLARATIVE_RULE_IDS`, bucket entry present.
- **Explain tests**: cover both `page()` and `score_one()` with
  `include_explanation[s]=True`. Verify `self_bridging_cascade: <path_info>`
  appears in the explanation for a known-matching candidate, and that
  patching the flag off removes it.

## Why This Matters

**Identity-preservation as a development discipline.** The
`--expect-identity` check asserts bitwise-identical scores between
runs. Violating it during development — even transiently — makes the
next `bench.py audit` delta unreadable: the diff includes infrastructure
noise, not just rule signal. The flag pattern quarantines
identity-breaking changes to a single deliberate flip in the audit
unit, keeping incremental landings safe.

**Mechanical enforcement over documentation trust.** Flag +
`ScoringConfigInputs` coupling means a developer cannot flip
`_ENABLE_*` and forget to re-pin. The hash change is automatic and the
audit tool refuses to read stale rows. Prior convention relied on
remembering to re-pin (session history shows this was informal and
error-prone); the new pattern makes it impossible to skip.

**`CARD_LEVEL_RULES` preserves auditor metric honesty.** The per-port
coverage metric tracks how many distinct commander-port shapes are
covered by at least one firing rule. Listing a multi-port conjunction
rule in `_CARD_ATTR_GATES` with a single-port predicate would falsely
show commanders with one matching port as "covered" even when the
other required port is absent. `CARD_LEVEL_RULES` membership removes
the rule from the per-port denominator, keeping coverage percentages
meaningful.

**Iteration order is a silent performance-optimization trap.** The
`cmdr_event` IDF dedup label depends on source-order first-match.
Without an explicit docstring warning, an optimizer adding a pre-filter
index would silently shift `cmdr_event` labels and break
`--expect-identity` — the audit failure diff would be confusing
because scores are *almost* identical, just redistributed across
different IDF buckets. Document the constraint at the traversal site
so the next optimizer knows before they reorder.

**Audit-iterate rhythm produces rules whose qualitative behavior is
known, not guessed.** Same `hidden_gem_hit_rate` across three variants
with declining aggregate delta demonstrated that narrowing removes
collateral without losing real signal. The final shape reflects actual
EDH cascade identities (Gitrog, Yawgmoth, Korvold, Muldrotha, Teysa)
rather than merely a green histogram metric. This aligns with
`memory/feedback_general_not_specific.md` (general structural rules
beat per-archetype curation) and `memory/feedback_no_individual_rules.md`
(auto memory [claude]).

**Explain plumbing parity matters for debugging workflows.** `page()`
serves batch recommendations; `score_one()` is the entry point people
reach for when investigating *why* a specific card ranked highly. If
`UniversalScore` only threads through `page()`, the richer explanation
silently vanishes from the debugging path — the one where it matters
most. Test both endpoints.

## When to Apply

### Triggers

- Adding a new rule family that requires ≥2 distinct commander-port
  matches on a single candidate (cascade, multi-channel resonance,
  dual-trigger payoff, cross-card bridge if/when plan 003's
  pathway-extension work lands).
- Adding a rule that cannot be expressed as a single declarative
  WHERE fragment in `RuleInterpreter` (lateral correlation across two
  rows of the same candidate's `card_ports`).
- Any scoring-path change that needs to ship across multiple
  infrastructure units before the audit gate fires. The flag is the
  mechanism that keeps incremental landings identity-clean.

### Non-triggers

- Single-port depth-1 matches — add a `ComplementRule` tuple to
  `COMPLEMENT_RULES` in `core.py:603-670`. No new module, no flag.
- Rules expressible as one SQL WHERE fragment — add a row to
  `data/rules_seed.json` and re-import. The rule_id automatically
  enters `DECLARATIVE_RULE_IDS`. No Python file.
- Pure-infra refactors with no scoring impact —
  `memory/feedback_audit_every_change.md` explicitly exempts these
  from the audit gate (auto memory [claude]). No flag, no re-pin.
- Rules that fire on a single commander port but consult card
  attributes (subtypes, oracle text) — inline helpers in
  `_card_attr_complements()` with a gate entry in `_CARD_ATTR_GATES`.
  No family module, no flag.

## Examples

### Canonical reference: `self_bridging_cascade`

- `src/mtg_synergy_graph/complement_rules/pathway.py` — full module:
  `_ENABLE_PATHWAY_RULES` flag, `_walk_self_paths` pure walker,
  `_find_self_bridging_cascade` three-stage pipeline, `_format_path_info`
  explain formatter, `@functools.cache` on `_type_token_set`, the
  intentional M-set/walker asymmetry documented in
  `_port_pair_matches`.
- `docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md`
  — original plan with FR1/FR2 specification, Unit structure, and the
  pre-authorized `CandidateCache` mitigation.
- `docs/RULE_HISTORY.md` § 2026-04-23 — audit variants table,
  post-review fix pass (3 P1 findings), perf pass with measurements.

### What the audit-iterate cycle exposed

Two concrete collateral sources surfaced during the 2026-04-23 narrowing
(see Step 9's variant table for the quantitative view):

- **Split-ability equipment via `valid_filter`.** The Forge port
  extractor splits a single "equipped creature attacks → pump" ability
  into separate `trigger.Attacks` and `effect.Pump` rows on the same
  equipment card. Under v1's `valid_filter` channel, they matched as an
  internal edge when sharing a card-type family (Creature) — which
  displaced Sublime Archangel in Rafiq's top-30 with Strength-Testing
  Hammer. Not a real cascade; a port-extraction artifact.
- **Split-ability via wildcard triggers.** `EVENT_MATCH_MAP` has
  wildcard targets for catch-all triggers (`Attacks → *`,
  `SpellCast → *`, `LandPlayed → *`). Dropping `valid_filter` in v2 did
  not cut this because the same split-ability ports matched via
  `event_match` with the wildcard. v3's fix is tightening
  `_canonical_trigger_effect` to skip wildcard targets — named
  `trigger → effect` edges only. The public `graph_engine.match_event`
  still honors wildcards for commander-vs-candidate matching where
  wildcard semantics are appropriate; the walker's private variant is
  tighter by design.

`hidden_gem_hit_rate` stayed at 0.8423 across all three variants —
identical uplift from the 0.7287 baseline. Narrowing didn't lose
signal; it removed collateral. See `docs/RULE_HISTORY.md` § 2026-04-23
for the full narrative.

### CandidateCache field-addition precedent (session history)

The `CandidateCache.ports_by_card` addition in the 2026-04-23
perf pass followed a pattern established 2026-04-17 with
`token_etb_damage_cards` and extended by `untap_combo_cards`. The
reusable steps:

1. Add a typed field to the `CandidateCache` dataclass in
   `penalties.py`.
2. Add a `_bulk_load_*` function that runs the SQL and returns the
   cache-friendly shape (`frozenset[str]`, `dict[str, list[PortRow]]`,
   etc.).
3. Wire the loader into `build_candidate_cache()`.
4. Update the rule helper to accept `candidate_cache` and read from it,
   falling back to SQL when the cache is `None` (for tests that don't
   build the cache).

The motivation is always profile-driven: the 2026-04-17 addition
targeted an 818 ms commander-independent SQL query that dominated
`engine.page()` time; the 2026-04-23 addition targeted a 17k-row
Stage-2 bulk fetch on Korvold. Commander-independent SQL is the right
fit for `CandidateCache`; commander-dependent SQL belongs in the rule
helper.

## Related

- `docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md`
  — authoritative spec for `self_bridging_cascade`-specific design
  decisions.
- `docs/RULE_HISTORY.md` § 2026-04-23 — post-landing audit narrative,
  three-variant table, review fix pass, perf pass measurements.
- `docs/brainstorms/2026-04-21-pathway-scoring-requirements.md` —
  origin requirements (FR1–FR6, feature-flag + audit gate in FR4).
- `docs/RULE_PLANNING.md` — surrounding scaffolder workflow; "when to
  scaffold Python instead" and `CARD_LEVEL_RULES` definition.
- `docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md` —
  `bench.py audit` infrastructure, `ScoringConfigInputs`,
  `compute_config_hash`, `--expect-identity`, `--repin`.
- `docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md`
  § FR6 — imperative escape-hatch (multi-port conjunction stays
  Python).
- `docs/COMPLEMENT_RULES.md` — complement-rule catalogue.
- `CLAUDE.md` § Scoring Architecture — project quick-reference.
- `memory/feedback_audit_every_change.md` — the audit-every-change
  mandate this pattern implements.
- `memory/feedback_general_not_specific.md` — the general-mechanism
  principle that motivates multi-port structural rules.
- `src/mtg_synergy_graph/complement_rules/pathway.py` — canonical
  implementation.
- `src/mtg_synergy_graph/complement_rules/panharmonicon.py` — two-stage
  bulk-load template.
- `src/mtg_synergy_graph/complement_rules/utility/flicker.py` —
  two-port self-join + `CARD_LEVEL_RULES` precedent.
- `src/mtg_synergy_graph/penalties.py` — `CandidateCache` field-addition
  pattern.
