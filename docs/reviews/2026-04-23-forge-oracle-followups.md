# Forge-Second-Oracle — code-review deferrals

Follow-up items from the `/ce-code-review` pass on `feat/forge-oracle-phase-1`
(2026-04-23). The 10 always-on + 6 conditional reviewers produced ~40
findings; the LFG pass applied all `safe_auto` fixes inline. The items
below are the `gated_auto` and `manual` findings that need a deliberate
decision before they can land, plus the `advisory` items that stay in
the report rather than becoming work.

Run artifact: `.context/compound-engineering/ce-code-review/20260423-160807-90185045/`.

---

## Gated-auto (concrete fix exists but changes behavior)

### P1

#### G1. PPMI marginal normalization bug (ppmi.py)

- **Finding:** correctness COR-002 + adversarial ADV-006, 2-reviewer agreement, confidence 0.87.
- **Issue:** `marginal_weighted[sig]` accumulates over every deck with size >= 1, but the denominator `n_contributing_decks` counts only decks with size >= 2. When a subkind appears frequently in single-subkind decks, `p_a = marginal_weighted[sig] / n_contributing_decks` can exceed 1.0, inflating the denominator in the PMI calculation and systematically driving PPMI toward 0 for the most-frequent signatures.
- **Impact on real corpus:** uncertain until empirically checked. The live 667-deck corpus has a few hundred single-subkind deck variants. Worst case: high-frequency subkinds (basic mana rocks, common cost.tap patterns) are silently suppressed from `forge_signal`, de-prioritizing exactly the subkinds most frequently curated by Forge designers.
- **Concrete fix:** either (a) count decks with size >= 1 in the marginal denominator (use total contributing decks), or (b) track `n_marginal` separately from `n_contributing_decks`. Option (b) preserves the current pair-normalization semantics and changes only the marginals.
- **Why gated:** changes the numerical output of every PPMI row and therefore every `forge_signal`. Must be audited against the live corpus before landing — a rebuild with the fix and a diff of `--vs-forge-oracle` τ values will show whether real rankings change meaningfully.
- **Verification needed:** rebuild forge_oracle.db with the fix; re-run `bench.py audit --vs-forge-oracle`; compare aggregate τ + top-10 divergences.

#### G2. Concurrent build race on deterministic `.tmp` path (ingest.py)

- **Finding:** adversarial ADV-001, confidence 0.88.
- **Issue:** `forge_oracle.py build` computes `tmp_path = target_db_path.with_suffix(target_db_path.suffix + ".tmp")` — deterministic. Two concurrent builds would open the same temp file, interleave writes, and race on `tmp_path.replace(target_db_path)`. The surviving process writes its own `config_hash` so `verify_current_or_raise` passes on corrupt output.
- **Concrete fix:** use `tempfile.mkstemp(dir=target_db_path.parent, prefix=target_db_path.stem + ".", suffix=".tmp")` to get a per-invocation unique temp path. Falls back to atomic rename as before.
- **Why gated:** behavior change in a code path that the existing atomic-rename test covers. Needs a new concurrent-builder test to confirm both processes land clean DBs (the fix makes this testable: before the fix concurrent builds produce UB, after the fix they produce two committed DBs in sequence, last-writer-wins).
- **Why not P0:** no current workflow spawns concurrent builds. The race is a footgun for a future multi-agent harness, not an active bug.

### P2

#### G3. `propose-rules` JSON output (agent-native W1 + cli-readiness CLR-002)

- **Finding:** 2-reviewer agreement, confidence 0.94.
- **Issue:** `scripts/forge_oracle.py propose-rules` emits Markdown only. Agents wanting to iterate proposals and invoke `scaffold_rule.py` per entry must parse Markdown bullet lists. The `--vs-forge-oracle` sibling already has `--format json`; propose-rules does not.
- **Concrete fix:** add `--format {md,json}` to the propose-rules argparse subparser; add `_render_proposals_json()` emitting `[{rank, template, rule_id, bucket, multiplier, weighted_impact, forge_signal, has_generator, run_hint, exemplar_commanders}, ...]`.
- **Why gated:** new feature surface. The JSON shape needs review before it becomes an agent contract.

#### G4. Narrow `.gitignore` forge_oracle negation (.gitignore)

- **Finding:** security SEC-001, confidence 0.82.
- **Issue:** `!data/forge_oracle/` + `!data/forge_oracle/**` un-ignore every file under that directory. Intent is only `version.txt`; any file (including a secret) dropped there would be silently tracked.
- **Concrete fix:** replace the two broad negations with `!data/forge_oracle/version.txt` only. Matches the existing `!data/*_seed.json` pattern.
- **Why gated:** tightening the negation may break Units 11+ if they commit additional oracle artifacts. Decision needed: do future units need to commit more files under `data/forge_oracle/`, and if so, which?

#### G5. `PreconDeck` / `_CommanderReport` mutable fields with frozen=True (dck_parser.py, forge_oracle_handler.py)

- **Findings:** kieran-python KP-002 (confidence 0.90), KP-003 (confidence 0.85).
- **Issue:** two frozen dataclasses carry `list[...]` fields. `frozen=True` prevents reassignment but not `.append()`. The frozen guarantee is false.
- **Concrete fix:** convert list fields to tuples; add `slots=True` to `_CommanderReport` to match the rest of the codebase.
- **Why gated:** changes the type signature of fields read across the codebase (parser consumers, handler rendering). A simple refactor but touches call sites that need updating together.

#### G6. `_render_proposals_markdown` untyped parameters (scripts/forge_oracle.py)

- **Finding:** kieran-python KP-004, confidence 0.85.
- **Issue:** `proposals: list, generators: dict` with comments explaining the real types. 48-LOC function with attribute access on both parameters throughout; readers have no way to discover the contract without following the untyped path.
- **Concrete fix:** `TYPE_CHECKING` block importing `gap_report.RuleProposal` + a `Callable` type alias. Zero runtime cost with `from __future__ import annotations`.
- **Why gated:** requires `TYPE_CHECKING` import coordination with runtime sys.path mutation. Small refactor; straightforward but needs attention.

#### G7. Corrupt `config_hash` format check (config.py)

- **Finding:** reliability REL-005, confidence 0.68.
- **Issue:** a truncated or non-hex `config_hash` value (from mid-write corruption) raises `OracleConfigStaleError` with message "built under a different config". Operators read that as "rebuild fixes it" and loop on rebuilds rather than diagnosing the actual DB corruption.
- **Concrete fix:** before the equality check, validate `len(stored) == 64 and all(c in '0123456789abcdef' for c in stored.lower())`; on mismatch raise `OracleConfigMissingError` with a "possibly corrupt DB" hint.
- **Why gated:** adds a new error path. Trivial logic, but changes observable behavior for edge-case inputs.

---

## Manual (actionable, needs human judgment)

### P1

#### M1. Content hash over ingested `.dck` files vs git HEAD SHA (adversarial ADV-003)

- **Issue:** `OracleConfigInputs.forge_sha` comes from `git rev-parse HEAD`, not a content hash of the actually-ingested `.dck` file tree. A partial sparse-checkout update (some `.dck` files at new SHA content, others cached from old object store) produces a valid-looking hash over mixed-state data. All strict consumers pass verification on silently-wrong PPMI.
- **Tradeoff:** adding a content hash over the sorted `(path, sha256(content))` tuples for every ingested `.dck` file is the safest fix. Cost: one extra pass over the corpus at build time (~1s at current scale). Alternative: document the sparse-checkout-consistency invariant and add a regression test that sanity-checks a known per-file content hash, which is cheaper but doesn't catch future drift.
- **Decision needed:** accept the extra build cost for content hashing, or settle for the invariant-doc approach?

### P2

#### M2. `propose-rules` TOCTOU between hash verify and data read (adversarial ADV-005)

- **Issue:** strict consumer `propose-rules` verifies the hash on an open connection, closes the connection, then calls `rank_gaps` which re-opens via the soft `load_forge_signals` path. If the DB disappears between verify and read, propose-rules silently degrades to volume-only with exit 0, violating the declared strict-consumer contract.
- **Tradeoff:** holding the connection open across `rank_gaps` would fix this but requires threading the open `sqlite3.Connection` through the `gap_report.rank_gaps` API (currently takes only a `Path`). Alternative: re-verify the hash after `rank_gaps` returns, before emitting the Markdown report.
- **Decision needed:** refactor the API, or add a post-read re-verify.

#### M3. `--vs-forge-oracle` uses stale pinned fixture for "our" ranks (adversarial ADV-007)

- **Issue:** pinned `FixtureEntry.scores` is our ranking; forge side is live. After a new rule lands but before `bench.py audit --repin --yes`, the tau measures stale-us vs live-Forge, making divergences misleading as rule-proposal seeds.
- **Tradeoff:** re-score live for "our" ranks every time instead of reading the fixture. Adds scoring cost (~seconds). Alternative: detect stale fixture via `--expect-identity` pre-check and refuse to run if the fixture is stale.
- **Decision needed:** always-live vs staleness-guard.

#### M4. `card_hints` name normalization boundary (adversarial ADV-008)

- **Issue:** `_fetch_hint_rows` queries `card_hints.card_name = cards.name`. If the importer ever stores hints under a different normalization (Forge-internal vs Scryfall-canonical), the lookup returns zero rows silently and pair_scorer returns 0.0 for affected cards.
- **Action:** empirical check on the real DB — sample 100 hint rows, confirm `cards.name = card_hints.card_name` holds without case/whitespace drift. If it does, add a regression test. If it doesn't, add a normalization layer.
- **Decision needed:** needs a data check, not a code change upfront.

#### M5. Bulk-load refactor of `forge_rank_candidates` (performance PERF-001)

- **Issue:** 4 SQL queries per `rate_pair` call × 30 candidates × 100 commanders = 12,000 queries for `--vs-forge-oracle`. Currently 0.4s — fine today. At 500-commander future scale: ~60k queries, ~1–2s.
- **Tradeoff:** bulk-load all card views + hint rows for the commander + candidate set via IN-clauses (~4 queries per commander total), score in memory. Eliminates N+1 at the cost of an in-memory copy of the scored set.
- **Decision needed:** premature if golden set stays at 100 commanders; worth doing before scaling up.

#### M6. `scaffold_rule._GENERATORS` private-symbol coupling (maintainability M003)

- **Issue:** `scripts/forge_oracle.py propose-rules` does `sys.path.insert(0, scripts/)` + `from scaffold_rule import _GENERATORS`. Renaming `_GENERATORS` in scaffold_rule.py silently breaks at runtime.
- **Fix:** expose `get_generators()` or rename to `GENERATORS` (public constant) in scaffold_rule.py; update propose-rules to use the public name.
- **Decision needed:** coordinate with scaffold_rule's existing contract (it may be imported by other scripts in scripts/ too).

### P3

#### M7. Additional testing gaps (testing T002, T003, T004, T005, T006, T008, T009, T011, T012)

- Tests for: `_score_commander` <2 resolved branch, `forge_rank_candidates` partial resolution, gap_report soft-fallback with stale hash, markdown happy-path tau-value assertion, rank_gaps snapshot integration, Ability hint category, unknown Color hint, `.dck` TOCTOU, all-singleton PPMI corpus.
- **Why manual:** each is a new test-case design. Not blocking — overall coverage is 87% and the core paths are exercised. Good candidates for a "harden tests" follow-up PR.

---

## Advisory (report-only, no action taken)

| Finding | Reviewer | Reason for advisory |
|---|---|---|
| `--vs-forge-oracle` is tracking-only | adversarial ADV-007 + plan | Explicit plan decision, not a bug |
| `--build` has no `--format json` | cli-readiness CLR-001 | Future UX improvement, not a bug |
| No `forge_oracle.py config` inspect subcommand | cli-readiness CLR-003 | Nice-to-have for agents |
| gap_report silent fallback no stdout indicator | cli-readiness CLR-005 | Stderr warning covers it today |
| `_stubs.py` linear growth per new mode | maintainability M005 | Revisit past ~12 modes |
| `sys.path.insert` defense-in-depth | security SEC-002 + M003 | Acceptable for local dev tool |

---

## Applied in this pass (`/ce-code-review` LFG)

**Safe_auto fixes landed alongside this doc:**

- T010 (test_bench_vs_forge_oracle capsys double-drain)
- ADV-004 (grep-fence regex extended to catch `from mtg_synergy_graph import forge_oracle`)
- COR-001 (both CLI catch blocks broadened to catch `OracleForgeCheckoutError`, `OracleVersionFileError`)
- REL-001 (git subprocess timeout + `OracleForgeCheckoutError` on `TimeoutExpired`)
- REL-002 (`tmp_path.unlink(missing_ok=True)` + try/except to prevent secondary-exception replacement)
- REL-003 (isolation-test subprocess `TimeoutExpired` → `pytest.fail` with actionable message)
- REL-004 (synergy-db `sqlite3.DatabaseError` guard in propose-rules)
- W2 (argparse `--format` already registered globally — added explicit CLI regression test)
- COR-004 (gap_weight p95 off-by-one index correction)
- COR-005 (test rename for `needs_partially_met_name_penalty`)
- KP-004 (`_render_proposals_markdown`: removed untyped-param bypass by lint-fixing the imports; still carries the TODO for Protocol-based typing — tracked as G6)
- KP-005 (logger.warning in `_render_proposals_markdown` generator-raised branch)
- KP-006 (`iter_deck_files` return type → `Iterator[Path]`, unused `Iterable` import removed)
- KP-007 (conftest.py adds `sys.path.insert` once; duplicate inserts removed from individual test files)
- KP-008 (rank_gaps `warn_fn` guarded when `forge_oracle_db is None` — no more "at None is missing")
- KP-009 (`[d for d in decks]` → `list(decks)` in ppmi.py)
- CLR-004 (audit subparser `--forge-oracle-db` / `--smoothing-k` / `--min-decks` help text prefixed with "Used by --vs-forge-oracle only.")
- SEC-003 (Markdown pipe escape via `_md_escape` for commander + candidate names in `--vs-forge-oracle` handler)
- M001 (`ranking.ranks_of` deleted — zero production callers; removed its two tests)
- Finding #10 (`pair_scorer._apply_hints` guard restructured so no-targets+negative path returns explicitly; no more correct-by-accident fallthrough)

**Regression tests added in the same pass:**

- `test_cli_vs_forge_oracle_format_json_through_argparse` — end-to-end JSON format flag via `bench_main()`
- `test_grep_fence_detects_package_level_import_violation` — covers single-line + multi-line parenthesized forms

**Verification:** full suite 1568 passed (+2 new tests), coverage 87.23%, `bench.py audit --expect-identity` PASS.
