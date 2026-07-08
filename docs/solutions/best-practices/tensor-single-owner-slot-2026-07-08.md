---
last_updated: 2026-07-08
module: bench
title: The persisted tensor is single-owner per config_hash — forensics and cohort fixtures compete for one slot
tags:
  - tensor
  - rule-contributions
  - forensics
  - repin
  - config-hash
  - staple-only
  - eval-infra
problem_type: best_practice
resolution_type: guideline
applies_when:
  - A `bench.py audit --forensics` run shows most golden-100 commanders with "tensor cands 0" and OUTRANKED misses tagged `staple_only`
  - You want to de-blind forensics OR run a `--rule` / `--inspect` / `--collinearity` / cohort `--per-commander-ndcg` read and the tensor "has no rows at the current config_hash"
  - Planning to "expand the eval set" to de-blind OUTRANKED — read this first; de-blinding is a re-pin, not a commander-count increase
created: 2026-07-08
plan_ref: none (finding surfaced during eval-improvement investigation 2026-07-08)
---

# The persisted tensor holds ONE fixture's commanders per config_hash

## The constraint

`rule_contributions` (the persisted contribution tensor in
`data/synergy.db`) is tagged per row with `config_hash` but **not** with a
fixture id. `bench.py audit --repin` (`bench/handlers.py:128`) does:

```python
conn.execute("DELETE FROM rule_contributions WHERE config_hash = ?", (live_hash,))
# ...then repopulates ONLY the fixture's commanders via TensorWriter
```

So every re-pin **evicts** whatever commanders currently occupy the live
config_hash and replaces them with the re-pinned fixture's commanders. All
committed fixtures (`golden_set_run.json` 100, `golden_set_run_500.json`
500, `golden_set_archetype_payoff.json` 33, `golden_set_outlet_payoff.json`
126) share the **same** live `config_hash` (they pin the same scoring
config), so they all compete for **one** tensor slot. **The last fixture
re-pinned owns the tensor; every other fixture is tensor-blind.**

## The symptom this produces

Any tensor-derived read of a commander NOT in the currently-pinned fixture
returns zero rows. In `--forensics` this makes `classify_miss`
(`bench/forensics.py`) tag every OUTRANKED miss `staple_only` (because
`has_tensor_rows` is `False`), and the "divergent (justified)" gate collapse
to ~0 justified — **a pinning artifact, not zero rule credit.** The
forensics module docstring already carries this caveat; this note is the
mechanism behind it.

Worked example (2026-07-08): the tensor was left pinned on the 33-commander
archetype-payoff cohort after the subtype-supply cycle. A `--forensics` run
over golden-100 then showed **97 / 100 commanders with `tensor cands 0`**
and only the 3 in `archetype_cohort ∩ golden_100` (Omnath Locus of Rage,
Slimefoot, Wilhelt) with real rows. `staple_only` read **1,163**; justified
divergence read **2.0%**. After re-pinning golden-100, `staple_only`
dropped to **15** and justified divergence rose to **85.7%** — the true
picture (the model's EDHREC-divergent picks are overwhelmingly
mechanically grounded). Nothing about the *scoring* changed; only which
commanders the tensor covered.

## De-blinding procedure (and its cost)

To make forensics / `--rule` / `--collinearity` diagnosable for a fixture's
commanders, re-pin the tensor on **that** fixture:

```bash
uv run scripts/bench.py audit --repin --yes --fixture tests/fixtures/golden_set_run.json
```

- **Cost:** ~1–2 min wall-clock; ~4,900 tensor rows/commander (100 → ~0.5M
  rows, 500 → ~2.5M). Score-identical when the scoring config is unchanged
  (config_hash stays the same) — only the fixture JSON's `created_at`
  changes, which is churn worth reverting with `git checkout`.
- **Cost you must accept:** it EVICTS whatever fixture currently owns the
  slot. If you de-blind golden-100 you re-blind the archetype/outlet cohort
  `--per-commander-ndcg` reads, and vice-versa. Restore with the other
  fixture's `--repin` when done. (`--repin` preserves the cohort's
  `cohort_members` snapshot; after a cardsfolder refresh use the fixture's
  bootstrap script instead — see CLAUDE.md.)

**Note:** `golden_set_run.json` (100) is NOT a subset of
`golden_set_run_500.json` (only ~61 overlap; the 100 is hand-curated
archetype-diverse, the 500 is top-by-EDHREC-rank). Re-pinning the 500 only
de-blinds ~61 of the 100 forensics commanders — re-pin the **100**
specifically to de-blind the forensics fixture completely.

## "Expand the eval set" ≠ de-blinding, and ≠ refilling gap_report

Two goals often conflated under "expand the eval set" decouple:

- **De-blinding forensics OUTRANKED** = a tensor **re-pin** (above), NOT a
  commander-count increase. The commanders already exist in the fixture;
  they're just absent from the tensor because another fixture owns the slot.
- **Refilling the gap_report queue** = neither. `scripts/gap_report.py`
  scans the **full card DB** (`WHERE legal_commander = 1`), not any golden
  fixture. New queue items come from new *card mechanics* (a cardsfolder
  import), not more eval commanders. Growing the golden set does nothing for
  the gap queue.

The eval fixture's real leverage is **validation power** (can a proposed
cohort rule's gain be measured undiluted) — which is what the cohort
fixtures (plan 2026-07-03-001) address, not raw commander count.

## The durable fix (DONE 2026-07-08)

`--repin` is now **additive**. The blanket `DELETE ... WHERE config_hash`
was replaced by `bench.tensor.evict_fixture_rows(conn, config_hash,
commanders)` (called from `bench/handlers.py`), which scopes eviction to
`(config_hash, commander IN <fixture commanders>)`. Forensics-broad
(golden-100/500) and cohort-narrow (archetype/outlet) tensors now COEXIST
at the same config_hash — de-blinding one fixture no longer re-blinds the
others, so the re-pin tug-of-war is gone. The PK `(commander, candidate,
rule_id, config_hash)` set-dedups a commander shared by two fixtures, and
clearing the whole re-pinned commander before repopulation still drops
orphan rows from a rule that stopped firing.

**Behavior change.** A `WHERE config_hash = ?` read with no commander
filter now spans the **union** of pinned fixtures rather than the
last-pinned one. The union is monotonic and PK-deduped (no
double-counting), but its membership is the set of fixtures ever
pinned-and-not-evicted in *this* DB — i.e. **pin-history-dependent, and
NOT reproducible across machines/checkouts** (a single deterministic
`--repin` reconstructs the old single-owner state; nothing reconstructs
an arbitrary accumulated union). It is therefore wrong to treat a raw
config_hash-only aggregate as "one fixture."

Two consequences, handled differently:

- **demand-coverage pool sizes are fixture-scoped (a correctness fix,
  not just cosmetics).** `_rule_pool_sizes` feeds a comparison against
  the fixed `BURIAL_POOL_FLOOR = 500`, so a union-inflated count could
  push a rule across the threshold and misclassify it as IDF-burial. It
  now receives the run's `fixture_commanders` and filters to them via
  `bench.tensor.commander_filter_sql`, keeping the count genuinely
  fixture-wide.
- **`--rule`, `--collinearity`, `--embedding-dedup` still span the
  union** — a documented diagnostic caveat, not a bug. Their numbers are
  monotonic in population but pin-history-dependent; for a fixture-clean
  read, run them against a DB with only that fixture pinned. (These are
  design-time diagnostics, not gates; `--collinearity`'s VIF/Pearson are
  additionally non-monotonic in population, so a mixed pin especially
  warrants a single-fixture DB.)

Per-commander readers (forensics main path, cohort
`--per-commander-ndcg`) were always commander-scoped and are unaffected.

**Known residual — orphan rows on fixture shrink.** Because eviction is
scoped to the *current* fixture's commanders, a commander REMOVED from a
fixture (a cohort re-derivation dropping members, a data refresh dropping
commander-illegal cards — neither flips config_hash) is never re-targeted
by a later `--repin` and its rows persist at the live config_hash. The
old blanket `DELETE ... WHERE config_hash` self-healed this. The rows are
now harmless to the fixture-scoped aggregate readers above (they are not
in the fixture), but they do accumulate on disk until the next
config_hash flip orphans them under the old hash. `--repin` now reports
`tensor rows evicted: N` so the count is observable. A true fix (a
fixture-id/owner column, or a periodic GC) is left as follow-up.

Historical note: before this fix you had to treat the tensor slot as a
single-owner resource and re-pin deliberately, restoring the prior owner
when finished. That is no longer necessary.
