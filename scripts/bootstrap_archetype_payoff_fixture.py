"""One-shot bootstrap: build the archetype-payoff cohort fixture.

Selects the archetype-payoff cohort (``bench.cohorts.archetype_payoff_cohort``:
legal legendary-creature commanders with a subtype-keyed death payoff), filters
to those with at least one ``High Synergy Cards`` row in EDHREC's tags.db, and
pins their top-N scores to ``tests/fixtures/golden_set_archetype_payoff.json``.

The cohort membership is **snapshotted** into the fixture (``cohort_members``)
so the per-commander NDCG cohort slice partitions reproducibly from the pin —
the live predicate is a volatile multi-join over port data that can partition
differently after a cardsfolder refresh (plan 2026-07-03-001 Key Decision 4).

This is an evaluation instrument, NOT a scoring change: the golden-100/500 audit
fixtures hold only a couple of these commanders, so a future archetype-payoff
mechanism's cohort effect is invisible in the aggregate. Re-run after every
cardsfolder import or scoring-config change (the fixture carries its own
``config_hash`` and is enforced by ``tests/bench/test_fixture_freshness.py``).

Exit codes mirror ``bootstrap_golden_set_500.py``: 0 success, 2 if a required
DB is missing.

``_select_cohort_commanders`` and ``main`` take a ``cohort_fn`` (plan
2026-07-07-002 Task 3) so a second cohort predicate can reuse this build/pin
protocol without duplicating it — see ``scripts/bootstrap_outlet_payoff_fixture.py``.
Both default to ``archetype_payoff_cohort`` / ``OUTPUT_PATH`` so this module's
own behavior (including the ``boot.main()`` / ``boot._select_cohort_commanders()``
no-arg calls in ``tests/bench/test_archetype_payoff_fixture.py``, which
monkeypatch the module-level ``SYNERGY_DB``/``EDHREC_DB``/``OUTPUT_PATH``
globals) is unchanged.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

# Make src/ importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtg_synergy_graph.bench.cohorts import archetype_payoff_cohort
from mtg_synergy_graph.bench.fixture import build_and_write_fixture, high_synergy_slug_counts
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.validate import commander_to_slug

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNERGY_DB = REPO_ROOT / "data" / "synergy.db"
EDHREC_DB = REPO_ROOT / "data" / "tags.db"
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_archetype_payoff.json"
MIN_HIGH_SYNERGY_ROWS = 1

CohortFn = Callable[[sqlite3.Connection], set[str]]


def _select_cohort_commanders(cohort_fn: CohortFn = archetype_payoff_cohort) -> list[str]:
    """Cohort commanders with sufficient EDHREC High-Synergy data.

    Returns the kept list ordered by ``edhrec_rank`` ascending (nulls last),
    then name — deterministic so re-builds reproduce the same entry order.
    Drops (and logs) cohort commanders with zero ``High Synergy Cards`` rows.
    """
    cards_conn = open_db(str(SYNERGY_DB), create=False)
    edhrec_conn = open_db(str(EDHREC_DB), create=False)
    try:
        cohort = sorted(cohort_fn(cards_conn))
        slug_counts = high_synergy_slug_counts(edhrec_conn)

        # edhrec_rank for deterministic ordering, fetched in one static query
        # over the legendary-creature superset (every cohort member is one) —
        # avoids N point lookups without dynamic SQL / injection surface.
        ranks: dict[str, int | None] = dict(
            cards_conn.execute(
                "SELECT name, edhrec_rank FROM cards "
                "WHERE legal_commander = 1 "
                "AND supertypes LIKE '%Legendary%' "
                "AND card_types LIKE '%Creature%'"
            ).fetchall()
        )

        kept: list[str] = []
        dropped: list[str] = []
        for name in cohort:
            if slug_counts.get(commander_to_slug(name), 0) < MIN_HIGH_SYNERGY_ROWS:
                dropped.append(name)
            else:
                kept.append(name)

        kept.sort(key=lambda n: (ranks.get(n) if ranks.get(n) is not None else 1_000_000_000, n))

        print(
            f"cohort={len(cohort)} kept={len(kept)} dropped={len(dropped)} (no High-Synergy EDHREC data)",
            file=sys.stderr,
        )
        if dropped:
            print(f"  dropped: {sorted(dropped)}", file=sys.stderr)
        return kept
    finally:
        cards_conn.close()
        edhrec_conn.close()


def main(cohort_fn: CohortFn = archetype_payoff_cohort, output_path: Path | None = None) -> int:
    if not SYNERGY_DB.exists():
        print(f"error: {SYNERGY_DB} not found. Run scripts/import_cardsfolder.py.", file=sys.stderr)
        return 2
    if not EDHREC_DB.exists():
        print(f"error: {EDHREC_DB} not found.", file=sys.stderr)
        return 2

    commanders = _select_cohort_commanders(cohort_fn)
    if not commanders:
        print("error: no cohort commanders cleared the EDHREC High-Synergy filter.", file=sys.stderr)
        return 2

    # output_path defaults to the module-level OUTPUT_PATH read at call time
    # (not bound as a default-arg value) so tests that monkeypatch
    # boot.OUTPUT_PATH and call boot.main() with no args keep working.
    resolved_output_path = output_path if output_path is not None else OUTPUT_PATH

    # Snapshot cohort membership for reproducible slice partitioning (KD 4).
    fixture = build_and_write_fixture(
        SYNERGY_DB, EDHREC_DB, commanders, resolved_output_path, cohort_members=sorted(commanders)
    )
    print(
        f"wrote {len(fixture.entries)} entries to {resolved_output_path} (config_hash={fixture.config_hash[:12]}...)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
