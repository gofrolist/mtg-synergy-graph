---
title: "feat: Unified eval harness + rule-contribution tensor"
type: feat
status: active
date: 2026-04-22
origin: docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md
---

# feat: Unified eval harness + rule-contribution tensor

## Overview

Replace the four existing eval scripts (`_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, `weight_grid_search.py`) with one `scripts/bench.py` CLI backed by a persisted rule-contribution tensor. Cut per-change audit cycle from ~10 min to < 30 s on a pinned reference fixture. Replace the binary TRIVIAL verdict with a rank-shuffle histogram. Install a pre-commit hook that runs `bench.py` advisorily on scoring-path edits so the `memory/feedback_audit_every_change.md` guardrail becomes cheap to follow.

## Problem Frame

Every recent commit touching the scoring path is a manually-performed coordinate-descent step limited by a ~10-minute audit cycle (see `origin: docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md`). The existing eval surface is 1,343 LOC across four scripts, each re-scoring the full 100-commander golden set from scratch. That latency blocks rule-authoring throughput and makes the "audit every scoring change" guardrail expensive to enforce. The work is not extracting new data — `UniversalScore.complements` already exposes per-(commander, candidate, rule) contributions in-memory per `src/mtg_synergy_graph/universal_scorer.py:204-252`. The work is **persisting** those contributions once and replacing full re-runs with SQL queries over the persisted tensor.

## Requirements Trace

- **R1** (origin FR1 — Rule-contribution tensor): persist per-(commander, candidate, rule) contributions to a new SQLite table.
- **R2** (origin FR2 — Pinned reference fixture): extend `tests/fixtures/golden_set_run.json` to carry the tensor baseline; add `--repin` for explicit acceptance.
- **R3** (origin FR3 — Unified CLI): consolidate the four scripts into `scripts/bench.py` with subcommands (`audit`, `--rule`, `--inspect`, `--collinearity`, `--repin`, `--expect-identity`).
- **R4** (origin FR4 — Rank-shuffle histogram): replace binary TRIVIAL verdict; roll up to the existing 5-category rubric so consumers keep working.
- **R5** (origin FR5 — Pre-commit hook): auto-run on edits to `src/mtg_synergy_graph/complement_rules/**`, `universal_scorer.py`, `graph_engine.py`; warn on HARMFUL; do not block.
- **R6** (origin FR6 — MI-VIF collinearity): `bench.py audit --collinearity` outputs pairwise VIF + Pearson correlation for rule activations.
- **R7** (origin FR7 — Pure-infra refactor mode): `bench.py audit --expect-identity` asserts byte-identical per-(cmdr, cand) scores against the pinned baseline.
- **R8** (origin Success Criterion 1): latency ≤ 30 s for `audit`; ≤ 2 s for `--rule` / `--inspect`.

## Scope Boundaries

- No change to scoring semantics — `score_all_universal` output is bitwise-identical pre/post. Verified by R7.
- No coordinate-descent weight optimizer (downstream follow-up; depends on this landing).
- No scale to 2,761 commanders — stays at 100-cmdr golden set at MVP. Schema must admit the scale without rework.
- No CI / GitHub Actions integration. Local-only.
- No changes to `recommend.py --explain` output format (it can read from the tensor opportunistically; fallback to recompute when tensor missing).

### Deferred to Separate Tasks

- CI integration on a future PR once the local harness is validated.
- 2,761-commander scale — brainstorm pointed to it as a follow-up; revisit after Survivor 1 lands.
- Coordinate-descent weight tuning — separate brainstorm + plan, depends on R1 + R6.

## Context & Research

### Relevant Code and Patterns

- **`src/mtg_synergy_graph/universal_scorer.py`**
  - `UniversalScore` (line 183): fields `complements: list[PortComplement]`, `idf_weights: dict[...]`, staple/circuit/cmc/rank bonuses.
  - `score()` cached-property (lines 204–252): loop sums IDF-weighted synergy per rule.
  - `to_legacy_buckets()` (lines 258–295): already produces per-rule breakdowns — extend, don't re-invent.
  - `_compute_idf_weights()` (line 778): applies `_RULE_QUALITY_MULTIPLIER` at compute-time. Multiplier changes invalidate tensor.
  - `CandidateCache`: natural extension point for the tensor cache.
- **`src/mtg_synergy_graph/complement_rules/core.py:1183-1202`** — `find_all_complements(conn, commander_set, rules, candidate_cache)` returns `list[PortComplement(rule_id, direction, candidate, cmdr_event, cand_event, filter_group, branch_kind)]`. Each port match is already addressable by `rule_id`.
- **`scripts/_audit_rule_impact.py`** — monkey-patches `_find_<rule_id>` helpers to return `[]`; re-scores; diffs NDCG. Output: JSON dicts `{rule_id, touched, scored, sum_delta, movers, verdict}`. Verdict rubric (lines 16–31): `positive` / `MARGINAL` / `TRIVIAL` / `CONTENTIOUS` / `HARMFUL`.
- **`src/mtg_synergy_graph/schema.sql`** — existing tables: `cards`, `card_ports`, `synergy_edges`, `card_svars`, `port_attributes`. No contribution column exists yet.
- **`tests/fixtures/golden_set_run.json`** — per-commander object: `{commander, edhrec_top10, hi_syn_hits, hi_syn_total, ndcg30, on_page_hits, top10}`. Extend with tensor rows.
- **`.pre-commit-config.yaml`** — existing hooks: ruff, pyright, pytest. Additive extension point.
- **`pyproject.toml`** — Python 3.13, pytest cov≥80%, pyright strict, ruff `E/W/F/I/B/UP/S/SIM/RUF`.
- **`docs/RULE_PLANNING.md`** — documents the `gap_report → scaffold → audit` workflow that `bench.py` must preserve.

### Institutional Learnings

- No `docs/solutions/` directory exists. No institutional-learning entries to carry forward.
- Relevant memory:
  - `memory/feedback_audit_every_change.md` — this plan is the instantiation of the guardrail.
  - `memory/feedback_audit_metric_too_coarse.md` — TRIVIAL hides rank shuffles; histogram verdict addresses this.

### External References

Not consulted. Local code patterns are strong (SQLite schema additions are well-precedented; monkey-patch audit is already in-repo). MI-VIF is a standard numpy one-liner; no external package needed.

## Key Technical Decisions

- **Tensor storage = new `rule_contributions` table in the existing SQLite DB**, not a separate file. Rationale: keeps all scoring artifacts in one place; leverages existing import/migration infrastructure; enables joins to `cards` / `card_ports` for analysis. Extends `src/mtg_synergy_graph/schema.sql`.
- **Config hash is the cache key**. Compute `config_hash = sha256(sorted(rule_ids) + sorted(_RULE_QUALITY_MULTIPLIER.items()) + sorted(_FLAT_WEIGHT_OVERRIDES.items()))` at tensor build time. On audit, compute current hash; mismatch → warn + require `--repin`. No attempt at per-rule incremental invalidation at MVP (full rebuild on re-pin only per origin FR1).
- **Tensor write-path via opt-in hook on `UniversalScore`**, not a scorer rewrite. Add an optional `tensor_sink` callable that `score()` calls once per (commander, candidate). Sink is wired only during `bench.py --repin`; normal inference has zero overhead. Keeps R7 identity invariant trivial to prove.
- **Rank-shuffle histogram is strict superset of existing verdicts.** Histogram buckets: `no_change`, `rank_shuffle_within_top30`, `rank_shuffle_across_top30_boundary`, `hi_syn_gain`, `hi_syn_loss`. 5-category verdict is derived from the histogram so downstream consumers (commit-log conventions, `docs/RULE_HISTORY.md`) keep working. TRIVIAL is redefined: only fires when all commanders land in `no_change`.
- **Pre-commit hook is advisory, not blocking.** HARMFUL verdict prints a warning banner on stderr; commit proceeds. Matches origin brainstorm decision (user explicitly chose "warn, not block"). Hard block is a configurable follow-up.
- **`bench.py` replaces the four scripts; old scripts become thin shims** that print a deprecation line and delegate to `bench.py` subcommands. Rationale: preserves documented commands in `CLAUDE.md` without forcing all contributors to learn the new CLI on day 1. Shims can be removed in a future cleanup.
- **MI-VIF via numpy, not scikit-learn.** VIF is `diag(inv(corr_matrix))`. Numpy is already a dependency; adding scikit-learn for one formula violates project's "minimal dependencies" norm in `pyproject.toml`.
- **Per-commander tensor slice lives in JSON in pinned fixture**, not in a committed SQLite blob. Rationale: git-diff-friendly, reviewable, merge-conflict-friendly; SQLite blobs are opaque.

## Open Questions

### Resolved During Planning

- **Tensor schema shape** (origin open question): row-per-(cmdr, cand, rule) with a `contribution` float column. Long-thin beats wide-per-rule because rule set changes over time. See Unit 2.
- **Invalidation handshake when `_RULE_QUALITY_MULTIPLIER` changes** (origin open question): config-hash mismatch detected at audit time; blocks audit until `--repin`. Simpler than per-multiplier tracking. See Unit 3.
- **Pre-commit framework vs bare hook** (origin open question): extend existing `.pre-commit-config.yaml` (already configured with ruff/pyright/pytest). See Unit 7.
- **`--inspect` output pagination** (origin open question): default 100 rows, `--limit N` flag, sorted by `abs(contribution) DESC`. Rule firing on 1,000+ commanders paginates naturally.

### Deferred to Implementation

- Exact SQL indexing strategy (covering indexes vs per-column) — tune against real query times after Unit 2 lands a working build. Goal: `audit --rule` and `--inspect` queries ≤ 2 s.
- Whether `UniversalScore.score()` cached_property should be invalidated when tensor_sink is set (might need explicit `invalidate_cache()` call before a re-score). Decide when implementing Unit 2.
- Shim deprecation messaging wording — decide at Unit 8 implementation time.

## Implementation Units

- [ ] **Unit 1: Scaffold `bench/` module + `scripts/bench.py` CLI skeleton**

**Goal:** Create the package structure, CLI skeleton with all subcommands wired to stubs, and the migration that adds the `rule_contributions` table to the schema.

**Requirements:** R1 (table schema), R3 (unified CLI).

**Dependencies:** None.

**Files:**
- Create: `scripts/bench.py` — thin CLI entry point using argparse; dispatches to `bench` module subcommands.
- Create: `src/mtg_synergy_graph/bench/__init__.py` — re-exports.
- Create: `src/mtg_synergy_graph/bench/cli.py` — argparse setup + subcommand dispatch.
- Create: `src/mtg_synergy_graph/bench/_stubs.py` — placeholder functions returning "NotImplemented" for Unit 2+ slots.
- Modify: `src/mtg_synergy_graph/schema.sql` — add `CREATE TABLE IF NOT EXISTS rule_contributions (commander TEXT, candidate TEXT, rule_id TEXT, contribution REAL, idf_weight REAL, raw_count INTEGER, config_hash TEXT, computed_at TEXT, PRIMARY KEY (commander, candidate, rule_id, config_hash))` plus `CREATE INDEX idx_rule_contributions_rule ON rule_contributions(rule_id, config_hash)` and `CREATE INDEX idx_rule_contributions_cmdr_hash ON rule_contributions(commander, config_hash)`.
- Modify: `src/mtg_synergy_graph/importer.py` (or wherever schema is applied) — ensure the new table is created on fresh imports.
- Test: `tests/bench/test_cli_skeleton.py` — exercises every subcommand dispatches without crashing (stubs return NotImplemented cleanly).

**Approach:**
- CLI verbs: `audit` (default), with flags `--rule <id>`, `--inspect <id>`, `--collinearity`, `--repin`, `--expect-identity`, `--format {md,json}`, `--output <path>`, `--commander <name>`, `--limit N`.
- Subcommand dispatch as a dict of callables; each Unit 2-7 fills in its slot.
- Schema migration must be idempotent (`IF NOT EXISTS`) to support re-import on existing DBs.

**Patterns to follow:**
- `scripts/gap_report.py` for CLI shape + argparse conventions.
- `src/mtg_synergy_graph/schema.sql` for table creation idiom + indexes.

**Test scenarios:**
- Happy path: `bench.py audit --help` prints all subcommand flags and exits 0.
- Happy path: `bench.py audit` with stubs prints "not yet implemented" diagnostic and exits non-zero cleanly.
- Edge case: `bench.py audit --format bogus` rejects invalid format with argparse error.
- Integration: running `importer.py` against a fresh SQLite DB creates the `rule_contributions` table with correct PRIMARY KEY + two indexes, verified via `PRAGMA table_info` / `PRAGMA index_list`.

**Verification:**
- `uv run scripts/bench.py audit --help` exits 0 and lists every subcommand planned in R3.
- A fresh `uv run scripts/import_cardsfolder.py` produces a DB whose `rule_contributions` table exists with the documented schema.

---

- [ ] **Unit 2: Tensor write-path via `UniversalScore` hook**

**Goal:** Add opt-in contribution sink to `UniversalScore.score()` so per-(cmdr, cand, rule) rows can be extracted during scoring. Persist them to `rule_contributions`.

**Requirements:** R1, R7 (identity preservation depends on tensor capturing exactly what `score()` produces).

**Dependencies:** Unit 1.

**Files:**
- Modify: `src/mtg_synergy_graph/universal_scorer.py` — add optional `tensor_sink: Callable[[str, str, str, float, float, int], None] | None` field on `UniversalScore`; in `score()` cached_property, call sink once per (commander, candidate, rule_id) with `(contribution, idf_weight, raw_count)`; guard with `if self.tensor_sink is not None`.
- Create: `src/mtg_synergy_graph/bench/tensor.py` — `TensorWriter` class wrapping a SQLite connection; batches inserts; exposes `as_sink() -> Callable` for `UniversalScore` hookup; computes `config_hash` from active rule set + multipliers + overrides.
- Test: `tests/bench/test_tensor_write.py`
- Test: `tests/bench/test_universal_scorer_identity.py` — scoring WITHOUT a tensor_sink produces bitwise-identical `UniversalScore.score()` output to pre-change.

**Execution note:** Start with a failing identity test (`tests/bench/test_universal_scorer_identity.py`) that asserts scoring has no observable behavior change when `tensor_sink=None`. Keep that test green through every subsequent change in this unit.

**Approach:**
- `tensor_sink` is `None` by default in production paths — this is how R7 is guaranteed.
- Sink is a callable, not a class dep, to keep `UniversalScore` lightweight and avoid import cycles.
- `TensorWriter` batches inserts in chunks of ~10,000 rows to avoid SQLite transaction overhead.
- `config_hash` = hex digest of SHA256 over `(sorted(registered_rule_ids), sorted(_RULE_QUALITY_MULTIPLIER.items()), sorted(_FLAT_WEIGHT_OVERRIDES.items()))`. Stored alongside each row.

**Patterns to follow:**
- Optional hook field pattern in `CandidateCache` (same file) — keeps hot path untouched when unused.
- Batch-insert pattern in `src/mtg_synergy_graph/importer.py`.

**Test scenarios:**
- Happy path: scoring 1 commander × 100 candidates × N rules writes exactly (distinct rule firings) rows; each row has non-null contribution + IDF weight + config hash.
- Happy path: running a full golden-set scoring with sink writes ~N_cmdrs × ~N_candidates × sparsity rows; query `SELECT COUNT(*) FROM rule_contributions` returns non-zero.
- Identity: `UniversalScore.score()` with `tensor_sink=None` produces bitwise-identical output pre- and post-change. Run against a 5-commander fixture.
- Edge case: `config_hash` computation is deterministic — running `TensorWriter.compute_hash()` twice on the same config yields the same hash.
- Edge case: changing `_RULE_QUALITY_MULTIPLIER` for any rule changes the hash.
- Integration: write tensor for commander A, then commander B; query by commander filters correctly via index.
- Error path: sink callable that raises propagates the error, does not silently swallow.

**Verification:**
- After scoring a small fixture with `tensor_sink` wired, `SELECT rule_id, contribution FROM rule_contributions WHERE commander = ? AND candidate = ?` returns the same per-rule breakdown that `UniversalScore.to_legacy_buckets()` produces in-memory.
- Identity test passes: `pytest tests/bench/test_universal_scorer_identity.py -q` green.

---

- [ ] **Unit 3: Pinned reference fixture + `--repin` / `--expect-identity`**

**Goal:** Extend `tests/fixtures/golden_set_run.json` to carry the tensor baseline. Implement `bench.py audit --repin` (rebuild + write) and `bench.py audit --expect-identity` (load + assert bitwise-identical per-(cmdr, cand) scores).

**Requirements:** R2, R7.

**Dependencies:** Unit 2.

**Files:**
- Modify: `tests/fixtures/golden_set_run.json` — extended schema includes top-level `config_hash` field + per-commander `tensor_rows: [{candidate, rule_id, contribution, idf_weight, raw_count}]` section.
- Create: `src/mtg_synergy_graph/bench/fixture.py` — `PinnedFixture` class with `load(path)`, `write(path, scores, tensor_rows)`, `compare(live, pinned) -> list[Discrepancy]`, `assert_identity(live, pinned)`.
- Modify: `src/mtg_synergy_graph/bench/cli.py` — wire `--repin` and `--expect-identity` subcommand slots.
- Test: `tests/bench/test_fixture_roundtrip.py`
- Test: `tests/bench/test_expect_identity.py`

**Approach:**
- `--repin` requires `--yes` flag to avoid accidental overwrites. Without `--yes`, prints a preview diff and exits with instruction.
- `--expect-identity` iterates `(commander, candidate)` in both live and pinned; asserts score fields match bitwise (float equality, not approximate). Any diff is a failure.
- Fixture grows — quick estimate: 100 cmdrs × ~500 cands × ~5 rules = ~250k rows, ~100 KB JSON after compact formatting. Acceptable.

**Patterns to follow:**
- Existing JSON fixture shape in `tests/fixtures/golden_set_run.json`.
- `scripts/golden_set_track.py` for fixture-diff UX.

**Test scenarios:**
- Happy path: `--repin --yes` writes a fixture; immediate `--expect-identity` passes.
- Happy path: tweak `_RULE_QUALITY_MULTIPLIER` for one rule, run `--expect-identity` → fails with specific (cmdr, cand, score_delta) list.
- Edge case: `--repin` without `--yes` prints preview and exits non-zero; fixture unchanged.
- Edge case: fixture load from nonexistent path produces a clear error.
- Edge case: fixture with wrong `config_hash` triggers a warning with remediation hint ("run `bench.py audit --repin --yes`").
- Error path: corrupted JSON in fixture → clear ValidationError not a cryptic JSON exception.
- Integration: `--repin` → manual edit of one rule file that shouldn't change scoring → `--expect-identity` passes (tests that refactors preserve scores).

**Verification:**
- `bench.py audit --repin --yes` on a fresh checkout writes a fixture matching the current scoring path.
- `bench.py audit --expect-identity` green immediately after `--repin`.
- Any intentional score change (e.g., weight tweak) causes `--expect-identity` to fail with actionable output.

---

- [ ] **Unit 4: `bench.py audit` main subcommand**

**Goal:** Full audit on 100-cmdr golden set against pinned fixture. Prints aggregate NDCG delta, per-commander winners/losers, and verdict roll-up. Replaces `_audit_rule_impact.py`'s `_run_baseline_pass`.

**Requirements:** R3, R4 (delegates to Unit 5 for histogram), R8 (latency ≤ 30 s).

**Dependencies:** Unit 2, Unit 3.

**Files:**
- Create: `src/mtg_synergy_graph/bench/audit.py` — `run_audit(conn, fixture_path, rule_filter=None) -> AuditReport`.
- Create: `src/mtg_synergy_graph/bench/report.py` — `AuditReport` dataclass + `to_markdown()` + `to_json()` renderers.
- Modify: `src/mtg_synergy_graph/bench/cli.py` — wire `audit` as default subcommand.
- Test: `tests/bench/test_audit_main.py`

**Approach:**
- Uses `concurrent.futures.ProcessPoolExecutor` across the 100 commanders (mirrors current `_audit_rule_impact.py` parallelism).
- Each worker scores its commander WITH tensor sink → tensor row stream → main process aggregates + diffs against pinned fixture.
- Wall-clock target ≤ 30 s on an M-class Mac. Bench against the existing `_audit_rule_impact.py` runtime.
- Output in `.audit/last.md` by default when `--output` is omitted and `--format md`.

**Patterns to follow:**
- `scripts/_audit_rule_impact.py` structure for per-commander execution.
- `scripts/golden_set_track.py` for aggregate reporting.

**Test scenarios:**
- Happy path: audit on HEAD vs freshly-repinned fixture returns `Δ NDCG = 0.000000`, `histogram: 100 no_change`.
- Happy path: synthetic single-rule weight change produces a verdict on the expected commanders; all others unchanged.
- Edge case: missing fixture → clear error with `--repin` hint.
- Edge case: fixture config_hash mismatch (rule set changed, fixture stale) → warning + instruction, not a crash.
- Error path: one worker crashing propagates failure, prints which commander faulted.
- Integration: full golden-set run completes within 30 s wall-clock.

**Verification:**
- `bench.py audit` on current HEAD produces a clean report with `Δ NDCG = 0.0`.
- Timing: `time uv run scripts/bench.py audit` ≤ 30 s.
- Output `.audit/last.md` is well-formed markdown with all required sections.

---

- [ ] **Unit 5: Rank-shuffle histogram verdict + 5-category rollup**

**Goal:** Implement the five-bucket histogram defined in origin FR4, and derive the existing `positive/MARGINAL/TRIVIAL/CONTENTIOUS/HARMFUL` rollup from it.

**Requirements:** R4.

**Dependencies:** Unit 4.

**Files:**
- Create: `src/mtg_synergy_graph/bench/histogram.py` — `compute_histogram(live, pinned) -> Histogram`, `rollup_to_verdict(hist) -> Verdict`.
- Modify: `src/mtg_synergy_graph/bench/audit.py` — include histogram in `AuditReport`.
- Modify: `src/mtg_synergy_graph/bench/report.py` — render histogram in both `md` and `json`.
- Test: `tests/bench/test_histogram.py`
- Test: `tests/bench/test_verdict_compat.py`

**Approach:**
- Per commander, classify into one of: `no_change`, `rank_shuffle_within_top30`, `rank_shuffle_across_top30_boundary`, `hi_syn_gain`, `hi_syn_loss`.
- Aggregate counts across 100 commanders.
- Verdict mapping (preserves `_audit_rule_impact.py` semantics):
  - `TRIVIAL` iff `no_change == 100` (tightened from "net zero movement" — rank shuffles are no longer lumped with TRIVIAL).
  - `HARMFUL` iff aggregate NDCG Δ < 0 AND no commander hit `hi_syn_gain`.
  - `CONTENTIOUS` iff aggregate NDCG Δ < 0 AND ≥ 1 hi_syn_gain ≥ 0.05 NDCG.
  - `MARGINAL` iff aggregate NDCG Δ > 0 AND `|hi_syn_gain| + |hi_syn_loss| / touched < 0.1`.
  - `positive` otherwise.
- Verdict output includes a reference `rank_shuffle_within_top30` count as an advisory number. Previously-TRIVIAL rule shuffles become visible.

**Patterns to follow:**
- `scripts/_audit_rule_impact.py:16-31` for existing verdict semantics.

**Test scenarios:**
- Happy path: 100 no-change commanders → TRIVIAL, verdict mapping exact.
- Happy path: single-rule addition that produces 10 `hi_syn_gain` + 90 `no_change` → `positive` verdict.
- Edge case: 100 rank-shuffles within top-30, no hi-syn delta → `positive` verdict with histogram flagging movement (not TRIVIAL, since movement exists — this differs from old TRIVIAL which would have been called).
- Edge case: 0 `hi_syn_gain` + 10 `hi_syn_loss` + aggregate Δ < 0 → HARMFUL.
- Edge case: 2 `hi_syn_gain` + 5 `hi_syn_loss` + aggregate Δ < 0 + one gain ≥ 0.05 → CONTENTIOUS.
- Verdict compat: for last 10 accepted rule commits (read from git log), the new verdict classifies each as the old rubric did (pull-5 commits pattern check).

**Verification:**
- `tests/bench/test_verdict_compat.py` iterates the 10 historical commits and asserts same verdict.
- Histogram output in markdown reports shows counts + examples.

---

- [ ] **Unit 6: `--rule` / `--inspect` / `--collinearity` subcommands**

**Goal:** Implement per-rule ablation, contribution-tensor inspection, and rule-pair MI-VIF collinearity as SQL queries over the persisted tensor.

**Requirements:** R3, R6.

**Dependencies:** Unit 4.

**Files:**
- Create: `src/mtg_synergy_graph/bench/rule_ops.py` — `ablate_rule(conn, rule_id, fixture) -> AuditReport` (uses tensor diff, not re-score), `inspect_rule(conn, rule_id, limit=100) -> list[row]`.
- Create: `src/mtg_synergy_graph/bench/collinearity.py` — builds rule × (cmdr, cand) activation matrix; computes Pearson correlation + VIF via numpy.
- Modify: `src/mtg_synergy_graph/bench/cli.py` — wire all three slots.
- Test: `tests/bench/test_rule_ops.py`
- Test: `tests/bench/test_collinearity.py`

**Approach:**
- `--rule <id>` SQL: `SELECT commander, candidate, contribution FROM rule_contributions WHERE rule_id = ? AND config_hash = ?` — no re-score needed. Diff vs pinned fixture produces ablation NDCG delta.
- `--inspect <id>` SQL: same query, `ORDER BY ABS(contribution) DESC LIMIT ?`. Renders as Markdown table.
- `--collinearity` builds a sparse matrix `rules × pairs`, computes `corr = np.corrcoef(activations)`, then `vif = 1 / (1 - r_i^2)` per rule. Flags pairs with VIF > 5 + Pearson |r| > 0.8.
- MI-VIF is a specific technique; `bench.py audit --collinearity --metric mi` path is deferred (optional; add only if the Pearson version is insufficient in practice).

**Patterns to follow:**
- Windowed SQL queries in `src/mtg_synergy_graph/engine.py` for query style.
- `numpy.corrcoef` for pairwise correlation; no new dependencies.

**Test scenarios:**
- Happy path: `--rule cheat_cmc` returns a non-empty row list with expected commander names.
- Happy path: `--inspect cheat_cmc --limit 10` returns 10 rows sorted by contribution magnitude.
- Edge case: `--rule nonexistent_rule_id` prints "no rule named X"; exit non-zero.
- Edge case: `--collinearity` on a single rule returns empty-pairs report (no comparison).
- Edge case: `--collinearity` handles rules that fire on zero pairs (VIF undefined) by excluding them with a warning.
- Latency: `--rule` + `--inspect` complete in ≤ 2 s on the full tensor.
- Integration: MI-VIF flags a known-collinear pair (e.g., identify from historic `counter_feeder` ≈ `modified_feeder` if present), or documents absence if none.

**Verification:**
- Latency measurement in test: `timeit` around `--rule` returns < 2 s.
- Collinearity report includes at least one flagged pair or explicit "no collinear pairs detected" line.

---

- [ ] **Unit 7: Pre-commit hook + `.audit/last.md` output**

**Goal:** Install pre-commit hook that runs `bench.py audit` advisorily on edits to scoring-path files. Output to `.audit/last.md`. Warn on HARMFUL; do not block.

**Requirements:** R5.

**Dependencies:** Unit 4, Unit 5.

**Files:**
- Modify: `.pre-commit-config.yaml` — add new local hook entry.
- Modify: `.gitignore` — ignore `.audit/` directory (the report is a local artifact).
- Create: `src/mtg_synergy_graph/bench/hook_entry.py` — pre-commit hook entry point that runs `bench.py audit` and always exits 0 (warnings go to stderr; commits proceed).
- Test: `tests/bench/test_pre_commit_hook.py` — invokes the hook against a fixture repo state.

**Approach:**
- Hook `id: bench-audit`; `entry: uv run python -m mtg_synergy_graph.bench.hook_entry`; `language: system`; `pass_filenames: false`; `files: ^src/mtg_synergy_graph/(complement_rules/.*\.py|universal_scorer\.py|graph_engine\.py)$`; `stages: [pre-commit]`; `verbose: true`.
- Hook entry wraps `run_audit`, writes Markdown to `.audit/last.md`, prints one-line summary to stderr. On HARMFUL verdict, prints `⚠ HARMFUL audit — review .audit/last.md`. Always exits 0.
- `.audit/last.md` replaced on each run; `.audit/history/<timestamp>.md` archive optional (skip at MVP; log via commit inclusion pattern if desired later).

**Patterns to follow:**
- Existing hooks in `.pre-commit-config.yaml` for shell conventions.
- `pre-commit` docs for `files:` regex.

**Test scenarios:**
- Happy path: editing `src/mtg_synergy_graph/complement_rules/density.py` triggers the hook; `.audit/last.md` is written; commit proceeds.
- Happy path: editing `README.md` (outside scoring path) does NOT trigger hook.
- Edge case: HARMFUL verdict prints warning banner; commit still proceeds; hook exits 0.
- Edge case: hook running on first commit (no pinned fixture yet) prints "no pinned baseline; run `bench.py audit --repin --yes` to establish one" and exits 0.
- Error path: hook entry script crash does not hang; exits non-zero with diagnostic (signals a real bug, not a HARMFUL audit).
- Integration: simulated regression commit (rule weight change) triggers hook, writes `.audit/last.md` with HARMFUL verdict + per-commander losers list; commit proceeds with warning.

**Verification:**
- `pre-commit run bench-audit --all-files` against a known-good checkout produces a POSITIVE/TRIVIAL report and writes `.audit/last.md`.
- Against a synthetic regression, the report is HARMFUL and the warning banner is visible.

---

- [ ] **Unit 8: Migrate old scripts to thin shims; update docs**

**Goal:** Leave `_audit_rule_impact.py`, `golden_set_track.py`, `compare_edhrec.py`, `weight_grid_search.py`, `broad_set_track.py` as thin deprecation shims delegating to `bench.py`. Update `CLAUDE.md` to document the new CLI.

**Requirements:** R3 (soft) — preserves documented commands while pointing to the new authoritative entry.

**Dependencies:** Unit 4, Unit 6.

**Files:**
- Modify: `scripts/_audit_rule_impact.py` — replace body with `deprecation_shim("bench.py audit")`.
- Modify: `scripts/golden_set_track.py` — shim to `bench.py audit --expect-identity` (or `--repin` depending on flags).
- Modify: `scripts/compare_edhrec.py` — shim to a new `bench.py compare-edhrec` subcommand (add to Unit 6 scope if time permits, or keep compare_edhrec.py for this one purpose).
- Modify: `scripts/weight_grid_search.py` — shim to `bench.py audit` + iteration (weight sweep is a script on top of `bench.py`; keep the shim mentioning the new workflow).
- Modify: `scripts/broad_set_track.py` — shim to `bench.py audit --set broad` (broad set flag, deferred to follow-up if needed).
- Modify: `CLAUDE.md` — add `bench.py audit` / `bench.py audit --rule X` / `bench.py audit --repin` / `bench.py audit --expect-identity` to the Common Commands section.
- Modify: `docs/RULE_PLANNING.md` — update workflow diagram references from `_audit_rule_impact.py` → `bench.py audit`.
- Test: `tests/bench/test_shims.py` — each shim prints a deprecation notice + delegates.

**Approach:**
- Shims print `DEPRECATED: use 'bench.py <subcommand>'` to stderr, then invoke the underlying `bench` subcommand. Keeps existing muscle memory working while surfacing the new path.
- Removing the shims entirely is a follow-up cleanup PR after ≥ 4 weeks of visible deprecation.
- `compare_edhrec` and `broad_set_track` have different purposes; keep them as first-class `bench` subcommands (add under Unit 6 or a follow-up unit if scope grows).

**Patterns to follow:**
- Other Python deprecation shim patterns in the ecosystem — `warnings.warn(DeprecationWarning, stacklevel=2)` + delegation.

**Test scenarios:**
- Happy path: each shim prints deprecation notice and exits with same code as the delegated `bench.py` call.
- Happy path: `CLAUDE.md` + `docs/RULE_PLANNING.md` reference the new commands.
- Edge case: shim can still be invoked with old flags where the new CLI accepts them (argparse pass-through).
- Edge case: shim invoked with now-unsupported flags prints mapping instruction ("use `bench.py audit --X` instead of old `--Y`").

**Verification:**
- `uv run scripts/_audit_rule_impact.py` runs and outputs the same verdict it used to (via delegation), plus a deprecation notice.
- `CLAUDE.md` grep for `_audit_rule_impact` returns a deprecation note, not a command recommendation.

## System-Wide Impact

- **Interaction graph:** `UniversalScore.score()` now has an optional `tensor_sink` hook; scorer callers that don't set it are unaffected. `CandidateCache` is untouched. `find_all_complements()` is untouched.
- **Error propagation:** tensor write failures in worker processes (Unit 4) must propagate, not silently drop rows — partial tensors silently breaking audits is the main failure mode to guard against. Worker crashes fail the audit; the main process prints which commander faulted.
- **State lifecycle risks:** `.audit/last.md` is recreated on every hook run. No cache of old audits at MVP. If hook is interrupted mid-write, the file is left in whatever state it's in — low risk since it's a local artifact (add `.audit/` to `.gitignore` per Unit 7).
- **API surface parity:** no external APIs change. All changes are internal tooling.
- **Integration coverage:** integration tests must prove identity preservation (R7) end-to-end, since unit tests of `score()` alone don't exercise the sink + tensor + fixture-diff flow.
- **Unchanged invariants:** `score_all_universal()` output is bitwise-identical. `recommend.py --explain` output format is unchanged. The 5-category verdict rubric's downstream consumers (commit messages, `docs/RULE_HISTORY.md`) still see the same labels.

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Tensor rows silently miss a rule → audit wrong | Identity test on scoring output + explicit `--expect-identity` gate; any row loss produces bitwise score diff |
| `config_hash` collision gives false identity | SHA256 over sorted-canonical serialization; risk essentially zero |
| Pre-commit hook slows down commits measurably | Hook only runs when scoring-path files change (files regex); typical commit unaffected; timing target < 30 s total |
| SQLite contention if `--repin` and `--audit` run concurrently | `--repin` takes exclusive lock; concurrent audit fails with clear error |
| Shim pattern confuses users — unclear which is "real" | Deprecation notice on stderr explicitly points to `bench.py`; `CLAUDE.md` updated |
| 5-category verdict compat breaks on edge cases not covered in test_verdict_compat | Pull last 20 commits instead of 10 for the compat fixture; expand if needed |
| Collinearity report on 40+ rules produces too much output | Default: only print pairs with VIF > 5 AND |r| > 0.8; full matrix behind `--collinearity --verbose` |

## Documentation / Operational Notes

- `CLAUDE.md` Common Commands section updated with new `bench.py` invocations (Unit 8).
- `docs/RULE_PLANNING.md` workflow diagram updated (Unit 8).
- New file `.audit/` appears in working trees after first hook run; `.gitignore` excludes it.
- `tests/fixtures/golden_set_run.json` grows by ~100 KB after first `--repin`; reviewable in git diff (JSON formatting preserved, not minified).

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-21-unified-eval-harness-requirements.md](../brainstorms/2026-04-21-unified-eval-harness-requirements.md)
- Ideation seed: [docs/ideation/2026-04-21-recommendation-model-ideation.md](../ideation/2026-04-21-recommendation-model-ideation.md) Survivor 1
- Guardrail: `memory/feedback_audit_every_change.md`
- Supporting memory: `memory/feedback_audit_metric_too_coarse.md` (motivates the histogram verdict)
- Related code:
  - `src/mtg_synergy_graph/universal_scorer.py` (UniversalScore)
  - `src/mtg_synergy_graph/complement_rules/core.py:1183-1202` (find_all_complements)
  - `scripts/_audit_rule_impact.py:16-31` (verdict rubric to preserve compat with)
  - `src/mtg_synergy_graph/schema.sql` (extension point)
  - `.pre-commit-config.yaml` (hook extension point)
