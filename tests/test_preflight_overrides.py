"""Tests for the append-only CSV writers in
``mtg_synergy_graph.preflight.overrides``.

Both writers are deterministic given the input and the audit_dir
override, so these tests exercise schema correctness, header-on-
first-write behavior, and append semantics.
"""

from __future__ import annotations

import csv
from pathlib import Path

from mtg_synergy_graph.preflight.overrides import (
    PREFLIGHT_OVERRIDES_FIELDS,
    WALKER_OUTCOMES_FIELDS,
    record_force_override,
    record_walker_outcome,
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open() as fh:
        return list(csv.DictReader(fh))


def test_walker_outcomes_writes_header_on_first_write(tmp_path):
    record_walker_outcome(
        gap_id="trigger.X[*]",
        verdict_severity="WARN",
        verdict_reason="FIXTURE_BLIND_SPOT: 0/5",
        attempted=True,
        post_scaffold_outcome="passed",
        audit_dir=tmp_path,
    )
    rows = _read_csv(tmp_path / "walker_outcomes.csv")
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == set(WALKER_OUTCOMES_FIELDS)
    assert row["gap_id"] == "trigger.X[*]"
    assert row["verdict"] == "WARN"
    assert row["verdict_reason"] == "FIXTURE_BLIND_SPOT: 0/5"
    assert row["attempted"] == "true"
    assert row["post_scaffold_outcome"] == "passed"
    # timestamp is populated and ISO-8601-ish.
    assert "T" in row["timestamp"]
    assert row["timestamp"].endswith("+00:00") or row["timestamp"].endswith("Z")


def test_walker_outcomes_appends_subsequent_rows_without_extra_header(tmp_path):
    """Second invocation appends a row, not a duplicate header."""
    record_walker_outcome(
        gap_id="a",
        verdict_severity="WARN",
        verdict_reason="r1",
        attempted=True,
        post_scaffold_outcome="passed",
        audit_dir=tmp_path,
    )
    record_walker_outcome(
        gap_id="b",
        verdict_severity="REJECT",
        verdict_reason="r2",
        attempted=False,
        post_scaffold_outcome="not_attempted",
        audit_dir=tmp_path,
    )
    rows = _read_csv(tmp_path / "walker_outcomes.csv")
    assert len(rows) == 2
    assert [r["gap_id"] for r in rows] == ["a", "b"]
    assert rows[1]["attempted"] == "false"


def test_force_override_writes_header_with_reason(tmp_path):
    record_force_override(
        gap_id="trigger.X[*]",
        gate_name="stage_a",
        verdict_severity="WARN",
        reason="Iroas/Losheel are mech-irrelevant for this rule",
        audit_dir=tmp_path,
    )
    rows = _read_csv(tmp_path / "preflight_overrides.csv")
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == set(PREFLIGHT_OVERRIDES_FIELDS)
    assert row["gap_id"] == "trigger.X[*]"
    assert row["gate_name"] == "stage_a"
    assert row["verdict"] == "WARN"
    assert row["reason"] == "Iroas/Losheel are mech-irrelevant for this rule"


def test_force_override_appends_multiple_rows(tmp_path):
    for i in range(3):
        record_force_override(
            gap_id=f"g{i}",
            gate_name="stage_a",
            verdict_severity="WARN",
            reason=f"reason {i}",
            audit_dir=tmp_path,
        )
    rows = _read_csv(tmp_path / "preflight_overrides.csv")
    assert len(rows) == 3
    assert [r["reason"] for r in rows] == ["reason 0", "reason 1", "reason 2"]


def test_walker_outcomes_creates_audit_dir_if_missing(tmp_path):
    nested = tmp_path / "sub" / "audit"
    assert not nested.exists()
    record_walker_outcome(
        gap_id="g",
        verdict_severity="WARN",
        verdict_reason="r",
        attempted=True,
        post_scaffold_outcome="reverted",
        audit_dir=nested,
    )
    assert (nested / "walker_outcomes.csv").exists()
