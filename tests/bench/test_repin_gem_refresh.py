"""``bench.py audit --repin`` gem-refresh plumbing (plan 2026-07-02-003 Unit 1).

``handle_repin`` must pass an ``edhrec_conn`` into ``build_fixture`` so
re-pins refresh the per-commander hidden-gem legacy values instead of
echoing stale ones forever (the R9 rider: the ≈:104 call site lacked
the plumbing, so every pin since plan-003-gem carried pre-plan gem
numbers and ``--inspect-gems`` rendered ``Δ —``).

A pre-flight validation probe against ``edhrec_card_synergy`` is
required BEFORE any scoring work: ``_edhrec_top_30`` swallows
per-commander ``sqlite3.DatabaseError`` by design, so a corrupt or
empty EDHREC DB would otherwise silently produce the exact gem-less
pin this plumbing exists to prevent.

Uses synthetic tmp-path DBs throughout (never project-relative paths —
see the conftest repo-root guard).
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mtg_synergy_graph.bench import handlers
from mtg_synergy_graph.bench.fixture import (
    FixtureEntry,
    PinnedFixture,
    build_fixture,
)
from mtg_synergy_graph.bench.handlers import handle_repin
from mtg_synergy_graph.db import open_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _args(
    fixture: Path,
    db: Path | str,
    *,
    edhrec_db: str | None,
    yes: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(db),
        fixture=str(fixture),
        edhrec_db=edhrec_db,
        yes=yes,
    )


def _seed_synergy_db(conn: sqlite3.Connection) -> None:
    """Minimal synergy DB: one commander + two candidates that score."""
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
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Flicker Guy", "Creature", "", 3, "R", 600, 1),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Flicker Guy", "effect", "ChangesZone", "Creature.YouCtrl", "{flicker}"),
    )
    conn.commit()


def _make_edhrec_db(path: Path, rows: list[tuple[str, str, str, float]]) -> None:
    """Write a synthetic EDHREC DB file with the given synergy rows."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)")
    conn.executemany(
        "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def synergy_db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "synergy.db"
    conn = open_db(path)
    _seed_synergy_db(conn)
    conn.close()
    yield path


def _write_gemless_fixture(path: Path, db_path: Path) -> PinnedFixture:
    """Pin an existing fixture WITHOUT gem keys (the stale-legacy shape)."""
    conn = open_db(db_path)
    try:
        fixture = build_fixture(conn, ["Test Commander"])
    finally:
        conn.close()
    # Simulate plan-002-era legacy: unrelated fields present, gem keys absent.
    entries = [FixtureEntry(commander=e.commander, scores=e.scores, legacy={"ndcg30": 0.5}) for e in fixture.entries]
    stale = PinnedFixture(
        config_hash=fixture.config_hash,
        created_at="t",
        schema_version=fixture.schema_version,
        entries=entries,
    )
    stale.write(path)
    return stale


# ---------------------------------------------------------------------------
# Error paths — pre-flight validation
# ---------------------------------------------------------------------------


def test_missing_edhrec_db_exits_2_without_writing(
    synergy_db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nonexistent EDHREC DB path → exit 2, no fixture write, hint."""
    fixture_path = tmp_path / "pin.json"
    stale = _write_gemless_fixture(fixture_path, synergy_db_path)
    before = fixture_path.read_text()

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(tmp_path / "nope.db")))

    assert rc == 2
    assert fixture_path.read_text() == before, "fixture must not be rewritten"
    err = capsys.readouterr().err
    assert "--edhrec-db" in err
    assert stale.entries  # sanity: the pin we preserved was non-empty


def test_edhrec_db_none_exits_2(
    synergy_db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """args.edhrec_db of None (no CLI default) → exit 2 with hint."""
    fixture_path = tmp_path / "pin.json"
    _write_gemless_fixture(fixture_path, synergy_db_path)

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=None))

    assert rc == 2
    assert "--edhrec-db" in capsys.readouterr().err


def test_corrupt_edhrec_db_exits_2_no_partial_pin(
    synergy_db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty-file EDHREC DB (no table) → exit 2 before any scoring work."""
    fixture_path = tmp_path / "pin.json"
    _write_gemless_fixture(fixture_path, synergy_db_path)
    before = fixture_path.read_text()
    bad_edhrec = tmp_path / "empty.db"
    bad_edhrec.touch()

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(bad_edhrec)))

    assert rc == 2
    assert fixture_path.read_text() == before, "no partial pin on corrupt EDHREC DB"
    err = capsys.readouterr().err
    assert "unusable" in err or "edhrec" in err.lower()


def test_garbage_file_edhrec_db_exits_2(
    synergy_db_path: Path,
    tmp_path: Path,
) -> None:
    """Non-SQLite garbage file → sqlite3.DatabaseError → exit 2."""
    fixture_path = tmp_path / "pin.json"
    _write_gemless_fixture(fixture_path, synergy_db_path)
    garbage = tmp_path / "garbage.db"
    garbage.write_text("this is not a sqlite database at all" * 40)

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(garbage)))

    assert rc == 2


def test_preview_mode_skips_edhrec_validation(
    synergy_db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --yes the confirmation gate fires first (exit 1), even
    when the EDHREC DB is missing — the preview is a dry-run."""
    fixture_path = tmp_path / "pin.json"
    _write_gemless_fixture(fixture_path, synergy_db_path)

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(tmp_path / "nope.db"), yes=False))

    assert rc == 1
    assert "--yes" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Happy path — gem keys refreshed
# ---------------------------------------------------------------------------


def test_repin_passes_edhrec_conn_to_build_fixture(
    synergy_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build_fixture call receives a live edhrec_conn (not None)."""
    fixture_path = tmp_path / "pin.json"
    stale = _write_gemless_fixture(fixture_path, synergy_db_path)
    edhrec_path = tmp_path / "tags.db"
    _make_edhrec_db(
        edhrec_path,
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )

    seen: dict[str, object] = {}

    def _stub(conn, commanders, existing=None, tensor_writer=None, edhrec_conn=None):  # type: ignore[no-untyped-def]
        seen["edhrec_conn"] = edhrec_conn
        return stale

    monkeypatch.setattr(handlers, "build_fixture", _stub)

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(edhrec_path)))

    assert rc == 0
    assert seen["edhrec_conn"] is not None


def test_repin_adds_gem_keys_to_gemless_legacy(
    synergy_db_path: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: a pin whose legacy lacks gem keys gains fresh ones,
    matching a direct build_fixture(edhrec_conn=...) computation, and
    unrelated legacy fields survive."""
    fixture_path = tmp_path / "pin.json"
    _write_gemless_fixture(fixture_path, synergy_db_path)
    edhrec_path = tmp_path / "tags.db"
    _make_edhrec_db(
        edhrec_path,
        [("test-commander", "Token Maker", "High Synergy Cards", 0.5)],
    )

    rc = handle_repin(_args(fixture_path, synergy_db_path, edhrec_db=str(edhrec_path)))
    assert rc == 0

    written = PinnedFixture.load(fixture_path)
    assert len(written.entries) == 1
    entry = written.entries[0]
    assert "hidden_gem_hit_rate" in entry.legacy
    assert "hidden_cards" in entry.legacy
    assert entry.legacy["ndcg30"] == 0.5, "unrelated legacy fields preserved"

    # Reference computation through the library path on the same DBs.
    conn = open_db(synergy_db_path)
    edhrec_conn = sqlite3.connect(edhrec_path)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        reference = build_fixture(conn, ["Test Commander"], edhrec_conn=edhrec_conn)
    finally:
        edhrec_conn.close()
        conn.close()
    ref_entry = reference.entries[0]
    assert entry.legacy["hidden_gem_hit_rate"] == ref_entry.legacy["hidden_gem_hit_rate"]
    assert entry.legacy["hidden_cards"] == ref_entry.legacy["hidden_cards"]
