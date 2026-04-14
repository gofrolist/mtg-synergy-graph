"""Tests for mtg_synergy_graph.complement_rules.utility functions.

Uses an in-memory SQLite database with minimal card_ports rows to exercise
each branch of the five _find_* functions in utility.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.utility import (
    _find_cost_payoff_complements,
    _find_extra_land_plays,
    _find_flicker_synergy,
    _find_opponent_forcing,
    _find_wheel_synergy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEMA = """\
CREATE TABLE cards (
    name TEXT PRIMARY KEY,
    oracle_id TEXT,
    mana_cost TEXT,
    cmc REAL,
    types TEXT,
    supertypes TEXT,
    subtypes TEXT,
    card_types TEXT,
    colors TEXT,
    color_identity TEXT,
    power TEXT,
    toughness TEXT,
    loyalty TEXT,
    keywords TEXT,
    oracle_text TEXT,
    is_commander BOOLEAN DEFAULT FALSE,
    deck_hints TEXT,
    deck_needs TEXT,
    deck_has TEXT,
    edhrec_rank INTEGER,
    rarity TEXT,
    set_code TEXT
);

CREATE TABLE card_ports (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    card_name          TEXT NOT NULL REFERENCES cards(name),
    port_type          TEXT NOT NULL,
    event_class        TEXT NOT NULL,
    valid_filter       TEXT,
    zone_origin        TEXT,
    zone_destination   TEXT,
    phase              TEXT,
    affected_scope     TEXT,
    effect_zone        TEXT,
    cost_subtype       TEXT,
    cost_target        TEXT,
    trigger_source     TEXT,
    mana_restriction   TEXT,
    amount             TEXT,
    counter_type       TEXT,
    granted_keyword    TEXT,
    granted_ability    TEXT,
    execute_ref        TEXT,
    sub_ability_ref    TEXT,
    is_conditional     BOOLEAN DEFAULT FALSE,
    branch_kind        TEXT DEFAULT 'root',
    branch_parent      TEXT,
    source_svar        TEXT,
    chain_depth        INTEGER DEFAULT 0,
    scaling_expression TEXT,
    is_optional        BOOLEAN DEFAULT FALSE,
    is_combat          BOOLEAN DEFAULT FALSE,
    is_curse           BOOLEAN DEFAULT FALSE,
    replacement_event  TEXT,
    replacement_result TEXT,
    replacement_player TEXT,
    duration           TEXT,
    raw_line           TEXT
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@pytest.fixture()
def conn():
    """Fixture: in-memory DB with schema, auto-closed after each test."""
    c = _make_db()
    yield c
    c.close()


def _add_card(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO cards (name) VALUES (?)", (name,))


def _add_port(conn: sqlite3.Connection, card_name: str, **kwargs) -> None:
    _add_card(conn, card_name)
    cols = ["card_name", "port_type", "event_class"]
    vals = [card_name, kwargs.get("port_type", ""), kwargs.get("event_class", "")]
    for k, v in kwargs.items():
        if k not in ("port_type", "event_class"):
            cols.append(k)
            vals.append(v)
    placeholders = ",".join("?" * len(cols))
    col_str = ",".join(cols)
    conn.execute(f"INSERT INTO card_ports ({col_str}) VALUES ({placeholders})", vals)  # noqa: S608


def _port_row(**kwargs) -> dict:
    """Build a PortRow dict (used as cmdr_ports list element)."""
    return dict(kwargs)


def _candidates(results) -> set[str]:
    return {r.candidate for r in results}


def _rule_ids(results) -> set[str]:
    return {r.rule_id for r in results}


# ===========================================================================
# _find_opponent_forcing
# ===========================================================================


class TestFindOpponentForcing:
    """Test the opponent-forcing synergy matcher (Tergrid, Nekusar)."""

    def test_no_triggers_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="effect", event_class="Discard")]
        assert _find_opponent_forcing(conn, cmdr_ports, set()) == []

    def test_non_opponent_trigger_returns_empty(self, conn):
        """A Discarded trigger without opponent filter should not match."""
        cmdr_ports = [_port_row(port_type="trigger", event_class="Discarded", valid_filter="Card.Self")]
        assert _find_opponent_forcing(conn, cmdr_ports, set()) == []

    def test_discarded_opponent_trigger_finds_discard_effects(self, conn):
        """Tergrid-like: Discarded trigger with Opp filter -> Discard effects."""
        _add_port(conn, "Smallpox", port_type="effect", event_class="Discard", valid_filter="Opponent")
        _add_port(conn, "Dark Deal", port_type="effect", event_class="Discard", valid_filter="Player")
        _add_port(conn, "Self Discard", port_type="effect", event_class="Discard", valid_filter="Card.Self")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Discarded", valid_filter="Card.OppCtrl")]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Smallpox" in names
        assert "Dark Deal" in names
        # Card.Self is not in _OPPONENT_FILTERS
        assert "Self Discard" not in names

    def test_sacrificed_opponent_trigger_finds_sacrifice_effects(self, conn):
        """Tergrid-like: Sacrificed trigger with Opp filter -> Sacrifice effects."""
        _add_port(conn, "Plaguecrafter", port_type="effect", event_class="Sacrifice", valid_filter="Opponent")
        _add_port(conn, "All Sac", port_type="effect", event_class="SacrificeAll", valid_filter="Player")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Sacrificed", valid_filter="Creature.OppCtrl")]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Plaguecrafter" in names
        assert "All Sac" in names

    def test_drawn_opponent_trigger_finds_draw_effects(self, conn):
        """Nekusar-like: Drawn trigger with Player filter -> Draw effects."""
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw", valid_filter="Player")
        _add_port(conn, "Brainstorm", port_type="effect", event_class="Draw", valid_filter="Card.YouOwn")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.OppOwn")]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Windfall" in names
        # Card.YouOwn is not an opponent-targeting filter for Draw
        assert "Brainstorm" not in names

    def test_drawn_each_filter_matches(self, conn):
        """Drawn trigger with 'Each' filter should also match draws."""
        _add_port(conn, "Font of Mythos", port_type="effect", event_class="Draw", valid_filter="EachPlayer")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="EachPlayer")]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        assert "Font of Mythos" in _candidates(results)

    def test_excludes_commander_set(self, conn):
        """Cards in cmdr_set should be excluded from results."""
        _add_port(conn, "Smallpox", port_type="effect", event_class="Discard", valid_filter="Opponent")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Discarded", valid_filter="Card.OppCtrl")]
        results = _find_opponent_forcing(conn, cmdr_ports, {"Smallpox"})
        assert _candidates(results) == set()

    def test_dedup_across_effects(self, conn):
        """Same card should not appear twice if it matches multiple effect events."""
        _add_port(conn, "Dual Card", port_type="effect", event_class="Discard", valid_filter="Opponent")
        _add_port(conn, "Dual Card", port_type="effect", event_class="Sacrifice", valid_filter="Opponent")

        cmdr_ports = [
            _port_row(port_type="trigger", event_class="Discarded", valid_filter="Creature.OppCtrl"),
            _port_row(port_type="trigger", event_class="Sacrificed", valid_filter="Creature.OppCtrl"),
        ]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        assert len([r for r in results if r.candidate == "Dual Card"]) == 1

    def test_rule_id_and_events(self, conn):
        """Verify rule_id and event fields on results."""
        _add_port(conn, "Dark Deal", port_type="effect", event_class="Discard", valid_filter="Player")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Discarded", valid_filter="Card.OppCtrl")]
        results = _find_opponent_forcing(conn, cmdr_ports, set())
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "opponent_forcing"
        assert r.direction == "synergy"
        assert r.cmdr_event == "Discarded"
        assert r.cand_event == "force_Discard"


# ===========================================================================
# _find_wheel_synergy
# ===========================================================================


class TestFindWheelSynergy:
    """Test wheel synergy matcher (Nekusar wants Windfall-type cards)."""

    def test_no_drawn_trigger_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="effect", event_class="Draw")]
        assert _find_wheel_synergy(conn, cmdr_ports, set()) == []

    def test_self_drawn_trigger_returns_empty(self, conn):
        """Niv-Mizzet (Card.YouOwn) should NOT trigger wheel synergy."""
        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.YouOwn")]
        assert _find_wheel_synergy(conn, cmdr_ports, set()) == []

    def test_opponent_drawn_finds_wheels(self, conn):
        """Nekusar with Opp-facing Drawn trigger should find wheel effects."""
        # Windfall has both Draw and Discard effects
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(conn, "Windfall", port_type="effect", event_class="Discard")
        # Brainstorm has only Draw (no Discard) -- not a wheel
        _add_port(conn, "Brainstorm", port_type="effect", event_class="Draw")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.OppOwn")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Windfall" in names
        assert "Brainstorm" not in names

    def test_player_filter_drawn_trigger(self, conn):
        """Drawn trigger with 'Player' filter should also trigger wheel matching."""
        _add_port(conn, "Wheel of Fortune", port_type="effect", event_class="Draw")
        _add_port(conn, "Wheel of Fortune", port_type="effect", event_class="Discard")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Player")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Wheel of Fortune" in _candidates(results)

    def test_each_filter_drawn_trigger(self, conn):
        """Drawn trigger with 'Each' filter should also trigger wheel matching."""
        _add_port(conn, "Whispering Madness", port_type="effect", event_class="Draw")
        _add_port(conn, "Whispering Madness", port_type="effect", event_class="Discard")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="EachPlayer")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Whispering Madness" in _candidates(results)

    def test_excludes_commander_set(self, conn):
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(conn, "Windfall", port_type="effect", event_class="Discard")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.OppOwn")]
        results = _find_wheel_synergy(conn, cmdr_ports, {"Windfall"})
        assert _candidates(results) == set()

    def test_rule_id_and_events(self, conn):
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(conn, "Windfall", port_type="effect", event_class="Discard")

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.OppOwn")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "wheel_synergy"
        assert r.cmdr_event == "Drawn"
        assert r.cand_event == "wheel"


# ===========================================================================
# _find_cost_payoff_complements
# ===========================================================================


class TestFindCostPayoff:
    """Test cost-payoff matcher (Borborygmos: discard Land -> graveyard return)."""

    def test_no_cost_ports_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_non_discard_cost_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="cost", event_class="sacrifice", cost_subtype="1/Creature")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_generic_discard_cost_returns_empty(self, conn):
        """Discard<1/Card> (generic) should NOT trigger cost_payoff."""
        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Card")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_hand_discard_returns_empty(self, conn):
        """Discard<1/Hand> should NOT trigger cost_payoff."""
        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Hand")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_cardname_discard_returns_empty(self, conn):
        """Discard<1/CARDNAME> should NOT trigger cost_payoff."""
        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/CARDNAME")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_nickname_discard_returns_empty(self, conn):
        """Discard<1/NICKNAME> should NOT trigger cost_payoff."""
        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/NICKNAME")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []

    def test_typed_discard_finds_graveyard_return(self, conn):
        """Discard Land cost -> finds cards that return Lands from graveyard."""
        _add_port(
            conn,
            "Crucible of Worlds",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land",
        )
        _add_port(
            conn,
            "Regrowth",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Card",
        )

        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Land")]
        results = _find_cost_payoff_complements(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Crucible of Worlds" in names
        # "Card" does not contain "Land"
        assert "Regrowth" not in names

    def test_retrace_keyword_found(self, conn):
        """Typed discard cost -> Retrace keyword cards should match."""
        _add_port(conn, "Worm Harvest", port_type="keyword", event_class="Retrace")

        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Land")]
        results = _find_cost_payoff_complements(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Worm Harvest" in names
        retrace_results = [r for r in results if r.candidate == "Worm Harvest"]
        assert retrace_results[0].cand_event == "Retrace"
        assert retrace_results[0].cmdr_event == "discard_Land"

    def test_dredge_keyword_found(self, conn):
        """Typed discard cost -> Dredge keyword cards should match."""
        _add_port(conn, "Life from the Loam", port_type="keyword", event_class="Dredge3")

        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Land")]
        results = _find_cost_payoff_complements(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Life from the Loam" in names
        dredge_results = [r for r in results if r.candidate == "Life from the Loam"]
        assert dredge_results[0].cand_event == "Dredge"

    def test_excludes_commander_set(self, conn):
        _add_port(
            conn,
            "Crucible of Worlds",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land",
        )
        _add_port(conn, "Worm Harvest", port_type="keyword", event_class="Retrace")
        _add_port(conn, "Life from the Loam", port_type="keyword", event_class="Dredge3")

        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Land")]
        results = _find_cost_payoff_complements(
            conn, cmdr_ports, {"Crucible of Worlds", "Worm Harvest", "Life from the Loam"}
        )
        assert _candidates(results) == set()

    def test_dedup_across_graveyard_and_retrace(self, conn):
        """A card matching both graveyard return and Retrace should appear once."""
        _add_port(
            conn,
            "Multi Card",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land",
        )
        _add_port(conn, "Multi Card", port_type="keyword", event_class="Retrace")

        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="1/Land")]
        results = _find_cost_payoff_complements(conn, cmdr_ports, set())
        multi_results = [r for r in results if r.candidate == "Multi Card"]
        assert len(multi_results) == 1

    def test_multiple_discard_types(self, conn):
        """Commander with two typed discard costs should find cards for both."""
        _add_port(
            conn,
            "Land Return",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land",
        )
        _add_port(
            conn,
            "Creature Return",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Creature",
        )

        cmdr_ports = [
            _port_row(port_type="cost", event_class="discard", cost_subtype="1/Land"),
            _port_row(port_type="cost", event_class="discard", cost_subtype="1/Creature"),
        ]
        results = _find_cost_payoff_complements(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Land Return" in names
        assert "Creature Return" in names

    def test_cost_subtype_no_slash_ignored(self, conn):
        """A cost_subtype without '/' (no parts[1]) should not match."""
        cmdr_ports = [_port_row(port_type="cost", event_class="discard", cost_subtype="Land")]
        assert _find_cost_payoff_complements(conn, cmdr_ports, set()) == []


# ===========================================================================
# _find_flicker_synergy
# ===========================================================================


class TestFindFlickerSynergy:
    """Test flicker synergy matcher (Gonti: self-ETB + high-value effect)."""

    def _self_etb_port(self):
        return _port_row(
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )

    def _high_value_effect(self, event="Dig"):
        return _port_row(port_type="effect", event_class=event)

    def test_no_self_etb_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone")]
        assert _find_flicker_synergy(conn, cmdr_ports, set()) == []

    def test_self_etb_without_high_value_effect_returns_empty(self, conn):
        """Self-ETB trigger alone (no high-value effect) should not match."""
        cmdr_ports = [self._self_etb_port()]
        assert _find_flicker_synergy(conn, cmdr_ports, set()) == []

    def test_high_value_effect_without_self_etb_returns_empty(self, conn):
        cmdr_ports = [self._high_value_effect()]
        assert _find_flicker_synergy(conn, cmdr_ports, set()) == []

    def test_true_flicker_found(self, conn):
        """Card with BF->Exile AND Exile->BF effects should be found."""
        _add_port(
            conn,
            "Conjurer's Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Conjurer's Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect()]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert "Conjurer's Closet" in _candidates(results)

    def test_flickerwisp_pattern_found(self, conn):
        """Card with BF->Exile + DelayedTrigger should be found."""
        _add_port(
            conn,
            "Flickerwisp",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(conn, "Flickerwisp", port_type="trigger", event_class="DelayedTrigger")

        cmdr_ports = [self._self_etb_port(), self._high_value_effect()]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert "Flickerwisp" in _candidates(results)

    def test_exile_only_not_found(self, conn):
        """Card with only BF->Exile (no return) should NOT be found."""
        _add_port(
            conn,
            "Swords to Plowshares",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect()]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert "Swords to Plowshares" not in _candidates(results)

    def test_excludes_commander_set(self, conn):
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect()]
        results = _find_flicker_synergy(conn, cmdr_ports, {"Closet"})
        assert _candidates(results) == set()

    def test_rule_id_and_events(self, conn):
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect()]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "flicker_synergy"
        assert r.cmdr_event == "self_etb"
        assert r.cand_event == "flicker"

    def test_gain_control_is_high_value(self, conn):
        """GainControl effect should count as high-value."""
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect("GainControl")]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert len(results) == 1

    def test_generic_choice_is_high_value(self, conn):
        """GenericChoice effect should count as high-value."""
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [self._self_etb_port(), self._high_value_effect("GenericChoice")]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert len(results) == 1

    def test_non_self_etb_not_matched(self, conn):
        """ChangesZone trigger WITHOUT Card.Self filter should not count as self-ETB."""
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
        )
        _add_port(
            conn,
            "Closet",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Exile",
            zone_destination="Battlefield",
        )

        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="ChangesZone",
                valid_filter="Creature.YouCtrl",
                zone_destination="Battlefield",
            ),
            self._high_value_effect(),
        ]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_non_battlefield_etb_not_matched(self, conn):
        """ChangesZone trigger to non-Battlefield zone should not count."""
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="ChangesZone",
                valid_filter="Card.Self",
                zone_destination="Graveyard",
            ),
            self._high_value_effect(),
        ]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert results == []


# ===========================================================================
# _find_extra_land_plays
# ===========================================================================


class TestFindExtraLandPlays:
    """Test extra-land-plays matcher (Azusa -> landfall triggers)."""

    def test_no_static_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="trigger", event_class="ChangesZone")]
        assert _find_extra_land_plays(conn, cmdr_ports, set()) == []

    def test_non_adjust_land_static_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ CantBeSacrificed")]
        assert _find_extra_land_plays(conn, cmdr_ports, set()) == []

    def test_adjust_land_plays_finds_landfall(self, conn):
        """Azusa-like: AdjustLandPlays static -> landfall triggers."""
        _add_port(
            conn,
            "Lotus Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="root",
        )
        _add_port(
            conn,
            "Non Landfall",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="root",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, set())
        names = _candidates(results)
        assert "Lotus Cobra" in names
        assert "Non Landfall" not in names

    def test_excludes_commander_set(self, conn):
        _add_port(
            conn,
            "Lotus Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="root",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, {"Lotus Cobra"})
        assert _candidates(results) == set()

    def test_rule_id_and_events(self, conn):
        _add_port(
            conn,
            "Lotus Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="root",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, set())
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "effect_feeds_trigger"
        assert r.cmdr_event == "extra_land_plays"
        assert r.cand_event == "ChangesZone_Land"
        assert r.branch_kind == "root"

    def test_dedup_multiple_landfall_ports(self, conn):
        """Card with multiple landfall ports should only appear once."""
        _add_port(
            conn,
            "Omnath",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="root",
        )
        _add_port(
            conn,
            "Omnath",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="branch",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, set())
        omnath_results = [r for r in results if r.candidate == "Omnath"]
        assert len(omnath_results) == 1

    def test_branch_kind_propagated(self, conn):
        """branch_kind should be passed through from the port row."""
        _add_port(
            conn,
            "Avenger",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind="subability",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, set())
        assert len(results) == 1
        assert results[0].branch_kind == "subability"

    def test_null_branch_kind_defaults_to_root(self, conn):
        """NULL branch_kind in DB should default to 'root'."""
        _add_port(
            conn,
            "Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
            branch_kind=None,
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line="S:Mode$ AdjustLandPlays")]
        results = _find_extra_land_plays(conn, cmdr_ports, set())
        assert len(results) == 1
        assert results[0].branch_kind == "root"
