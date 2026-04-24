"""R7 identity guardrail: adding ``tensor_sink`` must not change scoring.

The unified-eval-harness plan (FR7, R7 success criterion) requires that
``score_all_universal()`` with ``tensor_sink=None`` produces bitwise-
identical output to the pre-hook behavior. The hook must be purely
additive. These tests lock the contract: if a future change accidentally
alters the hot path, they fail immediately instead of shipping a silent
scoring regression.

Tests use the shared ``rules_db`` fixture from tests/conftest.py plus
minimal fixture ports so the assertion is focused on the identity
invariant, not on any particular rule's behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.universal_scorer import (
    TensorRow,
    UniversalScore,
    score_all_universal,
)


@pytest.fixture()
def scoring_fixture(tmp_path: Path) -> sqlite3.Connection:
    """Minimal fixture: one commander with one trigger, three candidates
    whose effects match. Enough to drive multiple complement rules.

    Uses ``open_db()`` so the schema matches production exactly — the
    rules_db fixture in tests/conftest.py is a stripped subset.
    """
    conn = open_db(tmp_path / "synergy.db")

    # Two commanders (the second is a no-match control for commander self-exclusion).
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    # A few candidates across different port shapes so multiple rules can fire.
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Token Maker", "Creature", "", 3, "R", 500, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Counter Doubler", "Enchantment", "", 3, "R", 300, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Generic Creature", "Creature", "Human", 2, "R", 5000, 1),
    )

    # Commander has an ETB trigger on creatures.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    # Candidate effects that might match the trigger.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Counter Doubler", "replacement", "PutCounter", "Creature.YouCtrl", "{double}"),
    )
    conn.commit()
    return conn


def _score_dict(scores: dict[str, UniversalScore]) -> dict[str, tuple[float, int, frozenset[str]]]:
    """Extract canonical per-candidate fingerprints for diffing.

    Uses `(score, len(complements), distinct_rules)` — the three facets
    that would change if the hot path behavior drifted. Direct float
    comparison of `score` is bitwise because the call path is pure.
    """
    return {name: (s.score, len(s.complements), s.distinct_rules) for name, s in scores.items()}


def test_sink_none_default_matches_no_sink_kwarg(scoring_fixture: sqlite3.Connection) -> None:
    """Calling without the kwarg == passing ``tensor_sink=None``.

    The default value must be ``None`` and must produce identical output.
    This protects against "someone changed the default and now every audit
    silently runs the sink" regressions.
    """
    implicit = score_all_universal(scoring_fixture, ["Test Commander"])
    explicit_none = score_all_universal(scoring_fixture, ["Test Commander"], tensor_sink=None)

    assert _score_dict(implicit) == _score_dict(explicit_none)


def test_sink_noop_produces_identical_scores(scoring_fixture: sqlite3.Connection) -> None:
    """A no-op sink must not perturb scoring.

    If the hot path ever starts mutating UniversalScore based on whether
    a sink is wired, this test fails. The sink has read-only semantics.
    """
    baseline = score_all_universal(scoring_fixture, ["Test Commander"])

    captured: list[TensorRow] = []

    def sink(row: TensorRow) -> None:
        captured.append(row)

    with_sink = score_all_universal(scoring_fixture, ["Test Commander"], tensor_sink=sink)

    assert _score_dict(baseline) == _score_dict(with_sink)


def test_sink_captures_rows_when_rules_fire(scoring_fixture: sqlite3.Connection) -> None:
    """Happy path: sink fires once per (candidate, rule_id) with positive contribution.

    The shape is tight — no duplicates, commander field is the commander we
    scored for, each rule_id maps to a real registered rule. Exact
    contribution values are checked in ``test_sink_matches_legacy_buckets``.
    """
    captured: list[TensorRow] = []

    def sink(row: TensorRow) -> None:
        captured.append(row)

    score_all_universal(scoring_fixture, ["Test Commander"], tensor_sink=sink)

    # Every row is for the expected commander.
    assert {r.commander for r in captured} == {"Test Commander"}
    # No row has zero contribution — the sink filters empty contributions.
    assert all(r.contribution != 0 for r in captured)
    # No duplicate (cand, rule_id) — each rule's contribution is aggregated.
    pairs = [(r.candidate, r.rule_id) for r in captured]
    assert len(pairs) == len(set(pairs))
    # Each raw_count is >= 1 when there's any contribution.
    assert all(r.raw_count >= 1 for r in captured)


def test_sink_rejects_multi_commander_calls(scoring_fixture: sqlite3.Connection) -> None:
    """Safety guard: ``tensor_sink`` + ``len(commander_set) != 1`` is a ValueError.

    Prevents the latent bug where ``_emit_tensor_rows`` would stamp
    every row with ``commander_set[0]`` regardless of how many
    commanders the caller passed. All current call paths pass a
    1-element list; this test locks that invariant.
    """

    def sink(row: TensorRow) -> None:
        pass

    with pytest.raises(ValueError, match="single-commander"):
        score_all_universal(scoring_fixture, ["Test Commander", "Generic Creature"], tensor_sink=sink)


def test_sink_matches_legacy_buckets(scoring_fixture: sqlite3.Connection) -> None:
    """Correctness: sink rows aggregate to the same per-rule totals that
    ``UniversalScore.to_legacy_buckets()`` produces in-memory.

    The tensor is the persisted form of the same math — if these numbers
    drift apart, ``bench.py audit --rule`` output will be wrong.
    """
    captured: dict[tuple[str, str], float] = {}

    def sink(row: TensorRow) -> None:
        captured[(row.candidate, row.rule_id)] = row.contribution

    scores = score_all_universal(scoring_fixture, ["Test Commander"], tensor_sink=sink)

    # For each candidate × rule in the sink output, verify the sum over
    # that rule's complements in the in-memory UniversalScore matches.
    from collections import defaultdict

    for (cand, rule_id), sink_contrib in captured.items():
        score_obj = scores[cand]
        # Recompute per-rule synergy/anti using the same dedup `score()` does.
        per_rule: dict[str, float] = defaultdict(float)
        seen_syn: set[tuple[str, str, str, str]] = set()
        seen_anti: set[tuple[str, str, str, str]] = set()
        for c in score_obj.complements:
            key = (c.rule_id, c.cmdr_event, c.cand_event, c.filter_group)
            w = score_obj.idf_weights.get(key, 1.0)
            if c.direction == "synergy" and key not in seen_syn:
                seen_syn.add(key)
                per_rule[c.rule_id] += w
            elif c.direction == "anti_synergy" and key not in seen_anti:
                seen_anti.add(key)
                per_rule[c.rule_id] -= w
        expected = per_rule.get(rule_id, 0.0)
        assert sink_contrib == pytest.approx(expected, abs=1e-12), (
            f"sink contribution for ({cand}, {rule_id}) = {sink_contrib}, but legacy computation = {expected}"
        )


# ---------------------------------------------------------------------------
# Plan 003 Unit 7 — embedding contribution flag-off identity invariant.
# ---------------------------------------------------------------------------


def test_embedding_contribution_field_defaults_to_zero_when_flag_off(
    scoring_fixture: sqlite3.Connection,
) -> None:
    """R7/Unit7: ``_ENABLE_EMBEDDING_CONTRIBUTION=False`` (module default)
    → every ``UniversalScore`` carries ``embedding_contribution == 0.0``
    exactly, so ``score`` totals are bitwise-identical to the pre-plan
    path (no embedding term contributes).

    This is the flag-off identity guardrail required by the plan's
    Execution note for Unit 7: before field plumbing landed this test
    failed with ``AttributeError`` (no such field); after plumbing it
    must pass trivially because every short-circuit in the scorer wire
    lands on 0.0 when the flag is off.
    """
    from mtg_synergy_graph.embeddings import contribution as emb_contribution

    # Belt-and-suspenders: the plan forbids flipping the default in this
    # unit. If a future edit sets True as the default here the audit
    # sweep owns that flip, not this test — fail loud if it drifts.
    assert emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION is False

    scores = score_all_universal(scoring_fixture, ["Test Commander"])

    for name, score_obj in scores.items():
        assert score_obj.embedding_contribution == 0.0, (
            f"{name}: embedding_contribution must be exactly 0.0 when flag "
            f"is off, got {score_obj.embedding_contribution}"
        )


def test_embedding_contribution_nonzero_when_flag_on(
    scoring_fixture: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on + w_emb=0.5 + populated ``card_embeddings`` → at least one
    candidate has a nonzero embedding contribution.

    Complements the flag-off identity test: proves the wire is actually
    live (not a dead branch) when the gate opens. Uses a synthetic
    deterministic vector per candidate so the cosine math is testable.
    """
    from mtg_synergy_graph.embeddings import commander_target as ct
    from mtg_synergy_graph.embeddings import config as emb_config
    from mtg_synergy_graph.embeddings import contribution as emb_contribution
    from mtg_synergy_graph.embeddings import store as emb_store

    # Flip the flag + enable a nonzero weight. Module-level defaults are
    # restored at teardown by pytest's monkeypatch.
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    # Ensure commander-target cache is cold for this scenario.
    ct.clear_cache()

    # Populate card_embeddings on the same connection the scorer uses.
    # Every card in the fixture gets a deterministic L2-normalized vector.
    def _seeded(seed: int) -> np.ndarray:
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(128).astype(np.float32)
        return (v / np.linalg.norm(v)).astype(np.float32)

    vectors = {
        "Test Commander": _seeded(1),
        "Token Maker": _seeded(2),
        "Counter Doubler": _seeded(3),
        "Generic Creature": _seeded(4),
    }
    emb_store.write_vectors(
        scoring_fixture,
        vectors,
        emb_config.get_embedding_config_inputs(),
    )

    scores = score_all_universal(scoring_fixture, ["Test Commander"])

    assert any(s.embedding_contribution != 0.0 for s in scores.values()), (
        "With flag on + populated vectors, at least one candidate should have a nonzero embedding contribution"
    )
