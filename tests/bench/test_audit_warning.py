"""FR4 stderr warning behavior in ``handle_audit`` (Unit 3 of plan 003).

Mocks ``run_audit`` to return a pre-built ``AuditReport`` so we can
test the warning-print code path without touching a real DB / fixture.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import mtg_synergy_graph.bench.audit as audit_module
from mtg_synergy_graph.bench.audit import handle_audit
from mtg_synergy_graph.bench.fixture import IdentityReport
from mtg_synergy_graph.bench.histogram import Histogram, Verdict
from mtg_synergy_graph.bench.report import AuditReport


def _make_report(
    *,
    delta: float | None,
    aggregate_live: float | None,
    aggregate_pinned: float | None,
    warning: bool,
    is_identical: bool = True,
) -> AuditReport:
    """Build a synthetic ``AuditReport`` with preset hidden-gem fields."""
    identity = IdentityReport()  # empty = identical
    if not is_identical:
        identity.missing_commanders = ["X"]
    return AuditReport(
        fixture_path="baseline.json",
        pinned_config_hash="abc",
        live_config_hash="abc",
        aggregate_score_delta=0.0,
        commanders_compared=100,
        identity_report=identity,
        histogram=Histogram(samples={}),
        verdict=Verdict.TRIVIAL,
        aggregate_hidden_gem_hit_rate=aggregate_live,
        pinned_hidden_gem_hit_rate=aggregate_pinned,
        hidden_gem_hit_rate_delta=delta,
        hidden_gem_warning=warning,
        commanders_with_edhrec=97 if aggregate_live is not None else 0,
    )


def _write_dummy_fixture(fixture_path: Path) -> None:
    """Write a minimal fixture file with one entry so ``handle_audit``
    passes its existence + non-empty checks.
    """
    fixture_path.write_text(
        '{"schema_version": 1, "config_hash": "abc", "created_at": "t", '
        '"entries": [{"commander": "X", "scores": {"Y": 1.0}}]}'
    )


def _args_for(fixture_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        fixture=str(fixture_path),
        db="ignored.db",
        format="md",
        output=None,
    )


# ---------------------------------------------------------------------------
# Warning path
# ---------------------------------------------------------------------------


def test_warning_prints_to_stderr_when_delta_below_threshold(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delta = -0.025 → stderr contains the FR4 warning line."""
    fixture_path = tmp_path / "baseline.json"
    _write_dummy_fixture(fixture_path)

    report = _make_report(
        delta=-0.025,
        aggregate_live=0.113,
        aggregate_pinned=0.138,
        warning=True,
    )
    monkeypatch.setattr(audit_module, "run_audit", lambda _db, _fx, edhrec_db=None: report)

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        exit_code = handle_audit(_args_for(fixture_path))
    finally:
        os.chdir(old_cwd)

    captured = capsys.readouterr()
    assert exit_code == 0  # identical report → exit 0
    # Warning body
    assert "hidden_gem_hit_rate dropped" in captured.err
    assert "--inspect-gems" in captured.err
    # Summary still appears
    assert "gem_Δ" in captured.err
    assert "WARN" in captured.err


def test_warning_format_uses_abs_delta_and_3_decimals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delta shown as absolute value with 3 decimals; pinned + live too."""
    fixture_path = tmp_path / "baseline.json"
    _write_dummy_fixture(fixture_path)

    report = _make_report(
        delta=-0.034,
        aggregate_live=0.113,
        aggregate_pinned=0.147,
        warning=True,
    )
    monkeypatch.setattr(audit_module, "run_audit", lambda _db, _fx, edhrec_db=None: report)

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        handle_audit(_args_for(fixture_path))
    finally:
        os.chdir(old_cwd)

    captured = capsys.readouterr()
    # abs(delta) = 0.034
    assert "0.034" in captured.err
    # Pinned (0.147) and live (0.113) to 3 decimals
    assert "0.147" in captured.err
    assert "0.113" in captured.err


# ---------------------------------------------------------------------------
# No-warning path
# ---------------------------------------------------------------------------


def test_no_warning_when_delta_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delta = 0 → stderr has summary but no warning line."""
    fixture_path = tmp_path / "baseline.json"
    _write_dummy_fixture(fixture_path)

    report = _make_report(
        delta=0.0,
        aggregate_live=0.15,
        aggregate_pinned=0.15,
        warning=False,
    )
    monkeypatch.setattr(audit_module, "run_audit", lambda _db, _fx, edhrec_db=None: report)

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        handle_audit(_args_for(fixture_path))
    finally:
        os.chdir(old_cwd)

    captured = capsys.readouterr()
    assert "hidden_gem_hit_rate dropped" not in captured.err
    assert "--inspect-gems" not in captured.err
    # Summary still carries gem_Δ marker
    assert "gem_Δ" in captured.err
    assert "WARN" not in captured.err


def test_no_warning_when_aggregate_none(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No EDHREC data → warning never fires, summary uses em-dash."""
    fixture_path = tmp_path / "baseline.json"
    _write_dummy_fixture(fixture_path)

    report = _make_report(
        delta=None,
        aggregate_live=None,
        aggregate_pinned=None,
        warning=False,
    )
    monkeypatch.setattr(audit_module, "run_audit", lambda _db, _fx, edhrec_db=None: report)

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        handle_audit(_args_for(fixture_path))
    finally:
        os.chdir(old_cwd)

    captured = capsys.readouterr()
    assert "hidden_gem_hit_rate dropped" not in captured.err
    assert "gem_Δ=—" in captured.err


# ---------------------------------------------------------------------------
# Idempotency — warning printed exactly once per run
# ---------------------------------------------------------------------------


def test_warning_printed_exactly_once_per_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sinks (markdown file + stdout print) must not duplicate warning."""
    fixture_path = tmp_path / "baseline.json"
    _write_dummy_fixture(fixture_path)

    report = _make_report(
        delta=-0.050,
        aggregate_live=0.100,
        aggregate_pinned=0.150,
        warning=True,
    )
    monkeypatch.setattr(audit_module, "run_audit", lambda _db, _fx, edhrec_db=None: report)

    import os

    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        handle_audit(_args_for(fixture_path))
    finally:
        os.chdir(old_cwd)

    captured = capsys.readouterr()
    # Count the warning-header occurrences in stderr.
    assert captured.err.count("hidden_gem_hit_rate dropped") == 1
