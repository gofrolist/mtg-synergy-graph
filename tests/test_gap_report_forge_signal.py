"""Integration tests for ``scripts/gap_report.py`` forge_signal wiring.

Focused on the re-ranking semantics: the introduction of ``forge_signal``
must never change gap_report's output when the sidecar is absent, and
must reorder gaps when the sidecar provides stronger signal for some
subkinds than others.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 6.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# Make scripts/ importable for tests that want to drive the internals.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import gap_report as gr  # noqa: E402


def _make_gap(pt: str, ev: str, sub: str, commanders: int, activations: int) -> gr.GapStat:
    return gr.GapStat(
        signature=(pt, ev, sub),
        commanders=commanders,
        activations=activations,
        exemplars=(),
        top_rules=(),
    )


# ---------------------------------------------------------------------------
# GapStat contract — forge_signal default + weighted_impact
# ---------------------------------------------------------------------------


def test_gapstat_default_forge_signal_is_neutral() -> None:
    """Existing gap_report tests (zero regression)."""
    g = _make_gap("effect", "Token", "", 10, 2)
    assert g.forge_signal == pytest.approx(1.0)
    assert g.weighted_impact == pytest.approx(g.impact)


def test_gapstat_weighted_impact_is_impact_times_signal() -> None:
    g = _make_gap("effect", "Token", "", 10, 0)  # impact = 10
    g = dataclasses.replace(g, forge_signal=1.3)
    assert g.weighted_impact == pytest.approx(13.0)


def test_gapstat_forge_signal_out_of_plan_range_still_accepted() -> None:
    """The dataclass doesn't validate; gap_weight caps values. Ensure the
    dataclass won't crash if a downstream change ever emits a value outside
    [1.0, 1.5]."""
    g = dataclasses.replace(_make_gap("effect", "Token", "", 10, 0), forge_signal=0.7)
    assert g.weighted_impact == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Sorting — forge_signal changes top-ranked gap
# ---------------------------------------------------------------------------


def test_forge_signal_can_flip_top_ranking() -> None:
    """Gap A: impact=10, forge_signal=1.0.  Gap B: impact=8, forge_signal=1.4.
    Unweighted: A > B. Weighted: B=11.2 > A=10 → B wins.
    """
    a = dataclasses.replace(_make_gap("eff", "A", "", 10, 0), forge_signal=1.0)
    b = dataclasses.replace(_make_gap("eff", "B", "", 8, 0), forge_signal=1.4)
    gaps = [a, b]
    gaps.sort(key=lambda s: (-s.weighted_impact, -s.commanders, s.signature))
    assert gaps[0].signature == ("eff", "B", "")


def test_equal_signals_preserve_volume_order() -> None:
    """When every gap has forge_signal=1.0, sort matches pre-Unit-6 order."""
    a = _make_gap("eff", "A", "", 10, 0)
    b = _make_gap("eff", "B", "", 5, 0)
    gaps = sorted([a, b], key=lambda s: (-s.weighted_impact, -s.commanders, s.signature))
    assert [g.signature for g in gaps] == [("eff", "A", ""), ("eff", "B", "")]


# ---------------------------------------------------------------------------
# Format report — forge_signal line appears only when != 1.0
# ---------------------------------------------------------------------------


def _fake_proposal(gap: gr.GapStat) -> gr.RuleProposal:
    return gr.RuleProposal(
        gap=gap,
        template="placeholder",
        rationale="test",
        gate_sketch="(port) -> True",
        tier_sketches=(),
        pool_sizes={},
    )


def test_report_omits_forge_signal_line_when_neutral() -> None:
    gap = _make_gap("effect", "Token", "", 12, 4)
    report = gr._format_report([_fake_proposal(gap)], stats_total=1, eligible_total=1)
    assert "forge_signal" not in report
    assert "Impact" in report


def test_report_includes_forge_signal_line_when_boosted() -> None:
    gap = dataclasses.replace(_make_gap("effect", "Token", "", 12, 4), forge_signal=1.4)
    report = gr._format_report([_fake_proposal(gap)], stats_total=1, eligible_total=1)
    assert "forge_signal" in report
    assert "1.40" in report
