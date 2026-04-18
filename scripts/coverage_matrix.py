"""Coverage matrix: which port shapes have rules vs. which are gaps.

Two-axis report over the legendary-creature commander universe:

1. **Formal rule coverage** — derived statically from
   ``COMPLEMENT_RULES``. Each formal rule declares ``cmdr_port_type``
   and ``event_pairs`` (the cmdr_event keys it consumes). A
   (port_type, event_class) cell is "formally covered" iff at least
   one rule has ``cmdr_port_type == port_type`` AND ``event_class in
   event_pairs``. This is exact.

2. **Empirical rule activation** — derived dynamically by running
   ``find_all_complements`` on every commander and recording which
   rule_ids emit at least one PortComplement. A cell is
   "empirically covered" iff some commander carrying that port shape
   produces *any* rule activation. Card-attribute rules (the loose
   ``_card_attr_complements`` family) don't expose their gate
   statically, so this signal catches them too.

Per (port_type, event_class) cell the report includes:

- ``commanders``: distinct legendary-creature commanders carrying
  this port shape (from the port universe).
- ``formally_covered_by``: list of rule_ids whose ``event_pairs``
  include this cmdr_event (port_type-aware).
- ``empirical_activations``: count of commanders carrying this shape
  who get at least one rule activation in find_all_complements.
- ``activation_rate``: empirical_activations / commanders, capped at
  1.0. A cell with high commander reach AND low activation rate is a
  genuine gap.

Outputs:
- ``docs/coverage_matrix.json`` — full matrix.
- stdout — top gaps (high commanders, low activation_rate, no formal
  coverage), then top covered cells for reference.
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

from mtg_synergy_graph.complement_rules.core import (
    COMPLEMENT_RULES,
    find_all_complements,
    load_ports_for_set,
)
from mtg_synergy_graph.penalties import build_candidate_cache


def _formal_coverage() -> dict[tuple[str, str], list[str]]:
    """Return mapping (port_type, event_class) → [rule_id, ...] from
    the static ``event_pairs`` of every formal ComplementRule.
    """
    out: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for rule in COMPLEMENT_RULES:
        for cmdr_event in rule.event_pairs:
            out[(rule.cmdr_port_type, cmdr_event)].append(rule.rule_id)
    return dict(out)


def _commander_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM cards "
            "WHERE legal_commander = 1 "
            "AND types LIKE '%Creature%' "
            "AND supertypes LIKE '%Legendary%' "
            "ORDER BY name"
        )
    ]


def _commander_port_shapes(conn: sqlite3.Connection, commander_names: list[str]) -> dict[str, set[tuple[str, str]]]:
    """Return mapping commander_name → set of (port_type, event_class)
    shapes carried by their ports.
    """
    out: dict[str, set[tuple[str, str]]] = {}
    for name in commander_names:
        ports = load_ports_for_set(conn, [name])
        out[name] = {((p.get("port_type") or "").strip(), (p.get("event_class") or "").strip()) for p in ports}
    return out


def _build_matrix(
    conn: sqlite3.Connection,
    commanders: list[str],
    shapes_by_cmdr: dict[str, set[tuple[str, str]]],
    *,
    progress_every: int = 200,
) -> dict[tuple[str, str], dict]:
    formal = _formal_coverage()
    cache = build_candidate_cache(conn)

    # Aggregate per cell: total commanders carrying it, and how many
    # produce at least one rule activation across find_all_complements.
    commanders_per_cell: collections.Counter[tuple[str, str]] = collections.Counter()
    activations_per_cell: collections.Counter[tuple[str, str]] = collections.Counter()
    rule_hits_per_cell: dict[tuple[str, str], collections.Counter[str]] = collections.defaultdict(collections.Counter)

    for shapes in shapes_by_cmdr.values():
        for cell in shapes:
            commanders_per_cell[cell] += 1

    t0 = time.time()
    for i, name in enumerate(commanders):
        if i and i % progress_every == 0:
            elapsed = time.time() - t0
            print(
                f"  ...{i}/{len(commanders)}  ({elapsed:.0f}s, {i / elapsed:.0f}/s)",
                file=sys.stderr,
                flush=True,
            )
        try:
            comps = find_all_complements(conn, [name], candidate_cache=cache)
        except Exception as exc:
            print(f"  [skip] {name}: {exc}", file=sys.stderr)
            continue
        rule_ids = {c.rule_id for c in comps}
        for cell in shapes_by_cmdr.get(name, ()):
            if rule_ids:
                activations_per_cell[cell] += 1
                for rid in rule_ids:
                    rule_hits_per_cell[cell][rid] += 1

    matrix: dict[tuple[str, str], dict] = {}
    for cell, n_cmdrs in commanders_per_cell.items():
        n_act = activations_per_cell.get(cell, 0)
        rate = n_act / n_cmdrs if n_cmdrs else 0.0
        matrix[cell] = {
            "commanders": n_cmdrs,
            "formally_covered_by": formal.get(cell, []),
            "empirical_activations": n_act,
            "activation_rate": rate,
            "top_rules": dict(rule_hits_per_cell[cell].most_common(5)),
        }
    return matrix


def _print_summary(matrix: dict[tuple[str, str], dict], top_n: int = 25) -> None:
    cells = list(matrix.items())

    # Gaps: no formal coverage, ranked by commander reach.
    gaps_no_formal = [(cell, info) for cell, info in cells if not info["formally_covered_by"]]
    gaps_no_formal.sort(key=lambda kv: -kv[1]["commanders"])

    # Hard gaps: low activation rate AND no formal coverage AND
    # meaningful reach.
    hard_gaps = [
        (cell, info) for cell, info in gaps_no_formal if info["commanders"] >= 10 and info["activation_rate"] < 0.5
    ]

    print("\n=== HARD GAPS: no formal rule, <50% empirical activation, ≥10 commanders ===")
    print(f"{'port_type':>12}  {'event_class':35}  {'cmdrs':>5}  {'act':>6}  top_rules")
    for (pt, ev), info in hard_gaps[:top_n]:
        rules = ", ".join(f"{r}({n})" for r, n in info["top_rules"].items())
        print(f"{pt:>12}  {ev[:35]:35}  {info['commanders']:>5}  {info['activation_rate'] * 100:>5.0f}%  {rules}")

    print("\n=== TOP UNCOVERED CELLS (no formal rule, sorted by commander reach) ===")
    for (pt, ev), info in gaps_no_formal[:top_n]:
        rules = ", ".join(f"{r}({n})" for r, n in info["top_rules"].items())
        print(f"{pt:>12}  {ev[:35]:35}  {info['commanders']:>5}  {info['activation_rate'] * 100:>5.0f}%  {rules}")

    formally_covered = [(c, i) for c, i in cells if i["formally_covered_by"]]
    formally_covered.sort(key=lambda kv: -kv[1]["commanders"])
    print(f"\n=== FORMAL COVERAGE: {len(formally_covered)}/{len(cells)} cells covered by ≥1 rule ===")
    for (pt, ev), info in formally_covered[:10]:
        cov = ", ".join(info["formally_covered_by"])
        print(f"{pt:>12}  {ev[:35]:35}  {info['commanders']:>5}  {info['activation_rate'] * 100:>5.0f}%  {cov}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/coverage_matrix.json"),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="if >0, sample only the first N commanders (for fast iteration)",
    )
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        commanders = _commander_names(conn)
        if args.limit > 0:
            commanders = commanders[: args.limit]
        print(f"loading port shapes for {len(commanders)} commanders...", file=sys.stderr)
        shapes_by_cmdr = _commander_port_shapes(conn, commanders)
        print("running find_all_complements...", file=sys.stderr)
        matrix = _build_matrix(conn, commanders, shapes_by_cmdr)
    finally:
        conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    serialised = {f"{pt}|{ev}": info for (pt, ev), info in sorted(matrix.items())}
    args.out.write_text(json.dumps(serialised, indent=2, sort_keys=True))
    _print_summary(matrix, top_n=args.top)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
