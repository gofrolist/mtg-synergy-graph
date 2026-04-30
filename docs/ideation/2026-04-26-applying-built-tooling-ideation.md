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
**Confidence:** 85% · **Complexity:** Medium · **Status:** Explored

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
