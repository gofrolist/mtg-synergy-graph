"""SQLite helpers: open + initialize the synergy.db schema."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def _schema_sql() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with the v1.2.2 schema applied.

    Safe to call repeatedly — uses CREATE TABLE IF NOT EXISTS, so existing
    rows are preserved. Foreign keys + WAL are enabled for the new package.

    The ``port_nodes`` view is created via
    :func:`port_graph.projection.create_port_nodes_view` immediately
    after applying ``schema.sql`` so the view DDL has exactly one
    source of truth (``PORT_NODES_VIEW_SQL``) — the inline copy that
    used to live in ``schema.sql`` was removed to eliminate drift.
    """
    # check_same_thread=False is required: consumers like mtg-edh-builder
    # run engine methods via Starlette's run_in_threadpool (multi-threaded).
    conn = sqlite3.connect(str(path), check_same_thread=False)
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
