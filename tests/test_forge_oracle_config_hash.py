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


def test_hash_flips_when_port_signature_version_changes() -> None:
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


def test_verify_passes_when_hash_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "oracle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    inputs = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    expected = fo_config.compute_oracle_hash(inputs)
    conn.execute("INSERT INTO oracle_config(key, value) VALUES ('config_hash', ?)", (expected,))
    conn.commit()

    fo_config.verify_current_or_raise(conn, inputs)  # no-op on match
    conn.close()


def test_verify_raises_on_stale_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "oracle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute("INSERT INTO oracle_config(key, value) VALUES ('config_hash', 'stale_value')")
    conn.commit()
    inputs = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    with pytest.raises(fo_config.OracleConfigStaleError, match=r"(?i)rebuild"):
        fo_config.verify_current_or_raise(conn, inputs)
    conn.close()


def test_verify_raises_when_hash_row_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "oracle.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE oracle_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    inputs = fo_config.OracleConfigInputs(
        forge_sha="a" * 40,
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
        port_signature_version="2",
        java_method_id="m",
    )
    with pytest.raises(fo_config.OracleConfigMissingError):
        fo_config.verify_current_or_raise(conn, inputs)
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
        smoothing_k=0.5,
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
    assert stored["ppmi_smoothing_k"] == "0.5"
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
        smoothing_k=0.5,
    )

    inputs = fo_config.get_oracle_config_inputs(
        ppmi_smoothing_k=0.5,
        min_decks_count=3,
    )
    conn = sqlite3.connect(target)
    try:
        fo_config.verify_current_or_raise(conn, inputs)  # must not raise
    finally:
        conn.close()
