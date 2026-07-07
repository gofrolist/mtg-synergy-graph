import math
import sqlite3

from mtg_synergy_graph.quality import _amount_value, quality_multiplier, rate_signal


def test_amount_value_mapping():
    assert _amount_value("3") == 3.0
    assert _amount_value("X") == 2.5
    assert _amount_value("All") == 4.0
    assert _amount_value("SVarWeird") == 1.0
    assert _amount_value("99") == 6.0  # capped
    assert _amount_value("-1") == 0.0  # floored


def test_quality_multiplier_bounded():
    assert quality_multiplier(0.0, q=0.2, r0=2.0) == 1.0
    assert quality_multiplier(1e9, q=0.2, r0=2.0) < 1.2 + 1e-9


def test_rate_signal_from_synthetic_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE cards (name TEXT, cmc REAL)")
    conn.execute("CREATE TABLE card_ports (card_name TEXT, port_type TEXT, amount TEXT)")
    conn.execute("INSERT INTO cards VALUES ('Engine', 2.0), ('OneShot', 2.0)")
    conn.executemany(
        "INSERT INTO card_ports VALUES (?, ?, ?)",
        [("Engine", "trigger", ""), ("Engine", "effect", "2"), ("OneShot", "effect", "2")],
    )
    rates = rate_signal(conn)
    assert math.isclose(rates["Engine"], 1.0)  # 1.0 * 2 / 2
    assert math.isclose(rates["OneShot"], 0.5)  # 0.5 * 2 / 2 (no engine shape)
