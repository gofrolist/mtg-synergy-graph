"""One-shot bootstrap: build a 500-commander pinned fixture for optimizer validation.

Selects the top 500 legendary creatures by ``edhrec_rank`` that have at
least one ``High Synergy Cards`` row in EDHREC's tags.db. The
high-synergy section caps at 10 rows per commander upstream, so we
require ``>= 1`` not ``>= 30``; the optimizer's nDCG and gem-rate
metrics tolerate small label sets, and ``_warn_missing_edhrec_labels``
already drops commanders with no labels from the gradient.

Output: ``tests/fixtures/golden_set_run_500.json``. The canonical 100-cmdr
fixture at ``tests/fixtures/golden_set_run.json`` is left untouched.

Re-run after every cardsfolder import or scoring config change. Takes
~5-10 seconds on the 100-commander baseline; ~1-2 minutes for 500.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Make src/ importable when run as a bare script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mtg_synergy_graph.bench.fixture import build_fixture
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.validate import commander_to_slug

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNERGY_DB = REPO_ROOT / "data" / "synergy.db"
EDHREC_DB = REPO_ROOT / "data" / "tags.db"
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_set_run_500.json"
TARGET_COUNT = 500
MIN_HIGH_SYNERGY_ROWS = 1


def _select_top_commanders() -> list[str]:
    """Top legendary creatures by EDHREC rank with sufficient EDHREC data."""
    cards_conn = open_db(str(SYNERGY_DB))
    edhrec_conn = open_db(str(EDHREC_DB))
    try:
        # Build the slug -> count map in one query instead of N round trips.
        # The corpus is ~25k commander slugs; one GROUP BY beats 1.5k point lookups.
        slug_counts: dict[str, int] = dict(
            edhrec_conn.execute(
                "SELECT commander_slug, COUNT(*) FROM edhrec_card_synergy "
                "WHERE section = 'High Synergy Cards' "
                "GROUP BY commander_slug"
            ).fetchall()
        )

        # Pull more than 500 candidates because some will fail the high-synergy
        # filter. ~3x oversample empirically covers it.
        rows = cards_conn.execute(
            "SELECT name, edhrec_rank FROM cards "
            "WHERE legal_commander = 1 "
            "AND supertypes LIKE '%Legendary%' "
            "AND card_types LIKE '%Creature%' "
            "AND edhrec_rank IS NOT NULL "
            "ORDER BY edhrec_rank ASC LIMIT ?",
            (TARGET_COUNT * 3,),
        ).fetchall()

        selected: list[str] = []
        skipped_no_edhrec = 0
        for name, _rank in rows:
            count = slug_counts.get(commander_to_slug(name), 0)
            if count < MIN_HIGH_SYNERGY_ROWS:
                skipped_no_edhrec += 1
                continue
            selected.append(name)
            if len(selected) >= TARGET_COUNT:
                break

        print(
            f"selected {len(selected)} / {TARGET_COUNT} commanders "
            f"(scanned {len(rows)} candidates; skipped {skipped_no_edhrec} no-edhrec)",
            file=sys.stderr,
        )
        return selected
    finally:
        cards_conn.close()
        edhrec_conn.close()


def main() -> int:
    if not SYNERGY_DB.exists():
        print(f"error: {SYNERGY_DB} not found. Run scripts/import_cardsfolder.py.", file=sys.stderr)
        return 2
    if not EDHREC_DB.exists():
        print(f"error: {EDHREC_DB} not found.", file=sys.stderr)
        return 2

    commanders = _select_top_commanders()
    if len(commanders) < TARGET_COUNT:
        print(
            f"warning: only {len(commanders)} commanders met the EDHREC filter "
            f"(target was {TARGET_COUNT}); proceeding with what we got.",
            file=sys.stderr,
        )

    config_hash = compute_config_hash()
    conn = open_db(str(SYNERGY_DB))
    edhrec_conn = open_db(str(EDHREC_DB))
    try:
        fixture = build_fixture(conn, commanders, edhrec_conn=edhrec_conn)
    finally:
        conn.close()
        edhrec_conn.close()

    fixture.config_hash = config_hash
    fixture.created_at = datetime.now(UTC).isoformat(timespec="seconds")
    fixture.write(OUTPUT_PATH)
    print(
        f"wrote {len(fixture.entries)} entries to {OUTPUT_PATH} (config_hash={config_hash[:12]}...)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
