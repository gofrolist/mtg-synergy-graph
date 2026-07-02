"""Color-identity-conditioned IDF denominator (plan 2026-07-02-001, Unit 2).

Covers the flag-gated ``legal_pool`` parameter on ``_compute_idf_basis``:
conditioned N counting, the R2a orphaned-key fallback, flat-rule bypass,
``cond_mult`` / panharmonicon-floor composition, the pool-derivation
helper (identity union, legality predicate, partner sets, colorless),
the pool-vs-``engine.page()`` parity invariant, and flag-ON end-to-end
determinism. Flag-off bitwise identity is the load-bearing contract:
``bench.py audit --expect-identity`` must stay green while the flag
defaults to ``False``.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

from mtg_synergy_graph import universal_scorer
from mtg_synergy_graph.complement_rules.core import PortComplement
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.universal_scorer import (
    _FLAT_COUNT_RULES,
    _color_legal_pool,
    _compute_idf_basis,
    _compute_idf_weights,
    _idf_weights_from_basis,
    maybe_color_legal_pool,
    score_all_universal,
)


def _pc(rule_id: str, candidate: str, filter_group: str = "") -> PortComplement:
    return PortComplement(
        rule_id=rule_id,
        direction="synergy",
        candidate=candidate,
        cmdr_event="Sacrificed",
        cand_event="Sacrifice",
        filter_group=filter_group,
    )


# ---------------------------------------------------------------------------
# Conditioned N counting (pure, no DB)
# ---------------------------------------------------------------------------


class TestConditionedBasis:
    def test_conditioned_n_counts_only_pool_members(self):
        comps = [_pc("trigger_effect", c) for c in ("A", "B", "C", "D")]
        key = ("trigger_effect", "Sacrificed", "Sacrifice", "")

        unconditioned = _compute_idf_basis(comps)
        conditioned = _compute_idf_basis(comps, legal_pool=frozenset({"A", "B"}))

        assert unconditioned.base_idf_non_flat[key] == pytest.approx(1.0 / math.log2(5.0))
        assert conditioned.base_idf_non_flat[key] == pytest.approx(1.0 / math.log2(3.0))

    def test_none_pool_is_bitwise_identical_to_legacy(self):
        comps = [_pc("trigger_effect", c) for c in ("A", "B", "C")] + [
            _pc("spell_density", "A"),
            _pc("trigger_effect", "B", ":cond"),
        ]
        legacy = _compute_idf_weights(comps)
        via_none = _idf_weights_from_basis(_compute_idf_basis(comps, legal_pool=None))
        via_default = _idf_weights_from_basis(_compute_idf_basis(comps))

        assert via_none == legacy
        assert via_default == legacy

    def test_orphaned_key_falls_back_to_global_n(self):
        """R2a: in-pool N = 0 keeps the unconditioned weight — no
        ZeroDivisionError, no weight-1.0 inflation, key still present."""
        comps = [_pc("trigger_effect", c) for c in ("A", "B", "C", "D")]
        key = ("trigger_effect", "Sacrificed", "Sacrifice", "")

        conditioned = _compute_idf_basis(comps, legal_pool=frozenset({"Z"}))

        assert key in conditioned.base_idf_non_flat
        assert conditioned.base_idf_non_flat[key] == pytest.approx(1.0 / math.log2(5.0))

    def test_flat_rule_bypasses_pool(self):
        assert "spell_density" in _FLAT_COUNT_RULES
        comps = [_pc("spell_density", c) for c in ("A", "B", "C")]
        key = ("spell_density", "Sacrificed", "Sacrifice", "")

        unconditioned = _compute_idf_basis(comps)
        conditioned = _compute_idf_basis(comps, legal_pool=frozenset({"A"}))

        assert conditioned.flat_weights[key] == unconditioned.flat_weights[key]
        assert key not in conditioned.base_idf_non_flat

    def test_cond_mult_composes_on_conditioned_n(self):
        comps = [_pc("trigger_effect", "A", ":cond"), _pc("trigger_effect", "B", ":cond")]
        key = ("trigger_effect", "Sacrificed", "Sacrifice", ":cond")

        conditioned = _compute_idf_basis(comps, legal_pool=frozenset({"A"}))

        # n conditioned to 1, THEN the 0.5 cond_mult: 1/log2(2) * 0.5
        assert conditioned.base_idf_non_flat[key] == pytest.approx(0.5)

    def test_panharmonicon_floor_applies_to_conditioned_n(self):
        comps = [
            PortComplement(
                rule_id="panharmonicon",
                direction="synergy",
                candidate=c,
                cmdr_event="ChangesZone",
                cand_event="Token",
            )
            for c in ("A", "B", "C", "D")
        ]
        key = ("panharmonicon", "ChangesZone", "Token", "")

        conditioned = _compute_idf_basis(comps, legal_pool=frozenset({"A", "B"}))

        # Floor n = max(n, 30) binds after conditioning: 1/log2(31).
        assert conditioned.base_idf_non_flat[key] == pytest.approx(1.0 / math.log2(31.0))


# ---------------------------------------------------------------------------
# Pool derivation (DB-backed)
# ---------------------------------------------------------------------------

_CARD_INSERT = (
    "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture()
def pool_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = open_db(tmp_path / "synergy.db")
    rows = [
        ("Cmdr White", "Creature", "", 3, "W", 100, 1),
        ("Cmdr Black", "Creature", "", 3, "B", 110, 1),
        ("Cmdr Colorless", "Creature", "", 4, "", 120, 1),
        ("White Card", "Creature", "", 2, "W", 500, 1),
        ("Black Card", "Creature", "", 2, "B", 510, 1),
        ("WB Card", "Enchantment", "", 2, "W,B", 520, 1),
        ("Colorless Card", "Artifact", "", 1, "", 530, 1),
        ("Some Plane", "Plane", "", 0, "", 540, 1),
        ("Acorn Card", "Creature", "", 2, "W", 550, 0),
    ]
    conn.executemany(_CARD_INSERT, rows)
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestColorLegalPool:
    def test_mono_commander_pool(self, pool_db: sqlite3.Connection):
        pool = _color_legal_pool(pool_db, ["Cmdr White"])
        assert pool == frozenset({"White Card", "Colorless Card", "Cmdr Colorless"})

    def test_partner_set_uses_identity_union(self, pool_db: sqlite3.Connection):
        pool = _color_legal_pool(pool_db, ["Cmdr White", "Cmdr Black"])
        assert "WB Card" in pool
        assert "White Card" in pool and "Black Card" in pool
        # Commanders themselves are excluded, mirroring page().
        assert "Cmdr White" not in pool and "Cmdr Black" not in pool

    def test_colorless_commander_pool(self, pool_db: sqlite3.Connection):
        pool = _color_legal_pool(pool_db, ["Cmdr Colorless"])
        assert pool == frozenset({"Colorless Card"})

    def test_excludes_non_edh_types_and_illegal(self, pool_db: sqlite3.Connection):
        pool = _color_legal_pool(pool_db, ["Cmdr White"])
        assert "Some Plane" not in pool
        assert "Acorn Card" not in pool

    def test_missing_commander_raises(self, pool_db: sqlite3.Connection):
        with pytest.raises(ValueError, match="Nonexistent"):
            _color_legal_pool(pool_db, ["Nonexistent Commander"])

    def test_cache_and_sql_paths_agree(self, pool_db: sqlite3.Connection):
        from mtg_synergy_graph.penalties import build_candidate_cache

        cache = build_candidate_cache(pool_db)
        via_cache = _color_legal_pool(pool_db, ["Cmdr White"], candidate_cache=cache)
        via_sql = _color_legal_pool(pool_db, ["Cmdr White"], candidate_cache=None)
        assert via_cache == via_sql

    def test_pool_matches_engine_legal_cards(self, tmp_path: Path):
        """R2a invariant: the pool equals the legal set page()/legal_cards
        ranks, on both derivation paths — orphaned keys therefore cannot
        touch any ranked candidate."""
        from mtg_synergy_graph.engine import SynergyEngine
        from mtg_synergy_graph.penalties import build_candidate_cache

        db_path = tmp_path / "synergy.db"
        conn = open_db(db_path)
        conn.executemany(
            _CARD_INSERT,
            [
                ("Cmdr White", "Creature", "", 3, "W", 100, 1),
                ("White Card", "Creature", "", 2, "W", 500, 1),
                ("Black Card", "Creature", "", 2, "B", 510, 1),
                ("Colorless Card", "Artifact", "", 1, "", 530, 1),
                ("Some Plane", "Plane", "", 0, "", 540, 1),
                ("Acorn Card", "Creature", "", 2, "W", 550, 0),
            ],
        )
        conn.commit()

        engine = SynergyEngine(db_path)
        try:
            legal = frozenset(engine.legal_cards("Cmdr White"))
        finally:
            engine.close()

        cache = build_candidate_cache(conn)
        assert _color_legal_pool(conn, ["Cmdr White"], candidate_cache=cache) == legal
        assert _color_legal_pool(conn, ["Cmdr White"], candidate_cache=None) == legal
        conn.close()


# ---------------------------------------------------------------------------
# Flag gating + end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture()
def scoring_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Mono-red commander; two candidates sharing one IDF key, one of them
    out-of-color — the shape where conditioning changes the weight."""
    conn = open_db(tmp_path / "synergy.db")
    conn.executemany(
        _CARD_INSERT,
        [
            ("Test Commander", "Creature", "", 4, "R", 1000, 1),
            ("Red Token Maker", "Creature", "", 3, "R", 500, 1),
            ("Blue Token Maker", "Creature", "", 3, "U", 400, 1),
        ],
    )
    port_insert = (
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)"
    )
    conn.execute(port_insert, ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"))
    conn.execute(port_insert, ("Red Token Maker", "effect", "Token", "Creature.Token", "{make}"))
    conn.execute(port_insert, ("Blue Token Maker", "effect", "Token", "Creature.Token", "{make}"))
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class TestFlagGating:
    def test_flag_defaults_off(self):
        assert universal_scorer._ENABLE_COLOR_CONDITIONED_IDF is False

    def test_maybe_pool_none_when_flag_off(self, pool_db: sqlite3.Connection):
        assert maybe_color_legal_pool(pool_db, ["Cmdr White"]) is None

    def test_maybe_pool_derives_when_flag_on(self, pool_db: sqlite3.Connection):
        with mock.patch.object(universal_scorer, "_ENABLE_COLOR_CONDITIONED_IDF", True):
            pool = maybe_color_legal_pool(pool_db, ["Cmdr White"])
        assert pool is not None and "White Card" in pool

    def test_flag_on_changes_scores_and_is_deterministic(self, scoring_db: sqlite3.Connection):
        """End-to-end flag-ON path (verify-from-stored-config learning):
        conditioning must actually change the in-color candidate's score
        (its shared key loses the out-of-color co-matcher from N), and
        repeated runs must agree bitwise."""
        off = score_all_universal(scoring_db, ["Test Commander"])
        with mock.patch.object(universal_scorer, "_ENABLE_COLOR_CONDITIONED_IDF", True):
            on_1 = score_all_universal(scoring_db, ["Test Commander"])
            on_2 = score_all_universal(scoring_db, ["Test Commander"])

        assert on_1["Red Token Maker"].score == on_2["Red Token Maker"].score
        assert on_1["Red Token Maker"].score != off["Red Token Maker"].score

    def test_cached_basis_path_matches_live_path_flag_on(self, scoring_db: sqlite3.Connection):
        """Optimizer fidelity under flag-ON: a basis built with the pool
        baked in (the optimize.py fill-site recipe) must reproduce the
        live no-basis path exactly — one mock-patch flips both."""
        from mtg_synergy_graph.complement_rules import find_all_complements
        from mtg_synergy_graph.universal_scorer import score_from_complements

        comps = find_all_complements(scoring_db, ["Test Commander"])
        with mock.patch.object(universal_scorer, "_ENABLE_COLOR_CONDITIONED_IDF", True):
            live = score_from_complements(scoring_db, ["Test Commander"], comps)
            pool = maybe_color_legal_pool(scoring_db, ["Test Commander"])
            basis = _compute_idf_basis(comps, legal_pool=pool)
            cached = score_from_complements(scoring_db, ["Test Commander"], comps, idf_basis=basis)

        assert {n: s.score for n, s in live.items()} == {n: s.score for n, s in cached.items()}
