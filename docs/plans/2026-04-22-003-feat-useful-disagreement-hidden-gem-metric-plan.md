---
title: "feat: Useful-disagreement objective — hidden_gem_hit_rate metric + warning, tracking-only at MVP"
type: feat
status: active
date: 2026-04-22
origin: docs/brainstorms/2026-04-21-useful-disagreement-requirements.md
---

# feat: Useful-disagreement — `hidden_gem_hit_rate`, tracking-only

## Overview

Add a second evaluation axis to `bench.py audit`: `hidden_gem_hit_rate`,
the fraction of our top-30 that both (a) EDHREC's top-30 synergy list
omitted and (b) our own rule tensor rates mechanically plausible. Track
it alongside the existing score-delta + histogram verdict, without
gating commits on it. First rollout is advisory: the audit prints the
aggregate + delta vs the pin, warns when the delta drops below
`_HIDDEN_GEM_WARN_THRESHOLD = 0.02`, and offers `--inspect-gems` for
per-commander diagnosis and `--trend hidden_gems` for longitudinal
review. Gating stays on NDCG@30 (via the existing histogram-based
verdict); promotion to a commit-gate is deferred and requires a future
brainstorm per FR6.

## Problem Frame

The project's stated intent — recorded three times in auto-memory
(`feedback_edhrec_not_goal`, `feedback_edhrec_hivemind`,
`feedback_edhrec_synergy_formula`) — is to find mechanically-plausible
"hidden gems" that EDHREC missed. EDHREC is a sanity check, not the
goal. But every acceptance gate today (pre-commit `bench.py audit`,
`docs/RULE_HISTORY.md` verdicts) grades rules by score deltas /
NDCG-proxy histogram buckets against EDHREC. A rule that lifts scoring
purely by concentrating lift on EDHREC top-10 staples scores
"POSITIVE" even though it moves us further from the stated goal.

Survivor 3 (IDF reforms) is a concrete instance: its BM25F variant
nudged aggregate NDCG and was abandoned largely because its per-
commander effects looked like popularity-chasing. We lacked a metric
that made that instinct measurable — the call was made by inspection,
not by a number. This plan builds that number.

(see origin: docs/brainstorms/2026-04-21-useful-disagreement-requirements.md)

## Requirements Trace

- **R1** (FR1) — Define and compute `hidden_gem_hit_rate(cmdr)` =
  `|plausible_hidden(cmdr)| / 30` where
  `plausible_hidden = (our_top_30 \ edhrec_top_30)` filtered by the
  mechanical-plausibility gate. Aggregate = mean over commanders with
  EDHREC data.
- **R2** (FR2) — Plausibility gate, purely mechanical:
  `N_rules_firing(cmdr, c) >= 2` OR
  `total_rule_contribution(cmdr, c) > median_contribution(cmdr)`.
  Reads from the persisted `rule_contributions` tensor under the live
  `config_hash` — no new data sources, no learned classifier.
- **R3** (FR3) — `bench.py audit` prints the aggregate metric + delta
  vs pinned baseline on every run. `--trend hidden_gems` emits CSV
  history (decision resolved in planning — no sparkline at MVP).
- **R4** (FR4) — Warning on aggregate delta < `-0.02` printed to
  stderr; commit still proceeds (tracking-only gate).
- **R5** (FR5) — `--inspect-gems` diagnostic: per-commander lost/gained
  hidden-gems table, respects `--commander NAME` filter.
- **R6** (FR6) — Escalation path documented in-code (near the
  threshold constant) and in CLAUDE.md: ≥20 commits tracked, human-
  correlation check, <10% false-positive rate before promotion to a
  commit-gate is proposed as a separate brainstorm.
- **R7** (Success criterion 1) — Metric is deterministic: three
  consecutive audit runs on an unchanged tree produce identical
  values.
- **R8** (Success criterion 2) — Baseline value pinned in
  `tests/fixtures/golden_set_run.json` (alongside existing `scores`).
- **R9** (Success criterion 5) — Explainability: `--inspect-gems`
  output names specific gained/lost cards per commander.

## Scope Boundaries

- No replacing NDCG / histogram verdict with this metric (non-goal 1).
- No gating commits on `hidden_gem_hit_rate` regressions (non-goal 2).
- No learned plausibility classifier, no text heuristics (non-goal 3).
- No pairwise-Jaccard diversity metric (non-goal 4).
- No move to the 2,761-commander pool (Survivor 1 scope; non-goal 5).
- Commander pool is the same 100-commander golden set the pinned
  fixture already uses — no second set at MVP.
- No promotion to a gate — FR6 promotion is itself a future brainstorm.

### Deferred to Separate Tasks

- **Embedding-based plausibility clause** — FR2 + Open Question 1:
  tighten plausibility with a commander-target cosine ≥ τ once
  Survivor 6 (content embeddings) lands. Separate plan.
- **Commander-popularity weighting** — Open Question 2 resolved as
  "equal weight at MVP." Revisit only if the metric proves useful.
- **EDHREC-agreement delta companion metric** — Open Question 6
  resolved as "skip at MVP" (hi_syn_gain / hi_syn_loss histogram
  buckets already capture this).
- **Explicit aggregate NDCG@30 line in audit output** — brainstorm
  FR3 example shows it, but the current histogram verdict already
  captures NDCG-direction signal. Adding explicit NDCG reporting is
  a separate (low-risk) follow-up; this plan keeps NDCG implicit to
  stay focused.
- **Retrospective replay harness** — user-chosen manual one-off;
  documented under Success Criteria section 3 for post-landing
  validation.

## Context & Research

### Relevant Code and Patterns

- `src/mtg_synergy_graph/bench/fixture.py` — `FixtureEntry`, `legacy`
  dict (already carries `ndcg30`, `hi_syn_hits`, `edhrec_top10`),
  `build_fixture(conn, commanders, existing, tensor_writer)`. This is
  the host for the new per-commander metric.
- `src/mtg_synergy_graph/bench/report.py` — `AuditReport`,
  `CommanderDelta`, `build_report(pinned, live)`, `_render_markdown`,
  `_as_json_dict`. Pattern for adding aggregate numbers + markdown
  rows.
- `src/mtg_synergy_graph/bench/audit.py` — `handle_audit`,
  `_print_summary`, `_write_default_output` (writes `.audit/last.md`).
  Same module will own the `.audit/history.csv` append.
- `src/mtg_synergy_graph/bench/handlers.py` — pattern for new CLI
  handlers. See `handle_inspect`, `handle_unknowns`.
- `src/mtg_synergy_graph/bench/cli.py` — mutually-exclusive mode group
  at `audit.add_mutually_exclusive_group()`. New flags slot in here.
- `src/mtg_synergy_graph/validate.py` — `_fetch_edhrec_sections`,
  `commander_to_slug`, `edhrec_labels_for_commander`. Reused for
  EDHREC top-30 fetch.
- `src/mtg_synergy_graph/bench/tensor.py` — `rule_contributions`
  table schema + `compute_config_hash`. The tensor already has every
  `(cmdr, cand, rule)` contribution row; the plausibility gate is a
  SQL `GROUP BY` over that table.

### Institutional Learnings

- `memory/feedback_edhrec_not_goal.md`,
  `memory/feedback_edhrec_hivemind.md`,
  `memory/feedback_edhrec_synergy_formula.md` — the stated intent
  this plan operationalizes.
- `memory/feedback_audit_every_change.md` — existing guardrail:
  every scoring-path change runs `bench.py audit`. The new metric
  rides that rail; it does not introduce a new hook.
- `memory/feedback_strategy_mapping.md`,
  `memory/project_reanimator_hisyn_gap.md` — reverted experiments
  that would be candidate retrospective validation subjects
  post-landing.

### External References

None. This is a local metric built on data we already have; no
external docs or prior art needed.

## Key Technical Decisions

- **Metric lives in a new module** (`bench/hidden_gems.py`), not bolted
  onto `report.py` or `fixture.py`. Small pure-function surface
  (plausibility + aggregate) is easier to test and reason about than
  an entanglement with the existing report builder.
- **Persist per-commander metric + hidden-gem list in
  `FixtureEntry.legacy`** rather than as first-class fields. Keeps
  `SCHEMA_VERSION` unchanged — load tolerates missing keys, repin adds
  them. No migration for existing fixtures.
- **Recompute at `build_fixture` time**, not at audit comparison. The
  computation reads the persisted tensor + EDHREC DB; both are
  available during `build_fixture`. Audit comparison stays a pure diff
  of pinned vs live entries.
- **Skip commanders without EDHREC data** (planning answer to Open
  Question 3). Aggregate = mean over commanders *with* EDHREC. Audit
  output reports `hidden_gem_hit_rate : 0.1467 (Δ -0.0034, 97 cmdrs)`
  so skip count is visible.
- **CSV-only trend output** (planning answer to Open Question 5). One
  row appended to `.audit/history.csv` per `bench.py audit` run.
  `--trend hidden_gems` prints the last N rows; no sparkline, no
  matplotlib dep.
- **History is append-only, untracked by git.** `.audit/` is already
  gitignored by pre-commit convention (writes `.audit/last.md`).
  `.audit/history.csv` lives there too. Regeneration on a fresh
  checkout means an empty trend until the first run — acceptable
  tradeoff vs carrying volatile data in git.
- **Threshold is a module-level constant** (`_HIDDEN_GEM_WARN_THRESHOLD
  = 0.02` in `bench/hidden_gems.py`), commented with the FR6
  escalation criteria. Future tuning is a one-line edit + audit run.
- **Warning is stderr, not stdout.** Mirrors the existing
  `_print_summary` pattern. `.audit/last.md` still captures the
  aggregate + delta for review.
- **Determinism via the tensor's `config_hash`.** The persisted tensor
  is deterministic under unchanged scoring config. Since the metric
  reads from the tensor + static EDHREC DB, the metric inherits
  determinism automatically. Test Unit 1 verifies this explicitly.

## Open Questions

### Resolved During Planning

- **Q1** (origin OQ #1): Plausibility expansion with embeddings after
  Survivor 6 lands → deferred to a separate plan (see Deferred to
  Separate Tasks).
- **Q2** (origin OQ #2): Commander-popularity weighting → "Likely OK
  at MVP"; equal weight confirmed (see Deferred to Separate Tasks).
- **Q3** (origin OQ #3): Commanders without EDHREC data → **Skip**,
  report skip count (decided via AskUserQuestion).
- **Q4** (origin OQ #4): "High synergy" vs "all top N" → use the
  existing "High Synergy Cards" section (origin doc's current answer;
  matches the NDCG reference set via the `synergy` sort order).
- **Q5** (origin OQ #5): Trend rendering → **CSV only** (decided via
  AskUserQuestion).
- **Q6** (origin OQ #6): EDHREC-agreement delta companion → skip at
  MVP (see Deferred to Separate Tasks).
- **Q7** (success criterion 3 validation): Replay harness vs manual →
  **Manual one-off after landing** (decided via AskUserQuestion).

### Deferred to Implementation

- Exact SQL vs Python shape for the plausibility gate (a `GROUP BY`
  on `rule_contributions` vs an in-memory aggregation over tensor
  rows returned by `build_fixture`'s sink). Will fall out naturally
  when writing Unit 1 — whichever is faster on a 100-commander pass.
- Whether `--inspect-gems` should default to Markdown or one-table-
  per-commander format. Depends on what the output looks like on real
  data. Pick at implementation time.
- Whether the `.audit/history.csv` write should be gated behind a
  success (identity OK) or every run. Default to every run; revisit
  if trend gets polluted by runs with obvious errors.

## Implementation Units

- [ ] **Unit 1: Hidden-gem metric core — plausibility gate + aggregate**

**Goal:** Pure functions that compute `plausibility(cmdr, cand,
contributions)` and `hidden_gem_hit_rate(our_top_30, edhrec_top_30,
contributions)` for a single commander. No DB side effects, no CLI
coupling.

**Requirements:** R1, R2, R7.

**Dependencies:** None (the `rule_contributions` tensor schema +
tensor writer already exist on main).

**Files:**
- Create: `src/mtg_synergy_graph/bench/hidden_gems.py`
- Test: `tests/bench/test_hidden_gems_core.py`

**Approach:**
- Module exposes: `_HIDDEN_GEM_WARN_THRESHOLD = 0.02` (with a
  docstring-commented FR6 escalation path),
  `HiddenGemReport` dataclass
  (frozen, `aggregate: float | None`, `per_commander: dict[str,
  HiddenGemEntry]`, `skipped_commanders: tuple[str, ...]`,
  `HiddenGemEntry(commander, rate, hidden_cards: tuple[str, ...])`),
  `plausibility(cmdr, cand, rows)` returning bool,
  `hidden_gem_hit_rate_for_commander(our_top_30, edhrec_top_30,
  contributions)` returning `HiddenGemEntry | None`,
  `aggregate_hidden_gem_hit_rate(entries)` returning mean.
- `contributions` input is an iterable of tensor rows
  `(candidate, rule_id, contribution)` for one commander — caller
  shapes it. Module stays DB-agnostic.
- Plausibility: count rules with `contribution > 0` (the `>= 2` leg)
  OR sum of contributions > per-commander median — with edge case of
  "one non-zero candidate" median = that one value itself; short-
  circuit when median is 0 (use the N_rules leg only).
- `our_top_30 \ edhrec_top_30` = set subtraction on card names.
- Return `None` aggregate when entries is empty; audit report prints
  `hidden_gem_hit_rate : — (no commanders with EDHREC data)`.

**Execution note:** Test-first. Metric definition is a pure function
and the plausibility formula is subtle — write the failing tests for
each formula branch before the implementation.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/histogram.py` — dataclass shape,
  pure-function aggregation, `Bucket` enum style.
- `src/mtg_synergy_graph/bench/collinearity.py` — how another pure
  numeric module exposes its surface (dataclass + compute function).

**Test scenarios:**
- Happy path — Commander has 5 cards in our top-30, EDHREC covers 3
  of them, remaining 2 each have `N_rules = 3` firing → plausibility
  True for both → `rate = 2/30 ≈ 0.0667`.
- Happy path — Commander has 1 hidden card with `N_rules = 1` but
  contribution > per-commander median → plausibility True via the OR
  leg → `rate = 1/30`.
- Edge case — Commander's hidden set is empty (EDHREC covers all of
  our top-30) → `rate = 0.0`, entry exists with empty `hidden_cards`.
- Edge case — Commander has no EDHREC top-30 data at all (caller
  passes `edhrec_top_30=None`) → function returns `None`; aggregator
  records commander under `skipped_commanders`.
- Edge case — Commander has exactly one candidate in our top-30 with
  any contribution → median-based branch short-circuits cleanly; no
  ZeroDivisionError.
- Edge case — All tensor contributions equal zero → N_rules_firing
  requires `> 0`; falls through to median branch which is also 0;
  plausibility False for every candidate.
- Error path — `our_top_30` has duplicates → `ValueError("our_top_30
  must be unique")` before computation.
- Integration — Running the same inputs twice returns identical
  floats (determinism for R7).

**Verification:**
- Pytest green on the new test file.
- Running the function twice on identical inputs yields identical
  dataclass instances (deep equality).
- Module exports and constants match what Units 2-5 import against.

- [ ] **Unit 2: Fixture integration — compute metric at `build_fixture`, persist in legacy dict**

**Goal:** `build_fixture` opens the EDHREC DB, computes per-commander
`hidden_gem_hit_rate` + `hidden_cards` while the tensor sink is
writing rows, and stores the results in `FixtureEntry.legacy` under
new keys so the pinned JSON carries the baseline.

**Requirements:** R1, R8.

**Dependencies:** Unit 1.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/fixture.py`
- Modify: `tests/bench/test_fixture.py` (or create if absent — check
  existing pattern)
- Test: `tests/bench/test_fixture_hidden_gems.py` (new)

**Approach:**
- Extend `build_fixture(conn, commanders, existing=None,
  tensor_writer=None, edhrec_conn=None)`. `edhrec_conn` defaults to
  `None`; callers that want the metric pass one.
- Inside the per-commander loop, collect tensor rows for that
  commander (the tensor writer already batches; extract via
  `score_commander`'s returned `_rows` tuple, currently discarded)
  and fetch EDHREC top-30 via `_fetch_edhrec_sections` + sort by
  synergy desc.
- Call `hidden_gem_hit_rate_for_commander` from Unit 1. Store
  `legacy["hidden_gem_hit_rate"] = entry.rate`,
  `legacy["hidden_cards"] = list(entry.hidden_cards)`.
- When `edhrec_conn is None` or EDHREC has no rows: skip — leave the
  legacy keys unset (not `None`, not `0`). Unit 3's aggregator filters
  on key presence. This way old fixtures without the keys behave the
  same as skipped commanders.
- EDHREC top-30 derivation: query
  `SELECT card_name FROM edhrec_card_synergy WHERE commander_slug = ?
  AND section = 'High Synergy Cards' ORDER BY synergy DESC LIMIT 30`.
  Same shape as `validate.py:_run_one` but at top-30 instead of
  top-10.
- `handle_repin` opens the EDHREC DB (path from `--edhrec-db` if
  added, otherwise env var, otherwise reuse existing discovery
  pattern from `validate.py`) and passes it to `build_fixture`.

**Patterns to follow:**
- `src/mtg_synergy_graph/validate.py:_run_one` — the working pattern
  for opening EDHREC, fetching sections, extracting top-N by synergy.
- `src/mtg_synergy_graph/bench/fixture.py:score_commander` — returned
  `_rows` gives us the tensor rows without re-scoring; use them.

**Test scenarios:**
- Happy path — `build_fixture` with EDHREC conn populates
  `legacy["hidden_gem_hit_rate"]` on every commander with EDHREC
  data. A fixture built without EDHREC conn produces entries with
  the legacy dict empty of gem keys (backwards compatible).
- Edge case — Commander with no rows in `edhrec_card_synergy` →
  `legacy["hidden_gem_hit_rate"]` key absent; aggregator skips.
- Edge case — EDHREC section exists but has 0 rows in "High Synergy
  Cards" → EDHREC top-30 is empty set → every our-top-30 card is
  "hidden"; plausibility gate decides.
- Integration — Build fixture once, reload from disk, compare: values
  round-trip through the legacy dict intact. Covers R8 (pinned
  baseline).
- Integration — Running `build_fixture` twice on the same DB + same
  commanders produces byte-identical `hidden_gem_hit_rate` floats
  (determinism for R7).

**Verification:**
- Running `bench.py audit --repin --yes` on the 100-commander golden
  set produces a fixture with gem fields populated on the
  EDHREC-covered commanders.
- `bench.py audit --expect-identity` on the new fixture stays PASS
  (identity check is over `scores` dict, gem fields don't affect it).

- [ ] **Unit 3: Audit report integration — aggregate, delta, warning**

**Goal:** `bench.py audit` computes aggregate `hidden_gem_hit_rate`
(live) and delta vs pinned; renders one line in Markdown + JSON
output + `_print_summary`; prints an FR4 stderr warning when
`delta < -_HIDDEN_GEM_WARN_THRESHOLD`.

**Requirements:** R3, R4.

**Dependencies:** Unit 2.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/report.py`
- Modify: `src/mtg_synergy_graph/bench/audit.py`
- Test: `tests/bench/test_report_hidden_gems.py` (new)
- Test: `tests/bench/test_audit_warning.py` (new)

**Approach:**
- `AuditReport` gains: `aggregate_hidden_gem_hit_rate: float | None`,
  `pinned_hidden_gem_hit_rate: float | None`,
  `hidden_gem_hit_rate_delta: float | None`,
  `hidden_gem_warning: bool`,
  `commanders_with_edhrec: int`.
- Aggregation helper in `report.py` reads `legacy["hidden_gem_hit_rate"]`
  across entries, skips missing keys, returns mean + count. Same for
  pinned side; delta is live − pinned (both `None` → delta `None`).
- `hidden_gem_warning = (delta is not None and delta <
  -_HIDDEN_GEM_WARN_THRESHOLD)`.
- `_render_markdown` appends after the existing `**Aggregate score Δ:**`
  line: `**hidden_gem_hit_rate:** \`0.1467 (Δ -0.0034, 97 cmdrs with
  EDHREC)\``. Emits `—` when `None`.
- `_as_json_dict` adds four keys: `aggregate_hidden_gem_hit_rate`,
  `hidden_gem_hit_rate_delta`, `hidden_gem_warning`,
  `commanders_with_edhrec`.
- `_print_summary` in audit.py appends
  `gem_Δ=-0.0034 WARN` (or `gem_Δ=0.0001`) to the single-line summary.
- FR4 warning printed separately to stderr when `hidden_gem_warning`:
  ```
  ⚠ hidden_gem_hit_rate dropped 0.034 on this change (from 0.147 to 0.113).
    Inspect: `bench.py audit --inspect-gems` to see which gems were lost.
  ```
  Printed before `_print_summary` so it's the first thing the user
  sees.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/report.py:_render_markdown` — how
  other aggregate lines are formatted.
- `src/mtg_synergy_graph/bench/audit.py:_print_summary` — the
  single-line stderr pattern.

**Test scenarios:**
- Happy path — Pinned + live both have gem data → aggregate, delta
  computed, markdown contains the new line with the expected number.
- Happy path — Pinned + live both clean → `hidden_gem_warning` False;
  no stderr warning printed.
- Edge case — Delta = -0.019 (just above threshold) → no warning
  (strictly less-than).
- Edge case — Delta = -0.02 exactly → no warning (strict inequality).
- Edge case — Delta = -0.021 → warning printed; exact wording matches
  the FR4 spec.
- Edge case — Live has gem data but pinned does not (old pin) →
  delta = `None`; markdown shows `Δ —`; no warning.
- Edge case — No commanders have EDHREC data → aggregate = `None`;
  markdown shows `—`; `commanders_with_edhrec = 0`.
- Integration — `handle_audit` writes the same content to
  `.audit/last.md` and stdout; the warning fires exactly once (not
  twice because of the two sinks).
- Integration — JSON output is valid (parseable) and contains all
  four new keys.

**Verification:**
- `bench.py audit` on the 100-cmdr set shows the new line.
- When the live tree is unchanged, delta = 0.0 and no warning fires.
- Running with `--format json` produces valid JSON containing the
  four new keys.

- [ ] **Unit 4: `--inspect-gems` CLI handler**

**Goal:** New mode `bench.py audit --inspect-gems` that produces a
per-commander table of `(commander, Δ, lost_gems, gained_gems)` by
diffing pinned vs live `hidden_cards` sets. Respects `--commander`
filter, `--format md/json`, `--output`.

**Requirements:** R5, R9.

**Dependencies:** Unit 3.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cli.py` (add mode flag,
  register handler)
- Modify: `src/mtg_synergy_graph/bench/handlers.py` (new
  `handle_inspect_gems`)
- Modify: `src/mtg_synergy_graph/bench/__init__.py` (register)
- Test: `tests/bench/test_handle_inspect_gems.py` (new)

**Approach:**
- Add `--inspect-gems` to the existing mutex mode group in `cli.py`
  (sibling of `--inspect`, `--collinearity`, `--unknowns`).
- `handle_inspect_gems(args)` loads pinned fixture (from `--fixture`)
  and re-scores live via `build_fixture` to get live gem data.
- Row = `(commander, rate_delta, lost: tuple[str, ...], gained:
  tuple[str, ...])`. `lost = pinned.hidden_cards - live.hidden_cards`,
  `gained = live.hidden_cards - pinned.hidden_cards`.
- Sort by `abs(rate_delta)` desc; truncate by `--limit` (default 50).
- Markdown output is a table matching FR5 shape:
  ```
  | commander | Δ | lost gems | gained gems |
  |-----------|--:|-----------|-------------|
  ```
  JSON output is a list of the row dataclass + aggregate summary on
  top.
- `--commander NAME` filter runs before sort and truncation.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/handlers.py:handle_inspect` — argparse
  extraction + `--format md/json` branch.
- `src/mtg_synergy_graph/bench/handlers.py:handle_unknowns` — the
  most recently-added handler; same skeleton shape.

**Test scenarios:**
- Happy path — Synthetic pinned + live with known deltas → output
  lists commanders in correct order; lost / gained cards match.
- Edge case — Empty pinned (no gem data) → handler exits 0 with
  "no baseline gems to compare" message.
- Edge case — `--commander "NameNotInFixture"` → handler exits 2 with
  actionable error.
- Edge case — `--limit 0` → empty table rendered (no rows).
- Error path — Fixture missing → exits 2 with the same message as
  `handle_audit`.
- Integration — CLI dispatch routes `--inspect-gems` through the
  `register()` table (matches the pattern other modes use).
- Integration — `--format json` output parseable and contains both
  per-row data and aggregate delta.

**Verification:**
- `bench.py audit --inspect-gems` on a mutated tree (scored once,
  then a weight tweaked) shows non-zero rows and the lost/gained
  cards are believable on manual inspection.
- `bench.py audit --inspect-gems --commander "Korvold, Fae-Cursed King"`
  emits one row or a "no delta" line.

- [ ] **Unit 5: `.audit/history.csv` persistence + `--trend hidden_gems` handler**

**Goal:** Every successful `bench.py audit` run appends one row to
`.audit/history.csv`; new `--trend hidden_gems` mode reads the file
and prints the last N entries as CSV (default N=20).

**Requirements:** R3 (the `--trend` subcommand portion).

**Dependencies:** Unit 3 (needs `hidden_gem_hit_rate_delta` to
already be in `AuditReport`).

**Files:**
- Modify: `src/mtg_synergy_graph/bench/audit.py` (append to history)
- Create: `src/mtg_synergy_graph/bench/history.py` (reader + writer)
- Modify: `src/mtg_synergy_graph/bench/cli.py` (add `--trend` flag)
- Modify: `src/mtg_synergy_graph/bench/handlers.py` (new
  `handle_trend_hidden_gems`)
- Modify: `src/mtg_synergy_graph/bench/__init__.py` (register)
- Test: `tests/bench/test_history.py` (new)
- Test: `tests/bench/test_handle_trend_hidden_gems.py` (new)

**Approach:**
- CSV schema:
  ```
  timestamp,commit_sha,config_hash,aggregate_score_delta,
  hidden_gem_hit_rate,hidden_gem_hit_rate_delta,
  commanders_compared,commanders_with_edhrec,verdict
  ```
  Header written on first run; subsequent runs append only.
- `history.py` exposes: `append_run(report, path=".audit/history.csv")`,
  `read_last(n, path)` returning dataclass rows. `commit_sha` read
  via `git rev-parse HEAD`; on failure (not a git checkout), write
  an empty string so the row shape is stable.
- `audit.py` calls `append_run(report)` after
  `_write_default_output`; failure to write history logs a warning
  to stderr but does NOT fail the audit (the primary output is the
  rendered report).
- `--trend hidden_gems` flag in the mutex mode group, takes optional
  `--trend-n` (default 20). Handler reads the last N rows + prints
  either CSV (default) or a Markdown table if `--format md`.
- Rows omit the header when `--format md` is chosen; full CSV
  (including header) when `--format csv` (or default). JSON format
  also supported for symmetry with the other handlers.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/audit.py:_write_default_output` — the
  `.audit/` directory convention.
- Standard-library `csv` module (no third-party dep).

**Test scenarios:**
- Happy path — `append_run` on an empty `.audit/` creates the file
  with header + one data row. Second append writes only the row.
- Happy path — `read_last(3)` on a 10-row file returns the 3 newest.
- Edge case — Row written outside a git checkout → `commit_sha` is
  empty, row still written.
- Edge case — `commanders_with_edhrec = 0` → `hidden_gem_hit_rate`
  column is empty string, not a spurious 0.
- Edge case — `--trend-n` larger than the file length → returns all
  rows; does not pad.
- Error path — Corrupted CSV (malformed line) → `read_last` skips
  the bad row with a stderr warning and returns the rest.
- Error path — `--trend hidden_gems` on a fresh checkout with no
  `.audit/history.csv` → exits 0 with "no history yet — run
  `bench.py audit` first" message.
- Integration — `bench.py audit` followed by `bench.py audit --trend
  hidden_gems` shows the just-appended row.

**Verification:**
- Running `bench.py audit` three times on the same tree produces three
  CSV rows with identical `hidden_gem_hit_rate` (covers R7
  determinism).
- `bench.py audit --trend hidden_gems --trend-n 5` prints the last
  five rows.

- [ ] **Unit 6: Documentation, escalation-path comment, CLAUDE.md**

**Goal:** Document the metric, the warning threshold, the escalation
path, and the memory-note connection. No code changes beyond a
prominent in-module docstring block.

**Requirements:** R6.

**Dependencies:** Units 1-5 landed.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/hidden_gems.py` (prominent
  docstring block near `_HIDDEN_GEM_WARN_THRESHOLD`)
- Modify: `CLAUDE.md` (new subsection under Scoring Architecture or
  adjacent to `bench.py` docs)
- Modify: `docs/RULE_HISTORY.md` (add landing entry under 2026-04-22)
- Create: `memory/feedback_hidden_gem_metric.md` (new memory note)
- Modify: `memory/MEMORY.md` (add the reference line)

**Approach:**
- Docstring block lists FR6 promotion criteria:
  1. Tracked for ≥ 20 commits.
  2. Human-confirmed that metric drops correlate with subjectively-
     bad scoring changes.
  3. False-positive rate ≤ 10% (at most 1 in 10 recent accepted
     commits triggered a spurious warning).
  4. Promotion is itself a new `ce-brainstorm` + `ce-plan` cycle —
     not a silent config change.
- CLAUDE.md addition: one paragraph under "Scoring Architecture" (or
  near the `bench.py audit` command table) introducing the metric +
  its tracking-only status + pointer to the docstring for the
  escalation path.
- `docs/RULE_HISTORY.md` entry documents the landing: purpose,
  threshold, link to brainstorm + plan.
- Memory note `feedback_hidden_gem_metric.md` — captures: "we now
  track hidden_gem_hit_rate alongside NDCG; metric is advisory at
  MVP; escalation path exists but requires a separate brainstorm."
  Same shape as existing feedback notes.

**Test expectation:** none — pure documentation changes.

**Verification:**
- `grep -q _HIDDEN_GEM_WARN_THRESHOLD` in `hidden_gems.py` returns a
  docstring block with the four promotion criteria.
- `CLAUDE.md` rendering shows the new subsection.
- `memory/MEMORY.md` has the new reference line.

## System-Wide Impact

- **Interaction graph:** `build_fixture` gains a new dependency on
  the EDHREC DB connection when gem metric is desired. Callers that
  pass `None` get the old behavior (no gem fields). Existing
  `bench.py audit --expect-identity` is unaffected — identity check
  is over `scores` dict only.
- **Error propagation:** A missing or malformed EDHREC DB should not
  crash `build_fixture` — it should skip the metric and log. Warning
  failures in history write do not break audits.
- **State lifecycle risks:** `.audit/history.csv` grows unbounded
  over time. Acceptable at MVP (one row per audit, ~200 bytes,
  1000 runs ≈ 200KB). Pruning is a future follow-up if it becomes a
  problem.
- **API surface parity:** `--trend`, `--inspect-gems` are new mode
  flags in the audit subcommand. They slot into the existing mutex
  group; no new subcommand.
- **Integration coverage:** Unit 2 and Unit 3 tests exercise
  `build_fixture` → `build_report` → markdown/JSON output with real
  EDHREC data (not mocked) to ensure the pipeline works end-to-end.
- **Unchanged invariants:**
  - Pinned fixture schema version (no bump — additive via `legacy`
    dict).
  - `rule_contributions` tensor schema (read-only from here).
  - `bench.py audit --expect-identity` semantics (gem fields don't
    affect score identity).
  - NDCG@30 histogram buckets and verdict rollup (untouched; stays
    the primary commit gate).
  - Pre-commit `bench-audit` hook (no threshold change; FR4 warning
    does not fail the hook because warnings print to stderr and the
    hook's exit code is from `bench.py audit` itself).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Metric is noisy — small scoring changes produce large gem-rate swings, triggering warning fatigue. | Threshold starts permissive (0.02). FR6 escalation path requires validation before any gating. If noise proves unacceptable, raise threshold or redefine plausibility — both are one-liners. |
| Plausibility gate is too loose — flags random cards as "gems." | Tests Unit 1 + post-landing manual retrospective (Success Criterion 3) validate the gate on real data. If loose, tighten `N_rules_firing >= 3` or scale median threshold. |
| Plausibility gate is too tight — zero hidden gems for every commander. | Aggregate of 0 is valid and surfaces the issue immediately. Same mitigation as above, reversed. |
| EDHREC DB path discovery inconsistent between `validate.py` and `build_fixture`. | Unit 2 reuses the same discovery pattern (pass `edhrec_conn` explicitly); no new path-guessing code. |
| `.audit/history.csv` data is lost on fresh checkouts. | Documented as acceptable at MVP; file is regenerable on future runs. Git-tracking the file is rejected — would pollute PRs and be noisy in code review. |
| Metric tied to a specific `config_hash`; a repin changes baseline in a way hidden from humans. | `--trend hidden_gems` shows `config_hash` per row so the user can see when the baseline shifted. |

## Documentation / Operational Notes

- Update `CLAUDE.md` Common Commands section with the two new
  subcommands (`--inspect-gems`, `--trend hidden_gems`).
- `.audit/history.csv` is automatically gitignored via the existing
  `.audit/` ignore.
- No new env vars, no new config file.
- Memory note added per Unit 6.

## Success Criteria

1. **Deterministic metric** — three runs of `bench.py audit` on an
   unchanged tree produce identical `hidden_gem_hit_rate` values.
   Verified by Unit 1 + Unit 2 tests.
2. **Baseline pinned** — after `bench.py audit --repin --yes`, the
   fixture JSON contains `legacy.hidden_gem_hit_rate` for every
   commander with EDHREC data. Verified by Unit 2 integration test.
3. **Retrospective discrimination (manual, post-landing)** — check
   out one of these previously-reverted experiments and run `bench.py
   audit`, confirming `hidden_gem_hit_rate_delta` is ≤ 0:
   - 2026-04-18 broad `gy_retrieval` rule (reverted; reanimator
     hi-syn gap — see `memory/project_reanimator_hisyn_gap.md`)
   - The deck-hint-match experiment (reverted; see
     `memory/feedback_edhrec_not_goal.md` context)
   - Survivor 3's BM25F feat branch (`feat/idf-reforms-bm25f-
     conditional`)
   At least one of the three should retrospectively flag non-
   positively. Document the finding in a follow-up memory note.
4. **Low false-positive rate** — Over 10 recent accepted commits
   replayed via `--trend hidden_gems`, at most 1 triggers a spurious
   warning (delta < -0.02 despite the commit being accepted). If more
   than 1, tune the threshold before promotion.
5. **Explainability** — `bench.py audit --inspect-gems --commander
   "Korvold, Fae-Cursed King"` emits a human-readable lost/gained
   list. A human can sanity-check at least 3 commanders this way.

## Alternative Approaches Considered

- **Gate commits on `hidden_gem_hit_rate` at MVP.** Rejected by
  origin-doc non-goal 2 — the metric isn't validated yet, gating
  prematurely would block legitimate changes.
- **Replace NDCG with hidden-gems.** Rejected by origin-doc non-goal
  1 — paradigm shift is too risky without validation.
- **Learned plausibility classifier (semantic similarity, learned
  weights).** Rejected by origin-doc non-goal 3. Mechanical-only
  gate keeps the project's no-ML discipline intact.
- **Make EDHREC-agreement a first-class metric alongside.** Skipped:
  the existing `hi_syn_gain` / `hi_syn_loss` histogram buckets
  already capture that signal; adding a parallel metric bloats the
  report without new information.
- **Store history in SQLite instead of CSV.** Rejected for MVP: CSV
  is git-diffable if a user wants to commit a snapshot, human-
  readable, and needs no schema. Revisit if history-query complexity
  grows.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-21-useful-disagreement-requirements.md](../brainstorms/2026-04-21-useful-disagreement-requirements.md)
- **Prerequisite plan:** [docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md](2026-04-22-001-feat-unified-eval-harness-plan.md) — rule-contribution tensor, `bench.py` CLI surface.
- **Related plan:** [docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md](2026-04-22-002-feat-typed-port-graph-substrate-plan.md) — just landed; orthogonal but tensor-consuming.
- **Memory alignment:** `memory/feedback_edhrec_not_goal.md`, `memory/feedback_edhrec_hivemind.md`, `memory/feedback_edhrec_synergy_formula.md`, `memory/feedback_audit_every_change.md`.
- **Memory context for retrospective validation:** `memory/project_reanimator_hisyn_gap.md`, `memory/feedback_strategy_mapping.md`.
