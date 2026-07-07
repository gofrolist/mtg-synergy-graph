"""Committed golden-fixture freshness gate (2026-06-09 audit follow-up).

The pinned fixtures fingerprint the scoring config via
``compute_config_hash()``. The regeneration discipline used to be
prose-only ("regenerate after data refreshes" in CLAUDE.md) and lapsed
in practice: ``golden_set_run_500.json`` went stale on 2026-04-30 and
the ``bench.py audit --optimize`` default exited 2 for ~3 weeks with
nothing flagging it. This test turns the convention into a CI gate
that needs no database: ``compute_config_hash()`` depends only on
tracked inputs (the rule registry, ``data/scoring_weights.json``, the
seed JSONs, ``heuristics.STAPLES``), so a config change committed
without a re-pin fails here immediately.

When intentionally re-pinning: regenerate the fixture(s) via
``bench.py audit --repin --yes`` (100-cmdr canonical) and
``scripts/bootstrap_golden_set_500.py`` (500-cmdr optimizer default)
and this test goes green again — no edit here is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.tensor import compute_config_hash

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

_COMMITTED_GOLDEN_FIXTURES = (
    "golden_set_run.json",
    "golden_set_run_500.json",
    # Archetype-payoff cohort fixture (plan 2026-07-03-001). Joins the no-DB
    # freshness gate so its config_hash cannot silently go stale at re-pin time.
    # Rebuild via `scripts/bootstrap_archetype_payoff_fixture.py`.
    "golden_set_archetype_payoff.json",
)


@pytest.mark.parametrize("fixture_name", _COMMITTED_GOLDEN_FIXTURES)
def test_committed_fixture_config_hash_is_fresh(fixture_name: str) -> None:
    fixture_path = _FIXTURES / fixture_name
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    pinned = payload.get("config_hash")
    assert pinned, f"{fixture_name} has no config_hash key — re-pin it"
    live = compute_config_hash()
    assert pinned == live, (
        f"{fixture_name} is stale: pinned config_hash {pinned[:12]}… != "
        f"live {live[:12]}…. A scoring-config input changed without a "
        "re-pin. Regenerate via `bench.py audit --repin --yes` (100-cmdr "
        "canonical) or `scripts/bootstrap_golden_set_500.py` (500-cmdr "
        "optimizer default)."
    )
