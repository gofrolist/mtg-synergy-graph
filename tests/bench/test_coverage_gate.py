from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    _compute_deltas,  # pure helper, unit-tested without an engine
    write_baseline,
)


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


def test_gate_flags_stale_baseline(tmp_path):
    path = tmp_path / "baseline.json"
    write_baseline(path, {"A": CoverageMetrics(0, 0, 0)}, config_hash="OLD")
    # run_gate is called with a mismatching live hash -> stale, no scoring.
    from mtg_synergy_graph.bench.coverage_report import run_gate

    res = run_gate(
        engine=None,
        conn=None,
        baseline_path=path,
        cohort_names=["A"],
        live_config_hash="NEW",
    )
    assert res.stale_baseline is True
