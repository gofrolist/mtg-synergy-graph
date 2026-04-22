"""Unit 1 — CLI skeleton + schema migration tests.

Exercises the argparse dispatcher and the schema addition for the
``rule_contributions`` table. Each subcommand mode is routed to its stub
and confirmed to raise ``NotImplementedError`` — later units will replace
the stubs with real implementations, at which point these tests break
deliberately and are replaced by the implementation's own tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.bench import cli as bench_cli
from mtg_synergy_graph.db import open_db

# ---------------------------------------------------------------------------
# Argparse + dispatch
# ---------------------------------------------------------------------------


def test_audit_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``bench.py audit --help`` prints usage and exits 0.

    Proves the argparse tree is well-formed and every flag is discoverable.
    """
    with pytest.raises(SystemExit) as exc_info:
        bench_cli.main(["audit", "--help"])
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    # Every mode flag is advertised in --help output.
    for flag in ("--rule", "--inspect", "--collinearity", "--repin", "--expect-identity"):
        assert flag in out, f"missing {flag} from --help output"
    # Shared flags too.
    for flag in ("--commander", "--limit", "--format", "--output", "--yes", "--db", "--fixture"):
        assert flag in out, f"missing {flag} from --help output"


def test_invalid_format_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Edge case: ``--format bogus`` is rejected by argparse."""
    with pytest.raises(SystemExit) as exc_info:
        bench_cli.main(["audit", "--format", "bogus"])
    # argparse exits with code 2 on usage errors.
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--format" in err


def test_mutually_exclusive_modes_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """Two mode flags at once (e.g., --repin + --expect-identity) are refused."""
    with pytest.raises(SystemExit) as exc_info:
        bench_cli.main(["audit", "--repin", "--expect-identity"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed" in err or "mutually exclusive" in err


# ---------------------------------------------------------------------------
# Stubs raise NotImplementedError
# ---------------------------------------------------------------------------


def test_unregistered_mode_still_raises_via_stub_fallback() -> None:
    """Defense in depth: if a future refactor forgets to register a mode,
    the stub table fallback surfaces a loud NotImplementedError instead
    of silently no-oping.

    Exercised by directly reaching into the handler table and swapping a
    mode back to its stub, then invoking that mode.
    """
    from mtg_synergy_graph.bench import _stubs as stubs

    original = bench_cli._HANDLERS["rule"]
    try:
        bench_cli._HANDLERS["rule"] = stubs.rule_stub
        with pytest.raises(NotImplementedError):
            bench_cli.main(["audit", "--rule", "x"])
    finally:
        bench_cli._HANDLERS["rule"] = original


def test_register_overrides_stub() -> None:
    """``register()`` lets later units swap in real handlers.

    Also guards against typos: registering under an unknown mode raises KeyError.
    """
    called: list[str] = []

    def fake_handler(args: object) -> int:
        called.append("ok")
        return 0

    original = bench_cli._HANDLERS["audit"]
    try:
        bench_cli.register("audit", fake_handler)
        exit_code = bench_cli.main(["audit"])
        assert exit_code == 0
        assert called == ["ok"]
    finally:
        bench_cli.register("audit", original)

    with pytest.raises(KeyError):
        bench_cli.register("nonexistent_mode", fake_handler)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_rule_contributions_table_created(tmp_path: Path) -> None:
    """Fresh DB via ``open_db()`` creates ``rule_contributions`` with the
    documented schema: 8 columns + composite primary key + 2 indexes.
    """
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        cols = conn.execute("PRAGMA table_info(rule_contributions)").fetchall()
        col_names = {row["name"] for row in cols}
        assert col_names == {
            "commander",
            "candidate",
            "rule_id",
            "contribution",
            "idf_weight",
            "raw_count",
            "config_hash",
            "computed_at",
        }

        # Primary key components (pk > 0 means participates; pk value orders them).
        pk_cols = sorted(
            ((row["pk"], row["name"]) for row in cols if row["pk"] > 0),
            key=lambda pair: pair[0],
        )
        assert [name for _, name in pk_cols] == [
            "commander",
            "candidate",
            "rule_id",
            "config_hash",
        ]

        # Index list: the two named indexes exist.
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(rule_contributions)")}
        assert "idx_rule_contributions_rule" in indexes
        assert "idx_rule_contributions_cmdr_hash" in indexes
    finally:
        conn.close()


def test_table_creation_is_idempotent(tmp_path: Path) -> None:
    """Re-opening the DB must not fail on existing table.

    Catches regressions where a CREATE INDEX forgets ``IF NOT EXISTS``.
    """
    db_path = tmp_path / "synergy.db"
    conn1 = open_db(db_path)
    conn1.execute(
        "INSERT INTO rule_contributions VALUES (?,?,?,?,?,?,?,?)",
        ("Korvold", "Bloodghast", "test_rule", 0.1, 0.2, 1, "abc123", "2026-04-22T00:00:00"),
    )
    conn1.commit()
    conn1.close()

    # Re-open should preserve rows, not crash.
    conn2 = open_db(db_path)
    try:
        row = conn2.execute("SELECT commander, rule_id FROM rule_contributions").fetchone()
        assert row is not None
        assert row["commander"] == "Korvold"
        assert row["rule_id"] == "test_rule"
    finally:
        conn2.close()


def test_indexes_support_common_queries(tmp_path: Path) -> None:
    """Integration: insert ~100 rows, verify index-backed queries complete.

    Cheap sanity check — EXPLAIN QUERY PLAN should use the indexes for
    ``WHERE rule_id = ?`` and ``WHERE commander = ?``. If a future index
    rename breaks this, the regression is immediate.
    """
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        rows = [
            (f"Cmdr{i % 10}", f"Cand{i}", f"rule_{i % 5}", 0.01 * i, 0.1, 1, "hash1", "2026-04-22T00:00:00")
            for i in range(100)
        ]
        conn.executemany("INSERT INTO rule_contributions VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()

        # Query by rule should use the rule-index.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM rule_contributions WHERE rule_id = ? AND config_hash = ?",
            ("rule_1", "hash1"),
        ).fetchall()
        plan_text = " ".join(str(r[-1]) for r in plan)
        assert "idx_rule_contributions_rule" in plan_text

        # Query by commander should use the commander-index.
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM rule_contributions WHERE commander = ? AND config_hash = ?",
            ("Cmdr0", "hash1"),
        ).fetchall()
        plan_text = " ".join(str(r[-1]) for r in plan)
        # Either primary-key or named index is an acceptable outcome; at
        # least one of them must be present. (SQLite may pick the PK over
        # a secondary index depending on statistics.)
        assert "idx_rule_contributions_cmdr_hash" in plan_text or "USING PRIMARY KEY" in plan_text
    finally:
        conn.close()
