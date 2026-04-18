"""Enumerate the Forge DSL port universe present in synergy.db.

Produces a structured catalog of every distinct port shape in the
``card_ports`` table — the finite vocabulary the engine has to write
rules against. The catalog is the foundation for the schema-driven
rule generator: every (port_type, event_class) cell with non-trivial
commander coverage is a candidate for a rule template.

Outputs:

- ``docs/port_universe.json`` — machine-readable catalog for downstream
  tooling (coverage matrix, rule-generator scaffolding).
- stdout — human-readable top-N summary per port_type.

Per (port_type, event_class) cell we report:
- ``rows``: total port rows in the database.
- ``cards``: distinct cards carrying this port shape.
- ``commanders``: distinct legendary-creature legal commanders (LHS-
  relevant — only their ports drive rule activation).
- ``filter_qualifiers``: top valid_filter qualifier tokens with
  frequencies. Qualifiers are tokens after the main type token,
  delimited by ``.`` or ``+`` (e.g. ``YouCtrl``, ``modified``,
  ``attacking``, ``counters_GE1_P1P1``). The ``!`` negation prefix is
  stripped before counting so ``nonHuman`` and ``Human`` collapse to
  the same axis but ``!attacking`` collapses with ``attacking``.
- ``raw_clause_keys``: top dict keys appearing in raw_line (e.g.
  ``ValidSource``, ``CombatDamage``, ``Affected``, ``ChangeType``,
  ``IsPresent``, ``TargetsValid``). These are the parameter slots the
  rule generator must understand.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

#: Tokens that appear after the main type token in valid_filter, e.g.
#: ``Creature.modified+YouCtrl`` → main=``Creature`` qualifiers=
#: ``modified``, ``YouCtrl``. Multi-OR alts are split on ``,``; we
#: include qualifiers from every alt to capture the full vocabulary.
_QUALIFIER_DELIMS = re.compile(r"[.+]")

#: Extracts dict-style clause keys from raw_line, e.g. ``'Mode': 'X'``
#: → key ``Mode``. Forge writes raw_line as a Python dict-repr string.
_RAW_KEY_RE = re.compile(r"'([A-Za-z_][A-Za-z0-9_]*)':")


def _extract_qualifiers(valid_filter: str) -> list[str]:
    """Return qualifier tokens from a valid_filter expression.

    Strips the main type token (first segment of the first OR-alt) and
    the leading ``!`` negation prefix from each remaining token. Empty
    tokens are dropped.
    """
    if not valid_filter:
        return []
    qualifiers: list[str] = []
    for alt in valid_filter.split(","):
        alt = alt.strip()
        if not alt:
            continue
        tokens = _QUALIFIER_DELIMS.split(alt)
        # tokens[0] is the main type/subtype; everything after is a qualifier.
        for tok in tokens[1:]:
            tok = tok.lstrip("!").strip()
            if tok:
                qualifiers.append(tok)
    return qualifiers


def _extract_clause_keys(raw_line: str) -> list[str]:
    """Return dict clause keys appearing in raw_line, deduped per row."""
    if not raw_line:
        return []
    return list(dict.fromkeys(_RAW_KEY_RE.findall(raw_line)))


def _load_commander_names(conn: sqlite3.Connection) -> set[str]:
    """Set of legal legendary-creature card names (the commander universe)."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM cards "
            "WHERE legal_commander = 1 "
            "AND types LIKE '%Creature%' "
            "AND supertypes LIKE '%Legendary%'"
        )
    }


def _build_catalog(conn: sqlite3.Connection) -> dict:
    commander_names = _load_commander_names(conn)
    catalog: dict[tuple[str, str], dict] = {}

    for row in conn.execute("SELECT port_type, event_class, valid_filter, raw_line, card_name FROM card_ports"):
        pt = (row[0] or "").strip()
        ev = (row[1] or "").strip()
        key = (pt, ev)
        cell = catalog.get(key)
        if cell is None:
            cell = {
                "rows": 0,
                "cards": set(),
                "commanders": set(),
                "filter_qualifiers": collections.Counter(),
                "raw_clause_keys": collections.Counter(),
            }
            catalog[key] = cell
        cell["rows"] += 1
        card_name = row[4]
        if card_name:
            cell["cards"].add(card_name)
            if card_name in commander_names:
                cell["commanders"].add(card_name)
        for q in _extract_qualifiers(row[2] or ""):
            cell["filter_qualifiers"][q] += 1
        for k in _extract_clause_keys(row[3] or ""):
            cell["raw_clause_keys"][k] += 1

    out: dict = {
        "summary": {
            "total_port_rows": sum(c["rows"] for c in catalog.values()),
            "distinct_cards": len({n for c in catalog.values() for n in c["cards"]}),
            "legendary_commanders": len(commander_names),
            "distinct_port_event_pairs": len(catalog),
        },
        "port_types": collections.defaultdict(dict),
    }
    for (pt, ev), cell in catalog.items():
        out["port_types"][pt][ev] = {
            "rows": cell["rows"],
            "cards": len(cell["cards"]),
            "commanders": len(cell["commanders"]),
            "filter_qualifiers": dict(cell["filter_qualifiers"].most_common(20)),
            "raw_clause_keys": dict(cell["raw_clause_keys"].most_common(15)),
        }
    out["port_types"] = dict(out["port_types"])
    return out


def _print_summary(catalog: dict, top_n: int = 15) -> None:
    s = catalog["summary"]
    print(
        f"Port universe: {s['total_port_rows']:,} rows, "
        f"{s['distinct_cards']:,} cards, "
        f"{s['legendary_commanders']:,} legendary-creature commanders, "
        f"{s['distinct_port_event_pairs']:,} (port_type, event_class) cells"
    )

    for pt in sorted(catalog["port_types"]):
        events = catalog["port_types"][pt]
        ranked = sorted(events.items(), key=lambda kv: -kv[1]["commanders"])[:top_n]
        print(f"\n=== {pt}  ({len(events)} distinct events, top {top_n} by commander reach) ===")
        for ev, info in ranked:
            print(f"  {ev:35} commanders={info['commanders']:>4}  cards={info['cards']:>5}  rows={info['rows']:>6}")
            qs = list(info["filter_qualifiers"].items())[:5]
            if qs:
                print(f"      qualifiers: {', '.join(f'{q}({n})' for q, n in qs)}")
            ks = list(info["raw_clause_keys"].items())[:5]
            if ks:
                print(f"      clause keys: {', '.join(f'{k}({n})' for k, n in ks)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/port_universe.json"),
        help="JSON catalog output path",
    )
    parser.add_argument("--top", type=int, default=15, help="top N events per port_type to print")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} does not exist", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        catalog = _build_catalog(conn)
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2, sort_keys=True))
    _print_summary(catalog, top_n=args.top)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
