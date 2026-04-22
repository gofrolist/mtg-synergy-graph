"""``bench.py audit --unknowns`` handler tests (Unit 6 of plan 003).

Covers the SQL aggregation + Markdown output shape. Uses synthetic
in-memory fixtures so tests are fast and independent of the
production DB.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from mtg_synergy_graph.bench.handlers import handle_unknowns
from mtg_synergy_graph.db import open_db


def _args(db_path: Path) -> argparse.Namespace:
    """Minimal Namespace — handle_unknowns only reads ``args.db``."""
    return argparse.Namespace(db=str(db_path))


def _seed(conn: sqlite3.Connection, cards: list[tuple[str, int | None]]) -> None:
    """Insert ``(name, edhrec_rank)`` cards."""
    for name, rank in cards:
        conn.execute(
            "INSERT OR IGNORE INTO cards (name, card_types, color_identity, legal_commander, edhrec_rank) "
            "VALUES (?, 'Creature', 'G', 1, ?)",
            (name, rank),
        )


def _port(conn: sqlite3.Connection, card_name: str, port_type: str, event_class: str) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class) VALUES (?, ?, ?)",
        (card_name, port_type, event_class),
    )


def test_no_unknowns_emits_clean_message(tmp_path: Path, capsys) -> None:
    """An empty DB (no card_ports rows) yields no UNKNOWN rows —
    the handler exits 0 with a ``No UNKNOWN port shapes detected.``
    message."""
    db = tmp_path / "synergy.db"
    conn = open_db(db)
    conn.commit()
    conn.close()

    rc = handle_unknowns(_args(db))
    assert rc == 0
    out = capsys.readouterr().out
    assert "No UNKNOWN port shapes detected" in out


def test_unknowns_groups_by_subkind(tmp_path: Path, capsys) -> None:
    """Two cards with the same UNKNOWN subkind produce one row with
    ``distinct_cards = 2``. Two cards with different UNKNOWN
    subkinds produce two rows, one per subkind."""
    db = tmp_path / "synergy.db"
    conn = open_db(db)
    try:
        _seed(conn, [("CardA", 100), ("CardB", 200), ("CardC", 300)])
        # Same subkind on two cards.
        _port(conn, "CardA", "trigger", "NewMechanic")
        _port(conn, "CardB", "trigger", "NewMechanic")
        # Different subkind on one card.
        _port(conn, "CardC", "effect", "SomeNovelEffect")
        conn.commit()
    finally:
        conn.close()

    rc = handle_unknowns(_args(db))
    assert rc == 0
    out = capsys.readouterr().out
    assert "trigger.NewMechanic" in out
    assert "effect.SomeNovelEffect" in out
    # 2 distinct subkinds across 3 cards.
    assert "2 distinct UNKNOWN subkind(s) across 3 card(s)." in out


def test_unknowns_rank_weight_prefers_top_rank_cards(tmp_path: Path, capsys) -> None:
    """Rank-weight formula ``SUM(30001 - edhrec_rank)`` should rank
    a popular card (low rank number) higher than an obscure one
    (high rank number). So two subkinds each with 1 distinct card
    order by the card's rank-weight, with the popular one first.
    """
    db = tmp_path / "synergy.db"
    conn = open_db(db)
    try:
        _seed(conn, [("TopPlayedCard", 5), ("ObscureCard", 25000)])
        _port(conn, "TopPlayedCard", "trigger", "FancyNewTrigger")
        _port(conn, "ObscureCard", "trigger", "ObscureNewTrigger")
        conn.commit()
    finally:
        conn.close()

    rc = handle_unknowns(_args(db))
    assert rc == 0
    out = capsys.readouterr().out
    fancy_pos = out.index("FancyNewTrigger")
    obscure_pos = out.index("ObscureNewTrigger")
    assert fancy_pos < obscure_pos, "top-rank card should sort first"


def test_unknowns_handles_missing_edhrec_rank(tmp_path: Path, capsys) -> None:
    """Cards with NULL edhrec_rank contribute 0 to the rank_weight
    but still count toward distinct_cards. Guards against ranking
    explosions when metadata is partial."""
    db = tmp_path / "synergy.db"
    conn = open_db(db)
    try:
        _seed(conn, [("UnrankedCard", None)])
        _port(conn, "UnrankedCard", "effect", "MysteryEffect")
        conn.commit()
    finally:
        conn.close()

    rc = handle_unknowns(_args(db))
    assert rc == 0
    out = capsys.readouterr().out
    assert "effect.MysteryEffect" in out
    # rank_weight column reads 0 for this subkind.
    # Format uses comma-separated, so "0" appears at end of the row.
    for line in out.splitlines():
        if "MysteryEffect" in line:
            assert line.rstrip().endswith("0") or line.rstrip().endswith("     0")


def test_unknowns_skips_mapped_rows(tmp_path: Path, capsys) -> None:
    """Known ``(port_type, event_class)`` pairs map to non-UNKNOWN
    node_kinds and must NOT appear in the report. Guards against
    accidentally surfacing already-classified shapes."""
    db = tmp_path / "synergy.db"
    conn = open_db(db)
    try:
        _seed(conn, [("KnownCard", 10)])
        # (trigger, SpellCast) maps to CAST — should not appear in
        # --unknowns output.
        _port(conn, "KnownCard", "trigger", "SpellCast")
        # (trigger, NovelEvent) falls through to UNKNOWN.
        _port(conn, "KnownCard", "trigger", "NovelEvent")
        conn.commit()
    finally:
        conn.close()

    rc = handle_unknowns(_args(db))
    assert rc == 0
    out = capsys.readouterr().out
    assert "trigger.NovelEvent" in out
    assert "trigger.SpellCast" not in out


def test_unknowns_cli_dispatch_registers(tmp_path: Path, capsys) -> None:
    """The argparse CLI routes ``--unknowns`` to handle_unknowns.
    Verifies the register() wiring in bench/__init__.py."""
    import mtg_synergy_graph.bench  # ensure handlers register  # noqa: F401
    from mtg_synergy_graph.bench.cli import main

    db = tmp_path / "synergy.db"
    conn = open_db(db)
    conn.close()

    rc = main(["audit", "--unknowns", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bench.py audit --unknowns" in out


def test_unknowns_mutually_exclusive_with_other_modes() -> None:
    """``--unknowns`` is in the mode mutex group, so combining with
    ``--expect-identity`` raises SystemExit (argparse) before the
    handler runs."""
    import pytest

    from mtg_synergy_graph.bench.cli import main

    with pytest.raises(SystemExit):
        main(["audit", "--unknowns", "--expect-identity"])
