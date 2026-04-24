"""Blob round-trip helpers for ``card_embeddings`` (plan 003 Unit 2).

Writes a full vector corpus into SQLite under a single transaction
(replace-all semantics: a rebuild is always wholesale; incremental
updates would leave ``vectorizer_version`` drift hazards on old rows).

Reads follow the graceful-fallback contract from
``src/mtg_synergy_graph/forge_oracle/gap_weight.py:load_forge_signals``:

* Missing table / ``sqlite3.DatabaseError`` / empty table / all-zero
  blobs / wrong-length blobs → excluded from the result (with
  ``logging.warning``).
* Never raises from the inference path.

The config-hash check is delegated to
``embeddings.config.verify_current_or_raise``. The store helpers
themselves do not verify — that's the caller's responsibility so
offline strict consumers (Unit 6) can re-raise while the inference
reader (Unit 5) can degrade.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 2.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

import numpy as np

from mtg_synergy_graph.embeddings.config import (
    _DEFAULT_SVD_DIMS,
    EmbeddingConfigInputs,
    compute_embedding_hash,
)

logger = logging.getLogger(__name__)


def _read_expected_dim(conn: sqlite3.Connection) -> int:
    """Return the ``svd_dims`` that stored rows should match.

    Reads from ``card_embeddings_config`` so a non-default build (e.g.,
    ``--svd-dims 64``) doesn't get silently filtered out by a hardcoded
    128. Falls back to :data:`_DEFAULT_SVD_DIMS` when the KV row is
    absent (pre-Unit-2 DB) — matches the historical behavior.

    Never raises: table-missing, key-missing, and malformed-int all
    degrade to the default. The caller (``load_card_embeddings``) is a
    graceful-fallback function and must not crash on a partially-built
    DB.
    """
    try:
        row = conn.execute("SELECT value FROM card_embeddings_config WHERE key = 'svd_dims'").fetchone()
    except sqlite3.OperationalError:
        return _DEFAULT_SVD_DIMS
    if row is None:
        return _DEFAULT_SVD_DIMS
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return _DEFAULT_SVD_DIMS


def write_vectors(
    conn: sqlite3.Connection,
    vectors: dict[str, np.ndarray],
    inputs: EmbeddingConfigInputs,
) -> None:
    """Replace-all write of ``vectors`` + config KV rows in one transaction.

    Steps (inside ``with conn:`` so the whole write is atomic):

    1. ``DELETE FROM card_embeddings`` — wholesale rebuild. No incremental
       path; a partial rebuild would leave ``vectorizer_version`` drift on
       old rows.
    2. Insert one ``card_embeddings`` row per ``(name, vector)``. Vectors
       are cast to ``float32`` before ``.tobytes()`` so storage is
       deterministic regardless of the caller's dtype.
    3. ``DELETE FROM card_embeddings_config`` + insert ``config_hash``
       plus one diagnostic row per ``EmbeddingConfigInputs`` field.

    Raises on any failure; the ``with conn:`` block rolls back so the
    DB is left untouched.
    """
    now_iso = datetime.now(UTC).isoformat()
    hash_value = compute_embedding_hash(inputs)

    with conn:
        conn.execute("DELETE FROM card_embeddings")
        rows = [
            (
                name,
                # '<f4' = little-endian float32 for cross-arch portability (plan 003 DM-001)
                np.asarray(vector, dtype="<f4").tobytes(),
                int(inputs.vectorizer_version),
                now_iso,
            )
            for name, vector in vectors.items()
        ]
        conn.executemany(
            "INSERT INTO card_embeddings (card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            rows,
        )

        conn.execute("DELETE FROM card_embeddings_config")
        kv_rows: list[tuple[str, str]] = [
            ("config_hash", hash_value),
            ("token_format_version", inputs.token_format_version),
            ("svd_dims", str(inputs.svd_dims)),
            ("min_df", str(inputs.min_df)),
            ("vectorizer_version", str(inputs.vectorizer_version)),
            ("port_signature_version", inputs.port_signature_version),
            ("built_at", now_iso),
        ]
        conn.executemany(
            "INSERT INTO card_embeddings_config(key, value) VALUES (?, ?)",
            kv_rows,
        )


def read_vector(conn: sqlite3.Connection, name: str) -> np.ndarray | None:
    """Return the ``float32`` vector for ``name``, or ``None`` if absent.

    Does not validate length or zero-ness — callers that need those
    guarantees should use ``load_card_embeddings``. This helper is a
    thin single-row lookup primarily for tests and the ``--explain``
    nearest-neighbor renderer (Unit 7).
    """
    row = conn.execute(
        "SELECT vector FROM card_embeddings WHERE card_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    blob = row[0]
    # '<f4' = little-endian float32 for cross-arch portability (plan 003 DM-001)
    return np.frombuffer(blob, dtype="<f4").copy()


def load_card_embeddings(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
    """Return ``{card_name: ndarray(128,)}``; never raises.

    Graceful-fallback contract (mirrors
    ``forge_oracle/gap_weight.py:load_forge_signals``):

    * Missing table (``sqlite3.OperationalError``) → ``{}`` + warning.
    * ``sqlite3.DatabaseError`` on read → ``{}`` + warning.
    * Empty table → ``{}`` + warning.
    * Per-row: all-zero blob → skipped + warning.
    * Per-row: wrong-length blob (not 128 × 4 bytes) → skipped + warning.

    Vectors are returned as **writable** ``float32`` arrays
    (``np.frombuffer(...).copy()``) so callers that want to add noise or
    rescale in-place don't trip the read-only-buffer restriction.
    """
    try:
        rows = conn.execute("SELECT card_name, vector FROM card_embeddings").fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("card_embeddings table missing; returning empty dict: %s", exc)
        return {}
    except sqlite3.DatabaseError as exc:
        logger.warning("card_embeddings read failed: %s", exc)
        return {}

    if not rows:
        logger.warning("card_embeddings table is empty; returning empty dict")
        return {}

    expected_dim = _read_expected_dim(conn)

    out: dict[str, np.ndarray] = {}
    for name, blob in rows:
        try:
            # '<f4' = little-endian float32 for cross-arch portability (plan 003 DM-001)
            vector = np.frombuffer(blob, dtype="<f4").copy()
        except (ValueError, TypeError) as exc:
            logger.warning("skipping %s: blob decode failed: %s", name, exc)
            continue
        if len(vector) != expected_dim:
            logger.warning(
                "skipping %s: vector length %d != expected %d",
                name,
                len(vector),
                expected_dim,
            )
            continue
        if not vector.any():
            logger.warning("skipping %s: all-zero vector", name)
            continue
        out[name] = vector
    return out
