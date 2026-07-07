import json
import math
import sqlite3
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# quality_sim kill-test instrument (plan 2026-07-06-001 Task 10)
# ---------------------------------------------------------------------------


def test_assemble_quality_q0_is_identity_and_reorders_at_q():
    from typing import ClassVar

    from mtg_synergy_graph.bench.quality_sim import assemble_quality

    class FakeSim:
        commander = "C"
        pool_order = ("A", "B")
        base_top_30 = ("A", "B")
        base_totals: ClassVar = {"A": 1.00, "B": 0.99}
        cmc_lookup: ClassVar = {}
        rank_lookup: ClassVar = {}

    rates = {"B": 10.0, "A": 0.0}
    assert assemble_quality(FakeSim(), rates, q=0.0, r0=2.0) == ("A", "B")
    assert assemble_quality(FakeSim(), rates, q=0.2, r0=2.0) == ("B", "A")


def test_build_parser_has_subcommands():
    from mtg_synergy_graph.bench.quality_sim import build_parser

    p = build_parser()
    args = p.parse_args(["bands", "--fixture", "tests/fixtures/golden_set_run.json"])
    assert args.command == "bands"


def test_parse_cells_malformed_raises_quality_sim_error():
    from mtg_synergy_graph.bench.quality_sim import QualitySimError, _parse_cells

    with pytest.raises(QualitySimError, match="malformed cell"):
        _parse_cells("0.05,1.0;not-a-cell")


def test_parse_cells_empty_raises_quality_sim_error():
    from mtg_synergy_graph.bench.quality_sim import QualitySimError, _parse_cells

    with pytest.raises(QualitySimError, match="produced no cells"):
        _parse_cells("   ;  ")


_DB = Path("data/synergy.db")


@pytest.mark.skipif(not _DB.exists(), reason="requires built data/synergy.db")
def test_bands_smoke_two_commanders(tmp_path):
    from mtg_synergy_graph.bench.quality_sim import main

    rc = main(
        [
            "bands",
            "--fixture",
            "tests/fixtures/golden_set_run.json",
            "--limit-commanders",
            "2",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    report = json.loads((tmp_path / "bands.json").read_text())
    assert "ndcg_band" in report and report["n_commanders"] == 2
