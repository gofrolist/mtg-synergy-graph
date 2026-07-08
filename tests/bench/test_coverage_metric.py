from dataclasses import dataclass, field

from mtg_synergy_graph.bench.coverage import (
    CoverageMetrics,
    compute_coverage,
    is_earned,
)


@dataclass(frozen=True)
class _Rec:
    """Minimal Recommendation stand-in: compute_coverage only reads .scores."""

    scores: dict[str, float] = field(default_factory=dict)


def _staple_only():
    # Pure staple + additive terms only (rank/cmc/circuit are NOT in scores).
    return _Rec(scores={"staple": 0.01, "total": 0.037})


def _earned(bucket="port_match", val=1.5):
    return _Rec(scores={bucket: val, "staple": 0.01, "total": 1.55})


def test_is_earned_false_for_staple_only():
    assert is_earned(_staple_only()) is False


def test_is_earned_true_for_synergy_bucket():
    assert is_earned(_earned()) is True


def test_is_earned_false_when_only_non_rule_keys_present():
    assert is_earned(_Rec(scores={"embedding": 0.4, "total": 0.4})) is False


def test_is_earned_true_for_negative_anti_synergy_bucket():
    # A firing anti-synergy rule still means "the engine said something".
    assert is_earned(_Rec(scores={"port_match": -0.5, "total": -0.5})) is True


def test_earned_top30_counts_only_top_n():
    items = [_earned() for _ in range(5)] + [_staple_only() for _ in range(40)]
    m = compute_coverage(items, top_n=30)
    assert m.earned_top30 == 5
    assert m.n_scored_cands == 5


def test_earned_top30_caps_at_top_n_window():
    # 35 earned; only the first 30 count toward earned_top30, all 35 toward pool.
    items = [_earned() for _ in range(35)]
    m = compute_coverage(items, top_n=30)
    assert m.earned_top30 == 30
    assert m.n_scored_cands == 35


def test_n_synergy_buckets_distinct_nonzero():
    items = [
        _earned(bucket="port_match"),
        _earned(bucket="cost_synergy"),
        _earned(bucket="port_match"),
        _staple_only(),
    ]
    m = compute_coverage(items, top_n=30)
    assert m.n_synergy_buckets == 2


def test_empty_page_all_zero():
    m = compute_coverage([], top_n=30)
    assert m == CoverageMetrics(earned_top30=0, n_scored_cands=0, n_synergy_buckets=0)
