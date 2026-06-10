---
title: "feat: Divergence forensics — per-miss failure taxonomy + metric sidecars"
type: feat
status: active
date: 2026-06-10
origin: docs/brainstorms/2026-06-10-divergence-forensics-requirements.md
---

# feat: Divergence Forensics — Per-Miss Failure Taxonomy + Metric Sidecars

## Overview

Add `bench.py audit --forensics`: a read-side standing instrument that classifies every (commander, missed EDHREC label card) pair into mechanical failure buckets, surfaces per-commander graded NDCG / raw DCG sidecars with a reconciliation assertion, reports justified divergence, appends a provenance-stamped row to a new `.audit/forensics_history.csv`, and (one-off) measures the EDHREC tiebreaker credit in the current sort key. Zero scoring-path changes; `--expect-identity` stays bitwise green.

## Problem Frame

NDCG@30 vs EDHREC has plateaued at ~0.256 and every prior accuracy lever was tried blind against that opaque aggregate (see origin: docs/brainstorms/2026-06-10-divergence-forensics-requirements.md). The instrument converts the plateau into sized failure classes (vocabulary vs calibration vs near-miss vs data gaps) that decide which queued scoring reform gets built next, while keeping the project's hidden-gem north star visible in every tracked row.

## Requirements Trace

(IDs from the origin document.)

- R1. `--forensics` classifies every miss into exactly one bucket (NEAR_MISS / OUTRANKED / FILTERED / DATA_GAP / NO_RULES — FILTERED added during planning, see Key Technical Decisions); miss universe = top-30 graded labels closed under boundary ties; ranks from a live `engine.page()` pass.
- R2. Report: aggregate + per-commander bucket counts, worst-divergence leaderboard, OUTRANKED rank-quantile + rule-family breakdown, displacer profiles; md (default `.audit/forensics.md`) + `--format json`.
- R3. NO_RULES drill-down: frequency-ranked shared `(node_kind, subkind)` port shapes.
- R4. SQL over tensor + labels, two-connection pattern, no live-rescoring beyond the single `page()` pass per commander, runtime comparable to a standard audit, `--expect-identity` unaffected.
- R5. Per-commander NDCG@30 on canonical validate-path labels + reconciliation assertion vs a same-run canonical recompute.
- R6. Raw DCG@30 sidecar on the same labels.
- R7. Append-only `forensics_history.csv` with provenance (config_hash, fixture SHA-256, EDHREC snapshot digest, commit_sha) + gem-axis column; `--trend forensics` reader with boundary markers.
- R8. One-off tiebreaker ablation with bracketing replacement keys; RULE_HISTORY flag entry if upper bound > 0.01 NDCG.
- R9. Justified-divergence view: all-sections reference set, gate-margin stratification, pass-rate reporting; plus `listed_nonpositive` count (planning addition, not in origin R9 — see Resolved During Planning; it is a separate reported count and never subtracts from bucket sums).

## Scope Boundaries

- No scoring changes (the live pass *consumes* `engine.page()`; the R8 ablation re-sorts in memory and never lands in `page()`).
- No gate changes — all metrics tracking-only; promotion needs its own escalation cycle (FR6 discipline).
- 100-commander fixture default in v1; 500-set extension is a follow-up.
- Light drill-down only (R3 frequency list); no demand-side clustering or rule mining.
- No production sort-key change from R8.

### Deferred to Separate Tasks

- 500-commander forensics run: needs a tensor-populating pass; future iteration.
- Any scoring reform funded by the bucket proportions: separate plans per reform.

## Context & Research

### Relevant Code and Patterns

- CLI wiring: `src/mtg_synergy_graph/bench/cli.py` — mutually-exclusive mode group (`_build_parser`), `_resolve_mode`, `_HANDLERS` + `src/mtg_synergy_graph/bench/_stubs.py` + `register()` calls in `src/mtg_synergy_graph/bench/__init__.py`; companion-flag stderr warnings ("has no effect without --X"); `--format` resolved per mode; exit codes 0 clean / 1 drift / 2 usage-or-stale-input.
- Live ranking + NDCG: `src/mtg_synergy_graph/validate.py` — `_run_one` (the canonical pass), `edhrec_labels_for_commander(conn, commander, grade_floor=0.0)`, `compute_ndcg` (gains `2^rel − 1`; absent label → 0; empty labels → 0.0). Aggregate = mean over ALL entries with zero-label commanders contributing 0.0.
- `engine.page()`: `src/mtg_synergy_graph/engine.py` — `limit=1_000_000` returns the full filtered ranking; legality filter chain (color identity, `legal_commander`, non-EDH types, commander self-exclusion) runs BEFORE sorting; sort key `(-total, cmc→99.0, edhrec_rank→UNRANKED_EDHREC_SENTINEL, name)`.
- Tensor reads: `src/mtg_synergy_graph/bench/rule_ops.py` — always `WHERE ... AND config_hash = ?`; stale-tensor exit-2 precedent in `src/mtg_synergy_graph/bench/optimize.py`.
- History CSV: `src/mtg_synergy_graph/bench/history.py` — `CSV_FIELDS` tuple + frozen row dataclass, append-mode `fh.tell() == 0` header-once, OSError→stderr-warn-never-fail, `fmt_float`; trend handler `handle_trend_hidden_gems` in `src/mtg_synergy_graph/bench/handlers.py`.
- Per-commander report template: `src/mtg_synergy_graph/bench/per_commander_ndcg.py` (frozen row dataclass, pure render fns, DB-touching compute fn, handler with exit-2 existence checks).
- Gem gate: `src/mtg_synergy_graph/bench/hidden_gems.py` — `plausibility()` (N_rules ≥ 2 OR contribution > cohort median), DB-agnostic `(candidate, rule_id, contribution)` tuples, public batched entry `hidden_gem_hit_rate_for_commander`.
- Port shapes: `port_nodes` view auto-created by `open_db()` (`src/mtg_synergy_graph/port_graph/projection.py`); access template `handle_unknowns` in `handlers.py`.
- Two-connection pattern (NOT ATTACH): `src/mtg_synergy_graph/bench/audit.py:50-62`, `per_commander_ndcg.py:171-173` — `open_db(args.db)` for synergy.db + `sqlite3.connect` with `row_factory=sqlite3.Row` for tags.db.
- Output discipline: `_write_default_output` in `bench/audit.py` (`.audit/last.md`, OSError degrades to warning); `.audit/` fully gitignored.

### Institutional Learnings

- `docs/solutions/best-practices/bm25-idf-null-result-2026-05-04.md` — build the reporter as a bundled-but-separable unit (own module, own commit) so it survives any experiment it informs; misses cluster by rule-family dominance.
- `docs/solutions/best-practices/infrastructure-without-scoring-activation-2026-04-24.md` — never compare against pinned keys that may be absent in old fixtures; filter on key presence.
- `docs/solutions/best-practices/read-sibling-solutions-before-improvement-levers-2026-05-05.md` — history rows must be self-explanatory; unattributed rows are documented pollution.
- `docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md` — a tool whose purpose IS a sidecar input exits 2 with a rebuild hint on missing/stale input; no silent degrade.
- `docs/solutions/best-practices/verify-from-stored-config-not-code-defaults-2026-04-23.md` — provenance sourced from stored artifact metadata, not recomputed defaults.
- `docs/solutions/best-practices/rule-quality-gates-2026-04-24.md` — tensor views are golden-set-bubble-bound; the report must state the bubble boundary.
- `docs/solutions/test-failures/forge-oracle-ci-git-checkout-stub-2026-04-23.md` — `data/*` is gitignored and absent in CI; tests must be CI-safe from the first commit (tmp_path synthetic DBs).
- `docs/solutions/best-practices/optimizer-perf-profile-2026-04-30.md` — overridable output/history path flags enable sentinel-safe testing; assert report determinism.

### External References

- None needed — strong local patterns throughout (external research skipped per Phase 1.2).

## Key Technical Decisions

- **Two connections instead of ATTACH** (deviation from origin R4 mechanism, same intent): the repo has zero ATTACH usage; every existing cross-DB consumer opens synergy.db via `open_db()` and tags.db via `sqlite3.connect`. Classification joins happen in Python over per-connection query results. This also dissolves the origin's stale-duplicate-tables risk (no shared SQL namespace) and the conftest ATTACH question.
- **Full-ranking live pass**: one `engine.page(commander, offset=0, limit=1_000_000)` call per commander supplies ranks for every legal candidate; "unranked" is precisely "absent from this filtered full ranking". The R8 ablation re-sorts this same in-memory list (identical filters and NULL sentinels by construction).
- **FILTERED bucket added** (planning addition to the origin's four): the tensor is pre-filter, so a label card can hold tensor rows yet be dropped by `page()`'s legality chain — matching no origin bucket and violating "exactly one bucket". FILTERED sits between OUTRANKED and DATA_GAP. `page()` never reports *why* it dropped a card, so the classifier re-evaluates the engine's exact predicate chain from `cards` rows in documented precedence — color-identity superset, `legal_commander = 0`, `NON_EDH_CARD_TYPES` membership (import the frozenset from `engine`, never copy it), EMPTY `card_types`, name ∈ commander set — yielding reason codes `color_illegal` / `not_legal` / `non_edh_type` / `empty_types` / `is_commander`, plus a `filter_reason_unknown` fallback for tensor-rows-present + legal + unranked cards (counted explicitly as a tensor-staleness diagnostic). A nonzero FILTERED count is itself a diagnostic (label/name-mapping or data issues); a nonzero `is_commander` count almost certainly indicates a normalization bug and gets its own test.
- **One normalization boundary, applied three times**: a single name-normalization helper used at labels↔ranking, labels↔tensor, and labels↔cards joins, so MDFC/split-face mismatches can't silently misfile a ranked card as DATA_GAP. Unresolved names → DATA_GAP `card_absent` with a `name_unmatched` flag.
- **Reconciliation against a same-run recompute, never the stored 0.256**: pinned ndcg30 reflects pin-time labels; tags.db refreshes legitimately move the live figure. Mechanism: the recompute issues its own `engine.page(commander, limit=30)` on the SAME engine instance (hits `_score_cache` — no second scoring pass, so R4 holds) and runs `compute_ndcg` on that independently obtained window; comparison uses UNROUNDED values on both sides (`_run_one` rounds to 6 dp — do not mix conventions), epsilon 1e-6. Honest framing: on a shared engine this is a **bookkeeping-consistency check** (it catches rank-extraction/label-handling bugs in forensics, which the planted-divergence test targets), not an independent production-faithfulness proof; for independence, a sampled check (~5 commanders) runs `validate._run_one` on a separately constructed `SynergyEngine` and must agree. Two denominators reported explicitly: canonical (all fixture commanders, zero-label → 0.0) — the one the reconciliation assertion uses; exclusion-based (zero-label commanders listed separately) for bucket proportions.
- **Gem-axis column computed in-run from the live top-30** via `hidden_gem_hit_rate_for_commander` (tensor contributions + HS-top-30 reference): self-contained, no dependency on a prior audit invocation; column named `gem_rate_forensics` to flag that its top-30 source (production `page()` order) differs marginally from the audit's pre-filter top-30. **Deliberate two-reference-set design:** the gem column keeps the HS-top-30 reference for continuity with the existing gem-axis series (origin R7's stated purpose), while R9's justified-divergence view uses the all-sections label set (origin R9's explicit requirement). They are different views answering different questions; the report says so where both appear.
- **EDHREC snapshot digest** = SHA-256 over `(COUNT(*), MAX(rowid))` of `edhrec_card_synergy` — cheap, stable, sufficient to detect refreshes; documented as new small infrastructure.
- **Sibling CSV with its own reader**: `forensics_history.csv` + `ForensicsHistoryRow` + dedicated field tuple; never shares `CSV_FIELDS` (strict-header reader breakage documented in origin). `--trend forensics` groups rows by (config_hash, snapshot_digest) and prints a boundary marker instead of computing deltas across boundaries.
- **Strict-consumer failure mode**: all preconditions (tensor exists + config-hash match, tags.db present, fixture present, every fixture entry single-commander) are checked before anything is written; failure → exit 2 with a rebuild hint, no partial `.audit/forensics.md`, no history row. Config-hash alone does NOT catch a cardsfolder re-import (it covers scoring config, not card data), so a synergy.db content digest (COUNT + MAX(rowid) over `cards`/`card_ports`, mirroring the EDHREC digest) is cross-checked against a value recorded at tensor-write time where available — at minimum a loud warning when synergy.db appears newer than the pin — and recorded as a provenance column.

## Open Questions

### Resolved During Planning

- Live-pass depth/mechanism: full ranking via `page(limit=1_000_000)`, one engine + one connection pair shared across the run.
- ATTACH vs two connections: two connections (repo convention).
- Snapshot digest derivation: content digest over row count + max rowid.
- conftest `*.db` sentinel: no exception needed — tests use `tmp_path` DBs exclusively.
- Zero-label commanders: excluded from bucket aggregates, listed separately; included at 0.0 in the reconciliation denominator (matches canonical aggregate).
- R9 negative-synergy listings: third per-commander count `listed_nonpositive` (membership check against the unfloored label set), distinct from justified/unjustified divergence.
- Multi-commander (partner) fixture entries: out of scope v1; fail loud if encountered.

### Deferred to Implementation

- Exact name-normalization rules (front-face split on " // ", punctuation folding): tune against the 32 known unmatched names once the join is running; the boundary and flag semantics are fixed here.
- `netted_zero` prevalence (contrib == 0.0 rows dropped by `universal_scorer`): quantify in-run; add the sub-tag only if material. The quantification must also cover the OUTRANKED side: a ranked card with positive live score but zero tensor rows may be cancellation, not staple bonus — report that count and split `staple_only` if it is nonzero.
- Gate-margin stratification bin edges for R9: choose after seeing the real distribution of N_rules / contribution-vs-median ratios; the reported quantities are fixed here.
- Markdown layout details of `.audit/forensics.md` and the exact JSON nesting: follow `per_commander_ndcg.py` / `_render_*_json` precedents.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Bucket classification per (commander, missed label card), after one full live ranking per commander:

```
miss universe = top-30 labels by synergy (grade_floor=0.0), closed under ties at the 30th value
for each miss (normalized name):
    rank = position in live full ranking (None if absent)
    if rank is not None and rank <= 30:      -> not a miss (sanity check; excluded upstream)
    elif rank in 31..60:                     -> NEAR_MISS
    elif rank is not None:                   -> OUTRANKED        (sub-tag staple_only if no tensor rows)
    elif has tensor rows for this commander: -> FILTERED
         reason = first failing predicate of page()'s drop chain, re-evaluated from cards:
                  color_illegal | not_legal | non_edh_type | empty_types | is_commander
                  (none failed -> filter_reason_unknown : tensor-staleness diagnostic)
    elif card absent / no ports / >50% UNKNOWN ports
                                             -> DATA_GAP          (reason: card_absent[+name_unmatched] |
                                                                   no_ports | unknown_ports)
    else                                     -> NO_RULES
```

Data flow: `tags.db` (labels, HS sections) + `synergy.db` (live ranking via engine, tensor, port_nodes, cards) → in-memory classification → renderers (md/json) + history append.

## Implementation Units

- [ ] **Unit 1: Forensics core — classification engine and data model**

**Goal:** `bench/forensics.py` with frozen dataclasses (`MissRecord`, `CommanderForensics`, `ForensicsReport`), the name-normalization helper, label/miss-universe loader (tie-closed top-30), the live full-ranking pass, and the five-bucket classifier with sub-tags.

**Requirements:** R1, R4 (partial)

**Dependencies:** None

**Files:**
- Create: `src/mtg_synergy_graph/bench/forensics.py`
- Test: `tests/bench/test_forensics_classify.py`

**Approach:**
- Pure classification functions over plain data (ranking list, tensor-row presence map, port-shape map) — DB access isolated in one compute function, mirroring `hidden_gems.py`'s DB-agnostic discipline.
- Precondition checks (tensor config-hash vs `compute_config_hash()`, tags.db existence, fixture existence, every fixture entry single-commander, synergy.db freshness digest vs pin) raise/exit 2 before any computation.
- The live pass captures, alongside each ranked name, the production sort-key components: `total_score` from the `Recommendation`, plus `cmc` and `edhrec_rank` re-read from `cards` with the engine's sentinels (NULL cmc → 99.0; NULL rank → `UNRANKED_EDHREC_SENTINEL`, imported from `engine`, never duplicated). `Recommendation` itself carries neither — Unit 6 depends on this capture.
- `engine._score_cache` is cleared after each commander's ranking is extracted — a shared engine otherwise retains all 100 full score caches in one process (multi-GB exposure no existing consumer has).
- Name normalization v1 contract: exact match, then front-face split on `" // "`; everything else is flagged `name_unmatched` rather than guessed. Tuning beyond this contract is the deferred item — the contract itself is fixed so two implementers produce the same `card_absent` counts.
- Commander list from the pinned fixture entries (`_load_commanders_from_fixture` precedent).

**Patterns to follow:** `bench/hidden_gems.py` (pure-core + DB shell), `bench/rule_ops.py` (hash-filtered tensor reads), `validate.py::_run_one` (live pass shape).

**Test scenarios:**
- Happy path: synthetic commander with one miss per bucket — each lands in exactly its bucket; proportions sum to 100%.
- Edge case: synergy tie spanning rank 30/31 → both tie members in the miss universe (tie closure).
- Edge case: zero-label commander → excluded from bucket aggregates, listed in the skip list.
- Edge case: zero-miss commander → `misses=0`, proportions rendered as null/—, excluded from leaderboard.
- Edge case: label card ranked in our top-30 → not a miss (excluded upstream).
- Error path: tensor config_hash ≠ live config → exit 2 with re-pin hint, nothing written.
- Error path: missing tags.db → exit 2 with rebuild hint.
- Error path: fixture contains a multi-commander (partner) entry → exit 2 with "partners not supported in v1" message.
- Integration: card with tensor rows but color-illegal for the commander → FILTERED `color_illegal` (not DATA_GAP/NO_RULES); reason derivation uses the imported `NON_EDH_CARD_TYPES` frozenset and covers `empty_types`; a legal+tensor-rows+unranked card → FILTERED `filter_reason_unknown`.
- Integration: a label name matching the commander itself after normalization → FILTERED `is_commander` (normalization-bug canary).
- Integration: ranked card with zero tensor rows → OUTRANKED `staple_only`.
- Integration: unmatched EDHREC name → DATA_GAP `card_absent` + `name_unmatched` flag.

**Verification:** classifier is deterministic for identical inputs (repeat-run equality test); every synthetic miss classified exactly once.

- [ ] **Unit 2: Metric sidecars — per-commander NDCG, raw DCG, reconciliation assertion**

**Goal:** Compute per-commander NDCG@30 (validate-path labels) and raw DCG@30 from the Unit 1 live rankings; assert the aggregate reconciles with a same-run canonical recompute within 1e-6, failing exit-2 with the first divergent commander named.

**Requirements:** R5, R6

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/bench/forensics.py`
- Test: `tests/bench/test_forensics_metrics.py`

**Approach:**
- Reuse `validate.compute_ndcg` and `edhrec_labels_for_commander` directly — no new label convention; raw DCG = the same gain/discount sum without the ideal normalizer.
- Reconciliation mechanism per Key Decisions: independent `engine.page(limit=30)` window on the same engine (cache hit, no second scoring pass), unrounded comparison, epsilon 1e-6; the assertion uses the CANONICAL denominator (all commanders, zero-label → 0.0). Bucket reporting uses the exclusion-based denominator. The independent sampled check (~5 commanders on a separately constructed engine via `validate._run_one`) provides the production-faithfulness evidence the shared-engine assertion cannot.

**Patterns to follow:** `bench/per_commander_ndcg.py` (row dataclass + pure renderers), `validate.py` (metric functions).

**Test scenarios:**
- Happy path: two synthetic commanders → per-commander NDCG matches a hand-computed validate-path value; aggregate reconciles.
- Edge case: zero-label commander contributes 0.0 to the canonical denominator but is absent from bucket aggregates.
- Error path: planted divergence (mismatched ranking) → reconciliation failure exits 2, names the commander, writes no report/history row.
- Happy path: raw DCG ≥ 0 and equals NDCG × ideal-DCG for a known small case.

**Verification:** reconciliation assertion demonstrably catches a planted ordering bug (bookkeeping-consistency regression test); the sampled independent-engine check agrees on the chosen commanders (production-faithfulness evidence).

- [ ] **Unit 3: Justified-divergence view (R9)**

**Goal:** Per-commander `justified_divergences` count + gate-margin stratification + gate pass-rate + `listed_nonpositive`, using the wider all-sections reference set with the existing plausibility-gate constants.

**Requirements:** R9

**Dependencies:** Unit 1

**Files:**
- Modify: `src/mtg_synergy_graph/bench/forensics.py`
- Test: `tests/bench/test_forensics_justified.py`

**Approach:**
- Thin wrapper passing the all-sections label-name set as the reference to the gate logic; do NOT reuse `hidden_gem_hit_rate_for_commander`'s HS-top-30 reference (documented mismatch). Gate constants unchanged (advisory only — never a gate, per FR6 discipline).
- Stratification reports the distribution of N_rules_firing and contribution/median ratio for justified picks; pass-rate of 100% is rendered with an explicit "gate too loose to discriminate" annotation.

**Patterns to follow:** `bench/hidden_gems.py` public functions and tuple-based contributions interface.

**Test scenarios:**
- Happy path: divergent pick passing the gate → counted justified; failing → unjustified.
- Edge case: pick listed by EDHREC at synergy ≤ 0 → counted under `listed_nonpositive` (a separate reported count; bucket sums and divergence annotations unchanged). Note: requires the unfloored label set — `edhrec_labels_for_commander` filters above `grade_floor`, so this check queries the table without the floor.
- Edge case: all divergent picks pass the gate → pass-rate 100% annotation present.
- Integration: justified counts annotate (`N (M justified)`) without changing bucket sums (proportions still 100%).

**Verification:** R9 outputs derive from Unit 1's live top-30 + tensor contributions + the all-sections label set loaded from the existing tags.db connection; no additional DB connections are opened. Repeat-run equality confirms determinism.

- [ ] **Unit 4: CLI wiring and report rendering**

**Goal:** `--forensics` mode flag, handler, markdown/JSON renderers, default output `.audit/forensics.md`, leaderboard, OUTRANKED quantile + rule-family breakdown, displacer profiles, NO_RULES port-shape list, golden-set-bubble caveat line.

**Requirements:** R2, R3, R4

**Dependencies:** Units 1–3

**Files:**
- Modify: `src/mtg_synergy_graph/bench/cli.py`, `src/mtg_synergy_graph/bench/_stubs.py`, `src/mtg_synergy_graph/bench/__init__.py`, `src/mtg_synergy_graph/bench/forensics.py`
- Test: `tests/bench/test_handle_forensics.py`

**Approach:**
- New entry in the mutually-exclusive mode group + `_resolve_mode` branch + `_HANDLERS` stub + `register()` call; handler signature `Callable[[Namespace], int]`; overridable `--output` and `--forensics-history` paths (sentinel-safe testing); OSError on default-path write degrades to stderr warning.
- Exit codes: 0 for a successful forensics run (read-only diagnostic — findings are not errors); 2 on usage/stale-input (missing fixture/tags.db, config-hash mismatch, reconciliation failure). Exit 1 (drift) is reserved for the main audit verdict and is never emitted by `--forensics`.
- Displacer profile, two levels: (a) a per-commander table of rule-family contribution shares for the cards occupying that commander's live top-30, and (b) one aggregate profile (mean family share across commanders) — so readers see both what we mis-ranked (misses) and what we over-ranked (displacers) at both granularities.
- Report header states the golden-set bubble boundary (which commanders/rules are tensor-visible).

**Patterns to follow:** `handle_unknowns` / `handle_inspect_gems` (handlers.py), `_write_default_output` (audit.py), `_render_*_{markdown,json}` pairs.

**Test scenarios:**
- Happy path: handler over a seeded tmp_path DB pair → exit 0, markdown contains all sections, `--format json` parses and mirrors counts.
- Edge case: empty miss set everywhere → report renders with zeros/—, exit 0.
- Error path: missing fixture → exit 2 before any output file is created.
- Integration: `--output tmp_path/report.md` writes there and skips `.audit/`.
- Integration: full `bench.py audit --expect-identity` still passes bitwise after the module lands (read-only proof).

**Verification:** running `--forensics` twice on identical inputs produces byte-identical reports.

- [ ] **Unit 5: Forensics history CSV + `--trend forensics`**

**Goal:** Append-only `.audit/forensics_history.csv` (`ForensicsHistoryRow`: timestamp, commit_sha, config_hash, fixture_sha256, edhrec_snapshot_digest, bucket proportions, aggregate NDCG, raw DCG, `gem_rate_forensics`, n_commanders, n_skipped) and a `--trend forensics` reader with (config_hash, snapshot) boundary markers.

**Requirements:** R7

**Dependencies:** Unit 4 (strictly sequential — both modify `cli.py`; land Unit 4 first, never develop in parallel)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/forensics.py`, `src/mtg_synergy_graph/bench/cli.py` (`--trend` choices + dispatch), `src/mtg_synergy_graph/bench/handlers.py`
- Test: `tests/bench/test_forensics_history.py`

**Approach:**
- Mirror `history.py` discipline exactly: own field tuple, `fh.tell() == 0` header-once, write-errors degrade to stderr warnings, `fmt_float` rendering, malformed-row-tolerant reader. Never share or extend `CSV_FIELDS`.
- Provenance read from stored artifacts (fixture file hash, tensor's stored config_hash) per the verify-from-stored-config learning; gem column computed in-run (Key Decisions).
- Trend reader prints a boundary marker row when config_hash or snapshot digest changes; no deltas across boundaries; duplicate same-config rows are accepted and stated as such.

**Patterns to follow:** `bench/history.py`, `handle_trend_hidden_gems` + `_render_trend_{csv,md,json}`.

**Test scenarios:**
- Happy path: first run creates file with header; second appends one row, header not duplicated.
- Edge case: malformed row in CSV → reader skips with warning, remaining rows render.
- Integration: trend output inserts a boundary marker between rows with different snapshot digests and computes no delta across it.
- Error path: unwritable history path → stderr warning, handler still exits 0.

**Verification:** a history row is written only when the full run succeeded (no row on reconciliation failure).

- [ ] **Unit 6: Tiebreaker ablation (R8, one-off mode)**

**Goal:** `--forensics --ablate-tiebreak` re-sorts each commander's in-memory full ranking under the weak (`(-score, name)`) and strong (`(-score, cmc, name)`) keys, reports the bracketed NDCG@30 delta range vs the production key, and prints the RULE_HISTORY flag text when the upper bound exceeds 0.01.

**Requirements:** R8

**Dependencies:** Unit 2 (metric machinery), Unit 4 (CLI)

**Files:**
- Modify: `src/mtg_synergy_graph/bench/forensics.py`, `src/mtg_synergy_graph/bench/cli.py`
- Test: `tests/bench/test_forensics_ablation.py`

**Approach:**
- Re-sorts Unit 1's captured per-candidate sort-key tuples (`total_score` from the ranking; `cmc`/`edhrec_rank` re-read from `cards` with engine sentinels — `Recommendation` carries neither, so "same objects" does NOT hold; the capture in Unit 1 is the load-bearing prerequisite). Never touches `engine.page()`. Companion-flag stderr warning when used without `--forensics`.
- Mandatory self-check before any delta is reported: re-sorting by the reconstructed production key `(-score, cmc, edhrec_rank, name)` must reproduce `page()`'s emitted order exactly for every commander; abort exit 2 otherwise (catches sentinel/NULL drift — `bench/optimize.py::score_commander_from_complements` is the sanctioned precedent for this reconstruction).
- Output is a section in the forensics report; the ≥0.01 flag emits ready-to-paste RULE_HISTORY markdown (the human commits it — keeping the run read-only).

**Patterns to follow:** `bench/optimize.py::score_commander_from_complements` (sanctioned production-faithful sort precedent), companion-flag warning table in `cli.py`.

**Test scenarios:**
- Happy path: synthetic score ties where edhrec_rank decides order → weak-key delta nonzero, strong-key delta smaller; range ordered correctly.
- Edge case: no score ties → both deltas exactly 0.0.
- Happy path: planted large tie-credit → flag text emitted with the measured range.
- Integration: ablation section appears only with the flag; absent otherwise.

**Verification:** the production-key reconstruction self-check passes on all commanders before deltas are emitted; deltas are pure functions of the Unit 1 capture (no extra scoring pass); repeat runs byte-identical.

## System-Wide Impact

- **Interaction graph:** new bench mode only; touches `cli.py`/`__init__.py` registration tables and adds one module. No imports into scoring modules; pre-commit bench hook not triggered (no `complement_rules/`, `universal_scorer.py`, `graph_engine.py`, `embeddings/`, or `scoring_weights.json` edits).
- **Error propagation:** strict-consumer preconditions exit 2 before writing; partial artifacts never land; history row only on full success.
- **State lifecycle risks:** `.audit/forensics_history.csv` is append-only, gitignored, regenerable; duplicate rows from repeated identical runs are accepted and documented.
- **API surface parity:** `--trend` gains a `forensics` choice; `--format json` parity with md is a per-unit test obligation.
- **Integration coverage:** `--expect-identity` bitwise pass after landing is the read-only proof; the reconciliation assertion is the bookkeeping-consistency proof, and the sampled independent-engine check is the production-faithfulness evidence.
- **Unchanged invariants:** `engine.page()` sort key and filters, `CSV_FIELDS`/`history.csv` schema, `compute_config_hash` inputs, all existing handler behaviors.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Full-ranking `page()` pass per commander is slower than expected | It's one call per commander on cached scoring; profile before optimizing (optimizer-perf learning: estimates were off 14–25×); runtime target is "comparable to a standard audit", not <30s |
| Shared engine accumulates 100 full score caches in one process (memory, not just runtime) | `engine._score_cache` cleared after each commander's ranking is extracted (Unit 1 approach); no existing 100-commander consumer retains caches |
| Name normalization misses cases beyond the 32 known | `name_unmatched` flag makes residual mismatches visible in the report instead of silently inflating DATA_GAP |
| OUTRANKED still dominates without discriminating | Rank-quantile + rule-family + displacer profiles are in v1 precisely for this; if proportions are still too close, the documented next step is the 500-set extension |
| Gem gate passes ~everything (R9 degeneracy) | Pass-rate + stratification reported; 100% pass-rate explicitly annotated as a finding about the gate |
| tags.db refresh mid-series invalidates trend comparisons | Snapshot digest column + boundary markers in the trend reader |
| CI lacks `data/*.db` | All tests build synthetic tmp_path DBs from the first commit (forge-oracle CI learning) |

## Documentation / Operational Notes

- Add the new commands to CLAUDE.md's command list (`--forensics`, `--ablate-tiebreak`, `--trend forensics`) after landing.
- R8's outcome (bracketed credit range) gets a dated RULE_HISTORY entry regardless of flag threshold, citing this plan. (Deliberate widening of origin R8's threshold-conditional entry: the project's documented null-result culture — BM25, embedding sweeps — says negative measurements are first-class records.)
- The forensics report's bucket→reform mapping is the input to the next ideation/plan cycle (color-conditioned IDF vs deck-shaped selection vs anti-synergy vs interaction learner).

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-10-divergence-forensics-requirements.md](../brainstorms/2026-06-10-divergence-forensics-requirements.md)
- Ideation: [docs/ideation/2026-06-10-synergy-accuracy-ideation.md](../ideation/2026-06-10-synergy-accuracy-ideation.md)
- Related code: `src/mtg_synergy_graph/bench/` (cli, handlers, history, hidden_gems, per_commander_ndcg, rule_ops, tensor), `src/mtg_synergy_graph/validate.py`, `src/mtg_synergy_graph/engine.py`
- Related plans: `docs/plans/2026-05-04-001-feat-bm25-idf-probe-plan.md` (per-commander reporter precedent), `docs/plans/2026-04-26-001-feat-tensor-weight-optimizer-plan.md` (history CSV + exit-code precedent)
