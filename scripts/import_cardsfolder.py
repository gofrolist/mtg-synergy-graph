"""Import a Forge cardsfolder into a synergy.db.

Usage:
    uv run python scripts/import_cardsfolder.py
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder


def main() -> int:
    # Surface the importer's INFO-level coverage stats (oracle_id /
    # edhrec_rank / color_identity); the default last-resort handler
    # only shows WARNING and above.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--folder",
        type=Path,
        default=Path("data/forge/forge-gui/res/cardsfolder"),
        help="Path to a Forge cardsfolder/ tree",
    )
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"), help="Output synergy.db path")
    parser.add_argument(
        "--scryfall-db",
        type=Path,
        default=Path("data/tags.db"),
        help="Scryfall-shaped sqlite DB (e.g. data/tags.db) used to populate cards.oracle_id.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Stop after N .txt files (smoke test)")
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
        conn,
        args.folder,
        scryfall_db=args.scryfall_db,
        limit=args.limit,
    )
    elapsed = time.perf_counter() - t0

    cur = conn.execute("SELECT COUNT(*) FROM port_attributes")
    attrs = cur.fetchone()[0]

    resolved = conn.execute("SELECT COUNT(*) FROM cards WHERE oracle_id IS NOT NULL").fetchone()[0]
    ranked = conn.execute("SELECT COUNT(*) FROM cards WHERE edhrec_rank IS NOT NULL").fetchone()[0]

    rate = cards / elapsed if elapsed else 0
    print(f"imported {cards} cards / {ports} ports / {attrs} attributes in {elapsed:.1f}s ({rate:.0f} cards/s)")
    if cards:
        oid_pct = 100.0 * resolved / cards
        rank_pct = 100.0 * ranked / cards
        print(f"oracle_id   coverage: {resolved}/{cards} ({oid_pct:.1f}%)")
        print(f"edhrec_rank coverage: {ranked}/{cards} ({rank_pct:.1f}%)")
    else:
        print("oracle_id coverage: 0/0")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
