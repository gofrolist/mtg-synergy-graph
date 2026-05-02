---
date: 2026-05-02
topic: preflight-gate-stack
---

# Pre-flight Gate Stack for Gap-Report Queue

## Problem Frame

The project's stated north star is "best-in-the-world synergy recommendation
model" — operationalized via two quality axes: aggregate `nDCG@30` (currently
~0.256) and `hidden_gem_hit_rate` (currently ~0.84). The
`gap_report → scaffold → audit` loop is the primary mechanism for adding new
complement rules that move these axes. As of 2026-04-25 the loop has a
documented 48% revert rate (24 of 50 top-50 gap_report entries reverted) — but
revert rate is **not the goal**. It is a process-cost proxy. The goal is more
shipped rules whose net effect is positive on the two quality axes, faster.

Three of four documented failure modes are deterministically or
heuristically detectable from data the audit already produces — but today they
are detected POST-SHIP via revert. Catching them BEFORE generator-writing
should both raise the success rate of attempts AND lower per-attempt cost.

Failure modes we want to catch BEFORE generator-writing:

1. **Untestable** — gap entry's gate fires on 0 commanders in the evaluation
   fixture (canonical case: `damage_prevention_voltron`, 2026-04-25 — 250-line
   generator wasted; one SQL query would have killed it). Deterministic.
2. **Vacuum-fill** — rule fires in mechanical empty space; cards enter top-30
   at uniform low scores because no other rule fires on the same commanders
   (canonical case: `ward_2_tribal`, 2026-04-24 — 867 cards entered top-30
   across 69 commanders at uniform +0.16). Predictable from candidate-pool
   shape pre-scoring.
3. **Flat-noise** — top-30 score CV < 0.02; rule contributes uniform mass
   without discriminating. Predictable from per-(cmdr, candidate) IDF
   distribution pre-scoring.

(Fourth documented failure mode — Forge-flavor amplification — is out of
scope for v1; see Scope Boundaries. Implication: the `<25%` revert-rate
target named in Success Criteria is a milestone-floor estimate; if forge-flavor
accounts for 30-40% of past reverts, the achievable v1 floor may be
30-35%. Tracked explicitly.)

The pre-flight stack runs as a library consumed by both `scripts/gap_report.py`
(humans reading the markdown report) and `scripts/scaffold_rule.py --walk`
(automated generator). Same logic, two surfaces.

**Phasing.** The 3 stages ship as **two sub-milestones inside one feature:**
v1 = Stage A only (deterministic, ~1-2 days). v1.5 = Stages B+C added only
if Stage A's first 2 weeks of live-measurement evidence shows a non-trivial
residual revert rate of types B+C predict. This honors the user's
"Full 3-stage as one milestone" framing (one continuous workstream) while
respecting the reviewer consensus that Stages B+C have undefined calibration
debt and should not commit to scope until Stage A's empirical signal arrives.

## Requirements

### Gate Stack (the three gates)

- R1. Stage A — golden-coverage prefilter. For a candidate
  `(commander_gate_predicate, candidate_predicate)`, count distinct
  commanders in the 500-cmdr fixture (`tests/fixtures/golden_set_run_500.json`)
  whose ports satisfy the commander_gate, AND separately count distinct
  commanders in the full legal-commander universe (`cards.legal_commander = 1`)
  satisfying the same gate. Verdicts:
    - `0` in fixture AND `0` in legal universe → `UNTESTABLE` (REJECT severity).
    - `0` in fixture AND `≥3` in legal universe → `FIXTURE_BLIND_SPOT`
      (WARN severity, not REJECT). The rule is structurally legitimate but
      cannot be measured against the current evaluation fixture.
    - `≥1` in fixture → `PASS`.
- R2. Stage B — paper-rule scoring simulator. **(v1.5; conditional on
  Stage A measurement evidence)** For a candidate
  `(commander_gate, candidate_predicate, multiplier_estimate)`, simulate
  the rule's contribution per `(commander, candidate)` pair against the
  500-cmdr fixture **using R12's ephemeral-registration mechanism — NOT
  a parallel scoring reimplementation.** Compute predicted Gate-A median
  (other-rules-firing on same commanders) and predicted Gate-B CV
  (top-30 score dispersion). Emit `VACUUM_FILL` verdict if
  predicted-Gate-A median < 3; `FLAT_NOISE` if predicted-Gate-B CV < 0.05.
  Both at WARN severity.
- R3. Stage C — embedding-shape prior. **(v1.5; conditional on
  Stage A measurement evidence)** For the same candidate, retrieve every
  card matching `candidate_predicate` from the existing 128-d
  `card_embeddings` table. **Stage C requires candidate-set size N ≥ 15;
  below that, Stage C emits `INSUFFICIENT_DATA` (treated as PASS for
  the pipeline verdict and surfaced explicitly in the per-gate reason).**
  k-means with N<15 is degenerate (k≥N produces singletons; k near N
  produces high-bias variance estimates), and the canonical narrow-rule
  cases (e.g. damage_prevention_voltron) sit in this range — Stage A's
  FIXTURE_BLIND_SPOT carries the signal there, not Stage C. For
  N ≥ 15: cluster the matched set via k-means (k=3-5, with k-means++
  initialization, fixed RNG seed for determinism). Compute the
  **intra-cluster cosine spread** (mean within-cluster variance), NOT
  the raw cosine spread of the full set. Emit `VACUUM_FILL` verdict if
  intra-cluster spread > threshold (calibrated per R13). The
  intra-cluster framing explicitly accommodates polysemous-correct rules
  (`counter_axis_feeder`, `gy_fuel`, `modified_axis_feeder`) whose
  candidate set is mechanically heterogeneous BETWEEN clusters but
  coherent WITHIN. Stage C is self-contained; it does not depend on
  Stage B's output. WARN severity. The embedding flag
  (`_ENABLE_EMBEDDING_CONTRIBUTION`) does not need to be on — the table
  exists regardless.
- R4. The pipeline emits a single `PipelineVerdict` per candidate combining
  the active gates. PipelineVerdict severity = max severity across gates.
  Reasons from each gate are concatenated. In v1 only Stage A runs;
  Stages B and C contribute `PASS` placeholders so the verdict structure
  is stable across phases.

### Verdict Semantics and Override

- R5. Three severity levels: `PASS`, `WARN`, `REJECT`. Match
  `rule_quality_gate.py` exit-code convention (0/1/2).
- R6. Stage A produces `PASS`, `WARN` (FIXTURE_BLIND_SPOT), or `REJECT`
  (UNTESTABLE). Stages B and C produce only `PASS` or `WARN`. Predictive
  gates never hard-block until calibration shows their false-positive rate
  is below an agreed threshold.
- R7. `--force` flag bypasses WARN verdicts (proceeds as if PASS for
  scoring purposes) but does NOT bypass REJECT. Every `--force` invocation
  appends one row to `.audit/preflight_overrides.csv` with timestamp,
  gap_id, gate name, verdict, override reason supplied by the user. The
  CSV is gitignored, append-only, no rotation; retention is unbounded
  because the calibration corpus value increases with size.
- R8. Walker (`scaffold_rule.py --walk`) treats WARN as `PASS-with-log`
  by default, NOT as REJECT. WARN candidates are attempted and the walker
  appends one row to `.audit/walker_outcomes.csv` with
  {timestamp, gap_id, verdict, attempted=true, post_scaffold_outcome}.
  This builds the false-positive measurement corpus that overrides the
  reviewer concern about silent walker over-rejection. A `--strict-warn`
  flag inverts the default for an autonomous run that wants conservative
  behavior. Additionally, walker attempts a randomized 10% sample of
  REJECT-classified candidates as a calibration bleed (logged the same
  way) — REJECT bleeds are bounded by a per-week budget set in walker
  config to avoid runaway cost.

### Library and Integration

- R9. The library lives at `src/mtg_synergy_graph/preflight/`. Two entry
  points: `evaluate_one(candidate) -> PipelineVerdict` and
  `evaluate_all(candidates: Iterable) -> Iterable[PipelineVerdict]`.
  Per-call cache reset between consumers is acceptable; what is NOT
  acceptable is silent cross-consumer state leakage. The functions are
  pure with respect to their explicit DB connection + scoring config
  arguments; module-level state (e.g., `functools.cache` on commander
  targets) is invalidated whenever the scoring config hash changes.
- R10. `gap_report.py` consumes the library at report-generation time;
  every entry in `docs/gap_report.md` shows a Pre-flight column with the
  verdict and one-line reason. Entries are sorted PASS first, then WARN,
  then REJECT. Each section header explicitly labels the count.
- R11. **(Removed in refinement.)** A JSON sidecar was previously
  proposed. The walker imports `gap_report.py` as a Python module today;
  no current consumer reads a file-format gap_report. Adding a JSON
  sidecar is deferred to a future PR if and when a file-reading consumer
  materializes (multiplier-zero shipping pipeline being one candidate, but
  it does not exist yet).
- R12. The simulator IS production scoring with the candidate rule
  registered ephemerally — NOT a parallel reimplementation. Concretely:
  the simulator imports `universal_scorer`'s scoring functions, registers
  the candidate rule as an in-memory rule for the duration of one
  `evaluate_one` call, runs the standard scoring path, then unregisters.
  Cache invalidation between calls is required. This eliminates the
  drift surface (no second scoring code path to keep in sync) at the
  cost of ~10× per-call overhead vs. a parallel reimplementation.
  Acceptable: pre-flight runs at human-attempt cadence (~10/day), not
  inference cadence. The pipeline is config-hash-gated against the union
  of `data/scoring_weights.json` + `data/event_match_seed.json` +
  `data/rules_seed.json` + git-tree-hash of
  `src/mtg_synergy_graph/universal_scorer.py` and
  `src/mtg_synergy_graph/graph_engine.py`; rejects with explicit error if
  hash mismatches.

### Calibration

- R13. A backtest harness replays the last N reverted rules through the
  full gate stack (sourced from `docs/RULE_HISTORY.md` + the append-only
  `docs/rule_attempts.jsonl` log read via `scripts/_attempt_log.py`'s
  `load_attempts()` helper — not a SQL table). The corpus is split
  **chronologically** into a calibration set (oldest 16) and a held-out
  validation set (newest 8). Gate thresholds may be tuned only against
  the calibration set; the validation set is read-only until a final
  pass. Output: `.audit/preflight_backtest.md` showing per-revert: which
  gate(s) caught it, the gate's reason, and whether the reason matches
  the actual revert reason. Threshold tuning AFTER seeing held-out
  results requires rolling the holdout split forward and starting fresh.
- R14. The backtest is invokable as `bench.py preflight-backtest` and
  runs as part of the change-control protocol any time a gate threshold
  moves.
- R15. Auto-trigger calibration. **(v1.5; manual `bench.py
  preflight-backtest` per R14 is sufficient in v1 since v1 has no
  WARN-from-predictive-gates verdicts to recalibrate.)** The walker /
  a quarterly cron run `bench.py preflight-backtest` automatically
  when ANY of these conditions hold: (a) override count over the last
  30 days exceeds a configurable threshold (default: 20); (b)
  override-rate / total-WARN ratio exceeds 50% **AND** denominator
  total-WARN ≥ 5 (the FP-corpus statistical-significance floor — same
  threshold as the Primary success criterion); (c) walker_outcomes.csv
  shows post-scaffold success rate on attempted-WARNs > 50% (predictive
  gates are too aggressive) **AND** denominator attempted-WARN ≥ 5; (d)
  90 days have elapsed since the last backtest. After firing, suppress
  re-fire for 7 days to avoid alert fatigue when conditions co-trigger.
  Backtest output is surfaced via the existing CLAUDE.md hook so the
  maintainer notices.

## Success Criteria

The criteria are organized by what they prove. **Quality-axis criteria are
the primary gate; cycle-cost criteria are secondary.** Pre-flight that
lowers revert rate while regressing the actual quality axes is a failure.

### Primary (quality-axis non-regression)

- **No NDCG regression.** Aggregate `nDCG@30` over the next 10 attempts
  ≥ baseline trajectory projected from the prior 10 attempts. Measured via
  `bench.py audit` history.
- **No hidden_gem_hit_rate regression.** `hidden_gem_hit_rate` over the
  next 10 attempts ≥ baseline trajectory. Measured via `bench.py audit`
  `--trend hidden_gems`.
- **False-positive corpus exists and is non-empty.** At least 5 entries
  in `walker_outcomes.csv` and/or `preflight_overrides.csv` recording
  WARN/REJECT candidates that subsequently passed post-scaffold
  `rule_quality_gate.py`. Without this corpus, FP rate is unmeasurable
  and v2 calibration of Stages B+C cannot proceed on evidence.

### Secondary (cycle-cost proxy)

- **Live revert rate.** Over the next 10 attempts, revert rate falls
  from baseline 48% to **<35%** (acknowledging the forge-flavor floor).
  If forge-flavor reverts are excluded from the denominator, target is
  <25%.
- **Override rate ceiling.** Override rate on WARN verdicts <30% over
  the 10-cycle window. Override rates above this signal that predictive
  gates are too aggressive and trigger R15.

### Functional and adoption

- **Functional success (v1.5 only).** Backtest catches ≥75% of the
  held-out 8-revert validation set, with ≥60% of catches having a
  gate-stated reason matching the actual revert reason. Tuning on the
  16-revert calibration set may achieve any catch rate; the 75% bar
  applies only to the held-out set.
- **Adoption.** `gap_report.py` and `scaffold_rule.py --walk` both
  consume the library on the next post-ship gap walk; humans use the
  PASS-sorted markdown without manual SQL pre-checks.
- **Maintenance.** Adding a new gate requires only adding one function to
  `preflight/gates.py` and registering it in the pipeline orchestrator.
  No changes to consumer code. Verified by adding a placeholder fourth
  gate (forge-flavor) as a no-op during v1 review.

## Scope Boundaries

- **Out of scope: Forge-flavor amplification gate.** Detecting that a
  rule's `forge_signal` derives from precon thematic pairing rather than
  mechanical synergy requires a PMI-vs-random-pair baseline and
  post-scoring evidence to calibrate. Defer to v2 once the v1 backtest
  provides false-positive evidence on stages B+C. **The success criteria
  acknowledge this gap explicitly via the <35% revert-rate target floor.**
- **Out of scope: New gate ordering/dependency mechanism.** Each gate runs
  independently. Stage C in this refinement no longer depends on Stage
  B's output (uses intra-cluster spread instead).
- **Out of scope: Migration of existing reverted rules into the gate
  pipeline.** Survivor #6 (revert quarantine with auto-resurrect probes)
  — separate brainstorm.
- **Out of scope: Walker UX redesign.** The `--strict-warn` and walker
  outcome logging are minimal. Larger walker reform (multiplier-zero
  shipping; survivor #4) is a separate brainstorm; the verdict-vs-action
  decoupling in R8 is the v1 step toward making walker action policy
  consumer-owned.
- **Out of scope: Removing existing post-scaffold gates.**
  `rule_quality_gate.py` Gates A/B/C remain mandatory after scaffold.
  Pre-flight runs BEFORE scaffold; complementary, not replacement.
- **Out of scope: Separate human-vs-walker override CSV.** Walker writes
  to `walker_outcomes.csv`; humans write to `preflight_overrides.csv`.
  Two distinct files because the FP-rate calculation differs (walker
  outcomes are post-scaffold-audit; human overrides are pre-scaffold
  judgments).

## Key Decisions

- **Phased milestone, not big-bang.** v1 ships Stage A; v1.5 ships
  Stages B+C only if measurement evidence justifies them. Reason:
  reviewer consensus that B+C have undefined calibration debt and Stage
  A alone delivers the largest documented save (untestable case). The
  user's "Full 3-stage as one milestone" choice is honored as
  "single continuous workstream" — one feature branch, one feature
  document, two ship dates separated by ~2 weeks of measurement.
- **Tiered severity (Stage A produces all three; B+C soft only).**
  Reason: Stage A is mostly deterministic; the WARN tier inside Stage A
  exists specifically for the fixture-blind-spot case where the rule is
  structurally legitimate but unmeasurable on the current fixture.
- **Walker treats WARN as PASS-with-log, not REJECT.** Reason: walker
  is the volume consumer; default-skip silently suppresses correct
  rules whose WARN comes from FP heuristics. The walker_outcomes.csv
  + 10% REJECT-bleed sample build the FP corpus that's currently
  missing. Without this corpus, B+C cannot be calibrated honestly.
- **Simulator IS production scoring, not a parallel reimplementation.**
  Reason: the drift surface of two scoring code paths is an open
  maintenance burden the project cannot absorb (single maintainer; cited
  in adversarial ADV-002 + feasibility F5). 10× per-call overhead is
  acceptable at human-attempt cadence. R12 is rewritten accordingly.
- **Library shape, not standalone CLI.** Reason: gap_report (humans) and
  walker (automation) both need the same logic. Library shape makes
  verdict-vs-action decoupling cleaner — the library returns verdicts;
  consumers own action policy. (Future: multiplier-zero shipping will
  treat WARN as ship-at-zero-and-let-optimizer-triage; gap_report and
  walker treat differently; same library serves all.)
- **500-cmdr fixture, not 100-cmdr.** Reason: per
  `docs/solutions/best-practices/optimizer-fixture-size-2026-04-30.md`,
  the 100-cmdr fixture is too small for trustworthy signal. Stage A
  prefilter on 500-cmdr produces a more realistic answer, supplemented
  by the legal-universe second corpus to catch the fixture-sample bias.
- **Backtest is held-out, not in-sample.** Reason: same engineer designs
  gates and scores backtest; full hindsight on revert reasons is a
  threshold-fitting trap. Chronological 16/8 split forces honest
  generalization measurement.
- **Override CSV retention is unbounded, gitignored.** Reason: the
  calibration corpus value compounds with size. Gitignored prevents
  accidental leakage of gap_id-level reasoning into the public repo.
- **Quality-axis success criteria are primary; revert-rate is
  secondary.** Reason: the project's north star is hidden_gem_hit_rate
  and NDCG, not cycle cost. Pre-flight that lowers revert rate while
  regressing the quality axes is a failure regardless of how clean the
  gate verdict logic looks.

## Dependencies / Assumptions

- The 500-cmdr fixture (`tests/fixtures/golden_set_run_500.json`) is
  current and regenerable via `scripts/bootstrap_golden_set_500.py`
  (verified 2026-04-30; 58k lines, 2.5MB).
- The persisted `card_embeddings` table is current (built post-cardsfolder
  refresh; Stage C requires it but does NOT require
  `_ENABLE_EMBEDDING_CONTRIBUTION = True`).
- The append-only `docs/rule_attempts.jsonl` log records revert reasons
  as free-text via `scripts/_attempt_log.py`. The backtest harness reads
  it via `load_attempts()`. Note: `reason` is currently unstructured
  (raw stack-trace dumps in some entries); mapping reverts onto the
  4 documented failure modes for the success criterion will require
  either a string-classification heuristic or backfilling a structured
  `failure_mode` field — flagged for planning.
- `cards.legal_commander = 1` is populated for the legal-universe second
  corpus check (R1). Verified per CLAUDE.md "Legality filter" section.
- Tensor optimizer M1 is shipped (per ideation status update 2026-05-02);
  multiplier-zero shipping (survivor #4) will eventually reuse the
  preflight library, but pre-flight v1 does NOT depend on multiplier-zero
  shipping landing first.

## Outstanding Questions

### Resolve Before Planning

(none — owner has made all product decisions. Planning may proceed.)

### Deferred to Planning

- [Affects R3][Needs research] What intra-cluster cosine-spread
  threshold predicts vacuum-fill at acceptable false-positive rate? The
  threshold must be calibrated against the 16-revert calibration set
  (R13), with the 8-revert holdout used for validation. Planning runs
  the initial pass; if the threshold cannot be set such that >70% of
  calibration vacuum-fills are caught with <30% FP, Stage C is
  abandoned in v1.5 (graceful degradation; v1 ships without it
  regardless).
- [Affects R12][Technical] What is the precise mechanism for ephemeral
  rule registration? `RULE_GATES` lives in
  `src/mtg_synergy_graph/complement_rules/registry.py` as an immutable
  `tuple[RuleGate, ...]` (not in `universal_scorer` — and not directly
  mutable). `_RULE_QUALITY_MULTIPLIER` lives in
  `src/mtg_synergy_graph/universal_scorer.py:593` as a mutable dict
  with an existing context-manager precedent (`patched_rule_quality_multiplier`,
  lines 596-635). For formal-rule (event_pairs-based) candidates, the
  cleanest path is to pass an extended `COMPLEMENT_RULES` list as the
  `rules` parameter to `find_all_complements` (already a parameter,
  not requiring global mutation). For card-attribute candidates,
  `_card_attr_complements()` (`complement_rules/core.py:1225-1259`)
  has 30+ hardcoded `_find_*` helper calls — ephemeral registration
  here requires a substantial refactor OR Stage B is restricted to
  formal rules only at v1.5 with the limitation documented. Planning
  must split R12 into sub-mechanisms by rule family AND verify
  thread-safety against `universal_scorer`'s `lru_cache(maxsize=4096)`
  + `cached_property` decorators (which can serve stale results across
  ephemeral registration boundaries).
- [Affects R8][Technical] What's the per-week REJECT-bleed budget for
  walker calibration sampling? Plausibly 1-3 attempts/week to avoid
  cycle cost. Planning confirms based on observed walker cadence.
- [Affects R10] **(Resolved in refinement.)** Sort order WITHIN each
  verdict band is "impact desc" — same as today's gap_report.md ranking.
  Owner decision; not a planning question.
- [Affects R13][Needs research] What's the heuristic for classifying
  unstructured `reason` strings in `rule_attempts.jsonl` onto the 4
  failure-mode taxonomy? Either a manual labeling pass on the 24
  reverts (~1 hour) OR a regex/LLM classifier — planning chooses.
  Without this, the "≥60% of catches have gate-stated reason matching
  actual revert reason" criterion is unmeasurable.

## Next Steps

`-> /ce-plan` for structured implementation planning. Plan must phase
v1 (Stage A only) and v1.5 (Stages B+C conditional on Stage A
measurement evidence) as separate implementation units within one
plan document.
