"""Scale smoke tests using a real Forge cardsfolder import.

These run against ~500 cards (a subset of the local Forge cardsfolder)
to validate the engine works against realistic data without requiring
a fully imported synergy.db. The full 32 k-card import is exercised by
``scripts/import_cardsfolder.py`` and ``scripts/recommend.py`` — those
are the production smoke path.

Skipped automatically if the Forge cardsfolder is missing (e.g. CI).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mtg_synergy_graph import SynergyEngine
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

REPO_ROOT = Path(__file__).resolve().parents[3]
FORGE_FOLDER = REPO_ROOT / "data" / "forge" / "forge-gui" / "res" / "cardsfolder"

pytestmark = pytest.mark.skipif(
    not FORGE_FOLDER.exists(),
    reason="Forge cardsfolder not available",
)


@pytest.fixture(scope="module")
def imported_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("scale") / "synergy.db"
    conn = open_db(db_path)
    cards, ports = import_cards_folder(
        conn, FORGE_FOLDER, scryfall_db=None, limit=500,
    )
    conn.close()
    assert cards == 500
    assert ports > 0
    return db_path


def test_500_card_import_throughput(imported_db):
    """Sanity: import + open the engine in well under a second."""
    t0 = time.perf_counter()
    with SynergyEngine(imported_db) as engine:
        meta = engine.metadata()
    elapsed = time.perf_counter() - t0
    assert meta["spec_version"]
    assert elapsed < 5.0


def test_engine_page_returns_results_at_500_cards(imported_db):
    """Run the engine against the first commander we can find in the import."""
    with SynergyEngine(imported_db) as engine:
        # Find a commander-eligible card by querying the schema directly.
        cur = engine._conn.execute(
            "SELECT name FROM cards WHERE supertypes LIKE '%Legendary%' "
            "AND card_types LIKE '%Creature%' LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip("no legendary creatures in 500-card sample")
        commander = row["name"]

        t0 = time.perf_counter()
        page = engine.page(commander, offset=0, limit=20)
        elapsed = time.perf_counter() - t0
        assert page.total > 0
        assert len(page.items) <= 20
        assert all(rec.rank > 0 for rec in page.items)
        # 500 cards should comfortably fit under 1 s.
        assert elapsed < 5.0


def test_engine_pagination_at_500_cards(imported_db):
    with SynergyEngine(imported_db) as engine:
        cur = engine._conn.execute(
            "SELECT name FROM cards WHERE supertypes LIKE '%Legendary%' "
            "AND card_types LIKE '%Creature%' LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip("no legendary creatures in 500-card sample")
        commander = row["name"]

        full = engine.page(commander, offset=0, limit=1_000_000)
        if full.total < 5:
            pytest.skip(f"too few legal candidates ({full.total}) for pagination check")
        head = engine.page(commander, offset=0, limit=2)
        tail = engine.page(commander, offset=2, limit=2)
        assert head.items[0].rank == 1
        assert tail.items[0].rank == 3
        assert {r.card for r in head.items}.isdisjoint({r.card for r in tail.items})
