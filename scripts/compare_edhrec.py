"""Compare the deterministic engine's recommendations against EDHREC.

Reports Hi-Syn / Top / OnPage / NotEDH counts per commander, mirroring the
existing ``scripts/compare_edhrec.py`` columns so the LightGBM and
deterministic baselines can be compared apples-to-apples.

Commanders are matched **by Scryfall oracle_id**: each input commander
name is resolved against the Scryfall ``cards`` table in ``--edhrec-db``
to get its oracle_id, and the synergy engine is then queried through
``SynergyEngine.page_by_oracle_id``. This sidesteps the DFC naming
mismatch (Forge stores the front face, Scryfall stores ``A // B``) and
distinguishes two failure modes:

- ``unknown commander`` — the input name is not in the Scryfall DB at
  all (typo, card doesn't exist).
- ``not in Forge source`` — the name resolves to an oracle_id but that
  oracle_id is missing from ``synergy.db`` (the Forge cardsfolder
  checkout predates the card).

Usage:
    # Single commander
    uv run python scripts/compare_edhrec.py \\
        --commander "Korvold, Fae-Cursed King"

    # Whole Golden Set
    uv run python scripts/compare_edhrec.py \\
        --commanders tests/fixtures/golden_set.json
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


def _resolve_name_to_oracle_id(
    scryfall_conn: sqlite3.Connection,
    name: str,
) -> str | None:
    """Look up ``name`` in the Scryfall ``cards`` table and return its oracle_id.

    Tries the canonical name first, then the DFC front-face substring
    (``A`` → ``A // ...``) and finally the back-face substring
    (``B`` → ``... // B``) so callers don't have to know which face the
    Scryfall DB stores a card under.
    """
    row = scryfall_conn.execute(
        "SELECT oracle_id FROM cards WHERE name = ? LIMIT 1",
        (name,),
    ).fetchone()
    if row is not None:
        return row["oracle_id"]

    # DFC front face: caller passed "Rona, Herald of Invasion", Scryfall
    # stores "Rona, Herald of Invasion // Rona, Tolarian Obliterator".
    row = scryfall_conn.execute(
        "SELECT oracle_id FROM cards WHERE name LIKE ? LIMIT 1",
        (name + " // %",),
    ).fetchone()
    if row is not None:
        return row["oracle_id"]

    # DFC back face: caller passed the back face only.
    row = scryfall_conn.execute(
        "SELECT oracle_id FROM cards WHERE name LIKE ? LIMIT 1",
        ("% // " + name,),
    ).fetchone()
    if row is not None:
        return row["oracle_id"]

    return None


def _resolve_commander_oracle_ids(
    scryfall_conn: sqlite3.Connection,
    spec: list[str],
) -> tuple[list[str] | None, str | None]:
    """Resolve every name in ``spec`` to an oracle_id.

    Returns ``(oracle_ids, None)`` on success, or ``(None, error)`` on
    the first name that doesn't resolve.
    """
    oids: list[str] = []
    for name in spec:
        oid = _resolve_name_to_oracle_id(scryfall_conn, name)
        if oid is None:
            return None, f"unknown commander: {name!r}"
        oids.append(oid)
    return oids, None


def main() -> int:
    from mtg_synergy_graph.bench._deprecation import emit_deprecation

    emit_deprecation("scripts/compare_edhrec.py", "bench.py audit")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--edhrec-db", type=Path, default=Path("data/tags.db"))
    parser.add_argument("--commander", help="single commander name")
    parser.add_argument("--commanders", type=Path, help="JSON list of commanders (alternative to --commander)")
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

    commanders = [args.commander] if args.commander else _load_commanders(args.commanders)

    header = f"{'commander':40}  {'hi-syn':>8}  {'top':>5}  {'on-page':>8}  {'not-edh':>8}"
    print(header)
    print("-" * len(header))

    hi_pcts: list[float] = []
    on_pcts: list[float] = []
    skipped_unknown = 0
    skipped_missing_in_forge = 0

    with SynergyEngine(args.db) as engine:
        for spec in commanders:
            cmdr = _normalise(spec)
            label = " + ".join(cmdr)[:40]

            oids, err = _resolve_commander_oracle_ids(edhrec_conn, cmdr)
            if err is not None:
                print(f"{label:40}  skipped ({err})")
                skipped_unknown += 1
                continue

            try:
                page = engine.page_by_oracle_id(
                    oids,
                    offset=0,
                    limit=args.top,
                )
            except LookupError as exc:
                print(f"{label:40}  skipped (not in Forge source: {exc})")
                skipped_missing_in_forge += 1
                continue
            except ValueError as exc:
                print(f"{label:40}  skipped ({exc})")
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
    if skipped_unknown or skipped_missing_in_forge:
        print(f"skipped: {skipped_unknown} unknown commander(s), {skipped_missing_in_forge} not in Forge source")

    edhrec_conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
