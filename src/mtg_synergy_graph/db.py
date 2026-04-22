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
    """
    # check_same_thread=False is required: consumers like mtg-edh-builder
    # run engine methods via Starlette's run_in_threadpool (multi-threaded).
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_schema_sql())
    conn.commit()
    return conn
