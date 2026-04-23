"""Tests for ``bench.py audit --vs-forge-oracle`` handler.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 7.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
from mtg_synergy_graph.bench.forge_oracle_handler import handle_vs_forge_oracle
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.forge_oracle import config as fo_config


def _make_synergy_db(tmp_path: Path) -> Path:
    """Minimal synergy.db with four cards + hints so pair_scorer returns
    distinguishable scores."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        conn.executemany(
            "INSERT INTO cards "
            "(name, oracle_id, card_types, types, subtypes, supertypes, color_identity, keywords) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("Commander One", "cmdr1", "Creature", "Creature Goblin", "Goblin", "Legendary", "R", "[]"),
                ("Goblin A", "ga", "Creature", "Creature Goblin", "Goblin", "", "R", "[]"),
                ("Goblin B", "gb", "Creature", "Creature Goblin", "Goblin", "", "R", "[]"),
                ("Serra Angel", "sa", "Creature", "Creature Angel", "Angel", "", "W", '["Flying"]'),
            ],
        )
        conn.execute(
            "INSERT INTO card_hints (card_name, kind, category, value) VALUES ('Commander One', 'hints', 'Type', 'Goblin')"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _make_fixture(tmp_path: Path, entries: list[FixtureEntry]) -> Path:
    path = tmp_path / "fixture.json"
    fixture = PinnedFixture(config_hash="unused", created_at="2026-04-23", entries=entries)
    fixture.write(path)
    return path


def _make_forge_oracle_db(tmp_path: Path) -> Path:
    """Build a minimal forge_oracle.db with a current config hash."""
    path = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE forge_precon_ppmi ("
        "  port_signature_a TEXT, port_signature_b TEXT,"
        "  ppmi REAL, decks_count INTEGER, last_updated TEXT,"
        "  PRIMARY KEY (port_signature_a, port_signature_b));"
        "CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    inputs = fo_config.get_oracle_config_inputs(ppmi_smoothing_k=0.5, min_decks_count=3)
    fo_config.write_oracle_config(conn, inputs)
    conn.commit()
    conn.close()
    return path


def _args(
    *,
    fixture: Path,
    db: Path,
    forge_oracle_db: Path,
    output: str | None = None,
    fmt: str | None = None,
    limit: int = 30,
    commander: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        fixture=str(fixture),
        db=str(db),
        forge_oracle_db=str(forge_oracle_db),
        smoothing_k=0.5,
        min_decks=3,
        limit=limit,
        output=output,
        format=fmt,
        commander=commander,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_emits_markdown_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """End-to-end: fixture + synergy.db + forge_oracle.db → Markdown report on stdout."""
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [
            FixtureEntry(
                commander="Commander One",
                scores={"Goblin A": 10.0, "Goblin B": 5.0, "Serra Angel": 1.0},
            ),
        ],
    )

    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db))
    assert rc == 0
    captured = capsys.readouterr()
    assert "vs-forge-oracle" in captured.out
    assert "Aggregate Forge-agreement" in captured.out
    assert "Commander One" in captured.out


def test_json_format(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [
            FixtureEntry(
                commander="Commander One",
                scores={"Goblin A": 10.0, "Goblin B": 5.0, "Serra Angel": 1.0},
            ),
        ],
    )
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db, fmt="json"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_commanders"] == 1
    assert payload["per_commander"][0]["commander"] == "Commander One"
    assert isinstance(payload["aggregate_tau"], float)


def test_output_file(tmp_path: Path) -> None:
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0, "Goblin B": 5.0})],
    )
    out_path = tmp_path / "report.md"
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db, output=str(out_path)))
    assert rc == 0
    assert out_path.is_file()
    assert "Aggregate Forge-agreement" in out_path.read_text(encoding="utf-8")


def test_commander_filter_restricts_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [
            FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0, "Goblin B": 5.0}),
            FixtureEntry(commander="Commander Ghost", scores={"Goblin A": 10.0}),  # commander absent from DB
        ],
    )
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db, commander="Commander One"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Commander One" in out
    assert "Commander Ghost" not in out


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_fixture_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    rc = handle_vs_forge_oracle(_args(fixture=tmp_path / "nope.json", db=db, forge_oracle_db=forge_db))
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_missing_forge_oracle_db_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_synergy_db(tmp_path)
    fixture = _make_fixture(tmp_path, [FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0})])
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=tmp_path / "nope.db"))
    assert rc == 2
    assert "forge_oracle.db" in capsys.readouterr().err


def test_stale_config_hash_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A DB with a different smoothing_k in oracle_config → stale → exit 2."""
    db = _make_synergy_db(tmp_path)
    forge_db = tmp_path / "forge_oracle.db"
    conn = sqlite3.connect(forge_db)
    conn.executescript(
        "CREATE TABLE forge_precon_ppmi ("
        "  port_signature_a TEXT, port_signature_b TEXT,"
        "  ppmi REAL, decks_count INTEGER, last_updated TEXT,"
        "  PRIMARY KEY (port_signature_a, port_signature_b));"
        "CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    # Write a hash computed from k=2.0 (different from the 0.5 default args
    # we'll pass in). Handler will compute current with 0.5 and find mismatch.
    stale_inputs = fo_config.get_oracle_config_inputs(ppmi_smoothing_k=2.0, min_decks_count=3)
    fo_config.write_oracle_config(conn, stale_inputs)
    conn.commit()
    conn.close()

    fixture = _make_fixture(tmp_path, [FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0})])
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db))
    assert rc == 2
    assert "different config" in capsys.readouterr().err.lower() or "rebuild" in capsys.readouterr().err.lower()


def test_unknown_commander_filter_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(tmp_path, [FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0})])
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db, commander="No Such Commander"))
    assert rc == 2


def test_no_scorable_commanders_returns_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """All fixture entries reference commander names absent from the DB → nothing to score."""
    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [FixtureEntry(commander="Ghost Commander", scores={"Ghost A": 1.0, "Ghost B": 0.5})],
    )
    rc = handle_vs_forge_oracle(_args(fixture=fixture, db=db, forge_oracle_db=forge_db))
    assert rc == 2
    assert "no commanders scorable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Integration with the top-level CLI
# ---------------------------------------------------------------------------


def test_cli_dispatches_to_vs_forge_oracle_handler(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`bench.py audit --vs-forge-oracle` wires through cli.main and the
    mode resolver picks the new handler."""
    from mtg_synergy_graph.bench import main as bench_main

    db = _make_synergy_db(tmp_path)
    forge_db = _make_forge_oracle_db(tmp_path)
    fixture = _make_fixture(
        tmp_path,
        [FixtureEntry(commander="Commander One", scores={"Goblin A": 10.0, "Goblin B": 5.0})],
    )
    argv = [
        "audit",
        "--vs-forge-oracle",
        "--fixture",
        str(fixture),
        "--db",
        str(db),
        "--forge-oracle-db",
        str(forge_db),
    ]
    rc = bench_main(argv)
    assert rc == 0
    assert "vs-forge-oracle" in capsys.readouterr().out


def test_cli_conflicting_mode_flags_rejected(tmp_path: Path) -> None:
    """--vs-forge-oracle is in the mutex group — cannot combine with e.g. --repin."""
    from mtg_synergy_graph.bench import main as bench_main

    with pytest.raises(SystemExit):
        bench_main(["audit", "--vs-forge-oracle", "--repin"])
