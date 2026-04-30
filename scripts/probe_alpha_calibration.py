"""One-off alpha-calibration probe for the tensor-driven weight optimizer.

Runs the optimizer at alpha ∈ {0.3, 0.5, 0.7} against the pinned 100-commander
fixture with everything else held constant (same split seed, same sweep cap,
same eps gates). Emits a comparison table of the per-rule diffs each alpha
produces, plus the per-axis deltas (nDCG vs gem).

Decision rule from the plan:
- If diffs are stable across alpha values, alpha=0.5 default is safe to ship.
- If diffs diverge, alpha is the dominant signal; pick alpha via the metric you
  actually care about (nDCG-leaning: lower alpha; gem-leaning: higher alpha) before
  applying the optimizer's proposal in production.

This script is NOT committed long-term — it's a calibration probe scheduled
in the brainstorm doc as "deferred to separate task" for post-merge.
"""

from __future__ import annotations

import time

from mtg_synergy_graph.bench.fixture import PinnedFixture
from mtg_synergy_graph.bench.optimize import (
    OptimizerConfig,
    run_optimizer,
)
from mtg_synergy_graph.db import open_db


def main() -> None:
    fixture = PinnedFixture.load("tests/fixtures/golden_set_run.json")
    commanders = [e.commander for e in fixture.entries]
    print(f"loaded {len(commanders)} commanders from pinned fixture")

    results = {}
    for alpha in (0.3, 0.5, 0.7):
        print(f"\n=== alpha = {alpha} ===")
        config = OptimizerConfig(
            alpha=alpha,
            max_sweeps=1,
            split_seed=42,
            run_self_test=False,
        )
        conn = open_db("data/synergy.db")
        edhrec_conn = open_db("data/tags.db")
        start = time.time()
        try:
            result = run_optimizer(conn, edhrec_conn, commanders, config=config)
        finally:
            conn.close()
            edhrec_conn.close()
        elapsed = time.time() - start
        print(f"  elapsed: {elapsed:.1f}s, accepted={result.n_steps_accepted}, partial={result.partial_sweep}")

        diffs = {}
        for rid, baseline_v in result.baseline_weights.items():
            final_v = result.final_weights[rid]
            if baseline_v != final_v:
                diffs[rid] = (baseline_v, final_v)
        results[alpha] = {
            "diffs": diffs,
            "train_ndcg": (result.train_ndcg_baseline, result.train_ndcg_final),
            "held_ndcg": (result.held_ndcg_baseline, result.held_ndcg_final),
            "train_gem": (result.train_gem_baseline, result.train_gem_final),
            "held_gem": (result.held_gem_baseline, result.held_gem_final),
            "composite_train_delta": result.train_composite_final - result.train_composite_baseline,
            "composite_held_delta": result.held_composite_final - result.held_composite_baseline,
            "n_accepted": result.n_steps_accepted,
            "partial": result.partial_sweep,
        }

    # Comparison table: union of changed rules across all alpha values.
    all_changed = sorted(set().union(*(set(r["diffs"].keys()) for r in results.values())))

    print("\n\n=== alpha-calibration comparison ===")
    print()
    print(f"{'rule_id':35s}  {'alpha=0.3':>16s}  {'alpha=0.5':>16s}  {'alpha=0.7':>16s}")
    print("-" * 90)
    for rid in all_changed:
        cells = []
        for alpha in (0.3, 0.5, 0.7):
            d = results[alpha]["diffs"].get(rid)
            if d is None:
                cells.append(f"{'(no change)':>16s}")
            else:
                cells.append(f"{d[0]:.3f} → {d[1]:.3f}")
        print(f"{rid:35s}  {cells[0]:>16s}  {cells[1]:>16s}  {cells[2]:>16s}")

    print()
    print("=== Per-axis deltas ===")
    print()
    print(f"{'axis':18s}  {'alpha=0.3':>16s}  {'alpha=0.5':>16s}  {'alpha=0.7':>16s}")
    print("-" * 80)
    for axis_label, key in [
        ("composite Δ (train)", "composite_train_delta"),
        ("composite Δ (held)", "composite_held_delta"),
    ]:
        vals = [f"{results[a][key]:+.4f}" for a in (0.3, 0.5, 0.7)]
        print(f"{axis_label:18s}  {vals[0]:>16s}  {vals[1]:>16s}  {vals[2]:>16s}")

    for axis_label, key in [
        ("train nDCG", "train_ndcg"),
        ("held  nDCG", "held_ndcg"),
        ("train gem", "train_gem"),
        ("held  gem", "held_gem"),
    ]:
        vals = []
        for a in (0.3, 0.5, 0.7):
            base, final = results[a][key]
            if base is None or final is None:
                vals.append("(None)")
            else:
                vals.append(f"{base:.4f}→{final:.4f}")
        print(f"{axis_label:18s}  {vals[0]:>16s}  {vals[1]:>16s}  {vals[2]:>16s}")

    # Summary heuristic: if all alpha values produce the SAME set of rule changes,
    # and the magnitudes differ by < 25%, declare "robust." Otherwise flag.
    rule_sets = [set(results[a]["diffs"].keys()) for a in (0.3, 0.5, 0.7)]
    if rule_sets[0] == rule_sets[1] == rule_sets[2]:
        print("\n[verdict] alpha robust: all three alpha values flag the same rules.")
        # Check magnitude stability.
        max_swing = 0.0
        for rid in all_changed:
            values = [results[a]["diffs"][rid][1] for a in (0.3, 0.5, 0.7)]
            spread = max(values) - min(values)
            mean = sum(values) / 3
            if mean > 0:
                rel = spread / mean
                if rel > max_swing:
                    max_swing = rel
        print(f"[verdict] max relative swing across alpha = {max_swing * 100:.1f}%")
        if max_swing < 0.25:
            print("[verdict] STABLE: alpha=0.5 default is safe to ship; apply the proposal.")
        else:
            print("[verdict] UNSTABLE: rule set agrees but magnitudes vary >25%; pick alpha by axis priority.")
    else:
        diff_summary = []
        for a, rules in zip((0.3, 0.5, 0.7), rule_sets, strict=True):
            diff_summary.append(f"alpha={a}: {sorted(rules)}")
        print("\n[verdict] alpha DOMINANT: different alpha values flag different rules.")
        for line in diff_summary:
            print(f"  {line}")
        print("[verdict] DO NOT apply the smoke proposal blindly; alpha calibration is the dominant signal.")


if __name__ == "__main__":
    main()
