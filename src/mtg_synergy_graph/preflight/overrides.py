"""Append-only CSV writers for pre-flight observability artifacts.

Two artifacts:

- ``.audit/walker_outcomes.csv`` — one row per walker iteration that
  hit a non-PASS pre-flight verdict, recording whether the walker
  attempted the candidate and (when attempted) the post-scaffold
  outcome. v1.0 schema is single-row per attempt; v1.5 may upgrade
  to two-phase write (see plan v1.5 Plan Trigger section).

- ``.audit/preflight_overrides.csv`` — one row per ``--force``
  invocation, recording the operator's explicit decision to bypass
  a WARN verdict. Reason text is required.

Both are append-only CSV with a stable header that's written on
first append. Both are gitignored (consistent with
``.audit/optimize_history.csv`` precedent). Retention is unbounded
because the calibration corpus value compounds with size.

This module deliberately knows nothing about the walker's flow
control — it just provides the writers. The walker (or any other
consumer) calls these to log the events it cares about.
"""

from __future__ import annotations

import csv
import datetime
from pathlib import Path

#: Project-root-relative location of the .audit/ directory.
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent.parent / ".audit"

WALKER_OUTCOMES_FIELDS: tuple[str, ...] = (
    "timestamp",
    "gap_id",
    "verdict",
    "verdict_reason",
    "attempted",
    "post_scaffold_outcome",
)

PREFLIGHT_OVERRIDES_FIELDS: tuple[str, ...] = (
    "timestamp",
    "gap_id",
    "gate_name",
    "verdict",
    "reason",
)


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _append_row(
    path: Path,
    fields: tuple[str, ...],
    row: dict[str, object],
) -> None:
    """Append ``row`` as a CSV record. Writes the header on first write.

    Caller-side schema discipline: ``row`` keys must match ``fields``
    exactly. Missing keys default to empty string; unknown keys are
    dropped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def record_walker_outcome(
    *,
    gap_id: str,
    verdict_severity: str,
    verdict_reason: str,
    attempted: bool,
    post_scaffold_outcome: str,
    audit_dir: Path | None = None,
) -> None:
    """Append one row to ``.audit/walker_outcomes.csv``.

    Called by ``scripts/scaffold_rule.py --walk`` after each iteration
    that hit a non-PASS pre-flight verdict. ``post_scaffold_outcome``
    is the string outcome from ``_attempt_one`` (one of ``passed``,
    ``reverted``, ``trivial``, ``marginal``, ``skipped``) or
    ``"not_attempted"`` when the walker chose to skip.
    """
    audit_dir = audit_dir or DEFAULT_AUDIT_DIR
    _append_row(
        audit_dir / "walker_outcomes.csv",
        WALKER_OUTCOMES_FIELDS,
        {
            "timestamp": _now_iso(),
            "gap_id": gap_id,
            "verdict": verdict_severity,
            "verdict_reason": verdict_reason,
            "attempted": "true" if attempted else "false",
            "post_scaffold_outcome": post_scaffold_outcome,
        },
    )


def record_force_override(
    *,
    gap_id: str,
    gate_name: str,
    verdict_severity: str,
    reason: str,
    audit_dir: Path | None = None,
) -> None:
    """Append one row to ``.audit/preflight_overrides.csv``.

    Called when an operator passes ``--force`` to bypass a WARN
    verdict. ``reason`` is the operator-supplied justification (the
    walker's ``--force`` flag is documented as requiring a separate
    reason on the command line, but this module does not enforce that
    — caller is responsible for prompting and validating the reason).
    """
    audit_dir = audit_dir or DEFAULT_AUDIT_DIR
    _append_row(
        audit_dir / "preflight_overrides.csv",
        PREFLIGHT_OVERRIDES_FIELDS,
        {
            "timestamp": _now_iso(),
            "gap_id": gap_id,
            "gate_name": gate_name,
            "verdict": verdict_severity,
            "reason": reason,
        },
    )
