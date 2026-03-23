import sqlite3

def test_get_connection_returns_connection():
    from mtg_synergy.db import get_connection
    conn = get_connection()
    assert isinstance(conn, sqlite3.Connection)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()

def test_get_connection_custom_path(tmp_path):
    from mtg_synergy.db import get_connection
    db_file = tmp_path / "test.db"
    conn = get_connection(str(db_file))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.close()
    assert db_file.exists()
