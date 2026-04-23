"""Tests for ``bench.py audit --trend hidden_gems`` (Unit 5 of hidden-gem plan).

Covers ``handle_trend_hidden_gems`` + CLI integration:

* Renders CSV (default), Markdown, JSON formats.
* Exits 0 with actionable stderr on a fresh checkout with no history.
* Dispatches via the existing register() table.
* ``--trend hidden_gems`` slots into the existing mode mutex group.
* ``--output PATH`` and ``--trend-n INT`` work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import handlers
from mtg_synergy_graph.bench import history as bench_history
from mtg_synergy_graph.bench.fixture import IdentityReport
from mtg_synergy_graph.bench.histogram import Histogram, Verdict
from mtg_synergy_graph.bench.report import AuditReport


def _make_report(live_config_hash: str = "abc1234567890def") -> AuditReport:
    identity = IdentityReport(
        config_hash_mismatch=None,
        missing_commanders=[],
        score_mismatches=[],
    )
    return AuditReport(
        fixture_path="baseline.json",
        pinned_config_hash="pinned_hash",
        live_config_hash=live_config_hash,
        aggregate_score_delta=0.0,
        commanders_compared=100,
        identity_report=identity,
        histogram=Histogram(samples={}),
        verdict=Verdict.TRIVIAL,
        aggregate_hidden_gem_hit_rate=0.1467,
        pinned_hidden_gem_hit_rate=0.15,
        hidden_gem_hit_rate_delta=-0.0034,
        hidden_gem_warning=False,
        commanders_with_edhrec=97,
    )


def _args(
    history_path: Path,
    *,
    trend: str = "hidden_gems",
    trend_n: int = 20,
    fmt: str = "csv",
    output: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        history=str(history_path),
        trend=trend,
        trend_n=trend_n,
        format=fmt,
        output=output,
    )


@pytest.fixture(autouse=True)
def _stub_commit_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_history, "_commit_sha", lambda: "deadbeef12345")


# ---------------------------------------------------------------------------
# Happy path — CSV (default)
# ---------------------------------------------------------------------------


def test_csv_format_shows_header_and_newest_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """5 rows in history + ``--trend-n 3 --format csv`` → header + 3 newest rows."""
    history_path = tmp_path / ".audit" / "history.csv"
    for i in range(5):
        bench_history.append_run(
            _make_report(live_config_hash=f"hash_{i:02d}"),
            path=history_path,
        )

    rc = handlers.handle_trend_hidden_gems(_args(history_path, trend_n=3, fmt="csv"))
    assert rc == 0
    out = capsys.readouterr().out
    # Header present.
    assert out.startswith("timestamp,")
    # 3 most-recent hashes appear; earliest two do not.
    assert "hash_02" in out
    assert "hash_03" in out
    assert "hash_04" in out
    assert "hash_00" not in out
    assert "hash_01" not in out


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_md_format_emits_markdown_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="md_hash"), path=history_path)

    rc = handlers.handle_trend_hidden_gems(_args(history_path, fmt="md"))
    assert rc == 0
    out = capsys.readouterr().out
    # Markdown table has pipe-delimited header row.
    assert "|" in out
    assert "timestamp" in out
    assert "md_hash" in out
    # Separator row with dashes.
    assert "---" in out


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_format_emits_valid_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="json_hash"), path=history_path)

    rc = handlers.handle_trend_hidden_gems(_args(history_path, fmt="json"))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert payload[0]["config_hash"] == "json_hash"


# ---------------------------------------------------------------------------
# Edge — no history yet
# ---------------------------------------------------------------------------


def test_missing_history_exits_0_with_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No ``.audit/history.csv`` → exit 0, stderr actionable message, empty stdout."""
    history_path = tmp_path / ".audit" / "history.csv"  # doesn't exist
    rc = handlers.handle_trend_hidden_gems(_args(history_path))
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "no history" in cap.err.lower()


# ---------------------------------------------------------------------------
# Edge — trend-n 0 in CSV format → header only
# ---------------------------------------------------------------------------


def test_trend_n_zero_yields_header_only_in_csv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="only"), path=history_path)

    rc = handlers.handle_trend_hidden_gems(_args(history_path, trend_n=0, fmt="csv"))
    assert rc == 0
    out = capsys.readouterr().out
    # CSV format: header present but no data rows.
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("timestamp,")
    assert "only" not in out


# ---------------------------------------------------------------------------
# --output PATH
# ---------------------------------------------------------------------------


def test_output_writes_to_file_with_stderr_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="output_hash"), path=history_path)

    out_path = tmp_path / "trend.csv"
    rc = handlers.handle_trend_hidden_gems(
        _args(history_path, output=str(out_path)),
    )
    assert rc == 0
    assert out_path.exists()
    content = out_path.read_text(encoding="utf-8")
    assert "output_hash" in content
    cap = capsys.readouterr()
    assert cap.out == ""
    assert str(out_path) in cap.err


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_cli_dispatch_routes_to_handle_trend(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main(["audit", "--trend", "hidden_gems"])`` routes via register()."""
    import mtg_synergy_graph.bench  # ensure registration  # noqa: F401
    from mtg_synergy_graph.bench.cli import main

    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(_make_report(live_config_hash="cli_hash"), path=history_path)

    rc = main(
        [
            "audit",
            "--trend",
            "hidden_gems",
            "--history",
            str(history_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "cli_hash" in out


def test_trend_mutually_exclusive_with_other_modes() -> None:
    """``--trend`` is in the mode mutex group → combining with another
    mode flag raises SystemExit from argparse."""
    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--trend", "hidden_gems", "--expect-identity"])


def test_trend_rejects_unknown_value() -> None:
    """``--trend bogus`` rejected by argparse choices."""
    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--trend", "bogus"])


# ---------------------------------------------------------------------------
# Integration: audit then trend
# ---------------------------------------------------------------------------


def test_audit_appends_and_trend_shows_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After ``bench.py audit`` runs, ``--trend`` shows the just-appended row.

    Simulated by directly invoking ``history.append_run`` (covered elsewhere
    end-to-end) then calling handle_trend.
    """
    history_path = tmp_path / ".audit" / "history.csv"
    bench_history.append_run(
        _make_report(live_config_hash="just_appended"),
        path=history_path,
    )

    rc = handlers.handle_trend_hidden_gems(_args(history_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "just_appended" in out
