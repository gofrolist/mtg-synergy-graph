"""Tests for ``forge_oracle.config`` — OracleConfigInputs + hash.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 5.

Mirrors the inference-path pattern in
``src/mtg_synergy_graph/universal_scorer.py`` (``ScoringConfigInputs``)
+ ``src/mtg_synergy_graph/bench/tensor.py`` (``compute_config_hash``)
applied to the offline oracle. The meta-principle carried over from
``docs/solutions/best-practices/flag-gated-multi-port-rule-pattern-2026-04-23.md``
is: mechanically enforce the subsystem invariant via a hash that
refuses stale comparisons.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.forge_oracle import config as fo_config
from mtg_synergy_graph.forge_oracle import ingest
from mtg_synergy_graph.forge_oracle.ppmi import DEFAULT_SMOOTHING_K

# ---------------------------------------------------------------------------
# compute_oracle_hash — determinism + sensitivity
# ---------------------------------------------------------------------------


def test_hash_is_deterministic_across_calls() -> None:
    inputs = fo_config.OracleConfigInputs(
        forge_sha="abcdef0123456789abcdef0123456789abcdef01",
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="CardRanker.getScoreForDeckHints@forge-x",
    )
    h1 = fo_config.compute_oracle_hash(inputs)
    h2 = fo_config.compute_oracle_hash(inputs)
    assert h1 == h2
    # Sanity: SHA-256 hex digest is 64 chars.
    assert len(h1) == 64


def test_hash_flips_when_forge_sha_changes() -> None:
    base = fo_config.OracleConfigInputs(
        forge_sha="abcdef0123456789abcdef0123456789abcdef01",
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    different = fo_config.OracleConfigInputs(
        forge_sha="00000000000000000000000000000000000000ff",
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    assert fo_config.compute_oracle_hash(base) != fo_config.compute_oracle_hash(different)


def test_hash_flips_when_smoothing_k_changes() -> None:
    base = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    bumped = base._replace(ppmi_smoothing_k=1.0)
    assert fo_config.compute_oracle_hash(base) != fo_config.compute_oracle_hash(bumped)


def test_hash_flips_when_min_decks_count_changes() -> None:
    base = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    bumped = base._replace(min_decks_count=5)
    assert fo_config.compute_oracle_hash(base) != fo_config.compute_oracle_hash(bumped)


def test_hash_flips_when_vocab_version_changes() -> None:
    base = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    bumped = base._replace(port_signature_version="3")
    assert fo_config.compute_oracle_hash(base) != fo_config.compute_oracle_hash(bumped)


def test_hash_flips_when_java_method_id_changes() -> None:
    base = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="CardRanker.getScoreForDeckHints@forge-a",
    )
    bumped = base._replace(java_method_id="CardRanker.getScoreForDeckHints@forge-b")
    assert fo_config.compute_oracle_hash(base) != fo_config.compute_oracle_hash(bumped)


# ---------------------------------------------------------------------------
# verify_current_or_raise — refuse-to-run contract
# ---------------------------------------------------------------------------


def _open_kv_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "oracle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    return conn


def _stable_inputs(**overrides: object) -> fo_config.OracleConfigInputs:
    base: dict[str, object] = {
        "forge_sha": "a" * 40,
        "ppmi_smoothing_k": 0.5,
        "min_decks_count": 3,
        "port_signature_version": "2",
        "java_method_id": "m",
    }
    base.update(overrides)
    return fo_config.OracleConfigInputs(**base)  # type: ignore[arg-type]


def test_verify_passes_when_stored_matches_current(tmp_path: Path) -> None:
    """Happy path: full KV rows + correct hash + matching inputs → no raise."""
    conn = _open_kv_db(tmp_path)
    try:
        inputs = _stable_inputs()
        fo_config.write_oracle_config(conn, inputs)
        conn.commit()
        fo_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_verify_raises_stale_when_stored_inputs_diverge_from_code(tmp_path: Path) -> None:
    """Stale: stored inputs are internally consistent but differ from current code.

    Build wrote ``ppmi_smoothing_k=0.5`` and a hash matching that. Code
    now expects the default (currently 0.0). Staleness check must fire
    naming the diverging field — not Corrupt, not Missing.
    """
    conn = _open_kv_db(tmp_path)
    try:
        stored_inputs = _stable_inputs(ppmi_smoothing_k=0.5)
        fo_config.write_oracle_config(conn, stored_inputs)
        conn.commit()
        current_code_inputs = _stable_inputs(ppmi_smoothing_k=DEFAULT_SMOOTHING_K)
        with pytest.raises(fo_config.OracleConfigStaleError) as exc_info:
            fo_config.verify_current_or_raise(conn, current_code_inputs)
        message = str(exc_info.value)
        assert "ppmi_smoothing_k" in message
        assert "stored=0.5" in message
        assert f"current={DEFAULT_SMOOTHING_K}" in message
        assert "rebuild" in message.lower()
    finally:
        conn.close()


def test_verify_raises_corrupt_when_stored_hash_contradicts_rows(tmp_path: Path) -> None:
    """Partial-write simulation: KV rows written but hash not updated."""
    conn = _open_kv_db(tmp_path)
    try:
        inputs = _stable_inputs()
        fo_config.write_oracle_config(conn, inputs)
        # Now overwrite the config_hash with a well-formed but wrong value,
        # simulating a partial write where rows were updated and the hash
        # update step failed.
        conn.execute(
            "UPDATE oracle_config SET value = ? WHERE key = 'config_hash'",
            ("a" * 64,),
        )
        conn.commit()
        with pytest.raises(fo_config.OracleConfigCorruptError) as exc_info:
            fo_config.verify_current_or_raise(conn, inputs)
        message = str(exc_info.value)
        correct_hash = fo_config.compute_oracle_hash(inputs)
        assert "aaaaaaaaaaaa" in message  # stored hash truncated prefix
        assert correct_hash[:12] in message  # recomputed hash truncated prefix
        assert "rebuild" in message.lower()
    finally:
        conn.close()


def test_verify_raises_missing_when_kv_table_empty(tmp_path: Path) -> None:
    """Empty KV table → Missing, not Corrupt or Stale."""
    conn = _open_kv_db(tmp_path)
    try:
        inputs = _stable_inputs()
        with pytest.raises(fo_config.OracleConfigMissingError):
            fo_config.verify_current_or_raise(conn, inputs)
    finally:
        conn.close()


def test_verify_raises_missing_when_only_config_hash_present(tmp_path: Path) -> None:
    """Only ``config_hash`` present (pre-split-DB style) → Missing."""
    conn = _open_kv_db(tmp_path)
    try:
        inputs = _stable_inputs()
        expected = fo_config.compute_oracle_hash(inputs)
        conn.execute("INSERT INTO oracle_config(key, value) VALUES ('config_hash', ?)", (expected,))
        conn.commit()
        with pytest.raises(fo_config.OracleConfigMissingError) as exc_info:
            fo_config.verify_current_or_raise(conn, inputs)
        assert "missing required keys" in str(exc_info.value)
    finally:
        conn.close()


def test_verify_raises_missing_when_kv_table_does_not_exist(tmp_path: Path) -> None:
    """No ``oracle_config`` table at all → Missing (graceful)."""
    db_path = tmp_path / "bare.db"
    conn = sqlite3.connect(db_path)
    try:
        inputs = _stable_inputs()
        with pytest.raises(fo_config.OracleConfigMissingError) as exc_info:
            fo_config.verify_current_or_raise(conn, inputs)
        assert "table is missing" in str(exc_info.value)
    finally:
        conn.close()


def test_verify_raises_corrupt_when_smoothing_k_malformed(tmp_path: Path) -> None:
    """Non-numeric ``ppmi_smoothing_k`` KV value → Corrupt. The row
    exists (so Missing is wrong) but its stored value cannot parse as
    ``float`` — the exact failure class ``OracleConfigCorruptError`` is
    designed to signal.
    """
    conn = _open_kv_db(tmp_path)
    try:
        inputs = _stable_inputs()
        fo_config.write_oracle_config(conn, inputs)
        conn.execute("UPDATE oracle_config SET value = 'not_a_float' WHERE key = 'ppmi_smoothing_k'")
        conn.commit()
        with pytest.raises(fo_config.OracleConfigCorruptError) as exc_info:
            fo_config.verify_current_or_raise(conn, inputs)
        assert "malformed" in str(exc_info.value)
    finally:
        conn.close()


def test_oracle_error_classes_inherit_from_common_base() -> None:
    """All three failure-mode subclasses descend from the same base."""
    assert issubclass(fo_config.OracleConfigStaleError, fo_config.OracleConfigError)
    assert issubclass(fo_config.OracleConfigMissingError, fo_config.OracleConfigError)
    assert issubclass(fo_config.OracleConfigCorruptError, fo_config.OracleConfigError)


def test_read_stored_oracle_config_returns_stored_not_current(tmp_path: Path) -> None:
    """``read_stored_oracle_config`` reflects what was written, not current defaults."""
    conn = _open_kv_db(tmp_path)
    try:
        stored_inputs = _stable_inputs(ppmi_smoothing_k=0.75, min_decks_count=7)
        fo_config.write_oracle_config(conn, stored_inputs)
        conn.commit()
        read_inputs, read_hash = fo_config.read_stored_oracle_config(conn)
        assert read_inputs == stored_inputs
        assert read_hash == fo_config.compute_oracle_hash(stored_inputs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integration: ingest.build_forge_oracle_db writes the hash
# ---------------------------------------------------------------------------


def _write_dck(dir_path: Path, name: str, cards: list[str]) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    content = f"[metadata]\nName={name}\n[Main]\n" + "".join(f"1 {c}|X|1\n" for c in cards)
    (dir_path / f"{name}.dck").write_text(content, encoding="utf-8")


def _make_synergy_db(tmp_path: Path) -> Path:
    from mtg_synergy_graph.db import open_db

    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        conn.executemany(
            "INSERT INTO cards (name, oracle_id, card_types, types) VALUES (?, ?, ?, ?)",
            [
                ("A", "oa", "Creature", "Creature"),
                ("B", "ob", "Creature", "Creature"),
            ],
        )
        conn.executemany(
            "INSERT INTO card_ports (card_name, port_type, event_class, zone_destination) VALUES (?, ?, ?, ?)",
            [
                ("A", "cost", "sacrifice", None),
                ("B", "trigger", "ChangesZone", "Graveyard"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_build_writes_config_hash_and_inputs(tmp_path: Path) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    decks = tmp_path / "decks"
    for i in range(3):
        _write_dck(decks, f"d_{i}", ["A", "B"])

    target = tmp_path / "forge_oracle.db"
    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target,
        deck_dirs=[decks],
        min_decks_count=3,
    )

    conn = sqlite3.connect(target)
    try:
        stored = dict(conn.execute("SELECT key, value FROM oracle_config").fetchall())
    finally:
        conn.close()

    # Hash row present + 64 hex chars
    assert "config_hash" in stored
    assert len(stored["config_hash"]) == 64
    # Individual inputs all stored for diagnostics
    assert stored["ppmi_smoothing_k"] == str(DEFAULT_SMOOTHING_K)
    assert stored["min_decks_count"] == "3"
    assert "java_method_id" in stored
    assert "port_signature_version" in stored
    assert "forge_sha" in stored
    # Forge SHA value is current pinned SHA (live test — tests/test_forge_oracle_version_pin.py
    # already asserts pin matches HEAD, so we can assert shape here).
    assert len(stored["forge_sha"]) == 40


def test_build_hash_matches_verify(tmp_path: Path) -> None:
    """Integration: after build, strict consumers can verify successfully."""
    synergy_db = _make_synergy_db(tmp_path)
    decks = tmp_path / "decks"
    for i in range(3):
        _write_dck(decks, f"d_{i}", ["A", "B"])

    target = tmp_path / "forge_oracle.db"
    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target,
        deck_dirs=[decks],
        min_decks_count=3,
    )

    inputs = fo_config.get_oracle_config_inputs(
        ppmi_smoothing_k=DEFAULT_SMOOTHING_K,
        min_decks_count=3,
    )
    conn = sqlite3.connect(target)
    try:
        fo_config.verify_current_or_raise(conn, inputs)  # must not raise
    finally:
        conn.close()
