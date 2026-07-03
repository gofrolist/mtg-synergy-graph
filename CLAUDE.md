# CLAUDE.md

Guidance for Claude Code working in this repo.

## Project Overview

MTG Synergy Graph — deterministic, rule-based EDH/Commander synergy scorer
using Forge DSL ports. No training, no EDHREC at inference.
Current aggregate NDCG@30 ~ 0.256 on the 100-commander golden set.

For user-facing setup / quick-start, see [README.md](README.md).
For the forward-looking rule-planning workflow (gap_report → scaffold →
audit cycle), see [docs/RULE_PLANNING.md](docs/RULE_PLANNING.md).
For a dated log of rule additions, audit verdicts, and per-commander
impact notes, see [docs/RULE_HISTORY.md](docs/RULE_HISTORY.md).
For documented solutions to past problems (bugs, best practices,
workflow patterns) — organized by category with YAML frontmatter
(`module`, `tags`, `problem_type`) — see
[docs/solutions/](docs/solutions/). Relevant when debugging or
implementing in a previously documented area.

## Common Commands

```bash
uv run python scripts/import_cardsfolder.py                              # Import fresh DB
uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King" --top 30 --explain
uv run pytest tests/                                                     # ~1230 tests, ~1-2s
uv run python scripts/gap_report.py                                      # Ranked gap proposals; since plan 2026-05-02-001 v1.0 each entry shows a Stage A pre-flight verdict (PASS / WARN / REJECT) and entries are grouped into bands
uv run python scripts/scaffold_rule.py --apply --walk N --strict-warn    # Walker (since plan 2026-05-02-001 v1.0): autonomous mode skips Stage A REJECTs and (with --strict-warn) WARNs; outcomes logged to .audit/walker_outcomes.csv. Add --force --force-reason "<text>" to override a WARN; logged to .audit/preflight_overrides.csv
uv run python scripts/rule_quality_gate.py --rule RULE_ID                # Pre-commit quality gate for new rules (catches vacuum-fill / flat-noise pathology; see docs/solutions/best-practices/rule-quality-gates-2026-04-24.md)
uv run python scripts/rule_quality_gate.py --all-declarative --sample 20 # Batch gate across current declarative set

# Unified eval harness (bench.py) — replaces _audit_rule_impact /
# golden_set_track / compare_edhrec / weight_grid_search / broad_set_track.
# See docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md.
uv run scripts/bench.py audit                                            # Compare live scoring vs pinned baseline, emit verdict + .audit/last.md
uv run scripts/bench.py audit --rule RULE_ID                             # Per-rule ablation summary (SQL over persisted tensor; <2s)
uv run scripts/bench.py audit --inspect RULE_ID --limit 20               # Top contribution rows for RULE_ID
uv run scripts/bench.py audit --collinearity                             # Pairwise VIF + Pearson correlation across rules
uv run scripts/bench.py audit --expect-identity                          # Assert bitwise-identical scores (for pure refactors)
uv run scripts/bench.py audit --repin --yes                              # Rebuild pinned fixture from current working tree
uv run scripts/bench.py audit --unknowns                                 # List port_nodes rows with node_kind='UNKNOWN' (plan 003 Unit 6)
uv run scripts/bench.py audit --inspect-gems                             # Per-commander lost/gained hidden-gem diff (plan 003-gem)
uv run scripts/bench.py audit --trend hidden_gems --trend-n 20           # CSV history of aggregate hidden_gem_hit_rate across audit runs
uv run scripts/bench.py audit --vs-forge-oracle                          # Kendall-τ sidecar comparing our top-N vs Forge CardRanker (plan 002)
uv run scripts/build_embeddings.py                                       # Build card_embeddings after cardsfolder refresh (plan 2026-04-23-003)
uv run scripts/bench.py audit --embedding-dedup                          # Rule-pair redundancy diagnostic in embedding space
uv run scripts/bench.py audit --embedding-dedup --threshold 0.90         # Looser threshold for exploration

# Divergence forensics (plan 2026-06-10-001) — read-side standing instrument; classifies every
# EDHREC-label miss into NEAR_MISS / OUTRANKED / FILTERED / DATA_GAP / NO_RULES buckets via a live
# production-ranking pass + the persisted tensor. Writes .audit/forensics.md and appends a
# provenance-stamped row to .audit/forensics_history.csv. Zero scoring-path changes.
uv run scripts/bench.py audit --forensics                                 # Full per-miss failure taxonomy + metric sidecars + R9 justified-divergence view
uv run scripts/bench.py audit --forensics --format json                   # Machine-readable report
uv run scripts/bench.py audit --forensics --ablate-tiebreak               # + one-off EDHREC tiebreaker-credit measurement (bracketed weak/strong keys)
uv run scripts/bench.py audit --trend forensics                           # History of bucket proportions / NDCG / gem rate with (config, snapshot) boundary markers

# Forge-Second-Oracle pipeline (plan 2026-04-23-002) — design-time only, never at inference.
# --smoothing-k defaults to 0.0 since 2026-04-24 (ac38957). The build-time and
# query-time flags must agree or the config-hash check on --vs-forge-oracle rejects the DB.
uv run scripts/forge_oracle.py build --smoothing-k 0.0                   # Build data/forge_oracle.db from Forge precon .dck corpus (~670 decks)
uv run scripts/forge_oracle.py propose-rules --top 20 --smoothing-k 0.0  # N rule scaffolds ranked by impact * forge_signal

# Embedding weight sweep (plan 003 Phase C) — design-time diagnostic, does not mutate state.
uv run scripts/sweep_embedding_weights.py                                # Grid-search (w_emb, k) cells, print hit_rate + score_delta per cell

# Portfolio-selection kill-test instrument (plan 2026-07-02-004, DECLINED at R0 — standing
# ranking-transform kill-test infra; zero scoring-path changes, reports to .audit/portfolio_sim/).
uv run scripts/portfolio_sim.py bands                                    # Bootstrap NDCG/gem noise bands + R9a baseline pass rates (500-cmdr fixture)
uv run scripts/portfolio_sim.py share                                    # Empirical addressable-share readout for EDHREC-label misses (R7b)
uv run scripts/portfolio_sim.py sweep                                    # Decay-form × λ × granularity × discount-base grid with trap/ablation sidecars

# Demand-coverage instrument (plan 2026-07-02-005, DECLINED at Stage 0 — standing forensics
# companion; zero scoring-path changes, reports to .audit/demand_coverage/). Three-way commander-
# port classification (unconsumed / material-yield / starved), feeder-yield diagnosis, and
# addressable-share readout vs the provisional pairing table (resource_flows_seed.json, hash-inert).
# Re-run after major rule additions or data refreshes to re-measure under-served demand.
uv run python scripts/demand_coverage.py                                 # Full 100-cmdr run (~2 min; includes a live forensics pass)
uv run python scripts/demand_coverage.py --commander "Yawgmoth, Thran Physician"  # Filtered debug run

# Tensor-driven weight optimizer (plan 2026-04-26-001 M1) — Coordinate Ascent over
# _RULE_QUALITY_MULTIPLIER. Emits .audit/optimize_proposal.json for human review;
# never auto-mutates src/mtg_synergy_graph/data/scoring_weights.json. Append-only history at
# .audit/optimize_history.csv. Exit codes: 0 success, 1 driver exception,
# 2 stale tensor / fixture too small, 3 self-test failed (calibration issue).
#
# --optimize defaults to tests/fixtures/golden_set_run_500.json (500 commanders);
# the 100-cmdr canonical is too small for trustworthy gradient signal — see
# docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md. Pass
# --fixture tests/fixtures/golden_set_run.json to use the 100-cmdr canonical
# explicitly. Regenerate the 500 fixture after data refreshes:
#   uv run python scripts/bootstrap_golden_set_500.py
uv run scripts/bench.py audit --optimize                                 # Full sweep on the 500-cmdr default fixture
uv run scripts/bench.py audit --optimize --no-self-test --max-sweeps 1   # Quick exploratory pass (skip planted-perturbation check)
uv run scripts/bench.py audit --optimize --seed 17                       # Different train/held split for cross-validation
uv run scripts/bench.py audit --optimize --format json                   # Machine-readable run-summary on stdout (for agent pipelines)
uv run scripts/bench.py audit --optimize --alpha 0.3                     # Tune nDCG-vs-gem blend (alpha=0 → gem-only, alpha=1 → nDCG-only)
uv run scripts/bench.py audit --optimize --grid 0.8 0.9 1.1 1.25         # Custom multiplicative grid (default 0.5 0.75 1.25 1.5 2.0)
# Other knobs: --eps-step --eps-cumulative --clamp-min --clamp-max --train-ratio --wall-clock-seconds --self-test-seed
```

The bench.py hook also runs advisorily on pre-commit when edits touch
`complement_rules/`, `universal_scorer.py`, `graph_engine.py`,
`embeddings/`, or `src/mtg_synergy_graph/data/scoring_weights.json`; see
`memory/feedback_audit_every_change.md` for the guardrail.

## Data Model

Port extraction: 108,644 ports from 32,327 cards (GenericChoice +
StaticAbilities$ expansion, deduped after A1's 2^N re-walk fix).

**Extra `port_attributes`** (beyond standard valid_filter explosion):
- `attr_kind='change_type'` for ChangeZone effect ChangeType$ clauses
  (Kaalia's Angel/Demon/Dragon cheat-into-play list).
- `attr_kind='token_color'` + `attr_kind='token_subtype'` for every TokenScript
  produced by a Token effect (multi-color prefixes like `gw`, `all`; and
  artifact-creature format like `c_0_1_a_thopter`).
- `attr_kind='attribute'` for `AlterAttribute Attributes$ <V>` values
  (added 2026-05-19, PR #47). One row per comma-separated entry on the
  effect port — surfaces Prepared (29 ports), Suspected (25), Solved
  (15), Plotted (4), Commander (3), Saddled (3), Harnessed (2). Joined
  by `complement_rules/prepared.py::_commander_prepares_creatures` for
  Prepared-enabler commander detection (slow path).

**Synthetic ports** (no source line in the Forge .txt — emitted by the
port extractor itself, not by parsing a `T:`/`A:`/`R:`/`K:` line):
- `port_type='static', event_class='AlternateMode',
  granted_keyword='Prepare'` for every card with the top-level
  `AlternateMode:Prepare` header (47 cards, PR #47). The synthesised
  port is the cheap-path commander-detection signal for
  `complement_rules/prepared.py::_commander_has_alternate_mode_prepare`.
  Emitted by `extract_alternate_mode_ports` in `ports.py`. Restricted
  to the `Prepare` value via `_ALTERNATE_MODE_PORT_VALUES` frozenset —
  emitting for other AlternateMode values (DoubleFaced, Adventure,
  Split, Modal, Flip, Specialize, Omen, Meld) perturbed the depth-2
  cascade walker's Stage-1 prefilter on Tergrid (Modal DFC) before
  the narrowing fix landed.

**`card_hints` table** — normalised projection of Forge's AI annotations
(`DeckNeeds`/`DeckHints`/`DeckHas` → kind `needs`/`hints`/`has`,
`BuffedBy` SVar → kind `buffed_by`). Populated by the importer. Not yet
used by any complement rule — exploratory rules (`deck_hint_match`,
`deck_needs_fulfilled`, `buffed_by_match`) were prototyped and reverted
after each regressed NDCG@30 against EDHREC (curated matches are too
broad and dilute the mechanical-port signal). Retained as data
infrastructure for future work.

**Legality filter** — `cards.legal_commander` is populated from
Scryfall's `legalities.commander` (1 = legal, 0 = not_legal / banned).
Rows with `legal_commander=0` (~1,679, mostly silver-border/acorn/
Unfinity leakage through the Forge cardsfolder) are hard-dropped by
`SynergyEngine.page()` and `legal_cards()` before scoring. Test
fixtures without the column default to 1.

## Scoring Architecture — Universal Port Matcher

Score = count of distinct mechanical interactions between commander ports
and candidate ports, weighted by specificity (IDF). **No hand-tuned weights.
No global penalties.** The commander's ports ARE the query.

### Complement Rules (`complement_rules/`)

Each rule wraps an existing mechanical map and declares when a
(commander_port, candidate_port) pair creates a synergy. Rules group into
primitives (trigger_effect, cost_feeds_trigger, resonance family), density
rules (spell/scaling/tribal/etb_self), archetype rules (voltron, go_wide,
combat_enhancer, graveyard_filler), gated axis-feeders
(`<axis>_feeder`: counter, modified, cardpower, tap_type, hand_size,
gy_fuel, lifegain, life_total, land_bounce, …), and the depth-2
pathway family.

**Depth-2 pathway (`complement_rules/pathway.py`, plan 2026-04-23-001).**
`self_bridging_cascade` fires on candidates whose internal port-graph
forms a length-≤2 loop reinforcing two distinct commander-port
matches — Gravecrawler (sac cost + Sacrificed trigger) for Korvold,
ETB+Token shapes for aristocrats, etc. Internal edges use two
canonical cascade substrates: `EVENT_MATCH_MAP` named trigger→effect
pairs (wildcard `*` rejected post-audit) and `COST_FEEDS_TRIGGER`.
Flag `_ENABLE_PATHWAY_RULES = True` since 2026-04-23 after POSITIVE
verdict at agg Δ +209.3 / hidden_gem_hit_rate 0.7287 → 0.8423.
Explainability: `--explain` emits a `path_info` line per firing
(`<subkind_a> <-> <subkind_b> (channel: event_match|cost_feeds)`).

Full rule catalogue, per-rule gate logic, and IDF weighting details:
see [docs/COMPLEMENT_RULES.md](docs/COMPLEMENT_RULES.md).

### Typed port graph (`port_graph/`, plan 003)

Layered on top of the Python rule helpers: a data-layer substrate
that lets a growing subset of rules be authored as data rows instead
of Python code.

- **Canonical vocabulary** (`port_graph/vocabulary.py`) — closed
  versioned sets of `NODE_KINDS` (21 event-node kinds), `MATCH_QUALITIES`
  (6 predicate kinds), `GATE_OPS` (10 JSON predicate ops) with
  `VOCAB_VERSION`. Every downstream table, view, and interpreter
  check references these constants.
- **`port_nodes` SQL view** (`port_graph/projection.py`) — projects
  every `card_ports` row to a canonical `node_kind` + `subkind`;
  unmapped shapes fall through to `UNKNOWN` with the raw
  `(port_type, event_class)` preserved. Drives the `--unknowns`
  audit reporter.
- **`event_match_map` + `cost_feeds_trigger` tables** (`port_graph/
  event_maps.py`) — SQLite tables seeded from
  `src/mtg_synergy_graph/data/event_match_seed.json`. The Python dicts in `graph_engine.py`
  are loaded from the same JSON at module import, so both
  representations cannot drift. Edit the JSON to add an equivalence.
- **`rules` table + `RuleInterpreter`** (`port_graph/rules_schema.py`,
  `port_graph/interpreter.py`) — declarative complement-rule rows in
  `src/mtg_synergy_graph/data/rules_seed.json`; each row's JSON predicates compile to SQL
  fragments + Python gate callables at interpreter init. Rule IDs
  in `complement_rules.registry.DECLARATIVE_RULE_IDS` route through
  the interpreter; every other rule_id stays on the Python-helper
  path. A rule_id lives in EXACTLY ONE of the two sides.
- **Authoring new rules for covered families**: edit
  `src/mtg_synergy_graph/data/rules_seed.json` + `DECLARATIVE_RULE_IDS`; re-import (or
  call `seed_rules_db(conn)`); no new Python file. The
  `peer_tribal_keyword` family (16 migrated rules in plan 003 Units
  7-8) is the canonical template.
- **`--unknowns` CLI** — `bench.py audit --unknowns` surfaces
  `port_nodes` rows classified as `UNKNOWN`, ranked by distinct
  cards × EDHREC rank weight. Run after each Forge cardsfolder
  refresh to see candidate shapes for vocabulary expansion.

### Evaluation — `hidden_gem_hit_rate` (plan 003-gem)

A second evaluation axis tracked alongside the existing histogram
verdict: `hidden_gem_hit_rate = |plausible_hidden| / 30` per
commander, where `plausible_hidden = (our_top_30 \ edhrec_top_30)`
filtered by a mechanical-plausibility gate (`N_rules_firing >= 2`
OR `total_contribution > per-commander-median`). Operationalizes
`memory/feedback_edhrec_not_goal.md` — the stated "find hidden gems
from mechanics" intent, now measurable.

- **Tracking-only at MVP.** Commit gate stays on the histogram
  verdict; a stderr warning fires when aggregate delta < −0.02 but
  the audit still exits 0. Promotion to a commit-gate requires a
  separate `ce-brainstorm` + `ce-plan` cycle per FR6 escalation.
  See `src/mtg_synergy_graph/bench/hidden_gems.py` module docstring
  for the criteria.
- **Persisted in `FixtureEntry.legacy`** — no schema bump. Old pins
  without gem keys are tolerated (aggregator filters on key
  presence); re-pinning populates them via the new
  `build_fixture(conn, commanders, edhrec_conn=...)` path.
- **`.audit/history.csv`** — every `bench.py audit` run appends a
  row (timestamp, commit_sha, config_hash, aggregate_score_delta,
  hidden_gem_hit_rate, delta, n_commanders, verdict). Gitignored;
  regenerable on fresh checkout.
- **`--inspect-gems` CLI** — per-commander diff of lost/gained
  hidden-gem sets between pin and live. Δ column shows integer
  count out of 30 for legibility.
- **`--trend hidden_gems`** — CSV reader over `.audit/history.csv`;
  `--format md` + `--format json` also supported.

### Algorithm

1. Load commander ports (cached)
2. For each complement rule, find matching candidate ports (2 SQL queries)
3. For card-attribute rules, match against cards table
4. For declarative rules in `DECLARATIVE_RULE_IDS`, run the
   interpreter against the `rules` table
5. Compute IDF weights from candidate frequency
6. Score each candidate = sum of IDF-weighted synergy - anti-synergy + staple bonus
7. Sort by (-score, cmc, edhrec_rank, name)

### Content embeddings (plan 003, flag-gated)

128-dim deterministic TF-IDF + truncated-SVD vectors per card (hand-rolled,
numpy only — no scipy/sklearn). Features: `port_type`, `event_class`,
`zone_origin`/`zone_destination`, `counter_type`, `branch_kind`,
exploded `port_attributes` rows, Scryfall keywords. No oracle-text,
no popularity.

- **Storage**: `card_embeddings` + `card_embeddings_config` tables in
  `data/synergy.db`. Built by `scripts/build_embeddings.py`; rebuilt
  after each `import_cardsfolder.py` refresh. Hybrid hash discipline
  (plan 2026-04-23-003 D3): `EmbeddingConfigInputs` + KV table +
  `ScoringConfigInputs.vectorizer_version`.
- **Scoring**: `embedding_contribution = w_emb · exp(-k · N_rules) ·
  cosine(v_cand, v_cmdr_target)`. Exponential decay so well-rule-
  covered candidates see near-zero contribution.
- **Flag**: `_ENABLE_EMBEDDING_CONTRIBUTION = False` default-off
  (`src/mtg_synergy_graph/embeddings/contribution.py`). Audit-gated
  weight sweep + re-pin required before flipping to True.
- **Diagnostic**: `bench.py audit --embedding-dedup` flags rule
  pairs with near-parallel candidate-activation sets in embedding
  space. Complements `--collinearity` (different mechanism, same
  intent).
- **Commander target**: lazy + `functools.cache` keyed by
  `(commander_set, hi_syn_limit)`. EDHREC hi-syn used OFFLINE
  ONLY at target-vector build; inference path is EDHREC-clean.

### Key Files

- `complement_rules/` — rule helpers + registry, `find_all_complements()`
- `universal_scorer.py` — IDF computation, `score_all_universal()`
- `engine.py` — `SynergyEngine.page()`, public API
- `graph_engine.py` — `EVENT_MATCH_MAP`, `COST_FEEDS_TRIGGER`, port matching primitives
- `port_graph/` — typed port-graph substrate: vocabulary, `port_nodes`
  view, `event_match_map`/`cost_feeds_trigger`/`rules` tables,
  `RuleInterpreter`
- `src/mtg_synergy_graph/data/event_match_seed.json`, `src/mtg_synergy_graph/data/rules_seed.json` — committed
  seed artifacts. Edit these instead of code for equivalence /
  declarative-rule changes. Since 2026-06-09 functional edits to
  either file (and to `heuristics.STAPLES`) flip
  `compute_config_hash` → re-pin required; prose blocks (`_readme`,
  grammar docs) do not. Drift between `rules_seed.json` and an
  already-built DB's `rules` table raises at interpreter init —
  re-run the importer or `seed_rules_db(conn)` to resync.
- `src/mtg_synergy_graph/data/scoring_weights.json` — source-of-truth for
  `_RULE_QUALITY_MULTIPLIER` (per-rule IDF multipliers) and
  `_FLAT_WEIGHT_OVERRIDES` (per-rule density-bucket overrides),
  loaded by `universal_scorer` at module import. Edit a `value` to
  retune (flips `compute_config_hash` → re-pin via `bench.py audit
  --repin --yes`); edit a `comment` for context (does not flip the
  hash). Per-key sweep history lives in commit messages and
  `docs/RULE_HISTORY.md`.

## Conventions

- Cards keyed by Scryfall `oracle_id`.
- SQL fragment interpolation guarded by `_VALID_*_EXPRS` frozensets +
  `ValueError` (never `assert` — stripped by `python -O`).
- Rule additions follow the gap_report → scaffold → audit workflow. See
  [docs/RULE_PLANNING.md](docs/RULE_PLANNING.md) for the full pipeline.
- Tests must never pass a literal project-relative DB path
  (`db="data/synergy.db"`, `"data/tags.db"`, etc.) to any code that
  may reach `open_db()` / `sqlite3.connect()`. `CREATE TABLE IF NOT
  EXISTS` will silently materialize an empty file at the repo root,
  poisoning skip-guards in other tests that key off file existence
  (e.g., `tests/test_etb_tapped_stax_null_filter_invariant.py`). Use
  `tmp_path` paths even when the test expects an early bail —
  refactors can move the `open_db` call before the bail. See PR
  history for `tests/bench/test_optimize.py` (commit `af4f8bc`).
  Since 2026-06-09 this is mechanically enforced: a session-scoped
  autouse fixture in `tests/conftest.py` fails the run when a new
  `*.db` file appears under the repo root or `data/`, and
  `SynergyEngine` opens its DB with `open_db(..., create=False)`
  (FileNotFoundError with a rebuild hint instead of materializing an
  empty DB).

## Release workflow

Trigger via `gh workflow run release.yml -f bump=<major|minor|patch>`.
Pipeline: `release.yml` (bump pyproject + git-cliff CHANGELOG + open
PR with auto-merge) → PR merge → `tag-release.yml` (push `vX.Y.Z`
tag) → `publish-wheel.yml` (build wheel + create GitHub release).

**Gotchas — every one of these has bitten us:**

- **`GITHUB_TOKEN` does not cascade-trigger workflows.** The default
  token used inside an Action will NOT fire other `on: pull_request`
  or `on: push: tags` workflows. Two consequences:
  - The release PR opened by `release.yml` does not auto-trigger CI
    on the release branch — PR sits in `BLOCKED` mergeability with
    no checks. Recovery: `gh pr close <N> && gh pr reopen <N>` from
    user creds (NOT inside an Action). The reopen fires
    `on: pull_request` workflows; auto-merge gets cleared by the
    close, so re-enable with `gh pr merge <N> --squash --auto`.
  - The tag pushed by `tag-release.yml` does not auto-trigger
    `publish-wheel.yml`. Recovery: `git push origin
    :refs/tags/vX.Y.Z && git push origin vX.Y.Z` from a local clone
    under user creds. Since 2026-06-09 a tag ruleset ("Protect
    release tags", id 17471689) blocks `v*` update/deletion for
    non-admins; the recovery still works for the repo admin via
    bypass — expect a "Bypassed rule violations" notice. Tag
    CREATION stays open because GitHub Actions cannot be a bypass
    actor on a personal repo and `tag-release.yml` must push new
    tags; wheel provenance attestation (publish-wheel.yml) makes a
    forged tag's artifact detectable regardless.

- **Repo setting prerequisite.** `Settings → Actions → General →
  Workflow permissions → Allow GitHub Actions to create and approve
  pull requests` must be ON, otherwise `gh pr create` inside
  `release.yml` fails with "GitHub Actions is not permitted to
  create or approve pull requests". Verify via `gh api
  repos/<owner>/<repo>/actions/permissions/workflow` —
  `can_approve_pull_request_reviews` must be `true`. Flip with
  `gh api -X PUT
  repos/<owner>/<repo>/actions/permissions/workflow -f
  default_workflow_permissions=read -F
  can_approve_pull_request_reviews=true`.

- **Pre-commit hook rebuilds `uv.lock`.** An empty commit on a
  release branch may fail pre-commit if the local lock drifts from
  the branch's `pyproject.toml`. Prefer the close/reopen workaround
  above over local commits on the release branch.

- **Branch protection on `main`.** Two required status checks. Direct
  pushes from a privileged user emit "Bypassed rule violations" but
  go through; prefer the PR path for anything beyond a hotfix.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
