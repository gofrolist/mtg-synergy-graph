---
last_updated: 2026-04-30
module: bench/optimize
title: Profile-driven optimizer perf — measure before chasing reviewer estimates
tags: [perf, profiling, cprofile, lru-cache, optimizer, benchmarking]
problem_type: best_practice
symptoms:
  - perf-review estimate "20-35% speedup" turned out to be 1.4% for one finding and 7% for another
  - architectural splits ("hoist this loop") looked correct but didn't move the needle
  - intuition about which functions are hot turned out to be wrong by an order of magnitude
resolution_type: pattern
applies_when:
  - A perf reviewer flagged multiple optimizations with stacked estimated gains.
  - The hot path runs millions of iterations across O(thousands) of grid cells.
  - Wall-clock per run is long enough (minutes) that benchmarking is feasible.
created: 2026-04-30
---

# Profile-driven optimizer perf

When a code reviewer estimates speedups, those estimates are educated guesses. Multi-million-call hot paths violate the reviewer's mental model regularly, because the dominant cost is rarely where the reviewer expects. Run cProfile FIRST, then target the actual hot lines.

## Methodology

Two benchmarks against the pinned 100-commander golden set, each running one full sweep (53 rules × 5 grid cells × 100 commanders ≈ 26,500 grid evaluations × ~4,000 candidates each ≈ 43.85M scoring calls):

```bash
{ time uv run scripts/bench.py audit --optimize \
    --no-self-test --max-sweeps 1 \
    --proposal-path /tmp/proposal.json \
    --optimize-history /tmp/history.csv ; } 2>&1 | tee /tmp/run.log
```

For profiling:

```bash
uv run python -m cProfile -o /tmp/profile.prof \
    scripts/bench.py audit --optimize --no-self-test --max-sweeps 1 ...

uv run python -c "
import pstats
pstats.Stats('/tmp/profile.prof').sort_stats('tottime').print_stats(30)
"
```

cProfile adds ~55% overhead (190s unprofiled → 298s profiled), but relative ordering of hot functions is preserved.

## Findings

### Reviewer estimate vs measured (per the post-#27 ce-code-review)

| Finding | Reviewer estimate | Measured | Verdict |
|---|---:|---:|---|
| #7 IDF basis cache | 20-35% | **1.4%** | Reviewer overestimated by 14-25× |
| #PERF-01 `_compute_pair_bonus` lru_cache | 7% | **6.6-7.9%** | Reviewer was right |
| #9 contributions skip in inner grid | (deferred — bias risk) | — | — |

### Top 30 hot functions by `tottime` (post-#7, before pair-bonus cache)

```
   ncalls  tottime  percall  cumtime  filename:lineno(function)
    10800   73.023    0.007   96.613  universal_scorer.py:732(_score_from_complements)
 43849786   35.258    0.000   70.656  bench/optimize.py:327(_fast_total)
    10800   32.155    0.003  273.303  bench/optimize.py:403(score_commander_from_complements)
    10800   29.544    0.003   43.748  bench/optimize.py:368(_build_contributions)
343522971   27.139    0.000   27.139  {method 'get' of 'dict' objects}
 43849786   21.680    0.000   23.829  universal_scorer.py:212(_compute_pair_bonus)  <-- cached
 43849786   11.145    0.000   18.372  bench/optimize.py:472(<lambda>)              <-- sort key
    10812    9.087    0.001   27.459  {method 'sort' of 'list' objects}
148420183    8.705    0.000    8.705  {method 'add' of 'set' objects}
143348879    7.458    0.000    7.458  {method 'append' of 'list' objects}
```

Total run: 297.9 seconds (1.1B function calls).

The architectural picture: 43.85M = once per (commander, grid cell, candidate). 343M = ~3-4 dict.get calls per scoring call, mostly in `_fast_total`'s idf-weight lookup. The reviewer's mental model that frequency-counting was a major cost was wrong — the dominant work is the `_fast_total` inner loop and `_score_from_complements` body, not `_compute_idf_weights`.

## Outcomes

| Run | Wall-clock | User CPU | Speedup vs prior |
|---|---:|---:|---:|
| Pre-#7 (`20cd129` + JSON-fix patch) | 3:12.93 | 189.03s | baseline |
| Post-#7 IDF basis cache | 3:10.21 | 186.43s | 1.4% |
| Post pair-bonus cache | **2:57.66** | **174.96s** | 6.6% |
| (cumulative since pre-#7) | | | **7.9%** |

All three runs produced identical proposals (16 steps accepted, train Δ=+0.0083, held Δ=+0.0047) — determinism confirmed.

## Pattern

For multi-million-call hot paths, **profile then optimize, never the reverse**. The cost of one cProfile run (~5 minutes on this codebase) is far less than the cost of refactoring for an architectural fix that turns out to deliver 1% instead of 30%. Save the architectural-fix energy for cases where the profile actually points there.

The IDF-basis-cache split is still a sound architectural change (it sets up future flat-override sweeps cleanly and reduces re-walks). It just isn't the perf win it was sold as. Both can be true.

## Related

- `_compute_pair_bonus` cache: `src/mtg_synergy_graph/universal_scorer.py` (with `cache_clear()` contract documented in the docstring)
- IDF basis split: `src/mtg_synergy_graph/universal_scorer.py` (`IdfBasis`, `_compute_idf_basis`, `_idf_weights_from_basis`)
- JSON-serialization fix that unblocked end-to-end benchmarking: PR #31, commit `ac6fa70`
- Bench harness: `bench.py audit --optimize` (see `CLAUDE.md` Common Commands)
