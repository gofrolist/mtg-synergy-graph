"""Run the deterministic synergy engine for one commander.

Usage:
    uv run python scripts/recommend.py --commander "Korvold, Fae-Cursed King"
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from mtg_synergy_graph import SynergyEngine


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--commander", required=True)
    parser.add_argument("--partner", default=None, help="Optional second commander (partner pair)")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--explain", action="store_true", help="Render plain-English explanations")
    args = parser.parse_args()

    cmdr: str | list[str]
    cmdr = [args.commander, args.partner] if args.partner else args.commander

    with SynergyEngine(args.db) as engine:
        meta = engine.metadata()
        print(f"engine: spec={meta['spec_version']} db={meta['db_path']}")

        t0 = time.perf_counter()
        page = engine.page(
            cmdr,
            offset=args.offset,
            limit=args.top,
            include_explanations=args.explain,
        )
        elapsed = time.perf_counter() - t0

        print(f"commander: {' + '.join(page.commander)}")
        print(f"colour identity: {page.color_identity}")
        print(f"total candidates: {page.total}  (window {page.offset}-{page.offset + len(page.items)} of {page.total})")
        print(f"computed in {elapsed:.2f}s")
        print()

        header = (
            f"{'rank':>4}  {'card':32}  {'total':>7}  "
            f"{'port':>5} {'cost':>5} {'lord':>5} {'amp':>5} "
            f"{'graph':>6} {'strat':>5} {'res':>5} {'stap':>5}"
        )
        print(header)
        print("-" * len(header))
        for rec in page.items:
            print(
                f"{rec.rank:>4}  {rec.card[:32]:32}  {rec.total_score:>7.1f}  "
                f"{rec.scores.get('port_match', 0):>5.0f} "
                f"{rec.scores.get('cost_synergy', 0):>5.0f} "
                f"{rec.scores.get('lord', 0):>5.0f} "
                f"{rec.scores.get('amplifier', 0):>5.0f} "
                f"{rec.scores.get('graph_metrics', 0):>6.1f} "
                f"{rec.scores.get('strategic', 0):>5.0f} "
                f"{rec.scores.get('resource_density', 0):>5.0f} "
                f"{rec.scores.get('staple', 0):>5.0f}"
            )
            if args.explain and rec.explanation:
                for line in rec.explanation:
                    print(f"        - {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
