"""SQLite helpers: open + initialize the synergy.db schema."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def _schema_sql() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def open_db(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """Open a SQLite connection with the v1.2.2 schema applied.

    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS, so existing
    rows are preserved. Foreign keys + WAL are enabled for the new package.

    ``create=False`` refuses to materialize a missing database: a
    nonexistent ``path`` raises :class:`FileNotFoundError` with a
    rebuild hint instead of silently producing a fully-schema'd empty
    DB at that path (the test-isolation landmine documented in
    CLAUDE.md Conventions). Read-side consumers (``SynergyEngine``)
    pass ``create=False``; the importer and fixture builders keep the
    creating default. ``:memory:`` paths are always allowed.

    The ``port_nodes`` view is created via
    :func:`port_graph.projection.create_port_nodes_view` immediately
    after applying ``schema.sql`` so the view DDL has exactly one
    source of truth (``PORT_NODES_VIEW_SQL``) — the inline copy that
    used to live in ``schema.sql`` was removed to eliminate drift.
    """
    path_str = str(path)
    if not create and path_str != ":memory:" and not Path(path_str).exists():
        raise FileNotFoundError(
            f"SQLite database not found: {path_str} — run "
            "scripts/import_cardsfolder.py to build it (or call "
            "open_db(..., create=True) if materializing a new DB is intended)."
        )
    # check_same_thread=False is required: consumers like mtg-edh-builder
    # run engine methods via Starlette's run_in_threadpool (multi-threaded).
    conn = sqlite3.connect(path_str, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_schema_sql())
    # Deferred import: port_graph.projection is a small leaf module but
    # importing it at top-level creates a dependency cycle risk as more
    # consumers grow. Local import keeps db.py trivially importable.
    from .port_graph.projection import create_port_nodes_view

    create_port_nodes_view(conn)
    conn.commit()
    return conn
