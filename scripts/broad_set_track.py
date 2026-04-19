"""Full-set NDCG regression tracker.

Complements ``golden_set_track.py``. Where the golden set is 100
hand-picked commanders for deep per-commander tolerance checks,
this script tracks aggregate NDCG@30 + per-commander outliers
across the **entire** EDHREC-matched legendary creature universe
(~2,790 commanders).

Catches regressions on commanders the golden set doesn't cover —
e.g. a new rule that ships clean against the 100 golden commanders
but degrades the long tail. Together with ``--touched-only`` mode
(see ``_touched_commanders.py``), per-attempt validation stays
cheap because only commanders whose ports activate the new rule's
gate get re-scored; the rest inherit baseline.

Two modes (mirroring golden_set_track):

  --bootstrap --baseline <path>
    Score every EDHREC-matched commander in parallel and write per-
    commander NDCG@30 + aggregate to ``<path>``. Use after an
    intentional scoring change to refresh the baseline.

  --check --baseline <path>
    Re-score and compare. Exits non-zero if:
    - aggregate drops more than ``--ndcg-tolerance`` (default
      0.001), OR
    - any commander drops more than ``--per-commander-tolerance``
      (default 0.05).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from _touched_commanders import find_touched_oracle_ids

from mtg_synergy_graph import SynergyEngine, commander_to_slug, compare_to_edhrec
from mtg_synergy_graph.complement_rules.registry import RULE_GATES


def _matched_commanders(syn_db: Path, edhrec_db: Path) -> list[tuple[str, str]]:
    """Return ``[(name, oracle_id), ...]`` for legendary creatures
    that ARE in synergy.db AND have an EDHREC slug match in tags.db.
    Sorted by name for determinism.
    """
    edhrec = sqlite3.connect(edhrec_db)
    slugs = {
        r[0]
        for r in edhrec.execute(
            "SELECT DISTINCT commander_slug FROM edhrec_card_synergy WHERE section='High Synergy Cards'"
        )
    }
    edhrec.close()

    syn = sqlite3.connect(syn_db)
    syn.row_factory = sqlite3.Row
    rows = syn.execute(
        "SELECT name, oracle_id FROM cards "
        "WHERE legal_commander = 1 "
        "AND types LIKE '%Creature%' "
        "AND supertypes LIKE '%Legendary%' "
        "AND oracle_id IS NOT NULL "
        "ORDER BY name"
    ).fetchall()
    syn.close()
    return [(r["name"], r["oracle_id"]) for r in rows if commander_to_slug(r["name"]) in slugs]


def _score_batch(
    args: tuple[Path, Path, list[tuple[str, str]]],
) -> dict[str, float]:
    """Worker entry: open own DB connections, score one batch."""
    syn_db, edhrec_db, batch = args
    edhrec_conn = sqlite3.connect(edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    out: dict[str, float] = {}
    try:
        with SynergyEngine(syn_db) as eng:
            for _name, oid in batch:
                try:
                    page = eng.page_by_oracle_id([oid], offset=0, limit=30)
                except (LookupError, ValueError):
                    continue
                cmp = compare_to_edhrec(page, edhrec_conn, top_n=30)
                if cmp.hi_syn_size > 0:
                    out[oid] = cmp.hi_syn_hits / cmp.hi_syn_size
    finally:
        edhrec_conn.close()
    return out


def _max_workers() -> int:
    """Worker count: cap at 8 to avoid SQLite contention thrashing."""
    return min(os.cpu_count() or 4, 8)


def _ndcg_per_commander(
    syn_db: Path,
    edhrec_db: Path,
    sample: list[tuple[str, str]],
    *,
    parallel: bool = True,
) -> dict[str, float]:
    """Return ``{oracle_id: ndcg}`` where ndcg = hi_syn_hits/hi_syn_size
    for commanders with hi_syn_size > 0; commanders without curated
    Hi-Syn data are dropped (can't compare).

    Parallelizes across processes when ``len(sample) > 16`` — below
    that the worker startup overhead outweighs the speedup.
    """
    if not sample:
        return {}
    n_workers = _max_workers() if parallel and len(sample) > 16 else 1
    if n_workers <= 1:
        return _score_batch((syn_db, edhrec_db, sample))

    # Round-robin distribution = balanced batches when scoring cost
    # has long tails (some commanders are expensive).
    batches: list[list[tuple[str, str]]] = [[] for _ in range(n_workers)]
    for i, item in enumerate(sample):
        batches[i % n_workers].append(item)
    work = [(syn_db, edhrec_db, b) for b in batches if b]

    out: dict[str, float] = {}
    with ProcessPoolExecutor(max_workers=n_workers) as pool:
        for chunk in pool.map(_score_batch, work):
            out.update(chunk)
    return out


def _aggregate(per_cmdr: dict[str, float]) -> float:
    return sum(per_cmdr.values()) / len(per_cmdr) if per_cmdr else 0.0


def _bootstrap(
    syn_db: Path,
    edhrec_db: Path,
    universe: list[tuple[str, str]],
    out_path: Path,
) -> int:
    print(
        f"computing NDCG for {len(universe)} EDHREC-matched commanders (parallel={_max_workers()} workers)...",
        file=sys.stderr,
    )
    per_cmdr = _ndcg_per_commander(syn_db, edhrec_db, universe)
    payload = {
        "universe_size": len(universe),
        "scored_commanders": len(per_cmdr),
        "aggregate_ndcg": _aggregate(per_cmdr),
        "per_commander": per_cmdr,
        "names_by_oracle_id": {oid: name for name, oid in universe},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"wrote baseline: {out_path}", file=sys.stderr)
    print(f"  scored: {len(per_cmdr)}/{len(universe)}", file=sys.stderr)
    print(f"  aggregate NDCG: {payload['aggregate_ndcg']:.4f}", file=sys.stderr)
    return 0


def _check(
    syn_db: Path,
    edhrec_db: Path,
    baseline_path: Path,
    aggregate_tolerance: float,
    per_commander_tolerance: float,
    touched_only: list[str] | None = None,
) -> int:
    if not baseline_path.exists():
        print(f"error: baseline {baseline_path} not found — run --bootstrap first", file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text())
    baseline_per: dict[str, float] = baseline["per_commander"]
    baseline_agg: float = baseline["aggregate_ndcg"]
    names_by_oid: dict[str, str] = baseline.get("names_by_oracle_id", {})

    # Re-score exactly the same commanders the baseline scored.
    sample = [(names_by_oid.get(oid, oid), oid) for oid in baseline_per]

    if touched_only:
        registered = {g.rule_id for g in RULE_GATES}
        unregistered = [r for r in touched_only if r not in registered]
        if unregistered:
            print(
                f"error: --touched-only rule(s) without registered gate: {unregistered}. "
                "Inheriting baseline for everyone would silently mask regressions; refusing.",
                file=sys.stderr,
            )
            return 2
        touched_oids = find_touched_oracle_ids(syn_db, names_by_oid, touched_only)
        sample_to_score = [(name, oid) for name, oid in sample if oid in touched_oids]
        print(
            f"touched-only mode ({','.join(touched_only)}): "
            f"re-scoring {len(sample_to_score)}/{len(sample)} commanders, "
            f"inheriting baseline for {len(sample) - len(sample_to_score)}",
            file=sys.stderr,
        )
        fresh_touched = _ndcg_per_commander(syn_db, edhrec_db, sample_to_score)
        # Untouched commanders inherit baseline scores (purely additive
        # rule changes can't perturb their NDCG).
        fresh_per = {oid: fresh_touched.get(oid, baseline_per[oid]) for oid in baseline_per}
    else:
        print(f"re-scoring {len(sample)} baseline commanders...", file=sys.stderr)
        fresh_per = _ndcg_per_commander(syn_db, edhrec_db, sample)
    fresh_agg = _aggregate(fresh_per)

    print(f"  baseline aggregate NDCG: {baseline_agg:.4f}", file=sys.stderr)
    print(f"  fresh aggregate NDCG:    {fresh_agg:.4f}", file=sys.stderr)
    print(f"  delta:                   {fresh_agg - baseline_agg:+.4f}", file=sys.stderr)

    failed = False
    agg_drop = baseline_agg - fresh_agg
    if agg_drop > aggregate_tolerance:
        print(
            f"\nFAIL: aggregate NDCG dropped {agg_drop:.4f} > {aggregate_tolerance}",
            file=sys.stderr,
        )
        failed = True

    # Per-commander outliers — significant individual regressions.
    drops = []
    for oid, base in baseline_per.items():
        fresh = fresh_per.get(oid, 0.0)
        delta = base - fresh
        if delta > per_commander_tolerance:
            drops.append((names_by_oid.get(oid, oid), base, fresh, delta))
    drops.sort(key=lambda x: -x[3])
    if drops:
        print(f"\nFAIL: {len(drops)} commander(s) dropped more than {per_commander_tolerance}:", file=sys.stderr)
        for name, base, fresh, delta in drops[:10]:
            print(f"  {name[:50]:50}  {base:.3f} -> {fresh:.3f}  (-{delta:.3f})", file=sys.stderr)
        if len(drops) > 10:
            print(f"  ... and {len(drops) - 10} more", file=sys.stderr)
        failed = True

    if not failed:
        print("\nPASS: no aggregate or per-commander regression.", file=sys.stderr)
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/synergy.db"))
    parser.add_argument("--edhrec-db", type=Path, default=Path("data/tags.db"))
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--bootstrap", action="store_true", help="write baseline (overwrites)")
    parser.add_argument(
        "--ndcg-tolerance",
        type=float,
        default=0.001,
        help="max tolerated aggregate NDCG drop (default 0.001)",
    )
    parser.add_argument(
        "--per-commander-tolerance",
        type=float,
        default=0.05,
        help="max tolerated per-commander NDCG drop (default 0.05)",
    )
    parser.add_argument(
        "--touched-only",
        nargs="+",
        metavar="RULE_ID",
        help=(
            "Re-score only commanders whose ports activate any of the "
            "named rules' registered gates. Inherit baseline scores for "
            "the rest. Safe ONLY for purely additive rule changes "
            "(new helper module + new gate, no edits to existing rules)."
        ),
    )
    args = parser.parse_args()

    if args.bootstrap:
        universe = sorted(_matched_commanders(args.db, args.edhrec_db))
        return _bootstrap(args.db, args.edhrec_db, universe, args.baseline)

    return _check(
        args.db,
        args.edhrec_db,
        args.baseline,
        aggregate_tolerance=args.ndcg_tolerance,
        per_commander_tolerance=args.per_commander_tolerance,
        touched_only=args.touched_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
