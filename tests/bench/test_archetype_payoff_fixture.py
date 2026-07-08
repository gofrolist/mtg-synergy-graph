"""Tests for the archetype-payoff cohort fixture bootstrap (plan 2026-07-03-001 Unit 2).

Exercises ``scripts/bootstrap_archetype_payoff_fixture.py`` end-to-end against
synthetic ``tmp_path`` synergy + EDHREC DBs (never project-relative literals;
a conftest autouse guard fails the run if a new ``*.db`` appears under the repo
root or ``data/``). Covers selection, the High-Synergy drop-and-log path, the
missing-DB error path, and the round-trip of the ``cohort_members`` snapshot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import bootstrap_archetype_payoff_fixture as boot
import pytest

from mtg_synergy_graph.bench.fixture import SCHEMA_VERSION, PinnedFixture
from mtg_synergy_graph.db import open_db


def _seed_synergy_db(path: Path) -> None:
    """Synergy DB with two cohort commanders (one EDHREC-covered, one not)."""
    conn = open_db(path)
    # Token-subtype vocab: one producer port carrying 'Saproling'.
    conn.execute(
        "INSERT INTO cards (name, supertypes, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES ('Token Producer', 'Legendary', 'Legendary Creature', '', 3, 'G', 900, 1)"
    )
    cur = conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES ('Token Producer', 'effect', 'Token')"
    )
    conn.execute(
        "INSERT INTO port_attributes (port_id, attr_kind, attr_value) VALUES (?, 'token_subtype', 'Saproling')",
        (int(cur.lastrowid),),
    )

    # Two cohort commanders: Saproling Sacrificed death triggers.
    for name, rank in (("Slime Lord", 100), ("Poor Fungus", 200)):
        conn.execute(
            "INSERT INTO cards (name, supertypes, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
            "VALUES (?, 'Legendary', 'Legendary Creature', '', 4, 'G', ?, 1)",
            (name, rank),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter) "
            "VALUES (?, 'trigger', 'Sacrificed', 'Saproling.YouCtrl')",
            (name,),
        )
    conn.commit()
    conn.close()


def _seed_edhrec_db(path: Path) -> None:
    """EDHREC DB: High-Synergy rows for 'Slime Lord' only ('Poor Fungus' absent)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)")
    conn.execute(
        "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) "
        "VALUES ('slime-lord', 'Sporecap Spider', 'High Synergy Cards', 0.5)"
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def wired_dbs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bootstrap module at synthetic tmp_path DBs; return output path."""
    synergy = tmp_path / "synergy.db"
    edhrec = tmp_path / "tags.db"
    out = tmp_path / "golden_set_archetype_payoff.json"
    _seed_synergy_db(synergy)
    _seed_edhrec_db(edhrec)
    monkeypatch.setattr(boot, "SYNERGY_DB", synergy)
    monkeypatch.setattr(boot, "EDHREC_DB", edhrec)
    monkeypatch.setattr(boot, "OUTPUT_PATH", out)
    return out


# ---------------------------------------------------------------------------
# Selection + drop-and-log
# ---------------------------------------------------------------------------


def test_select_keeps_edhrec_covered_drops_uncovered(wired_dbs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Edge: a cohort commander with zero High-Synergy rows is dropped and logged."""
    kept = boot._select_cohort_commanders()
    assert kept == ["Slime Lord"]
    err = capsys.readouterr().err
    assert "cohort=2" in err
    assert "Poor Fungus" in err  # logged as dropped


# ---------------------------------------------------------------------------
# main() happy path + round-trip
# ---------------------------------------------------------------------------


def test_main_writes_fixture_with_snapshot(wired_dbs: Path) -> None:
    """Happy path: main() writes a fixture with entries, config_hash, snapshot."""
    rc = boot.main()
    assert rc == 0
    assert wired_dbs.exists()

    loaded = PinnedFixture.load(wired_dbs)
    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.config_hash  # populated
    assert [e.commander for e in loaded.entries] == ["Slime Lord"]
    # cohort_members snapshot persists and reloads intact (Key Decision 4).
    assert loaded.cohort_members == ["Slime Lord"]


def test_main_config_hash_matches_live(wired_dbs: Path) -> None:
    """The written config_hash equals the live compute_config_hash (freshness gate)."""
    from mtg_synergy_graph.bench.tensor import compute_config_hash

    boot.main()
    loaded = PinnedFixture.load(wired_dbs)
    assert loaded.config_hash == compute_config_hash()


def test_main_is_idempotent(wired_dbs: Path) -> None:
    """Re-running reproduces the same entries + snapshot (modulo created_at)."""
    boot.main()
    first = PinnedFixture.load(wired_dbs)
    boot.main()
    second = PinnedFixture.load(wired_dbs)
    assert [e.commander for e in first.entries] == [e.commander for e in second.entries]
    assert first.cohort_members == second.cohort_members
    assert first.entries[0].scores == second.entries[0].scores


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_main_missing_synergy_db_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error path: a missing synergy DB → exit 2, no partial file written."""
    out = tmp_path / "out.json"
    monkeypatch.setattr(boot, "SYNERGY_DB", tmp_path / "absent.db")
    monkeypatch.setattr(boot, "EDHREC_DB", tmp_path / "also_absent.db")
    monkeypatch.setattr(boot, "OUTPUT_PATH", out)
    assert boot.main() == 2
    assert not out.exists()
