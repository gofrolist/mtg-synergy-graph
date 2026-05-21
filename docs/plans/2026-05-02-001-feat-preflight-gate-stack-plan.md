---
title: "feat: Pre-flight Gate Stack v1.0 (Stage A only)"
type: feat
status: active
date: 2026-05-02
origin: docs/brainstorms/2026-05-02-preflight-gate-stack-requirements.md
deepened: 2026-05-02
---

# feat: Pre-flight Gate Stack v1.0 (Stage A only)

## Overview

Add Stage A of the pre-flight gate stack: a deterministic golden-coverage
prefilter that runs BEFORE generator-writing in the
`gap_report → scaffold → audit` loop. v1.0 ships Stage A as a library consumed
by both `scripts/gap_report.py` (humans reading the markdown report) and
`scripts/scaffold_rule.py --walk` (automated generator).

**Stages B and C (paper-rule simulator + embedding-shape prior) are NOT in
v1.0.** Two passes of plan review surfaced concrete implementability concerns
(BACKTEST-CORPUS-N1 hard blocker — only 1 unique revert post-2026-04-23 cutoff;
k-means cross-machine determinism; RULE_GATES import-capture issues; promotion
gate anti-correlation; manual-labeling drift) that suggest the v1.5 design is
not ready to plan. v1.5 becomes a **separate ce-brainstorm/ce-plan cycle
once v1.0 measurement evidence accumulates** — see "v1.5 Plan Trigger" below.

The brainstorm at v1.5-time will have actual data: which failure modes recur,
how many candidates are formal-rule, what residual reverts look like after
Stage A. Planning on data > planning on hypotheses.

## Problem Frame

The project's stated north star is "best-in-the-world synergy recommendation
model" — operationalized via aggregate `nDCG@30` (~0.256) and
`hidden_gem_hit_rate` (~0.84). The `gap_report → scaffold → audit` loop is the
primary mechanism for adding new complement rules. As of 2026-04-25 the loop
has a documented 48% revert rate (24 of 50 top-50 gap_report entries reverted).

The most-cited canonical waste case is `damage_prevention_voltron` (2026-04-25):
250-LOC generator written, scaffolded, audit ran, then discovered the gate
fires on 0 of 100 golden commanders → unable to evaluate → reverted entirely.
**One SQL pre-check would have killed the proposal before any code was
written.** That pre-check IS Stage A.

v1.0 scope: prevent damage_prevention_voltron-class waste deterministically.
That's a smaller value claim than catching all four documented failure modes
(untestable, vacuum-fill, flat-noise, forge-flavor) — but it's measurable,
implementable, and validates the library shape that future v1.5 work will
extend.

See origin: `docs/brainstorms/2026-05-02-preflight-gate-stack-requirements.md`.

## Requirements Trace

### v1.0 (this plan)

- R1. Stage A — golden-coverage prefilter (fixture + legal universe, three-tier verdict)
- R4. PipelineVerdict combiner (max severity, concatenated reasons; in v1.0 the only gate is Stage A)
- R5. PASS/WARN/REJECT severity tiers, exit-code 0/1/2 convention
- R6. Stage A produces all three verdicts (PASS/FIXTURE_BLIND_SPOT/UNTESTABLE)
- R7. `--force` bypass with logged override CSV (`preflight_overrides.csv`)
- R8. Walker treats WARN as PASS-with-log (NOT REJECT); walker_outcomes.csv recorded per attempt (single-row in v1.0)
- R9. Pure-function library; per-call cache reset acceptable
- R10. gap_report.md adds Pre-flight column, sorts by verdict band

### Deferred to v1.5 (separate plan)

- R2. Stage B — paper-rule scoring simulator
- R3. Stage C — embedding-shape prior
- R11. JSON sidecar (no current file-reading consumer; revisit if multiplier-zero shipping pipeline is built)
- R12. Simulator IS production scoring with ephemeral registration; broad config-hash
- R13. Backtest harness with reverse-chronological holdout
- R14. `bench.py preflight-backtest` invokable
- R15. Auto-trigger calibration

## Scope Boundaries

- **Out of scope: Stages B and C.** v1.5 work; separate ce-brainstorm/ce-plan cycle. See "v1.5 Plan Trigger" section.
- **Out of scope: Forge-flavor amplification gate.** Documented as 4th failure mode in the brainstorm but never planned for v1.0; v1.5 if at all.
- **Out of scope: Removing existing post-scaffold gates.** `rule_quality_gate.py` Gates A/B/C remain mandatory after scaffold. Pre-flight runs BEFORE scaffold; complementary, not replacement.
- **Out of scope: Cross-artifact consistency checker.** With v1.5 deferred, the only persisted artifacts in v1.0 are `preflight_overrides.csv` and `walker_outcomes.csv`. Both are append-only, gitignored, with no inter-file invariants. Defensive parsing in v1.0 code is sufficient; no separate consistency script needed.

### Deferred to Separate Tasks

- **v1.5 (Stages B+C):** separate brainstorm + plan cycle, triggered per "v1.5 Plan Trigger" below.
- **Migration of existing reverted rules into the gate pipeline:** survivor #6 from `docs/ideation/2026-04-26-applying-built-tooling-ideation.md` (revert quarantine with auto-resurrect probes). Separate brainstorm.
- **Larger walker reform (multiplier-zero shipping):** survivor #4 from same ideation. Separate brainstorm.

## Context & Research

### Relevant Code and Patterns

- **`scripts/rule_quality_gate.py`** — POST-scaffold gates A/B/C. Pattern for severity+exit-code convention (0/1/2). Pre-flight is the PRE-scaffold complementary layer.
- **`scripts/gap_report.py`** — current ranker uses `commanders × (1 - covered_rate) × forge_signal`. Add Pre-flight column at the report-emit stage; consumer of the new library.
- **`scripts/scaffold_rule.py:2380-2433`** — walker loop. Hook point: AFTER `_pick_top_proposal()` (line 2393) and BEFORE the `_GENERATORS[proposal.template](proposal)` call (line 2402), so REJECT verdicts skip wasted scaffold work.
- **`scripts/_attempt_log.py`** — `record_attempt(AttemptRecord)` and `load_attempts()` over `docs/rule_attempts.jsonl`. Pattern for append-only log writing.
- **`src/mtg_synergy_graph/engine.py:172-195, 375-377`** — `legal_commander` filter pattern with defensive PRAGMA-table_info check for legacy DBs without the column. Stage A reuses this defensive shape.
- **`tests/fixtures/golden_set_run_500.json`** — 500-cmdr fixture. Stage A first-corpus.
- **`cards.legal_commander = 1`** — Stage A second-corpus; CLAUDE.md "Legality filter" section confirms ~1,679 cards filtered. Note: pass-2 review identified that the population path is opaque from `import_cardsfolder.py`; verify before relying on it.

### Institutional Learnings

- **`docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`** — the canonical source SQL pre-check. Stage A productizes it.
- **`docs/solutions/best-practices/rule-quality-gates-2026-04-24.md`** — Gates A/B/C exist post-scaffold; v1.5 will reuse the same metrics pre-scaffold (when v1.5 is planned).
- **`docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`** — 100-cmdr too small; Stage A uses 500-cmdr.

### Verified Data Points

- **damage_prevention_voltron** (canonical save case): broad legal-universe gate matches **31 commanders**. FIXTURE_BLIND_SPOT WARN tier correctly fires — keeping legal-universe second corpus in v1.0 is justified.
- **`docs/rule_attempts.jsonl`**: 91 records total, 32 with `outcome=reverted` (24 unique by `(template, rule_id)`); all timestamps ISO-8601 with second precision; `reason` field is unstructured free-text (raw stack traces in some entries).

## Key Technical Decisions

- **v1.0 ships Stage A only.** Two passes of plan review surfaced enough v1.5 implementability concerns to defer. v1.0's value is one canonical save case (damage_prevention_voltron-class) plus the library shape; v1.5's value depends on data we don't have yet (residual revert mix, formal-rule candidate share, calibration corpus).
- **v1.0 ships Unit 0 (audit + labeling) as a one-shot prerequisite, not ongoing maintenance.** With v1.5 deferred, the labeling pass is no longer load-bearing for promotion gate, R13 backtest, or auto-trigger conditions. It survives ONLY for the Stage A historical sanity check ("how many of 24 reverts would Stage A have caught?"). After that one use, labels are reference data, not a maintained corpus.
- **Smaller v1 (drop legal-universe corpus): REJECTED.** Verified that damage_prevention_voltron has 31 legal-universe commanders, so FIXTURE_BLIND_SPOT WARN correctly fires for the canonical save. Legal-universe second corpus stays in v1.0.
- **Library shape, not standalone CLI.** gap_report (humans) and walker (automation) both need the same logic. Library shape matches the brainstorm's intent and survives intact for v1.5 to extend.
- **500-cmdr fixture, not 100-cmdr.** Per `optimizer-fixture-size-2026-04-30.md`, the 100-cmdr fixture is too small for trustworthy signal.
- **walker_outcomes.csv: single-row write per attempt in v1.0.** Two-phase write was a v1.5 pattern designed for the FP-rate calculation that v1.5's predictive gates need. Without v1.5, walker_outcomes.csv just records what was attempted with what verdict — single row sufficient. Two-phase upgrade can happen in v1.5 plan if needed.
- **Override is logged, not silent.** `--force` writes to `.audit/preflight_overrides.csv`. The override mechanism stays, so the v1 → v1.5 evidence base accumulates from day one.
- **Defer config-hash extension to v1.5.** v1.0 Stage A is deterministic SQL; doesn't depend on the broader scoring config. The v1.5 ephemeral registration mechanism is what motivates the file-content sha256 hash extension; v1.0 doesn't need it. Avoids the pass-2 concern (AR2-3) about routine PRs to universal_scorer.py triggering re-pin friction.
- **Stage A historical sanity check has an explicit decision rule** (added per pass-2 product F3): If Unit 0's check shows Stage A would have caught < 3 of 24 historical reverts, owner explicitly acknowledges in `preflight-prereq-audit.md` that v1.0 ship justification rests on the canonical save case alone. If owner cannot defend the forward-looking claim with concrete in-flight evidence (categories of proposals likely to hit zero fixture commanders), reduce v1.0 scope further: ship Stage A library only, defer gap_report.md integration (Unit 2) and walker integration (Unit 3) to a separate decision.

## Open Questions

### Resolved During Planning

- **v1.0 scope**: Stage A only; v1.5 deferred to separate planning cycle.
- **Legal-universe corpus in v1.0**: keep — verified canonical save case requires it.
- **Smaller v1 (no legal-universe)**: rejected based on verified data.
- **walker_outcomes.csv schema in v1.0**: single-row write (event/walker_pid columns are v1.5).
- **Override CSV separation**: walker writes to `walker_outcomes.csv`; humans to `preflight_overrides.csv`.
- **Override CSV retention**: append-only, gitignored, unbounded.
- **R10 sort order within band**: impact desc.
- **Stage A historical sanity check decision rule**: explicit acknowledgment required if catch rate < 3-of-24.

### Deferred to v1.5 Plan

- All R12 implementation questions (ephemeral registration mechanism, cache enumeration, RULE_GATES patching, config-hash extension, k-means determinism mitigation, etc.). v1.5 plan must address them with the data v1.0 measurement provides.

### Deferred to Implementation

- **legal_commander population path verification.** CLAUDE.md says it's populated; pass-2 feasibility found `import_cardsfolder.py` doesn't visibly populate it. Implementer verifies the actual population path (likely a separate Scryfall-fetch step) before relying on the column. If the column isn't reliably populated for fresh DBs, Stage A's defensive PRAGMA fallback (per engine.py precedent) silently degrades to fixture-only — document this in code if it occurs.

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

### v1.0 pipeline data flow

```
gap_report.py / scaffold_rule.py --walk
                |
                v
        evaluate_one(candidate, conn, scoring_config)
                |
                v
   +--------------------------+
   |  pipeline orchestrator   |
   +--------------------------+
                |
                v
            stage_a
            golden coverage
            (fixture + legal universe)
                |
                v
        PipelineVerdict
        (severity = stage_a.severity)
                |
                v
        consumer-specific action policy
        (gap_report.md sort | walker skip/attempt)

(v1.5 will add stage_b and stage_c gates as siblings to stage_a;
 the pipeline orchestrator's combiner contract is forward-compatible.)
```

### Stage A verdict decision matrix

| fixture hits | legal-universe hits | Verdict       | Severity |
|--------------|---------------------|---------------|----------|
| ≥ 1          | (any)               | PASS          | PASS     |
| 0            | ≥ 3                 | FIXTURE_BLIND_SPOT | WARN  |
| 0            | < 3                 | UNTESTABLE    | REJECT   |

## Implementation Units

### Phase v1.0 (~2-3 days)

- [ ] **Unit 0: Card-attribute audit + manual labeling pass (one-shot prerequisite)**

**Goal:** Resolve two empirical questions BEFORE Unit 1 implementation: (a) [DEFERRED to v1.5 plan] what fraction of current top-50 gap_report candidates are formal-rule vs card-attribute — only relevant when v1.5 is planned. (b) Classify the 24 historical reverts onto a failure-mode taxonomy so the Stage A historical sanity check ("how many would Stage A have caught?") can run before v1.0 ships.

**Requirements:** Prerequisite for R1 sanity check.

**Dependencies:** None.

**Files:**
- Create: `docs/rule_attempts_labels.jsonl` (24 entries, one per `(template, rule_id)` revert)
- Output written to: `docs/preflight-prereq-audit.md` (committed)

**Approach:**
- **Manual labeling pass** (~2 hours; pass-2 estimate adjusted from 1 hour): For each of the 24 unique reverts in `rule_attempts.jsonl`, read the `reason` field, cross-reference `docs/RULE_HISTORY.md` if needed, classify as one of `untestable | vacuum_fill | flat_noise | forge_flavor | test_failure | other`. Persist labels to `docs/rule_attempts_labels.jsonl` with explicit per-entry reasoning. Ambiguous cases get a `secondary_label`. Reasoning field MUST quote a verbatim snippet from the `reason` field of the original entry (per pass-2 adversarial AR2-6) to anchor judgments.
- **Stage A historical sanity check**: For each of the 24 reverts, count how many had 0 fixture commanders matching the gate. Document in `preflight-prereq-audit.md`. Apply the explicit decision rule (Key Technical Decisions): if catch rate < 3-of-24, owner acknowledges the v1.0 justification rests on the canonical save case alone, and decides whether to proceed with Units 1-3 OR scope further down to library-only.
- **Card-attribute audit**: SKIP for v1.0. Document in `preflight-prereq-audit.md` that the audit is deferred to v1.5 planning.

**Patterns to follow:**
- One-shot scripts: `scripts/bootstrap_golden_set_500.py` is precedent for "run once, write a JSON artifact" tooling.
- Labeling JSONL: `_attempt_log.AttemptRecord` schema as a reference shape.

**Test scenarios:**
- Test expectation: none — Unit 0 is a discovery / labeling task, not feature code. Outputs are reviewed by hand and gate downstream Unit decisions.

**Verification:**
- `docs/preflight-prereq-audit.md` exists with: (a) Stage A historical sanity check result (X of 24 reverts caught); (b) explicit decision per the decision rule.
- `docs/rule_attempts_labels.jsonl` exists with 24 labeled entries; ambiguous cases have `secondary_label` populated; every `reasoning` quotes verbatim from original `reason`.
- Owner reviews the audit before Unit 1 begins.

---

- [ ] **Unit 1: Pre-flight library scaffolding + Stage A gate**

**Goal:** Create the `preflight/` library and implement Stage A (golden-coverage prefilter with fixture + legal-universe two-corpus check).

**Requirements:** R1, R4, R5, R6, R9.

**Dependencies:** Unit 0 (sanity check decision must be reviewed).

**Files:**
- Create: `src/mtg_synergy_graph/preflight/__init__.py`
- Create: `src/mtg_synergy_graph/preflight/types.py`
- Create: `src/mtg_synergy_graph/preflight/gates.py`
- Create: `src/mtg_synergy_graph/preflight/pipeline.py`
- Create: `tests/test_preflight_stage_a.py`
- Create: `tests/test_preflight_pipeline.py`

**Approach:**
- `types.py`: `Candidate` (commander_gate_predicate + candidate_predicate), `GateVerdict` (severity, name, reason), `PipelineVerdict` (max severity, list of GateVerdicts).
- `gates.py`: `stage_a_golden_coverage(candidate, conn) -> GateVerdict`. Two SQL queries — one against the 500-fixture commanders, one against `cards WHERE legal_commander = 1`. Decision matrix per High-Level Technical Design.
- `pipeline.py`: `evaluate_one(candidate, conn, scoring_config) -> PipelineVerdict`. Calls active gates; combines verdicts. In v1.0 only stage_a is wired; the gate-list shape is forward-compatible for v1.5 to add stage_b/stage_c without consumer-side changes.
- The `commander_gate_predicate` schema for v1.0: a JSON dict with `port_type`, `event_class`, optional `replacement_result`, optional `valid_filter_contains`, optional `raw_line_like`. **Note:** gap_report.py currently emits a 3-tuple signature `(port_type, event_class, sub_discriminator)`; the v1.0 schema is a SUPERSET. Implementer chooses: (a) extend gap_report.py to emit the additional fields; or (b) compute valid_filter_contains/raw_line_like from the proposal's exemplars at evaluate_one call time. Plan recommends (b) — keeps gap_report.py minimally changed.
- Defensive PRAGMA-table_info check for `legal_commander` column (per engine.py:172-195 precedent). On legacy DB without the column, log explicit warning and degrade to fixture-only check (no FIXTURE_BLIND_SPOT verdict possible).

**Patterns to follow:**
- Verdict severity convention from `scripts/rule_quality_gate.py` (PASS/WARN/REJECT).
- SQL pre-check shape from `docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md`.
- `legal_commander` filter pattern from `src/mtg_synergy_graph/engine.py:172-195` (defensive PRAGMA-table_info check for legacy DBs).

**Test scenarios:**
- Happy path: Known-good rule (`counter_axis_feeder` gate) → fixture hits ≥ 1 → PASS.
- Edge case: Gate with 0 fixture hits and 31 legal-universe hits (damage_prevention_voltron pattern) → FIXTURE_BLIND_SPOT (WARN).
- Edge case: Gate with 0 fixture hits and 0 legal-universe hits → UNTESTABLE (REJECT).
- Edge case: Gate with 0 fixture hits and 1-2 legal-universe hits → UNTESTABLE (REJECT — below `≥3` threshold).
- Error path: `commander_gate_predicate` references unknown `port_type` → raises explicit `ValueError`, not silent PASS.
- Integration: `evaluate_one` with stage_a only returns `PipelineVerdict(severity=stage_a.severity, gates=[stage_a_verdict])`.
- Edge case: Legacy DB without `legal_commander` column (PRAGMA absent) → fall back to fixture-only with explicit warning logged. FIXTURE_BLIND_SPOT verdict cannot fire under this fallback; document this limitation.

**Verification:**
- `pytest tests/test_preflight_stage_a.py tests/test_preflight_pipeline.py` passes.
- Running `evaluate_one` against the damage_prevention_voltron-shaped gate returns FIXTURE_BLIND_SPOT.

---

- [ ] **Unit 2: gap_report.py integration**

**Goal:** Add a Pre-flight column to `docs/gap_report.md`; sort entries by verdict band (PASS, WARN, REJECT); emit per-band counts.

**Requirements:** R10.

**Dependencies:** Unit 1.

**Files:**
- Modify: `scripts/gap_report.py`
- Test: `tests/test_gap_report_integration.py`

**Approach:**
- After `gap_report.py` builds its ranked proposals list, run `pipeline.evaluate_all()` over the proposals' gate signatures.
- Add a `Pre-flight` column to the markdown emit: `PASS` / `WARN: <reason>` / `REJECT: <reason>`.
- Sort: PASS first by impact desc; then WARN by impact desc; then REJECT by impact desc.
- Section headers in markdown explicitly label band counts: `### PASS (N entries)`, `### WARN — predicted issues (M entries)`, `### REJECT — untestable (K entries)`.
- Existing `gap_report.md` consumers (humans + walker via direct Python import) keep working — no programmatic API removed.

**Patterns to follow:**
- Markdown emission pattern in existing `gap_report.py` (the `_emit_proposal()` block).
- The walker imports `gap_report.py` as a Python module today (`scripts/scaffold_rule.py:50-56` imports `GapStat, RuleProposal, _commander_names, _propose, _scan_universe`; line 2151 mutates `gap_report.RULE_GATES` directly via `_refresh_registry`). **Do NOT rename or remove** `GapStat`, `RuleProposal`, `_commander_names`, `_propose`, `_scan_universe`, or the `RULE_GATES` module attribute during this integration. Add the Pre-flight column at the report-emit stage as an additive change only.

**Test scenarios:**
- Happy path: Generate gap_report.md with all-PASS proposals → markdown contains `### PASS (N entries)` header with all entries below it; no WARN or REJECT sections.
- Edge case: Mix of PASS/WARN/REJECT → all three section headers present, sorted by band, impact-desc within band.
- Edge case: All-REJECT scenario → only REJECT section; PASS and WARN sections show `(0 entries)` and are present for consistency.
- Integration: Walker's `_pick_top_proposal()` continues to work after the integration (calls Python module directly, not file-reading).

**Verification:**
- `uv run python scripts/gap_report.py` produces `docs/gap_report.md` with three sections.
- Walker invocation against the new gap_report.py produces a valid pick.

---

- [ ] **Unit 3: scaffold_rule.py walker integration + --force override + override CSV**

**Goal:** Walker consults pre-flight before each `_attempt_one`. WARN candidates are attempted by default (PASS-with-log); REJECT candidates are skipped. `--force` flag bypasses WARN with logged override.

**Requirements:** R7, R8.

**Dependencies:** Unit 1, Unit 2.

**Files:**
- Modify: `scripts/scaffold_rule.py`
- Modify: `scripts/gap_report.py` (CLI: add `--force` flag for human-driven preflight invocation)
- Create: `src/mtg_synergy_graph/preflight/overrides.py` (CSV writer for `.audit/preflight_overrides.csv` and `.audit/walker_outcomes.csv`)
- Test: `tests/test_walker_preflight_integration.py`
- Test: `tests/test_preflight_overrides.py`

**Approach:**
- Walker hook: AFTER `_pick_top_proposal()` (line 2393) and BEFORE `_GENERATORS[proposal.template](proposal)` (line 2402), call `pipeline.evaluate_one(proposal_to_candidate(proposal), conn, scoring_config)`.
- If verdict is REJECT (UNTESTABLE) → skip iteration, increment `counts["skipped"]`, log explicit message ("Stage A REJECT: 0 fixture commanders, 0 legal-universe commanders"), continue.
- If verdict is WARN (FIXTURE_BLIND_SPOT) → continue to `_attempt_one` by default. Append one row to `walker_outcomes.csv` with schema: `{timestamp, gap_id, verdict, attempted=true, post_scaffold_outcome=<result>}`. (Single-row v1.0 schema; two-phase upgrade is a v1.5 concern.)
- `--strict-warn` flag inverts the WARN default for autonomous-conservative mode.
- `--force` (gap_report.py CLI): prompts for override reason (or accepts `--force-reason "..."`), calls `pipeline.evaluate_one()`, if verdict is WARN appends row to `.audit/preflight_overrides.csv` with timestamp, gap_id, gate name, verdict, reason. REJECT cannot be force-bypassed (UNTESTABLE means literally zero commanders carry the gate; bypassing is meaningless).

**Patterns to follow:**
- Append-only CSV pattern from `scripts/_attempt_log.py:63-73` (write-then-fsync optional in v1.0; not on hot path).
- Walker loop structure at `scripts/scaffold_rule.py:2386-2433`.

**Test scenarios:**
- Happy path: Walker iteration with PASS verdict → proceeds to `_attempt_one`, no preflight rows written.
- Edge case: Walker iteration with WARN (FIXTURE_BLIND_SPOT) verdict + default policy → attempts; writes one row to `walker_outcomes.csv` with `verdict=WARN, attempted=True, post_scaffold_outcome=<result>`.
- Edge case: Walker iteration with WARN verdict + `--strict-warn` flag → skipped; `counts["skipped"]` increments.
- Edge case: Walker iteration with REJECT (UNTESTABLE) verdict → skipped regardless of flags; `counts["skipped"]` increments; explicit log message.
- Error path: `--force` invoked on a REJECT candidate → exits with explicit error; no row written to overrides CSV.
- Happy path: `--force` invoked on a WARN candidate with reason → row appended to `.audit/preflight_overrides.csv` with all fields populated.
- Edge case: `--force` invoked on a WARN candidate WITHOUT supplying reason → exits with explicit error prompting for reason.
- Integration: After walker completes, `walker_outcomes.csv` and `.audit/preflight_overrides.csv` reflect the iteration history (assert via file read in test).

**Verification:**
- Walker run on a known-WARN candidate produces a row in `walker_outcomes.csv`.
- `--force` on a known-WARN candidate appends to overrides CSV.
- Stage A REJECT candidate is correctly skipped without invoking `_GENERATORS` (the wasted scaffold-work prevention is the canonical save value).

## Success Criteria

The criteria are organized by what they prove. Quality-axis non-regression is
primary; cycle-cost improvement is secondary.

### Primary (quality-axis non-regression)

- **No NDCG regression.** Aggregate `nDCG@30` over the next 10 attempts ≥ −0.005 sum-delta tolerance (5× typical per-attempt step). No single attempt with NDCG drop > 0.003 absent compensating gain elsewhere in the same window. Measured via `bench.py audit --trend`.
- **No hidden_gem_hit_rate regression.** Rolling 10-attempt mean ≥ prior 10-attempt mean − 0.01 tolerance. Measured via `bench.py audit --trend hidden_gems`.
- If <10 attempts occur in the v1.0 measurement window, criterion is evaluated on whatever attempts did occur. The cadence question (is v1.0 producing enough signal to even test?) is answered separately when the v1.5 plan is drafted.

### Secondary (cycle-cost proxy)

- **Stage A ships at least one save.** Within the first 10 walker attempts post-v1.0, Stage A REJECT or FIXTURE_BLIND_SPOT fires at least once on a candidate that would otherwise have wasted ≥50 LOC of generator. If Stage A never fires in the first 10 attempts, owner reviews whether the predicate-shape distribution has shifted (e.g., away from damage_prevention_voltron-class proposals) and decides whether v1.0 was correctly sized.

### Functional and adoption

- **Adoption.** `gap_report.py` and `scaffold_rule.py --walk` both consume the library on the next post-ship gap walk; humans use the PASS-sorted markdown without manual SQL pre-checks.
- **Maintenance.** Adding a new gate (when v1.5 is planned) requires only adding one function to `preflight/gates.py` and registering it in the pipeline orchestrator. No changes to consumer code. The library's gate-list shape is forward-compatible by design.

## v1.5 Plan Trigger

v1.5 (Stages B+C) is deferred to a separate ce-brainstorm/ce-plan cycle. The
trigger to invoke that cycle is **either** of:

- **Evidence trigger**: After v1.0 has been live for ≥ 4 weeks AND ≥ 15 walker
  attempts have occurred, label the in-window reverts (~30 min, one-shot).
  If ≥ 30% of in-window reverts are labeled `vacuum_fill`, `flat_noise`, or
  `forge_flavor`, invoke ce-brainstorm with the topic "v1.5 pre-flight
  predictive gates" and the labeled corpus as input data.
- **Capacity trigger**: If the maintainer has ≥ 1 week of uninterrupted
  capacity AND wants to invest in pre-flight predictive gates regardless of
  in-window evidence (e.g., as part of a focused rule-shipping sprint),
  invoke ce-brainstorm. The brainstorm explicitly evaluates:
  (a) R12 mechanism choice (formal-rule-only vs core.py refactor) with
      current gap_report queue distribution as input
  (b) k-means cross-machine determinism mitigation (float64 cast / pin
      numpy / accept drift)
  (c) RULE_GATES patching strategy (extend compute_config_hash signature,
      NOT monkey-patch)
  (d) Promotion gate reframe (avoid anti-correlation with v1.0 success)
  (e) Backtest corpus strategy (corpus may still be N=1 post-cutoff;
      decide between counterfactual replay, label-mode validation, or
      no-historical-backtest)
  (f) Manual labeling lifecycle (when does it stop, how is drift detected)

The trigger is a planning gate, not an automatic action — owner judgment
applies. v1.5 may also be permanently deferred if neither trigger fires
within 6 months (declare v1.5 obsolete; investigate alternative survivors
from `docs/ideation/2026-04-26-applying-built-tooling-ideation.md` instead).

## System-Wide Impact

- **Interaction graph:** Pre-flight library is consumed by `gap_report.py` (read-only, library call at report-emit time) and `scaffold_rule.py --walk` (read-only library call at iteration start; new write to `walker_outcomes.csv`). No production scoring path is mutated.
- **Error propagation:** REJECT verdicts skip walker iteration with explicit log; WARN verdicts proceed by default with logged outcome. CSV write failures (disk full, permission denied) fail loud — pre-flight does not silently swallow IO errors.
- **State lifecycle risks:** v1.0 walker_outcomes.csv is single-row write per attempt (no two-phase pending state to manage). preflight_overrides.csv is append-only. Both gitignored; OS handles eventual cleanup.
- **API surface parity:** `gap_report.py` keeps its existing Python-module import surface for the walker. No JSON sidecar in v1.0 (R11 deferred). No CI script additions; no pre-commit hook additions.
- **Integration coverage:** Walker integration test (Unit 3) crosses the gap_report.py ↔ pre-flight library ↔ scaffold_rule.py ↔ walker_outcomes.csv boundary — ensures the four components work end-to-end on PASS, WARN, REJECT, and error-path scenarios.
- **Unchanged invariants:** `bench.py audit --expect-identity` MUST continue to pass throughout v1.0 development. v1.0 does not modify production scoring; pre-flight is purely a pre-scaffold filter.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Stage A's single canonical save (damage_prevention_voltron-class) is the only recurring use case; Stage A rarely fires in practice | Unit 0 historical sanity check + explicit decision rule (Key Technical Decisions) — if catch rate < 3-of-24, owner decides whether v1.0 is still worth shipping. v1.0 is intentionally low-cost (~2-3 days) precisely because the value claim is modest. |
| `legal_commander` column not reliably populated on fresh DBs (population path opaque from importer) | Defensive PRAGMA fallback per engine.py:172-195 precedent; document degraded behavior in code if column is absent. Implementer verifies population path during Unit 1. |
| FP corpus (preflight_overrides.csv + walker_outcomes.csv) accumulates too slowly to inform v1.5 decision | v1.5 plan trigger has both an evidence trigger AND a capacity trigger; the latter does not require accumulated FP data. v1.0 is independently valuable regardless of v1.5 outcome. |
| Pre-flight latency degrades human gap_report iteration cadence | Performance budget: gap_report.py full re-run with pre-flight should complete within 30s (Stage A is two SQL queries per proposal). If exceeded, batch the queries or cache more aggressively. |
| v1.5 never gets planned (capacity trigger never fires; evidence trigger doesn't reach 30% threshold) | Acceptable outcome. v1.0 still delivers the canonical save case prevention; v1.5 was contingent on data not yet observed. After 6 months without v1.5 trigger firing, declare v1.5 obsolete and re-evaluate against alternative ce-ideate survivors. |

## Documentation / Operational Notes

- Update `CLAUDE.md` to document the pre-flight stack as part of the rule-shipping workflow (after v1.0 lands). Note that v1.0 is Stage A only; v1.5 status is "deferred, see plan trigger".
- Update `docs/RULE_PLANNING.md` to insert pre-flight as the new step between gap_report and scaffold_rule.
- v1.0 ship announcement (commit message or PR body) should reference the brainstorm + plan documents and document the v1.5 plan trigger.
- After v1.0 ships: add `.audit/preflight_overrides.csv` and `.audit/walker_outcomes.csv` to `.gitignore` (consistent with `.audit/optimize_history.csv` precedent).
- `docs/preflight-prereq-audit.md` and `docs/rule_attempts_labels.jsonl` are committed (the former is the Stage A historical sanity check decision artifact; the latter is reference data for the eventual v1.5 plan trigger evaluation).

## Sources & References

- **Origin document:** [docs/brainstorms/2026-05-02-preflight-gate-stack-requirements.md](../brainstorms/2026-05-02-preflight-gate-stack-requirements.md)
- **Prior ideation:** [docs/ideation/2026-04-26-applying-built-tooling-ideation.md](../ideation/2026-04-26-applying-built-tooling-ideation.md) (Continuation 2026-05-02 section, Survivor #1)
- **Canonical save case:** [docs/solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md](../solutions/best-practices/gap-report-impact-vs-golden-set-coverage-2026-04-25.md)
- **Post-scaffold gates pattern:** [scripts/rule_quality_gate.py](../../scripts/rule_quality_gate.py) and [docs/solutions/best-practices/rule-quality-gates-2026-04-24.md](../solutions/best-practices/rule-quality-gates-2026-04-24.md)
- **Fixture sizing rationale:** [docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md](../solutions/best-practices/optimizer-fixture-size-2026-04-30.md)
- **Walker code:** [scripts/scaffold_rule.py:2380-2433](../../scripts/scaffold_rule.py)
- **Attempt log:** [scripts/_attempt_log.py](../../scripts/_attempt_log.py), [docs/rule_attempts.jsonl](../rule_attempts.jsonl)
- **Legality filter precedent:** [src/mtg_synergy_graph/engine.py:172-195](../../src/mtg_synergy_graph/engine.py)
