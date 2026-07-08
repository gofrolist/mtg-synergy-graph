import types

from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import _compute_deltas, run_gate


class _FakeEngine:
    """Minimal engine: every page is empty (no earned candidates), so
    ``run_gate`` can be exercised without a real scored DB."""

    def __init__(self) -> None:
        self._score_cache: dict = {}

    def page(self, commanders, *, offset, limit):
        return types.SimpleNamespace(items=[])


def test_compute_deltas_vs_baseline():
    baseline = {
        "A": CoverageMetrics(0, 0, 0),
        "B": CoverageMetrics(10, 50, 3),
    }
    live = {
        "A": CoverageMetrics(7, 40, 2),  # +7
        "B": CoverageMetrics(9, 48, 3),  # -1 regression
    }
    deltas, mean = _compute_deltas(live, baseline)
    assert deltas == {"A": 7, "B": -1}
    assert mean == 3.0


def test_compute_deltas_warns_on_names_absent_from_baseline(caplog):
    import logging

    baseline = {"A": CoverageMetrics(5, 0, 0)}
    live = {"A": CoverageMetrics(6, 0, 0), "NewCmdr": CoverageMetrics(9, 0, 0)}
    with caplog.at_level(logging.WARNING):
        deltas, _ = _compute_deltas(live, baseline)
    assert deltas == {"A": 1}  # NewCmdr silently un-differenceable...
    assert "NewCmdr" in caplog.text  # ...but the drop is warned, not silent


def test_run_gate_diffs_cohort_and_control_against_preloaded_baseline():
    # run_gate takes an already-loaded, already-staleness-validated baseline
    # (staleness is _cmd_gate's job, tested DB-free in test_coverage_cli.py).
    baseline = {
        "A": CoverageMetrics(0, 0, 0),
        "B": CoverageMetrics(0, 0, 0),
        "C": CoverageMetrics(0, 0, 0),
    }
    result = run_gate(
        _FakeEngine(),
        conn=None,  # unused: run_census gets an explicit commander list
        cohort_names=["A"],
        baseline=baseline,
        control_size=2,
        seed=17,
    )
    # Fake engine yields empty pages -> earned_top30 0 for all; deltas 0.
    assert result.cohort_deltas == {"A": 0}
    assert result.cohort_delta_mean == 0.0
    # Control is drawn from baseline minus the cohort {A} -> {B, C}.
    assert set(result.control_deltas) == {"B", "C"}
