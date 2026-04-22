"""Unit 4 — ``bench.py audit`` main subcommand tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import cli as bench_cli
from mtg_synergy_graph.bench.audit import run_audit
from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
    PinnedFixture,
    TensorRow,
    build_fixture,
)
from mtg_synergy_graph.bench.report import build_report
from mtg_synergy_graph.db import open_db


@pytest.fixture()
def seeded_db_path(tmp_path: Path) -> Path:
    """Minimal DB shared by Unit 3 & Unit 4 tests."""
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


def _prepin(db_path: Path, fixture_path: Path, commanders: list[str]) -> None:
    conn = open_db(db_path)
    try:
        build_fixture(conn, commanders).write(fixture_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build_report aggregation
# ---------------------------------------------------------------------------


def test_build_report_clean_pass() -> None:
    """Pinned == live → identical report, aggregate delta 0."""
    entry = FixtureEntry(
        commander="C",
        scores={"A": 1.0, "B": 0.5},
        tensor_rows=[
            TensorRow(candidate="A", rule_id="r1", contribution=0.5, idf_weight=0.5, raw_count=1),
        ],
    )
    pinned = PinnedFixture(config_hash="x", created_at="t", entries=[entry])
    live = PinnedFixture(config_hash="x", created_at="t", entries=[entry])

    report = build_report("baseline.json", pinned, live)
    assert report.is_identical
    assert report.aggregate_score_delta == 0.0
    assert report.per_commander[0].score_delta_sum == 0.0
    assert report.per_commander[0].added_to_top30 == ()
    assert report.per_commander[0].removed_from_top30 == ()
    assert report.per_rule == []


def test_build_report_score_drift_flags_commander() -> None:
    """Score change on one candidate bubbles into per-commander delta."""
    pinned_entry = FixtureEntry(commander="C", scores={"A": 1.0, "B": 0.5})
    live_entry = FixtureEntry(commander="C", scores={"A": 1.5, "B": 0.5})

    pinned = PinnedFixture(config_hash="x", created_at="t", entries=[pinned_entry])
    live = PinnedFixture(config_hash="x", created_at="t", entries=[live_entry])

    report = build_report("baseline.json", pinned, live)
    assert not report.is_identical
    assert report.aggregate_score_delta == pytest.approx(0.5)
    cd = report.per_commander[0]
    assert cd.commander == "C"
    assert cd.score_delta_sum == pytest.approx(0.5)


def test_build_report_top30_add_remove() -> None:
    """Top-30 membership change is surfaced as added/removed lists."""
    pinned_scores = {f"cand_{i}": 1.0 - i * 0.01 for i in range(35)}
    live_scores = dict(pinned_scores)
    # Drop cand_29 out of top-30 (lower its score); lift cand_30 in.
    live_scores["cand_29"] = 0.0
    live_scores["cand_30"] = 1.5

    pinned_entry = FixtureEntry(commander="C", scores=pinned_scores)
    live_entry = FixtureEntry(commander="C", scores=live_scores)

    pinned = PinnedFixture(config_hash="x", created_at="t", entries=[pinned_entry])
    live = PinnedFixture(config_hash="x", created_at="t", entries=[live_entry])

    report = build_report("baseline.json", pinned, live)
    cd = report.per_commander[0]
    assert "cand_30" in cd.added_to_top30
    assert "cand_29" in cd.removed_from_top30


def test_build_report_rule_rollup() -> None:
    """Tensor drift rolls up into per-rule deltas with cmdr/cand counts."""
    pinned_entry = FixtureEntry(
        commander="C",
        scores={"A": 0.5},
        tensor_rows=[
            TensorRow(candidate="A", rule_id="r1", contribution=0.5, idf_weight=0.5, raw_count=1),
        ],
    )
    live_entry = FixtureEntry(
        commander="C",
        scores={"A": 0.8},
        tensor_rows=[
            TensorRow(candidate="A", rule_id="r1", contribution=0.8, idf_weight=0.5, raw_count=1),
        ],
    )

    pinned = PinnedFixture(config_hash="x", created_at="t", entries=[pinned_entry])
    live = PinnedFixture(config_hash="x", created_at="t", entries=[live_entry])

    report = build_report("baseline.json", pinned, live)
    assert len(report.per_rule) == 1
    rd = report.per_rule[0]
    assert rd.rule_id == "r1"
    assert rd.contribution_delta_sum == pytest.approx(0.3)
    assert rd.commanders_touched == 1
    assert rd.candidates_touched == 1


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_report_markdown_renders_headers_and_status() -> None:
    pinned = PinnedFixture(config_hash="abc12345deadbeef", created_at="t")
    live = PinnedFixture(config_hash="abc12345deadbeef", created_at="t")
    report = build_report("baseline.json", pinned, live)

    md = report.to_markdown()
    assert "# bench.py audit" in md
    assert "baseline.json" in md
    assert "identical" in md
    assert "abc12345dead" in md


def test_report_json_is_valid_and_round_trips() -> None:
    """JSON output is parseable and includes expected top-level keys."""
    pinned = PinnedFixture(config_hash="x", created_at="t")
    live = PinnedFixture(config_hash="y", created_at="t")
    report = build_report("baseline.json", pinned, live)

    js = report.to_json()
    parsed = json.loads(js)
    assert parsed["fixture_path"] == "baseline.json"
    assert parsed["pinned_config_hash"] == "x"
    assert parsed["live_config_hash"] == "y"
    assert parsed["config_hash_matches"] is False


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_audit_cli_pass_on_freshly_pinned_fixture(
    tmp_path: Path, seeded_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end: repin then `bench.py audit` → exit 0, PASS summary."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])

    # Run from tmp_path so .audit/last.md lands there.
    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        exit_code = bench_cli.main(
            [
                "audit",
                "--db",
                str(seeded_db_path),
                "--fixture",
                str(fixture_path),
            ]
        )
    finally:
        os.chdir(old_cwd)
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "PASS" in err
    # Default-output side-effect: `.audit/last.md` written in the cwd.
    assert (tmp_path / ".audit" / "last.md").exists()


def test_audit_cli_detects_drift_exit_1(
    tmp_path: Path, seeded_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inject drift, confirm exit=1 + DRIFT summary + mismatch details."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])

    # Mutate one score in the pinned fixture.
    data = json.loads(fixture_path.read_text())
    if data["entries"][0]["scores"]:
        first_cand = next(iter(data["entries"][0]["scores"]))
        data["entries"][0]["scores"][first_cand] += 1.0
    fixture_path.write_text(json.dumps(data))

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        exit_code = bench_cli.main(
            [
                "audit",
                "--db",
                str(seeded_db_path),
                "--fixture",
                str(fixture_path),
            ]
        )
    finally:
        os.chdir(old_cwd)

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "DRIFT" in err


def test_audit_cli_missing_fixture_errors(
    tmp_path: Path, seeded_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fixture file doesn't exist → exit 2 with actionable hint."""
    exit_code = bench_cli.main(
        [
            "audit",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(tmp_path / "nope.json"),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "--repin" in err


def test_audit_cli_empty_fixture_errors(
    tmp_path: Path, seeded_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Well-formed JSON but no entries → exit 2."""
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
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
        ]
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "no entries" in err.lower()


def test_audit_cli_writes_to_explicit_output(tmp_path: Path, seeded_db_path: Path) -> None:
    """`--output path.md` writes there instead of `.audit/last.md`."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])
    out_path = tmp_path / "report.md"

    exit_code = bench_cli.main(
        [
            "audit",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
            "--output",
            str(out_path),
        ]
    )
    assert exit_code == 0
    assert out_path.exists()
    assert "bench.py audit" in out_path.read_text()


def test_audit_cli_json_format(tmp_path: Path, seeded_db_path: Path) -> None:
    """`--format json` produces valid JSON to the output file."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])
    out_path = tmp_path / "report.json"

    exit_code = bench_cli.main(
        [
            "audit",
            "--db",
            str(seeded_db_path),
            "--fixture",
            str(fixture_path),
            "--format",
            "json",
            "--output",
            str(out_path),
        ]
    )
    assert exit_code == 0
    parsed = json.loads(out_path.read_text())
    assert "aggregate_score_delta" in parsed
    assert "per_commander" in parsed
    assert "per_rule" in parsed


# ---------------------------------------------------------------------------
# run_audit integration
# ---------------------------------------------------------------------------


def test_run_audit_returns_report_on_clean_fixture(tmp_path: Path, seeded_db_path: Path) -> None:
    """Direct API call after repin returns a passing report."""
    fixture_path = tmp_path / "baseline.json"
    _prepin(seeded_db_path, fixture_path, ["Test Commander"])
    report = run_audit(seeded_db_path, fixture_path)
    assert report.is_identical
    assert report.aggregate_score_delta == 0.0
