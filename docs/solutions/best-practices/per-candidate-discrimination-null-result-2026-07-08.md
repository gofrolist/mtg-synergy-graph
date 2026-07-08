---
last_updated: 2026-07-08
module: universal_scorer
title: Per-candidate discrimination is dead against EDHREC-NDCG — no mechanical (or design-time-embedding) signal beats production out-of-sample
tags:
  - per-candidate-discrimination
  - rank-bonus
  - tiebreak
  - embeddings
  - flat-credit
  - null-result
  - kill-test
  - held-out-validation
problem_type: null-result
resolution_type: reference
applies_when:
  - Considering "add a per-candidate signal so the model can rank WITHIN a rule-class" (the recurring ceiling behind the spell_density and death_outlet null-results)
  - Tempted to swap the EDHREC-powered rank_bonus / edhrec_rank tiebreak for a mechanical per-candidate signal (port richness, embedding similarity, card "quality")
  - Re-opening the embedding contribution as a per-candidate micro-term (NOT the additive exp-decay term measured null in infrastructure-without-scoring-activation-2026-04-24.md)
  - Weighing the standing rank_bonus keep/remove decision
created: 2026-07-08
plan_ref: none (exploratory kill-test, no plan)
---

# Per-candidate discrimination: DECLINED (kill-test null-result)

## The reframing that made this testable

Every recent DECLINE bottoms out at the same ceiling: the scorer gives
**flat, class-uniform IDF credit** — every candidate sharing a
`(rule_id, cmdr_event, cand_event, filter_group)` key gets the identical
weight, so the model cannot rank *within* a class. See
[death-outlet-feeder-null-result-2026-07-07.md](death-outlet-feeder-null-result-2026-07-07.md)
(flat credit over ~2,000 sac-outlets) and the heterogeneity finding in
[spell-density-calibration-null-result-2026-07-08.md](spell-density-calibration-null-result-2026-07-08.md)
(a scalar multiplier can't "help Kess, demote Melek").

The key observation that turned this from a vague aspiration into a crisp
kill-test: **the score already HAS a per-candidate discriminator slot.** The
in-score `rank_bonus` micro-term (`0.005*(1-edhrec_rank/30000)`,
`universal_scorer.rank_bonus_for_rank`) PLUS the `edhrec_rank` component of
the production sort key `(-total_score, cmc, edhrec_rank, name)` together
break the flat-credit ties per-candidate — and they are powered by EDHREC.
On golden-100 they are worth **+0.0448 NDCG@30** (production 0.2364 vs
0.1916 with both ablated; matches the standing `--forensics` rank_bonus
sidecar of −0.0453).

So "per-candidate discrimination" has a precise operational question:

> Can a purely-MECHANICAL per-candidate signal, placed in that same slot,
> recover (or beat) the +0.0448 that EDHREC currently provides there?

If yes → per-candidate discrimination **and** removal of the
EDHREC-at-inference dependency in one move. If no → the plateau is defended
at the per-candidate level, not just per-class.

## Method (zero scoring-path mutation)

Non-mutating in-process probe over golden-100:

1. **PRODUCTION bar** — `engine.page()` as-is → 0.2364.
2. **MECHANICAL floor** — monkeypatch `rank_bonus_for_rank → 0.0`, re-sort by
   `(-mech_score, cmc, name)` (EDHREC-clean tiebreak) → 0.1916.
3. **TEST** — `mech_score + μ · norm_signal(candidate)`, same clean tiebreak,
   swept over μ ∈ {0.01, 0.1, 0.5, 1, 3, 10}. Each signal per-commander
   min-max normalized to [0,1] (higher = better).
4. **Overfit check** — select μ\* = argmax on a 50-commander TRAIN split
   (even index of sorted names), read NDCG at that fixed μ\* on the held-out
   50-commander TEST split.
5. **Noise band** — 500-resample bootstrap (seed 17) → 95% half-width.

Signals:

- `edhrec_oracle` = −edhrec_rank (EDHREC; **harness sanity — MUST recover credit**)
- `port_count` = #distinct `card_ports` rows (pure mechanical "build-around power")
- `emb_own` = cosine to commander-own-vector embedding target (100% mechanical)
- `emb_hisyn` = cosine to the hi-syn-augmented target (`build_commander_target_vector`
  with `edhrec_conn` — EDHREC used OFFLINE at target-build only, per the
  existing embedding design; EDHREC-clean at inference)

## Result — nothing mechanical works; the embedding "win" was in-sample noise

In-sample μ-sweep (whole-100 NDCG@30; floor 0.1916, production 0.2364):

| signal | μ=0.01 | 0.1 | 0.5 | 1.0 | 3.0 | 10.0 |
|---|---:|---:|---:|---:|---:|---:|
| edhrec_oracle | 0.2290 | 0.2774 | 0.3050 | 0.3165 | 0.3440 | **0.3608** |
| port_count | 0.1850 | 0.1684 | 0.1327 | 0.0905 | 0.0424 | 0.0325 |
| emb_own | 0.1992 | 0.1964 | 0.1800 | 0.1519 | 0.1169 | 0.1014 |
| emb_hisyn | 0.2144 | 0.2291 | 0.2494 | **0.2506** | 0.2345 | 0.2121 |

Held-out overfit check (μ\* selected on train, read on TEST; test production
0.2643, test floor 0.2080):

| signal | μ\* | TEST@μ\* | **TEST − production** |
|---|---:|---:|---:|
| edhrec_oracle | 10.0 | 0.4006 | **+0.1364** (EDHREC, generalizes) |
| port_count | 0.01 | 0.2003 | −0.0640 (dead) |
| emb_own | 0.01 | 0.2187 | −0.0456 (dead) |
| emb_hisyn | 1.0 | 0.2618 | **−0.0025** (ties, does NOT beat) |

Bootstrap 95% half-widths: floor ±0.0292, production ±0.0312,
emb_hisyn@1.0 ±0.0294.

**Readings:**

- **`port_count` is an ANTI-signal** — monotonically worse than floor at every
  μ (0.19 → 0.03). High-port-count cards are complex build-arounds, not the
  efficient staples EDHREC ranks. "Mechanical richness" is negatively
  correlated with EDHREC relevance.
- **`emb_own` (100% mechanical) is noise** — a +0.0076 blip at μ=0.01 that
  reverses immediately and lands −0.0456 held-out. No scalable signal.
- **`edhrec_oracle` recovers and blows past the credit** (→0.3608 in-sample,
  +0.1364 held-out) — proving the slot has enormous headroom **only when the
  signal correlates with the label**, which no mechanical signal does. This is
  the harness self-check: the slot is not inert, it is starved of mechanical
  signal.
- **`emb_hisyn`'s apparent +0.0142 in-sample win was peak-picked noise.** It sat
  inside the ±0.029 band the whole time, and held-out (μ\* chosen on train) it
  lands at **−0.0025** — a hair *below* production. It does NOT generalize.
  This is exactly the artifact held-out validation exists to catch. Note this
  is a DIFFERENT embedding formulation than the additive
  `w_emb · exp(-k · N_rules) · cosine` term measured null in
  [infrastructure-without-scoring-activation-2026-04-24.md](infrastructure-without-scoring-activation-2026-04-24.md)
  — a flat per-candidate micro-term, still null out-of-sample.

## Verdict and implications

**Per-candidate discrimination against the EDHREC-NDCG objective is dead** —
mechanical and design-time-embedding alike. No per-candidate signal beats
production out-of-sample. The plateau is defended at BOTH the per-class and
the per-candidate level.

**rank_bonus keep/remove decision (CLAUDE.md standing item):** this is direct
evidence to **KEEP** rank_bonus if the objective is EDHREC-NDCG — the +0.0448
credit it carries in the per-candidate slot cannot be replaced by any
mechanical or design-time-embedding signal. Removing it forfeits 0.0448 with
no mechanical substitute available. (A purity-motivated removal remains a
separate value judgment; this only settles that there is no mechanical
replacement.)

## What this does NOT rule out

- **A genuinely new embedding architecture** — a vectorizer that encodes
  features `emb_own` currently misses (the existing 128-dim TF-IDF+SVD over
  ports/keywords carries no within-class ordering signal). This is one of the
  named re-sweep preconditions in
  [infrastructure-without-scoring-activation-2026-04-24.md](infrastructure-without-scoring-activation-2026-04-24.md);
  the present probe does not meet it (same vectorizer, same target).
- **A different objective than EDHREC-NDCG.** This measures against
  `edhrec_labels_for_commander`; EDHREC is a proxy, not the goal (the "find
  hidden gems from mechanics" intent operationalized by `hidden_gem_hit_rate`
  — see the CLAUDE.md Evaluation section and `bench/hidden_gems.py`). A
  hidden-gem objective might value a per-candidate mechanical signal that
  EDHREC-NDCG punishes.
- **New card mechanics** (a cardsfolder import) — refills the gap_report queue
  with per-class rules; orthogonal to per-candidate ordering.

## Reproduce

Scratch probe (non-mutating, ~2 min): monkeypatch `rank_bonus_for_rank → 0`,
`engine.page(cmdr, limit=10**7)` for the mechanical scores, add
`μ · minmax(signal)`, re-sort `(-score, cmc, name)`, NDCG@30 vs
`edhrec_labels_for_commander(grade_floor=0.0)`. Train/test split = even/odd of
sorted commander names; bootstrap band seed 17. Signals from `card_ports`
(count), `card_embeddings` + `build_commander_target_vector` (cosine). No
persisted-tensor dependency, so no re-pin — see
[tensor-single-owner-slot-2026-07-08.md](tensor-single-owner-slot-2026-07-08.md)
for why avoiding the tensor here sidesteps the single-owner-slot cost.
