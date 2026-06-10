---
date: 2026-06-10
topic: divergence-forensics
---

# Divergence Forensics: Per-Miss Failure Taxonomy + Metric Sidecars

## Problem Frame

NDCG@30 vs the EDHREC golden set has plateaued at ~0.256 (down from 0.262 in April). Every accuracy lever tried so far — linear weight retuning, embedding contribution, more walker rules, BM25 IDF — was attempted *blind* against an opaque aggregate, and all were declined. We do not know which failure class dominates the remaining headroom: cards we have no vocabulary for, cards we see but mis-rank, near-misses, or port-extraction gaps. This work converts the plateau into sized, separately fundable problems and hardens the measurement enough to trust the next round of verdicts. It directly informs which queued scoring reform (color-conditioned IDF, deck-shaped selection, anti-synergy channel, interaction learner) gets built next.

**Measurement ground truth (verified):** the canonical 0.256 comes from `validate._run_one` → `engine.page()` (production ordering `(-score, cmc, edhrec_rank, name)` with NULL sentinels cmc→99.0, edhrec_rank→`UNRANKED_EDHREC_SENTINEL`) → `compute_ndcg` against **graded labels**: `edhrec_labels_for_commander(conn, commander, grade_floor=0.0)` returns raw `edhrec_card_synergy.synergy` values across **all** sections. The metric is already graded, not binary; the forensics path reuses exactly these labels everywhere (bench's `load_edhrec_labels` 3.0/1.0 scheme is a second, divergent convention that forensics must NOT use).

**Rank-source ground truth (verified, drove a design reversal):** the pinned fixture stores raw `score_all_universal` output — *before* `engine.page()`'s candidate filters (color-identity subset, `legal_commander`, non-EDH card types, commander self-exclusion). Measured: ~37% of pinned (commander, card) pairs are color-illegal for their commander; after filtering, 46/100 commanders retain < 61 usable pinned scores and 9 retain < 30. Pinned scores therefore CANNOT reproduce the production ranking, and all ranks in this design come from a live production-ranking pass instead (see Key Decisions).

**Stated bet (explicit):** this instrument diagnoses divergence from EDHREC; it does not promote EDHREC to the optimization target. The project north star remains hidden-gems-from-mechanics with EDHREC as sanity check (`memory/feedback_edhrec_not_goal.md`). The taxonomy therefore reports justified divergence (R9) alongside misses, and the gem axis rides in every tracked row (R7), so an EDHREC-closeness reform can never look free if it costs gems (the BM25 episode: aggregate NDCG −0.007 but hidden-gem rate +0.04).

Origin: `docs/ideation/2026-06-10-synergy-accuracy-ideation.md` idea #1 (4-frame convergence).

## Requirements

**Miss taxonomy (standing instrument)**

- R1. A `bench.py audit --forensics` subcommand classifies every (commander, missed card) pair into exactly one bucket.
  - *Miss universe:* the commander's top-30 label cards by graded `synergy` from `edhrec_labels_for_commander(conn, commander, grade_floor=0.0)` (all sections), **closed under boundary ties** (all label cards with synergy ≥ the 30th-highest value — 53/100 commanders have ties spanning the rank-30/31 boundary, and NDCG@30 is indifferent between tie members), that are absent from our top-30. HS-section membership is a reported attribute on each miss.
  - *Our ranking:* one live production-ranking pass per commander via the same `engine.page()` path that produced the 0.256 figure, taken to a depth that covers all label cards (depth/mechanism at planning). NOT reconstructed from pinned scores — see rank-source ground truth above.
  - *Buckets, in precedence order:*
    1. **NEAR_MISS** — card ranks 31–60 in the live production ranking.
    2. **OUTRANKED** — card ranks 61+ AND has rows in the persisted `rule_contributions` tensor for this commander. Calibration/aggregation gap. Cards ranked 61+ with NO tensor rows but a nonzero score (staple-bonus-only side channel) get a `staple_only` sub-tag here, not NO_RULES — they are seen and ranked, just rule-uncovered.
    3. **DATA_GAP** — card unranked/unscored AND (absent from `cards`, OR zero `port_nodes` rows, OR >50% of its `port_nodes` rows have `node_kind = 'UNKNOWN'`). Reason code (`card_absent` / `no_ports` / `unknown_ports`) — the sub-cases map to different owners (name-normalization/import vs extraction vs vocabulary).
    4. **NO_RULES** — card unranked/unscored, no tensor rows, but ports project to known node kinds. Rule-coverage gap.
  - *Known caveat:* tensor-row absence is not strictly "no rules fired" — `universal_scorer` drops rows whose contribution nets to exactly 0.0. Planning quantifies prevalence; if material, such cards get a `netted_zero` sub-tag under OUTRANKED.
- R2. The report emits aggregate bucket proportions, per-commander bucket counts (with per-commander tensor-coverage counts as caveat columns), and a worst-divergence commander leaderboard (misses weighted by graded synergy), as markdown to `.audit/forensics.md` (default) and `--format json` (JSON schema for nested breakdowns fixed at planning). **Within OUTRANKED** — the expected majority bucket — the report breaks down by rank quantile (61–100 / 101–500 / >500) and by dominant rule family of the missed card's tensor contributions. **Displacer profile:** per commander, the report also summarizes the rule-family contribution profile of the cards occupying our top-30 — the anti-synergy and deck-shaped-selection reforms act on over-scored displacers, not on the misses, and arbitration between the three calibration reforms needs both sides.
- R3. For the NO_RULES bucket only: a frequency-ranked list of the top shared `(node_kind, subkind)` port shapes across all NO_RULES cards, so the bucket is immediately actionable as rule proposals. (Full demand-side clustering is out of scope — see boundaries.)
- R4. *(Mechanism revised during planning — see plan Key Technical Decisions.)* Bucket discrimination and breakdowns run as SQL over the persisted tensor and `edhrec_card_synergy` using the repo's established two-connection pattern (`open_db()` for `synergy.db` + `sqlite3.connect` for `tags.db`; joins in Python — the repo has zero ATTACH usage, which also dissolves the stale-duplicate-tables risk since there is no shared SQL namespace). Ranks come from the live pass in R1. Zero scoring-path changes (`bench.py audit --expect-identity` must still pass bitwise); runtime target: comparable to a standard `bench.py audit` run (the audit already live-scores all 100 commanders).

**Justified-divergence view**

- R9. For each commander, the report lists our top-30 picks that are absent from the **all-sections graded label set** (the same set as R1's miss universe — NOT the HS-top-30 set used by `hidden_gem_hit_rate_for_commander`, which therefore cannot be reused as-is; a thin wrapper applies the same plausibility-gate constants to the wider reference set). Output spec: a per-commander `justified_divergences` integer column, and OUTRANKED/NEAR_MISS counts annotated as `N (M justified)`; bucket proportions still sum to 100% of misses — justification annotates, it does not subtract. **Degeneracy guard:** at the current operating point (gem rate 0.8423) the binary plausibility gate passes nearly every divergent pick, so R9 also stratifies justified picks by gate margin (N_rules_firing and contribution-vs-median ratio) and reports the gate pass-rate — a 100% pass-rate row signals the gate is too loose to discriminate, which is itself a finding, not a success.

**Metric sidecars (tracking-only)**

- R5. Per-commander metric surfacing in the forensics report using the canonical validate-path labels and the live ranking from R1: per-commander NDCG@30 and its contribution to the aggregate. **Reconciliation assertion:** the run must verify its aggregate NDCG@30 matches the canonical validate-path aggregate within a small epsilon and fail loud on mismatch — this doubles as the end-to-end proof that the forensics ranking is production-faithful. Explicitly NOT a new label convention.
- R6. A raw DCG@30 sidecar on the same labels (Saito & Joachims 2023: nDCG's per-query normalization can anti-correlate with true quality), reported per-commander and aggregate.
- R7. Each `--forensics` run appends bucket proportions + aggregate NDCG + raw DCG + `hidden_gem_hit_rate` (the gem axis must stay readable next to the EDHREC-side series) to a sibling `.audit/forensics_history.csv` (appending columns to `.audit/history.csv` would break `bench/history.py::read_last()`'s strict header check), readable via `--trend forensics`. Provenance columns: `config_hash`, fixture-file SHA-256, and an EDHREC snapshot digest (no snapshot id exists today — derive as a cheap content digest over `edhrec_card_synergy`, e.g. row count + max rowid + sampled hash; exact derivation at planning; this is small NEW infrastructure, not reuse). Cross-row comparison is only valid within identical (config, snapshot) pairs; reform effects are read as pre/post deltas around a single change.

**Tiebreaker ablation (one-off experiment)**

- R8. A one-off measurement of EDHREC tiebreaker credit: re-sort the live-scored candidate set under **bracketing replacement keys** — weak (`(-score, name)`) and strong (`(-score, cmc, name)`) — and report the pair of NDCG@30 deltas vs the production key as a bounded range (a single delta is replacement-dependent and must not be presented as "the" credit). Decision rule: if the upper bound exceeds 0.01 NDCG, record a dated entry in `docs/RULE_HISTORY.md` flagging sort-key remediation as a candidate next-cycle change (out of scope here) — the flag must land in an artifact, not just the run output. Interpret R8 **before** reading bucket proportions: large tiebreaker credit means the top-30 boundary — and therefore the miss set — is partly a tiebreaker artifact.

## Success Criteria

- Every missed card lands in exactly one bucket; proportions sum to 100% of misses; per-commander tensor-coverage caveats are explicit.
- The R5 reconciliation assertion passes: forensics' aggregate NDCG@30 reproduces the canonical figure within epsilon.
- The report answers "where does the remaining NDCG headroom live?" with numbers — including inside OUTRANKED (rank-quantile + rule-family resolution on the miss side, displacer profiles on the other side) — and each bucket maps to a named next reform (NO_RULES → rule/vocabulary work; OUTRANKED → the three calibration reforms, arbitrated by miss-vs-displacer evidence; NEAR_MISS → tiebreak & small reordering; DATA_GAP → per reason code; overall bucket balance informs whether the interaction learner is warranted).
- R9 makes justified divergence visible without degenerating: the gate pass-rate and margin stratification are reported, and a too-loose gate is surfaced as a finding.
- R8 produces a bracketed credit range; if the upper bound > 0.01, the RULE_HISTORY flag entry exists.
- All deliverables land without a re-pin and without moving any gated metric; the history CSV gains one row per run with full provenance.

## Scope Boundaries

- **No scoring changes** — read-side infrastructure only; `--expect-identity` is the proof. (The live ranking pass *consumes* the production path; it does not modify it.)
- **No gate changes** — sidecar metrics are tracking-only; promoting any to a commit gate requires its own escalation cycle (same discipline as `hidden_gem_hit_rate` FR6).
- **100-commander fixture only in v1** — extending to the 500-set requires a tensor-populating run and is a follow-up (and the likely next step if v1 bucket proportions are too close to discriminate).
- **Light drill-down only** — R3 is a top-shapes frequency list; demand-side clustering, rule mining, and auto-scaffolding stay in their own ideation survivors.
- **No production sort-key change** — R8 measures and files a flag; it does not ship a fix.

## Key Decisions

- **Standing instrument over one-shot**: bucket proportions become a per-run tracked series, so every future reform shows which failure class it actually moved. (User-selected.)
- **Full package over taxonomy-only**: metric sidecars and tiebreaker ablation ride along because they share the same data plumbing and their verdicts gate how the taxonomy is read. (User-selected.)
- **Ranks come from a live production-ranking pass, not pinned-score reconstruction** (REVERSED in review pass 2): pinned scores are pre-filter artifacts — ~37% of pinned pairs are color-illegal for their commander and 46/100 commanders retain <61 legal pinned scores — so reconstruction cannot reproduce the 0.256 ordering. Live ranking via `engine.page()` is exact by construction, matches the existing `bench.py audit` live-vs-pin pattern, eliminates filter-replication coupling and NULL-sentinel duplication, and makes the R5 reconciliation assertion meaningful. The tensor is used for what it actually contains: per-rule contribution data for bucket discrimination and family breakdowns.
- **Miss universe = top-30 graded labels (all sections), closed under boundary ties**: aligns the taxonomy with the metric it explains; the HS section caps at ~10 cards per commander and covers only a slice of the label mass; tie-closure makes the miss set deterministic (53/100 commanders have boundary ties).
- **Reuse validate-path labels everywhere in forensics**: two label conventions exist (validate raw-synergy vs bench 3.0/1.0); forensics standardizes on the one that produced the canonical figure, with the exact call `edhrec_labels_for_commander(conn, commander, grade_floor=0.0)` in both R1 and R5.
- **Bucket precedence puts NEAR_MISS first and DATA_GAP before NO_RULES**: a card we nearly ranked is not a vocabulary problem, and a card with absent/UNKNOWN ports must not be miscounted as a missing-rule problem.

## Dependencies / Assumptions

- Persisted tensor is fresh under the current `config_hash` (verified: 440,652 rows, 100 commanders, single `config_hash`/`computed_at`; distinct candidates per commander range 5–26,119, mean ~4.4k — 72/100 commanders below 4k). The CLI must fail loud on config-hash mismatch with the live config. Coverage skew biases bucket discrimination on thin commanders; per-commander coverage counts are mandatory report columns.
- `edhrec_card_synergy` carries graded `synergy` values in `data/tags.db` (verified; actual range **[−0.84, 1.0]** with ~148k negative rows — harmless because the validate loader filters `synergy > 0`, but do not rely on a positive-only range elsewhere). Commanders with zero label rows are excluded from aggregates and listed separately (skip sentinel, mirroring `fetch_high_synergy_top_n`).
- `engine.page()` is deterministic given the DB and is the path that produced 0.256 (verified via `validate._run_one`); the live pass reuses it unmodified.
- `src/mtg_synergy_graph/bench/per_commander_ndcg.py` is reusable as a structural template only: its `load_edhrec_labels()` gain function is the 3.0/1.0 convention; R5 wires `validate.edhrec_labels_for_commander` instead.
- Name normalization between `edhrec_card_synergy.card_name` and `cards.name`: 32 distinct HS-section names verified to have no exact match (split/MDFC faces, punctuation) — load-bearing for DATA_GAP `card_absent` accuracy, handled at planning.
- `data/tags.db` contains stale duplicate copies of several synergy.db tables — forensics SQL must schema-qualify all table references after ATTACH.

## Outstanding Questions

### Deferred to Planning
- [Affects R1][Technical] Live-pass depth/mechanism: page to a fixed deep limit vs score-all-then-rank; how to obtain ranks for label cards outside the fetched window.
- [Affects R1][Technical] Name-normalization strategy (split/MDFC face mapping, punctuation folding) so mismatches don't inflate DATA_GAP `card_absent`.
- [Affects R1][Technical] Quantify `netted_zero` (contrib == 0.0 drop) and `staple_only` prevalence to size the sub-tags.
- [Affects R2][Technical] JSON schema for nested OUTRANKED/displacer breakdowns.
- [Affects R7][Technical] Exact EDHREC snapshot-digest derivation (file hash vs content digest over `edhrec_card_synergy`).
- [Affects R9][Technical] Gate-margin stratification thresholds; shared-helper boundary with `bench/hidden_gems.py` (same constants, wider reference set).
- [Affects R4][Technical] Whether the `tests/conftest.py` new-`*.db`-file sentinel needs an exception for ATTACH-pattern tests.

## Next Steps

-> `/ce-plan` for structured implementation planning (no blocking questions remain).
