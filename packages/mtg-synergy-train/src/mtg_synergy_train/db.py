"""Centralized database connection factory.

All modules that need SQLite access should use get_connection() from here
instead of calling sqlite3.connect() directly.
"""
import sqlite3
from mtg_synergy.config import DB_PATH

# Hardcoded allowlist — never accept pragma names/values from external input.
_SAFE_PRAGMAS: dict[str, str] = {
    "journal_mode": "WAL",
    "synchronous": "NORMAL",
}


def get_connection(path: str | None = None) -> sqlite3.Connection:
    """Create a configured SQLite connection.

    Args:
        path: Database file path. Defaults to config.DB_PATH.

    Returns:
        Connection with WAL mode and NORMAL sync enabled.
    """
    db_path = path or str(DB_PATH)
    conn = sqlite3.connect(db_path)
    for pragma, value in _SAFE_PRAGMAS.items():
        conn.execute(f"PRAGMA {pragma}={value}")
    return conn
