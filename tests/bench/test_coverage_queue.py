from mtg_synergy_graph.bench.coverage import CoverageMetrics
from mtg_synergy_graph.bench.coverage_report import (
    poverty_queue,
    stratified_control,
)


def _metrics(n):
    return {f"C{i:03d}": CoverageMetrics(i % 31, i, i % 8) for i in range(n)}


def test_poverty_queue_ascending_by_earned():
    m = {
        "A": CoverageMetrics(5, 10, 2),
        "B": CoverageMetrics(0, 0, 0),
        "C": CoverageMetrics(5, 3, 1),
    }
    q = poverty_queue(m)
    assert q[0] == ("B", 0)
    # Tie at earned=5 broken by name.
    assert q[1] == ("A", 5)
    assert q[2] == ("C", 5)


def test_stratified_control_deterministic_and_excludes_cohort():
    m = _metrics(500)
    cohort = {"C000", "C001", "C002"}
    a = stratified_control(m, exclude=cohort, size=200, seed=17)
    b = stratified_control(m, exclude=cohort, size=200, seed=17)
    assert a == b  # deterministic
    assert len(a) == 200
    assert not (set(a) & cohort)  # excludes cohort


def test_stratified_control_caps_at_available():
    m = _metrics(50)
    ctrl = stratified_control(m, exclude=set(), size=200, seed=17)
    assert len(ctrl) == 50  # cannot exceed the pool


def test_stratified_control_represents_every_band():
    # 10 earned-bands (0, 3, 6, ... 27), 30 commanders each = 300 pool.
    # Round-robin sampling must surface every band so a regression concentrated
    # in any single band stays visible in the control (not drowned by band 0).
    m = {f"b{b}_c{i:02d}": CoverageMetrics(b * 3, 0, 0) for b in range(10) for i in range(30)}
    ctrl = stratified_control(m, exclude=set(), size=50, seed=17)
    assert len(ctrl) == 50
    bands = {min(m[name].earned_top30 // 3, 9) for name in ctrl}
    assert bands == set(range(10))  # all 10 bands represented
