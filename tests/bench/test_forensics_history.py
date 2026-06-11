"""Forensics history CSV + ``--trend forensics`` tests (Unit 5 of
plan 2026-06-10-001).

DB-shell tests build a synthetic synergy.db from the committed Forge
fixture cards under ``tmp_path`` ONLY (the session conftest sentinel
fails the run on any stray ``*.db`` under the repo root or ``data/``)
and ALWAYS route both the report (``--output``) and the history CSV
(``--forensics-history``) to ``tmp_path`` so nothing is ever written
under the repo root ``.audit/``. Seeding helpers are local copies of
the ones in ``tests/bench/test_handle_forensics.py`` (tests/ is not an
importable package). Reader / trend tests are pure (hand-written CSV
files, no DB).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.forensics import ForensicsReconciliationError
from mtg_synergy_graph.bench.forensics_history import (
    BOUNDARY_MARKER,
    FORENSICS_CSV_FIELDS,
    edhrec_snapshot_digest,
    fixture_file_sha256,
    handle_trend_forensics,
    read_last_forensics,
)
from mtg_synergy_graph.bench.forensics_report import handle_forensics
from mtg_synergy_graph.bench.history import CSV_FIELDS
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

KORVOLD = "Korvold, Fae-Cursed King"
KORVOLD_SLUG = "korvold-fae-cursed-king"


# ---------------------------------------------------------------------------
# Seeding helpers — local copies from sibling forensics test modules
# ---------------------------------------------------------------------------


def _write_fixture(path: Path, commanders: list[object]) -> None:
    """Minimal PinnedFixture JSON (schema v2 shape)."""
    payload = {
        "schema_version": 2,
        "config_hash": "irrelevant-for-forensics",
        "created_at": "2026-06-10T00:00:00+00:00",
        "entries": [{"commander": c, "scores": {}} for c in commanders],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_tensor_row(
    conn: sqlite3.Connection,
    commander: str,
    candidate: str,
    config_hash: str,
    *,
    rule_id: str = "test_rule",
    contribution: float = 1.0,
) -> None:
    conn.execute(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, config_hash, computed_at) "
        "VALUES (?, ?, ?, ?, 1.0, 1, ?, '2026-06-10T00:00:00+00:00')",
        (commander, candidate, rule_id, contribution, config_hash),
    )


def _make_tags_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
        )
        conn.executemany("INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _args(
    db: Path | str,
    fixture: Path | str,
    tags: Path | str,
    *,
    history: Path | str,
    output: Path | str,
    fmt: str = "md",
) -> argparse.Namespace:
    """Namespace with the attrs handle_forensics reads. ``output`` and
    ``history`` are MANDATORY here so no test can accidentally write to
    the repo-root ``.audit/``."""
    return argparse.Namespace(
        db=str(db),
        fixture=str(fixture),
        edhrec_db=str(tags),
        format=fmt,
        output=str(output),
        forensics_history=str(history),
    )


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return (raw_lines, rows-as-dicts) of a history CSV."""
    lines = path.read_text(encoding="utf-8").splitlines()
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return lines, rows


# ---------------------------------------------------------------------------
# Trend-test row helper (no DB)
# ---------------------------------------------------------------------------


def _row_cells(
    *,
    ts: str = "2026-06-10T00:00:00+00:00",
    commit: str = "deadbeef",
    config: str = "cfg1",
    fixture: str = "f" * 64,
    snap: str = "snap1",
    syn: str = "syn1",
    ndcg: str = "0.100000",
    gem: str = "0.300000",
) -> list[str]:
    """One well-formed 16-cell forensics history row."""
    return [
        ts,
        commit,
        config,
        fixture,
        snap,
        syn,
        "0.200000",
        "0.200000",
        "0.200000",
        "0.200000",
        "0.200000",
        ndcg,
        "0.150000",
        gem,
        "1",
        "0",
    ]


def _write_history_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FORENSICS_CSV_FIELDS)
        writer.writerows(rows)


def _trend_args(history: Path, *, fmt: str = "md", trend_n: int = 20) -> argparse.Namespace:
    return argparse.Namespace(
        forensics_history=str(history),
        trend_n=trend_n,
        format=fmt,
        output=None,
    )


# ---------------------------------------------------------------------------
# Fixtures — module-scoped synergy.db (import is the slow part)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synergy_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """synergy.db built from the committed Forge fixture cards, with
    tensor rows at the CURRENT config hash (tensor-populated
    precondition + gem-rate cohort inputs)."""
    db_path = tmp_path_factory.mktemp("forensics_history") / "synergy.db"
    conn = open_db(db_path)
    try:
        import_cards_folder(conn, FIXTURES, scryfall_db=None)
        config_hash = compute_config_hash()
        _seed_tensor_row(conn, KORVOLD, "Phyrexian Altar", config_hash, rule_id="trigger_effect", contribution=2.0)
        _seed_tensor_row(conn, KORVOLD, "Bloodghast", config_hash, rule_id="gy_fuel_feeder", contribution=1.0)
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def paths(synergy_db: Path, tmp_path: Path) -> dict[str, Path]:
    """tags.db (with HS data → gem rate non-None) + fixture.json +
    per-test history/output targets under tmp_path."""
    tags_path = tmp_path / "tags.db"
    fixture_path = tmp_path / "fixture.json"
    _make_tags_db(
        tags_path,
        [
            (KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0),
            (KORVOLD_SLUG, "Bloodghast", "Top Cards", 1.0),
            (KORVOLD_SLUG, "Imaginary Synergy Piece", "Creatures", 0.5),
        ],
    )
    _write_fixture(fixture_path, [KORVOLD])
    return {
        "db": synergy_db,
        "tags": tags_path,
        "fixture": fixture_path,
        "history": tmp_path / "forensics_history.csv",
        "output": tmp_path / "report.md",
    }


# ---------------------------------------------------------------------------
# Schema discipline — sibling tuple, never shared with history.CSV_FIELDS
# ---------------------------------------------------------------------------


class TestSchemaDiscipline:
    def test_sibling_field_tuple_not_shared(self) -> None:
        assert FORENSICS_CSV_FIELDS != CSV_FIELDS
        # Not an extension of the audit-history schema either: the
        # strict-header readers on both sides must never see each
        # other's columns.
        assert FORENSICS_CSV_FIELDS[: len(CSV_FIELDS)] != CSV_FIELDS
        assert len(FORENSICS_CSV_FIELDS) == 16


# ---------------------------------------------------------------------------
# Append discipline — header once, append-only
# ---------------------------------------------------------------------------


class TestAppendDiscipline:
    def test_first_run_creates_header_second_appends(self, paths: dict[str, Path]) -> None:
        args = _args(paths["db"], paths["fixture"], paths["tags"], history=paths["history"], output=paths["output"])
        assert handle_forensics(args) == 0
        lines, rows = _read_csv(paths["history"])
        assert lines[0] == ",".join(FORENSICS_CSV_FIELDS)
        assert len(rows) == 1

        assert handle_forensics(args) == 0
        lines, rows = _read_csv(paths["history"])
        assert len(rows) == 2
        # Header exactly once.
        assert sum(1 for line in lines if line == ",".join(FORENSICS_CSV_FIELDS)) == 1
        # The reader round-trips both rows.
        assert len(read_last_forensics(20, path=paths["history"])) == 2


# ---------------------------------------------------------------------------
# Row contents — gem rate, digests, proportions, provenance
# ---------------------------------------------------------------------------


class TestRowContents:
    def test_gem_rate_digests_and_proportions(self, paths: dict[str, Path]) -> None:
        args = _args(paths["db"], paths["fixture"], paths["tags"], history=paths["history"], output=paths["output"])
        assert handle_forensics(args) == 0
        _lines, rows = _read_csv(paths["history"])
        (row,) = rows

        # gem_rate_forensics populated (HS data exists for Korvold).
        assert row["gem_rate_forensics"] != ""
        assert 0.0 <= float(row["gem_rate_forensics"]) <= 1.0

        # Provenance: config_hash is the live one; fixture_sha256 is the
        # hashlib digest over the fixture FILE BYTES; the snapshot digest
        # matches a direct recompute on the same tags.db.
        assert row["config_hash"] == compute_config_hash()
        assert row["fixture_sha256"] == hashlib.sha256(paths["fixture"].read_bytes()).hexdigest()
        tags_conn = sqlite3.connect(paths["tags"])
        try:
            assert row["edhrec_snapshot_digest"] == edhrec_snapshot_digest(tags_conn)
        finally:
            tags_conn.close()
        assert row["synergy_db_digest"] != ""
        assert row["timestamp"] != ""

        # Bucket proportions are fractions of total misses summing to
        # 1.0 (the one miss is the unmatched EDHREC-only name → DATA_GAP).
        fractions = [float(row[c]) for c in ("near_miss", "outranked", "filtered", "data_gap", "no_rules")]
        assert sum(fractions) == pytest.approx(1.0)
        assert float(row["data_gap"]) == pytest.approx(1.0)

        assert row["n_commanders"] == "1"
        assert row["n_skipped"] == "0"

    def test_gem_rate_empty_for_none(self, synergy_db: Path, tmp_path: Path) -> None:
        """No High-Synergy section rows → HS reference is None for every
        commander → gem_rate_forensics is the empty-string sentinel."""
        tags_path = tmp_path / "tags.db"
        fixture_path = tmp_path / "fixture.json"
        _make_tags_db(
            tags_path,
            [
                (KORVOLD_SLUG, "Bloodghast", "Top Cards", 1.0),
                (KORVOLD_SLUG, "Imaginary Synergy Piece", "Creatures", 0.5),
            ],
        )
        _write_fixture(fixture_path, [KORVOLD])
        history = tmp_path / "hist.csv"
        args = _args(synergy_db, fixture_path, tags_path, history=history, output=tmp_path / "report.md")
        assert handle_forensics(args) == 0
        _lines, rows = _read_csv(history)
        assert rows[0]["gem_rate_forensics"] == ""
        # Reader promotes the empty cell back to None.
        assert read_last_forensics(1, path=history)[0].gem_rate_forensics is None


# ---------------------------------------------------------------------------
# Digest helpers (direct)
# ---------------------------------------------------------------------------


class TestDigests:
    def test_snapshot_digest_changes_when_row_added(self, tmp_path: Path) -> None:
        tags_path = tmp_path / "tags.db"
        _make_tags_db(tags_path, [(KORVOLD_SLUG, "Phyrexian Altar", "High Synergy Cards", 2.0)])
        conn = sqlite3.connect(tags_path)
        try:
            before = edhrec_snapshot_digest(conn)
            # Exact Key-Decision derivation: SHA-256 over "{COUNT}:{MAX(rowid)}".
            assert before == hashlib.sha256(b"1:1").hexdigest()
            conn.execute(
                "INSERT INTO edhrec_card_synergy VALUES (?, ?, ?, ?)",
                (KORVOLD_SLUG, "Bloodghast", "Top Cards", 1.0),
            )
            conn.commit()
            after = edhrec_snapshot_digest(conn)
        finally:
            conn.close()
        assert after != before
        assert after == hashlib.sha256(b"2:2").hexdigest()

    def test_fixture_sha256_matches_hashlib_over_file_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "fixture.json"
        target.write_bytes(b'{"entries": []}')
        assert fixture_file_sha256(target) == hashlib.sha256(b'{"entries": []}').hexdigest()


# ---------------------------------------------------------------------------
# Failure paths — no row on reconciliation failure; unwritable path degrades
# ---------------------------------------------------------------------------


class TestFailurePaths:
    def test_no_row_when_reconciliation_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from mtg_synergy_graph.bench import forensics_report

        def _boom(**_kwargs: object) -> None:
            raise ForensicsReconciliationError("planted reconciliation failure")

        monkeypatch.setattr(forensics_report, "compute_forensics", _boom)
        history = tmp_path / "hist.csv"
        args = _args(
            tmp_path / "synergy.db",
            tmp_path / "fixture.json",
            tmp_path / "tags.db",
            history=history,
            output=tmp_path / "report.md",
        )
        rc = handle_forensics(args)
        assert rc == 2
        assert "planted reconciliation failure" in capsys.readouterr().err
        assert not history.exists()

    def test_unwritable_history_path_warns_and_exits_0(
        self,
        paths: dict[str, Path],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A file where the history's parent DIRECTORY should be →
        mkdir raises OSError → stderr warning, exit still 0."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        history = blocker / "hist.csv"
        args = _args(paths["db"], paths["fixture"], paths["tags"], history=history, output=paths["output"])
        rc = handle_forensics(args)
        assert rc == 0
        err = capsys.readouterr().err
        assert "failed to append history row" in err
        assert not history.exists()
        # The report itself still landed.
        assert paths["output"].exists()


# ---------------------------------------------------------------------------
# Reader — malformed-row tolerance
# ---------------------------------------------------------------------------


class TestReader:
    def test_malformed_rows_skipped_with_warning(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        history = tmp_path / "hist.csv"
        good_1 = _row_cells(ts="2026-06-10T00:00:00+00:00")
        good_2 = _row_cells(ts="2026-06-10T01:00:00+00:00", ndcg="0.110000")
        bad_field_count = ["only", "two"]
        bad_numeric = _row_cells(ts="2026-06-10T02:00:00+00:00", ndcg="not-a-float")
        _write_history_csv(history, [good_1, bad_field_count, bad_numeric, good_2])

        rows = read_last_forensics(20, path=history)
        assert [r.timestamp for r in rows] == [good_1[0], good_2[0]]
        err = capsys.readouterr().err
        assert "malformed" in err
        # Remaining rows still render through the trend handler.
        rc = handle_trend_forensics(_trend_args(history, fmt="md"))
        assert rc == 0
        out = capsys.readouterr().out
        assert good_1[0] in out
        assert good_2[0] in out

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_last_forensics(20, path=tmp_path / "nope.csv") == []


# ---------------------------------------------------------------------------
# --trend forensics — boundary markers + within-group deltas
# ---------------------------------------------------------------------------


class TestTrendForensics:
    @staticmethod
    def _three_rows(history: Path) -> None:
        """Two same-group rows, then a row with a NEW snapshot digest."""
        _write_history_csv(
            history,
            [
                _row_cells(ts="2026-06-10T00:00:00+00:00", ndcg="0.100000", gem="0.300000"),
                _row_cells(ts="2026-06-10T01:00:00+00:00", ndcg="0.120000", gem="0.350000"),
                _row_cells(ts="2026-06-11T00:00:00+00:00", snap="snap2", ndcg="0.200000", gem="0.400000"),
            ],
        )

    def test_md_boundary_marker_and_deltas(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        history = tmp_path / "hist.csv"
        self._three_rows(history)
        rc = handle_trend_forensics(_trend_args(history, fmt="md"))
        assert rc == 0
        out = capsys.readouterr().out
        # Exactly one boundary marker between snap1 and snap2 rows.
        assert out.count(BOUNDARY_MARKER) == 1
        # Within-group deltas present (row 2 vs row 1).
        assert "+0.020000" in out
        assert "+0.050000" in out
        # No delta computed ACROSS the boundary (0.200000 - 0.120000).
        assert "+0.080000" not in out
        # The post-boundary row carries em-dash deltas.
        snap2_line = next(line for line in out.splitlines() if "snap2" in line)
        assert snap2_line.rstrip().endswith("| — | — |")
        # Marker sits between the snap1 rows and the snap2 row.
        lines = out.splitlines()
        assert lines.index(next(line for line in lines if BOUNDARY_MARKER in line)) < lines.index(snap2_line)

    def test_config_hash_change_also_inserts_boundary(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        history = tmp_path / "hist.csv"
        _write_history_csv(
            history,
            [
                _row_cells(ts="2026-06-10T00:00:00+00:00", config="cfg1", ndcg="0.100000"),
                _row_cells(ts="2026-06-10T01:00:00+00:00", config="cfg2", ndcg="0.150000"),
            ],
        )
        rc = handle_trend_forensics(_trend_args(history, fmt="md"))
        assert rc == 0
        out = capsys.readouterr().out
        assert out.count(BOUNDARY_MARKER) == 1
        assert "+0.050000" not in out

    def test_json_deltas_and_boundary_entries(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        history = tmp_path / "hist.csv"
        self._three_rows(history)
        rc = handle_trend_forensics(_trend_args(history, fmt="json"))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 4  # 3 rows + 1 boundary
        assert payload[0]["ndcg_delta"] is None
        assert payload[1]["ndcg_delta"] == pytest.approx(0.02)
        assert payload[1]["gem_rate_delta"] == pytest.approx(0.05)
        assert payload[2] == {"boundary": True, "marker": BOUNDARY_MARKER}
        assert payload[3]["ndcg_delta"] is None
        assert payload[3]["edhrec_snapshot_digest"] == "snap2"

    def test_duplicate_same_config_rows_accepted(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Repeated identical runs share a group: no boundary, 0 deltas."""
        history = tmp_path / "hist.csv"
        _write_history_csv(history, [_row_cells(), _row_cells()])
        rc = handle_trend_forensics(_trend_args(history, fmt="json"))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload) == 2
        assert payload[1]["ndcg_delta"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CLI wiring — --trend forensics dispatch, csv format, missing file
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_trend_forensics_format_csv_works(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
        from mtg_synergy_graph.bench.cli import main

        history = tmp_path / "hist.csv"
        _write_history_csv(history, [_row_cells()])
        rc = main(["audit", "--trend", "forensics", "--format", "csv", "--forensics-history", str(history)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == ",".join(FORENSICS_CSV_FIELDS)
        assert "cfg1" in out

    def test_missing_history_advisory_exit_0(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
        from mtg_synergy_graph.bench.cli import main

        rc = main(["audit", "--trend", "forensics", "--forensics-history", str(tmp_path / "missing.csv")])
        assert rc == 0
        cap = capsys.readouterr()
        assert "no forensics history yet" in cap.err
        assert cap.out == ""

    def test_default_format_is_md(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
        from mtg_synergy_graph.bench.cli import main

        history = tmp_path / "hist.csv"
        _write_history_csv(history, [_row_cells()])
        rc = main(["audit", "--trend", "forensics", "--forensics-history", str(history)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("| timestamp |")

    def test_forensics_history_flag_warns_without_forensics_modes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
        from mtg_synergy_graph.bench.cli import main

        # --trend hidden_gems with a non-default --forensics-history →
        # companion-flag warning; missing hidden_gems history still
        # exits 0 with its own advisory.
        rc = main(
            [
                "audit",
                "--trend",
                "hidden_gems",
                "--history",
                str(tmp_path / "history.csv"),
                "--forensics-history",
                str(tmp_path / "fh.csv"),
            ]
        )
        assert rc == 0
        err = capsys.readouterr().err
        assert "--forensics-history has no effect" in err
