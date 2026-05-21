---
date: 2026-04-26
topic: applying-built-tooling
focus: use the validation/creation infra we've already shipped (`bench.py`, `gap_report`, `scaffold_rule`, `rule_quality_gate`, `forge_oracle`, port-graph, embeddings flag, `hidden_gem_hit_rate`) to actually move recommendation-model quality
mode: repo-grounded
---

# Ideation: Applying Built Tooling to Move Model Quality

## Grounding Context

**Codebase.** NDCG@30 ~0.256 on 100-cmdr golden set (small regression from 0.262 in 2026-04-21 doc). hidden_gem_hit_rate 0.8423 after `self_bridging_cascade` (plan 001) landed — second axis is healthy, primary is stuck. 62 hand-authored complement rules over 108k Forge ports from 32k cards. `--collinearity` and `--embedding-dedup` say the catalogue is healthy — pruning is *not* the next leverage point.

**Tooling shipped:** `bench.py audit` (`--inspect`, `--collinearity`, `--inspect-gems`, `--trend`, `--vs-forge-oracle`, `--embedding-dedup`, `--unknowns`, `--repin`) + persisted per-(commander, candidate, rule) tensor; `gap_report.py`; `scaffold_rule.py`; `rule_quality_gate.py` (Gates A/B/C); `forge_oracle.py` (PPMI on 670 Forge precons); typed port-graph + `rules_seed.json` + `RuleInterpreter`; 128-d content embeddings (default OFF); `hidden_gem_hit_rate` (tracking-only); `data/scoring_weights.json`.

**Documented blockers (the slack we're targeting):**
1. `gap_report` queue **structurally exhausted** on the 100-cmdr golden set — 50 entries: 24 reverted, 19 no generator, 7 untried no-coverage, 1 with golden-set hit (`gap-report-queue-dry-on-golden-set-2026-04-25.md`).
2. Writer-side audit gap on declarative rules — `RuleInterpreter` rules fire at runtime but auditor falls back to "probably covered"; `weight_hint` loaded but never consumed (commit 6fa552f).
3. Manual weight tuning is the only active lever — no automated search over `scoring_weights.json`.
4. Embedding flag stuck OFF — sweep was marginal-positive, below rubric (`infrastructure-without-scoring-activation-2026-04-24.md`).
5. `hidden_gem_hit_rate` is tracking-only — warning fires at Δ<-0.02 but commit still passes.

**External grounding (Phase 1 web research):**
- Saito & Joachims 2023 (arXiv:2307.15053): nDCG can anti-correlate with online metrics; raw DCG correlated 0.97-0.98 vs nDCG -0.91. **Implication:** flat 0.256 may be NDCG saturation, not model ceiling.
- AnyBURL / PyClause: rule mining from triples; documented 70-96% rule reduction at 91% retention via subset selection. Forge precon corpus → triples maps directly.
- Reciprocal Rank Fusion (Cormack et al. 2009): parameter-free; immune to score-scale mismatches; standard hybrid pattern in BM25+neural.
- Coordinate Ascent / SPSA against NDCG (RankLib, Stockfish `tune.cpp`): tensor makes O(1) per step.
- VIF stepwise pruning, SNIPS reweighting, BM25-as-baseline.

**Anti-goals (do not violate):** No EDHREC at inference. Per-commander rules. Per-archetype rules. EDHREC hivemind as design oracle. Cherry-picked Forge data.

## Ranked Ideas

### 1. Tensor-driven weight optimizer (SPSA / Coordinate Ascent + raw-DCG + hidden-gem floor + `weight_hint` as Bayesian prior)
**Description.** Replace manual `data/scoring_weights.json` editing with an offline optimizer that runs SPSA or Coordinate Ascent over `_RULE_QUALITY_MULTIPLIER` and `_FLAT_WEIGHT_OVERRIDES`. Each step is O(1): re-rank by tensor dot-product, no DB rebuild. Objective = raw DCG@30 (Saito & Joachims 2023 — nDCG can anti-correlate with ground truth) **subject to** `hidden_gem_hit_rate ≥ 0.84` as a hard Lagrangian floor. SNIPS-reweight per-commander gradient so heavy-popularity commanders don't dominate. Posterior weight = `(n·empirical + tau·weight_hint) / (n + tau)` — wires the dead `weight_hint` field into the loop as a prior. Output: a candidate `scoring_weights.json` diff with verdict pre-attached, gated through `bench.py audit --repin`.
**Rationale.** "Manual weight tuning is the only active lever" is the single biggest documented blocker. The tensor literally encodes the LTR feature matrix already. Stockfish-style SPSA tunes 60+ chess-eval features against win-rate exactly the way we need to tune ~62 IDF multipliers against DCG — structural fit is near-perfect, including the noisy non-differentiable objective. Saito & Joachims caveat addresses why aggregate NDCG might be saturating at 0.256: nDCG normalization can mask gains on sparse commanders.
**Downsides.** Requires careful held-out commander split to avoid overfitting (the 100-cmdr set is small). SPSA convergence is variance-sensitive. The Lagrangian-floor constraint can dead-lock if NDCG and hidden-gem are anti-correlated on margin.
**Confidence:** 85% · **Complexity:** Medium · **Status:** SHIPPED (plan 2026-04-26-001 M1) — converging at tiny gradients (+3.6e-4 train, +8.9e-5 held); 6 rules accepting consistent multiplier tweaks across 4 runs; 9 dead-keys on 500-train split; no proposal applied yet (human review pending).

### 2. Writer-side audit gate compiler (close the declarative-rule blind spot)
**Description.** For every rule in `DECLARATIVE_RULE_IDS`, compile its `rules_seed.json` JSON predicates into the same per-port gate predicate the auditor uses, so `bench.py audit --inspect RULE_ID` actually walks the firing population instead of falling through to the "probably covered" branch. The data exists in `rules_seed.json` and `RuleInterpreter` already returns SQL fragments + Python callables — this is plumbing, not new physics. Add `bench.py audit --writer-trace RULE_ID` that emits the generated SQL + gate trace.
**Rationale.** Documented systemic blind spot (commit 6fa552f). Right now declarative rules contribute to scores at runtime but the audit reports them as opaque, masking real impact. Prerequisite for survivor #1 to converge on right weights for the migrated `peer_tribal_keyword` family, for survivor #5's mined rules to be auditable from day one, and for any future "rules-as-data is the default authoring surface" inversion.
**Downsides.** Low-glamour plumbing. Risk: edge cases where the JSON predicate has a Python gate callable that the SQL path can't pre-filter equivalently.
**Confidence:** 90% · **Complexity:** Low-Medium · **Status:** Unexplored

### 3. Universe-wide lift gate + hidden-gem floor as commit constraint
**Description.** Add **Gate D** to `rule_quality_gate.py`: `rule_lift = score_with_R / score_baseline > 1.10` measured across all Forge legendary creatures (~1,800 commanders), where baseline scores by `Σ port_idf` only (the trivial port-frequency floor). Any rule failing Gate D is rejected before merge regardless of golden-set behavior. Simultaneously promote `hidden_gem_hit_rate` from tracking-only to a hard commit gate: `bench.py audit` exits non-zero if the metric drops below the rolling-30-day baseline by >2σ. The 100-cmdr golden set becomes a regression sanity check, not the maximand.
**Rationale.** Gap-report queue structurally exhausted on the 100-cmdr golden set, and `weight_hint` / `hidden_gem` / writer-side audit are all advisory rather than authoritative. The system says "find hidden gems from mechanics" three times in MEMORY.md but every gate is golden-set-vs-EDHREC-bound. A universe-wide lift gate is universe-clean (no popularity oracle), uses BM25-baseline IR convention, and unblocks survivor #5 by giving mined rules a quantitative pass/fail.
**Downsides.** Promoting hidden_gem to commit-gate triggers the FR6 escalation in `memory/feedback_hidden_gem_metric.md` — needs a separate `ce-brainstorm` cycle. Lift-threshold tuning (1.10) needs sweep validation.
**Confidence:** 75% · **Complexity:** Medium · **Status:** Unexplored

### 4. Dark-launch shadow mode + counterfactual replay of reverted rules
**Description.** New rules ship at `weight=0` with `shadow=True`; the scorer evaluates them but excludes from final ranking. Tensor accrues per-(commander, candidate, shadow_rule) firings as a side channel. After N audits, run a counterfactual replay: "if `weight=w`, would NDCG and hidden_gem move in the right direction *given everything else that has shipped since*?" Promote (set weight via survivor #1) when both signals agree. Critically, also persist firings for the **24 reverted rules** in `RULE_HISTORY.md`.
**Rationale.** Reverts compound: every revert was correct against its contemporaneous catalogue but stops being correct as the catalogue evolves. Right now there's no mechanism to revisit a revert except by fully re-implementing it. Compounds with #1 (counterfactual replay = optimizer-over-shadow-channel) and #5 (mined rules ship in shadow — zero opportunity cost).
**Downsides.** Doubles tensor write cost per audit (still tractable). Some reverted rules were reverted for *quality* reasons (vacuum-fill, flat-noise) that won't change — needs filtering on Gate-A/B/C/D pass before counterfactual replay is meaningful.
**Confidence:** 70% · **Complexity:** Medium · **Status:** Unexplored

### 5. AnyBURL/PyClause rule mining over Forge-precon × port-graph triples
**Description.** Convert the 670 Forge precon decks into triples `(commander_oid, in_deck_with, candidate_oid)` joined to the typed `port_nodes` projection. Feed to AnyBURL or PyClause to mine confidence-scored Horn rules over port-graph predicates. Auto-emit highest-confidence, lowest-collinearity rules as `rules_seed.json` rows; ship them in shadow mode (#4); promote via Gate D (#3) and #1's optimizer.
**Rationale.** The gap-report queue is structurally exhausted on the golden set. We need a *different* discovery surface that proposes rules we wouldn't have noticed by walking unmatched ports. Forge precons are designer-authored — categorically distinct from EDHREC popularity. AnyBURL was designed for anytime learning on small graphs; published 70-96% rule-count reduction at 91% retention via subset selection. Stays inside the no-EDHREC constraint because mining is design-time.
**Downsides.** Boldest survivor. Small corpus (~670 decks) is at the lower edge of AnyBURL's documented range — confidence statistics will be noisy. PPMI-trap precedent means smoothing parameters need empirical validation. Highest risk of producing rules that pass mining confidence but fail Gate B (vacuum-fill).
**Confidence:** 65% · **Complexity:** High · **Sequencing:** land #2 and #3 first · **Status:** Unexplored

### 6. RRF hybrid combiner over (rule, embedding, forge_oracle) channels
**Description.** Compute three independent rankings per commander: (a) current IDF-weighted rule score, (b) cosine to the cached commander target vector, (c) Forge PPMI co-occurrence rank. Fuse via RRF: `final_rank = Σ 1/(k + rank_i)`, k≈10-20 for top-30 lists. RRF is parameter-light, immune to score-scale mismatch, and degrades gracefully when one channel is silent.
**Rationale.** The embedding additive sweep was marginal because additive blending conflates two different signal qualities — sparse-precise rules vs dense-recall content. RRF is the BM25+neural pattern. It also turns `forge_oracle` from design-time-only into a runtime signal *without* violating the no-EDHREC constraint. Hidden_gem rate should specifically benefit because RRF lifts candidates the rule layer ranks at zero but the embedding/oracle layer has signal on.
**Downsides.** Three-channel system increases inference complexity. Forge oracle's PPMI table needs to be loaded in the inference path (currently offline-only, hash-gated) — careful firewall needed.
**Confidence:** 70% · **Complexity:** Medium · **Sequencing:** ship after #1 and #2 · **Status:** Unexplored

## Cross-Cutting Guardrail

Every survivor's implementation passes through `bench.py audit` before merge (per `feedback_audit_every_change.md`). Pure-infra changes (#2, #4, #6 channel plumbing) exempt from NDCG gate but must pass identity check on existing rule set.

## Sequencing

**#2 → #1 → #3 → #4 → #5 → #6.** Each lands in 1-3 days except #5 (week+). The chain converts existing infra into the autonomous scaffold→audit→ship loop the focus hint asks for.

## Rejection Summary

| # | Idea | Reason rejected |
|---|------|-----------------|
| 1 | Per-density × per-color-identity weight cells | Premature; fold into #1's option set after it lands. |
| 2 | NMF on contribution tensor for latent rule families | Speculative on sparse data; better as later diagnostic. |
| 3 | ECFP/Morgan fingerprints replacing TF-IDF embedding | Substrate rebuild for marginal gain. |
| 4 | Precompute commander × top-2k offline | Solves latency we don't have; wrong leverage. |
| 5 | LLM-generated `scaffold_rule.py` generator templates | Auditability concern; #5's mining produces declarative rules directly. |
| 6 | Forge oracle as inference-time additive PPMI score | Subsumed by #6 RRF channel. |
| 7 | Embedding inverted decay (universal backstop for unreleased cards) | Partially subsumed by #6. Revisit if unreleased-card complaints surface. |
| 8 | Drop NDCG entirely, evaluate on internal consistency only | Too aggressive; raw DCG inside #1 is the moderate version. |
| 9 | TREC-pooling LLM-judge labels for non-EDHREC golden set | Labeling cost unjustified before #1-#3 land. |
| 10 | Auto-revert on regression | Too brittle; #1's human-in-loop diff review is safer first step. |
| 11 | Universe-side gates demote golden to sanity-check | Same intent as #3, less specific. |
| 12 | Forge precons as constraint set, not corpus | Subsumed by #5. |
| 13 | Rules-as-data as default authoring surface | Process change; implied once #2 closes the writer-side gap. |
| 14 | Generate 200 narrow rules combinatorially | Subsumed by #5 (AnyBURL is the principled version). |
| 15 | LambdaMART projecting to linear weights | Companion to #1, not a separate survivor. |
| 16 | Self-loop lift as primary metric | Folded into #3 (Gate D). |
| 17 | Embedding-diagnosed rule desert detector | Useful diagnostic but doesn't move model directly. |
| 18 | CI-gated audit-on-PR with config-hash diff | Process improvement; doesn't move model. |
| 19 | 3-model ensemble framing | Same architecture as #6 RRF. |
| 20 | Stratified golden-set expansion | Marginal vs #3's universe-side promotion. |

Full rejection list and raw 49-candidate dump in scratch (`/var/folders/.../compound-engineering/ce-ideate/b7e3d4a1/`).

---

## Continuation 2026-05-02 — Gap-Attempt Strategy (Survivors v2)

**Trigger.** User asked "which top entries from `gap_report.py` are worth attempting next?" Continuation from prior survivors after tensor optimizer (prior #1) shipped without changing the gap-queue exhaustion verdict.

### Delta since 2026-04-26
- Tensor optimizer M1 SHIPPED. 4 runs, converging at tiny gradients; 6 rules accept consistent multiplier tweaks; 9 dead-keys on 500-train split; **no proposal applied yet**.
- 500-cmdr fixture (`tests/fixtures/golden_set_run_500.json`) shipped — but only `--optimize` consumes it; `gap_report.py` still ranks by universe signal.
- Zero new complement rules shipped. All 5 prior blockers persist unchanged.
- Top-20 gap_report categorization unchanged from 2026-04-25 verdict: 9 prior-revert-with-template, 7 needs_template, 1 untried-clean (#15, ZERO golden coverage). 6 of 7 generator templates BLOCKED.

### New external grounding
- **CMIM** (Fleuret 2004; arXiv 2306.03301) — incremental feature selection by `I(new; label | existing)`. Reframes "which rule next?" as "which rule has highest CMI given current 60?"
- **MOFSRank** — redundancy in ranking-contribution space (rank distribution VIF), not value space.
- **DR estimator** (Saito RecSys 2020) — per-query DR > SNIPS for small query sets (n<150).
- **Subsumption-lattice rule pruning** (CSGM, Zang & Xu) — redundancy = prediction-set ⊆ union of existing.
- **SOFaiR** (FAccT 2024) — OWA loss explicitly protects bottom-k queries.
- **InfoRank** (WWW 2024) — CMI as debiasing loss penalising features informative-of-logging-artifact.

### Survivors v2 (6 of 48 raw)

#### 1. Pre-flight gate stack for gap_report queue
**Description.** A <60s pipeline that runs BEFORE generator-writing: (a) golden-coverage SQL pre-check (drop entries with 0 hits in 500-cmdr fixture), (b) paper-rule SQL-only scoring simulator predicting per-cmdr top-30 contribution from a `(commander_gate, candidate_predicate, idf)` triple, (c) embedding-space candidate-cluster shape prior (predicts vacuum-fill / flat-noise from cosine spread). Reject candidates that fail any stage.
**Rationale.** Three of four documented failure modes (untestable, vacuum-fill, flat-noise) are deterministically detectable from data the audit already produces. Today they're all detected POST-SHIP via revert. The damage_prevention_voltron incident (2026-04-25) wasted a 250-line generator on a rule with zero golden coverage — one SQL query would have killed it.
**Downsides.** Plumbing-heavy. The simulator step needs care to match production scoring exactly. Embedding-shape prior is heuristic, not deterministic.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Explored (selected for ce-brainstorm 2026-05-02)

#### 2. Demand-side gap reframe (replace universe ranker)
**Description.** Replace `gap_report.py`'s `commanders × (1-covered) × forge_signal` ranker with: per-commander `hidden_gem_deficit = |edhrec_top_30 \ our_top_30|` over the 500-cmdr fixture, then surface (port_node_kind, subkind) shapes most over-represented in lost cards. Top-N becomes "shapes whose absence systematically hurts golden commanders," not "shapes underrepresented in the universe."
**Rationale.** "Structurally exhausted" is an artifact of the supply-side ranker. Aligns with `feedback_edhrec_not_goal.md` north-star. Stays inside anti-goals — EDHREC is used only as a gap-detection signal, exactly as `--inspect-gems` already uses it. Compounds with #1 (every demand-side proposal automatically passes the golden-coverage prefilter).
**Downsides.** Requires per-(cmdr, candidate, lost-card-port-shape) aggregation table; not a 1-day build. May surface gaps that need new vocabulary, not just new rules.
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored

#### 3. Optimizer-as-gap-discovery (mine M1 artifacts already produced)
**Description.** Build `bench.py audit --gap-from-optimizer` that emits: dead-keys → predicate-loosening proposals; clamp-saturating rules → "adjacent rule missing from this family" gaps; gradient nullspace → CMIM-flavored "what direction in feature space is unexplained?" Optionally extend with PCA on the persisted contribution tensor.
**Rationale.** The data exists. The optimizer paid the compute cost. The 9 dead-keys are stealth gaps — rules that exist but don't fire are evidence the predicate-vocabulary needs loosening. Compounds with #1 (every optimizer-derived gap inherits a contribution prior).
**Downsides.** Only works for rule families the optimizer already touches; novel families won't have a gradient signal. Tightens coupling between optimizer state and gap_report state.
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored

#### 4. Multiplier-zero shipping pipeline (industrialize prior #4)
**Description.** New rules ship to `complement_rules/` (or `rules_seed.json`) at `_RULE_QUALITY_MULTIPLIER = 0.0`. They fire in the trace, populate the tensor, contribute zero to scores. The next `--optimize` run picks the multiplier from the grid; positive convergence → auto-promote PR; converges to 0 or below threshold → auto-quarantine. Single PR can ship 5-10 candidate rules at zero-risk.
**Rationale.** Prior #4 (dark-launch shadow mode) defined the channel; this defines the missing PROMOTION mechanism. Drops cost-per-attempt close to zero, breaks 1-rule-per-PR cadence. Cascades with #2 + #3.
**Downsides.** Requires writer-side audit gate compiler (prior #2) to land first. Tensor write cost roughly doubles. Some rules need positive weight to be testable at all.
**Confidence:** 75% · **Complexity:** Medium · **Sequencing:** after prior #2 · **Status:** Unexplored

#### 5. Gate-relaxation as gap-closure (extend optimizer action space)
**Description.** Extend tensor optimizer's action space from `_RULE_QUALITY_MULTIPLIER` to ALSO include selected rule gate constants — `min_count >= 2/3`, `_FLAT_WEIGHT_OVERRIDES` density thresholds, IDF-clipping thresholds. Optimizer can then close some gaps by relaxing existing rules instead of requiring new ones.
**Rationale.** Documented next-step (B) in queue-dry post-mortem. Gate constants are arbitrarily-chosen at scaffold time and never re-validated. Avoids vacuum-fill and shared-axis-different-archetype regression entirely (no new rule introduced). Compounds with #3 (optimizer-driven mining specifically targets "which gate on which rule should move?").
**Downsides.** Action space combinatorially larger; SPSA convergence slower. Some gates encode mechanical correctness; relaxing silently breaks semantics. Needs per-gate "relaxable" annotation.
**Confidence:** 75% · **Complexity:** Medium · **Status:** Unexplored

#### 6. Revert quarantine with auto-resurrect probes
**Description.** Every `git revert` of a complement rule writes `quarantine/<rule_id>.json` with failure metric values, hypothesized blocker condition, and a SQL probe evaluating the condition. After every cardsfolder import / fixture expansion / vocab expansion, importer evaluates every probe. Passing probes auto-promote rule into a "re-attempt" queue with fresh audit job scheduled.
**Rationale.** 9 of top-20 gap_report entries are prior-revert-with-template. Reverts compound: each was correct against contemporaneous catalogue but stops being correct as catalogue evolves. JSON quarantine + SQL probe captures revert-time intent (when memory is freshest). Compounds with #4 (resurrected rules ship at multiplier=0, optimizer triages).
**Downsides.** Requires discipline at revert time. Some reverts are quality-mode (vacuum-fill, flat-noise) that no data refresh can fix — needs Gates A+B filter before counterfactual replay is meaningful.
**Confidence:** 70% · **Complexity:** Medium · **Status:** Unexplored

### Sequencing v2
**#1 → #2 → #5 → #3 → #6 → #4.** #1 and #3 are pure-infra (no scoring change). #2 is `gap_report.py` rewrite. #5 extends optimizer M1. #4 needs prior #2 (writer-side audit) to land first.

### Cross-Cutting Observation
Three survivors (#1, #3, #5) compose into a tight loop: pre-screen blocks bad candidates → optimizer mines existing data for gap signal → existing rules retune via gate-relaxation. That's an answer to "which gaps?" that doesn't require attempting any new entry from the current `gap_report.py`.

### Rejection Summary v2

| # | Idea | Reason rejected |
|---|------|-----------------|
| 21 | Forced substitution sweep (every new rule must displace existing) | Premature; folds into #5. |
| 22 | LLM-generates the generator from gap row | Auditability concern (per prior doc #5 rejection). Repeat. |
| 23 | Causal-ablation primary signal replacing NDCG | Too aggressive (per prior doc #8 rejection). Repeat. |
| 24 | Pure-intrinsic eval — drop EDHREC entirely | Anti-goal pretends EDHREC isn't useful as sanity check. Over-restrictive. |
| 25 | Optimizer-at-inference Thompson-sampling bandit | Solves latency we don't have; inference is offline. |
| 26 | Synthetic Commander Stress-Test Fixture | Conflicts with `optimizer-fixture-size-2026-04-30.md` lesson. |
| 27 | 10-cmdr informativeness sieve | Same conflict (smaller fixtures = worse signal). |
| 28 | Per-half oracle_ids for split/MDFC cards | Substrate change; not gap-attempt strategy. |
| 29 | Cascade pathway depth-3/depth-4 generator | Better as a separate ce-brainstorm cycle. |
| 30 | Wikipedia annual notability re-review (cull obsolete rules) | Subsumed by #5 + #3. |
| 31 | ABC analysis cull C-class rules first | Premature deletion risky; folds into #3 + #5. |
| 32 | Top-200 horizon instead of top-30 | Useful tweak; folds into #2 as the per-cmdr aggregator. |
| 33 | Wine triangulation (≥2 of 3 detectors agree) | Folds into #1's gate stack as one panelist. |
| 34 | Forge-Oracle anti-coverage diff as gap generator | Already exists as `forge_oracle.py propose-rules`; missing piece is pre-screen (#1). |
| 35 | Bandit over (template, signature, gate-version) | Subsumed by #4. |
| 36 | Embedding-space cluster-holes as gap reframe | Diagnostic-only; subsumed by #2 + #3 CMIM extension. |
| 37 | Rule-pair interaction gaps (Friedman H-statistic) | Pathway.py handles depth-2; deeper without specific failure-mode driver is speculative. |
| 38 | Drug-discovery SA score for rules | Folds into #1's pre-screen stack as a column. |
| 39 | Gap-cell siblings clustering | Folds into #2 (demand-side ranker naturally surfaces families). |
| 40 | Generator industrialization stack (declarative-only + DSL + LLM) | Direction not strategy; defer to separate ce-brainstorm. |
| 41 | Audit-before-scaffold via synthetic injection | Subsumed by #1's paper-rule simulator. |
| 42 | Vaccine Phase IIa — focused 20-cmdr sub-fixture | Useful add to #1; not own survivor. |

Full raw 48-candidate dump and dedup clusters in scratch (`/var/folders/.../compound-engineering/ce-ideate/a3f7c2e1/raw-candidates.md`).
