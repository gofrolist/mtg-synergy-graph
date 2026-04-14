"""Tests for mtg_synergy_graph.signals module — targeting full coverage."""

from __future__ import annotations

import pytest

from mtg_synergy_graph.signals import (
    _DETECTOR_MAX_DELTA,
    SIGNAL_TIER,
    TIER_BREADTH_WEIGHT,
    CandidateSignals,
    Signal,
    delta_to_confidence,
    signals_from_scored,
    tier_for,
)

# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------


class TestSignal:
    def test_frozen(self) -> None:
        s = Signal(signal_type="port_match", confidence=0.8, tier=1, evidence={})
        with pytest.raises(AttributeError):
            s.confidence = 0.5  # type: ignore[misc]

    def test_fields(self) -> None:
        ev = {"rule": "trigger_effect"}
        s = Signal(signal_type="lord", confidence=0.5, tier=1, evidence=ev)
        assert s.signal_type == "lord"
        assert s.confidence == 0.5
        assert s.tier == 1
        assert s.evidence is ev


# ---------------------------------------------------------------------------
# CandidateSignals — breadth / depth / composite
# ---------------------------------------------------------------------------


class TestCandidateSignals:
    def test_empty(self) -> None:
        cs = CandidateSignals()
        assert cs.signal_types == frozenset()
        assert cs.breadth == 0.0
        assert cs.depth == 0.0
        assert cs.composite_score == 0.0

    def test_single_tier1_signal(self) -> None:
        cs = CandidateSignals(signals=[Signal("port_match", 0.9, 1, {})])
        assert cs.signal_types == frozenset({"port_match"})
        assert cs.breadth == pytest.approx(1.0)
        assert cs.depth == pytest.approx(0.9 * 1.0)
        assert cs.composite_score == pytest.approx(100.0 + 0.9)

    def test_single_tier2_signal(self) -> None:
        cs = CandidateSignals(signals=[Signal("cost_synergy", 0.6, 2, {})])
        assert cs.breadth == pytest.approx(0.7)
        assert cs.depth == pytest.approx(0.6 * 0.7)

    def test_single_tier3_signal(self) -> None:
        cs = CandidateSignals(signals=[Signal("catchall", 0.4, 3, {})])
        assert cs.breadth == pytest.approx(0.3)
        assert cs.depth == pytest.approx(0.4 * 0.3)

    def test_multiple_distinct_types(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.8, 1, {}),
                Signal("cost_synergy", 0.5, 2, {}),
                Signal("catchall", 1.0, 3, {}),
            ]
        )
        expected_breadth = 1.0 + 0.7 + 0.3
        assert cs.breadth == pytest.approx(expected_breadth)
        expected_depth = 0.8 * 1.0 + 0.5 * 0.7 + 1.0 * 0.3
        assert cs.depth == pytest.approx(expected_depth)

    def test_duplicate_signal_type_picks_best_tier(self) -> None:
        """Two signals of the same type: breadth uses the best (lowest) tier."""
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.5, 2, {}),
                Signal("port_match", 0.9, 1, {}),
            ]
        )
        # Best tier for port_match is 1 -> breadth weight 1.0
        assert cs.breadth == pytest.approx(1.0)

    def test_depth_takes_max_confidence_per_type(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.3, 1, {}),
                Signal("port_match", 0.7, 1, {}),
            ]
        )
        # Max confidence 0.7, tier 1 weight 1.0
        assert cs.depth == pytest.approx(0.7 * 1.0)

    def test_replacement_excluded_from_breadth(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.8, 1, {}),
                Signal("replacement", 0.5, 1, {}),
            ]
        )
        # replacement should not count toward breadth
        assert cs.breadth == pytest.approx(1.0)

    def test_replacement_subtracted_from_depth(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.8, 1, {}),
                Signal("replacement", 0.3, 1, {}),
            ]
        )
        # depth = port_match_conf * weight - max(replacement_conf)
        assert cs.depth == pytest.approx(0.8 * 1.0 - 0.3)

    def test_multiple_replacement_signals_take_max(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 1.0, 1, {}),
                Signal("replacement", 0.2, 1, {}),
                Signal("replacement", 0.6, 1, {}),
            ]
        )
        assert cs.depth == pytest.approx(1.0 - 0.6)

    def test_only_replacement_signals(self) -> None:
        cs = CandidateSignals(signals=[Signal("replacement", 0.5, 1, {})])
        assert cs.breadth == 0.0
        assert cs.depth == pytest.approx(-0.5)

    def test_composite_score_formula(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 1.0, 1, {}),
                Signal("lord", 0.5, 1, {}),
            ]
        )
        expected = cs.breadth * 100.0 + cs.depth
        assert cs.composite_score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# CandidateSignals.to_legacy_buckets
# ---------------------------------------------------------------------------


class TestToLegacyBuckets:
    def test_empty_signals(self) -> None:
        cs = CandidateSignals()
        buckets = cs.to_legacy_buckets()
        assert buckets["total"] == 0.0
        assert buckets["port_match"] == 0.0

    def test_uses_delta_from_evidence(self) -> None:
        cs = CandidateSignals(signals=[Signal("port_match", 0.5, 1, {"_delta": 3.0})])
        buckets = cs.to_legacy_buckets()
        assert buckets["port_match"] == pytest.approx(3.0)

    def test_falls_back_to_confidence_times_max(self) -> None:
        cs = CandidateSignals(signals=[Signal("port_match", 0.5, 1, {})])
        buckets = cs.to_legacy_buckets()
        expected = 0.5 * _DETECTOR_MAX_DELTA["port_match"]
        assert buckets["port_match"] == pytest.approx(expected)

    def test_non_dict_evidence(self) -> None:
        """When evidence is not a dict, _delta lookup should be skipped."""
        cs = CandidateSignals(
            signals=[Signal("lord", 0.4, 1, "not a dict")]  # type: ignore[arg-type]
        )
        buckets = cs.to_legacy_buckets()
        expected = 0.4 * _DETECTOR_MAX_DELTA["lord"]
        assert buckets["lord"] == pytest.approx(expected)

    def test_total_is_sum_of_buckets(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.5, 1, {"_delta": 3.0}),
                Signal("lord", 0.3, 1, {"_delta": 2.0}),
            ]
        )
        buckets = cs.to_legacy_buckets()
        from mtg_synergy_graph.scoring import BUCKETS

        manual_total = sum(buckets[b] for b in BUCKETS)
        assert buckets["total"] == pytest.approx(manual_total)

    def test_multiple_signals_same_bucket_accumulate(self) -> None:
        cs = CandidateSignals(
            signals=[
                Signal("port_match", 0.5, 1, {"_delta": 2.0}),
                Signal("port_match", 0.3, 1, {"_delta": 1.5}),
            ]
        )
        buckets = cs.to_legacy_buckets()
        assert buckets["port_match"] == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# delta_to_confidence helper
# ---------------------------------------------------------------------------


class TestDeltaToConfidence:
    def test_known_bucket(self) -> None:
        # port_match max is 10.0
        assert delta_to_confidence("port_match", 5.0) == pytest.approx(0.5)

    def test_clamps_at_one(self) -> None:
        assert delta_to_confidence("port_match", 100.0) == pytest.approx(1.0)

    def test_clamps_at_zero_for_negative(self) -> None:
        assert delta_to_confidence("port_match", -5.0) == pytest.approx(0.0)

    def test_unknown_bucket_defaults_to_10(self) -> None:
        assert delta_to_confidence("unknown_bucket_xyz", 5.0) == pytest.approx(0.5)

    def test_zero_max_delta(self) -> None:
        """If max_delta is 0, should return 0.0 to avoid division by zero."""
        # Monkey-patch temporarily
        _DETECTOR_MAX_DELTA["__test_zero"] = 0.0
        try:
            assert delta_to_confidence("__test_zero", 5.0) == 0.0
        finally:
            del _DETECTOR_MAX_DELTA["__test_zero"]


# ---------------------------------------------------------------------------
# tier_for helper
# ---------------------------------------------------------------------------


class TestTierFor:
    def test_known_types(self) -> None:
        assert tier_for("port_match") == 1
        assert tier_for("cost_synergy") == 2
        assert tier_for("catchall") == 3

    def test_unknown_defaults_to_3(self) -> None:
        assert tier_for("nonexistent_signal") == 3


# ---------------------------------------------------------------------------
# signals_from_scored adapter
# ---------------------------------------------------------------------------


class TestSignalsFromScored:
    def _make_buckets(self, **overrides: float) -> dict[str, float]:
        from mtg_synergy_graph.scoring import BUCKETS

        b: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
        b["total"] = 0.0
        b.update(overrides)
        return b

    def test_empty_input(self) -> None:
        cs = signals_from_scored(self._make_buckets(), [])
        assert len(cs.signals) == 0

    def test_match_records_with_delta(self) -> None:
        records = [
            {"bucket": "port_match", "_delta": 5.0, "rule": "trigger_effect"},
        ]
        cs = signals_from_scored(self._make_buckets(port_match=5.0), records)
        assert len(cs.signals) == 1
        s = cs.signals[0]
        assert s.signal_type == "port_match"
        assert s.confidence == pytest.approx(0.5)  # 5/10
        assert s.tier == 1
        assert s.evidence is records[0]

    def test_match_records_without_delta(self) -> None:
        """Legacy records without _delta split bucket total evenly."""
        records = [
            {"bucket": "lord"},
            {"bucket": "lord"},
        ]
        buckets = self._make_buckets(lord=6.0)
        cs = signals_from_scored(buckets, records)
        assert len(cs.signals) == 2
        # Each should get confidence from 6.0/2 = 3.0, max_delta for lord=12
        expected_conf = delta_to_confidence("lord", 3.0)
        for s in cs.signals:
            assert s.confidence == pytest.approx(expected_conf)

    def test_fallback_for_bucket_without_records(self) -> None:
        """Non-zero bucket with no match records emits a fallback signal."""
        buckets = self._make_buckets(catchall=0.8)
        cs = signals_from_scored(buckets, [])
        assert len(cs.signals) == 1
        s = cs.signals[0]
        assert s.signal_type == "catchall"
        assert s.confidence == pytest.approx(delta_to_confidence("catchall", 0.8))
        assert s.evidence == {"raw_delta": 0.8}

    def test_zero_bucket_no_fallback(self) -> None:
        """Zero-valued bucket should not emit a fallback signal."""
        buckets = self._make_buckets(catchall=0.0)
        cs = signals_from_scored(buckets, [])
        assert len(cs.signals) == 0

    def test_unknown_bucket_in_records_skipped(self) -> None:
        """Records with a bucket not in _DETECTOR_MAX_DELTA are ignored."""
        records = [{"bucket": "totally_unknown_xyz", "_delta": 5.0}]
        cs = signals_from_scored(self._make_buckets(), records)
        assert len(cs.signals) == 0

    def test_records_and_fallback_coexist(self) -> None:
        """Records cover port_match; catchall has no records -> fallback."""
        records = [{"bucket": "port_match", "_delta": 3.0}]
        buckets = self._make_buckets(port_match=3.0, catchall=0.5)
        cs = signals_from_scored(buckets, records)
        types = {s.signal_type for s in cs.signals}
        assert "port_match" in types
        assert "catchall" in types
        assert len(cs.signals) == 2

    def test_negative_delta_uses_abs(self) -> None:
        """Negative _delta is passed through abs before computing confidence."""
        records = [{"bucket": "replacement", "_delta": -4.0}]
        buckets = self._make_buckets(replacement=-4.0)
        cs = signals_from_scored(buckets, records)
        assert len(cs.signals) == 1
        assert cs.signals[0].confidence == pytest.approx(delta_to_confidence("replacement", 4.0))


# ---------------------------------------------------------------------------
# Module-level constant sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_all_signal_tiers_have_breadth_weight(self) -> None:
        tiers_used = set(SIGNAL_TIER.values())
        for t in tiers_used:
            assert t in TIER_BREADTH_WEIGHT

    def test_detector_max_delta_covers_signal_tier(self) -> None:
        for signal_type in SIGNAL_TIER:
            assert signal_type in _DETECTOR_MAX_DELTA, (
                f"{signal_type} in SIGNAL_TIER but missing from _DETECTOR_MAX_DELTA"
            )
