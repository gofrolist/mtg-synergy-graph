"""Unit 6 — ``--rule`` / ``--inspect`` / ``--collinearity`` tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import cli as bench_cli
from mtg_synergy_graph.bench.collinearity import compute_collinearity
from mtg_synergy_graph.bench.rule_ops import ablate_rule, inspect_rule
from mtg_synergy_graph.bench.tensor import compute_config_hash
from mtg_synergy_graph.db import open_db


def _seed_tensor(conn: sqlite3.Connection, rows: list[tuple[str, str, str, float]]) -> str:
    """Insert ``(commander, candidate, rule_id, contribution)`` rows under
    the current config_hash. Helper for every Unit 6 test.
    """
    h = compute_config_hash()
    conn.executemany(
        "INSERT INTO rule_contributions "
        "(commander, candidate, rule_id, contribution, idf_weight, raw_count, "
        "config_hash, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(c, cand, rid, contrib, 0.1, 1, h, "2026-04-22T00:00:00") for c, cand, rid, contrib in rows],
    )
    conn.commit()
    return h


@pytest.fixture()
def tensor_db(tmp_path: Path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "synergy.db")
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# ablate_rule
# ---------------------------------------------------------------------------


def test_ablate_rule_returns_none_on_empty_tensor(tensor_db: sqlite3.Connection) -> None:
    """No rows for that rule → None (ambiguous: never-fired or stale)."""
    assert ablate_rule(tensor_db, "nonexistent") is None


def test_ablate_rule_aggregates_per_commander(tensor_db: sqlite3.Connection) -> None:
    """Sums contributions per commander, returns top-20 by |aggregate|."""
    _seed_tensor(
        tensor_db,
        [
            ("Korvold", "Bloodghast", "r1", 0.3),
            ("Korvold", "Mayhem Devil", "r1", 0.5),
            ("Muldrotha", "Bloodghast", "r1", 0.1),
            ("Muldrotha", "Other", "r2", 0.2),  # different rule, ignored
        ],
    )
    summary = ablate_rule(tensor_db, "r1")
    assert summary is not None
    assert summary.rule_id == "r1"
    assert summary.commanders_affected == 2
    # Two DISTINCT candidates (Bloodghast, Mayhem Devil) — Bloodghast
    # appears for both commanders but dedups in COUNT(DISTINCT candidate).
    assert summary.candidates_affected == 2
    assert summary.aggregate_contribution_removed == pytest.approx(0.9)
    # Korvold has the largest |aggregate|.
    assert summary.per_commander_removed[0][0] == "Korvold"
    assert summary.per_commander_removed[0][1] == pytest.approx(0.8)


def test_ablate_rule_filters_by_config_hash(tensor_db: sqlite3.Connection) -> None:
    """Rows under a stale hash are ignored."""
    # Row under live hash.
    _seed_tensor(tensor_db, [("C", "A", "r1", 0.5)])
    # Row under a bogus stale hash.
    tensor_db.execute(
        "INSERT INTO rule_contributions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("C", "B", "r1", 10.0, 0.1, 1, "stale_hash", "2026-01-01T00:00:00"),
    )
    tensor_db.commit()

    summary = ablate_rule(tensor_db, "r1")
    assert summary is not None
    # Only the row under the current config_hash counts.
    assert summary.aggregate_contribution_removed == pytest.approx(0.5)
    assert summary.candidates_affected == 1


# ---------------------------------------------------------------------------
# inspect_rule
# ---------------------------------------------------------------------------


def test_inspect_rule_returns_rows_sorted_by_abs_contribution(
    tensor_db: sqlite3.Connection,
) -> None:
    """Output order is |contribution| desc; negative magnitudes rank."""
    _seed_tensor(
        tensor_db,
        [
            ("C", "A", "r1", 0.1),
            ("C", "B", "r1", -0.5),  # largest magnitude
            ("C", "D", "r1", 0.3),
        ],
    )
    rows = inspect_rule(tensor_db, "r1")
    assert [r.candidate for r in rows] == ["B", "D", "A"]


def test_inspect_rule_honors_limit(tensor_db: sqlite3.Connection) -> None:
    _seed_tensor(
        tensor_db,
        [(f"C{i}", f"cand_{i}", "r1", 0.01 * i) for i in range(1, 20)],
    )
    rows = inspect_rule(tensor_db, "r1", limit=5)
    assert len(rows) == 5


def test_inspect_rule_filters_by_commander(tensor_db: sqlite3.Connection) -> None:
    _seed_tensor(
        tensor_db,
        [
            ("Korvold", "A", "r1", 0.5),
            ("Muldrotha", "A", "r1", 0.4),
        ],
    )
    rows = inspect_rule(tensor_db, "r1", commander="Korvold")
    assert len(rows) == 1
    assert rows[0].commander == "Korvold"


def test_inspect_rule_empty_result_is_empty_list(
    tensor_db: sqlite3.Connection,
) -> None:
    """No rows → empty list (not None), so callers can iterate cleanly."""
    assert inspect_rule(tensor_db, "missing") == []


# ---------------------------------------------------------------------------
# compute_collinearity
# ---------------------------------------------------------------------------


def test_collinearity_empty_tensor(tensor_db: sqlite3.Connection) -> None:
    """No rows → empty report, no crash."""
    report = compute_collinearity(tensor_db)
    assert report.rules_examined == 0
    assert report.pairs_flagged == ()


def test_collinearity_single_rule_no_pairs(tensor_db: sqlite3.Connection) -> None:
    """One rule + sufficient variance → no pairs (nothing to compare)."""
    _seed_tensor(
        tensor_db,
        [(f"C{i}", f"cand_{i}", "r1", 0.1 * i) for i in range(1, 10)],
    )
    report = compute_collinearity(tensor_db)
    assert report.rules_examined == 1
    assert report.pairs_flagged == ()


def test_collinearity_flags_perfectly_correlated_rules(
    tensor_db: sqlite3.Connection,
) -> None:
    """Two rules with identical activation vectors → flagged (r=1)."""
    rows: list[tuple[str, str, str, float]] = []
    for i in range(1, 20):
        rows.append((f"C{i}", f"cand_{i}", "r_a", 0.1 * i))
        rows.append((f"C{i}", f"cand_{i}", "r_b", 0.1 * i))  # identical
        rows.append((f"C{i}", f"cand_{i}", "r_c", 0.2 * i - 0.05))  # different shape
    _seed_tensor(tensor_db, rows)
    report = compute_collinearity(tensor_db)

    flagged_ids = {(p.rule_a, p.rule_b) for p in report.pairs_flagged}
    # r_a vs r_b pair should be flagged as collinear.
    assert ("r_a", "r_b") in flagged_ids
    # The top-flagged pair has Pearson r very close to 1.
    top = report.pairs_flagged[0]
    assert top.pearson_r == pytest.approx(1.0, abs=1e-6)


def test_collinearity_drops_zero_variance_rules(
    tensor_db: sqlite3.Connection,
) -> None:
    """A rule whose activations are all identical has no correlation signal
    and must be dropped from the pairwise comparison cleanly."""
    rows: list[tuple[str, str, str, float]] = []
    # Varying rule.
    for i in range(1, 10):
        rows.append((f"C{i}", f"cand_{i}", "r_var", 0.1 * i))
    # Zero-variance rule: same contribution everywhere.
    for i in range(1, 10):
        rows.append((f"C{i}", f"cand_{i}", "r_flat", 0.5))
    _seed_tensor(tensor_db, rows)
    report = compute_collinearity(tensor_db)
    assert "r_flat" in report.rules_dropped


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_rule_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        _seed_tensor(
            conn,
            [("Korvold", "Bloodghast", "r1", 0.3), ("Muldrotha", "Other", "r1", 0.1)],
        )
    finally:
        conn.close()

    exit_code = bench_cli.main(["audit", "--rule", "r1", "--db", str(db_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "bench.py audit --rule r1" in out
    assert "Korvold" in out


def test_cli_rule_missing_rule_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "synergy.db"
    open_db(db_path).close()  # create empty DB

    exit_code = bench_cli.main(["audit", "--rule", "nonexistent", "--db", str(db_path)])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "no tensor rows" in err


def test_cli_inspect_outputs_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        _seed_tensor(
            conn,
            [("Korvold", "Bloodghast", "r1", 0.3), ("Korvold", "Mayhem Devil", "r1", 0.5)],
        )
    finally:
        conn.close()

    exit_code = bench_cli.main(["audit", "--inspect", "r1", "--limit", "10", "--db", str(db_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "bench.py audit --inspect r1" in out
    assert "Korvold" in out
    assert "Mayhem Devil" in out


def test_cli_collinearity_outputs_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        rows: list[tuple[str, str, str, float]] = []
        for i in range(1, 20):
            rows.append((f"C{i}", f"cand_{i}", "r_a", 0.1 * i))
            rows.append((f"C{i}", f"cand_{i}", "r_b", 0.1 * i))
        _seed_tensor(conn, rows)
    finally:
        conn.close()

    exit_code = bench_cli.main(["audit", "--collinearity", "--db", str(db_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "bench.py audit --collinearity" in out
    assert "r_a" in out and "r_b" in out
