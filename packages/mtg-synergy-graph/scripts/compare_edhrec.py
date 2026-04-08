"""Compare the deterministic engine's recommendations against EDHREC.

Reports Hi-Syn / Top / OnPage / NotEDH counts per commander, mirroring the
existing ``scripts/compare_edhrec.py`` columns so the LightGBM and
deterministic baselines can be compared apples-to-apples.

Usage:
    # Single commander
    uv run python packages/mtg-synergy-graph/scripts/compare_edhrec.py \\
        --db /tmp/synergy_full.db \\
        --edhrec-db data/tags.db \\
        --commander "Korvold, Fae-Cursed King" \\
        --top 50

    # Whole Golden Set
    uv run python packages/mtg-synergy-graph/scripts/compare_edhrec.py \\
        --db /tmp/synergy_full.db \\
        --edhrec-db data/tags.db \\
        --commanders packages/mtg-synergy-graph/tests/fixtures/golden_set.json \\
        --top 50
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean

from mtg_synergy_graph import SynergyEngine, compare_to_edhrec


def _load_commanders(path: Path) -> list[object]:
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "commanders" in raw:
        return list(raw["commanders"])
    if isinstance(raw, dict) and "entries" in raw:
        return [e["commander"] for e in raw["entries"]]
    raise ValueError(f"unrecognised commanders file: {path}")


def _normalise(spec: object) -> list[str]:
    if isinstance(spec, str):
        return [spec]
    return list(spec)  # type: ignore[arg-type]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--edhrec-db", required=True, type=Path)
    parser.add_argument("--commander", help="single commander name")
    parser.add_argument("--commanders", type=Path,
                        help="JSON list of commanders (alternative to --commander)")
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    if not args.commander and not args.commanders:
        print("error: pass --commander or --commanders", file=sys.stderr)
        return 2
    if not args.edhrec_db.exists():
        print(f"error: {args.edhrec_db} does not exist", file=sys.stderr)
        return 2

    edhrec_conn = sqlite3.connect(args.edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row

    commanders = (
        [args.commander] if args.commander else _load_commanders(args.commanders)
    )

    header = (
        f"{'commander':40}  {'hi-syn':>8}  {'top':>5}  "
        f"{'on-page':>8}  {'not-edh':>8}"
    )
    print(header)
    print("-" * len(header))

    hi_pcts: list[float] = []
    on_pcts: list[float] = []

    with SynergyEngine(args.db) as engine:
        for spec in commanders:
            cmdr = _normalise(spec)
            try:
                page = engine.page(cmdr, offset=0, limit=args.top)
            except ValueError as exc:
                print(f"{' + '.join(cmdr)[:40]:40}  skipped ({exc})")
                continue
            cmp = compare_to_edhrec(page, edhrec_conn, top_n=args.top)
            print(
                f"{cmp.commander[:40]:40}  "
                f"{cmp.hi_syn_hits:>3}/{max(cmp.hi_syn_size, 1):>3}  "
                f"{cmp.top_hits:>5}  "
                f"{cmp.on_page_hits:>4}/{args.top:<3}  "
                f"{cmp.not_edh:>4}/{args.top:<3}"
            )
            if cmp.hi_syn_size:
                hi_pcts.append(cmp.hi_syn_hits / cmp.hi_syn_size)
            on_pcts.append(cmp.on_page_hits / args.top)

    if hi_pcts:
        print()
        print(f"avg Hi-Syn  : {mean(hi_pcts) * 100:.1f}%")
    if on_pcts:
        print(f"avg OnPage  : {mean(on_pcts) * 100:.1f}%")

    edhrec_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
