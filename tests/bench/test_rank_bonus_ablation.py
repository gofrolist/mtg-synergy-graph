"""rank_bonus-ablation sidecar tests (Task 1 of plan
2026-07-07-002).

Pure tests drive :func:`compute_rank_bonus_ablation` /
:func:`rank_bonus_for` over hand-built :class:`RankedCandidate`
tuples (synthetic rank_bonus-decided ties, sentinel no-op), mirroring
``tests/bench/test_forensics_ablation.py``'s style for the sibling R8
tiebreaker-ablation. Renderer tests confirm the one md line / JSON
field the CLI wiring surfaces on every ``--forensics`` run.
"""

from __future__ import annotations

import math

import pytest

from mtg_synergy_graph import universal_scorer
from mtg_synergy_graph.bench.forensics import (
    BUCKETS,
    RANK_BONUS_SENTINEL_CAP,
    CommanderForensics,
    RankBonusAblation,
    RankedCandidate,
    aggregate_forensics,
    compute_rank_bonus_ablation,
    rank_bonus_for,
)
from mtg_synergy_graph.bench.forensics_report import (
    ForensicsRenderData,
    render_forensics_json,
    render_forensics_markdown,
)
from mtg_synergy_graph.engine import UNRANKED_EDHREC_SENTINEL
from mtg_synergy_graph.validate import compute_ndcg

CMDR = "General Gee"


def _dcg(gains: list[float]) -> float:
    return sum((math.pow(2.0, rel) - 1.0) / math.log2(i + 2) for i, rel in enumerate(gains) if rel > 0)


# ---------------------------------------------------------------------------
# rank_bonus_for — the mirrored in-score formula
# ---------------------------------------------------------------------------


class TestRankBonusFor:
    def test_rank_zero_is_max_bonus(self) -> None:
        assert rank_bonus_for(0) == pytest.approx(0.005)

    def test_rank_30000_is_zero_bonus(self) -> None:
        assert rank_bonus_for(30000) == pytest.approx(0.0)

    def test_engine_sentinel_is_zero_bonus(self) -> None:
        """UNRANKED_EDHREC_SENTINEL (10**9) — the sentinel
        load_card_meta stamps on unranked cards — must clamp to
        exactly the same zero bonus as the scorer's own local 99999
        fallback (both exceed the 30000 divisor)."""
        assert rank_bonus_for(UNRANKED_EDHREC_SENTINEL) == 0.0
        assert rank_bonus_for(RANK_BONUS_SENTINEL_CAP) == 0.0
        assert rank_bonus_for(UNRANKED_EDHREC_SENTINEL) == rank_bonus_for(RANK_BONUS_SENTINEL_CAP)


class TestRankBonusForDelegatesToUniversalScorer:
    """F3 (PR #103 review): the formula must live in exactly one place —
    ``universal_scorer.rank_bonus_for_rank`` — with ``forensics.rank_bonus_for``
    delegating to it after applying its local sentinel cap. Cross-checks both
    sides so the two can never silently drift again."""

    @pytest.mark.parametrize("rank", [1, 15000, 29999, 30000, 99999, 10**9])
    def test_matches_universal_scorer_formula(self, rank: int) -> None:
        assert rank_bonus_for(rank) == universal_scorer.rank_bonus_for_rank(min(rank, 99999))


# ---------------------------------------------------------------------------
# compute_rank_bonus_ablation — synthetic scenarios
# ---------------------------------------------------------------------------


class TestRankBonusDecidedFlip:
    """Two candidates share the SAME underlying mechanical total
    (5.0); only their rank_bonus differs (rank 100 vs rank 20000), so
    the raw production order is decided purely by rank_bonus. Under
    ablation the mechanical totals tie exactly and the (cmc, name)
    tiebreak flips the order (name chosen deliberately reversed)."""

    MECHANICAL_TOTAL = 5.0
    RANK_HI_BONUS = 100  # smaller edhrec_rank -> LARGER bonus
    RANK_LO_BONUS = 20000  # larger edhrec_rank -> smaller bonus

    def _ranking(self) -> tuple[RankedCandidate, ...]:
        bonus_hi = rank_bonus_for(self.RANK_HI_BONUS)
        bonus_lo = rank_bonus_for(self.RANK_LO_BONUS)
        assert bonus_hi > bonus_lo > 0.0  # sanity: both nonzero, hi > lo

        # "Zeta" carries the larger bonus -> ranks 1st in production;
        # "Alpha" carries the smaller bonus -> ranks 2nd. Alphabetical
        # order is the OPPOSITE of production order, so an ablated tie
        # flips them.
        zeta = RankedCandidate(
            name="Zeta", rank=1, total_score=self.MECHANICAL_TOTAL + bonus_hi, cmc=2.0, edhrec_rank=self.RANK_HI_BONUS
        )
        alpha = RankedCandidate(
            name="Alpha",
            rank=2,
            total_score=self.MECHANICAL_TOTAL + bonus_lo,
            cmc=2.0,
            edhrec_rank=self.RANK_LO_BONUS,
        )
        return (zeta, alpha)

    def test_raw_order_is_rank_bonus_decided(self) -> None:
        ranking = self._ranking()
        assert [rc.name for rc in sorted(ranking, key=lambda rc: rc.rank)] == ["Zeta", "Alpha"]

    def test_ablation_flips_the_pair(self) -> None:
        ranking = self._ranking()
        labels = {"Zeta": 3.0, "Alpha": 1.0}
        ablation = compute_rank_bonus_ablation({CMDR: ranking}, {CMDR: labels}, n_canonical=1, top_n=2)

        # Raw NDCG: Zeta (rel 3.0) then Alpha (rel 1.0) — production order.
        ideal = _dcg([3.0, 1.0])
        expected_raw = _dcg([3.0, 1.0]) / ideal
        # Ablated: Alpha then Zeta — the flip.
        expected_ablated = _dcg([1.0, 3.0]) / ideal

        assert ablation.ndcg_raw == pytest.approx(expected_raw)
        assert ablation.ndcg_ablated == pytest.approx(expected_ablated)
        assert ablation.ndcg_ablated < ablation.ndcg_raw
        assert ablation.delta == pytest.approx(expected_ablated - expected_raw)
        assert ablation.delta < 0.0


class TestSentinelIsNoOp:
    def test_sentinel_edhrec_rank_ablation_is_a_no_op(self) -> None:
        """A candidate at the UNRANKED sentinel has zero rank_bonus
        already — ablating it changes nothing about its relative
        order against a distinctly-scored peer."""
        high = RankedCandidate(name="Alpha", rank=1, total_score=10.0, cmc=2.0, edhrec_rank=UNRANKED_EDHREC_SENTINEL)
        low = RankedCandidate(name="Beta", rank=2, total_score=5.0, cmc=2.0, edhrec_rank=UNRANKED_EDHREC_SENTINEL)
        labels = {"Alpha": 3.0, "Beta": 1.0}
        ablation = compute_rank_bonus_ablation({CMDR: (high, low)}, {CMDR: labels}, n_canonical=1, top_n=2)
        assert ablation.ndcg_raw == pytest.approx(ablation.ndcg_ablated)
        assert ablation.delta == pytest.approx(0.0)


class TestNdcgRawMatchesExistingHelper:
    def test_ndcg_raw_equals_compute_ndcg_over_captured_order(self) -> None:
        specs = [
            RankedCandidate(name="Zinnia", rank=1, total_score=10.0, cmc=1.0, edhrec_rank=50),
            RankedCandidate(name="Echo", rank=2, total_score=9.0, cmc=2.0, edhrec_rank=1),
            RankedCandidate(name="Yarrow", rank=3, total_score=8.0, cmc=2.0, edhrec_rank=1),
        ]
        labels = {"Zinnia": 3.0, "Yarrow": 1.0}
        ablation = compute_rank_bonus_ablation({CMDR: tuple(specs)}, {CMDR: labels}, n_canonical=1, top_n=3)

        expected = compute_ndcg([rc.name for rc in specs], labels, k=3)
        assert ablation.ndcg_raw == pytest.approx(expected)


class TestCanonicalDenominator:
    def test_dilutes_with_skipped_commanders(self) -> None:
        specs = (
            RankedCandidate(name="Zinnia", rank=1, total_score=10.0, cmc=1.0, edhrec_rank=50),
            RankedCandidate(name="Echo", rank=2, total_score=9.0, cmc=2.0, edhrec_rank=1),
        )
        labels = {"Zinnia": 3.0}
        one = compute_rank_bonus_ablation({CMDR: specs}, {CMDR: labels}, n_canonical=1, top_n=2)
        two = compute_rank_bonus_ablation({CMDR: specs}, {CMDR: labels}, n_canonical=2, top_n=2)
        assert two.ndcg_raw == pytest.approx(one.ndcg_raw / 2)
        assert two.ndcg_ablated == pytest.approx(one.ndcg_ablated / 2)

    def test_n_canonical_below_rankings_raises(self) -> None:
        specs = (RankedCandidate(name="Zinnia", rank=1, total_score=10.0, cmc=1.0, edhrec_rank=50),)
        with pytest.raises(ValueError, match="n_canonical"):
            compute_rank_bonus_ablation({CMDR: specs}, {CMDR: {}}, n_canonical=0)


# ---------------------------------------------------------------------------
# Renderer wiring — the sidecar's one md line / JSON field
# ---------------------------------------------------------------------------


def _empty_render_data(rank_bonus_ablation: RankBonusAblation | None) -> ForensicsRenderData:
    entry = CommanderForensics(
        commander=CMDR,
        misses=(),
        bucket_counts=dict.fromkeys(BUCKETS, 0),
        live_top_30=(),
        ranking=(),
    )
    report = aggregate_forensics([entry])
    return ForensicsRenderData(
        report=report,
        config_hash="cafebabe0000",
        fixture_path="fixture.json",
        enrichments=(),
        outranked_rank_quantiles=(("61-100", 0), ("101-500", 0), (">500", 0)),
        outranked_family_contributions=(),
        aggregate_displacer_shares=(),
        no_rules_port_shapes=(),
        rank_bonus_ablation=rank_bonus_ablation,
    )


class TestRendererWiring:
    def test_markdown_line_present_when_set(self) -> None:
        ablation = RankBonusAblation(n_commanders=1, ndcg_raw=0.2328, ndcg_ablated=0.1887, delta=-0.0441)
        out = render_forensics_markdown(_empty_render_data(ablation))
        assert "rank_bonus-ablated NDCG@30: 0.1887 (raw 0.2328, delta -0.0441)" in out
        assert "EDHREC-at-inference credit" in out

    def test_markdown_omits_line_when_none(self) -> None:
        out = render_forensics_markdown(_empty_render_data(None))
        assert "rank_bonus-ablated NDCG@30" not in out

    def test_json_field_present_when_set(self) -> None:
        ablation = RankBonusAblation(n_commanders=1, ndcg_raw=0.2328, ndcg_ablated=0.1887, delta=-0.0441)
        import json

        payload = json.loads(render_forensics_json(_empty_render_data(ablation)))
        block = payload["rank_bonus_ablation"]
        assert block["ndcg_raw"] == pytest.approx(0.2328)
        assert block["ndcg_ablated"] == pytest.approx(0.1887)
        assert block["delta"] == pytest.approx(-0.0441)
        assert block["n_commanders"] == 1

    def test_json_field_absent_when_none(self) -> None:
        import json

        payload = json.loads(render_forensics_json(_empty_render_data(None)))
        assert "rank_bonus_ablation" not in payload
