"""Append-only ``.audit/history.csv`` log of ``bench.py audit`` runs.

Each successful ``bench.py audit`` run adds one row so an operator (or
``bench.py audit --trend hidden_gems``) can review longitudinal trends
in the hidden-gem metric alongside the existing aggregate score delta
and verdict.

Design invariants
-----------------
* **Append-only.** Never rewritten; the header is emitted once on first
  write and preserved on every subsequent append.
* **No third-party deps.** Uses the stdlib ``csv`` module so the file
  stays git-diffable / human-readable.
* **Never crashes the audit.** I/O failures degrade to a stderr warning;
  the caller (:mod:`bench.audit`) wraps ``append_run`` in try/except
  belt-and-suspenders in case a future refactor tightens this.
* **Gitignored via ``.audit/``.** Matches the existing pre-commit hook
  convention — see ``.gitignore`` at repo root.

CSV schema (9 columns, header written once)::

    timestamp,commit_sha,config_hash,aggregate_score_delta,
    hidden_gem_hit_rate,hidden_gem_hit_rate_delta,
    commanders_compared,commanders_with_edhrec,verdict

Empty string is used as the sentinel for missing numeric data (e.g. a
``None`` ``hidden_gem_hit_rate`` before Unit 2 pins gem fields). The
``csv`` module parses empty strings as the empty string on read; the
``HistoryRow`` constructor promotes them back to ``None`` / defaults.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mtg_synergy_graph.bench.report import AuditReport

#: CSV column order. Must match ``HistoryRow`` field order. Touch both
#: together or the round-trip tests will catch the drift.
CSV_FIELDS: tuple[str, ...] = (
    "timestamp",
    "commit_sha",
    "config_hash",
    "aggregate_score_delta",
    "hidden_gem_hit_rate",
    "hidden_gem_hit_rate_delta",
    "commanders_compared",
    "commanders_with_edhrec",
    "verdict",
)

_DEFAULT_PATH = Path(".audit/history.csv")


@dataclass(frozen=True)
class HistoryRow:
    """One row of ``.audit/history.csv``, typed and immutable.

    Float fields are ``None`` when the CSV column is empty (e.g. an old
    run predating the gem metric). Integer fields default to ``0`` when
    blank rather than ``None`` — ``commanders_compared`` and
    ``commanders_with_edhrec`` are always computed from non-None counts
    by :class:`AuditReport`, so a blank here only happens on malformed
    input that ``read_last`` already skips.
    """

    timestamp: str
    commit_sha: str
    config_hash: str
    aggregate_score_delta: float | None
    hidden_gem_hit_rate: float | None
    hidden_gem_hit_rate_delta: float | None
    commanders_compared: int
    commanders_with_edhrec: int
    verdict: str


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def append_run(
    report: AuditReport,
    path: str | Path = _DEFAULT_PATH,
) -> None:
    """Append one row summarizing ``report`` to the history CSV.

    Creates the parent directory (``.audit/``) and emits the header if
    the file does not exist or is empty. Any exception during write
    degrades to a stderr warning — a history write failure must never
    abort the primary audit output.
    """
    csv_path = Path(path)
    try:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = (not csv_path.exists()) or csv_path.stat().st_size == 0
        with csv_path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(CSV_FIELDS)
            writer.writerow(_row_from_report(report))
    except (OSError, csv.Error) as exc:
        # csv.Error covers NUL bytes / non-encodable characters in the
        # rendered cells; OSError covers disk failures, permission
        # errors, etc. Either way, the history write must never turn a
        # PASS audit into a failure — we degrade to a stderr warning
        # that includes the exception class for triage.
        print(
            f"bench.py audit: warning: failed to append history row to {csv_path}: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )


def _row_from_report(report: AuditReport) -> list[str]:
    """Render the ``AuditReport`` into the CSV's 9 string cells."""
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    return [
        timestamp,
        _commit_sha(),
        report.live_config_hash,
        _fmt_float(report.aggregate_score_delta),
        _fmt_float(report.aggregate_hidden_gem_hit_rate),
        _fmt_float(report.hidden_gem_hit_rate_delta),
        str(report.commanders_compared),
        str(report.commanders_with_edhrec),
        report.verdict.value,
    ]


def _fmt_float(value: float | None) -> str:
    """Render a float with fixed precision; ``None`` → empty string.

    Six decimals is enough to preserve NDCG-level deltas on playback;
    we never need more precision for a trend chart. Empty string
    (not ``"None"``, not ``"null"``) keeps the CSV compact and numeric-
    parser friendly downstream.
    """
    if value is None:
        return ""
    return f"{value:.6f}"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def read_last(n: int, path: str | Path = _DEFAULT_PATH) -> list[HistoryRow]:
    """Return the last ``n`` rows of ``path`` (chronological order).

    Returns ``[]`` when the file does not exist. Skips malformed rows
    (wrong field count, unparseable numbers) with a stderr warning so
    a single bad append can't break the whole trend command.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    if n <= 0:
        return []

    rows: list[HistoryRow] = []
    try:
        with csv_path.open(encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                return []
            if tuple(header) != CSV_FIELDS:
                print(
                    f"bench.py audit: warning: {csv_path} header mismatch; expected {CSV_FIELDS}, got {tuple(header)}",
                    file=sys.stderr,
                )
                return []
            for lineno, raw in enumerate(reader, start=2):
                parsed = _parse_row(raw, lineno=lineno, path=csv_path)
                if parsed is not None:
                    rows.append(parsed)
    except OSError as exc:
        print(
            f"bench.py audit: warning: failed to read history from {csv_path}: {exc}",
            file=sys.stderr,
        )
        return []

    if n >= len(rows):
        return rows
    return rows[-n:]


def _parse_row(raw: list[str], *, lineno: int, path: Path) -> HistoryRow | None:
    """Convert one raw CSV row to a ``HistoryRow``; skip + warn on error."""
    if len(raw) != len(CSV_FIELDS):
        print(
            f"bench.py audit: warning: skipping malformed row {lineno} in "
            f"{path}: expected {len(CSV_FIELDS)} fields, got {len(raw)}",
            file=sys.stderr,
        )
        return None
    try:
        return HistoryRow(
            timestamp=raw[0],
            commit_sha=raw[1],
            config_hash=raw[2],
            aggregate_score_delta=_parse_float(raw[3]),
            hidden_gem_hit_rate=_parse_float(raw[4]),
            hidden_gem_hit_rate_delta=_parse_float(raw[5]),
            commanders_compared=_parse_int(raw[6]),
            commanders_with_edhrec=_parse_int(raw[7]),
            verdict=raw[8],
        )
    except ValueError as exc:
        print(
            f"bench.py audit: warning: skipping bad row {lineno} in {path}: {exc}",
            file=sys.stderr,
        )
        return None


def _parse_float(value: str) -> float | None:
    if value == "":
        return None
    return float(value)


def _parse_int(value: str) -> int:
    if value == "":
        return 0
    return int(value)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def _commit_sha() -> str:
    """Return ``git rev-parse HEAD`` or an empty string.

    Empty string covers three failure modes: git binary missing
    (``FileNotFoundError``), not inside a git checkout (``CalledProcess
    Error``), and any other unexpected subprocess error. The CSV row
    shape stays stable either way — trend consumers see ``""`` as the
    "no git" sentinel.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — git is on PATH in every dev/CI env we ship
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


__all__ = [
    "CSV_FIELDS",
    "HistoryRow",
    "append_run",
    "read_last",
]
