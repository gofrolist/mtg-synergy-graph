"""Tests for ``forge_oracle.ingest`` — end-to-end PPMI build orchestrator.

Plan: docs/plans/2026-04-23-002-feat-forge-second-oracle-plan.md Unit 4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.forge_oracle import ingest


def _write_dck(dir_path: Path, name: str, main_cards: list[str], commander: str | None = None) -> Path:
    """Write a minimal ``.dck`` file with the given cards."""
    dir_path.mkdir(parents=True, exist_ok=True)
    lines = ["[metadata]", f"Name={name}"]
    if commander:
        lines += ["[Commander]", f"1 {commander}|X|1"]
    lines += ["[Main]"]
    for card in main_cards:
        lines.append(f"1 {card}|X|1")
    path = dir_path / f"{name}.dck"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_synergy_db(tmp_path: Path) -> Path:
    """Build a minimal synergy DB with cards + card_ports for ingest testing.

    Uses ``open_db`` so the schema (including the ``port_nodes`` view)
    is created the same way production creates it.
    """
    db_path = tmp_path / "synergy.db"
    conn = open_db(db_path)
    try:
        # Four test cards with distinguishable subkinds
        conn.executemany(
            "INSERT INTO cards (name, oracle_id, card_types, types) VALUES (?, ?, ?, ?)",
            [
                ("Sac Outlet", "soid", "Creature", "Creature"),
                ("Death Trigger", "dtid", "Creature", "Creature"),
                ("Token Gen", "tgid", "Enchantment", "Enchantment"),
                ("ETB Payoff", "epid", "Creature", "Creature"),
            ],
        )
        # Ports projecting to distinct subkinds via port_nodes view:
        #   ('cost', 'sacrifice')   -> node_kind SACRIFICE, subkind 'cost.sacrifice'
        #   ('trigger', 'ChangesZone') with zone_destination='Graveyard' -> DIES, subkind 'trigger.ChangesZone'
        #   ('effect', 'Token')   -> ZONECHANGE, subkind 'effect.Token'
        #   ('trigger', 'ChangesZone') with zone_destination='Battlefield' -> ETB, subkind 'trigger.ChangesZone'
        conn.executemany(
            "INSERT INTO card_ports (card_name, port_type, event_class, zone_destination) VALUES (?, ?, ?, ?)",
            [
                ("Sac Outlet", "cost", "sacrifice", None),
                ("Death Trigger", "trigger", "ChangesZone", "Graveyard"),
                ("Token Gen", "effect", "Token", None),
                ("ETB Payoff", "trigger", "ChangesZone", "Battlefield"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# End-to-end: deck → PPMI rows written to sidecar DB
# ---------------------------------------------------------------------------


def test_build_produces_ppmi_rows(tmp_path: Path) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"

    # 3 decks containing the sac-outlet + death-trigger pair; 3 decks containing
    # token-gen + ETB-payoff. Each pair appears in >= 3 decks (threshold).
    for i in range(3):
        _write_dck(decks_dir, f"sac_aristocrats_{i}", ["Sac Outlet", "Death Trigger"])
    for i in range(3):
        _write_dck(decks_dir, f"etb_tokens_{i}", ["Token Gen", "ETB Payoff"])

    target_db = tmp_path / "forge_oracle.db"
    stats = ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=3,
        smoothing_k=0.5,
    )
    assert stats.decks_parsed == 6
    assert stats.ppmi_rows_written > 0
    assert target_db.is_file()

    # Verify the two expected synergy pairs landed as rows
    out_conn = sqlite3.connect(target_db)
    try:
        out_conn.row_factory = sqlite3.Row
        rows = out_conn.execute(
            "SELECT port_signature_a, port_signature_b, ppmi, decks_count "
            "FROM forge_precon_ppmi ORDER BY port_signature_a, port_signature_b"
        ).fetchall()
    finally:
        out_conn.close()

    pairs = {(r["port_signature_a"], r["port_signature_b"]) for r in rows}
    assert ("cost.sacrifice", "trigger.ChangesZone") in pairs
    assert ("effect.Token", "trigger.ChangesZone") in pairs
    for r in rows:
        assert r["port_signature_a"] < r["port_signature_b"]  # canonical order
        assert r["decks_count"] >= 3
        assert r["ppmi"] >= 0.0


def test_build_skips_pairs_below_threshold(tmp_path: Path) -> None:
    """Pairs in only 2 decks are dropped by default threshold (3)."""
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"
    for i in range(2):
        _write_dck(decks_dir, f"sparse_{i}", ["Sac Outlet", "Death Trigger"])

    target_db = tmp_path / "forge_oracle.db"
    stats = ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=3,
        smoothing_k=0.5,
    )
    assert stats.ppmi_rows_written == 0


def test_build_is_idempotent(tmp_path: Path) -> None:
    """Two successive builds produce the same PPMI rows (content-identical,
    ignoring last_updated)."""
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"
    for i in range(3):
        _write_dck(decks_dir, f"deck_{i}", ["Sac Outlet", "Death Trigger", "ETB Payoff"])

    target_db = tmp_path / "forge_oracle.db"
    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=3,
        smoothing_k=0.5,
    )
    conn1 = sqlite3.connect(target_db)
    rows1 = sorted(
        conn1.execute("SELECT port_signature_a, port_signature_b, ppmi, decks_count FROM forge_precon_ppmi").fetchall()
    )
    conn1.close()

    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=3,
        smoothing_k=0.5,
    )
    conn2 = sqlite3.connect(target_db)
    rows2 = sorted(
        conn2.execute("SELECT port_signature_a, port_signature_b, ppmi, decks_count FROM forge_precon_ppmi").fetchall()
    )
    conn2.close()

    assert rows1 == rows2


def test_build_unknown_cards_counted_but_no_failure(tmp_path: Path) -> None:
    """Cards in .dck files not present in our cards table are skipped
    and counted, without failing the whole build."""
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"
    _write_dck(decks_dir, "mixed", ["Sac Outlet", "Nonexistent Card", "Another Ghost"])
    for i in range(2):
        _write_dck(decks_dir, f"normal_{i}", ["Sac Outlet", "Death Trigger"])

    target_db = tmp_path / "forge_oracle.db"
    stats = ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=1,  # relax so we can observe rows
        smoothing_k=0.0,
    )
    assert stats.unknown_card_names >= 2  # "Nonexistent Card", "Another Ghost"
    assert stats.decks_parsed == 3
    assert target_db.is_file()


def test_build_missing_synergy_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="synergy DB not found"):
        ingest.build_forge_oracle_db(
            synergy_db_path=tmp_path / "nonexistent.db",
            target_db_path=tmp_path / "out.db",
            deck_dirs=[tmp_path],
        )


def test_build_preserves_existing_db_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Atomic rename: a crashed mid-build leaves the prior DB intact."""
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"
    for i in range(3):
        _write_dck(decks_dir, f"d_{i}", ["Sac Outlet", "Death Trigger"])

    target_db = tmp_path / "forge_oracle.db"
    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=3,
        smoothing_k=0.5,
    )
    first_mtime = target_db.stat().st_mtime_ns
    first_size = target_db.stat().st_size
    assert first_size > 0

    # Force a failure inside the write path via monkeypatch
    def _boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(ingest, "_write_ppmi_rows", _boom)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        ingest.build_forge_oracle_db(
            synergy_db_path=synergy_db,
            target_db_path=target_db,
            deck_dirs=[decks_dir],
            min_decks_count=3,
            smoothing_k=0.5,
        )

    # Prior DB still in place — atomic rename means the temp file was discarded
    assert target_db.is_file()
    assert target_db.stat().st_size == first_size
    assert target_db.stat().st_mtime_ns == first_mtime
    # And the temp file was cleaned up
    assert not target_db.with_suffix(target_db.suffix + ".tmp").exists()


def test_build_creates_target_parent_dir(tmp_path: Path) -> None:
    synergy_db = _make_synergy_db(tmp_path)
    decks_dir = tmp_path / "decks"
    _write_dck(decks_dir, "d", ["Sac Outlet"])

    target_db = tmp_path / "nested" / "subdir" / "forge_oracle.db"
    ingest.build_forge_oracle_db(
        synergy_db_path=synergy_db,
        target_db_path=target_db,
        deck_dirs=[decks_dir],
        min_decks_count=1,
        smoothing_k=0.5,
    )
    assert target_db.is_file()
