"""Unit 5 tests — inference-path reader + graceful-fallback contract.

Covers the ``embedding_contribution`` pure-function contract and the
``load_card_embeddings_verified`` wrapper's failure-mode taxonomy:

* Flag gating — default ``False`` always returns ``0.0``; only when
  toggled ON do the downstream computations run.
* Happy paths — cosine * decay * w_emb math with known inputs.
* Short-circuit edge cases — ``v_cmdr is None`` / candidate missing
  from vectors → ``0.0``.
* Exponential decay — N_rules≥large → contribution dampens toward
  zero (no discontinuity).
* Verified loader — config-hash match returns vectors; missing hash /
  stale hash / missing table / DB error → ``{}`` + warning, never
  raises.
* Integration — end-to-end wiring from populated DB through verified
  load → commander target → contribution.

Plan: docs/plans/2026-04-23-003-feat-content-embeddings-fallback-plan.md Unit 5.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from mtg_synergy_graph.embeddings import config as emb_config
from mtg_synergy_graph.embeddings import contribution as emb_contribution
from mtg_synergy_graph.embeddings import store as emb_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unit_vector(seed: int, dim: int = 128) -> np.ndarray:
    """Deterministic L2-normalized float32 vector of a given dim."""
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(dim).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    assert norm > 0.0
    return (vec / norm).astype(np.float32)


def _make_parallel_pair(seed: int, cosine: float, dim: int = 128) -> tuple[np.ndarray, np.ndarray]:
    """Return two L2-unit vectors whose dot product equals ``cosine``.

    Constructed by mixing a shared direction with an orthogonal
    correction — exact cosine (within float32 eps), no Gram-Schmidt
    surprises.
    """
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(dim).astype(np.float32)
    base /= np.linalg.norm(base)

    other = rng.standard_normal(dim).astype(np.float32)
    # Remove the component of other along base.
    other -= float(other @ base) * base
    other /= np.linalg.norm(other)
    # Now base and other are orthonormal. Build target = c*base + sqrt(1-c^2)*other.
    target = cosine * base + math.sqrt(1.0 - cosine * cosine) * other
    target = target.astype(np.float32)
    target /= np.linalg.norm(target)
    return base, target


def _make_store_db(tmp_path: Path) -> sqlite3.Connection:
    """Open a connection with only the two embedding tables applied."""
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


def _make_inputs_matching_current() -> emb_config.EmbeddingConfigInputs:
    """Build inputs that match ``get_embedding_config_inputs()`` exactly.

    This ensures ``verify_current_or_raise`` passes post-write.
    """
    return emb_config.get_embedding_config_inputs()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_contribution_flag_on_w_zero_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag on + w_emb=0.0 → 0.0 regardless of cosine/decay.

    Scales the flip knob independently of the gate — this test guards
    the audit sweep's ability to land ``_ENABLE_EMBEDDING_CONTRIBUTION
    = True`` with ``w_emb = 0.0`` as an identity-preserving step.
    """
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.0)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr = _make_unit_vector(seed=1)
    v_cand = _make_unit_vector(seed=2)

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand},
    )

    assert result == 0.0


def test_contribution_flag_on_n_rules_zero_returns_w_times_cosine(monkeypatch: pytest.MonkeyPatch) -> None:
    """N_rules=0 → decay = 1.0 → contribution = w_emb * cosine."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr, v_cand = _make_parallel_pair(seed=3, cosine=0.5)

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand},
    )

    # decay = exp(0) = 1.0; result = 0.5 * 1.0 * 0.5 = 0.25.
    assert result == pytest.approx(0.25, abs=1e-5)


def test_contribution_flag_on_n_rules_five_dampened(monkeypatch: pytest.MonkeyPatch) -> None:
    """N_rules=5 → decay ~ exp(-4) → contribution dampened ~50×."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr, v_cand = _make_parallel_pair(seed=4, cosine=0.5)

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=5,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand},
    )

    expected = 0.5 * math.exp(-0.8 * 5) * 0.5
    assert result == pytest.approx(expected, abs=1e-5)
    # Sanity: well below the N_rules=0 contribution.
    assert abs(result) < 0.01


def test_contribution_default_flag_off_returns_zero() -> None:
    """Module default is flag=False, w=0.0 → always returns 0.0.

    No monkeypatching — exercise the shipped module state directly to
    prove the safety contract of Unit 5 (the function is shippable in
    isolation; all short-circuit paths return 0.0 until Unit 7's
    audit-gated flip).
    """
    assert emb_contribution._ENABLE_EMBEDDING_CONTRIBUTION is False
    v_cmdr = _make_unit_vector(seed=5)
    v_cand = _make_unit_vector(seed=6)

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand},
    )

    assert result == 0.0


# ---------------------------------------------------------------------------
# Short-circuit edge cases
# ---------------------------------------------------------------------------


def test_contribution_v_cmdr_none_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """v_cmdr=None (no golden-set commander) → 0.0."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cand = _make_unit_vector(seed=7)
    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=2,
        v_cmdr=None,
        vectors={"X": v_cand},
    )
    assert result == 0.0


def test_contribution_candidate_not_in_vectors_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Candidate name not in vectors dict → 0.0."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr = _make_unit_vector(seed=8)
    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={},
    )
    assert result == 0.0


def test_contribution_nan_cosine_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """FU-4 guard: NaN cosine must not propagate into the score.

    Constructs a degenerate vector pair that yields NaN via
    ``v_cand @ v_cmdr`` (NaN in ``v_cand`` fans out through the dot).
    Without the guard, ``0.5 * decay * NaN == NaN`` would contaminate
    the final score and break Python's sort ordering.
    """
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr = _make_unit_vector(seed=30)
    # Inject a NaN component — this can happen if an upstream bug emits
    # a degenerate vector (zero-norm division, inf from a bad TF-IDF
    # row, etc.). The guard must short-circuit to 0.0.
    v_cand_bad = np.ones(128, dtype=np.float32)
    v_cand_bad[0] = float("nan")

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand_bad},
    )
    assert result == 0.0


def test_contribution_inf_cosine_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """FU-4 guard: ±inf cosine must also short-circuit to 0.0."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr = _make_unit_vector(seed=31)
    v_cand_bad = np.zeros(128, dtype=np.float32)
    v_cand_bad[0] = float("inf")

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=0,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand_bad},
    )
    assert result == 0.0


def test_contribution_n_rules_large_dampens_to_near_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """N_rules=20 → exp(-0.8*20) ~ 1.1e-7 → near-zero contribution."""
    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 1.0)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    v_cmdr, v_cand = _make_parallel_pair(seed=9, cosine=1.0)

    result = emb_contribution.embedding_contribution(
        candidate_name="X",
        n_rules=20,
        v_cmdr=v_cmdr,
        vectors={"X": v_cand},
    )
    assert abs(result) < 1e-5


# ---------------------------------------------------------------------------
# load_card_embeddings_verified tests
# ---------------------------------------------------------------------------


def test_load_verified_happy_path(tmp_path: Path) -> None:
    """Populated DB with matching config_hash → full vectors dict."""
    conn = _make_store_db(tmp_path)
    try:
        inputs = _make_inputs_matching_current()
        vectors = {
            "A": _make_unit_vector(seed=10),
            "B": _make_unit_vector(seed=11),
        }
        emb_store.write_vectors(conn, vectors, inputs)

        loaded = emb_contribution.load_card_embeddings_verified(conn)

        assert set(loaded.keys()) == {"A", "B"}
        np.testing.assert_array_equal(loaded["A"], vectors["A"])
        np.testing.assert_array_equal(loaded["B"], vectors["B"])
    finally:
        conn.close()


def test_load_verified_missing_config_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """DB has vectors but no config_hash row → {} + warning."""
    conn = _make_store_db(tmp_path)
    try:
        # Insert vectors directly without touching card_embeddings_config.
        valid_vec = _make_unit_vector(seed=12)
        conn.execute(
            "INSERT INTO card_embeddings(card_name, vector, vectorizer_version, built_at) VALUES (?, ?, ?, ?)",
            ("A", valid_vec.tobytes(), 1, "2026-04-23T00:00:00+00:00"),
        )
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.contribution"):
            result = emb_contribution.load_card_embeddings_verified(conn)

        assert result == {}
        # Warning message mentions the config KV table / rebuild hint.
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "config_hash" in combined or "card_embeddings_config" in combined
    finally:
        conn.close()


def test_load_verified_stale_config_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """DB has vectors + wrong config_hash → {} + warning."""
    conn = _make_store_db(tmp_path)
    try:
        # Write with one set of inputs, then corrupt the stored hash so
        # the next verify treats it as stale.
        inputs = _make_inputs_matching_current()
        emb_store.write_vectors(conn, {"A": _make_unit_vector(seed=13)}, inputs)

        conn.execute("UPDATE card_embeddings_config SET value = 'not_the_real_hash' WHERE key = 'config_hash'")
        conn.commit()

        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.contribution"):
            result = emb_contribution.load_card_embeddings_verified(conn)

        assert result == {}
        # Warning includes hash truncation and / or rebuild command.
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert ("not_the_real" in combined) or ("build_embeddings" in combined) or ("Rebuild" in combined)
    finally:
        conn.close()


def test_load_verified_missing_table_returns_empty(tmp_path: Path) -> None:
    """DB without card_embeddings table → {} (already handled by store)."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(db_path)
    try:
        result = emb_contribution.load_card_embeddings_verified(conn)
        assert result == {}
    finally:
        conn.close()


def test_load_verified_never_raises_on_db_error(tmp_path: Path) -> None:
    """Pathological DB (closed connection) → {} with no exception escaping."""
    conn = _make_store_db(tmp_path)
    # Write + commit some data so the store sees rows, then close the
    # connection to induce DatabaseError on subsequent reads.
    emb_store.write_vectors(
        conn,
        {"A": _make_unit_vector(seed=14)},
        _make_inputs_matching_current(),
    )
    conn.close()

    # load_card_embeddings_verified on a closed connection must not
    # raise — graceful degradation is the whole contract.
    result = emb_contribution.load_card_embeddings_verified(conn)
    assert result == {}


def test_load_verified_database_error_on_verify_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sqlite3.DatabaseError raised by verify_current_or_raise → {} + warning.

    Exercises the defensive catch in ``load_card_embeddings_verified``
    — the store happy-path returns vectors, then verify trips a
    ``DatabaseError`` (simulated via monkeypatch) and must degrade.
    """
    conn = _make_store_db(tmp_path)
    try:
        emb_store.write_vectors(
            conn,
            {"A": _make_unit_vector(seed=15)},
            _make_inputs_matching_current(),
        )

        def exploding_verify(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.DatabaseError("simulated verify DB error")

        monkeypatch.setattr(
            "mtg_synergy_graph.embeddings.contribution.emb_config.verify_current_or_raise",
            exploding_verify,
        )

        with caplog.at_level(logging.WARNING, logger="mtg_synergy_graph.embeddings.contribution"):
            result = emb_contribution.load_card_embeddings_verified(conn)

        assert result == {}
        combined = " ".join(rec.getMessage() for rec in caplog.records)
        assert "simulated verify DB error" in combined
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_contribution_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Populated DB → verified load → commander target → contribution (flag ON)."""
    from mtg_synergy_graph.embeddings import commander_target as ct

    monkeypatch.setattr(emb_contribution, "_ENABLE_EMBEDDING_CONTRIBUTION", True)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_W", 0.5)
    monkeypatch.setattr(emb_contribution, "_EMBEDDING_K", 0.8)

    conn = _make_store_db(tmp_path)
    try:
        inputs = _make_inputs_matching_current()
        commander = "Korvold, Fae-Cursed King"
        candidate = "Dockside Extortionist"
        vectors_on_disk = {
            commander: _make_unit_vector(seed=20),
            candidate: _make_unit_vector(seed=21),
            "Unrelated Card": _make_unit_vector(seed=22),
        }
        emb_store.write_vectors(conn, vectors_on_disk, inputs)

        # Verified load.
        vectors = emb_contribution.load_card_embeddings_verified(conn)
        assert commander in vectors
        assert candidate in vectors

        # Commander target without EDHREC conn — falls back to cmdr vec.
        ct.clear_cache()
        v_cmdr = ct.build_commander_target_vector(
            (commander,),
            vectors,
            edhrec_conn=None,
        )
        assert v_cmdr is not None

        result = emb_contribution.embedding_contribution(
            candidate_name=candidate,
            n_rules=2,
            v_cmdr=v_cmdr,
            vectors=vectors,
        )

        # Expected shape: w_emb * exp(-k*n) * cosine(v_cand, v_cmdr).
        # With L2-normalized random vectors, cosine is bounded in [-1, 1];
        # the final contribution is in [-0.5 * decay, 0.5 * decay].
        decay = math.exp(-0.8 * 2)
        assert -0.5 * decay <= result <= 0.5 * decay
        # Not exactly zero (independent random seeds → nonzero cosine).
        assert result != 0.0
    finally:
        conn.close()
