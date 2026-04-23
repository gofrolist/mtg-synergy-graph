"""Integration tests for ``bench.py audit`` wiring of ``edhrec_db``.

Guards against structural-drift regressions on the hidden-gem metric:

* ``run_audit`` must open the EDHREC DB when one is configured and
  pass it through to ``build_fixture`` so the live aggregate gets
  populated. Without this, the FR3 ``gem_Δ`` summary line is always
  ``—`` and FR4's warning cannot fire.
* When the configured EDHREC DB path does not exist, the audit must
  degrade gracefully: print a stderr warning, continue, and surface
  ``gem_Δ=—`` without crashing.

Uses synthetic in-memory DBs so tests run in milliseconds and do not
depend on ``data/synergy.db`` + ``data/tags.db``.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from mtg_synergy_graph.bench.audit import handle_audit
from mtg_synergy_graph.bench.fixture import build_fixture
from mtg_synergy_graph.db import open_db

# ---------------------------------------------------------------------------
# Synthetic DB helpers
# ---------------------------------------------------------------------------


def _seed_synergy_db(conn: sqlite3.Connection) -> None:
    """Minimal DB with one commander + several candidates that produce
    enough tensor rows that the plausibility gate has real data to
    work with on hidden candidates."""
    conn.execute(
        "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Test Commander", "Creature", "", 4, "R", 1000, 1),
    )
    for cand, rank in [
        ("Token Maker", 500),
        ("Flicker Guy", 600),
        ("Hidden A", 700),
        ("Hidden B", 800),
        ("Hidden C", 900),
    ]:
        conn.execute(
            "INSERT INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cand, "Creature", "", 3, "R", rank, 1),
        )
    # Commander: ETB trigger on creatures you control.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Test Commander", "trigger", "ChangesZone", "Creature.YouCtrl", "{ETB}"),
    )
    # Candidates with matching effects — produce tensor rows.
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Token Maker", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Flicker Guy", "effect", "ChangesZone", "Creature.YouCtrl", "{flicker}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Hidden A", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Hidden B", "effect", "Token", "Creature.Token", "{make}"),
    )
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) VALUES (?, ?, ?, ?, ?)",
        ("Hidden C", "effect", "ChangesZone", "Creature.YouCtrl", "{flicker}"),
    )
    conn.commit()


def _make_edhrec_db(path: Path) -> None:
    """Synthetic EDHREC DB listing Token Maker + Flicker Guy as the
    High Synergy Cards for our test commander. Everything else (Hidden
    A/B/C) is absent and thus a hidden-gem candidate.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE edhrec_card_synergy (commander_slug TEXT, card_name TEXT, section TEXT, synergy REAL)"
        )
        conn.executemany(
            "INSERT INTO edhrec_card_synergy (commander_slug, card_name, section, synergy) VALUES (?, ?, ?, ?)",
            [
                ("test-commander", "Token Maker", "High Synergy Cards", 0.5),
                ("test-commander", "Flicker Guy", "High Synergy Cards", 0.4),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _args(
    *,
    db: Path,
    fixture: Path,
    edhrec_db: Path | None,
    history: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(db),
        fixture=str(fixture),
        edhrec_db=str(edhrec_db) if edhrec_db is not None else None,
        history=str(history),
        format="md",
        output="-",
    )


@pytest.fixture()
def seeded_setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create the synergy DB, the EDHREC DB, and pin a fresh fixture.

    Returns ``(synergy_db, edhrec_db, fixture_path, history_path)``.
    The fixture carries live hidden-gem data so the audit has a
    non-None pinned aggregate to delta against.
    """
    synergy_db = tmp_path / "synergy.db"
    edhrec_db = tmp_path / "tags.db"
    fixture_path = tmp_path / "fixture.json"
    history_path = tmp_path / "history.csv"

    # 1) Seed synergy DB.
    conn = open_db(synergy_db)
    try:
        _seed_synergy_db(conn)
    finally:
        conn.close()

    # 2) Create EDHREC DB with rows.
    _make_edhrec_db(edhrec_db)

    # 3) Pin a fresh fixture using the EDHREC DB so pinned entry gets
    #    hidden-gem fields.
    conn = open_db(synergy_db)
    edhrec_conn = sqlite3.connect(edhrec_db)
    edhrec_conn.row_factory = sqlite3.Row
    try:
        fixture = build_fixture(
            conn,
            ["Test Commander"],
            edhrec_conn=edhrec_conn,
        )
    finally:
        edhrec_conn.close()
        conn.close()
    fixture.write(fixture_path)

    return synergy_db, edhrec_db, fixture_path, history_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_audit_with_edhrec_db_populates_gem_delta(
    seeded_setup: tuple[Path, Path, Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``handle_audit`` with ``edhrec_db`` → live aggregate populated and
    the summary line renders a numeric ``gem_Δ=+N.NNNN`` (not ``—``).
    """
    synergy_db, edhrec_db, fixture_path, history_path = seeded_setup

    args = _args(
        db=synergy_db,
        fixture=fixture_path,
        edhrec_db=edhrec_db,
        history=history_path,
    )
    rc = handle_audit(args)
    # Identity-clean (pin was built on the same tree) ⇒ exit 0.
    assert rc == 0

    err = capsys.readouterr().err
    # The summary should have a *numeric* gem delta, not the em-dash
    # sentinel. Accept +N.NNNN or -N.NNNN (both signs are valid for
    # "we have data").
    assert "gem_Δ=—" not in err
    assert "gem_Δ=" in err
    # The numeric form uses a signed 4-decimal format.
    # Extract the substring after "gem_Δ=" — ensure the next 7 chars
    # start with a sign.
    gem_idx = err.index("gem_Δ=")
    fragment = err[gem_idx + len("gem_Δ=") : gem_idx + len("gem_Δ=") + 7]
    assert fragment[0] in "+-", f"expected signed number after gem_Δ=, got {fragment!r}"


def test_run_audit_nonexistent_edhrec_db_warns_and_degrades(
    seeded_setup: tuple[Path, Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the configured ``edhrec_db`` path does not exist, the audit
    must NOT fail — it degrades to "no gem data on the live side"
    (gem_Δ=—) and emits a stderr warning about the missing DB.
    """
    synergy_db, _real_edhrec_db, fixture_path, history_path = seeded_setup

    missing_edhrec = tmp_path / "does-not-exist.db"
    assert not missing_edhrec.exists()

    args = _args(
        db=synergy_db,
        fixture=fixture_path,
        edhrec_db=missing_edhrec,
        history=history_path,
    )
    rc = handle_audit(args)
    # Audit still succeeds (identical scores; gem fields degrade
    # silently).
    assert rc == 0

    err = capsys.readouterr().err
    # Warning about the missing EDHREC DB went to stderr.
    assert "not found" in err
    assert "hidden_gem_hit_rate will be unavailable" in err
    # Live has no gem data, so the delta is None → em-dash sentinel.
    assert "gem_Δ=—" in err
