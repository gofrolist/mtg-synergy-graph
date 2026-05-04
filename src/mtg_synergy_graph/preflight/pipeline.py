"""Pipeline orchestrator for the pre-flight gate stack.

Combines individual gate verdicts into a single ``PipelineVerdict`` per
candidate. v1.0 wires only Stage A; v1.5 will add Stages B and C as
sibling gates.

Two entry points:

- ``evaluate_one(candidate, conn)`` — single candidate, eager.
- ``evaluate_all(candidates, conn)`` — iterable of candidates, lazy
  (yields one verdict at a time so consumers can stream results).

Both are pure functions: no shared state across calls, no caching that
crosses consumer boundaries. The pipeline is deterministic given the
same DB state.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path

from .gates import stage_a_golden_coverage
from .types import Candidate, PipelineVerdict, Severity


def evaluate_one(
    candidate: Candidate,
    conn: sqlite3.Connection,
    *,
    fixture_path: Path | None = None,
) -> PipelineVerdict:
    """Run all active gates against ``candidate`` and combine verdicts.

    v1.0 invokes only ``stage_a_golden_coverage``. The gate-list is
    forward-compatible: when v1.5 lands, additional gates slot in here
    without changing the consumer-side contract.
    """
    stage_a_verdict = stage_a_golden_coverage(candidate, conn, fixture_path=fixture_path)
    gates = (stage_a_verdict,)
    severity = max(
        (g.severity for g in gates),
        key=lambda s: s.value,
        default=Severity.PASS,
    )
    return PipelineVerdict(candidate=candidate, severity=severity, gates=gates)


def evaluate_all(
    candidates: Iterable[Candidate],
    conn: sqlite3.Connection,
    *,
    fixture_path: Path | None = None,
) -> Iterator[PipelineVerdict]:
    """Run ``evaluate_one`` over each candidate.

    Sequential by design — concurrent invocation is not supported in v1.0
    (and v1.5's ephemeral-registration mechanism would have its own
    thread-safety constraints).
    """
    for candidate in candidates:
        yield evaluate_one(candidate, conn, fixture_path=fixture_path)
