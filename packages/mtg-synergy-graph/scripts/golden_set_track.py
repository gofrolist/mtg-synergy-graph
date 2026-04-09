"""Golden Set regression tracker (SPEC §10.4).

Two modes:

* ``--bootstrap`` — run the engine on every commander listed in
  ``--commanders`` (or ``--input-baseline``) and write the current top-10 +
  NDCG@30 to ``--baseline``. Use this to refresh the baseline after an
  intentional scoring change.
* ``--check`` (default) — re-run the engine and compare against the
  committed baseline. Exits non-zero on regression so CI can gate merges.

Usage:
    uv run python packages/mtg-synergy-graph/scripts/golden_set_track.py \\
        --db /tmp/synergy_full.db \\
        --edhrec-db data/tags.db \\
        --commanders packages/mtg-synergy-graph/tests/fixtures/golden_set.json \\
        --baseline   packages/mtg-synergy-graph/tests/fixtures/golden_set_run.json \\
        --bootstrap
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from mtg_synergy_graph import (
    SynergyEngine,
    bootstrap_golden_set,
    check_golden_set,
    regression_failed,
)


def _load_commanders(path: Path) -> list[object]:
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "commanders" in raw:
        return list(raw["commanders"])
    if isinstance(raw, dict) and "entries" in raw:
        return [e["commander"] for e in raw["entries"]]
    raise ValueError(f"unrecognised commanders file: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--edhrec-db", type=Path, default=None,
                        help="data/tags.db (for NDCG@30 — optional)")
    parser.add_argument("--commanders", type=Path,
                        help="JSON list of commanders (string or [a, b] pair)")
    parser.add_argument("--baseline", required=True, type=Path,
                        help="Output / input baseline JSON")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Write current run as baseline (overwrites)")
    parser.add_argument("--ndcg-tolerance", type=float, default=0.005)
    parser.add_argument("--jitter", type=int, default=5)
    parser.add_argument("--graph-metrics", action="store_true",
                        help="Enable SPEC §6.8 causal graph metrics "
                             "(graph_neighbor_overlap, graph_pagerank). "
                             "Cold start is ~30s slower but subsequent "
                             "pages reuse the adjacency cache.")
    args = parser.parse_args()

    edhrec_conn: sqlite3.Connection | None = None
    if args.edhrec_db:
        if not args.edhrec_db.exists():
            print(f"warning: {args.edhrec_db} not found — running without NDCG", file=sys.stderr)
        else:
            edhrec_conn = sqlite3.connect(args.edhrec_db)
            edhrec_conn.row_factory = sqlite3.Row

    with SynergyEngine(args.db, graph_metrics=args.graph_metrics) as engine:
        if args.bootstrap:
            if not args.commanders:
                print("error: --bootstrap requires --commanders", file=sys.stderr)
                return 2
            commanders = _load_commanders(args.commanders)
            print(f"bootstrap: {len(commanders)} commanders → {args.baseline}")
            report = bootstrap_golden_set(
                engine, commanders, args.baseline, edhrec_conn=edhrec_conn,
            )
            print(f"  entries:  {len(report.entries)}")
            print(f"  agg NDCG: {report.aggregate_ndcg}")
            return 0

        if not args.baseline.exists():
            print(f"error: baseline {args.baseline} does not exist (use --bootstrap)",
                  file=sys.stderr)
            return 2

        report = check_golden_set(
            engine,
            args.baseline,
            edhrec_conn=edhrec_conn,
            jitter=args.jitter,
            ndcg_tolerance=args.ndcg_tolerance,
        )
        print(f"check: {len(report.entries)} commanders")
        print(f"  fresh agg NDCG:    {report.aggregate_ndcg}")
        print(f"  baseline agg NDCG: {report.baseline_ndcg}")
        if report.rank_shifts:
            print(f"  rank shifts:    {len(report.rank_shifts)}")
            for r in report.rank_shifts[:10]:
                print(f"    {r['commander']}: +{r['added'][:3]}  -{r['removed'][:3]}")
        if report.ndcg_drops:
            print(f"  NDCG drops:     {len(report.ndcg_drops)}")
            for d in report.ndcg_drops[:10]:
                print(f"    {d['commander']}: {d['baseline']:.4f} → {d['fresh']:.4f}")
        if report.drift:
            print(f"  drift errors:   {len(report.drift)}")

        return 1 if regression_failed(report, ndcg_tolerance=args.ndcg_tolerance) else 0


if __name__ == "__main__":
    raise SystemExit(main())
