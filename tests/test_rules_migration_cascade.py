"""cascade_tribal migration identity test (plan 003 Unit 7).

Locks the POC-migration contract: the declarative `cascade_tribal`
rule row in `data/rules_seed.json` produces PortComplement records
whose set equals what the deleted Python helper would have produced.

Three guards:

1. ``cascade_tribal`` appears in ``DECLARATIVE_RULE_IDS`` — the
   registry routes it to the interpreter, not to the Python-helper
   dispatch.
2. ``cascade_tribal`` does NOT appear in any ``RuleGate`` — the
   legacy registration was removed.
3. On a fixture cascade commander, ``find_all_complements`` emits
   exactly the expected set of ``cascade_tribal`` PortComplements
   (one per non-commander card with a ``keyword.Cascade`` port).

End-to-end bitwise identity against the pinned golden fixture is
held by ``bench.py audit --expect-identity`` at CI time; this file
is the focused unit test that fails fast if the JSON row drifts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mtg_synergy_graph.complement_rules import find_all_complements
from mtg_synergy_graph.complement_rules.registry import DECLARATIVE_RULE_IDS, RULE_GATES
from mtg_synergy_graph.db import open_db


def _seed_card(conn: sqlite3.Connection, name: str, *, is_commander: bool = False) -> None:
    """Insert a minimal legal commander/creature card."""
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types, subtypes, cmc, color_identity, edhrec_rank, legal_commander) "
        "VALUES (?, 'Creature', '', 4, 'G', 1000, 1)",
        (name,),
    )


def _seed_cascade_port(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) "
        "VALUES (?, 'keyword', 'Cascade', '', 'K:Cascade')",
        (name,),
    )


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


def test_cascade_tribal_is_declarative() -> None:
    """Registry's DECLARATIVE_RULE_IDS carries cascade_tribal; this
    is what triggers the interpreter branch in
    ``_card_attr_complements``."""
    assert "cascade_tribal" in DECLARATIVE_RULE_IDS


def test_cascade_tribal_not_in_python_rule_gates() -> None:
    """No ``RuleGate`` entry for cascade_tribal — its Python helper
    was deleted. A rule_id lives in EITHER the declarative side OR
    the Python registry, never both."""
    gate_ids = {g.rule_id for g in RULE_GATES}
    assert "cascade_tribal" not in gate_ids


# ---------------------------------------------------------------------------
# End-to-end emission on a synthetic cascade commander
# ---------------------------------------------------------------------------


def test_cascade_tribal_emits_one_complement_per_other_cascade_card(tmp_path: Path) -> None:
    """Happy path: a commander with ``keyword.Cascade`` triggers the
    rule; every OTHER card carrying ``keyword.Cascade`` becomes a
    candidate. The commander itself is excluded via
    ``not_in_commander_set``.

    Uses a synthetic DB so the test is fast and independent of the
    prod cardsfolder's current Cascade population.
    """
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "Maelstrom Wanderer")
        _seed_card(conn, "Shardless Agent")
        _seed_card(conn, "Bloodbraid Elf")
        _seed_card(conn, "Imoti Celebrant of Bounty")
        _seed_card(conn, "UnrelatedCreature")  # no Cascade
        _seed_cascade_port(conn, "Maelstrom Wanderer")
        _seed_cascade_port(conn, "Shardless Agent")
        _seed_cascade_port(conn, "Bloodbraid Elf")
        _seed_cascade_port(conn, "Imoti Celebrant of Bounty")
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
            "VALUES ('UnrelatedCreature', 'trigger', 'SpellCast', '')"
        )

        # Also seed the rules table via the importer's seed helper.
        # Without this, the interpreter has no rules to run.
        from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db

        seed_rules_db(conn)

        conn.commit()

        comps = find_all_complements(conn, ["Maelstrom Wanderer"])
        cascade = [c for c in comps if c.rule_id == "cascade_tribal"]
        candidates = {c.candidate for c in cascade}
        assert candidates == {"Shardless Agent", "Bloodbraid Elf", "Imoti Celebrant of Bounty"}
        # Every complement carries the declarative row's declared
        # cmdr_event / cand_event / filter_group.
        for c in cascade:
            assert c.cmdr_event == "cascade_tribal"
            assert c.cand_event == "same_keyword_partner"
            assert c.filter_group == ""
            assert c.direction == "synergy"
    finally:
        conn.close()


def test_cascade_tribal_gate_fails_without_cascade_keyword(tmp_path: Path) -> None:
    """A commander without a Cascade keyword port → zero
    cascade_tribal complements. Gate short-circuits before the SQL
    query against card_ports.
    """
    conn = open_db(tmp_path / "synergy.db")
    try:
        _seed_card(conn, "NonCascadeCommander")
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, raw_line) "
            "VALUES ('NonCascadeCommander', 'trigger', 'SpellCast', '')"
        )
        _seed_card(conn, "Bloodbraid Elf")
        _seed_cascade_port(conn, "Bloodbraid Elf")

        from mtg_synergy_graph.port_graph.rules_schema import seed_rules_db

        seed_rules_db(conn)
        conn.commit()

        comps = find_all_complements(conn, ["NonCascadeCommander"])
        cascade = [c for c in comps if c.rule_id == "cascade_tribal"]
        assert cascade == []
    finally:
        conn.close()
