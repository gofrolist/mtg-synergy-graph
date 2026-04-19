"""Broad-set NDCG regression tracker.

Complements ``golden_set_track.py``. Where the golden set is 100
hand-picked commanders, this script samples ``--sample`` commanders
(default 500, deterministic seed) from the EDHREC-matched legendary
creature universe and tracks aggregate NDCG@30 against a baseline.

Catches regressions on commanders the golden set doesn't cover —
e.g. a new rule that ships clean against the 100 golden commanders
but degrades 200 niche commanders. Without this check, the autonomy
stack only validates ~3% of the engine's surface area.

Two modes (mirroring golden_set_track):

  --bootstrap --baseline <path>
    Compute current per-commander NDCG@30 + aggregate, write to
    ``<path>``. Use after an intentional scoring change to refresh
    the broad baseline.

  --check --baseline <path>
    Re-compute and compare. Exits non-zero if:
    - aggregate drops more than ``--ndcg-tolerance`` (default
      0.001), OR
    - any commander drops more than ``--per-commander-tolerance``
      (default 0.05).

Sample is deterministic: ``random.seed(--seed)`` (default 42) +
sorted candidate list = same N commanders every run. Re-snapshot
only when the underlying card pool / commander universe changes.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

from mtg_synergy_graph import SynergyEngine, commander_to_slug, compare_to_edhrec


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


def _ndcg_per_commander(
    syn_db: Path,
    edhrec_db: Path,
    sample: list[tuple[str, str]],
) -> dict[str, float]:
    """Return ``{oracle_id: ndcg}`` where ndcg = hi_syn_hits/hi_syn_size
    for commanders with hi_syn_size > 0; commanders without curated
    Hi-Syn data are dropped (can't compare).
    """
    edhrec_conn = sqlite3.connect(edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    out: dict[str, float] = {}
    with SynergyEngine(syn_db) as eng:
        for _name, oid in sample:
            try:
                page = eng.page_by_oracle_id([oid], offset=0, limit=30)
            except (LookupError, ValueError):
                continue
            cmp = compare_to_edhrec(page, edhrec_conn, top_n=30)
            if cmp.hi_syn_size > 0:
                out[oid] = cmp.hi_syn_hits / cmp.hi_syn_size
    edhrec_conn.close()
    return out


def _aggregate(per_cmdr: dict[str, float]) -> float:
    return sum(per_cmdr.values()) / len(per_cmdr) if per_cmdr else 0.0


def _bootstrap(
    syn_db: Path,
    edhrec_db: Path,
    sample: list[tuple[str, str]],
    out_path: Path,
) -> int:
    print(f"computing NDCG for {len(sample)} sampled commanders...", file=sys.stderr)
    per_cmdr = _ndcg_per_commander(syn_db, edhrec_db, sample)
    payload = {
        "sample_size": len(sample),
        "scored_commanders": len(per_cmdr),
        "aggregate_ndcg": _aggregate(per_cmdr),
        "per_commander": per_cmdr,
        "names_by_oracle_id": {oid: name for name, oid in sample},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"wrote baseline: {out_path}", file=sys.stderr)
    print(f"  scored: {len(per_cmdr)}/{len(sample)}", file=sys.stderr)
    print(f"  aggregate NDCG: {payload['aggregate_ndcg']:.4f}", file=sys.stderr)
    return 0


def _check(
    syn_db: Path,
    edhrec_db: Path,
    baseline_path: Path,
    aggregate_tolerance: float,
    per_commander_tolerance: float,
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
    parser.add_argument("--sample", type=int, default=500, help="commander sample size for bootstrap")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic sampling")
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
    args = parser.parse_args()

    if args.bootstrap:
        all_matched = _matched_commanders(args.db, args.edhrec_db)
        # Deterministic sampling — not a security context. Suppress S311.
        rng = random.Random(args.seed)  # noqa: S311
        sample = sorted(rng.sample(all_matched, min(args.sample, len(all_matched))))
        return _bootstrap(args.db, args.edhrec_db, sample, args.baseline)

    return _check(
        args.db,
        args.edhrec_db,
        args.baseline,
        aggregate_tolerance=args.ndcg_tolerance,
        per_commander_tolerance=args.per_commander_tolerance,
    )


if __name__ == "__main__":
    raise SystemExit(main())
