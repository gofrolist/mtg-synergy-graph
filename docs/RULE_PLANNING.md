# Rule planning workflow

Schema-driven workflow for adding complement rules. Replaces the
ad-hoc loop ("find a broken commander, write a rule, repeat") with a
coverage-first pipeline that treats Forge ports as a finite,
enumerable vocabulary.

## Why schema-driven

EDHREC tells us *which cards* a commander wants. Forge tells us *what
the commander mechanically does*. The engine should be able to write
a useful pile for any commander whose ports we understand — including
brand-new printings EDHREC has never seen. EDHREC remains the
validation oracle, not the design oracle.

## Tooling

| Script | Output | Purpose |
|---|---|---|
| `scripts/gap_report.py` | `docs/gap_report.md` | **Primary entry point.** Sub-cell coverage + template matching. Outputs a ranked markdown queue of concrete rule proposals. Ranks by `impact * forge_signal` when `data/forge_oracle.db` is present (plan 002 Unit 6); silent fallback to volume-only when the sidecar is missing. The next rule to write is the top entry — no human prioritization required. **Since 2026-05-02 (plan 2026-05-02-001 v1.0)**: each entry shows a `Pre-flight: <PASS\|WARN\|REJECT>` verdict from the Stage A golden-coverage check; entries are grouped into PASS / WARN / REJECT bands with explicit counts, surfacing untestable proposals at the bottom of the report. |
| `src/mtg_synergy_graph/preflight/` | `.audit/walker_outcomes.csv`, `.audit/preflight_overrides.csv` | **Pre-flight Stage A library** (plan 2026-05-02-001 v1.0). Deterministic golden-coverage prefilter that runs BEFORE generator-writing. Two SQL queries per candidate: count distinct fixture commanders matching the gate, count distinct legal-universe commanders. Verdict matrix: ≥1 fixture → PASS; 0 fixture, ≥3 legal-universe → WARN (FIXTURE_BLIND_SPOT); 0 fixture, <3 legal → REJECT (UNTESTABLE). Consumed by `gap_report.py` (Pre-flight column) and `scaffold_rule.py --walk` (skip REJECTs, attempt WARNs by default with `--strict-warn` to invert and `--force` to override). v1.5 (Stages B+C — paper-rule simulator + embedding-shape prior) is deferred to a separate brainstorm/plan cycle per plan v1.5 Plan Trigger. |
| `scripts/scaffold_rule.py` | `src/.../generated/<rule_id>.py`, `tests/test_generated_<rule_id>.py`, integration patches | **Auto-generates rule scaffolding** from the top auditor proposal. Dry-run by default; `--apply` writes files and patches integration points (core.py, registry.py, universal_scorer.py). Output requires validation — scaffolded gates may fire too broadly for the curated EDHREC archetype. **Since 2026-05-02 (plan 2026-05-02-001 v1.0)**: walker mode (`--walk`) consults Stage A pre-flight before each iteration: REJECT verdicts are skipped (saves the wasted scaffold cycle), WARN verdicts are attempted by default and logged to `.audit/walker_outcomes.csv`. `--strict-warn` inverts the WARN default; `--force` + `--force-reason "<text>"` overrides --strict-warn and logs the human decision to `.audit/preflight_overrides.csv`. |
| `scripts/forge_oracle.py build` | `data/forge_oracle.db` | **Build the Forge-signal sidecar** (plan 002). One-time ingest of ~670 Forge precon `.dck` files → PPMI co-occurrence over `port_nodes.subkind` pairs. RAPM-lineup-adjusted, Laplace-smoothed, filtered to `decks_count >= 3`. Rebuild after `git -C data/forge pull` via `scripts/forge_oracle.py build` + update `data/forge_oracle/version.txt`. |
| `scripts/forge_oracle.py propose-rules --top N` | stdout / markdown | **Forge-signal-ranked rule proposals.** For each top-N gap whose template is in `scaffold_rule._GENERATORS`, emits a scaffold preview ready for `scripts/scaffold_rule.py` invocation. Refuses stale / missing sidecar with `rebuild` hint. |
| `scripts/bench.py audit --vs-forge-oracle` | stdout / markdown | **Kendall-τ sidecar** (plan 002 Unit 7). For each commander in the pinned fixture, compares our top-N ranking to Forge's `CardRanker` ranking over the same candidate set. Reports aggregate τ + per-commander breakdown + top-10 divergences. Tracking-only; does NOT gate commits. |
| `scripts/port_universe.py` | `docs/port_universe.json` | Enumerate every distinct (port_type, event_class) cell in `card_ports` with commander reach, top valid_filter qualifiers, and top raw_line clause keys. Re-run after every importer change. |
| `scripts/coverage_matrix.py` | `docs/coverage_matrix.json` | Cell-level coverage (coarser than `gap_report.py`). Useful for sanity checks and historical diffs. |
| `scripts/bench.py audit` | `.audit/last.md` + stdout | **Authoritative eval harness** (Unit 1-8 of `docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md`). Replaces `_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, `weight_grid_search.py`, `broad_set_track.py`. Persists per-rule contributions to the DB so `--rule RULE_ID` / `--inspect RULE_ID` / `--collinearity` answer as SQL queries in <2s. Run before/after every scoring-path change. |
| `scripts/golden_set_track.py` | stdout | **DEPRECATED** — prints a bench.py pointer and still runs the legacy NDCG@30 regression check. Will be removed in a follow-up cleanup. Prefer `bench.py audit --expect-identity` + `--repin --yes`. |
| `scripts/compare_edhrec.py` | stdout | Hi-Syn / Top / OnPage breakdown for any commander or commander list — used for *validation*, not planning. |

## Declarative Rules (plan 003+)

For rule families covered by the `port_graph.interpreter.RuleInterpreter`
(currently `peer_tribal_keyword` — 14 keyword-tribal rules + 2
replacement-stack rules), new rules land as rows in
`data/rules_seed.json`, **not** as new Python files in
`complement_rules/generated/`. The interpreter compiles each row's JSON
predicates to SQL fragments + Python gate callables at load time.

### Adding a new keyword-tribal rule

1. Add a row to `data/rules_seed.json`. Start by copying an existing
   tribal row and changing only the `rule_id`, `event_class` (five
   places in a tribal row), and `cmdr_event`.
2. Add the new `rule_id` to `DECLARATIVE_RULE_IDS` in
   `src/mtg_synergy_graph/complement_rules/registry.py`.
3. Re-seed the DB: `uv run python -c "from mtg_synergy_graph.db import
   open_db; from mtg_synergy_graph.port_graph.rules_schema import
   seed_rules_db; conn = open_db('data/synergy.db');
   seed_rules_db(conn); conn.close()"`.
4. Run `uv run scripts/bench.py audit` against the pinned golden
   fixture. If the rule introduces genuine new matches, the audit
   will show a positive NDCG delta. If it should be identity-
   preserving against the pin (rare for a new rule), use
   `bench.py audit --expect-identity`.
5. Commit: `data/rules_seed.json` + `registry.py` + updated pinned
   fixture (via `bench.py audit --repin --yes` if positive landing).

### Adding a new replacement-stack rule

Same as keyword-tribal but the predicate tree has `replacement_result`
on each `has_port` leaf (in addition to `port_type` + `event_class`).
See `repl_moved_exile_stack` / `repl_damagedone_counters_stack` in the
seed for reference.

### When to scaffold Python instead

Rules outside the declarative-family coverage still use the
`scripts/scaffold_rule.py` Python-file workflow below. The interpreter
gate grammar (`has_port`, `filter_tag`, `zone_eq`, `counter_type`,
`tribe`, `color`, `not_in_commander_set`, plus `and`/`or`/`not`
combinators) covers single-port-shape rules; anything needing multi-
port conjunction / per-card attribute inspection / custom aggregation
stays Python. See the brainstorm
`docs/brainstorms/2026-04-21-typed-port-graph-requirements.md` FR6
for the imperative-escape-hatch principle.

### Parity gate when migrating Python → declarative (issue #16)

The golden fixture (`bench.py audit --expect-identity`) compares
aggregate per-commander scores. A genuine sub-epsilon semantic shift
in one rule can hide as a "float ordering artifact", get absorbed by
`--repin`, and silently change scoring forever. To prevent that:

**Before deleting the Python helper for a migrated rule**, write a
parity test using `tests._parity.assert_rule_parity`. It runs the
Python helper and the declarative interpreter on the same synthetic
fixture DB and asserts the per-rule `PortComplement` sets are
identical. See `tests/test_rule_parity_harness.py` for the canonical
shape — `_faithful_cascade_helper` is the template.

Workflow:

1. Author the declarative row in `data/rules_seed.json` and add
   `rule_id` to `DECLARATIVE_RULE_IDS`.
2. Build a synthetic fixture exercising the rule's gate shape
   (peer ports, anti-self exclusion, filter tags).
3. **Keep the Python helper temporarily.** Write a parity test
   calling `assert_rule_parity(conn, commanders, rule_id=...,
   py_helper=...)`.
4. Run the parity test. Diverging rows are listed by name —
   adjust the declarative predicate until green.
5. Once parity is green, delete the Python helper and re-pin the
   fixture if needed.

## Scaffolder workflow

The auto-generation flow that closes the loop:

```bash
# 1. See what's at the top of the queue
uv run python scripts/gap_report.py

# 2. Generate code + apply integration patches
uv run python scripts/scaffold_rule.py --apply

# 3. Validate (REQUIRED — auto-generated gates may over-apply)
uv run pytest tests/test_generated_<rule_id>.py -v
uv run pytest tests/                                       # full suite
uv run python scripts/golden_set_track.py \
    --baseline tests/fixtures/golden_set_run.json
uv run python scripts/gap_report.py                        # cell should drop

# 4a. If golden NDCG drops -> tighten the generated gate's
#     QUALIFIER_BLOCKERS / OTHER_TYPE_TOKENS, lower the multiplier,
#     or `git checkout` to revert and refine the template.
# 4b. If clean -> commit.
```

The scaffolder generates Python code that compiles and tests pass on
the synthetic fixtures. But auto-generated tier choices may not
match EDHREC's curated archetype for every commander the gate fires
on (a creature-count scaler template fits Hamza but pushes anthems
on Selvala, who's mana-axis instead). Treat scaffolder output as a
starting point: review what commanders the gate fires on, run
golden NDCG, refine before commit.

Adding more generators: extend `_GENERATORS` dict in
`scripts/scaffold_rule.py`. Each generator is a function that
returns a `ScaffoldArtifacts` with helper source, test source, and
integration-point strings.

## The pipeline

### 1. Run the auditor

```bash
uv run python scripts/gap_report.py
```

Generates `docs/gap_report.md` with a ranked queue of sub-cell gaps,
each annotated with:
- Reach (commanders carrying the signature)
- Activation rate (fraction with any rule activation today)
- Impact (`commanders × (1 - activation_rate)`)
- Best-fit template from the catalog (`peer_tribal`,
  `damage_amp_doubler`, `damage_prevention_voltron`,
  `replacement_stack`, `axis_feeder`)
- Gate sketch + tier sketches + estimated pool sizes
- Exemplar commanders with no rule activation

### 2. Pick the top entry — no judgment required

The next rule to write is the entry at position #1. Skip it only if
the template label says `[IMPLEMENTED]` (auditor's safety net so we
don't redo a finished rule).

To refresh the underlying universe / coverage artifacts:

```bash
uv run python scripts/port_universe.py
uv run python scripts/coverage_matrix.py
```

Both produce JSON artifacts under `docs/`.

### 3. Plan the rule before coding

The auditor's proposal is the seed. Expand it into a short note covering:

- **Cell signature**: `(port_type, event_class)` plus any qualifier
  axis being extracted (e.g. `valid_filter ~ '%modified%'`).
- **Mechanical hypothesis**: why these commanders need this rule —
  what player-facing pattern is it modelling? A one-paragraph
  description that doesn't reference any specific commander.
- **Tier structure**: which candidate-side ports we'll match, ranked
  by specificity. Each tier should map cleanly to a SQL query.
- **Self / scope / description rejection list**: which qualifier
  positions or clause keys must be skipped to avoid false positives
  (the `TargetsValid` / `Card.Self+modified` / `TriggerDescription`
  family).
- **Expected reach**: count of commanders the gate should fire for,
  estimated from the catalog. Cross-check after implementation.
- **Validation plan**: which existing commanders should improve
  (Kodama-style), which must not regress (Pearl-Ear-style), and
  what the multiplier baseline should be.

### 4. TDD the rule

Test file: `tests/test_<area>_rules.py`, one new `TestFind…` class
per rule. Required tests:

- Gate rejection: empty / unrelated commander → `[]`.
- Qualifier rejection: Self-anchored / TargetsValid / description
  clause → `[]`.
- Per-tier match: minimal candidate row per tier with the expected
  `cand_event` value.
- Per-tier exclusion: candidate that almost matches but should fall
  through (e.g. self-sac-only producers).
- Dedup: card matching multiple tiers gets exactly one complement,
  highest priority wins.
- Commander self-exclusion.
- `rule_id` value is exactly the new ID.

Each test uses the in-memory schema fixture in
`tests/test_utility_rules.py` (`_make_db()`).

### 5. Wire and weight

- Add helper to `complement_rules/<file>.py`.
- Import + dispatch in `complement_rules/core.py`
  (`_card_attr_complements` or the formal rules tuple).
- Add `_RULE_TO_BUCKET` entry in `universal_scorer.py`.
- Add `_RULE_QUALITY_MULTIPLIER` entry with a comment justifying the
  multiplier in terms of pool size and tier specificity.

### 6. Validate

```bash
uv run pytest tests/                                           # 850+ tests
uv run python scripts/golden_set_track.py \
    --baseline tests/fixtures/golden_set_run.json              # NDCG must not regress
uv run python scripts/coverage_matrix.py                       # cell should now show formal coverage / activation lift
```

For non-golden impact, sample 200–500 commanders carrying the cell's
shape and report aggregate Hi-Syn / OnPage delta. Don't refactor the
gate to chase a single commander's curated picks — EDHREC is a
sanity check, not the target.

### 6.5. Redundancy check (adds-signal-not-just-overlap gate)

Before committing, confirm the new rule adds independent mechanical
signal rather than duplicating an existing rule's contributions:

```bash
uv run scripts/bench.py audit --embedding-dedup          # content-space overlap
uv run scripts/bench.py audit --collinearity             # score-space correlation
```

The two diagnostics answer different questions:

- **`--embedding-dedup`** (cosine similarity of candidate-activation
  sets in content space) — finds rules that *fire on similar cards*.
  Content overlap is expected: multiple rules legitimately fire on
  the same card when it satisfies multiple mechanical axes.
- **`--collinearity`** (Pearson correlation + VIF on per-candidate
  score contributions) — finds rules whose score contributions are
  *statistically redundant*. Two rules scoring the same dimension of
  synergy collapse here even if their activation sets diverge.

A new rule that flags on `--embedding-dedup` but not `--collinearity`
is healthy: it fires on overlapping cards for different mechanical
reasons. A rule that flags on `--collinearity` (|r| > 0.8 AND VIF > 5)
is duplicating existing signal — refine the gate, narrow the
activation set, or drop the rule.

**Reference baseline (2026-04-24, 62-rule catalogue):** zero collinear
pairs even at relaxed thresholds (r > 0.5, VIF > 2.0). If a new
rule introduces a collinear pair, investigate before committing. See
`docs/solutions/best-practices/rule-consolidation-null-result-2026-04-24.md`
for the background.

### 6.6. Know the queue state before running the walker

`scripts/scaffold_rule.py --walk N --apply` is the automation seam for
draining the forge-signal-weighted proposal queue. Before running it,
always check the generator-catalog state:

```bash
uv run python scripts/scaffold_rule.py --show-template-stats
```

If every template shows `BLOCKED` status, the walker will silently
return zero attempts. That's generator-catalog exhaustion, not a
pipeline bug. The fix is not `--force` — it's extending the catalog
(new `_AXIS_FEEDER_TIERS` qualifiers, a new generator for a
currently-`needs_template` proposal, or refining a BLOCKED template
to filter the sub-class of proposals that reverted against it). See
`docs/solutions/best-practices/scaffold-queue-generator-exhaustion-2026-04-24.md`
for the full rubric, Options A/B/C/D, and re-check triggers.

**Also pre-check golden-set coverage.** Even when a template has
a generator, the proposal may be untestable: zero of the 100
golden-set commanders may carry the gate, in which case
`bench.py audit --rule` returns no signal and shipping the rule
violates `memory/feedback_audit_every_change.md`. Run the per-rule
pre-check (one SQL query — see
`docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`)
before scaffolding. As of 2026-04-25 the queue is **structurally
exhausted on the current 100-cmdr fixture** — every untried
proposal targets archetypes the golden set wasn't designed to
cover. See
`docs/solutions/best-practices/gap-report-queue-dry-on-golden-set-2026-04-25.md`
for the empirical state and the two real next steps (expand the
golden set, or pivot to existing-rule tuning).

### 7. Commit

One commit per rule:

```
feat: <rule_id> for <mechanic>-axis commanders

<one-paragraph what + why, no commander names in the header>

Impact:
- <commander A>: hi_syn X→Y, on_page A→B
- ...
- Golden set NDCG unchanged at <value>

<test count> new tests; <total> total tests, <coverage>% coverage.
```

## Anti-patterns

- **Per-commander hardcoding.** If the gate mentions a card name or
  oracle text fragment that isn't a Forge port shape, it's wrong.
  Extract the underlying mechanical signature instead.
- **Chasing EDHREC overlap as the goal.** Hi-Syn for niche
  commanders is curated by tens of decks and isn't a reliable
  ranking signal. Use it to validate that a *mechanical* hypothesis
  surfaces correct cards, not as the optimisation target.
- **Rule sprawl from the same axis.** If two new rules differ only
  in which tier they emphasize, merge them. Each rule should have a
  distinct mechanical hypothesis.
- **Skipping the regression baseline.** The golden set takes ~5 s to
  re-score. Never commit a rule without confirming NDCG.

## Rule-gate registry

`src/mtg_synergy_graph/complement_rules/registry.py` holds the per-
rule gate predicates the auditor uses for exact per-port attribution.
Two sources populate `RULE_GATES`:

1. **Formal rules** — auto-registered from `COMPLEMENT_RULES`. Each
   rule's gate is "port_type matches AND event_class in event_pairs".
2. **Card-attribute rules** — hand-registered when their gate isn't
   trivially derivable from a static dataclass. The rule author adds
   a gate function (`(PortRow) -> bool`) alongside the helper
   function in `utility.py` / `density.py` / etc. Without
   registration, the rule still works at runtime but the auditor
   can't attribute its activations to a specific port.

`CARD_LEVEL_RULES` is a special set of rules whose activation is
based on the commander's card-identity attributes (subtypes, oracle
text features) rather than any port shape — `tribal_density`,
`lord`, `value_engine`, `affinity_archetype`, `static_strategy`.
These are excluded from the auditor's unregistered-rule fallback so
their firing doesn't bloat per-signature activation rates.

When adding a new rule:
- Author the helper + tests as usual.
- Add a gate function to `registry.py` if the rule consumes a
  specific port shape.
- Add to `CARD_LEVEL_RULES` if it operates on card identity.
- Re-run `gap_report.py` to confirm the registry change shrinks
  apparent gaps for ports the rule was supposed to cover.

## Known limitations of the tooling

- Rules without a registered gate fall back to commander-level
  attribution: if any unregistered rule fires for a commander, every
  port shape they carry is marked "covered" by it. This means
  unregistered rules can mask real gaps. Mitigation: register every
  port-level rule. Today 35 of ~60 rules are registered (9 formal +
  26 card-attribute); the remaining ~25 rules are either narrow
  specialists or genuine card-level rules already in
  `CARD_LEVEL_RULES`.
- `port_universe.json` enumerates qualifier *tokens* but not their
  combinatorial overlap (e.g. `attacking+modified`). For nuanced
  axes look at the raw `valid_filter` distribution in
  `card_ports`.
- `gap_report.py` template catalog is small (5 templates today). A
  proposal labelled `replacement_stack` or `axis_feeder` is the
  auditor's best fit, not a guaranteed match — investigate the
  actual mechanic before implementing. Templates without a
  reasonable fit fall through (`_propose` returns None) and the
  gap is omitted from the report rather than surfacing a misleading
  proposal.
