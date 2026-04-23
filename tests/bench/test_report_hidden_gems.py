"""Audit report integration of hidden-gem metric (Unit 3 of plan 003).

Tests that ``build_report`` aggregates ``legacy["hidden_gem_hit_rate"]``
across entries, computes the delta vs pinned, and populates the new
``AuditReport`` fields (aggregate, pinned, delta, warning flag,
commander count). Markdown and JSON renderers surface the new values
with the expected formatting and keys.

All fixtures are synthesized in-memory — this file is a pure-function
test of the report layer, not an end-to-end pipeline test.
"""

from __future__ import annotations

import json

import pytest

from mtg_synergy_graph.bench.fixture import FixtureEntry, PinnedFixture
from mtg_synergy_graph.bench.hidden_gems import HIDDEN_GEM_WARN_THRESHOLD
from mtg_synergy_graph.bench.report import build_report

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_with_gem_rates(rates: list[float | None]) -> PinnedFixture:
    """Build a ``PinnedFixture`` with one entry per rate.

    A ``None`` rate means the commander has no EDHREC data → gem keys
    absent from ``legacy``.
    """
    entries: list[FixtureEntry] = []
    for i, rate in enumerate(rates):
        legacy: dict[str, object] = {}
        if rate is not None:
            legacy["hidden_gem_hit_rate"] = rate
            legacy["hidden_cards"] = []
        entries.append(
            FixtureEntry(
                commander=f"Cmdr_{i}",
                scores={f"cand_{i}": 1.0},
                legacy=legacy,
            )
        )
    return PinnedFixture(config_hash="x", created_at="t", entries=entries)


# ---------------------------------------------------------------------------
# Happy path — aggregate + delta computed
# ---------------------------------------------------------------------------


def test_both_sides_have_gem_data_aggregate_and_delta_computed() -> None:
    """Pinned + live both populated → aggregate, delta, count all present."""
    pinned_rates = [0.1, 0.2, 0.15, 0.1, 0.2, 0.1, 0.15, 0.2, 0.1, 0.2]
    live_rates = [0.1, 0.15, 0.15, 0.1, 0.2, 0.1, 0.15, 0.2, 0.1, 0.2]

    pinned = _fixture_with_gem_rates(list(pinned_rates))
    live = _fixture_with_gem_rates(list(live_rates))

    report = build_report("baseline.json", pinned, live)
    assert report.aggregate_hidden_gem_hit_rate is not None
    assert report.pinned_hidden_gem_hit_rate is not None
    assert report.hidden_gem_hit_rate_delta is not None
    assert report.commanders_with_edhrec == 10

    # Delta = live_mean - pinned_mean
    expected_pinned = sum(pinned_rates) / 10
    expected_live = sum(live_rates) / 10
    assert report.pinned_hidden_gem_hit_rate == expected_pinned
    assert report.aggregate_hidden_gem_hit_rate == expected_live
    assert report.hidden_gem_hit_rate_delta == expected_live - expected_pinned
    # Delta above threshold → no warning.
    assert report.hidden_gem_warning is False


def test_both_sides_zero_gives_zero_delta_no_warning() -> None:
    """Clean case: both pinned + live all zero → delta 0, no warning."""
    pinned = _fixture_with_gem_rates([0.0, 0.0, 0.0])
    live = _fixture_with_gem_rates([0.0, 0.0, 0.0])
    report = build_report("baseline.json", pinned, live)
    assert report.aggregate_hidden_gem_hit_rate == 0.0
    assert report.pinned_hidden_gem_hit_rate == 0.0
    assert report.hidden_gem_hit_rate_delta == 0.0
    assert report.hidden_gem_warning is False


# ---------------------------------------------------------------------------
# Threshold boundary tests (strict inequality)
# ---------------------------------------------------------------------------


def test_delta_just_above_threshold_no_warning() -> None:
    """Δ strictly above ``-HIDDEN_GEM_WARN_THRESHOLD`` → warning False.

    Pinned and live chosen so ``live - pinned`` is a small negative
    delta strictly above the negative threshold. The test asserts
    behavior (no warning) rather than exact float value, since
    IEEE-754 would otherwise require an exactly-representable delta.
    """
    # Construct inputs so live_mean - pinned_mean is well above
    # -HIDDEN_GEM_WARN_THRESHOLD (which is 0.02). Using 0.0 and 0.01 is
    # IEEE-754 exact enough for the behavior claim to be robust.
    pinned = _fixture_with_gem_rates([0.01])
    live = _fixture_with_gem_rates([0.0])
    report = build_report("baseline.json", pinned, live)
    assert report.hidden_gem_hit_rate_delta is not None
    # Behavior claim: delta is above -threshold (so no warning fires).
    assert report.hidden_gem_hit_rate_delta > -HIDDEN_GEM_WARN_THRESHOLD
    assert report.hidden_gem_warning is False


def test_delta_exactly_threshold_no_warning_strict_inequality() -> None:
    """delta bitwise == -HIDDEN_GEM_WARN_THRESHOLD → warning False.

    Subtraction against zero is exact in IEEE-754, so ``0.0 - threshold``
    is bitwise ``-threshold``. This lets us test the strict-inequality
    boundary cleanly.
    """
    pinned = _fixture_with_gem_rates([HIDDEN_GEM_WARN_THRESHOLD])
    live = _fixture_with_gem_rates([0.0])
    report = build_report("baseline.json", pinned, live)
    assert report.hidden_gem_hit_rate_delta is not None
    # Delta must be at -threshold (not strictly below) — warning stays False.
    assert report.hidden_gem_hit_rate_delta == -HIDDEN_GEM_WARN_THRESHOLD
    assert report.hidden_gem_warning is False


def test_delta_below_threshold_triggers_warning() -> None:
    """delta strictly below -HIDDEN_GEM_WARN_THRESHOLD → warning True.

    Asserts behavior (warning boolean) rather than exact float value,
    since the boundary claim is "delta < -threshold strictly".
    """
    # Drop of 0.03 (live_mean=0.17, pinned_mean=0.2) is well below the
    # -0.02 threshold. Exact float representation isn't critical — the
    # behavior claim is "dropping more than the threshold must warn."
    pinned = _fixture_with_gem_rates([0.20])
    live = _fixture_with_gem_rates([0.17])
    report = build_report("baseline.json", pinned, live)
    assert report.hidden_gem_hit_rate_delta is not None
    assert report.hidden_gem_hit_rate_delta < -HIDDEN_GEM_WARN_THRESHOLD
    assert report.hidden_gem_warning is True


def test_improvement_never_warns() -> None:
    """Positive delta (metric improved) → warning False."""
    pinned = _fixture_with_gem_rates([0.10])
    live = _fixture_with_gem_rates([0.15])
    report = build_report("baseline.json", pinned, live)
    assert report.hidden_gem_hit_rate_delta is not None
    assert report.hidden_gem_hit_rate_delta == pytest.approx(0.05)
    assert report.hidden_gem_hit_rate_delta > 0
    assert report.hidden_gem_warning is False


# ---------------------------------------------------------------------------
# Asymmetric availability (old pin / new pin edges)
# ---------------------------------------------------------------------------


def test_live_has_gem_data_pinned_does_not() -> None:
    """Old pin (no gem keys) + new live → delta None, warning False."""
    pinned = _fixture_with_gem_rates([None, None])
    live = _fixture_with_gem_rates([0.1, 0.2])
    report = build_report("baseline.json", pinned, live)
    assert report.pinned_hidden_gem_hit_rate is None
    assert report.aggregate_hidden_gem_hit_rate == pytest.approx(0.15)
    assert report.hidden_gem_hit_rate_delta is None
    assert report.hidden_gem_warning is False


def test_pinned_has_gem_data_live_does_not() -> None:
    """Pinned has data, live has none → delta None, warning False."""
    pinned = _fixture_with_gem_rates([0.1, 0.2])
    live = _fixture_with_gem_rates([None, None])
    report = build_report("baseline.json", pinned, live)
    assert report.pinned_hidden_gem_hit_rate == pytest.approx(0.15)
    assert report.aggregate_hidden_gem_hit_rate is None
    assert report.hidden_gem_hit_rate_delta is None
    assert report.hidden_gem_warning is False


def test_no_edhrec_data_anywhere() -> None:
    """No commander has gem data → aggregate None, count 0."""
    pinned = _fixture_with_gem_rates([None, None, None])
    live = _fixture_with_gem_rates([None, None, None])
    report = build_report("baseline.json", pinned, live)
    assert report.aggregate_hidden_gem_hit_rate is None
    assert report.pinned_hidden_gem_hit_rate is None
    assert report.hidden_gem_hit_rate_delta is None
    assert report.commanders_with_edhrec == 0
    assert report.hidden_gem_warning is False


# ---------------------------------------------------------------------------
# Mixed population (some commanders with EDHREC, some without)
# ---------------------------------------------------------------------------


def test_mixed_population_aggregates_only_over_edhrec_commanders() -> None:
    """5 with gem data, 5 without → aggregate is mean over 5."""
    pinned = _fixture_with_gem_rates([0.1, None, 0.2, None, 0.3, None, 0.1, None, 0.2, None])
    live = _fixture_with_gem_rates([0.1, None, 0.2, None, 0.3, None, 0.1, None, 0.2, None])
    report = build_report("baseline.json", pinned, live)
    assert report.commanders_with_edhrec == 5
    assert report.aggregate_hidden_gem_hit_rate is not None
    assert abs(report.aggregate_hidden_gem_hit_rate - (0.1 + 0.2 + 0.3 + 0.1 + 0.2) / 5) < 1e-9


# ---------------------------------------------------------------------------
# Rendering — markdown
# ---------------------------------------------------------------------------


def test_markdown_contains_new_line_with_numeric_values() -> None:
    """Markdown emits the ``**hidden_gem_hit_rate:**`` line."""
    pinned = _fixture_with_gem_rates([0.10, 0.20, 0.15])
    live = _fixture_with_gem_rates([0.12, 0.22, 0.17])
    report = build_report("baseline.json", pinned, live)
    md = report.to_markdown()
    assert "hidden_gem_hit_rate" in md
    # Aggregate formatted with 4 decimals.
    assert "0.1700" in md
    # Delta formatted with explicit sign + 4 decimals.
    assert "+0.0200" in md
    # Commander count shown.
    assert "3 cmdrs" in md


def test_markdown_no_edhrec_shows_emdash() -> None:
    """Empty aggregate → markdown uses em-dash sentinel."""
    pinned = _fixture_with_gem_rates([None, None])
    live = _fixture_with_gem_rates([None, None])
    report = build_report("baseline.json", pinned, live)
    md = report.to_markdown()
    assert "hidden_gem_hit_rate" in md
    assert "no commanders with EDHREC data" in md


def test_markdown_asymmetric_pinned_missing_shows_delta_emdash() -> None:
    """Pinned has no gem data but live does → delta em-dash, count shown."""
    pinned = _fixture_with_gem_rates([None, None])
    live = _fixture_with_gem_rates([0.1, 0.2])
    report = build_report("baseline.json", pinned, live)
    md = report.to_markdown()
    # Aggregate printed, delta rendered as em-dash because no pinned mean.
    assert "hidden_gem_hit_rate" in md
    assert "0.1500" in md
    assert "Δ —" in md


# ---------------------------------------------------------------------------
# Rendering — JSON
# ---------------------------------------------------------------------------


def test_json_has_all_four_new_keys_at_expected_types() -> None:
    """JSON output exposes the four new hidden_gem fields."""
    pinned = _fixture_with_gem_rates([0.10, 0.20])
    live = _fixture_with_gem_rates([0.12, 0.22])
    report = build_report("baseline.json", pinned, live)
    parsed = json.loads(report.to_json())

    assert "aggregate_hidden_gem_hit_rate" in parsed
    assert "hidden_gem_hit_rate_delta" in parsed
    assert "hidden_gem_warning" in parsed
    assert "commanders_with_edhrec" in parsed

    assert isinstance(parsed["aggregate_hidden_gem_hit_rate"], float)
    assert isinstance(parsed["hidden_gem_hit_rate_delta"], float)
    assert isinstance(parsed["hidden_gem_warning"], bool)
    assert isinstance(parsed["commanders_with_edhrec"], int)


def test_json_nulls_when_aggregate_none() -> None:
    """When no gem data exists, JSON values are ``null`` (not ``0``)."""
    pinned = _fixture_with_gem_rates([None])
    live = _fixture_with_gem_rates([None])
    report = build_report("baseline.json", pinned, live)
    parsed = json.loads(report.to_json())

    assert parsed["aggregate_hidden_gem_hit_rate"] is None
    assert parsed["hidden_gem_hit_rate_delta"] is None
    assert parsed["hidden_gem_warning"] is False
    assert parsed["commanders_with_edhrec"] == 0
