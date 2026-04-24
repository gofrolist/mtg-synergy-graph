"""Unit 2 tests — card_embeddings blob store round-trip + fallback.

Covers:

* Round-trip: ``write_vectors`` + ``load_card_embeddings`` preserves
  bitwise-identical ``float32`` arrays.
* ``read_vector`` returns the exact array for known names, ``None``
  for missing names.
* Graceful-fallback reader:
  - missing table → ``{}`` + warning
  - empty table → ``{}`` + warning
  - all-zero row → row skipped, others kept
  - wrong-length row → row skipped, others kept
* Transaction atomicity: simulated mid-write exception leaves DB
  unchanged.
* Replace-all semantics: re-running with a smaller corpus wipes old
  rows rather than accumulating.
* Config KV rows are written alongside vectors; post-write
  ``verify_current_or_raise`` passes.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import store as emb_store


def _make_store_db(tmp_path: Path) -> sqlite3.Connection:
    """Open a connection with only the two embedding tables applied.

    Kept intentionally narrower than ``db.open_db`` so these tests do
    not couple to the full schema (which includes the FK from
    ``card_embeddings.card_name`` to ``cards(name)`` — irrelevant to
    the blob round-trip contract).
    """
    db_path = tmp_path / "emb.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE card_embeddings (
            card_name          TEXT PRIMARY KEY,
            vector             BLOB NOT NULL,
            vectorizer_version INTEGER NOT NULL,
            built_at           TEXT NOT NULL
        );
        CREATE TABLE card_embeddings_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    return conn


def _make_inputs() -> emb_config.EmbeddingConfigInputs:
    return emb_config.EmbeddingConfigInputs(
        token_format_version="v1",  # noqa: S106 — version tag, not a secret
        svd_dims=128,
        min_df=2,
        vectorizer_version=1,
        port_signature_version="v1",
    )


def _make_vector(seed: int) -> np.ndarray:
    """Deterministic 128-dim unit vector with a small offset per ``seed``.

    Not all-zero (so it survives ``load_card_embeddings`` filtering).
    """
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(128).astype(np.float32)
    # L2-normalize so it matches the downstream contract from Unit 1's
    # ``truncated_svd`` — every vector read at inference is unit-norm.
    vec /= np.linalg.norm(vec)
    return vec


# ---------------------------------------------------------------------------
# write_vectors + load_card_embeddings round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_bits(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        vectors = {
            "Lightning Bolt": _make_vector(1),
            "Counterspell": _make_vector(2),
            "Sol Ring": _make_vector(3),
        }
        emb_store.write_vectors(conn, vectors, _make_inputs())

        loaded = emb_store.load_card_embeddings(conn)
        assert set(loaded.keys()) == set(vectors.keys())
        for name, expected in vectors.items():
            assert np.array_equal(loaded[name], expected)
            # Dtype preserved.
            assert loaded[name].dtype == np.float32
    finally:
        conn.close()


def test_read_vector_returns_exact_float32(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        vectors = {"Lightning Bolt": _make_vector(1)}
        emb_store.write_vectors(conn, vectors, _make_inputs())

        loaded = emb_store.read_vector(conn, "Lightning Bolt")
        assert loaded is not None
        assert np.array_equal(loaded, vectors["Lightning Bolt"])
        assert loaded.dtype == np.float32
    finally:
        conn.close()


def test_read_vector_returns_none_for_missing_name(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        emb_store.write_vectors(conn, {"A": _make_vector(1)}, _make_inputs())
        assert emb_store.read_vector(conn, "Nonexistent Card") is None
    finally:
        conn.close()


def test_read_vector_returned_array_is_writable(tmp_path: Path) -> None:
    """``.copy()`` in read_vector means the caller can mutate in place.

    Without ``.copy()``, ``np.frombuffer`` returns a read-only view
    over the sqlite3 row buffer and any downstream mutation raises
    ``ValueError: assignment destination is read-only``.
    """
    conn = _make_store_db(tmp_path)
    try:
        emb_store.write_vectors(conn, {"A": _make_vector(1)}, _make_inputs())
        loaded = emb_store.read_vector(conn, "A")
        assert loaded is not None
        loaded[0] = 0.0  # must not raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Graceful-fallback reader contract
# ---------------------------------------------------------------------------


def test_load_returns_empty_on_missing_tables(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """DB that never had the embedding tables applied."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(db_path)
    try:
        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.store"):
            result = emb_store.load_card_embeddings(conn)
        assert result == {}
        assert any("missing" in rec.message or "missing" in rec.getMessage() for rec in caplog.records)
    finally:
        conn.close()


def test_load_returns_empty_on_empty_table(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = _make_store_db(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.store"):
            result = emb_store.load_card_embeddings(conn)
        assert result == {}
        assert any("empty" in rec.getMessage() for rec in caplog.records)
    finally:
        conn.close()


def test_load_skips_all_zero_row_and_keeps_valid_rows(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = _make_store_db(tmp_path)
    try:
        zero_blob = np.zeros(128, dtype=np.float32).tobytes()
        valid_vec = _make_vector(1)
        conn.executemany(
            "INSERT INTO card_embeddings(card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            [
                ("AllZero", zero_blob, 1, "2026-04-23T00:00:00+00:00"),
                ("Valid", valid_vec.tobytes(), 1, "2026-04-23T00:00:00+00:00"),
            ],
        )
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.store"):
            result = emb_store.load_card_embeddings(conn)

        assert "AllZero" not in result
        assert "Valid" in result
        assert np.array_equal(result["Valid"], valid_vec)
        assert any("AllZero" in rec.getMessage() for rec in caplog.records)
    finally:
        conn.close()


def test_load_skips_wrong_length_row_and_keeps_valid_rows(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    conn = _make_store_db(tmp_path)
    try:
        short_blob = np.ones(64, dtype=np.float32).tobytes()
        valid_vec = _make_vector(2)
        conn.executemany(
            "INSERT INTO card_embeddings(card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            [
                ("TooShort", short_blob, 1, "2026-04-23T00:00:00+00:00"),
                ("Valid", valid_vec.tobytes(), 1, "2026-04-23T00:00:00+00:00"),
            ],
        )
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.store"):
            result = emb_store.load_card_embeddings(conn)

        assert "TooShort" not in result
        assert "Valid" in result
        assert any("TooShort" in rec.getMessage() for rec in caplog.records)
    finally:
        conn.close()


def test_load_reads_expected_dim_from_stored_config(tmp_path: Path) -> None:
    """FU-1 regression: dim filter must come from stored config, not hardcode.

    If a future build uses ``--svd-dims 64`` and populates 64-float rows,
    ``load_card_embeddings`` should keep those rows, not silently skip
    them because of a hardcoded 128. This test simulates that scenario
    by populating a stored ``svd_dims=64`` KV row with matching 64-float
    blobs.
    """
    conn = _make_store_db(tmp_path)
    try:
        # Populate KV table claiming svd_dims=64 was the build target.
        conn.execute("INSERT INTO card_embeddings_config(key, value) VALUES ('svd_dims', '64')")
        # Write a 64-float vector (non-zero so the load filter keeps it).
        vec_64 = np.ones(64, dtype=np.float32).tobytes()
        conn.execute(
            "INSERT INTO card_embeddings(card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            ("SixtyFour", vec_64, 1, "2026-04-23T00:00:00+00:00"),
        )
        conn.commit()

        result = emb_store.load_card_embeddings(conn)

        assert "SixtyFour" in result
        assert len(result["SixtyFour"]) == 64
    finally:
        conn.close()


def test_load_falls_back_to_default_dim_when_no_config(tmp_path: Path) -> None:
    """No stored config → fall back to _DEFAULT_SVD_DIMS (128).

    Matches historical behavior so a DB that pre-dates Unit 2's
    config-hash discipline still loads correctly.
    """
    conn = _make_store_db(tmp_path)
    try:
        # Valid 128-float vector, no KV config row.
        valid_vec = _make_vector(99)
        conn.execute(
            "INSERT INTO card_embeddings(card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            ("Legacy", valid_vec.tobytes(), 1, "2026-04-23T00:00:00+00:00"),
        )
        conn.commit()

        result = emb_store.load_card_embeddings(conn)

        assert "Legacy" in result
        assert len(result["Legacy"]) == 128
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Atomicity + replace-all semantics
# ---------------------------------------------------------------------------


def test_write_vectors_is_atomic_on_mid_write_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash between the delete and the commit must leave DB unchanged."""
    conn = _make_store_db(tmp_path)
    try:
        # Seed an initial successful write so there is pre-existing state to
        # preserve across the failing second write.
        original = {"A": _make_vector(1), "B": _make_vector(2)}
        emb_store.write_vectors(conn, original, _make_inputs())

        # Monkeypatch ``compute_embedding_hash`` to raise during the second
        # write. This fires AFTER the DELETE but before the config KV
        # insert — exactly the mid-transaction failure mode that the
        # ``with conn:`` block must roll back.
        def exploding_hash(_inputs: emb_config.EmbeddingConfigInputs) -> str:
            raise RuntimeError("simulated mid-write failure")

        monkeypatch.setattr(emb_store, "compute_embedding_hash", exploding_hash)

        with pytest.raises(RuntimeError, match="simulated mid-write failure"):
            emb_store.write_vectors(conn, {"C": _make_vector(3)}, _make_inputs())

        # DB state is the pre-failure state.
        loaded = emb_store.load_card_embeddings(conn)
        assert set(loaded.keys()) == {"A", "B"}
        assert np.array_equal(loaded["A"], original["A"])
        assert np.array_equal(loaded["B"], original["B"])
    finally:
        conn.close()


def test_write_vectors_replaces_all_rows_not_incremental(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        first = {name: _make_vector(i) for i, name in enumerate(["A", "B", "C", "D", "E"], start=1)}
        emb_store.write_vectors(conn, first, _make_inputs())

        second = {name: _make_vector(i + 100) for i, name in enumerate(["X", "Y", "Z"], start=1)}
        emb_store.write_vectors(conn, second, _make_inputs())

        loaded = emb_store.load_card_embeddings(conn)
        assert set(loaded.keys()) == {"X", "Y", "Z"}

        # KV config table is also wiped + rewritten (not accumulated).
        config_rows = conn.execute("SELECT key FROM card_embeddings_config").fetchall()
        keys = [r[0] for r in config_rows]
        # Each key should appear exactly once; no duplicates from the second
        # write layering on top of the first.
        assert len(keys) == len(set(keys))
    finally:
        conn.close()


def test_write_vectors_populates_config_kv_and_verify_passes(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        inputs = _make_inputs()
        vectors = {"A": _make_vector(1)}
        emb_store.write_vectors(conn, vectors, inputs)

        stored = dict(conn.execute("SELECT key, value FROM card_embeddings_config").fetchall())
        # Hash + each diagnostic field.
        assert "config_hash" in stored
        assert len(stored["config_hash"]) == 64
        assert stored["token_format_version"] == "v1"  # noqa: S105 — version tag
        assert stored["svd_dims"] == "128"
        assert stored["min_df"] == "2"
        assert stored["vectorizer_version"] == "1"
        assert stored["port_signature_version"] == "v1"
        assert "built_at" in stored

        # Post-write, the strict verifier passes.
        emb_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_write_vectors_stamps_vectorizer_version_per_row(tmp_path: Path) -> None:
    conn = _make_store_db(tmp_path)
    try:
        inputs = _make_inputs()._replace(vectorizer_version=7)
        vectors = {"A": _make_vector(1), "B": _make_vector(2)}
        emb_store.write_vectors(conn, vectors, inputs)

        versions = [row[0] for row in conn.execute("SELECT vectorizer_version FROM card_embeddings").fetchall()]
        assert versions == [7, 7]
    finally:
        conn.close()
