---
title: Forge-Second-Oracle — Design-Time BoosterDraftAI + Precon PPMI Signals
type: feat
status: completed
date: 2026-04-23
origin: docs/brainstorms/2026-04-21-forge-second-oracle-requirements.md
---

# Forge-Second-Oracle — Design-Time BoosterDraftAI + Precon PPMI Signals

## Overview

Add two Forge-internal signals to the rule-authoring workflow (`gap_report.py`, `bench.py audit`) that prioritize **which coverage gap matters next**, not just which is biggest by volume. Both signals are offline, committed-artifact, **never consulted at inference**. The two signals:

1. A Python port of Forge's Java BoosterDraftAI pair-scoring heuristic — hundreds of designer-authored rules for two-card synergy (`forge_oracle.pair_scorer.rate_pair(a_oracle_id, b_oracle_id) -> float`).
2. A PPMI co-occurrence table over Forge's bundled precon decks, keyed by canonical `port_nodes.subkind` pairs, RAPM-lineup-adjusted with Laplace smoothing.

Both feed a new `forge_signal` column on `gap_report.py`, a new `bench.py audit --vs-forge-oracle` Kendall-τ agreement sidecar, and a `forge_oracle.py propose-rules` command that emits scaffold-ready rule proposals ranked by forge signal.

**Invariant:** `recommend.py`, `SynergyEngine.page`, `universal_scorer.py`, `graph_engine.py`, and every `complement_rules/` module must never import from `forge_oracle/`. Enforced by `bench.py audit --expect-identity` (behavioral) **and** a new grep-based regression test (structural).

## Problem Frame

`gap_report.py` currently ranks uncovered `(commander_port_shape, cand_event)` cells by `reach × (1 − covered_rate)`. That's good volume signal but every uncovered cell counts equally. Two Forge-internal signals — categorically distinct from EDHREC popularity — can tell us **which gaps are mechanically meaningful**: BoosterDraftAI's hand-coded pair heuristics (designer opinion) and precon co-occurrence PPMI (designer curation expressed through ship-ready decks).

Aligns with:
- `memory/feedback_extract_all_forge.md` (extract ALL Forge data; precons and AI were not yet in scope)
- `memory/feedback_forge_data_direct.md` (use Forge data directly; the Java port is a direct-use mechanism)
- `memory/feedback_edhrec_not_goal.md` (hidden gems from mechanics; this is a second mechanics-side proxy)
- `memory/feedback_audit_every_change.md` (every scoring change gated by NDCG audit; the `--expect-identity` invariant preserves this)

(see origin: `docs/brainstorms/2026-04-21-forge-second-oracle-requirements.md`)

## Requirements Trace

- **R1.** BoosterDraftAI Java → Python port (pair scoring only, no GameState/PlayerAI) — origin FR1.
- **R2.** `forge_precon_ppmi` table built from Forge precon decks, PPMI + RAPM lineup adjustment + Laplace smoothing — origin FR2.
- **R3.** `gap_report.py` re-ranked by `reach × (1 − covered_rate) × forge_oracle_weight`, new `forge_signal` column — origin FR3.
- **R4.** `bench.py audit --vs-forge-oracle` sidecar reporting aggregate Forge-agreement (Kendall-τ), per-commander delta, top-10 divergence pairs — origin FR4.
- **R5.** `scripts/forge_oracle.py propose-rules --top 20` delegates to `scaffold_rule._GENERATORS` — origin FR5.
- **R6.** Forge source SHA pinned in `data/forge_oracle/version.txt`; oracle artifacts refuse to serve when current `data/forge/` HEAD ≠ pinned SHA or when `OracleConfigInputs` hash drifts — origin FR6.
- **R7.** Inference path bitwise-identical before and after landing (verified by `bench.py audit --expect-identity` + structural import fence).

## Scope Boundaries

- Oracle is **never** consulted at inference (recommend.py / engine.page / universal_scorer / graph_engine / complement_rules all stay unchanged).
- No training, no learned weights. Static lookup tables only.
- No EDHREC data in any forge-oracle code path.
- No auto-accept of rule proposals — all go through existing `scaffold_rule → _audit_rule_impact` flow.
- Minimal Java port only — if the chosen method needs `GameState`/`PlayerAI`/`Card` runtime subclass context, STOP at the spike and escalate.

### Deferred to Separate Tasks

- Scryfall Tagger `otag:*` ingest (out-of-scope per origin).
- Full Forge draft-AI port (deck-building, archetype classification, mana-curve simulation).
- PPMI on EDHREC decklists (rejected in origin as popularity leak).
- Promotion of `--vs-forge-oracle` Kendall-τ to a commit-gate (tracking-only at MVP; promotion requires separate brainstorm, same escalation posture as `hidden_gem_hit_rate`).
- Integration of `forge_signal` with `hidden_gem_hit_rate` metric (future axis-combination work).

## Context & Research

### Relevant Code and Patterns

- `scripts/gap_report.py:125-143` — `GapStat` dataclass, `impact = commanders * (1 - activation_rate)`; extension point is the sort at L774 + Markdown renderer around L707.
- `src/mtg_synergy_graph/bench/cli.py:50-66,113,228-247` — mutex-group flag registration + `_resolve_mode` chain + `_HANDLERS` dict; `--vs-forge-oracle` follows this pattern.
- `src/mtg_synergy_graph/bench/handlers.py:704-826` — `handle_inspect_gems` is the direct architectural mirror for `--vs-forge-oracle` (PinnedFixture.load + live re-score + set/ranking diff + Markdown rendering).
- `src/mtg_synergy_graph/bench/handlers.py:749-783` — sidecar-DB flag pattern (`--edhrec-db`): missing-file exit 2 with actionable hint, `DatabaseError` catch with repair hint, `finally: close()`.
- `scripts/scaffold_rule.py:1770-1777` — flat `_GENERATORS: dict[str, Callable]` registry; `propose-rules` iterates `RuleProposal`s and delegates, no new generator code needed.
- `src/mtg_synergy_graph/importer.py:63` — `_build_oracle_id_resolver` (4-tier: exact non-token, exact any, DFC front, DFC back); reused by the `.dck` ingest for display-name → oracle_id resolution.
- `src/mtg_synergy_graph/port_graph/projection.py:44` — `subkind = port_type || '.' || event_class`; natural PPMI join key, no shim needed.
- `src/mtg_synergy_graph/universal_scorer.py` (`ScoringConfigInputs` + `get_scoring_config_inputs`) — template for the offline analog `OracleConfigInputs`.
- `src/mtg_synergy_graph/bench/tensor.py` (`compute_config_hash`) — template for `compute_oracle_hash`.
- `.gitignore` (`data/*` + `!data/*_seed.json`) + `tests/test_seed_files_tracked.py` — precedent for committed artifacts under `data/`.

### Institutional Learnings

- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md` — inference-path flag-gate pattern does **not** apply here (this is offline). But the *meta-principle* — mechanical enforcement of a subsystem invariant via a hash that refuses stale comparisons — transfers directly. Apply it offline as `OracleConfigInputs` + `compute_oracle_hash` → sidecar refuses to run when stale.
- `docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md` — any committed forge-oracle artifact (version.txt, eventual PPMI seed) must use `data/* + !data/<artifact>` pattern in `.gitignore` AND be asserted by `tests/test_seed_files_tracked.py` (extend `REQUIRED_TRACKED`). Pre-commit passes against working tree but CI fails on missing index entries.

### External References

- Forge `Card-Forge/forge.git` GitHub repo — source of truth for BoosterDraftAI and precon `.dck` files; currently checked out at SHA `ed97d9bb` at `data/forge/`.

## Key Technical Decisions

- **Decision: `data/forge/` stays a vendored partial clone, not a submodule.** The existing setup (partial clone, shallow, sparse-checkout) is working; switching to a submodule would force every contributor to re-clone the full Forge history. Instead, extend the existing sparse-checkout patterns to include `forge-ai/` and precon trees. Pin the Forge SHA in `data/forge_oracle/version.txt` (committed to our repo); oracle code verifies at load time that `data/forge/ HEAD == version.txt`.
- **Decision: PPMI join key is `port_nodes.subkind`, not raw port tuples.** Plan 002 already landed the canonical projection; its 21 `node_kind` values and `port_type.event_class` `subkind` give a globally unique, SQL-queryable key. `UNKNOWN` subkinds (~28%) are included in the table but can be filtered at rank time; drop-vs-include decision is per-consumer.
- **Decision: BoosterDraftAI port happens after a time-boxed spike gate.** The chosen Java method is unknown until we read `forge-ai/src/main/java/forge/ai/BoosterDraftAI.java` (and `CardSynergy.java`, `DeckgenUtil.java`). Unit 2 is a reconnaissance spike with a GO/NO-GO verdict; if the chosen method's Python-equivalent surface exceeds ~500 LOC, STOP and escalate before starting Unit 3.
- **Decision: Oracle sidecar DB is a separate file (`data/forge_oracle.db`), not a new table in `mtg_synergy.db`.** Sidecar isolation makes it trivially easy to verify "inference path doesn't touch this" (the file simply isn't opened), and the oracle DB is large/regenerable while `mtg_synergy.db` is small and pinned.
- **Decision: `gap_report.py` sidecar read is optional with graceful fallback.** If `data/forge_oracle.db` is missing, `forge_signal = 1.0` and the old ranking is recovered bitwise. Missing sidecar is not an error — we don't want contributors without the oracle build to see CI explode. `bench.py audit --vs-forge-oracle` is stricter: missing sidecar → exit 2 (must explicitly invoke the subcommand with intent).
- **Decision: `OracleConfigInputs` captures Forge SHA + PPMI smoothing `k` + `decks_count` threshold + port-signature canonicalization version + chosen Java method identifier.** Hash mismatch between sidecar rows and current config → refuse-to-run in `--vs-forge-oracle` and `propose-rules` (but not `gap_report.py`, which uses stale-but-present signal silently rather than breaking the rule-authoring workflow).
- **Decision: Structural grep fence complements `--expect-identity`.** `--expect-identity` catches behavioral drift; grep catches imports that someone added "just in case" and would later be load-bearing. Belt-and-suspenders: both together prove isolation.

## Open Questions

### Resolved During Planning

- **Will the ≤500 LOC Python port be feasible?** Unknown until spike reads `BoosterDraftAI.java` on the pinned SHA. Unit 2 is the spike; subsequent units gate on its verdict.
- **Git submodule vs snapshot-copy for Forge?** Keep the existing partial-clone setup; extend sparse-checkout patterns; pin the SHA in `version.txt`. No submodule.
- **Does a `.dck` parser already exist?** No. `find src scripts -name "*.dck*"` returns zero. Write one (<50 LOC; `.dck` is INI-ish with `[metadata]`, `[main]`, `[sideboard]` sections).
- **PPMI join key — canonical subkind or raw tuple?** Canonical `port_nodes.subkind`. `UNKNOWN` subkinds included; filterable at rank time.
- **Sidecar DB fatal or silent when missing?** Silent in `gap_report.py` (fallback to `1.0`), fatal in `bench.py audit --vs-forge-oracle` + `propose-rules` (exit 2).

### Deferred to Implementation

- **Exact Java method to port** — candidates per origin: `BoosterDraftAI.rateCard`, `CardSynergy.getSynergy`, `DeckgenUtil.*`. Final choice depends on which has the most pure pair-scoring surface (fewest `GameState`/`PlayerAI` dependencies). Unit 2 decides.
- **Double-faced / adventure / split card handling in pair counts** — origin flagged this. Decide during Unit 4 after seeing how `.dck` files reference DFCs (by front-face name? both faces? combined string?). The oracle_id resolver already handles DFC faces via `alternate_name`; treat DFCs as their oracle_id entity (one pair-count contribution per DFC, not per face).
- **Exact precon subtree paths inside `forge-gui/res/`** — upstream Forge keeps precons in at least `quest/precons/`, `blockdata/`, potentially `commander/`. Enumerate during Unit 1 sparse-checkout; finalize in Unit 4.
- **`OracleConfigInputs` exact field list** — skeleton is Forge SHA + smoothing `k` + threshold + canonicalization version + Java method identifier, but may gain fields during implementation (e.g., normalization strategy, min-deck-count). Add as needed; each addition flips the hash.
- **Kendall-τ aggregation strategy** — per-commander weighted by candidate count, or unweighted mean? Decide during Unit 7 after seeing top-30-size distribution; default to unweighted mean.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Data flow:

```
data/forge/  (vendored partial-clone, SHA-pinned)
  ├── forge-game/                    ← existing; used by importer
  ├── forge-gui/res/cardsfolder/     ← existing; used by importer
  ├── forge-ai/                      ← NEW via sparse-checkout
  │     └── src/main/java/forge/ai/  (BoosterDraftAI.java, CardSynergy.java, ...)
  └── forge-gui/res/quest/precons/   ← NEW via sparse-checkout
        └── **/*.dck                  (precon deck files)

                        │ Unit 1: sparse-checkout ext + SHA pin
                        ▼
                 data/forge_oracle/version.txt  (committed, tracked)

                        │ Unit 2: recon spike (GO/NO-GO gate)
                        ▼
                 docs/spikes/...-boosterdraft-port-feasibility.md

                        │ Unit 3 (Java port)      │ Unit 4 (PPMI ingest)
                        ▼                         ▼
          pair_scorer.rate_pair()       scripts/forge_oracle.py build
                   │                              │
                   │                              ▼
                   │                    data/forge_oracle.db
                   │                       └── forge_precon_ppmi
                   │                       └── oracle_config (hash)
                   │                              │
                   └──────────┬───────────────────┘
                              │ Unit 5: OracleConfigInputs + hash
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   Unit 6:              Unit 7:                Unit 8:
   gap_report.py        bench.py audit         forge_oracle.py
   forge_signal         --vs-forge-oracle      propose-rules
   column (silent       Kendall-τ sidecar       → delegates to
    fallback)           (hash strict)            scaffold_rule._GENERATORS

  ─────────────── INFERENCE PATH (untouched) ───────────────
  recommend.py → SynergyEngine.page → universal_scorer
                      ↑
                      │ Unit 9: grep fence + --expect-identity
                      │         assert zero forge_oracle imports
```

Every horizontal arrow into the inference path is prohibited by Unit 9's regression test.

## Implementation Units

- [x] **Unit 1: Forge sparse-checkout extension + SHA pin**

**Goal:** Make `forge-ai/` and precon deck subtrees available in `data/forge/` via sparse-checkout; pin the Forge SHA in a committed `data/forge_oracle/version.txt`; extend `.gitignore` + `tests/test_seed_files_tracked.py` for the new tracked artifact.

**Requirements:** R6, R7 (foundation for isolation).

**Dependencies:** None.

**Files:**
- Create: `data/forge_oracle/version.txt` (single-line Forge commit SHA, plus a comment line with human-readable Forge release/tag)
- Create: `src/mtg_synergy_graph/forge_oracle/__init__.py` (empty package skeleton)
- Modify: `.gitignore` — add `!data/forge_oracle/version.txt` (and prepare for future `!data/forge_oracle/*.json` if oracle config is committed)
- Modify: `tests/test_seed_files_tracked.py` — extend `REQUIRED_SEED_FILES` tuple (or split into `REQUIRED_TRACKED_FILES` with `version.txt` added)
- Modify: existing Forge-checkout setup script or documentation (discover in implementation — if setup is doc-only, add sparse-checkout patterns to `docs/` setup notes; if it's a script, extend it)
- Create: `docs/FORGE_ORACLE.md` (brief: how to refresh the Forge checkout, what SHA pinning means, how to bump the pin)
- Test: `tests/test_forge_oracle_version_pin.py`

**Approach:**
- Add sparse-checkout patterns: `forge-ai/` and precon deck subtrees (`forge-gui/res/quest/precons/`, `forge-gui/res/blockdata/`, potentially `forge-gui/res/commander/` — enumerate during spike).
- `version.txt` is a committed file with current Forge SHA (`ed97d9bb` at time of writing). Oracle code reads this at load time and compares against `git -C data/forge rev-parse HEAD`.
- Follow the `!data/*_seed.json` pattern in `.gitignore`.

**Patterns to follow:**
- `.gitignore` (`data/*` + `!data/*_seed.json`) and `tests/test_seed_files_tracked.py`.

**Test scenarios:**
- Happy path — `version.txt` exists, contains a 40-char-ish SHA, is tracked by `git ls-files`.
- Happy path — `SHA_FROM_FILE == git -C data/forge rev-parse HEAD` when checkout is at the pinned SHA.
- Error path — version mismatch raises clear `OracleVersionMismatchError` with the actionable hint "run `scripts/forge_oracle.py upgrade` to bump" (error class stub, real upgrade command in Unit 5+).
- Error path — missing `data/forge/.git` or missing `version.txt` raises descriptive error, not a `FileNotFoundError`.
- Integration — `tests/test_seed_files_tracked.py` asserts `data/forge_oracle/version.txt` is in `git ls-files`.

**Verification:**
- `data/forge_oracle/version.txt` committed and tracked.
- Sparse-checkout extended, `data/forge/forge-ai/` and at least one `.dck` deck dir exist on disk.
- `uv run pytest tests/test_forge_oracle_version_pin.py tests/test_seed_files_tracked.py` passes.

---

- [x] **Unit 2: BoosterDraftAI reconnaissance spike (time-boxed, gate)**

**Goal:** Read the Forge Java AI source on the pinned SHA, identify the single method that best matches "two cards → float synergy score" with minimal `GameState`/`PlayerAI` context, estimate the Python-equivalent LOC, and emit a GO/NO-GO verdict. **All subsequent units (3, 7, 8) gate on this.**

**Requirements:** R1 (feasibility check).

**Dependencies:** Unit 1.

**Files:**
- Create: `docs/spikes/2026-04-23-boosterdraft-port-feasibility.md`
- No source code changes.

**Approach:**
- Budget: ≤ 1 day of reading + documenting. Do not start Python porting in this unit.
- For each candidate method (`BoosterDraftAI.rateCard`, `CardSynergy.getSynergy`, `DeckgenUtil.*`, plus anything else surfaced by reading the files): count LOC, list external type dependencies, classify each dependency as (a) pure data (ok), (b) game-state context (blocker), (c) color/CMC/type lookup (equivalent exists in our DB).
- Rank candidates by "lowest GameState coupling." Pick the winner.
- Emit verdict: **GO** if chosen method's Python equivalent estimated ≤ 500 LOC and dependencies resolve to local DB; **NO-GO** if blocked by `GameState`/`PlayerAI`/runtime `Card` subclass context.
- If NO-GO, the plan degenerates: drop Unit 3, scope `--vs-forge-oracle` to PPMI-only (rename to `--vs-forge-precon`), and land Units 4-10 as a smaller win. Escalate to user before proceeding with the degenerate scope.

**Execution note:** Characterization-only. No code changes in this unit.

**Test scenarios:**
- Test expectation: none — this is a reconnaissance spike, not feature-bearing code.

**Verification:**
- `docs/spikes/2026-04-23-boosterdraft-port-feasibility.md` exists with: chosen method name + file:line, LOC estimate, external deps table, GO/NO-GO verdict, and (if GO) a small table of 10 known-to-Forge card pairs with Java reference scores to be used as fidelity fixtures in Unit 3.
- User explicitly approves the verdict before Unit 3 starts.

---

- [x] **Unit 3: Python port of the chosen Java pair-scorer** *(gated by Unit 2 GO verdict)*

**Goal:** Port the chosen Java method to pure-Python `forge_oracle.pair_scorer.rate_pair(a_oracle_id, b_oracle_id, conn) -> float`. No JVM, no game state. Deterministic. ≤ 500 LOC (hard cap per origin Success Criterion 5).

**Requirements:** R1.

**Dependencies:** Unit 2 (GO verdict).

**Files:**
- Create: `src/mtg_synergy_graph/forge_oracle/pair_scorer.py`
- Create: `tests/test_forge_oracle_pair_scorer.py`
- Create: `tests/fixtures/forge_oracle/java_reference_pairs.json` (10+ hand-computed Java reference pair scores from Unit 2)

**Approach:**
- Inputs: two oracle_ids; `sqlite3.Connection` for joining `cards` + `card_ports` + any `card_hints`/`port_attributes` the Java method reads.
- Use `@dataclass(frozen=True)` for any intermediate card-state representation (per `rules/python/coding-style.md` immutability default).
- Pull card color, CMC, types, subtypes, keywords, and ports from local DB (all already imported). If the Java method reads anything else, flag in the spike and escalate.
- Output: float score. Brainstorm does not fix the scale — preserve Java's original scale (could be int, could be 0-100, could be arbitrary); the consumer sides (gap_report weighting, Kendall-τ) are rank-preserving so absolute scale doesn't matter for the MVP.
- Pure function. No caching in this unit (add a `@functools.cache` wrapper at call sites in Unit 6+ if profiling shows a hotspot).

**Execution note:** Test-first. Write the fidelity fixture tests (fail initially) before porting the Java logic.

**Patterns to follow:**
- `src/mtg_synergy_graph/complement_rules/pathway.py` for pure-function shape + `@functools.cache` style.
- `src/mtg_synergy_graph/importer.py` for `sqlite3` + oracle_id resolution patterns.

**Test scenarios:**
- Happy path — each of the 10+ fixture pairs matches the Java reference score within documented tolerance (exact if possible; otherwise `abs(py - java) < 1e-6`). Drives the port.
- Happy path — symmetry: `rate_pair(a, b) == rate_pair(b, a)` when the Java method is symmetric (verify in spike; if asymmetric, test both directions).
- Edge case — same-card pair `rate_pair(a, a)`: matches Java behavior (likely returns 0.0 or a self-synergy constant).
- Edge case — one oracle_id absent from DB: raises `LookupError` (not `KeyError` from the SQL layer; explicit check at function entry).
- Edge case — card with zero ports in `card_ports`: returns a well-defined value (likely 0.0 or Java default), not a divide-by-zero or `None`.
- Error path — connection is None / closed: raises `ValueError` with a clear message.
- Numeric stability — same inputs repeatedly produce bitwise-identical output (determinism is a functional requirement).

**Verification:**
- All fidelity fixture cases pass.
- `wc -l src/mtg_synergy_graph/forge_oracle/pair_scorer.py < 500`.
- `grep -rn "forge_oracle" src/mtg_synergy_graph/{engine,universal_scorer,graph_engine,complement_rules}.py src/mtg_synergy_graph/complement_rules/` returns zero matches.

---

- [x] **Unit 4: Precon `.dck` parser + `forge_precon_ppmi` table ingest**

**Goal:** One-time build of `data/forge_oracle.db`'s `forge_precon_ppmi` table from Forge's bundled precon decks. Drives `scripts/forge_oracle.py build` subcommand.

**Requirements:** R2.

**Dependencies:** Unit 1.

**Files:**
- Create: `src/mtg_synergy_graph/forge_oracle/dck_parser.py` (parse `.dck` INI-format: `[metadata]`, `[main]`, `[sideboard]`)
- Create: `src/mtg_synergy_graph/forge_oracle/ppmi.py` (PPMI + RAPM lineup adjustment + Laplace smoothing, pure math)
- Create: `src/mtg_synergy_graph/forge_oracle/ingest.py` (orchestrates: walk decks → resolve names → extract pair co-occurrences → PPMI → insert)
- Create: `src/mtg_synergy_graph/forge_oracle/schema.sql` (table definitions)
- Create: `scripts/forge_oracle.py` (CLI with `build` subcommand; `inspect`, `propose-rules`, `upgrade` subcommands added in later units)
- Create: `tests/test_forge_oracle_dck_parser.py`
- Create: `tests/test_forge_oracle_ppmi_math.py`
- Create: `tests/test_forge_oracle_ingest.py`
- Create: `tests/fixtures/forge_oracle/sample.dck`, `tests/fixtures/forge_oracle/minimal_decks/` (synthetic)

**Approach:**
- `.dck` format per upstream Forge: ASCII, section headers in brackets, `N cardname` lines in each section. Metadata header contains deck name + (sometimes) author + deck type.
- Display-name → oracle_id via reused `_build_oracle_id_resolver` from `importer.py`. Cards absent from our DB (e.g., Un-set / acorn-border / unreleased in precon corpus) are skipped with a counter logged; ingest does not fail.
- DFCs / adventures / split cards: treat as their oracle_id entity. One contribution per (card_a_oracle_id, card_b_oracle_id) pair per deck (not per face).
- Pair extraction per deck: for each unordered pair of cards in the main deck (not sideboard), emit one `(oracle_id_a, oracle_id_b)` contribution with `a < b` (canonical order).
- Project to port signatures: for each card, look up its distinct `port_nodes.subkind` set; emit all cartesian-product `(subkind_a, subkind_b)` pairs. `UNKNOWN` subkinds included.
- PPMI math with RAPM-style lineup adjustment: control for each deck's "bag of port signatures" so pairs that happen inside a mana-rock-heavy deck don't get inflated. Formula deferred to the ppmi.py module (standard PMI + positive clamp + Laplace `k` smoothing; exact k chosen empirically during implementation).
- Minimum-evidence threshold: `decks_count >= 3` required to emit a non-null PPMI row (brainstorm Risk 2 mitigation).
- Output table: `forge_precon_ppmi (port_signature_a TEXT, port_signature_b TEXT, ppmi REAL, decks_count INTEGER, last_updated TEXT, PRIMARY KEY (port_signature_a, port_signature_b))` with canonical ordering `a < b`.
- `schema.sql` also defines `oracle_config (key TEXT PRIMARY KEY, value TEXT)` scaffold for Unit 5's hash.

**Execution note:** PPMI math is pure-function — test the math against hand-computed synthetic cases before wiring the ingest pipeline.

**Patterns to follow:**
- `src/mtg_synergy_graph/importer.py` for directory-walk + batched-commit + oracle_id resolution.
- `src/mtg_synergy_graph/port_graph/projection.py` (`subkind` computation) for the join key.
- `src/mtg_synergy_graph/db.py:open_db` for DB connection pattern.

**Test scenarios:**
- `.dck` parser happy path — parse a known-shape `.dck` fixture, extract main deck as `[(count, name), ...]`.
- `.dck` parser edge case — Unicode card names, trailing whitespace, Windows CRLF line endings, comments (if Forge supports `#` or `//`).
- `.dck` parser edge case — empty sideboard, missing metadata section, split cards formatted as `Name // Name`.
- `.dck` parser error path — malformed file (no section header) raises `DckParseError`.
- PPMI math happy path — 4-card synthetic corpus with hand-computed PPMI per pair matches within 1e-9.
- PPMI math edge case — pair appearing in exactly 1 deck → filtered out by `decks_count >= 3`.
- PPMI math edge case — pair with zero joint occurrences → `ppmi = 0` after positive clamp (never negative per P-PMI definition).
- PPMI math edge case — pair of identical signatures → excluded from output (self-pair is meaningless).
- PPMI math edge case — RAPM adjustment — pair that co-occurs ubiquitously (mana rocks) has its PPMI damped vs a pair that co-occurs selectively.
- Integration — `scripts/forge_oracle.py build --decks-dir <fixtures>` produces a sqlite DB with the expected row count, `decks_count` values, and a ppmi distribution that passes sanity bounds (`0 <= ppmi <= log(N)`).
- Integration — re-running `build` is idempotent: second run produces a DB with identical row contents (stable sort, `last_updated` updated).
- Integration — a `.dck` containing cards unknown to our DB logs skip count but does not fail the build.

**Verification:**
- `data/forge_oracle.db` exists with `forge_precon_ppmi` populated, `decks_count >= 3` filter applied.
- Build reruns idempotent: `sha256sum` of the DB after two successive `build`s differs only on `last_updated` columns.

---

- [x] **Unit 5: `OracleConfigInputs` + `compute_oracle_hash` + refuse-to-run wiring**

**Goal:** Mirror the inference-path `ScoringConfigInputs` / `compute_config_hash` pattern for the offline oracle. Write the hash into `oracle_config` on build; verify the hash on read in strict consumers.

**Requirements:** R6, R7.

**Dependencies:** Unit 4.

**Files:**
- Create: `src/mtg_synergy_graph/forge_oracle/config.py` (`OracleConfigInputs` NamedTuple, `get_oracle_config_inputs()` accessor, `compute_oracle_hash()`)
- Modify: `src/mtg_synergy_graph/forge_oracle/ingest.py` — write hash into `oracle_config` table at end of build
- Create: `tests/test_forge_oracle_config_hash.py`

**Approach:**
- `OracleConfigInputs` NamedTuple fields (initial): `forge_sha: str`, `ppmi_smoothing_k: float`, `min_decks_count: int`, `port_signature_version: int`, `java_method_id: str` (from Unit 2 spike — e.g., `"BoosterDraftAI.rateCard@forge-1.6.53"`).
- `compute_oracle_hash()` follows the template of `src/mtg_synergy_graph/bench/tensor.py:compute_config_hash` — SHA-256 over `repr(sorted(tuple(asdict(cfg).items())))`.
- `oracle_config` table has one row per key (flexible KV). Build writes: `forge_sha`, `ppmi_smoothing_k`, `min_decks_count`, `port_signature_version`, `java_method_id`, and `config_hash`.
- Refuse-to-run: `forge_oracle.config.verify_current_or_raise(conn)` compares `conn` `oracle_config.config_hash` to `compute_oracle_hash()`; mismatch raises `OracleConfigStaleError` with an actionable "run `scripts/forge_oracle.py build`" hint.

**Patterns to follow:**
- `src/mtg_synergy_graph/universal_scorer.py` `ScoringConfigInputs` + `get_scoring_config_inputs`.
- `src/mtg_synergy_graph/bench/tensor.py` `compute_config_hash`.
- `docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md` meta-principle (mechanical enforcement via hash).

**Test scenarios:**
- Happy path — `compute_oracle_hash()` is deterministic across runs with same inputs.
- Happy path — each field change (Forge SHA bump, smoothing `k` change, threshold change, canonicalization version bump, method ID change) flips the hash.
- Happy path — `verify_current_or_raise(conn)` passes when DB hash matches current config.
- Error path — stale DB hash raises `OracleConfigStaleError` with rebuild hint.
- Error path — missing `oracle_config` row raises `OracleConfigMissingError`.
- Integration — `forge_oracle.py build` writes a fresh hash; rebuild after changing `min_decks_count` produces a different stored hash.

**Verification:**
- Build writes `config_hash` into `oracle_config`.
- `verify_current_or_raise` is called at the top of `--vs-forge-oracle` and `propose-rules` handlers (Units 7, 8).

---

- [x] **Unit 6: `gap_report.py` re-ranking via `forge_signal` column**

**Goal:** Extend `gap_report.py` to optionally multiply each gap's impact by a Forge oracle signal sourced from `data/forge_oracle.db`. Silent fallback to `1.0` when sidecar is missing or stale.

**Requirements:** R3.

**Dependencies:** Unit 4 (sidecar DB exists); can run without Unit 3 (PPMI-only signal path works).

**Files:**
- Modify: `scripts/gap_report.py` — `GapStat` adds `forge_signal: float = 1.0`; `_scan_universe` (L158-209) populates from sidecar lookup; sort at L774 becomes `-s.impact * s.forge_signal`; Markdown renderer (~L707) adds `forge_signal` column
- Create: `src/mtg_synergy_graph/forge_oracle/gap_weight.py` (pure function: `compute_forge_weight(subkind, sidecar_conn) -> float`, normalized into `[0, 1.5]` range — default 1.0, boost > 1.0, dampen < 1.0)
- Modify: `tests/test_gap_report.py` (if exists) — add new test file if it doesn't
- Create: `tests/test_forge_oracle_gap_weight.py`

**Approach:**
- New CLI flag on `gap_report.py`: `--forge-oracle-db PATH` (default `data/forge_oracle.db`). Missing file → log a single warning ("forge_oracle.db missing; falling back to volume-only ranking") and proceed with all `forge_signal = 1.0`.
- Stale hash → same silent fallback with a warning (gap_report never hard-fails on oracle issues; it's a rule-authoring tool, should always produce a report).
- `compute_forge_weight(subkind, conn)` computes `max(normalized_pair_ppmi_for_this_subkind_across_cmdrs, normalized_boosterdraft_signal)` per origin FR3. Normalization: divide by 95th-percentile value from the sidecar so `forge_signal` stays in a bounded range; map into `[0.5, 1.5]` so the multiplier can both boost and dampen.
- Ranking change is monotonic under these weights: a signal of 1.0 preserves prior sort order bitwise.
- New Markdown column `forge_signal` rendered as e.g. `1.23` or `—` when `1.0` / absent.

**Patterns to follow:**
- `scripts/gap_report.py` existing argparse + Markdown rendering.

**Test scenarios:**
- Happy path — missing `--forge-oracle-db` file → all signals default to `1.0`, sort order matches pre-change gap_report output bitwise (regression guard).
- Happy path — present sidecar → gaps with high-PPMI subkind pairs rise in the rankings; low-signal gaps demote.
- Edge case — empty sidecar (build ran but produced zero rows) → all signals `1.0`, warning logged.
- Edge case — sidecar has rows but none for this gap's subkind → signal `1.0` (absence is not evidence of low synergy).
- Error path — corrupt sidecar DB (`sqlite3.DatabaseError`) → warning + fallback to `1.0`, gap_report still completes.
- Error path — stale sidecar hash → warning + fallback, gap_report completes.
- Integration — rerun with sidecar present twice → Markdown output byte-identical (determinism).
- Integration — top-10 gap rows change on at least 5 entries vs pre-change ranking (origin Success Criterion 2, informal / manual).

**Verification:**
- Without sidecar: `gap_report.py` output diff vs pre-Unit-6 baseline shows only the new `forge_signal` column with all-`1.0` values; rank order unchanged.
- With sidecar: `gap_report.py` output shows reordered top-10 with at least 5 position changes.

---

- [x] **Unit 7: `bench.py audit --vs-forge-oracle` Kendall-τ sidecar** *(gated by Unit 3 GO verdict)*

**Goal:** Add subcommand reporting Kendall-τ agreement between our live top-30 and BoosterDraftAI's top-30 over the same candidate pool, per commander + aggregate. Top-10 divergence pairs surfaced as rule-proposal seeds.

**Requirements:** R4.

**Dependencies:** Units 3, 5.

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cli.py` — register `--vs-forge-oracle` flag in mutex group (L113), add `"vs_forge_oracle"` mode to `_HANDLERS` (L66) + `_resolve_mode` (L247), add `--forge-oracle-db PATH` arg
- Create: `src/mtg_synergy_graph/bench/forge_oracle_handler.py` — `handle_vs_forge_oracle(args)` mirroring `handle_inspect_gems` shape
- Modify: `src/mtg_synergy_graph/bench/__init__.py` — register handler
- Create: `src/mtg_synergy_graph/forge_oracle/ranking.py` — `forge_topk(cmdr_oracle_id, candidates, conn, k=30)` pairwise-to-ranking aggregator using `pair_scorer.rate_pair`
- Create: `tests/test_bench_vs_forge_oracle.py`
- Modify: potentially extend `src/mtg_synergy_graph/bench/fixture.py` to persist per-commander Forge-agreement history (append-only into `.audit/history.csv`, new column, mirroring `hidden_gem_hit_rate` persistence)

**Approach:**
- For each commander in the golden set pinned fixture:
  - Our top-30 = live `SynergyEngine.page(cmdr, limit=30)` rankings.
  - Forge top-30 = `forge_topk(cmdr, candidate_pool, conn, k=30)` where `candidate_pool` = legal commander-color-compatible cards, scored by summing `rate_pair(cmdr, candidate)` (pair_scorer called once per candidate against the commander).
  - Kendall-τ = `scipy.stats.kendalltau(our_ranks, forge_ranks)` over the intersection of the two top-30s mapped into full-pool ranks. `scipy` is already a transitive dep via… (verify; if not, substitute pure-Python Kendall-τ — ≤ 30 LOC).
- Aggregate Forge-agreement = unweighted mean of per-commander τ values.
- Tracking-only (mirrors `hidden_gem_hit_rate` posture per origin + memory `feedback_hidden_gem_metric.md`). Handler exits 0 even when agreement drops unless `--strict` is passed (deferred — not in MVP).
- Top-10 (cmdr, candidate) divergence pairs = pairs where Forge ranks high but our scorer ranks low — candidates for rule proposals. Sorted by `(forge_rank_low, our_rank_high)` gap.
- Hash check: call `forge_oracle.config.verify_current_or_raise(conn)` at top; stale → exit 2.

**Patterns to follow:**
- `src/mtg_synergy_graph/bench/handlers.py:704-826` (`handle_inspect_gems`) — almost 1:1 structural copy.
- `src/mtg_synergy_graph/bench/handlers.py:749-783` (`--edhrec-db` flag pattern) — missing-file handling.
- `src/mtg_synergy_graph/bench/fixture.py` hidden-gem history CSV — mirror for Forge-agreement history.

**Test scenarios:**
- Happy path — synthetic fixture with known rankings → computed Kendall-τ matches hand-computed value.
- Happy path — Markdown report contains: aggregate number, per-commander table sorted by delta, top-10 divergences table.
- Edge case — commander has fewer than 30 eligible candidates → handler gracefully computes τ over the smaller intersection (documents `n_compared`).
- Edge case — `forge_topk` returns identical top-30 to our ranking → τ = 1.0, report highlights "identity agreement" (sanity check during early tuning).
- Error path — missing `--forge-oracle-db` file → exit 2 with "run `scripts/forge_oracle.py build` first".
- Error path — stale oracle hash → exit 2 with rebuild hint.
- Integration — `bench.py audit --expect-identity` still passes after this unit lands (no inference-path change).
- Integration — history CSV (`.audit/history.csv` or equivalent) gains a new column for aggregate Forge-agreement, appended on each run.

**Verification:**
- `scripts/bench.py audit --vs-forge-oracle` produces a Markdown report on the current golden set.
- `bench.py audit --expect-identity` continues to exit 0.
- History CSV gains the Forge-agreement column; values populated for this run.

---

- [x] **Unit 8: `scripts/forge_oracle.py propose-rules --top N`**

**Goal:** Iterate the gap report's top-N Forge-signal-ranked gaps, delegate each to `scaffold_rule._GENERATORS`, emit a Markdown proposal document listing proposed rule scaffolds pre-filled with the gap's port shape + sample commanders + sample Forge-flagged synergistic candidates.

**Requirements:** R5.

**Dependencies:** Units 4, 5, 6 (gap_report forge_signal wiring). Unit 3 is beneficial but not required — if only PPMI is available, proposals still fire from the PPMI signal.

**Files:**
- Modify: `scripts/forge_oracle.py` — add `propose-rules` subcommand
- Create: `src/mtg_synergy_graph/forge_oracle/proposer.py` — orchestration: run gap_report in-process, take top-N, for each `RuleProposal` with a known generator in `_GENERATORS`, call it to get `ScaffoldArtifacts`, emit
- Create: `tests/test_forge_oracle_proposer.py`

**Approach:**
- In-process call to the internals of `scripts/gap_report.py` (refactor minimally if needed to expose `_scan_universe` + `_propose` + `_eligible` as a function returning `list[RuleProposal]` with `forge_signal` attached; keep CLI entry untouched).
- Take top-N by `forge_signal`-adjusted impact.
- For each proposal: if its template name is in `scaffold_rule._GENERATORS`, call the generator → `ScaffoldArtifacts`; otherwise skip with a logged "no generator registered for template X" note.
- Emit per-proposal section to Markdown: title (port shape), impact + forge_signal + combined score, sample commanders (top 5 by activation count among affected commanders), sample Forge-flagged synergistic candidates (top 5 from sidecar PPMI or BoosterDraftAI), rendered scaffold template (helper + test source previews + integration patch line hints).
- Output: stdout by default; `--out PATH` writes to a file.
- Hash-check: `verify_current_or_raise` at top.

**Patterns to follow:**
- `scripts/scaffold_rule.py` (`_GENERATORS` dict, `ScaffoldArtifacts`) — delegation target.
- `scripts/gap_report.py` (`RuleProposal`) — source of proposals.

**Test scenarios:**
- Happy path — synthetic sidecar + synthetic gap-report state → proposer emits exactly N proposals, all with filled-in scaffold previews.
- Edge case — fewer than N eligible gaps have registered generators → emit as many as possible, note in output how many were skipped for unregistered templates.
- Edge case — `--top 0` → emit header-only report ("no proposals requested"), exit 0.
- Error path — stale oracle hash → exit 2.
- Error path — gap_report internals raise → propagate with a clear "gap report stage failed" wrapper.
- Integration — a proposal's scaffold preview, when manually copy-pasted into `scripts/scaffold_rule.py` invocation, produces a valid new rule file + test file (validated by downstream rule-authoring flow, not auto-tested here).

**Verification:**
- `scripts/forge_oracle.py propose-rules --top 5` emits a readable Markdown report.
- Each listed proposal has a scaffold template preview pre-filled with the gap's port shape.

---

- [x] **Unit 9: Inference-path isolation regression test (grep fence + --expect-identity)**

**Goal:** Mechanically enforce that no inference-path module imports from `forge_oracle/`, and that the inference path is bitwise-identical before and after the feature lands.

**Requirements:** R7.

**Dependencies:** Units 3, 4, 6 (most units — this unit ships alongside the final commits or as a preamble).

**Files:**
- Create: `tests/test_forge_oracle_isolation.py`

**Approach:**
- Test 1: grep fence. For each inference-path file/dir (`src/mtg_synergy_graph/engine.py`, `src/mtg_synergy_graph/universal_scorer.py`, `src/mtg_synergy_graph/graph_engine.py`, `src/mtg_synergy_graph/complement_rules/` recursive), assert no string `forge_oracle` appears in any non-docstring, non-comment source line. (Simple regex suffices — the codebase doesn't have dynamic imports or reflection in these files.)
- Test 2: assert `scripts/recommend.py` does not import `forge_oracle` either.
- Test 3: call `bench.py audit --expect-identity` as a subprocess (or via its Python entry) against the pinned fixture and assert exit 0. Integration test — kept fast by reusing the existing fixture.

**Patterns to follow:**
- `tests/test_seed_files_tracked.py` — subprocess+assert pattern.

**Test scenarios:**
- Happy path — current codebase passes the grep fence.
- Happy path — `bench.py audit --expect-identity` exits 0.
- Regression — if someone adds `from mtg_synergy_graph.forge_oracle import ...` to `universal_scorer.py`, this test fails with a clear "inference path must not import from forge_oracle/" message.
- Error path — if the inference path changes in a way that flips `--expect-identity`, this test fails loudly.

**Verification:**
- `uv run pytest tests/test_forge_oracle_isolation.py` passes.
- `grep -rn "forge_oracle" src/mtg_synergy_graph/{engine,universal_scorer,graph_engine}.py src/mtg_synergy_graph/complement_rules/ scripts/recommend.py` returns zero matches.

---

- [x] **Unit 10: Docs + solution writeup + RULE_HISTORY / RULE_PLANNING / CLAUDE.md updates**

**Goal:** Capture the new pipeline step in rule-authoring docs; date-log the landing in `docs/RULE_HISTORY.md`; extend CLAUDE.md's Common Commands; write a new `docs/solutions/` best-practice doc on the offline-oracle hash-enforcement pattern.

**Requirements:** R3, R4, R5 (documentation impacts).

**Dependencies:** All feature units complete (this is the writeup unit).

**Files:**
- Modify: `docs/RULE_PLANNING.md` — add forge-oracle step to the gap_report → scaffold → audit workflow
- Modify: `docs/RULE_HISTORY.md` — dated entry for the Forge-Second-Oracle landing with before/after gap-ranking diff
- Modify: `CLAUDE.md` — add forge_oracle.py + --vs-forge-oracle + propose-rules lines under "Common Commands"
- Modify: `README.md` — brief Forge-Second-Oracle mention if needed (check README current structure first)
- Create: `docs/solutions/best-practices/offline-oracle-hash-pattern-2026-XX-XX.md` — institutional doc on `OracleConfigInputs` + `compute_oracle_hash` + refuse-to-run pattern (per learnings-researcher recommendation #5)

**Test scenarios:**
- Test expectation: none — documentation only.

**Verification:**
- Docs render cleanly (Markdown lint if repo has one, otherwise visual).
- `docs/RULE_HISTORY.md` entry includes before/after top-10 diff from `gap_report.py` (origin Success Criterion 2).

## System-Wide Impact

- **Interaction graph:** forge_oracle/ module is isolated; only consumed by `scripts/gap_report.py` (soft read, fallback OK), `scripts/bench.py audit --vs-forge-oracle` (strict read), `scripts/forge_oracle.py *` (owns the pipeline). `scripts/recommend.py`, `SynergyEngine`, `universal_scorer`, `graph_engine`, `complement_rules/` must NOT import it (enforced by Unit 9).
- **Error propagation:** Strict consumers (`--vs-forge-oracle`, `propose-rules`) exit 2 on missing/stale sidecar. Soft consumer (`gap_report.py`) logs warning and uses `forge_signal = 1.0` fallback.
- **State lifecycle risks:**
  - Forge checkout diverging from pinned SHA (developer runs `git -C data/forge pull` without re-running oracle build): `OracleVersionMismatchError` surfaces on next strict consumer run; gap_report warns and falls back.
  - Oracle DB built at config `A`, consumed under config `B`: hash mismatch → strict consumers refuse; soft consumer warns.
  - Partial build crash (decks_dir vanishes mid-ingest): build uses a temp DB path + atomic rename at end; on crash the previous DB is preserved.
- **API surface parity:** No other interfaces expose the forge_oracle — it's offline tooling only. No LLM / MCP / HTTP surface; no agent-tool parity consideration.
- **Integration coverage:**
  - Unit 9 asserts inference path is bitwise-identical via `--expect-identity` + grep fence.
  - Unit 4 integration test asserts build is idempotent across reruns.
  - Unit 7 integration test asserts aggregate Forge-agreement is appended to history CSV.
- **Unchanged invariants:**
  - `recommend.py` output bitwise-identical before/after.
  - `SynergyEngine.page` return values bitwise-identical before/after.
  - `complement_rules/` and `universal_scorer.py` unchanged.
  - Existing `hidden_gem_hit_rate` tracking unchanged; Forge-agreement is a new independent axis.
  - Existing `--expect-identity` behavior unchanged (still passes).

## Alternative Approaches Considered

- **Wrap the Forge JVM at inference and query BoosterDraftAI live.** Rejected per origin non-goal — adds a JVM runtime dep and violates the offline invariant.
- **Drop FR1 (Java port), ship PPMI only.** Considered, deferred as fallback plan if Unit 2 spike returns NO-GO. Preserves FR2+FR3 value without the high-risk half.
- **Store oracle rows inside `mtg_synergy.db`.** Rejected in favor of a separate `data/forge_oracle.db` so "no inference-path contact" can be verified by "the main DB doesn't know about oracle tables."
- **Git submodule instead of partial-clone sparse-checkout.** Rejected — forces full Forge history clone on every contributor; existing partial-clone pattern is already working and well-understood.
- **Run the Python PPMI pipeline at inference to refresh as needed.** Rejected — PPMI is static per Forge release; build once, consume many times.
- **Make `gap_report.py` hard-fail on missing sidecar (strict mode).** Rejected — gap_report is the main rule-authoring tool and must always produce output; strict mode lives in `--vs-forge-oracle` and `propose-rules` where it belongs.

## Success Metrics

1. **Rule discovery closes the loop (origin Success Criterion 1).** Within 4 weeks of landing, at least one rule is authored whose gap_report rank was promoted by forge_signal and that subsequently passes NDCG audit with a POSITIVE verdict.
2. **Gap-report top-10 meaningfully reordered (origin Success Criterion 2).** At least 5 of the top-10 gaps differ in position vs pre-change output.
3. **Forge-agreement measurable + stable.** `bench.py audit --vs-forge-oracle` produces aggregate τ value every run; value recorded in history CSV; informal inspection shows τ trending up or flat during normal rule authoring.
4. **Bitwise identity preserved (origin Success Criterion 4).** `bench.py audit --expect-identity` passes before and after the feature lands.
5. **Java port scope discipline (origin Success Criterion 5).** `wc -l src/mtg_synergy_graph/forge_oracle/pair_scorer.py < 500`.

## Dependencies / Prerequisites

- Plan 001 (`unified-eval-harness`) — LANDED. `bench.py audit` host infrastructure, pinned fixture, history CSV.
- Plan 002 (`typed-port-graph-substrate`) — LANDED. Canonical `port_nodes.subkind` as PPMI join key.
- `scipy` for Kendall-τ — transitive dep check required in Unit 7; if absent, swap to pure-Python ≤30 LOC Kendall-τ.
- Upstream Forge `Card-Forge/forge.git` remains available as a partial clone source.

## Risk Analysis & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Java port scope blowup (BoosterDraftAI needs GameState/PlayerAI) | Med | High | Unit 2 spike is the gate; NO-GO drops Unit 3 and scopes plan to PPMI-only |
| Precon corpus too small for stable PPMI on rare pairs | Low-Med | Med | `decks_count >= 3` threshold + Laplace smoothing; BoosterDraftAI signal fills the gap if Unit 3 lands |
| Forge checkout SHA drifts (developer runs `git pull` in `data/forge/`) | Med | Low | `OracleVersionMismatchError` surfaces cleanly; refuse-to-run in strict consumers; warn-and-fall-back in gap_report |
| Oracle contaminates inference path via accidental import | Low | High | Unit 9 grep fence + existing `--expect-identity` audit = belt-and-suspenders |
| `.dck` parser edge cases (DFCs, adventures, split cards, foreign names) | Med | Low | Unit 4 edge-case test coverage; unknown-card skip-with-counter posture; name resolution reuses battle-tested `_build_oracle_id_resolver` |
| Oracle overfits to Forge designer biases (undervalues modern archetype tech) | Med | Low | All proposals human-reviewed via existing `scaffold_rule → _audit_rule_impact` flow; no auto-accept |
| `scripts/forge_oracle.py build` is slow enough to block iteration | Low | Low | One-time cost per Forge bump; idempotent; if > 5 min, add `--cached` shortcut in implementation |
| PPMI smoothing constant `k` chosen arbitrarily; sensitivity unclear | Med | Low | Hash-gated: changing `k` flips `config_hash`, forces rebuild; value can be tuned post-landing without breaking contracts |
| Kendall-τ choice of aggregation (weighted vs unweighted) materially moves the metric | Low | Low | Track unweighted mean; swap to weighted if post-landing investigation shows top-heavy bias |

## Phased Delivery

**Phase 1: Foundation + gate (Units 1-2).**
Extend sparse-checkout, pin SHA, recon spike. **Do not proceed until user approves Unit 2 verdict.**

**Phase 2: Data pipeline (Units 3-5).** *(Unit 3 gated by Unit 2 GO)*
Java port (if GO), PPMI ingest, oracle config hash. Land as separate commits where possible for independent audit.

**Phase 3: Consumer integration (Units 6-8).**
`gap_report.py` re-ranking, `bench.py audit --vs-forge-oracle`, `forge_oracle.py propose-rules`. Unit 6 can land before Units 7-8 if Unit 3 is delayed; gap_report works with PPMI-only signal.

**Phase 4: Hardening + docs (Units 9-10).**
Isolation regression test + writeups. Unit 9 should land alongside Phase 3 commits so CI catches any accidental inference-path contamination immediately.

## Documentation Plan

- `docs/RULE_PLANNING.md` — new step in the pipeline documented.
- `docs/RULE_HISTORY.md` — dated landing entry.
- `CLAUDE.md` — Common Commands block extended.
- `docs/FORGE_ORACLE.md` — new (Unit 1): how to refresh checkout, bump SHA.
- `docs/spikes/2026-04-23-boosterdraft-port-feasibility.md` — new (Unit 2): recon verdict.
- `docs/solutions/best-practices/offline-oracle-hash-pattern-2026-XX-XX.md` — new (Unit 10): institutional pattern doc.

## Operational / Rollout Notes

- **One-time build cost:** `scripts/forge_oracle.py build` is expected to take < 2 min on a modern laptop (precon deck count in the low hundreds × ~100 cards each × O(N²) pair extraction is bounded). No long-running ingest risk.
- **Disk:** Forge sparse-checkout grows by `forge-ai/` + precon decks. Estimated < 50 MB additional vs current partial clone.
- **Oracle DB size:** ~10 k distinct port signatures × sparsity ~0.5% = ~500 k rows × ~80 bytes each = ~40 MB. Fits in SQLite trivially. Not committed to git (regenerable); `.gitignore` `data/*` already excludes.
- **CI implications:** CI doesn't need `forge_oracle.db` for the main test suite (gap_report has graceful fallback; `--vs-forge-oracle` is a manual invocation, not part of `pytest`). Isolation test (Unit 9) runs in CI without needing the oracle DB.
- **Bumping the Forge SHA:** `scripts/forge_oracle.py upgrade` regenerates against a new Forge version, updates `version.txt`, rebuilds DB, emits a diff report (Forge agreement delta + new/removed PPMI pairs). Merge signal for the SHA bump.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-21-forge-second-oracle-requirements.md](../brainstorms/2026-04-21-forge-second-oracle-requirements.md)
- Seed idea: [docs/ideation/2026-04-21-recommendation-model-ideation.md](../ideation/2026-04-21-recommendation-model-ideation.md) (Survivor 4)
- Dependency plan: [docs/plans/2026-04-22-001-feat-unified-eval-harness-plan.md](2026-04-22-001-feat-unified-eval-harness-plan.md)
- Dependency plan: [docs/plans/2026-04-22-002-feat-typed-port-graph-substrate-plan.md](2026-04-22-002-feat-typed-port-graph-substrate-plan.md)
- Pattern reference: [docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md](../solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md) (meta-principle: mechanical enforcement via hash; inference-path specifics do not apply)
- Artifact-tracking precedent: [docs/solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md](../solutions/build-errors/gitignore-negation-under-ignored-parent-2026-04-23.md)
- Upstream source: `https://github.com/Card-Forge/forge` — vendored at `data/forge/` (partial clone, SHA `ed97d9bb` at planning time)
- Memory alignment: `memory/reference_forge_java_engine.md`, `memory/feedback_forge_data_direct.md`, `memory/feedback_edhrec_not_goal.md`, `memory/feedback_audit_every_change.md`, `memory/project_forge_pipeline_expansion.md`, `memory/feedback_hidden_gem_metric.md`
