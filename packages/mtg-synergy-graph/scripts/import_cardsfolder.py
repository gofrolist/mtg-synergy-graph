"""Import a Forge cardsfolder into a synergy.db.

Usage:
    uv run python packages/mtg-synergy-graph/scripts/import_cardsfolder.py \
        --folder      data/forge/forge-gui/res/cardsfolder \
        --db          /tmp/synergy.db \
        --scryfall-db data/tags.db
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True, type=Path,
                        help="Path to a Forge cardsfolder/ tree")
    parser.add_argument("--db", required=True, type=Path,
                        help="Output synergy.db path")
    parser.add_argument("--scryfall-db", required=True, type=Path,
                        help="Scryfall-shaped sqlite DB (e.g. data/tags.db) "
                             "used to populate cards.oracle_id. Required: "
                             "hard-fail if missing to prevent silent "
                             "regression into name-only matching.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N .txt files (smoke test)")
    args = parser.parse_args()

    if not args.folder.exists():
        print(f"error: {args.folder} does not exist", file=sys.stderr)
        return 2
    if not args.scryfall_db.exists():
        print(
            f"error: --scryfall-db does not exist: {args.scryfall_db}",
            file=sys.stderr,
        )
        return 2

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        args.db.unlink()

    print(f"opening {args.db}")
    conn = open_db(args.db)

    t0 = time.perf_counter()
    cards, ports = import_cards_folder(
        conn, args.folder,
        scryfall_db=args.scryfall_db,
        limit=args.limit,
    )
    elapsed = time.perf_counter() - t0

    cur = conn.execute("SELECT COUNT(*) FROM port_attributes")
    attrs = cur.fetchone()[0]

    resolved = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE oracle_id IS NOT NULL"
    ).fetchone()[0]

    rate = cards / elapsed if elapsed else 0
    print(f"imported {cards} cards / {ports} ports / {attrs} attributes "
          f"in {elapsed:.1f}s ({rate:.0f} cards/s)")
    if cards:
        pct = 100.0 * resolved / cards
        print(f"oracle_id coverage: {resolved}/{cards} ({pct:.1f}%)")
    else:
        print("oracle_id coverage: 0/0")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
