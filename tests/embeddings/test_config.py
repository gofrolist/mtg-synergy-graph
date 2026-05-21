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
        "vocab_version": "v1",
    }
    base.update(overrides)
    return emb_config.EmbeddingConfigInputs(**base)  # type: ignore[arg-type]


def _make_kv_db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in-memory-like DB with the minimal KV table."""
    db_path = tmp_path / "emb.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE card_embeddings_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    return conn


def _populate_kv(
    conn: sqlite3.Connection,
    inputs: emb_config.EmbeddingConfigInputs,
    *,
    override_hash: str | None = None,
) -> None:
    """Write all required KV rows for ``inputs`` into ``card_embeddings_config``.

    When ``override_hash`` is given, stores that value instead of the
    correct computed hash — the seam for corruption-test setup.
    """
    hash_value = override_hash if override_hash is not None else emb_config.compute_embedding_hash(inputs)
    kv_rows = [
        ("config_hash", hash_value),
        ("token_format_version", inputs.token_format_version),
        ("svd_dims", str(inputs.svd_dims)),
        ("min_df", str(inputs.min_df)),
        ("vectorizer_version", str(inputs.vectorizer_version)),
        ("vocab_version", inputs.vocab_version),
    ]
    conn.executemany(
        "INSERT INTO card_embeddings_config(key, value) VALUES (?, ?)",
        kv_rows,
    )
    conn.commit()


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


def test_hash_flips_when_vocab_version_changes() -> None:
    base = _make_inputs()
    bumped = base._replace(vocab_version="v2")
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
        "v1",  # vocab_version
    )
    kwargs = emb_config.EmbeddingConfigInputs(
        vocab_version="v1",
        vectorizer_version=1,
        min_df=2,
        svd_dims=128,
        token_format_version="v1",  # noqa: S106 — version tag, not a secret
    )
    assert emb_config.compute_embedding_hash(positional) == emb_config.compute_embedding_hash(kwargs)


# ---------------------------------------------------------------------------
# verify_current_or_raise — refuse-to-run contract
# ---------------------------------------------------------------------------


def test_verify_passes_when_stored_matches_current(tmp_path: Path) -> None:
    """Happy path: full KV rows + correct hash + matching inputs → no raise."""
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        _populate_kv(conn, inputs)
        emb_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_verify_raises_stale_when_stored_inputs_diverge_from_code(tmp_path: Path) -> None:
    """FU-1 regression guard: non-default build flags must not be silently stale.

    Build wrote ``min_df=3`` and stored the correct hash for that. Code
    defaults expect ``min_df=2``. Staleness check must fire with a
    field-naming message — not return ``{}`` silently, not raise Corrupt.
    """
    conn = _make_kv_db(tmp_path)
    try:
        stored_inputs = _make_inputs(min_df=3)  # simulate --min-df 3 build
        _populate_kv(conn, stored_inputs)
        current_code_inputs = _make_inputs(min_df=2)  # current code default
        with pytest.raises(emb_config.EmbeddingConfigStaleError) as exc_info:
            emb_config.verify_current_or_raise(conn, current_code_inputs)
        message = str(exc_info.value)
        # Message names the diverging field so a developer sees it immediately.
        assert "min_df" in message
        assert "stored=3" in message
        assert "current=2" in message
        assert "uv run scripts/build_embeddings.py" in message
    finally:
        conn.close()


def test_verify_raises_stale_when_stored_vocab_version_is_v2(
    tmp_path: Path,
) -> None:
    """Specific guard for the VOCAB v2 → v3 bump (commit 9677097).

    Wiring ``EmbeddingConfigInputs.vocab_version`` to
    ``port_graph.vocabulary.VOCAB_VERSION`` was added so that an
    embedding artifact built under VOCAB v2 is rejected when current
    code is on VOCAB v3. The other tests in this file use synthetic
    ``"v1"`` / ``"v2"`` prefixed values, which don't exercise the
    bare-integer format real stored DBs hold. This test pins the
    real-world rejection path: stored value ``"2"``, current code at
    ``VOCAB_VERSION="3"``, expect Stale with ``vocab_version``
    named.
    """
    from mtg_synergy_graph.port_graph import vocabulary as port_vocab

    # Skip if a future VOCAB bump lands on "2" — the simulated v2-stored
    # artifact would no longer be stale. (The bump is part of any such
    # change's own re-pin work, which is what we're guarding against.)
    if port_vocab.VOCAB_VERSION == "2":
        pytest.skip("VOCAB_VERSION reverted to 2; v2→current-code rejection not applicable")

    conn = _make_kv_db(tmp_path)
    try:
        stored_inputs = _make_inputs(vocab_version="2")
        _populate_kv(conn, stored_inputs)
        current_code_inputs = _make_inputs(vocab_version=port_vocab.VOCAB_VERSION)
        with pytest.raises(emb_config.EmbeddingConfigStaleError) as exc_info:
            emb_config.verify_current_or_raise(conn, current_code_inputs)
        message = str(exc_info.value)
        assert "vocab_version" in message
        assert "stored='2'" in message
        assert f"current='{port_vocab.VOCAB_VERSION}'" in message
        assert "uv run scripts/build_embeddings.py" in message
    finally:
        conn.close()


def test_verify_raises_corrupt_when_stored_hash_contradicts_rows(tmp_path: Path) -> None:
    """Partial-write simulation: rows were written but hash wasn't updated."""
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        _populate_kv(conn, inputs, override_hash="a" * 64)  # wrong but well-formed hash
        with pytest.raises(emb_config.EmbeddingConfigCorruptError) as exc_info:
            emb_config.verify_current_or_raise(conn, inputs)
        message = str(exc_info.value)
        correct_hash = emb_config.compute_embedding_hash(inputs)
        assert "aaaaaaaaaaaa" in message  # stored hash truncated prefix
        assert correct_hash[:12] in message  # recomputed hash truncated prefix
        assert "uv run scripts/build_embeddings.py" in message
    finally:
        conn.close()


def test_verify_raises_missing_when_kv_table_empty(tmp_path: Path) -> None:
    """Empty KV table → Missing, not Corrupt or Stale."""
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        with pytest.raises(emb_config.EmbeddingConfigMissingError):
            emb_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_verify_raises_missing_when_required_key_absent(tmp_path: Path) -> None:
    """Only ``config_hash`` present (pre-Unit-2 style DB) → Missing."""
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        expected = emb_config.compute_embedding_hash(inputs)
        conn.execute(
            "INSERT INTO card_embeddings_config(key, value) VALUES ('config_hash', ?)",
            (expected,),
        )
        conn.commit()
        with pytest.raises(emb_config.EmbeddingConfigMissingError) as exc_info:
            emb_config.verify_current_or_raise(conn, inputs)
        assert "missing required keys" in str(exc_info.value)
    finally:
        conn.close()


def test_verify_raises_corrupt_when_svd_dims_malformed(tmp_path: Path) -> None:
    """Non-integer svd_dims KV value → Corrupt. The row *exists* (so
    Missing is wrong) but its stored value is structurally broken —
    the exact failure class ``EmbeddingConfigCorruptError`` is designed
    to signal."""
    conn = _make_kv_db(tmp_path)
    try:
        inputs = _make_inputs()
        _populate_kv(conn, inputs)
        # Corrupt the int field after populating valid rows.
        conn.execute("UPDATE card_embeddings_config SET value = 'not_an_int' WHERE key = 'svd_dims'")
        conn.commit()
        with pytest.raises(emb_config.EmbeddingConfigCorruptError) as exc_info:
            emb_config.verify_current_or_raise(conn, inputs)
        assert "malformed" in str(exc_info.value)
    finally:
        conn.close()


def test_verify_raises_missing_when_kv_table_does_not_exist(tmp_path: Path) -> None:
    """No ``card_embeddings_config`` table at all → Missing (graceful)."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(db_path)
    try:
        inputs = _make_inputs()
        with pytest.raises(emb_config.EmbeddingConfigMissingError) as exc_info:
            emb_config.verify_current_or_raise(conn, inputs)
        assert "table is missing" in str(exc_info.value)
    finally:
        conn.close()


def test_all_error_classes_inherit_from_common_base() -> None:
    """Catching the base class subsumes all three failure modes.

    Inference-path callers (``load_card_embeddings_verified``) rely on
    this to degrade gracefully without type-matching every subclass.
    """
    assert issubclass(emb_config.EmbeddingConfigStaleError, emb_config.EmbeddingConfigError)
    assert issubclass(emb_config.EmbeddingConfigMissingError, emb_config.EmbeddingConfigError)
    assert issubclass(emb_config.EmbeddingConfigCorruptError, emb_config.EmbeddingConfigError)


def test_read_stored_config_returns_stored_not_current(tmp_path: Path) -> None:
    """``read_stored_config`` must return the artifact's build-time inputs.

    Provenance guarantee: the helper reflects what was written, not
    what ``get_embedding_config_inputs()`` returns right now.
    """
    conn = _make_kv_db(tmp_path)
    try:
        stored_inputs = _make_inputs(min_df=3, svd_dims=64, vectorizer_version=2)
        _populate_kv(conn, stored_inputs)
        read_inputs, read_hash = emb_config.read_stored_config(conn)
        assert read_inputs == stored_inputs
        assert read_hash == emb_config.compute_embedding_hash(stored_inputs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_embedding_config_inputs — ambient-config helper
# ---------------------------------------------------------------------------


def test_get_embedding_config_inputs_matches_vectorizer_version() -> None:
    from mtg_synergy_graph.port_graph import vocabulary as port_vocab

    inputs = emb_config.get_embedding_config_inputs()
    assert inputs.token_format_version == emb_vectorizer.TOKEN_FORMAT_VERSION
    # Defaults from plan D1 + Unit 1.
    assert inputs.svd_dims == 128
    assert inputs.min_df == 2
    assert inputs.vectorizer_version == 1
    # vocab_version now tracks VOCAB_VERSION so a vocabulary bump
    # invalidates stored embeddings (FU-follow-on from Phase A).
    assert inputs.vocab_version == port_vocab.VOCAB_VERSION
