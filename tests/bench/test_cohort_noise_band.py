"""Cohort NDCG noise-band derivation (plan 2026-07-03-001 Unit 4).

The cohort is small (~33), so a cohort-NDCG readout is only a "win" once it
clears the bootstrap noise band / minimum-detectable-effect. ``portfolio_sim
bands`` publishes that band over the cohort fixture; these tests lock the
``bootstrap_band`` primitive it uses: a finite, deterministic band under a
fixed seed, and graceful handling of a degenerate tiny cohort.
"""

from __future__ import annotations

import math

from mtg_synergy_graph.bench.portfolio_sim import bootstrap_band


def test_band_is_finite_and_deterministic() -> None:
    """Happy path: a realistic cohort NDCG sample yields a finite, stable band."""
    values = [0.12, 0.20, 0.05, 0.31, 0.18, 0.09, 0.27, 0.14, 0.22, 0.16]
    band = bootstrap_band(values, seed=17, n_boot=500)
    assert band["n"] == len(values)
    assert math.isfinite(band["mean"])
    assert math.isfinite(band["half_width"])
    assert band["half_width"] >= 0.0
    assert band["ci95_low"] <= band["mean"] <= band["ci95_high"]

    # Deterministic under a fixed seed.
    again = bootstrap_band(values, seed=17, n_boot=500)
    assert band == again


def test_degenerate_tiny_cohort_returns_band_without_error() -> None:
    """Edge: n<5 still returns a finite band (wide is fine; a crash is not)."""
    band = bootstrap_band([0.1, 0.4, 0.25], seed=17, n_boot=200)
    assert band["n"] == 3
    assert math.isfinite(band["half_width"])
    assert band["half_width"] >= 0.0


def test_empty_cohort_returns_zero_band() -> None:
    """Edge: an empty sample degrades to a zero band, not a ZeroDivisionError."""
    band = bootstrap_band([], seed=17, n_boot=200)
    assert band["n"] == 0
    assert band["half_width"] == 0.0
    assert band["mean"] == 0.0
