"""Tests for the Stage A pre-flight integration in scripts/gap_report.py.

Covers (a) the new ``_evaluate_preflight`` helper, (b) the band-sorted
``_format_report`` output, and (c) preservation of the existing
Python-module import surface (GapStat, RuleProposal, etc.) the walker
relies on.
"""

from __future__ import annotations

import sqlite3

import gap_report
from gap_report import (
    GapStat,
    RuleProposal,
    _commander_names,
    _evaluate_preflight,
    _format_report,
    _propose,
    _scan_universe,
)

# ---------------------------------------------------------------------------
# Import-surface preservation (Unit 2 must not rename or remove these
# symbols; the walker imports them directly per scaffold_rule.py:50-56.)
# ---------------------------------------------------------------------------


def test_walker_import_surface_preserved():
    """All five symbols the walker imports must remain accessible."""
    assert callable(_commander_names)
    assert callable(_propose)
    assert callable(_scan_universe)
    assert GapStat is not None
    assert RuleProposal is not None
    # Module attribute the walker mutates via _refresh_registry.
    assert hasattr(gap_report, "RULE_GATES")


# ---------------------------------------------------------------------------
# _evaluate_preflight helper
# ---------------------------------------------------------------------------


def _make_db_with_minimal_schema(rows: list[dict]) -> sqlite3.Connection:
    """Build an in-memory DB matching the minimum cards/card_ports schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE cards (name TEXT PRIMARY KEY, types TEXT, color_identity TEXT, "
        "card_types TEXT, legal_commander INTEGER DEFAULT 1)"
    )
    conn.execute(
        """CREATE TABLE card_ports (
            id INTEGER PRIMARY KEY, card_name TEXT, port_type TEXT,
            event_class TEXT, valid_filter TEXT, zone_origin TEXT,
            zone_destination TEXT, replacement_result TEXT
        )"""
    )
    for row in rows:
        conn.execute(
            "INSERT INTO cards (name, types, card_types) VALUES (?, ?, ?)",
            (row["name"], "Legendary Creature", "Creature"),
        )
        for port in row.get("ports", []):
            conn.execute(
                "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, "
                "zone_origin, zone_destination, replacement_result) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["name"],
                    port.get("port_type", ""),
                    port.get("event_class", ""),
                    port.get("valid_filter", ""),
                    port.get("zone_origin", ""),
                    port.get("zone_destination", ""),
                    port.get("replacement_result", ""),
                ),
            )
    conn.commit()
    return conn


def _make_proposal(signature: tuple[str, str, str], template: str = "test") -> RuleProposal:
    """Construct a minimal RuleProposal carrying just the fields needed
    by _format_report and _evaluate_preflight (signature + template)."""
    gap = GapStat(
        signature=signature,
        commanders=10,
        activations=5,
        exemplars=("Exemplar A",),
        top_rules=(),
    )
    return RuleProposal(
        gap=gap,
        template=template,
        rationale=f"test rationale for {signature}",
        gate_sketch="test_gate_sketch",
        tier_sketches=(),
        pool_sizes={},
    )


def test_evaluate_preflight_returns_verdict_per_unique_signature(monkeypatch, tmp_path):
    """One verdict per unique signature; duplicate signatures dedupe."""
    conn = _make_db_with_minimal_schema(
        [{"name": "X", "ports": [{"port_type": "trigger", "event_class": "DiesThisTurn"}]}]
    )
    # Point preflight at a tiny test fixture so it doesn't load the real 500.
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"entries": [{"commander": "X"}]}')
    monkeypatch.setattr("mtg_synergy_graph.preflight.gates.DEFAULT_FIXTURE_PATH", fixture)

    p1 = _make_proposal(("trigger", "DiesThisTurn", ""))
    p2 = _make_proposal(("trigger", "DiesThisTurn", ""))  # same signature
    p3 = _make_proposal(("replacement", "DamageDone", "Prevent"))
    verdicts = _evaluate_preflight([p1, p2, p3], conn)
    # Two unique signatures -> two verdicts (the duplicate dedupes).
    assert len(verdicts) == 2
    assert ("trigger", "DiesThisTurn", "") in verdicts
    assert ("replacement", "DamageDone", "Prevent") in verdicts


# ---------------------------------------------------------------------------
# _format_report band-sorted output
# ---------------------------------------------------------------------------


def test_format_report_emits_three_band_sections_with_counts():
    """Mixed verdicts -> three band sections with explicit counts."""
    pass_prop = _make_proposal(("trigger", "Pass", ""))
    warn_prop = _make_proposal(("trigger", "Warn", ""))
    reject_prop = _make_proposal(("trigger", "Reject", ""))

    from mtg_synergy_graph.preflight import (
        Candidate,
        GateVerdict,
        PipelineVerdict,
        Severity,
    )

    def _vd(prop: RuleProposal, severity: Severity, reason: str) -> PipelineVerdict:
        gate = GateVerdict(name="stage_a", severity=severity, reason=reason)
        return PipelineVerdict(
            candidate=Candidate(signature=prop.gap.signature, gap_id="test"),
            severity=severity,
            gates=(gate,),
        )

    verdicts = {
        pass_prop.gap.signature: _vd(pass_prop, Severity.PASS, "PASS: 3 fixture commanders"),
        warn_prop.gap.signature: _vd(warn_prop, Severity.WARN, "FIXTURE_BLIND_SPOT: 0 fixture, 5 legal"),
        reject_prop.gap.signature: _vd(reject_prop, Severity.REJECT, "UNTESTABLE: 0/0"),
    }

    report = _format_report(
        [pass_prop, warn_prop, reject_prop],
        stats_total=10,
        eligible_total=3,
        verdicts=verdicts,
    )

    # Band headers exist with correct counts.
    assert "### PASS (1 entries)" in report
    assert "### WARN (1 entries)" in report
    assert "### REJECT (1 entries)" in report

    # Pre-flight summary line at top of ranked proposals section.
    assert "Pre-flight summary**: 1 PASS · 1 WARN · 1 REJECT" in report

    # Band ordering: PASS section appears before WARN, WARN before REJECT.
    pass_idx = report.find("### PASS")
    warn_idx = report.find("### WARN")
    reject_idx = report.find("### REJECT")
    assert pass_idx < warn_idx < reject_idx

    # Per-entry Pre-flight line carries the verdict reason.
    assert "**Pre-flight**: PASS — PASS: 3 fixture commanders" in report
    assert "**Pre-flight**: WARN — FIXTURE_BLIND_SPOT" in report
    assert "**Pre-flight**: REJECT — UNTESTABLE" in report


def test_format_report_handles_all_pass_no_warn_no_reject():
    """All-PASS scenario still emits empty WARN and REJECT sections."""
    p = _make_proposal(("trigger", "OnlyPass", ""))
    from mtg_synergy_graph.preflight import (
        Candidate,
        GateVerdict,
        PipelineVerdict,
        Severity,
    )

    verdicts = {
        p.gap.signature: PipelineVerdict(
            candidate=Candidate(signature=p.gap.signature, gap_id="x"),
            severity=Severity.PASS,
            gates=(GateVerdict(name="stage_a", severity=Severity.PASS, reason="PASS"),),
        )
    }
    report = _format_report([p], stats_total=1, eligible_total=1, verdicts=verdicts)
    assert "### PASS (1 entries)" in report
    assert "### WARN (0 entries)" in report
    assert "### REJECT (0 entries)" in report
    # Empty sections explicitly say "(none)".
    warn_section = report.split("### WARN (0 entries)")[1].split("### REJECT")[0]
    assert "(none)" in warn_section


def test_format_report_unevaluated_band_appears_when_verdicts_dict_partial():
    """Proposals without a verdict land in an UNEVALUATED section."""
    p_known = _make_proposal(("trigger", "Known", ""))
    p_unknown = _make_proposal(("trigger", "Unknown", ""))
    from mtg_synergy_graph.preflight import (
        Candidate,
        GateVerdict,
        PipelineVerdict,
        Severity,
    )

    verdicts = {
        p_known.gap.signature: PipelineVerdict(
            candidate=Candidate(signature=p_known.gap.signature, gap_id="k"),
            severity=Severity.PASS,
            gates=(GateVerdict(name="stage_a", severity=Severity.PASS, reason="PASS"),),
        )
    }
    report = _format_report([p_known, p_unknown], stats_total=2, eligible_total=2, verdicts=verdicts)
    assert "### UNEVALUATED (1 entries)" in report
    assert "1 unevaluated" in report


def test_format_report_no_verdicts_falls_back_to_unevaluated():
    """When verdicts dict is None or empty, all proposals -> UNEVALUATED."""
    p = _make_proposal(("trigger", "X", ""))
    report = _format_report([p], stats_total=1, eligible_total=1, verdicts=None)
    assert "### UNEVALUATED (1 entries)" in report
    assert "### PASS (0 entries)" in report


def test_format_report_entries_within_band_preserve_input_order():
    """Within a band, the order of the input proposals is preserved
    (which the caller is expected to have sorted by impact desc)."""
    p_high = _make_proposal(("trigger", "High", ""))
    p_low = _make_proposal(("trigger", "Low", ""))
    from mtg_synergy_graph.preflight import (
        Candidate,
        GateVerdict,
        PipelineVerdict,
        Severity,
    )

    def _pass_vd(p: RuleProposal) -> PipelineVerdict:
        return PipelineVerdict(
            candidate=Candidate(signature=p.gap.signature, gap_id="x"),
            severity=Severity.PASS,
            gates=(GateVerdict(name="stage_a", severity=Severity.PASS, reason="PASS"),),
        )

    verdicts = {p_high.gap.signature: _pass_vd(p_high), p_low.gap.signature: _pass_vd(p_low)}
    report = _format_report([p_high, p_low], stats_total=2, eligible_total=2, verdicts=verdicts)
    high_idx = report.find("trigger.High")
    low_idx = report.find("trigger.Low")
    assert high_idx < low_idx
