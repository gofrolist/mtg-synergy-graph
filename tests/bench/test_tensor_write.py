"""TensorWriter + config_hash tests (Unit 2).

Covers the persistence side of the tensor hook: rows flow from
``score_all_universal(..., tensor_sink=writer.sink)`` through batching
into SQLite, tagged with a stable config_hash that flips on any scoring-
config change. The hot-path identity invariant lives in
``test_universal_scorer_identity.py``; this file owns the writer itself.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from mtg_synergy_graph.bench.tensor import TensorWriter, compute_config_hash
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.universal_scorer import (
    _FLAT_WEIGHT_OVERRIDES,
    _RULE_QUALITY_MULTIPLIER,
    _SYNERGY_PAIRS,
    TensorRow,
    score_all_universal,
)


def _row(
    cmdr: str = "Cmdr",
    cand: str = "Card",
    rule_id: str = "rule_x",
    contribution: float = 0.5,
    idf: float = 0.5,
    count: int = 1,
) -> TensorRow:
    """Short factory for test TensorRow construction."""
    return TensorRow(
        commander=cmdr,
        candidate=cand,
        rule_id=rule_id,
        contribution=contribution,
        idf_weight=idf,
        raw_count=count,
    )


# ---------------------------------------------------------------------------
# config_hash
# ---------------------------------------------------------------------------


def test_config_hash_is_deterministic() -> None:
    """Same scoring config + two calls = same hash.

    Deterministic hashing is the whole basis of the staleness check;
    if the hash ever drifts run-to-run, every audit reads stale rows.
    """
    assert compute_config_hash() == compute_config_hash()


def test_config_hash_is_sha256_hex() -> None:
    """Output is a 64-character hex string."""
    h = compute_config_hash()
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_config_hash_changes_on_quality_multiplier_change() -> None:
    """Any edit to _RULE_QUALITY_MULTIPLIER flips the hash.

    Without this property the tensor silently serves pre-tune data
    after a multiplier sweep.
    """
    baseline = compute_config_hash()
    mutated = dict(_RULE_QUALITY_MULTIPLIER)
    # Pick an existing rule; change its multiplier by a delta unlikely
    # to round to the same float as the baseline value.
    rule = next(iter(mutated))
    mutated[rule] = mutated[rule] + 0.123456789
    with patch.dict(_RULE_QUALITY_MULTIPLIER, mutated, clear=True):
        assert compute_config_hash() != baseline


def test_config_hash_changes_on_flat_weight_change() -> None:
    """Any edit to _FLAT_WEIGHT_OVERRIDES also flips the hash."""
    baseline = compute_config_hash()
    mutated = dict(_FLAT_WEIGHT_OVERRIDES)
    rule = next(iter(mutated))
    mutated[rule] = mutated[rule] + 0.042
    with patch.dict(_FLAT_WEIGHT_OVERRIDES, mutated, clear=True):
        assert compute_config_hash() != baseline


def test_config_hash_changes_on_pair_bonus_change() -> None:
    """_SYNERGY_PAIRS tuning flips the hash too.

    Pair bonuses fire inside ``score()`` after the per-rule sum, so
    retuning them changes downstream scoring even when IDF weights are
    stable. Hashing _SYNERGY_PAIRS catches this class of staleness.
    """
    baseline = compute_config_hash()
    mutated = dict(_SYNERGY_PAIRS)
    pair = next(iter(mutated))
    mutated[pair] = mutated[pair] + 0.013
    with patch.dict(_SYNERGY_PAIRS, mutated, clear=True):
        assert compute_config_hash() != baseline


# ---------------------------------------------------------------------------
# TensorWriter basic behavior
# ---------------------------------------------------------------------------


def _minimal_fixture(path: Path) -> sqlite3.Connection:
    """Same shape as the identity-test fixture; kept local to avoid
    coupling two test modules to one conftest fixture."""
    conn = open_db(path)
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Token Maker", "Creature", "", 3, "R", 500, 1),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.commit()
    return conn


def test_writer_persists_rows_end_to_end(tmp_path: Path) -> None:
    """Happy path: score with writer.sink, query SQLite, find the rows."""
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        with TensorWriter(conn) as writer:
            score_all_universal(conn, ["Test Commander"], tensor_sink=writer.sink)
            hash_used = writer.config_hash

        rows = conn.execute(
            "SELECT commander, candidate, rule_id, contribution, idf_weight, "
            "raw_count, config_hash FROM rule_contributions WHERE commander = ?",
            ("Test Commander",),
        ).fetchall()
        assert len(rows) > 0
        for r in rows:
            assert r["commander"] == "Test Commander"
            assert r["config_hash"] == hash_used
            assert r["raw_count"] >= 1
            # Contribution may be negative for anti-synergy but never zero
            # (zero-contribution rows are filtered by the sink emitter).
            assert r["contribution"] != 0
    finally:
        conn.close()


def test_writer_commits_on_normal_exit(tmp_path: Path) -> None:
    """Context manager commits once, on clean exit."""
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        with TensorWriter(conn) as writer:
            writer.sink(_row())
        # After exit, another connection can see the row (autocommit mode
        # is off, so commit must have been explicit).
        conn2 = sqlite3.connect(str(tmp_path / "synergy.db"))
        try:
            row = conn2.execute(
                "SELECT commander FROM rule_contributions WHERE candidate = ?",
                ("Card",),
            ).fetchone()
            assert row is not None
        finally:
            conn2.close()
    finally:
        conn.close()


def test_writer_discards_on_exception(tmp_path: Path) -> None:
    """Error path: if the context exits via exception, pending rows are dropped."""
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        with pytest.raises(ValueError), TensorWriter(conn) as writer:
            writer.sink(_row())
            # Row is buffered, not yet flushed. Raising here must not
            # persist it.
            raise ValueError("simulated failure")

        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM rule_contributions WHERE candidate = ?",
            ("Card",),
        ).fetchone()
        assert rows["n"] == 0
    finally:
        conn.close()


def test_writer_flushes_batches_over_size(tmp_path: Path) -> None:
    """Integration: ≥ _BATCH_SIZE rows auto-flush mid-session.

    Exercises the buffer-drain path so long sessions don't OOM on a
    tensor with millions of rows.
    """
    # Import locally to keep the patch scoped to this test.
    from mtg_synergy_graph.bench import tensor as tensor_mod

    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        with patch.object(tensor_mod, "_BATCH_SIZE", 3), TensorWriter(conn) as writer:
            for i in range(10):
                writer.sink(_row(cand=f"Card{i}", contribution=0.1 + i * 0.01, idf=0.1))
            # 10 rows / batch of 3 → 3 auto-flushes happened before
            # close; remaining 1 row still buffered → flushed on exit.
            assert writer.rows_written >= 9
        # After exit, all 10 rows should be persisted.
        final = conn.execute(
            "SELECT COUNT(*) AS n FROM rule_contributions WHERE commander = ?",
            ("Cmdr",),
        ).fetchone()
        assert final["n"] == 10
    finally:
        conn.close()


def test_writer_cannot_record_after_close(tmp_path: Path) -> None:
    """Edge: explicit close disables further recording.

    Guards against "context manager leaked and the sink captured post-
    commit" bugs.
    """
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        writer = TensorWriter(conn)
        writer.close()
        with pytest.raises(RuntimeError):
            writer.sink(_row())
    finally:
        conn.close()


def test_writer_close_is_idempotent(tmp_path: Path) -> None:
    """Calling close() twice must not double-commit or raise."""
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        writer = TensorWriter(conn)
        writer.sink(_row())
        writer.close()
        writer.close()  # must not raise
    finally:
        conn.close()


def test_writer_upserts_on_duplicate_rows(tmp_path: Path) -> None:
    """Re-running with the same config_hash overwrites, not duplicates.

    ``INSERT OR REPLACE`` semantics prevent tensor bloat when a commander
    is re-scored (e.g., `--repin` rebuilds twice in a session).
    """
    conn = _minimal_fixture(tmp_path / "synergy.db")
    try:
        for _ in range(2):
            with TensorWriter(conn) as writer:
                writer.sink(_row(contribution=0.3, idf=0.3))

        row = conn.execute(
            "SELECT COUNT(*) AS n FROM rule_contributions "
            "WHERE commander = 'Cmdr' AND candidate = 'Card' AND rule_id = 'rule_x'"
        ).fetchone()
        assert row["n"] == 1
    finally:
        conn.close()
