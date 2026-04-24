"""Unit 2 tests — EmbeddingConfigInputs + hash + verify_current_or_raise.

Mirrors ``tests/test_forge_oracle_config_hash.py`` for the embedding
pipeline. The meta-principle carried over from
``docs/solutions/best-practices/offline-oracle-hash-pattern-2026-04-23.md``
is: mechanically enforce the subsystem invariant via a hash that
refuses stale comparisons.

Covers:

* ``compute_embedding_hash`` — determinism across repeated calls,
  sensitivity to each field, stability across positional vs kwargs
  construction.
* ``verify_current_or_raise`` — passes on match, raises Stale on
  mismatch (with rebuild-command text), raises Missing on absent row.
* ``get_embedding_config_inputs`` — keeps ``token_format_version`` in
  sync with ``embeddings.vectorizer.TOKEN_FORMAT_VERSION``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import vectorizer as emb_vectorizer


def _make_inputs(**overrides: object) -> emb_config.EmbeddingConfigInputs:
    """Helper: build a valid ``EmbeddingConfigInputs`` with defaults."""
    base: dict[str, object] = {
        "token_format_version": "v1",
        "svd_dims": 128,
        "min_df": 2,
        "vectorizer_version": 1,
        "port_signature_version": "v1",
    }
    base.update(overrides)
    return emb_config.EmbeddingConfigInputs(**base)  # type: ignore[arg-type]


def _make_kv_db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in-memory-like DB with the minimal KV table."""
    db_path = tmp_path / "emb.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE card_embeddings_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    return conn


# ---------------------------------------------------------------------------
# compute_embedding_hash — determinism + sensitivity
# ---------------------------------------------------------------------------


def test_hash_is_deterministic_across_calls() -> None:
    inputs = _make_inputs()
    h1 = emb_config.compute_embedding_hash(inputs)
    h2 = emb_config.compute_embedding_hash(inputs)
    assert h1 == h2
    # SHA-256 hex digest is 64 chars.
    assert len(h1) == 64


def test_hash_flips_when_token_format_version_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(token_format_version="v2")  # noqa: S106 — version tag, not a secret
    assert emb_config.compute_embedding_hash(base) != emb_config.compute_embedding_hash(bumped)


def test_hash_flips_when_svd_dims_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(svd_dims=64)
    assert emb_config.compute_embedding_hash(base) != emb_config.compute_embedding_hash(bumped)


def test_hash_flips_when_min_df_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(min_df=3)
    assert emb_config.compute_embedding_hash(base) != emb_config.compute_embedding_hash(bumped)


def test_hash_flips_when_vectorizer_version_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(vectorizer_version=2)
    assert emb_config.compute_embedding_hash(base) != emb_config.compute_embedding_hash(bumped)


def test_hash_flips_when_port_signature_version_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(port_signature_version="v2")
    assert emb_config.compute_embedding_hash(base) != emb_config.compute_embedding_hash(bumped)


def test_hash_stable_across_positional_vs_kwargs_construction() -> None:
    """Sorting ``_asdict()`` items guarantees field-order-independence.

    Construct the tuple two ways — positional and kwargs — and verify
    both produce the same hash. This locks in the sort-before-hash
    invariant that protects against accidental field-order changes in
    the NamedTuple definition.
    """
    positional = emb_config.EmbeddingConfigInputs(
        "v1",  # token_format_version
        128,  # svd_dims
        2,  # min_df
        1,  # vectorizer_version
        "v1",  # port_signature_version
    )
    kwargs = emb_config.EmbeddingConfigInputs(
        port_signature_version="v1",
        vectorizer_version=1,
        min_df=2,
        svd_dims=128,
        token_format_version="v1",  # noqa: S106 — version tag, not a secret
    )
    assert emb_config.compute_embedding_hash(positional) == emb_config.compute_embedding_hash(kwargs)


# ---------------------------------------------------------------------------
# verify_current_or_raise — refuse-to-run contract
# ---------------------------------------------------------------------------


def test_verify_passes_when_hash_matches(tmp_path: Path) -> None:
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        expected = emb_config.compute_embedding_hash(inputs)
        conn.execute(
            "INSERT INTO card_embeddings_config(key, value) VALUES ('config_hash', ?)",
            (expected,),
        )
        conn.commit()
        # Must not raise.
        emb_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_verify_raises_stale_on_hash_mismatch(tmp_path: Path) -> None:
    conn = _make_kv_db(tmp_path)
    try:
        conn.execute("INSERT INTO card_embeddings_config(key, value) VALUES ('config_hash', 'stale_value_xxxx')")
        conn.commit()
        inputs = _make_inputs()
        with pytest.raises(emb_config.EmbeddingConfigStaleError) as exc_info:
            emb_config.verify_current_or_raise(conn, inputs)
        message = str(exc_info.value)
        # Message must include both stored and current hashes + rebuild cmd.
        assert "stale_value" in message
        current_hash = emb_config.compute_embedding_hash(inputs)
        assert current_hash[:12] in message
        assert "uv run scripts/build_embeddings.py" in message
    finally:
        conn.close()


def test_verify_raises_missing_when_hash_row_absent(tmp_path: Path) -> None:
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        with pytest.raises(emb_config.EmbeddingConfigMissingError):
            emb_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_stale_and_missing_inherit_from_common_base() -> None:
    """Catching the base class subsumes both failure modes.

    Inference-path callers rely on this to degrade gracefully without
    type-matching every subclass.
    """
    assert issubclass(emb_config.EmbeddingConfigStaleError, emb_config.EmbeddingConfigError)
    assert issubclass(emb_config.EmbeddingConfigMissingError, emb_config.EmbeddingConfigError)


# ---------------------------------------------------------------------------
# get_embedding_config_inputs — ambient-config helper
# ---------------------------------------------------------------------------


def test_get_embedding_config_inputs_matches_vectorizer_version() -> None:
    inputs = emb_config.get_embedding_config_inputs()
    assert inputs.token_format_version == emb_vectorizer.TOKEN_FORMAT_VERSION
    # Defaults from plan D1 + Unit 1.
    assert inputs.svd_dims == 128
    assert inputs.min_df == 2
    assert inputs.vectorizer_version == 1
    assert inputs.port_signature_version == "v1"
