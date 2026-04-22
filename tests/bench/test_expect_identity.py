"""CLI-level tests for ``--repin`` and ``--expect-identity`` (Unit 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench import cli as bench_cli
from mtg_synergy_graph.bench.fixture import build_fixture
from mtg_synergy_graph.db import open_db


@pytest.fixture()
def seeded_db_path(tmp_path: Path) -> Path:
    """Same minimal DB fixture used throughout Unit 3, as a path."""
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        conn.execute(
            "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Test Commander", "Creature", "", 4, "R", 1000, 1),
        )
        conn.execute(
            "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Token Maker", "Creature", "", 3, "R", 500, 1),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
            ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _prepin(conn_path: Path, fixture_path: Path, commanders: list[str]) -> None:
    """Helper to pre-populate a fixture without going through the CLI."""
    conn = open_db(conn_path)
    try:
        build_fixture(conn, commanders).write(fixture_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# --repin
# ---------------------------------------------------------------------------


def test_repin_without_yes_is_refused(tmp_path: Path, seeded_db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No accidental overwrites: ``--repin`` without ``--yes`` exits non-zero."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])
    before_mtime = fixture_path.stat().st_mtime

    exit_code = bench_cli.main(
        [
            "audit",
            "--repin",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 2
    # File untouched.
    assert fixture_path.stat().st_mtime == before_mtime
    err = capsys.readouterr().err
    assert "--yes" in err


def test_repin_without_existing_fixture_requires_bootstrap(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Edge case: no fixture yet → --repin --yes errors with bootstrap hint.

    Rationale: we derive the commander list from the existing fixture,
    so the very first fixture has to be created via
    ``golden_set_track.py --bootstrap``. ``--repin`` refuses to guess.
    """
    fixture_path = tmp_path / "missing.json"
    exit_code = bench_cli.main(
        [
            "audit",
            "--repin",
            "--yes",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "bootstrap" in err.lower()


def test_repin_with_yes_writes_fresh_fixture(
    tmp_path: Path,
    seeded_db_path: Path,
) -> None:
    """Happy path: ``--repin --yes`` overwrites the pinned fixture."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])
    before_mtime = fixture_path.stat().st_mtime

    # Sleep-free mtime guarantee: write the file with a past mtime.
    import os

    os.utime(fixture_path, (before_mtime - 60, before_mtime - 60))

    exit_code = bench_cli.main(
        [
            "audit",
            "--repin",
            "--yes",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 0
    assert fixture_path.stat().st_mtime > before_mtime - 60


# ---------------------------------------------------------------------------
# --expect-identity
# ---------------------------------------------------------------------------


def test_expect_identity_pass_on_freshly_pinned_fixture(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: right after ``--repin``, ``--expect-identity`` is PASS."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])

    exit_code = bench_cli.main(
        [
            "audit",
            "--expect-identity",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "PASS" in err


def test_expect_identity_missing_fixture_errors(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Edge case: fixture doesn't exist yet → exit 2 with actionable hint."""
    fixture_path = tmp_path / "does_not_exist.json"
    exit_code = bench_cli.main(
        [
            "audit",
            "--expect-identity",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--repin" in err


def test_expect_identity_fails_on_injected_drift(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inject a score drift into the fixture and confirm exit=1 + diagnostic."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])

    # Inject a drift by editing one score in the fixture JSON.
    import json

    data = json.loads(fixture_path.read_text())
    if data["entries"][0]["scores"]:
        first_cand = next(iter(data["entries"][0]["scores"]))
        data["entries"][0]["scores"][first_cand] += 0.5
    fixture_path.write_text(json.dumps(data))

    exit_code = bench_cli.main(
        [
            "audit",
            "--expect-identity",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "score mismatches" in err.lower()
    assert "FAIL" in err


def test_expect_identity_flags_config_hash_drift(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Config-hash mismatch is called out as a remediation path."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])

    # Inject stale config_hash.
    import json

    data = json.loads(fixture_path.read_text())
    data["config_hash"] = "0" * 64
    fixture_path.write_text(json.dumps(data))

    exit_code = bench_cli.main(
        [
            "audit",
            "--expect-identity",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "config_hash" in err


def test_expect_identity_empty_fixture_errors(
    tmp_path: Path,
    seeded_db_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture exists but has no entries → exit 2 with message.

    Guards against a silent success when the fixture was corrupted to
    an empty state.
    """
    import json

    fixture_path = tmp_path / "empty.json"
    fixture_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config_hash": "x",
                "created_at": "t",
                "entries": [],
            }
        )
    )
    exit_code = bench_cli.main(
        [
            "audit",
            "--expect-identity",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "no entries" in err.lower()
