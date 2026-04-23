"""Tests for ``.audit/history.csv`` append-only log (Unit 5 of hidden-gem plan).

Covers the read / write surface of ``bench.history``:

* ``append_run`` writes a header on first call, appends data-only rows
  afterwards, tolerates missing git checkouts, and never raises on I/O
  failure.
* ``read_last`` returns the last N rows, skips malformed lines with a
  stderr warning, and returns [] when the file doesn't exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import history as bench_history
from mtg_synergy_graph.bench.fixture import IdentityReport
from mtg_synergy_graph.bench.histogram import Histogram, Verdict
from mtg_synergy_graph.bench.report import AuditReport

# Capture the real ``_commit_sha`` before the autouse stub below replaces
# the module attribute. Tests that want to exercise the real function
# reinstate this reference via ``monkeypatch.setattr`` on the module.
_REAL_COMMIT_SHA = bench_history._commit_sha


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_report(
    *,
    live_config_hash: str = "abc1234567890def",
    aggregate_score_delta: float = 0.0,
    aggregate_hidden_gem_hit_rate: float | None = 0.1467,
    hidden_gem_hit_rate_delta: float | None = -0.0034,
    commanders_compared: int = 100,
    commanders_with_edhrec: int = 97,
    verdict: Verdict = Verdict.TRIVIAL,
) -> AuditReport:
    """Synthesize an ``AuditReport`` with just the fields ``append_run`` reads."""
    identity = IdentityReport(
        config_hash_mismatch=None,
        missing_commanders=[],
        score_mismatches=[],
    )
    return AuditReport(
        fixture_path="baseline.json",
        pinned_config_hash="pinned_hash",
        live_config_hash=live_config_hash,
        aggregate_score_delta=aggregate_score_delta,
        commanders_compared=commanders_compared,
        identity_report=identity,
        histogram=Histogram(samples={}),
        verdict=verdict,
        aggregate_hidden_gem_hit_rate=aggregate_hidden_gem_hit_rate,
        pinned_hidden_gem_hit_rate=0.15,
        hidden_gem_hit_rate_delta=hidden_gem_hit_rate_delta,
        hidden_gem_warning=False,
        commanders_with_edhrec=commanders_with_edhrec,
    )


@pytest.fixture(autouse=True)
def _stub_commit_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a stable hash so tests 1-3 don't depend on the real git state."""
    monkeypatch.setattr(bench_history, "_commit_sha", lambda: "deadbeefcafe")


# ---------------------------------------------------------------------------
# append_run
# ---------------------------------------------------------------------------


def test_append_run_writes_header_and_row_on_empty_dir(tmp_path: Path) -> None:
    """First ``append_run`` on a fresh dir writes header + data row."""
    path = tmp_path / ".audit" / "history.csv"
    report = _make_report()

    bench_history.append_run(report, path=path)

    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    header = lines[0].split(",")
    assert header == [
        "timestamp",
        "commit_sha",
        "config_hash",
        "aggregate_score_delta",
        "hidden_gem_hit_rate",
        "hidden_gem_hit_rate_delta",
        "commanders_compared",
        "commanders_with_edhrec",
        "verdict",
    ]
    # Data row contains our commit stub and config hash.
    assert "deadbeefcafe" in lines[1]
    assert "abc1234567890def" in lines[1]


def test_append_run_second_call_appends_only_data(tmp_path: Path) -> None:
    """Subsequent ``append_run`` calls must not re-emit the header."""
    path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(), path=path)
    bench_history.append_run(_make_report(live_config_hash="second_hash"), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # header + 2 rows
    assert lines[0].startswith("timestamp,")
    assert "abc1234567890def" in lines[1]
    assert "second_hash" in lines[2]


def test_append_run_empty_file_triggers_header_write(tmp_path: Path) -> None:
    """An existing but empty file should trigger a header write."""
    path = tmp_path / ".audit" / "history.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # size 0

    bench_history.append_run(_make_report(), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("timestamp,")
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# read_last
# ---------------------------------------------------------------------------


def test_read_last_returns_newest_rows_in_file_order(tmp_path: Path) -> None:
    """``read_last(3)`` on a 10-row file returns rows 8, 9, 10 (chronological)."""
    path = tmp_path / ".audit" / "history.csv"
    for i in range(10):
        bench_history.append_run(
            _make_report(live_config_hash=f"hash_{i:02d}"),
            path=path,
        )

    rows = bench_history.read_last(3, path=path)
    assert len(rows) == 3
    assert [r.config_hash for r in rows] == ["hash_07", "hash_08", "hash_09"]


def test_read_last_returns_all_when_n_exceeds_file(tmp_path: Path) -> None:
    """``read_last(n)`` with ``n > file_length`` returns all rows (no padding)."""
    path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="only"), path=path)

    rows = bench_history.read_last(20, path=path)
    assert len(rows) == 1
    assert rows[0].config_hash == "only"


def test_read_last_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """``read_last`` on a missing file returns ``[]``."""
    path = tmp_path / ".audit" / "nope.csv"
    rows = bench_history.read_last(5, path=path)
    assert rows == []


def test_read_last_skips_malformed_lines_with_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A row with the wrong field count is skipped, but other rows survive."""
    path = tmp_path / ".audit" / "history.csv"
    # Two valid rows + one malformed between them.
    bench_history.append_run(_make_report(live_config_hash="good_a"), path=path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("garbage_row_with,only_two_fields\n")
    bench_history.append_run(_make_report(live_config_hash="good_b"), path=path)

    rows = bench_history.read_last(10, path=path)
    hashes = [r.config_hash for r in rows]
    assert "good_a" in hashes
    assert "good_b" in hashes
    err = capsys.readouterr().err
    assert "skipping" in err.lower() or "malformed" in err.lower() or "bad" in err.lower()


# ---------------------------------------------------------------------------
# Optional-field handling
# ---------------------------------------------------------------------------


def test_append_run_none_gem_rate_writes_empty_string(tmp_path: Path) -> None:
    """``aggregate_hidden_gem_hit_rate is None`` → CSV column is empty (not "None")."""
    path = tmp_path / ".audit" / "history.csv"
    report = _make_report(
        aggregate_hidden_gem_hit_rate=None,
        hidden_gem_hit_rate_delta=None,
        commanders_with_edhrec=0,
    )
    bench_history.append_run(report, path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    data_row = lines[1].split(",")
    # Find the position by parsing the header.
    header = lines[0].split(",")
    gem_rate_idx = header.index("hidden_gem_hit_rate")
    gem_delta_idx = header.index("hidden_gem_hit_rate_delta")
    assert data_row[gem_rate_idx] == ""
    assert data_row[gem_delta_idx] == ""
    # Not the literal string "None".
    assert "None" not in data_row


# ---------------------------------------------------------------------------
# Git-commit handling
# ---------------------------------------------------------------------------


def test_commit_sha_empty_when_git_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_commit_sha`` returns "" if subprocess.run raises FileNotFoundError
    (git binary missing) or CalledProcessError (not a git checkout)."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("no git")

    # Reinstate the real function so we exercise it instead of the autouse stub.
    monkeypatch.setattr(bench_history, "_commit_sha", _REAL_COMMIT_SHA)
    monkeypatch.setattr(subprocess, "run", _boom)

    assert bench_history._commit_sha() == ""


def test_commit_sha_empty_on_non_git_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the real ``_commit_sha`` in a non-git directory → empty string."""
    monkeypatch.setattr(bench_history, "_commit_sha", _REAL_COMMIT_SHA)
    monkeypatch.chdir(tmp_path)
    sha = bench_history._commit_sha()
    assert sha == ""


def test_append_run_with_empty_commit_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row shape is stable when commit_sha is empty — other fields still present."""
    monkeypatch.setattr(bench_history, "_commit_sha", lambda: "")
    path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(), path=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    data = lines[1].split(",")
    assert len(data) == len(header)
    commit_idx = header.index("commit_sha")
    assert data[commit_idx] == ""
    # Other fields still populated.
    config_idx = header.index("config_hash")
    assert data[config_idx] == "abc1234567890def"


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


def test_append_run_never_raises_on_io_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """I/O failure logs a stderr warning but does NOT raise."""
    # Point at a path that can never be written (parent doesn't exist and
    # we sabotage mkdir).
    bad_path = Path("/nonexistent/root/impossible/history.csv")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(Path, "mkdir", _boom)
    # Should not raise.
    bench_history.append_run(_make_report(), path=bad_path)

    err = capsys.readouterr().err
    # A warning is emitted.
    assert "history" in err.lower() or "warn" in err.lower()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_fields(tmp_path: Path) -> None:
    """Write then read: every numeric field round-trips as expected."""
    path = tmp_path / ".audit" / "history.csv"
    report = _make_report(
        live_config_hash="roundtrip_hash",
        aggregate_score_delta=1.234567,
        aggregate_hidden_gem_hit_rate=0.555,
        hidden_gem_hit_rate_delta=-0.025,
        commanders_compared=100,
        commanders_with_edhrec=95,
        verdict=Verdict.POSITIVE,
    )
    bench_history.append_run(report, path=path)

    rows = bench_history.read_last(1, path=path)
    assert len(rows) == 1
    row = rows[0]
    assert row.commit_sha == "deadbeefcafe"
    assert row.config_hash == "roundtrip_hash"
    assert row.aggregate_score_delta == pytest.approx(1.234567, rel=1e-5)
    assert row.hidden_gem_hit_rate == pytest.approx(0.555, rel=1e-5)
    assert row.hidden_gem_hit_rate_delta == pytest.approx(-0.025, rel=1e-5)
    assert row.commanders_compared == 100
    assert row.commanders_with_edhrec == 95
    assert row.verdict == "positive"
