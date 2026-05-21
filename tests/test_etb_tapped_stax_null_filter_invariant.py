"""Issue #15: ``etb_tapped_stax_feeder`` requires non-NULL ``valid_filter``.

The declarative rule predicate compiles to SQL ``NOT (valid_filter LIKE
'%Self%')``. In SQLite three-valued logic, ``NOT (NULL LIKE '%Self%')``
evaluates to NULL and is treated as FALSE by the WHERE clause, silently
dropping rows. The previous Python helper used
``valid_filter IS NULL OR valid_filter NOT LIKE '%Self%'`` and explicitly
included NULL rows.

This invariant test asserts that no row in ``card_ports`` matching the
``replacement + Moved + ETBTapped`` port shape has a NULL ``valid_filter``.
A future ``import_cardsfolder.py`` run that emitted such a row would
cause the declarative rule to silently under-fire.

The test runs only when the populated production DB ``data/synergy.db``
exists; CI runners without the imported DB skip it. The bug manifests
only against real Forge-imported data, so a fixture-based test would not
exercise the failure mode.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_PROJECT_DB = Path(__file__).resolve().parent.parent / "data" / "synergy.db"


def _open_real_db_readonly() -> sqlite3.Connection:
    """Open ``data/synergy.db`` read-only, or skip the test if absent."""
    if not _PROJECT_DB.exists():
        pytest.skip("data/synergy.db not present; skipping production-data invariant")
    uri = f"file:{_PROJECT_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def test_etb_tapped_stax_feeder_has_no_null_valid_filter_rows() -> None:
    """No ``replacement+Moved+ETBTapped`` row may have NULL ``valid_filter``.

    The ``etb_tapped_stax_feeder`` declarative rule's
    ``NOT filter_tag(Self)`` clause silently drops NULL-filter rows due
    to SQLite NULL semantics (issue #15). The invariant guards against
    a future importer change that would introduce such rows.

    Today's count is 0 / 597 rows (verified 2026-04-24). If this test
    fails, fix the importer OR extend the declarative grammar with a
    NULL-as-match semantic before relying on this rule again.
    """
    conn = _open_real_db_readonly()
    try:
        null_rows = conn.execute(
            "SELECT COUNT(*) FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'Moved' "
            "AND replacement_result = 'ETBTapped' "
            "AND valid_filter IS NULL"
        ).fetchone()[0]
        total_rows = conn.execute(
            "SELECT COUNT(*) FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'Moved' "
            "AND replacement_result = 'ETBTapped'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert null_rows == 0, (
        f"etb_tapped_stax_feeder NULL-filter invariant broken: "
        f"{null_rows}/{total_rows} replacement+Moved+ETBTapped rows have NULL valid_filter. "
        f"These rows would be silently dropped by the rule's NOT filter_tag(Self) clause. "
        f"See issue #15."
    )
    assert total_rows > 0, (
        "No replacement+Moved+ETBTapped rows in card_ports — invariant trivially passes "
        "but the rule has nothing to operate on. Likely an importer regression."
    )
