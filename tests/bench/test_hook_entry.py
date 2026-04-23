"""Unit 7 — ``bench.hook_entry`` pre-commit hook entry point tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_synergy_graph.bench import hook_entry
from mtg_synergy_graph.bench.fixture import build_fixture
from mtg_synergy_graph.db import open_db


def _seed(db_path: Path) -> None:
    """Minimal seeded DB identical to Unit 4 fixture."""
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


def _prepin(db_path: Path, fixture_path: Path) -> None:
    conn = open_db(db_path)
    try:
        build_fixture(conn, ["Test Commander"]).write(fixture_path)
    finally:
        conn.close()


@pytest.fixture()
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the hook from a throwaway cwd so `.audit/` doesn't pollute."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_hook_skips_when_db_missing(isolated_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No DB at the configured path → skip with exit 0."""
    exit_code = hook_entry.main()
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "SKIP" in err
    assert "DB" in err


def test_hook_skips_when_fixture_missing(isolated_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """DB exists but no pinned fixture → skip with exit 0 + --repin hint."""
    db_path = isolated_cwd / "synergy.db"
    _seed(db_path)
    with patch.dict("os.environ", {"BENCH_DB": str(db_path)}):
        exit_code = hook_entry.main()
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "SKIP" in err
    assert "--repin" in err


def test_hook_pass_after_repin(isolated_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Clean baseline → PASS summary written to stderr + .audit/last.md."""
    db_path = isolated_cwd / "synergy.db"
    fixture_path = isolated_cwd / "baseline.json"
    _seed(db_path)
    _prepin(db_path, fixture_path)

    with patch.dict(
        "os.environ",
        {"BENCH_DB": str(db_path), "BENCH_FIXTURE": str(fixture_path)},
    ):
        exit_code = hook_entry.main()
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "PASS" in err
    # Report written to .audit/last.md in the cwd.
    assert (isolated_cwd / ".audit" / "last.md").exists()


def test_hook_csv_format_warns_and_skips_write(
    isolated_cwd: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BENCH_FORMAT=csv is only meaningful for ``--trend``. The hook
    should NOT silently fall back to markdown (the historical bug) and
    should NOT crash; it should emit a stderr warning and skip the
    ``.audit/last.*`` write.
    """
    db_path = isolated_cwd / "synergy.db"
    fixture_path = isolated_cwd / "baseline.json"
    _seed(db_path)
    _prepin(db_path, fixture_path)

    with patch.dict(
        "os.environ",
        {
            "BENCH_DB": str(db_path),
            "BENCH_FIXTURE": str(fixture_path),
            "BENCH_FORMAT": "csv",
        },
    ):
        exit_code = hook_entry.main()

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "csv" in err.lower()
    # No last.md or last.json should be written on csv format.
    assert not (isolated_cwd / ".audit" / "last.md").exists()
    assert not (isolated_cwd / ".audit" / "last.json").exists()


def test_hook_json_format_writes_json(isolated_cwd: Path) -> None:
    """BENCH_FORMAT=json produces parseable JSON at .audit/last.json."""
    db_path = isolated_cwd / "synergy.db"
    fixture_path = isolated_cwd / "baseline.json"
    _seed(db_path)
    _prepin(db_path, fixture_path)

    with patch.dict(
        "os.environ",
        {
            "BENCH_DB": str(db_path),
            "BENCH_FIXTURE": str(fixture_path),
            "BENCH_FORMAT": "json",
        },
    ):
        exit_code = hook_entry.main()
    assert exit_code == 0
    last_json = isolated_cwd / ".audit" / "last.json"
    assert last_json.exists()
    parsed = json.loads(last_json.read_text())
    assert "aggregate_score_delta" in parsed


# ---------------------------------------------------------------------------
# HARMFUL verdict warn-but-proceed
# ---------------------------------------------------------------------------


def test_hook_exits_zero_on_harmful_verdict(isolated_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """HARMFUL audit must still exit 0 so the commit proceeds (advisory)."""
    db_path = isolated_cwd / "synergy.db"
    fixture_path = isolated_cwd / "baseline.json"
    _seed(db_path)
    _prepin(db_path, fixture_path)

    # Mutate pinned fixture: drop a score so live run shows a HARMFUL drift.
    data = json.loads(fixture_path.read_text())
    if data["entries"][0]["scores"]:
        first_cand = next(iter(data["entries"][0]["scores"]))
        # Raise pinned so live is lower → aggregate delta negative → HARMFUL.
        data["entries"][0]["scores"][first_cand] += 10.0
    fixture_path.write_text(json.dumps(data))

    with patch.dict(
        "os.environ",
        {"BENCH_DB": str(db_path), "BENCH_FIXTURE": str(fixture_path)},
    ):
        exit_code = hook_entry.main()

    assert exit_code == 0  # advisory, not blocking
    err = capsys.readouterr().err
    assert "⚠" in err or "HARMFUL" in err.upper()
    assert ".audit/last.md" in err


# ---------------------------------------------------------------------------
# Internal error path
# ---------------------------------------------------------------------------


def test_hook_returns_1_on_internal_error(isolated_cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unexpected exception → exit 1 so the author sees a real bug, not a silent pass."""
    db_path = isolated_cwd / "synergy.db"
    fixture_path = isolated_cwd / "baseline.json"
    _seed(db_path)
    _prepin(db_path, fixture_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated hook bug")

    with (
        patch.dict(
            "os.environ",
            {"BENCH_DB": str(db_path), "BENCH_FIXTURE": str(fixture_path)},
        ),
        patch("mtg_synergy_graph.bench.hook_entry.run_audit", _boom),
    ):
        exit_code = hook_entry.main()
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "INTERNAL ERROR" in err


# ---------------------------------------------------------------------------
# .gitignore coverage (static check)
# ---------------------------------------------------------------------------


def test_audit_dir_is_gitignored() -> None:
    """`.audit/` is in .gitignore so reports don't leak into commits.

    Resolves the repo root explicitly so this test passes regardless of
    pytest's cwd. ``tests/bench/`` is two parents down from the repo.
    """
    repo_root = Path(__file__).resolve().parents[2]
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".audit/" in gitignore
