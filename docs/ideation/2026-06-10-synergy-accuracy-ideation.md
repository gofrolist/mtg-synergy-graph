---
date: 2026-06-10
updated: 2026-07-02
topic: synergy-accuracy
focus: get recommendation accuracy closer to EDHREC validation (NDCG@30 ~0.256 plateau) while staying EDHREC-free at inference
mode: repo-grounded
---

# Ideation: Closing the Gap to EDHREC Without Using EDHREC

## Grounding Context

**Codebase.** NDCG@30 ~0.256 on the 100-cmdr golden set (down from 0.262 in April); hidden_gem_hit_rate 0.8423 (healthy). 62 complement rules over 108k ports / 32k cards; IDF-weighted linear sum. Tooling: bench.py contribution tensor + per-rule ablation, gap_report Stage A, rule_quality_gate, coordinate-ascent optimizer (500-cmdr fixture), embeddings (flag OFF), forge_oracle PPMI (670 precons, design-time), typed port_graph + declarative rules path, depth-2 pathway family (biggest win ever: +209 agg, gem 0.73→0.84).

**Pre-rejected levers (need a named delta to re-run).** Linear weight retuning (near-optimal on 500-cmdr); embedding contribution flip (two sweeps, +0.0067 < 0.02 bar; unlock = new target composition or new vectorizer); walker rule shipping (queue exhausted, 6/7 templates blocked); BM25 IDF under NDCG-primary (cliffs −0.53; but gem +0.04 → re-probe legit under gem-primary); rule pruning (zero collinear pairs); card_hints rules (3 reverts).

**External grounding.** EDHREC moved to symmetric lift conditioned on color identity; RRF rank fusion; per-query isotonic (PAV) calibration; XGBoost rank:ndcg LambdaMART on small query sets (depth ≤4, leave-N-out CV); drug-synergy KG link prediction (popularity-debiased training); Saito & Joachims 2023 (nDCG can anti-correlate with true quality — raw DCG often better); CPR pairwise ranker for MTG draft (arXiv 2407.05879).

**Verified repo fact surfaced this session.** The inference sort key is `(-score, cmc, edhrec_rank, name)` — EDHREC is at inference today as a tiebreaker, in tension with the "no EDHREC at inference" constraint; unreleased cards have no edhrec_rank.

48 raw ideas from 6 framing agents; survivors below. Raw dump + dedupe clusters in scratch (`$TMPDIR/compound-engineering/ce-ideate/9a7af3d2/`).

## Forensics Evidence (2026-07-02 continuation)

Idea #1 shipped as PR #75; run on the post-a1eae6e refreshed DB (embeddings rebuilt, re-pinned, tensor ~440k rows). Aggregate NDCG@30 0.236 / raw DCG 0.847 (0.231/0.828 pre-fix — the color-identity fix cleared all 9 FILTERED color_illegal misses, all Chatterfang). Bucket proportions over 2,642 misses:

| bucket | share | reading |
|--------|------:|---------|
| OUTRANKED | 46.2% | calibration gap — co-dominant |
| NO_RULES | 41.5% | vocabulary gap — co-dominant |
| NEAR_MISS | 7.4% | within reach of small weight moves |
| DATA_GAP | 5.0% | 115/131 are unknown_ports |
| FILTERED | 0.0% | resolved by a1eae6e |

Sharper findings:

- **OUTRANKED misses are deep**: 447 sit at rank >500, 590 at 101–500, only 183 at 61–100. A top-200 re-rank layer (#3 strong variant) mechanically cannot reach ~85% of them; the fix must move base scores.
- **Displacer monoculture confirmed**: many live top-30s are 70–100% one rule family (Zur value_engine 100%, Xenagos self_bridging_cascade 100%, Feather cost_reducer 97.5%, Marrow-Gnawer tribal_density 93.5%). Meanwhile OUTRANKED missed cards' own contributions are dominated by generic density families (spell_density 24.2% + tribal_density 21.8%). Pattern: single-family flooding displaces EDHREC's diversified picks → re-funds the *weak* variant of #3 (concave within-family aggregation at scoring time), which was provisionally rejected pending "forensics picks intensity" — intensity is now picked.
- **NO_RULES port shapes are common, not exotic**: ZONECHANGE 161, STATIC_BUFF 159, TAP 132, ETB 129, DRAW 128 cards — plus UNKNOWN trigger.Phase (56) and effect.Cleanup (49) as vocabulary-expansion targets. Funds #6 and makes rejected idea "residual-to-rule" (defer condition: bucket-(a) dominance) eligible for reconsideration at 41.5%.
- **Tiebreaker ablation (R8): +0.000000** — the `edhrec_rank` inference tiebreaker earns zero NDCG credit; dropping it is a free EDHREC-clean purity cleanup.
- **R9 justified-divergence gate pass-rate 0.85** — hidden-gem behavior healthy; 285 unjustified divergent picks are the (modest) evidence base for #5.

## Ranked Ideas

### 1. Divergence forensics: per-miss failure taxonomy + metric reform
**Description:** `bench.py` reporter classifying every (commander, missed-EDHREC-top-30-card) pair via the contribution tensor into buckets: (a) zero rules fired (vocabulary gap), (b) rules fired but outranked (calibration gap), (c) ranked 31–60 (near-miss), (d) UNKNOWN port extraction (data gap). Bundled: re-grade golden-set gains with EDHREC's graded synergy score (deck% − color_baseline%), report raw DCG alongside NDCG, ablate the edhrec_rank tiebreaker to measure unearned credit.
**Rationale:** Every rejected lever was tried blind. Bucket proportions decide which of ideas 2–7 to fund; graded labels + raw DCG test whether 0.256 partly measures label coarseness; tiebreaker ablation restores honesty to the EDHREC-clean claim.
**Downsides:** Pure diagnostics; moves no metric by itself.
**Confidence:** 90% · **Complexity:** Low-Medium · **Status:** SHIPPED (PR #75, merged 2026-07-01; standing instrument `bench.py audit --forensics`)

### 2. Color-identity-conditioned IDF denominator
**Description:** Compute IDF over the commander's color-identity-legal pool instead of the 32k universe (optionally restrict the ranking pool too). Single choke point `_compute_idf_basis` in universal_scorer.py; cacheable per color bucket.
**Rationale:** 4-frame convergence; EDHREC's own methodological move; documented open direction; failure mode distinct from rejected BM25 (denominator population, not curve shape).
**Downsides:** Per-commander cliff risk (measure with per-commander histogram); cache plumbing.
**Confidence:** 75% · **Complexity:** Low-Medium · **Status:** PROBED & DECLINED (2026-07-02, plan 2026-07-02-001): small-N key saturation cliff — 28/500 commanders < −0.05, aggregate −0.0076 (CI excludes 0), gem down on both instruments, OUTRANKED unchanged; multiplier re-sweep ruled out calibration as confound. Population axis closed; see docs/solutions/best-practices/color-conditioned-idf-null-result-2026-07-02.md. Counter-hypothesis (displacer amplification) fired → evidence now points at #3-weak (concave within-family aggregation) as the next lever.

### 3. Deck-shaped top-30: redundancy-penalized greedy selection
**Description:** Re-rank top-200 by greedy marginal gain: discount candidates whose rule-firing niche (axis/family vector from the tensor) is already covered by higher picks (submodular/MMR). Output order is the top-30. Weak variant: concave within-axis aggregation at scoring time.
**Rationale:** 4-frame convergence on "EDHREC top-30 is a portfolio, ours is argmax-30" as the biggest structural mismatch. Re-rank layer leaves base scoring untouched; explainable pick-order.
**Downsides:** Selection-layer novelty; penalty shape needs sweeping.
**Confidence:** 70% · **Complexity:** Medium · **Status:** SPLIT VERDICT — strong variant (top-200 re-rank) DEFUNDED: ~85% of OUTRANKED misses sit beyond rank 200. Weak variant (concave within-family aggregation at scoring time) RE-FUNDED by displacer-monoculture evidence; it moves base scores everywhere.

### 4. Lift normalization: score minus expected-baseline panel
**Description:** Rank by `score(cmdr, card) − λ·panel_mean(card)` where panel_mean is precomputed at import over a fixed ~200-commander panel using our own scorer. Generic value engines sink; commander-specific spikes rise.
**Rationale:** Mechanics-only rebuild of EDHREC's synergy = deck% − baseline% formula; the missing "specificity of the card" term IDF can't express; works for unreleased cards immediately.
**Downsides:** λ tuning; partially obsoletes the staple bonus — interaction needs care.
**Confidence:** 65% · **Complexity:** Medium · **Status:** FUNDED (deep OUTRANKED ranks require base-score change; second in calibration track behind #2)

### 5. Anti-synergy unmet-demand penalty channel
**Description:** Demote candidates whose dominant ports are payoffs for an axis the commander provably doesn't feed (inverted axis-feeder gates); plus a declarative `conflict_map` seed for destructive pairs (gy-strategy commander vs gy-hate card, etc.). The `- anti_synergy` slot in the score formula already exists and is nearly unused.
**Rationale:** All 62 rules are promotion-side; demotion is an untouched lever targeting the most visible bad recommendations ("needs lifegain, commander provides none").
**Downsides:** False-positive demotions when the 99 (not the commander) supply the axis; gating discipline required.
**Confidence:** 70% · **Complexity:** Medium · **Status:** WEAKLY FUNDED (forensics measures misses, not bad picks; evidence base is R9's 285 unjustified divergent picks — revisit after calibration track)

### 6. PPMI-mined event-map substrate expansion
**Description:** Mine high-PPMI precon card pairs (forge_oracle), project both sides through port_nodes, surface trigger→effect type pairs co-occurring strongly but absent from event_match_seed.json. Each proposal is a one-line JSON row, individually audit-gated. Depth-3 pathway search explicitly deferred.
**Rationale:** Feeds the proven pathway winner; walker-exhaustion verdict covered rule templates, never substrate rows; design-time co-occurrence only.
**Downsides:** Sparse-corpus PPMI noise (smoothing trap documented — verify non-degenerate output).
**Confidence:** 70% · **Complexity:** Low-Medium · **Status:** FUNDED (NO_RULES 41.5% co-dominant; concrete targets in the NO_RULES port-shape table + 115 unknown_ports DATA_GAP misses)

### 7. Nonlinear interaction learner distilled to deterministic artifact
**Description:** Train LambdaMART (XGBoost rank:ndcg, depth ≤4, leave-20-cmdrs-out CV) or Texel-style pairwise logistic over per-rule contribution features at 500–2,761-commander scale; distill into committed `interaction_seed.json` (monotone transforms + sparse rule-pair multipliers) loaded like scoring_weights.json. Low-risk variant: model as discovery oracle only — mine top SHAP interactions into new declarative rules.
**Rationale:** 5-frame convergence. The optimizer's "near-optimal" verdict exhausted only the linear class; pathway +209 is existence proof interactions carry the remaining signal. This is the named delta past the weight-retune rejection.
**Downsides:** Boldest; overfitting discipline; heavy design-time EDHREC label use pushes against hidden-gem philosophy.
**Confidence:** 65% · **Complexity:** High · **Status:** STILL LAST (evidence consistent — missed cards fire only generic-density rules while specific families flood — but cheaper levers #2/#3-weak/#6 must exhaust first)

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Concave within-axis aggregation | Weak variant of #3; forensics picks intensity |
| 2 | Forge CardRanker / staple quality prior | Premise (staple blindness) unverified until #1; may move away from a lift-graded target |
| 3 | Rocchio pseudo-relevance-feedback expansion | Popularity-echo risk; overlaps #3 machinery |
| 4 | RRF channel fusion at inference | Already a 2026-04-26 survivor; no new delta |
| 5 | Auto-mirrored rules compiler | Vacuum-fill risk; subsumed by shadow pipeline |
| 6 | Residual-to-rule / AnyBURL closed loop | Defer until #1 shows bucket-(a) dominates |
| 7 | Forge goldfish-simulation oracle | Project-sized harness; revisit if #1 leaves the metric tension unresolved |
| 8 | Rate/cadence "impedance matching" features | Novel but speculative extraction burden |
| 9 | IDF functional-form refit (isotonic N→weight) | Revisit after #2 lands |
| 10 | Commander-target v2 demand-image embedding | Embedding headroom shrank below documented bar; after other channels |

## Sequencing

**#1 → (#2, #6 in parallel) → #3/#4/#5 funded by #1's bucket sizes → #7 last.** Every scoring-path change passes `bench.py audit` per `feedback_audit_every_change.md`; #1 is read-side infra (no re-pin).

**Updated 2026-07-02 (post-#1 evidence):** #1 shipped. Next wave — **#2 color-conditioned IDF** and **#6 PPMI substrate expansion** in parallel (both Low-Medium, orthogonal, each individually audit-gated). Then **#3-weak concave within-family aggregation** (monoculture evidence) and **#4 lift normalization** for the remaining calibration gap; #5 after; #7 last. Free cleanup any time: drop the `edhrec_rank` sort tiebreaker (R8 ablation proved +0.000000 credit) — needs one audit run since tie order shifts, but restores a strictly EDHREC-clean inference path.
