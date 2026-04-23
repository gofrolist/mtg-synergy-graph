"""Shared helpers for querying the EDHREC synergy SQLite DB.

The ``edhrec_card_synergy`` table's ``section = 'High Synergy Cards'``
query shape is used by at least two callers — ``bench.fixture`` (for
the hidden-gem metric) and ``validate._run_one`` (for the golden-set
tracker). Centralising the section string + query SQL here keeps the
two call sites in sync and removes the risk of one drifting off the
other when EDHREC renames a section.
"""

from __future__ import annotations

import sqlite3

#: Section name as it appears verbatim in ``edhrec_card_synergy``.
#: Changing this literal requires updating the EDHREC importer at the
#: same time — the value is pinned to what the upstream EDHREC scraper
#: emits.
HIGH_SYNERGY_SECTION = "High Synergy Cards"


def _query_high_synergy(
    edhrec_conn: sqlite3.Connection,
    commander_name: str,
    limit: int,
) -> list[str]:
    """Internal: return ordered card names (desc synergy) or empty list."""
    # Local import to avoid a module-level cycle — ``validate`` now
    # imports from this module for the shared-helper fix. Moving
    # ``commander_to_slug`` itself would invalidate tests that import
    # it from ``validate``.
    from mtg_synergy_graph.validate import commander_to_slug

    slug = commander_to_slug(commander_name)
    rows = edhrec_conn.execute(
        "SELECT card_name FROM edhrec_card_synergy "
        "WHERE commander_slug = ? AND section = ? "
        "ORDER BY synergy DESC LIMIT ?",
        (slug, HIGH_SYNERGY_SECTION, limit),
    ).fetchall()
    # Rows come as sqlite3.Row or plain tuple; the first column is
    # always ``card_name``. Index by position to handle both.
    return [r[0] for r in rows]


def fetch_high_synergy_top_n(
    edhrec_conn: sqlite3.Connection,
    commander_name: str,
    *,
    limit: int = 30,
) -> set[str] | None:
    """Return the top ``limit`` High Synergy Cards for a commander.

    Returns ``None`` when the commander has no rows in the
    ``'High Synergy Cards'`` section (either because no rows exist for
    the commander at all, or because rows exist only in other
    sections). ``None`` is the "no reference data → skip" sentinel
    used by downstream callers; an empty set would conflate "no data"
    with "commander has zero high-synergy entries."

    ``limit`` defaults to 30 so the hidden-gem caller can use the bare
    helper; callers that want a smaller window override.
    """
    names = _query_high_synergy(edhrec_conn, commander_name, limit)
    if not names:
        return None
    return set(names)


def fetch_high_synergy_top_n_ordered(
    edhrec_conn: sqlite3.Connection,
    commander_name: str,
    *,
    limit: int = 10,
) -> tuple[str, ...]:
    """Return the top ``limit`` High Synergy Cards as an ordered tuple.

    Preserves synergy-descending order. Returns an empty tuple when
    the commander has no rows. Used by callers that need rank order
    (e.g. ``validate._run_one`` for its ``edhrec_top10`` field).
    """
    return tuple(_query_high_synergy(edhrec_conn, commander_name, limit))


__all__ = [
    "HIGH_SYNERGY_SECTION",
    "fetch_high_synergy_top_n",
    "fetch_high_synergy_top_n_ordered",
]
