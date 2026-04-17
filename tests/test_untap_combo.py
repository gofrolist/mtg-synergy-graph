"""Tests for the untap_combo rule.

Covers ``_find_untap_combo`` which matches broad-scope untap cards
(Dramatic Reversal, Unwinding Clock, Voltaic Key, Paradox Engine) to
commanders whose activated tap ability produces mana (Urza, Selvala)
or who have a ``TapsForMana`` trigger (Kinnan).

Narrow by design — the rule intentionally excludes tribal tap-payoff
commanders (Krenko, Lathril, Kumena) because untap combo cards aren't
their archetype and including them regressed 11 NDCG values.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.utility import _find_untap_combo


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE cards (
            name       TEXT PRIMARY KEY,
            card_types TEXT,
            types      TEXT,
            subtypes   TEXT
        );
        CREATE TABLE card_ports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            card_name        TEXT NOT NULL,
            port_type        TEXT NOT NULL,
            event_class      TEXT NOT NULL,
            valid_filter     TEXT,
            zone_origin      TEXT,
            zone_destination TEXT,
            cost_target      TEXT,
            raw_line         TEXT
        );
    """)
    return conn


@pytest.fixture()
def conn():
    c = _make_db()
    yield c
    c.close()


def _port(
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    cost_target: str = "",
    raw_line: str = "",
) -> dict:
    return {
        "card_name": card_name,
        "port_type": port_type,
        "event_class": event_class,
        "valid_filter": valid_filter,
        "zone_origin": "",
        "zone_destination": "",
        "cost_target": cost_target,
        "raw_line": raw_line,
    }


def _insert_port(
    conn: sqlite3.Connection,
    card_name: str,
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
) -> None:
    conn.execute(
        "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter) VALUES (?, ?, ?, ?)",
        (card_name, port_type, event_class, valid_filter),
    )


def _insert_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO cards (name, card_types) VALUES (?, ?)",
        (name, ""),
    )


def _candidates(results: list) -> set[str]:
    return {r.candidate for r in results}


def _mass_untap(conn: sqlite3.Connection, card_name: str) -> None:
    """Insert a ``UntapAll Permanent.YouCtrl`` port (Dramatic Reversal
    shape)."""
    _insert_port(conn, card_name, "effect", "UntapAll", valid_filter="Permanent.nonLand+YouCtrl")


class TestFindUntapCombo:
    def test_no_tap_cost_returns_empty(self, conn) -> None:
        """Commander with no tap cost and no TapsForMana trigger — gate
        fails, no matches even if Dramatic Reversal is in the DB."""
        _insert_card(conn, "Dramatic Reversal")
        _mass_untap(conn, "Dramatic Reversal")
        conn.commit()

        ports = [_port("Generic Cmdr", "trigger", "SpellCast")]
        assert _find_untap_combo(conn, ports, {"Generic Cmdr"}) == []

    def test_tap_cost_without_mana_rejected(self, conn) -> None:
        """Tap cost alone doesn't qualify — the ability must also
        produce Mana (or the commander must have TapsForMana). Krenko's
        tap-for-tokens shape fails the gate."""
        _insert_card(conn, "Dramatic Reversal")
        _mass_untap(conn, "Dramatic Reversal")
        conn.commit()

        ports = [
            _port("Krenko, Mob Boss", "cost", "tap"),
            _port("Krenko, Mob Boss", "effect", "Token"),
        ]
        assert _find_untap_combo(conn, ports, {"Krenko, Mob Boss"}) == []

    def test_tap_plus_mana_activates(self, conn) -> None:
        """Urza's ``tap_type`` cost paired with a Mana effect fires
        the rule."""
        _insert_card(conn, "Dramatic Reversal")
        _mass_untap(conn, "Dramatic Reversal")
        _insert_card(conn, "Unwinding Clock")
        _insert_port(conn, "Unwinding Clock", "static", "UntapOtherPlayer")
        conn.commit()

        ports = [
            _port("Urza, Lord High Artificer", "cost", "tap_type"),
            _port("Urza, Lord High Artificer", "effect", "Mana"),
        ]
        results = _find_untap_combo(conn, ports, {"Urza, Lord High Artificer"})
        assert "Dramatic Reversal" in _candidates(results)
        assert "Unwinding Clock" in _candidates(results)
        assert all(r.rule_id == "untap_combo" for r in results)

    def test_tapsformana_trigger_activates(self, conn) -> None:
        """Kinnan-style ``TapsForMana`` trigger alone (no tap cost)
        qualifies — his engine scales with every tap event."""
        _insert_card(conn, "Dramatic Reversal")
        _mass_untap(conn, "Dramatic Reversal")
        conn.commit()

        ports = [
            _port(
                "Kinnan, Bonder Prodigy",
                "trigger",
                "TapsForMana",
                valid_filter="Permanent.nonLand",
            )
        ]
        results = _find_untap_combo(conn, ports, {"Kinnan, Bonder Prodigy"})
        assert "Dramatic Reversal" in _candidates(results)

    def test_artifact_targeted_untap_matches(self, conn) -> None:
        """Voltaic-Key-shape ``Untap vf='Artifact'`` is in the pool."""
        _insert_card(conn, "Voltaic Key")
        _insert_port(conn, "Voltaic Key", "effect", "Untap", valid_filter="Artifact")
        conn.commit()

        ports = [
            _port("Urza, Lord High Artificer", "cost", "tap_type"),
            _port("Urza, Lord High Artificer", "effect", "Mana"),
        ]
        results = _find_untap_combo(conn, ports, {"Urza, Lord High Artificer"})
        assert "Voltaic Key" in _candidates(results)

    def test_creature_targeted_untap_excluded(self, conn) -> None:
        """``Untap vf='Creature'`` is NOT in this rule's pool — it's
        handled by the narrower ``_find_untap_synergy`` instead. Keeps
        the untap_combo pool focused on the combo shapes."""
        _insert_card(conn, "Quirion Ranger")
        _insert_port(
            conn,
            "Quirion Ranger",
            "effect",
            "Untap",
            valid_filter="Creature",
        )
        conn.commit()

        ports = [
            _port("Urza, Lord High Artificer", "cost", "tap_type"),
            _port("Urza, Lord High Artificer", "effect", "Mana"),
        ]
        results = _find_untap_combo(conn, ports, {"Urza, Lord High Artificer"})
        assert "Quirion Ranger" not in _candidates(results)

    def test_commander_excluded_from_results(self, conn) -> None:
        """Commander itself doesn't show up in its own recommendations."""
        _insert_card(conn, "Self Untap Cmdr")
        _mass_untap(conn, "Self Untap Cmdr")
        conn.commit()

        ports = [
            _port("Self Untap Cmdr", "cost", "tap"),
            _port("Self Untap Cmdr", "effect", "Mana"),
        ]
        results = _find_untap_combo(conn, ports, {"Self Untap Cmdr"})
        assert "Self Untap Cmdr" not in _candidates(results)

    def test_complement_metadata(self, conn) -> None:
        """Complements carry the expected rule_id and event metadata."""
        _insert_card(conn, "Paradox Engine")
        _insert_port(
            conn,
            "Paradox Engine",
            "effect",
            "UntapAll",
            valid_filter="Permanent.YouCtrl+nonLand",
        )
        conn.commit()

        ports = [
            _port("Cmdr", "cost", "tap"),
            _port("Cmdr", "effect", "Mana"),
        ]
        results = _find_untap_combo(conn, ports, {"Cmdr"})
        engine = next(r for r in results if r.candidate == "Paradox Engine")
        assert engine.rule_id == "untap_combo"
        assert engine.direction == "synergy"
        assert engine.cmdr_event == "tap_engine"
        assert engine.cand_event == "broad_untap"
