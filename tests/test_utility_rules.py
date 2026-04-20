"""Tests for mtg_synergy_graph.complement_rules.utility functions.

Uses an in-memory SQLite database with minimal card_ports rows to exercise
each branch of the five _find_* functions in utility.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.utility import (
    _classify_tap_type_axis,
    _find_cardpower_axis_feeders,
    _find_cost_payoff_complements,
    _find_counter_axis_feeders,
    _find_counter_target_payoff,
    _find_creature_untap_engine,
    _find_creatures_as_lands_landfall,
    _find_damage_doubler_synergy,
    _find_extra_land_plays,
    _find_flicker_synergy,
    _find_gy_fuel_feeders,
    _find_hand_size_feeders,
    _find_land_bounce_feeders,
    _find_life_total_feeders,
    _find_lifegain_feeders,
    _find_mana_doubler_synergy,
    _find_modified_axis_feeders,
    _find_monarch_synergy,
    _find_opponent_forcing,
    _find_tap_type_feeders,
    _find_wheel_synergy,
    _is_big_hand_commander,
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

    def test_self_drawn_with_token_finds_wheels(self, conn):
        """The Locust God (Card.YouCtrl Drawn + Token effect) wants wheels — each
        draw makes a token, so forced mass-draw = mass tokens."""
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Windfall",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.YouCtrl"),
            _port_row(port_type="effect", event_class="Token"),
        ]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Windfall" in _candidates(results)

    def test_self_drawn_damage_payoff_skips_wheels(self, conn):
        """Niv-Mizzet Parun (self Drawn + DealDamage payoff) prefers cantrips,
        not wheels — EDHREC deck runs high-count repeatable draws."""
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Windfall",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )
        cmdr_ports = [
            _port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.YouCtrl"),
            _port_row(port_type="effect", event_class="DealDamage"),
        ]
        assert _find_wheel_synergy(conn, cmdr_ports, set()) == []

    def test_loot_card_excluded(self, conn):
        """Loot cards (Bag of Holding: Discard NumCards=1) are not wheels. Only
        ``Mode: Hand`` discards count — those empty the hand, which is what
        triggers mass-draw payoffs."""
        _add_port(conn, "Bag of Holding", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Bag of Holding",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'You', 'NumCards': '1', 'Mode': 'TgtChoose'}",
        )
        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.YouCtrl")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Bag of Holding" not in _candidates(results)

    def test_opponent_drawn_finds_wheels(self, conn):
        """Nekusar with Opp-facing Drawn trigger should find wheel effects."""
        # Windfall has both Draw and Mode: Hand Discard effects
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Windfall",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )
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
        _add_port(
            conn,
            "Wheel of Fortune",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Player")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Wheel of Fortune" in _candidates(results)

    def test_each_filter_drawn_trigger(self, conn):
        """Drawn trigger with 'Each' filter should also trigger wheel matching."""
        _add_port(conn, "Whispering Madness", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Whispering Madness",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="EachPlayer")]
        results = _find_wheel_synergy(conn, cmdr_ports, set())
        assert "Whispering Madness" in _candidates(results)

    def test_excludes_commander_set(self, conn):
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Windfall",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )

        cmdr_ports = [_port_row(port_type="trigger", event_class="Drawn", valid_filter="Card.OppOwn")]
        results = _find_wheel_synergy(conn, cmdr_ports, {"Windfall"})
        assert _candidates(results) == set()

    def test_rule_id_and_events(self, conn):
        _add_port(conn, "Windfall", port_type="effect", event_class="Draw")
        _add_port(
            conn,
            "Windfall",
            port_type="effect",
            event_class="Discard",
            raw_line="{'Defined': 'Player', 'Mode': 'Hand'}",
        )

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

    def test_lagrella_temporary_exile_qualifies(self, conn):
        """Lagrella-style ETB: exiles other creatures until she leaves
        (ChangeZone BF→Exile with ReturnAbility in raw_line). This is
        a flicker engine — re-entering her re-exiles targets =
        extra ETB triggers. The gate accepts this ChangeZone shape."""
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
        lagrella_etb_exile = _port_row(
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Exile",
            valid_filter="Creature.Other",
            raw_line=(
                "{'DB': 'ChangeZone', 'Origin': 'Battlefield', "
                "'Destination': 'Exile', 'Duration': 'UntilHostLeavesPlay', "
                "'ReturnAbility': 'DBReturn'}"
            ),
        )
        cmdr_ports = [self._self_etb_port(), lagrella_etb_exile]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert "Closet" in _candidates(results)

    def test_brinelin_bounce_not_flicker(self, conn):
        """Plain bounce (ChangeZone BF→Hand, Brinelin pattern) is NOT
        a flicker engine — the commander doesn't retrigger anything.
        This gate must reject it so generic bounce commanders don't
        pick up the flicker-support pool."""
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
        brinelin_bounce = _port_row(
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Battlefield",
            zone_destination="Hand",
            valid_filter="Permanent.nonLand",
            raw_line="{'DB': 'ChangeZone', 'Origin': 'Battlefield', 'Destination': 'Hand'}",
        )
        cmdr_ports = [self._self_etb_port(), brinelin_bounce]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_sharuum_reanimation_not_flicker(self, conn):
        """GY→BF reanimation (Sharuum, Bladewing) has dedicated
        artifact_recursion / gy_loader axes. The flicker gate must
        reject zone_origin=Graveyard so reanimator commanders don't
        absorb flicker-support cards."""
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
        reanimation = _port_row(
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            zone_destination="Battlefield",
            valid_filter="Artifact.YouCtrl",
            raw_line="{'Origin': 'Graveyard', 'Destination': 'Battlefield'}",
        )
        cmdr_ports = [self._self_etb_port(), reanimation]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_lavinia_detain_qualifies(self, conn):
        """Lavinia of the Tenth's ETB Detain on opponent permanents is
        a temporary disable — flickering her re-detains different
        targets, giving repeated removal. Detain is always on
        opponent permanents, so the event class alone qualifies."""
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
        lavinia_detain = _port_row(
            port_type="effect",
            event_class="Detain",
            valid_filter="Valid Permanent.OppCtrl+nonLand+cmcLE4",
        )
        cmdr_ports = [self._self_etb_port(), lavinia_detain]
        results = _find_flicker_synergy(conn, cmdr_ports, set())
        assert "Closet" in _candidates(results)

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


# ---------------------------------------------------------------------------
# _find_creatures_as_lands_landfall
# ---------------------------------------------------------------------------


class TestFindCreaturesAsLandsLandfall:
    """Ashaya-style static (AddType Land on Creature Affected) should
    synthesize a landfall-payoff axis, since her creatures are also
    lands and thus trigger landfall effects on ETB."""

    _ASHAYA_STATIC = (
        "{'Mode': 'Continuous', 'Affected': 'Creature.!token+YouCtrl', "
        "'AddType': 'Forest & Land', 'Description': \"Nontoken creatures "
        'you control are Forest lands in addition to their other types."}'
    )

    def test_no_type_bending_static_skips(self, conn):
        """Commander without the Affected=Creature / AddType=Land static
        doesn't activate."""
        cmdr_ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_creatures_as_lands_landfall(conn, cmdr_ports, set()) == []

    def test_land_to_creature_static_skips(self, conn):
        """Opposite direction (Nissa: Forests are creatures) must NOT
        fire — that's a separate archetype."""
        cmdr_ports = [
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line="{'Mode': 'Continuous', 'Affected': 'Forest.YouCtrl', 'AddType': 'Creature & Elf'}",
            )
        ]
        assert _find_creatures_as_lands_landfall(conn, cmdr_ports, set()) == []

    def test_ashaya_matches_landfall_trigger(self, conn):
        """Ashaya static matches candidates with ChangesZone Land ETB
        triggers (Rampaging Baloths, Lotus Cobra)."""
        _add_port(
            conn,
            "Rampaging Baloths",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )
        _add_port(
            conn,
            "Lotus Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )
        # Non-landfall trigger — should NOT match
        _add_port(
            conn,
            "Grim Haruspex",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.YouCtrl",
            zone_destination="Graveyard",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line=self._ASHAYA_STATIC)]
        names = _candidates(_find_creatures_as_lands_landfall(conn, cmdr_ports, set()))
        assert "Rampaging Baloths" in names
        assert "Lotus Cobra" in names
        assert "Grim Haruspex" not in names

    def test_ashaya_matches_landplayed_trigger(self, conn):
        """LandPlayed triggers (Emeria Angel, Scute Swarm) also match
        — they fire when a land is played, which Ashaya's creatures
        functionally become."""
        _add_port(
            conn,
            "Emeria Angel",
            port_type="trigger",
            event_class="LandPlayed",
            valid_filter="",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line=self._ASHAYA_STATIC)]
        names = _candidates(_find_creatures_as_lands_landfall(conn, cmdr_ports, set()))
        assert "Emeria Angel" in names

    def test_commander_excluded_from_pool(self, conn):
        _add_port(
            conn,
            "Ashaya, Soul of the Wild",
            port_type="trigger",
            event_class="LandPlayed",
            valid_filter="",
        )
        _add_port(
            conn,
            "Rampaging Baloths",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )

        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line=self._ASHAYA_STATIC)]
        names = _candidates(_find_creatures_as_lands_landfall(conn, cmdr_ports, {"Ashaya, Soul of the Wild"}))
        assert "Ashaya, Soul of the Wild" not in names
        assert "Rampaging Baloths" in names

    def test_rule_id_and_events(self, conn):
        _add_port(
            conn,
            "Lotus Cobra",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Land.YouCtrl",
            zone_destination="Battlefield",
        )
        cmdr_ports = [_port_row(port_type="static", event_class="Continuous", raw_line=self._ASHAYA_STATIC)]
        results = _find_creatures_as_lands_landfall(conn, cmdr_ports, set())
        assert len(results) == 1
        r = results[0]
        assert r.rule_id == "creatures_as_lands_landfall"
        assert r.cmdr_event == "creatures_are_lands"
        assert r.cand_event == "landfall_payoff"


# ---------------------------------------------------------------------------
# _find_draw_synergy
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _find_mana_doubler_synergy
# ---------------------------------------------------------------------------


class TestManaDoublerSynergy:
    def test_tapsformana_finds_produce_mana_replacement(self, conn):
        """TapsForMana trigger -> finds ProduceMana replacement."""
        _add_port(
            conn, "Mana Reflection", port_type="replacement", event_class="ProduceMana", replacement_event="ProduceMana"
        )
        cmdr_ports = [_port_row(port_type="trigger", event_class="TapsForMana")]
        results = _find_mana_doubler_synergy(conn, cmdr_ports, set())
        assert len(results) == 1
        assert results[0].candidate == "Mana Reflection"
        assert results[0].rule_id == "mana_doubler"

    def test_no_mana_trigger_returns_empty(self, conn):
        _add_port(
            conn, "Mana Reflection", port_type="replacement", event_class="ProduceMana", replacement_event="ProduceMana"
        )
        cmdr_ports = [_port_row(port_type="trigger", event_class="SpellCast")]
        results = _find_mana_doubler_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_excludes_commander(self, conn):
        _add_port(conn, "Cmdr", port_type="replacement", event_class="ProduceMana", replacement_event="ProduceMana")
        cmdr_ports = [_port_row(port_type="trigger", event_class="TapsForMana")]
        results = _find_mana_doubler_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results == []


# ===========================================================================
# _find_monarch_synergy
# ===========================================================================


class TestFindMonarchSynergy:
    """Queen Marchesa: her ETB makes her monarch. Cards with the same
    BecomeMonarch effect and pillowfort statics (CantAttackUnless) are
    the archetype's core."""

    def _monarch_cmdr_ports(self):
        return [_port_row(port_type="effect", event_class="BecomeMonarch")]

    def test_no_monarch_effect_returns_empty(self, conn):
        cmdr_ports = [_port_row(port_type="effect", event_class="Token")]
        assert _find_monarch_synergy(conn, cmdr_ports, set()) == []

    def test_finds_other_monarch_givers(self, conn):
        """Courts (Court of Grace, Ambition) also give monarch — core picks."""
        _add_port(conn, "Court of Grace", port_type="effect", event_class="BecomeMonarch")
        _add_port(conn, "Thorn of the Black Rose", port_type="effect", event_class="BecomeMonarch")

        results = _find_monarch_synergy(conn, self._monarch_cmdr_ports(), set())
        names = _candidates(results)
        assert "Court of Grace" in names
        assert "Thorn of the Black Rose" in names

    def test_finds_pillowfort(self, conn):
        """Ghostly Prison / Windborn Muse (CantAttackUnless) protect the monarch."""
        _add_port(conn, "Ghostly Prison", port_type="static", event_class="CantAttackUnless")
        _add_port(conn, "Windborn Muse", port_type="static", event_class="CantAttackUnless")
        _add_port(conn, "Unrelated Static", port_type="static", event_class="Continuous")

        results = _find_monarch_synergy(conn, self._monarch_cmdr_ports(), set())
        names = _candidates(results)
        assert "Ghostly Prison" in names
        assert "Windborn Muse" in names
        assert "Unrelated Static" not in names

    def test_excludes_commander(self, conn):
        _add_port(conn, "Queen Marchesa", port_type="effect", event_class="BecomeMonarch")
        results = _find_monarch_synergy(conn, self._monarch_cmdr_ports(), {"Queen Marchesa"})
        assert "Queen Marchesa" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(conn, "Court of Grace", port_type="effect", event_class="BecomeMonarch")
        results = _find_monarch_synergy(conn, self._monarch_cmdr_ports(), set())
        assert all(r.rule_id == "monarch_synergy" for r in results)


# ===========================================================================
# _find_counter_target_payoff
# ===========================================================================


class TestFindCounterTargetPayoff:
    """XP-counter distributor archetype: commander scales with
    Experience counters (``scales_with YourCountersExperience`` — a
    Forge SVar emitted by any card using the XP-counter mechanism)
    AND actively distributes P1P1 counters to OTHER creatures. Both
    gates are mechanism-specific, not commander-name-specific —
    future printings using XP counters automatically qualify. Cards
    that benefit from being counter-targeted (CounterAdded self
    trigger, scales_with own P1P1) are the canonical payoffs."""

    def _ezuri_ports(self):
        return [
            _port_row(port_type="scales_with", event_class="YourCountersExperience"),
            _port_row(
                port_type="effect",
                event_class="PutCounter",
                valid_filter="Creature.Other+YouCtrl",
                counter_type="P1P1",
            ),
        ]

    def test_no_put_counter_on_other_returns_empty(self, conn):
        """Commander without a P1P1 distribution effect yields nothing."""
        cmdr_ports = [
            _port_row(port_type="scales_with", event_class="YourCountersExperience"),
            _port_row(port_type="effect", event_class="PutCounter", valid_filter="You", counter_type="Experience"),
        ]
        assert _find_counter_target_payoff(conn, cmdr_ports, set()) == []

    def test_no_xp_scaling_skips_non_xp_commanders(self, conn):
        """Commanders without the XP-counter scaler (Ghave / Heliod /
        Lathiel distribute P1P1 but through sac / lifegain mechanisms)
        don't activate this rule — other counter-caring axes are
        covered by counter_producer / counter_axis_feeder /
        proliferate_synergy."""
        _add_port(
            conn,
            "Fathom Mage",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="P1P1",
        )
        cmdr_ports = [
            _port_row(port_type="effect", event_class="PutCounter", valid_filter="Creature", counter_type="P1P1"),
        ]
        assert _find_counter_target_payoff(conn, cmdr_ports, set()) == []

    def test_finds_counter_added_trigger(self, conn):
        """Fathom Mage: 'Whenever a +1/+1 counter is put on me, draw a card.'"""
        _add_port(
            conn,
            "Fathom Mage",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="P1P1",
        )
        results = _find_counter_target_payoff(conn, self._ezuri_ports(), set())
        assert "Fathom Mage" in _candidates(results)

    def test_finds_scales_with_p1p1(self, conn):
        """Gyre Sage / Chasm Skulker grow with +1/+1 counters."""
        _add_port(conn, "Gyre Sage", port_type="scales_with", event_class="CardCounters.P1P1")
        _add_port(conn, "Chasm Skulker", port_type="scales_with", event_class="CardCounters.P1P1")
        results = _find_counter_target_payoff(conn, self._ezuri_ports(), set())
        names = _candidates(results)
        assert "Gyre Sage" in names
        assert "Chasm Skulker" in names

    def test_excludes_other_counter_types(self, conn):
        """Loyalty / charge / energy counter triggers don't match P1P1 distribution."""
        _add_port(
            conn,
            "Loyalty Triggered",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="LOYALTY",
        )
        _add_port(conn, "Charge Scales", port_type="scales_with", event_class="CardCounters.CHARGE")
        results = _find_counter_target_payoff(conn, self._ezuri_ports(), set())
        names = _candidates(results)
        assert "Loyalty Triggered" not in names
        assert "Charge Scales" not in names

    def test_self_only_put_counter_does_not_fire(self, conn):
        """Commander that only puts counters on self (not targeting others) doesn't qualify —
        this rule is about the distribution-target archetype."""
        _add_port(
            conn,
            "Fathom Mage",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="P1P1",
        )
        cmdr_ports = [
            _port_row(port_type="scales_with", event_class="YourCountersExperience"),
            _port_row(port_type="effect", event_class="PutCounter", valid_filter="Self", counter_type="P1P1"),
        ]
        assert _find_counter_target_payoff(conn, cmdr_ports, set()) == []

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Ezuri",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="P1P1",
        )
        results = _find_counter_target_payoff(conn, self._ezuri_ports(), {"Ezuri"})
        assert "Ezuri" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Fathom Mage",
            port_type="trigger",
            event_class="CounterAdded",
            valid_filter="Card.Self",
            counter_type="P1P1",
        )
        results = _find_counter_target_payoff(conn, self._ezuri_ports(), set())
        assert all(r.rule_id == "counter_target_payoff" for r in results)


# ===========================================================================
# _find_creature_untap_engine
# ===========================================================================


class TestFindCreatureUntapEngine:
    """Selvala taps herself for mana. Cards that untap a creature (Quirion
    Ranger, Scryb Ranger, Staff of Domination) are the archetype's core.

    Distinct from untap_combo (narrow artifact-untap pool for Urza/Emry)
    and untap_synergy (broad, covers all tap-cost commanders including
    Krenko's non-mana tap). This rule targets tap-for-mana engines
    specifically so creature-untappers rank above the tribal/utility
    tap cards that existing rules already surface.
    """

    def _selvala_ports(self):
        return [
            _port_row(port_type="cost", event_class="tap"),
            _port_row(port_type="effect", event_class="Mana"),
        ]

    def test_no_mana_effect_skips(self, conn):
        """Krenko (tap for Tokens) doesn't qualify — not a mana engine."""
        _add_port(conn, "Quirion Ranger", port_type="effect", event_class="Untap", valid_filter="Creature")
        cmdr_ports = [
            _port_row(port_type="cost", event_class="tap"),
            _port_row(port_type="effect", event_class="Token"),
        ]
        assert _find_creature_untap_engine(conn, cmdr_ports, set()) == []

    def test_no_tap_cost_skips(self, conn):
        """Commander without tap cost yields nothing."""
        _add_port(conn, "Quirion Ranger", port_type="effect", event_class="Untap", valid_filter="Creature")
        cmdr_ports = [_port_row(port_type="effect", event_class="Mana")]
        assert _find_creature_untap_engine(conn, cmdr_ports, set()) == []

    def test_finds_creature_untappers(self, conn):
        """Quirion Ranger / Scryb Ranger / Staff of Domination all untap a
        creature — premium Selvala enablers."""
        _add_port(conn, "Quirion Ranger", port_type="effect", event_class="Untap", valid_filter="Creature")
        _add_port(conn, "Scryb Ranger", port_type="effect", event_class="Untap", valid_filter="Creature")
        _add_port(conn, "Staff of Domination", port_type="effect", event_class="Untap", valid_filter="")

        results = _find_creature_untap_engine(conn, self._selvala_ports(), set())
        names = _candidates(results)
        assert "Quirion Ranger" in names
        assert "Scryb Ranger" in names
        assert "Staff of Domination" in names

    def test_excludes_non_creature_untap(self, conn):
        """Land-only untap (Wilderness Reclamation) is Kinnan territory,
        not Selvala — this rule should skip it."""
        _add_port(
            conn, "Wilderness Reclamation", port_type="effect", event_class="UntapAll", valid_filter="Land.YouCtrl"
        )
        results = _find_creature_untap_engine(conn, self._selvala_ports(), set())
        assert "Wilderness Reclamation" not in _candidates(results)

    def test_excludes_commander(self, conn):
        _add_port(conn, "Selvala", port_type="effect", event_class="Untap", valid_filter="Creature")
        results = _find_creature_untap_engine(conn, self._selvala_ports(), {"Selvala"})
        assert "Selvala" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(conn, "Quirion Ranger", port_type="effect", event_class="Untap", valid_filter="Creature")
        results = _find_creature_untap_engine(conn, self._selvala_ports(), set())
        assert all(r.rule_id == "creature_untap_engine" for r in results)


class TestFindCounterAxisFeeders:
    """General rule: for any commander port (trigger, scales_with,
    static) whose valid_filter contains a ``counters_GE_<TYPE>``
    qualifier on a broad scope (YouCtrl / Other, not Self), extract the
    (main_subject, counter_type) axis and match candidates that:

    - scale/payoff on the same axis (same ``counters_GE_<TYPE>`` filter
      on scales_with or static-Continuous ports),
    - produce the matching counter type on creatures
      (PutCounter[All] counter_type=<TYPE> valid_filter ~ Creature),
    - have the matching etbCounter:<TYPE>:N keyword, or
    - for P1P1 specifically, have Persist / Undying / Modular (the
      native P1P1/M1M1 cycling keywords).

    Self-only filters (Card.Self+counters_*) are rejected — they
    describe a card scaling with its OWN counters (Incubation Druid,
    Ochre Jelly) rather than a commander-level axis. Dedup to one
    complement per card, highest-priority tier winning."""

    def _marchesa_ports(self):
        return [
            _port_row(
                port_type="trigger",
                event_class="ChangesZone",
                zone_origin="Battlefield",
                zone_destination="Graveyard",
                valid_filter="Card.YouCtrl+counters_GE1_P1P1",
            )
        ]

    def _hamza_ports(self):
        return [
            _port_row(
                port_type="scales_with",
                event_class="Valid",
                valid_filter="Creature.YouCtrl+counters_GE1_P1P1",
            )
        ]

    def test_no_counter_axis_skips(self, conn):
        """Commander without any counters_GE filter doesn't activate."""
        ezuri_ports = [_port_row(port_type="scales_with", event_class="YourCountersExperience")]
        _add_port(
            conn,
            "Drana, Liberator of Malakir",
            port_type="effect",
            event_class="PutCounterAll",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl+attacking",
        )
        assert _find_counter_axis_feeders(conn, ezuri_ports, set()) == []

    def test_self_only_counters_filter_skips(self, conn):
        """Card.Self+counters_* (Incubation Druid, Ochre Jelly) is a
        self-scaler, not a commander-level axis."""
        ports = [_port_row(port_type="scales_with", event_class="Valid", valid_filter="Card.Self+counters_GE1_P1P1")]
        assert _find_counter_axis_feeders(conn, ports, set()) == []

    def test_marchesa_activates_via_trigger_filter(self, conn):
        """Marchesa's ChangesZone death trigger with counters_GE_P1P1
        qualifier activates the rule and matches P1P1 producers."""
        _add_port(
            conn,
            "Drana, Liberator of Malakir",
            port_type="effect",
            event_class="PutCounterAll",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl+attacking",
        )
        _add_port(
            conn, "Drana, Liberator of Malakir", port_type="trigger", event_class="DamageDone", valid_filter="Card.Self"
        )
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        names = _candidates(results)
        assert "Drana, Liberator of Malakir" in names

    def test_hamza_activates_via_scales_with(self, conn):
        """Hamza's scales_with with counters_GE_P1P1 qualifier
        activates the rule the same way Marchesa's trigger does."""
        _add_port(
            conn,
            "Drana, Liberator of Malakir",
            port_type="effect",
            event_class="PutCounterAll",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl+attacking",
        )
        _add_port(
            conn, "Drana, Liberator of Malakir", port_type="trigger", event_class="DamageDone", valid_filter="Card.Self"
        )
        results = _find_counter_axis_feeders(conn, self._hamza_ports(), set())
        names = _candidates(results)
        assert "Drana, Liberator of Malakir" in names

    def test_counter_axis_payoff_tier(self, conn):
        """Cards whose scales_with / static Continuous filters on
        ``counters_GE_<TYPE>`` mirror the commander's axis — highest
        priority tier."""
        _add_port(
            conn,
            "Inspiring Call",
            port_type="scales_with",
            event_class="Valid",
            valid_filter="Creature.YouCtrl+counters_GE1_P1P1",
        )
        _add_port(
            conn,
            "Abzan Falconer",
            port_type="static",
            event_class="Continuous",
            raw_line=(
                "{'Mode': 'Continuous', 'Affected': 'Creature.YouCtrl+counters_GE1_P1P1', 'AddKeyword': 'Flying'}"
            ),
        )
        results = _find_counter_axis_feeders(conn, self._hamza_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Inspiring Call") == "counter_axis_payoff"
        assert events.get("Abzan Falconer") == "counter_axis_payoff"

    def test_counter_producer_tier(self, conn):
        """PutCounter[All] effects producing the matching counter type
        on creatures — Drana, Thran Vigil, Unspeakable Symbol."""
        _add_port(
            conn,
            "Thran Vigil",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl",
        )
        _add_port(
            conn, "Thran Vigil", port_type="trigger", event_class="ChangesZoneAll", valid_filter="Creature.YouOwn"
        )
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Thran Vigil") == "counter_producer"

    def test_etb_counter_keyword_tier(self, conn):
        """etbCounter:P1P1 keyword cards (Iron Apprentice, Walking
        Ballista) enter with a counter that matches the axis."""
        _add_port(conn, "Iron Apprentice", port_type="keyword", event_class="etbCounter:P1P1:1")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Iron Apprentice") == "etb_counter_keyword"

    def test_persist_undying_modular_only_for_p1p1_axis(self, conn):
        """Persist (returns with -1/-1), Undying (returns with +1/+1),
        and Modular (dies → move P1P1 counters) all involve the P1P1
        axis. They qualify only when the commander's counter type is
        P1P1 (not for a hypothetical M1M1-only axis)."""
        _add_port(conn, "Glen Elendra Archmage", port_type="keyword", event_class="Persist")
        _add_port(conn, "Strangleroot Geist", port_type="keyword", event_class="Undying")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Glen Elendra Archmage") == "self_recur_keyword"
        assert events.get("Strangleroot Geist") == "self_recur_keyword"

    def test_counter_producer_rejects_self_sac_gated(self, conn):
        """Pizzasaur's PutCounter is downstream of a self-sac activated
        ability (cost_target=self). Its P1P1 output isn't a sustainable
        distributor for the axis — reject. Only cards whose PutCounter
        is triggered or cheaply activated (no self-sac cost) qualify."""
        _add_port(
            conn,
            "Pizzasaur",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature",
        )
        _add_port(
            conn,
            "Pizzasaur",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Card.Self",
            zone_destination="Battlefield",
        )
        _add_port(conn, "Pizzasaur", port_type="cost", event_class="sacrifice", cost_target="self")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        assert "Pizzasaur" not in _candidates(results)

    def test_one_complement_per_card_highest_priority_wins(self, conn):
        """Mikaeus has BOTH etbCounter:P1P1 AND PutCounterAll — he gets
        ONE match in the higher-priority tier (counter_producer, since
        PutCounterAll > etbCounter in priority). Avoids double-counting
        that would displace single-tier specialists."""
        _add_port(conn, "Mikaeus, the Lunarch", port_type="keyword", event_class="etbCounter:P1P1:X")
        _add_port(
            conn,
            "Mikaeus, the Lunarch",
            port_type="effect",
            event_class="PutCounterAll",
            counter_type="P1P1",
            valid_filter="Creature.StrictlyOther+YouCtrl",
        )
        _add_port(conn, "Mikaeus, the Lunarch", port_type="trigger", event_class="SpellCast", valid_filter="Card")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        mik = [r for r in results if r.candidate == "Mikaeus, the Lunarch"]
        assert len(mik) == 1
        assert mik[0].cand_event == "counter_producer"

    def test_excludes_commander(self, conn):
        """Commander never surfaces in its own recommendations."""
        _add_port(conn, "Marchesa, the Black Rose", port_type="keyword", event_class="Persist")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), {"Marchesa, the Black Rose"})
        assert "Marchesa, the Black Rose" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(conn, "Iron Apprentice", port_type="keyword", event_class="etbCounter:P1P1:1")
        results = _find_counter_axis_feeders(conn, self._marchesa_ports(), set())
        assert all(r.rule_id == "counter_axis_feeder" for r in results)


class TestFindModifiedAxisFeeders:
    """General rule: for any commander port whose valid_filter or
    raw_line contains the standalone ``modified`` qualifier on a
    non-Self axis, surface candidates that contribute the three
    flavors of modification — +1/+1 counters (producer / self-grower /
    doubler), Proliferate, and etb-counter / Modular keywords.

    Self-anchored conditions (``Card.Self+modified``) and
    description / TargetsValid clauses are rejected so commanders
    whose modified appears only as a self-condition or a flavor-text
    mention aren't promoted to modified-axis archetypes."""

    def _kodama_ports(self):
        return [
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line=("{'Mode': 'Continuous', 'Affected': 'Creature.modified+YouCtrl', 'AddKeyword': 'Trample'}"),
            )
        ]

    def test_no_modified_qualifier_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="DamageDone", valid_filter="Creature.YouCtrl")]
        assert _find_modified_axis_feeders(conn, ports, set()) == []

    def test_unmodified_substring_does_not_trigger(self, conn):
        """``unmodified`` is a different qualifier — word boundary
        check rejects it."""
        ports = [
            _port_row(
                port_type="trigger",
                event_class="Attacks",
                valid_filter="Creature.unmodified+YouCtrl",
            )
        ]
        assert _find_modified_axis_feeders(conn, ports, set()) == []

    def test_self_modified_skips(self, conn):
        """Ian the Reckless's ``IsPresent: Card.Self+modified`` is a
        self-condition (only Ian must be modified), not a payoff axis."""
        ports = [
            _port_row(
                port_type="trigger",
                event_class="Attacks",
                valid_filter="Card.Self",
                raw_line=(
                    "{'Mode': 'Attacks', 'ValidCard': 'Card.Self', "
                    "'IsPresent': 'Card.Self+modified', 'Execute': 'TrigDamage'}"
                ),
            )
        ]
        assert _find_modified_axis_feeders(conn, ports, set()) == []

    def test_targets_valid_skips(self, conn):
        """Pearl-Ear's ``TargetsValid: Permanent.modified+YouCtrl`` is
        a target qualifier on an Aura-tribal trigger. The commander is
        fundamentally Aura tribal — modified is incidental."""
        ports = [
            _port_row(
                port_type="trigger",
                event_class="SpellCast",
                valid_filter="Aura",
                raw_line=(
                    "{'Mode': 'SpellCast', 'ValidCard': 'Aura', "
                    "'TargetsValid': 'Permanent.modified+YouCtrl', "
                    "'Execute': 'TrigDraw'}"
                ),
            )
        ]
        assert _find_modified_axis_feeders(conn, ports, set()) == []

    def test_description_mention_skips(self, conn):
        """``modified`` appearing only in TriggerDescription / Description
        flavor text shouldn't activate the rule."""
        ports = [
            _port_row(
                port_type="trigger",
                event_class="Attacks",
                valid_filter="Card.Self",
                raw_line=(
                    "{'Mode': 'Attacks', 'ValidCard': 'Card.Self', "
                    "'TriggerDescription': "
                    "'Whenever this attacks, target modified creature gets +1/+1.'}"
                ),
            )
        ]
        assert _find_modified_axis_feeders(conn, ports, set()) == []

    def test_kodama_static_activates(self, conn):
        _add_port(
            conn,
            "Rishkar, Peema Renegade",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature",
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        names = _candidates(results)
        assert "Rishkar, Peema Renegade" in names

    def test_p1p1_doubler_tier(self, conn):
        """Hardened Scales / Doubling Season — replacement AddCounter
        with ValidCounterType P1P1 → AddOneMoreCounters."""
        _add_port(
            conn,
            "Hardened Scales",
            port_type="replacement",
            event_class="AddCounter",
            valid_filter="Creature.YouCtrl+inZoneBattlefield",
            raw_line=(
                "{'Event': 'AddCounter', 'ValidCard': 'Creature.YouCtrl+inZoneBattlefield', "
                "'ValidCounterType': 'P1P1', 'ReplaceWith': 'AddOneMoreCounters'}"
            ),
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Hardened Scales") == "modified_p1p1_doubler"

    def test_self_grower_tier(self, conn):
        """Self-counter creatures (Forgotten Ancient, Champion of
        Lambholt, Managorger Hydra) are modified on board because they
        carry their own +1/+1 counters."""
        conn.execute("INSERT INTO cards (name, types) VALUES (?, ?)", ("Champion of Lambholt", "Creature"))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, counter_type, valid_filter) "
            "VALUES (?, 'effect', 'PutCounter', 'P1P1', 'Self')",
            ("Champion of Lambholt",),
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Champion of Lambholt") == "modified_self_grower"

    def test_self_grower_skips_non_creature(self, conn):
        """Self-counter on a non-creature artifact isn't on the
        modified axis (modified targets creatures specifically)."""
        conn.execute("INSERT INTO cards (name, types) VALUES (?, ?)", ("Artifact Self-Counter", "Artifact"))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, counter_type, valid_filter) "
            "VALUES (?, 'effect', 'PutCounter', 'P1P1', 'Self')",
            ("Artifact Self-Counter",),
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        assert "Artifact Self-Counter" not in _candidates(results)

    def test_proliferate_tier(self, conn):
        _add_port(conn, "Evolution Sage", port_type="effect", event_class="Proliferate")
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Evolution Sage") == "modified_proliferate"

    def test_etb_keyword_tier(self, conn):
        _add_port(conn, "Iron Apprentice", port_type="keyword", event_class="etbCounter:P1P1:1")
        _add_port(conn, "Arcbound Ravager", port_type="keyword", event_class="Modular")
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Iron Apprentice") == "modified_etb_keyword"
        assert events.get("Arcbound Ravager") == "modified_etb_keyword"

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Kodama of the West Tree",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature",
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), {"Kodama of the West Tree"})
        assert "Kodama of the West Tree" not in _candidates(results)

    def test_one_complement_per_card_doubler_wins(self, conn):
        """A card matching both doubler and producer tiers gets the
        higher-priority doubler tier (only doubling matters for
        modified-axis ranking)."""
        conn.execute("INSERT INTO cards (name, types) VALUES (?, ?)", ("Hybrid Card", "Creature"))
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, valid_filter, raw_line) "
            "VALUES (?, 'replacement', 'AddCounter', 'Creature.YouCtrl+inZoneBattlefield', ?)",
            (
                "Hybrid Card",
                "{'Event': 'AddCounter', 'ValidCounterType': 'P1P1', 'ReplaceWith': 'AddOneMoreCounters'}",
            ),
        )
        conn.execute(
            "INSERT INTO card_ports (card_name, port_type, event_class, counter_type, valid_filter) "
            "VALUES (?, 'effect', 'PutCounter', 'P1P1', 'Creature')",
            ("Hybrid Card",),
        )
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        hybrid = [r for r in results if r.candidate == "Hybrid Card"]
        assert len(hybrid) == 1
        assert hybrid[0].cand_event == "modified_p1p1_doubler"

    def test_rule_id(self, conn):
        _add_port(conn, "Iron Apprentice", port_type="keyword", event_class="etbCounter:P1P1:1")
        results = _find_modified_axis_feeders(conn, self._kodama_ports(), set())
        assert all(r.rule_id == "modified_axis_feeder" for r in results)


class TestFindCardPowerAxisFeeders:
    """General rule for commanders with ``SVar:X:Count$CardPower`` —
    their abilities scale with their own power, so they want to be
    pumped (big-pump Equipment/Aura + P1P1 counter producers).

    Rejects other scales_with axes (TotalPower, greatestPower) so the
    rule stays mechanically distinct from the deleted ``power_matters``
    rule that fed unrelated high-power creatures to this archetype."""

    def _combustion_ports(self):
        return [
            _port_row(
                port_type="scales_with",
                event_class="CardPower",
                valid_filter="",
                scaling_expression="Count$CardPower",
                raw_line="SVar:X:Count$CardPower",
            )
        ]

    def test_no_cardpower_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="DamageDone", valid_filter="Card.Self")]
        assert _find_cardpower_axis_feeders(conn, ports, set()) == []

    def test_totalpower_does_not_activate(self, conn):
        """``TotalPower`` / ``greatestPower`` are different axes (they
        scan the board), not the self-power axis. Must not trigger."""
        ports = [
            _port_row(
                port_type="scales_with",
                event_class="TotalPower",
                raw_line="SVar:X:Count$TotalPower",
            )
        ]
        _add_port(
            conn,
            "Colossus Hammer",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': '10'}",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Colossus Hammer'")
        assert _find_cardpower_axis_feeders(conn, ports, set()) == []

    def test_big_attachment_tier_integer(self, conn):
        """Equipment with ``AddPower: '10'`` ≥ 3 qualifies."""
        _add_port(
            conn,
            "Colossus Hammer",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': '10'}",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Colossus Hammer'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Colossus Hammer") == "cardpower_big_attachment"

    def test_big_attachment_tier_scaling_svar(self, conn):
        """Equipment with ``AddPower: 'X'`` (scaling SVar) qualifies
        regardless of the integer comparison."""
        _add_port(
            conn,
            "Grafted Wargear",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': 'X'}",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Grafted Wargear'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Grafted Wargear") == "cardpower_big_attachment"

    def test_small_attachment_rejected(self, conn):
        """Equipment with ``AddPower: '1'`` < 3 does NOT qualify — +1/+0
        trinkets aren't meaningful power-scaling fuel. The p1p1_producer
        tier still won't pull it in because it has no PutCounter port."""
        _add_port(
            conn,
            "Short Sword",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': '1'}",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Short Sword'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        assert "Short Sword" not in _candidates(results)

    def test_big_aura_tier(self, conn):
        """Auras count too — Eldrazi Conscription-style +10/+10."""
        _add_port(
            conn,
            "Eldrazi Conscription",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EnchantedBy', 'AddPower': '10'}",
        )
        conn.execute("UPDATE cards SET types='Enchantment Aura' WHERE name='Eldrazi Conscription'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Eldrazi Conscription") == "cardpower_big_attachment"

    def test_non_attachment_with_add_power_rejected(self, conn):
        """A non-Equipment / non-Aura (e.g. a creature with an
        AddPower-granting static) isn't in the attachment tier —
        the gate specifically targets cards that STICK to the
        commander, so creatures don't qualify here."""
        _add_port(
            conn,
            "Battered Creature",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.YouCtrl', 'AddPower': '3'}",
        )
        conn.execute("UPDATE cards SET types='Creature' WHERE name='Battered Creature'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        assert "Battered Creature" not in _candidates(results)

    def test_p1p1_producer_tier(self, conn):
        """Hardened Scales-style producer on Creature target qualifies
        the p1p1_producer tier (even if no attachment pool present)."""
        _add_port(
            conn,
            "Rishkar, Peema Renegade",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl",
        )
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Rishkar, Peema Renegade") == "cardpower_p1p1_producer"

    def test_self_only_p1p1_rejected(self, conn):
        """Self-only PutCounter (Champion of Lambholt grows itself only)
        doesn't distribute counters to the commander — it should NOT
        land in cardpower_p1p1_producer."""
        _add_port(
            conn,
            "Champion of Lambholt",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Self",
        )
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        assert "Champion of Lambholt" not in _candidates(results)

    def test_self_sac_only_p1p1_rejected(self, conn):
        """A card whose ONLY sacrifice-cost targets itself is a
        one-shot payoff, not a sustained distributor — rejected via
        ``_only_self_sac_cost``."""
        _add_port(
            conn,
            "One-Shot Counter",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl",
        )
        _add_port(
            conn,
            "One-Shot Counter",
            port_type="cost",
            event_class="sacrifice",
            cost_target="self",
        )
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        assert "One-Shot Counter" not in _candidates(results)

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Combustion Man",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl",
        )
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), {"Combustion Man"})
        assert "Combustion Man" not in _candidates(results)

    def test_attachment_wins_over_producer(self, conn):
        """A single card carrying both an AddPower attachment port AND
        a P1P1 PutCounter effect gets ONE complement — the attachment
        tier (higher priority) wins."""
        _add_port(
            conn,
            "Hybrid Blade",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': '5'}",
        )
        _add_port(
            conn,
            "Hybrid Blade",
            port_type="effect",
            event_class="PutCounter",
            counter_type="P1P1",
            valid_filter="Creature.YouCtrl",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Hybrid Blade'")
        hybrids = [
            r
            for r in _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
            if r.candidate == "Hybrid Blade"
        ]
        assert len(hybrids) == 1
        assert hybrids[0].cand_event == "cardpower_big_attachment"

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Colossus Hammer",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Creature.EquippedBy', 'AddPower': '10'}",
        )
        conn.execute("UPDATE cards SET types='Artifact Equipment' WHERE name='Colossus Hammer'")
        results = _find_cardpower_axis_feeders(conn, self._combustion_ports(), set())
        assert results
        assert all(r.rule_id == "cardpower_axis_feeder" for r in results)


class TestClassifyTapTypeAxis:
    """Axis resolver extracts the SUBJECT from a ``tapXType<N/SUBJECT>``
    cost and classifies it as ``creature`` / ``artifact`` / ``permanent``.

    Mixed-subject costs (Caparocti: ``Artifact;Creature``) yield both
    classes. Permanent subsumes both — it's surfaced explicitly so
    ``Permanent``-tappers (Baylen / Hazel) can match every untap card.
    """

    def _cost(self, raw_line: str) -> dict:
        return _port_row(port_type="cost", event_class="tap_type", raw_line=raw_line)

    def test_no_cost_returns_empty(self):
        assert _classify_tap_type_axis([_port_row(port_type="trigger", event_class="Attacks")]) == frozenset()

    def test_creature_subtype_resolves_to_creature(self):
        ports = [self._cost("tapXType<1/Wizard>")]
        assert _classify_tap_type_axis(ports) == frozenset({"creature"})

    def test_generic_creature_resolves_to_creature(self):
        ports = [self._cost("G T tapXType<2/Creature>")]
        assert _classify_tap_type_axis(ports) == frozenset({"creature"})

    def test_artifact_resolves_to_artifact(self):
        ports = [self._cost("T tapXType<X/Artifact/artifacts you control>")]
        assert _classify_tap_type_axis(ports) == frozenset({"artifact"})

    def test_food_resolves_to_artifact(self):
        """``Food`` is an Artifact subtype and goes on the artifact axis."""
        ports = [self._cost("W T tapXType<X/Food>")]
        assert _classify_tap_type_axis(ports) == frozenset({"artifact"})

    def test_permanent_resolves_to_permanent(self):
        ports = [self._cost("tapXType<2/Permanent.token/token>")]
        assert _classify_tap_type_axis(ports) == frozenset({"permanent"})

    def test_subtype_qualifier_ignored(self):
        """``Halfling.Other`` / ``Artifact.!token`` qualifiers don't
        change the axis class — the head token before the first ``.``
        is what matters."""
        ports = [
            self._cost("tapXType<2/Halfling.Other/other halflings>"),
            self._cost("tapXType<1/Artifact.!token/nontoken artifact>"),
        ]
        assert _classify_tap_type_axis(ports) == frozenset({"creature", "artifact"})

    def test_mixed_subjects_yield_both(self):
        """Caparocti: ``Artifact;Creature`` — both classes."""
        ports = [self._cost("tapXType<2/Artifact;Creature/artifacts and/or creatures>")]
        assert _classify_tap_type_axis(ports) == frozenset({"creature", "artifact"})

    def test_multi_creature_subtypes_collapse(self):
        """The Archimandrite: ``Advisor.YouCtrl;Monk.YouCtrl;Artificer.YouCtrl``
        — all three are creature subtypes so just ``creature``."""
        ports = [
            self._cost(
                "tapXType<3/Advisor.YouCtrl;Monk.YouCtrl;Artificer.YouCtrl/"
                "Advisors, Artificers, and/or Monks you control>"
            )
        ]
        assert _classify_tap_type_axis(ports) == frozenset({"creature"})

    def test_malformed_raw_line_silently_skipped(self):
        """A malformed ``tapXType<N>`` with no ``/`` separator between
        count and subject fails the regex and is silently skipped.
        Pins the safe-fallback contract — if a future schema change
        accidentally loosens the regex, the port with no subject
        would otherwise leak into the creature branch by default."""
        ports = [self._cost("tapXType<1>")]
        assert _classify_tap_type_axis(ports) == frozenset()


class TestFindTapTypeFeeders:
    """General rule for ``cost.tap_type`` commanders (Azami, Urza,
    Aryel, Kumena, Apothecary White, Baylen). Matches untap engines
    that refresh the cost-target each rotation.

    Axis-aware: a creature-tap commander (Azami) doesn't get
    artifact-specific untappers (Unwinding Clock), and vice versa.
    Permanent-tappers (Baylen) match everything."""

    def _creature_cost(self):
        """Azami: tap Wizards (creature axis)."""
        return _port_row(port_type="cost", event_class="tap_type", raw_line="tapXType<1/Wizard>")

    def _artifact_cost(self):
        """Urza: tap Artifacts (artifact axis)."""
        return _port_row(port_type="cost", event_class="tap_type", raw_line="tapXType<1/Artifact>")

    def _permanent_cost(self):
        """Baylen: tap Permanent.token (permanent axis)."""
        return _port_row(port_type="cost", event_class="tap_type", raw_line="tapXType<2/Permanent.token/token>")

    def test_no_tap_type_cost_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_tap_type_feeders(conn, ports, set()) == []

    def test_sustained_untap_tier_creature_axis(self, conn):
        """Seedborn Muse (UntapOtherPlayer Permanent.YouCtrl) fires for
        a Wizard-tap commander — Permanent subsumes Creature."""
        _add_port(
            conn,
            "Seedborn Muse",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Permanent.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Seedborn Muse") == "tap_type_sustained_untap"

    def test_sustained_untap_tier_artifact_axis(self, conn):
        """Unwinding Clock (UntapOtherPlayer Artifact.YouCtrl) fires
        for an Urza (artifact-tap) commander."""
        _add_port(
            conn,
            "Unwinding Clock",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Artifact.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._artifact_cost()], set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Unwinding Clock") == "tap_type_sustained_untap"

    def test_artifact_untap_rejected_on_creature_axis(self, conn):
        """Unwinding Clock (Artifact.YouCtrl) MUST NOT fire for a
        Wizard-tap commander — the subject mismatch was the regression
        observed during the first audit pass (Aryel -0.167 NDCG)."""
        _add_port(
            conn,
            "Unwinding Clock",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Artifact.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], set())
        assert "Unwinding Clock" not in _candidates(results)

    def test_creature_untap_rejected_on_artifact_axis(self, conn):
        """Drumbellower (Creature.YouCtrl) MUST NOT fire for an
        Artifact-tap commander."""
        _add_port(
            conn,
            "Drumbellower",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Creature.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._artifact_cost()], set())
        assert "Drumbellower" not in _candidates(results)

    def test_permanent_axis_matches_all(self, conn):
        """Baylen (Permanent.token cost) matches every axis — no
        rejection based on subject class."""
        _add_port(
            conn,
            "Drumbellower",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Creature.YouCtrl'}",
        )
        _add_port(
            conn,
            "Unwinding Clock",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Artifact.YouCtrl'}",
        )
        names = _candidates(_find_tap_type_feeders(conn, [self._permanent_cost()], set()))
        assert {"Drumbellower", "Unwinding Clock"} <= names

    def test_self_only_untap_rejected(self, conn):
        """Bender's Waterskin (ValidCard: 'Card.Self') untaps only
        itself — no help for an external tap-cost commander."""
        _add_port(
            conn,
            "Bender's Waterskin",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Card.Self'}",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], set())
        assert "Bender's Waterskin" not in _candidates(results)

    def test_phase_untap_tier_creature_axis(self, conn):
        """Awakening: trigger.Phase (Upkeep) + effect.UntapAll
        (Creature,Land) — phase_untap tier for creature-axis."""
        _add_port(
            conn,
            "Awakening",
            port_type="trigger",
            event_class="Phase",
            phase="Upkeep",
            raw_line="{'Mode': 'Phase', 'Phase': 'Upkeep', 'Execute': 'TrigUntapAll'}",
        )
        _add_port(
            conn,
            "Awakening",
            port_type="effect",
            event_class="UntapAll",
            valid_filter="Creature,Land",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Awakening") == "tap_type_phase_untap"

    def test_sustained_wins_over_phase_on_dedup(self, conn):
        """A card matching BOTH tiers (hypothetical) gets one
        complement — the sustained (tier 1) wins over phase (tier 2)."""
        _add_port(
            conn,
            "Hybrid Untapper",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Creature.YouCtrl'}",
        )
        _add_port(
            conn,
            "Hybrid Untapper",
            port_type="trigger",
            event_class="Phase",
            phase="Upkeep",
            raw_line="{'Mode': 'Phase', 'Phase': 'Upkeep'}",
        )
        _add_port(
            conn,
            "Hybrid Untapper",
            port_type="effect",
            event_class="UntapAll",
            valid_filter="Creature.YouCtrl",
        )
        hybrids = [
            r for r in _find_tap_type_feeders(conn, [self._creature_cost()], set()) if r.candidate == "Hybrid Untapper"
        ]
        assert len(hybrids) == 1
        assert hybrids[0].cand_event == "tap_type_sustained_untap"

    def test_excludes_commander(self, conn):
        """Commander's own ports must not surface as candidates."""
        _add_port(
            conn,
            "Azami, Lady of Scrolls",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Creature.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], {"Azami, Lady of Scrolls"})
        assert "Azami, Lady of Scrolls" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Seedborn Muse",
            port_type="static",
            event_class="UntapOtherPlayer",
            raw_line="{'Mode': 'UntapOtherPlayer', 'ValidCard': 'Permanent.YouCtrl'}",
        )
        results = _find_tap_type_feeders(conn, [self._creature_cost()], set())
        assert results
        assert all(r.rule_id == "tap_type_feeder" for r in results)


class TestIsBigHandCommander:
    """Classify hand-size commanders as big-hand (reward more cards in
    hand) or small-hand (reward empty hand). Hand-size axis is
    bidirectional so feeding Reliquary-Tower staples to Hazoret /
    Neheb / Djeru-and-Hazoret / Flubs would be anti-synergy.

    Small-hand signals on the hand-binding SVar: ``LE0``/``LE1``/
    ``EQ0`` (mechanic fires when hand is tiny) or ``GE2``/``GE3``
    (Hazoret pattern — blocked when hand reaches 2+)."""

    def _hand_svar(self, name: str = "X") -> dict:
        return _port_row(
            port_type="scales_with",
            event_class="ValidHand Card.YouOwn",
            raw_line=f"SVar:{name}:Count$ValidHand Card.YouOwn",
        )

    def test_no_hand_svar_returns_false(self):
        """Non-hand-size commanders are neither big nor small — the
        gate returns False so the rule doesn't fire."""
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _is_big_hand_commander(ports) is False

    def test_alandra_default_big_hand(self):
        """Alandra has a hand-SVar but no small-hand compare — classified
        as big-hand by default."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="effect",
                event_class="PumpAll",
                raw_line=("{'DB': 'PumpAll', 'ValidCards': 'Card.Self,Drake.YouCtrl', 'NumAtt': '+X', 'NumDef': '+X'}"),
            ),
        ]
        assert _is_big_hand_commander(ports) is True

    def test_hazoret_ge2_rejected(self):
        """Hazoret: ``CheckSVar: X`` + ``SVarCompare: GE2`` on
        CantAttack = blocked when hand ≥ 2, wants ≤ 1."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="static",
                event_class="CantAttack,CantBlock",
                raw_line=(
                    "{'Mode': 'CantAttack,CantBlock', 'ValidCard': 'Card.Self', 'CheckSVar': 'X', 'SVarCompare': 'GE2'}"
                ),
            ),
        ]
        assert _is_big_hand_commander(ports) is False

    def test_neheb_le1_rejected(self):
        """Neheb: ``CheckSVar: X`` + ``SVarCompare: LE1`` on +2/+0
        static = bonus when hand ≤ 1, wants small hand."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Minotaur.YouCtrl', "
                    "'AddPower': '2', 'CheckSVar': 'X', 'SVarCompare': 'LE1'}"
                ),
            ),
        ]
        assert _is_big_hand_commander(ports) is False

    def test_flubs_eq0_rejected_via_branch_compare(self):
        """Flubs: ``BranchConditionSVar: X`` + ``BranchConditionSVarCompare:
        EQ0`` = draw when hand empty, discard otherwise. Ambiguous
        archetype but tilts small-hand."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="effect",
                event_class="Branch",
                raw_line=(
                    "{'DB': 'Branch', 'BranchConditionSVar': 'X', "
                    "'BranchConditionSVarCompare': 'EQ0', "
                    "'TrueSubAbility': 'DBDraw', 'FalseSubAbility': 'DBDiscard'}"
                ),
            ),
        ]
        assert _is_big_hand_commander(ports) is False

    def test_damia_lt7_is_big_hand(self):
        """Damia: ``SVarCompare: LT7`` on Upkeep trigger = refill to 7.
        LT7 is NOT in the small-hand set — big-hand."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="trigger",
                event_class="Phase",
                raw_line=(
                    "{'Mode': 'Phase', 'Phase': 'Upkeep', "
                    "'CheckSVar': 'X', 'SVarCompare': 'LT7', 'Execute': 'TrigDraw'}"
                ),
            ),
        ]
        assert _is_big_hand_commander(ports) is True

    def test_jin_gitaxias_ge7_is_big_hand(self):
        """Jin-Gitaxias: ``SVarCompare: GE7`` for transform = wants ≥ 7."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="effect",
                event_class="ChangeZone",
                raw_line=("{'AB': 'ChangeZone', 'CheckSVar': 'X', 'SVarCompare': 'GE7', 'Origin': 'Battlefield'}"),
            ),
        ]
        assert _is_big_hand_commander(ports) is True

    def test_compare_on_non_hand_svar_ignored(self):
        """A small-hand-looking compare on a DIFFERENT SVar (e.g., a
        counter count) must not reject the commander. Only compares
        bound to a hand-size SVar matter."""
        ports = [
            self._hand_svar("X"),
            _port_row(
                port_type="trigger",
                event_class="Phase",
                raw_line=("{'Mode': 'Phase', 'CheckSVar': 'Z', 'SVarCompare': 'LE1', 'Execute': 'TrigSomething'}"),
            ),
        ]
        assert _is_big_hand_commander(ports) is True

    def test_condition_check_svar_variant_rejected(self):
        """``ConditionCheckSVar`` is the third variant captured by
        ``_CHECK_SVAR_RE`` (alongside ``CheckSVar`` for Hazoret and
        ``BranchConditionSVar`` for Flubs). A small-hand compare
        paired with this variant must also trigger the rejection —
        otherwise any future commander emitting ``ConditionCheckSVar``
        would slip past the gate as a false-positive big-hand."""
        ports = [
            self._hand_svar(),
            _port_row(
                port_type="effect",
                event_class="ChangeZone",
                raw_line=("{'DB': 'ChangeZone', 'ConditionCheckSVar': 'X', 'SVarCompare': 'LE1'}"),
            ),
        ]
        assert _is_big_hand_commander(ports) is False


class TestFindHandSizeFeeders:
    """Rule for big-hand commanders: surface SetMaxHandSize:
    Unlimited statics (Reliquary Tower, Thought Vessel, Library of
    Leng). Skip small-hand commanders entirely."""

    def _big_hand_cmdr_ports(self):
        """Damia-style: hand SVar + no small-hand compare."""
        return [
            _port_row(
                port_type="scales_with",
                event_class="ValidHand Card.YouOwn",
                raw_line="SVar:X:Count$ValidHand Card.YouOwn",
            ),
            _port_row(
                port_type="effect",
                event_class="Draw",
                raw_line="{'DB': 'Draw', 'NumCards': 'Difference'}",
            ),
        ]

    def _hazoret_ports(self):
        """Small-hand: CheckSVar X + SVarCompare GE2."""
        return [
            _port_row(
                port_type="scales_with",
                event_class="ValidHand Card.YouOwn",
                raw_line="SVar:X:Count$ValidHand Card.YouOwn",
            ),
            _port_row(
                port_type="static",
                event_class="CantAttack,CantBlock",
                raw_line=(
                    "{'Mode': 'CantAttack,CantBlock', 'ValidCard': 'Card.Self', 'CheckSVar': 'X', 'SVarCompare': 'GE2'}"
                ),
            ),
        ]

    def test_no_hand_svar_skips(self, conn):
        """Non-hand-size commanders get no complements."""
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_hand_size_feeders(conn, ports, set()) == []

    def test_big_hand_surfaces_unlimited(self, conn):
        """Reliquary Tower qualifies — SetMaxHandSize: Unlimited."""
        _add_port(
            conn,
            "Reliquary Tower",
            port_type="static",
            event_class="Continuous",
            raw_line=("{'Mode': 'Continuous', 'Affected': 'You', 'SetMaxHandSize': 'Unlimited'}"),
        )
        results = _find_hand_size_feeders(conn, self._big_hand_cmdr_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Reliquary Tower") == "hand_size_no_max"

    def test_small_hand_cmdr_skipped(self, conn):
        """Hazoret-style small-hand commander gets NO complements even
        with a full SetMaxHandSize pool — feeding her those cards
        would be anti-synergy."""
        _add_port(
            conn,
            "Reliquary Tower",
            port_type="static",
            event_class="Continuous",
            raw_line=("{'Mode': 'Continuous', 'Affected': 'You', 'SetMaxHandSize': 'Unlimited'}"),
        )
        assert _find_hand_size_feeders(conn, self._hazoret_ports(), set()) == []

    def test_non_unlimited_max_hand_size_rejected(self, conn):
        """A static that sets a FIXED max hand size (e.g. Doctor Octopus
        ``SetMaxHandSize: '8'``) doesn't match — only ``Unlimited``
        qualifies for the tier."""
        _add_port(
            conn,
            "Doctor Octopus, Master Planner",
            port_type="static",
            event_class="Continuous",
            raw_line=("{'Mode': 'Continuous', 'Affected': 'You', 'SetMaxHandSize': '8'}"),
        )
        results = _find_hand_size_feeders(conn, self._big_hand_cmdr_ports(), set())
        assert "Doctor Octopus, Master Planner" not in _candidates(results)

    def test_non_static_max_hand_size_rejected(self, conn):
        """A SetMaxHandSize mention outside a static.Continuous port
        (e.g. in a one-shot Effect) doesn't qualify — the tier targets
        persistent statics only."""
        _add_port(
            conn,
            "Temporary Effect",
            port_type="effect",
            event_class="Effect",
            raw_line=(
                "{'DB': 'Effect', 'StaticAbilities': 'STHandSize', "
                "'Duration': 'AsLongAsControl', 'SetMaxHandSize': 'Unlimited'}"
            ),
        )
        results = _find_hand_size_feeders(conn, self._big_hand_cmdr_ports(), set())
        assert "Temporary Effect" not in _candidates(results)

    def test_excludes_commander(self, conn):
        """Jin-Gitaxias has the static himself — must not self-match."""
        _add_port(
            conn,
            "Jin-Gitaxias",
            port_type="static",
            event_class="Continuous",
            raw_line=("{'Mode': 'Continuous', 'Affected': 'You', 'SetMaxHandSize': 'Unlimited'}"),
        )
        results = _find_hand_size_feeders(conn, self._big_hand_cmdr_ports(), {"Jin-Gitaxias"})
        assert "Jin-Gitaxias" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Thought Vessel",
            port_type="static",
            event_class="Continuous",
            raw_line=("{'Mode': 'Continuous', 'Affected': 'You', 'SetMaxHandSize': 'Unlimited'}"),
        )
        results = _find_hand_size_feeders(conn, self._big_hand_cmdr_ports(), set())
        assert results
        assert all(r.rule_id == "hand_size_feeder" for r in results)


class TestFindGyFuelFeeders:
    """Rule for commanders with ``cost.exile_from_grave`` +
    ``cost_target='any'`` — Aphemia / Ashnod / Araumi / Drivnod /
    Egon / Gorex / Ishkanah / Kethis / Osgir / Ultimecia / Varina /
    Winter. They pay by exiling graveyard cards, so the archetype
    reward is self-mill (fills graveyard, lets the cost fire more).

    Self-target escape-style commanders (Wilson, Symbiote Spider-Man,
    Venom, Tocasia, Morbius, Spider-Slayer, Beetle) are a different
    archetype and rejected by the gate."""

    def _any_target_cost(self):
        """Araumi-style: tap + exile X cards from any graveyard."""
        return _port_row(
            port_type="cost",
            event_class="exile_from_grave",
            cost_target="any",
            raw_line="T ExileFromGrave<X/Card>",
        )

    def _self_target_cost(self):
        """Wilson-style: exile CARDNAME from your own graveyard."""
        return _port_row(
            port_type="cost",
            event_class="exile_from_grave",
            cost_target="self",
            raw_line="1 G W ExileFromGrave<1/CARDNAME>",
        )

    def test_no_exile_cost_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_gy_fuel_feeders(conn, ports, set()) == []

    def test_self_target_cost_skipped(self, conn):
        """Self-escape commanders want different support (die-triggers,
        sac outlets). The rule explicitly gates on cost_target='any'."""
        _add_port(
            conn,
            "Hedron Crab",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'Defined': 'You', 'NumCards': '3'}",
        )
        assert _find_gy_fuel_feeders(conn, [self._self_target_cost()], set()) == []

    def test_self_mill_tier_integer(self, conn):
        """Hedron Crab: NumCards: 3, Defined: You — qualifies."""
        _add_port(
            conn,
            "Hedron Crab",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'Defined': 'You', 'NumCards': '3'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Hedron Crab") == "gy_fuel_self_mill"

    def test_self_mill_scaling_svar_qualifies(self, conn):
        """Altar of Dementia: NumCards: X (scaled by sacrificed creature
        power) — X/Y/Z all qualify regardless of the integer check."""
        _add_port(
            conn,
            "Altar of Dementia",
            port_type="effect",
            event_class="Mill",
            raw_line="{'AB': 'Mill', 'Cost': 'Sac<1/Creature>', 'NumCards': 'X', 'Defined': 'You'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Altar of Dementia") == "gy_fuel_self_mill"

    def test_num_cards_2_rejected(self, conn):
        """NumCards=2 cantrip-mill rejected (tightened from 2 to 3
        after audit — Osgir -0.093 / Ultimecia -0.441 regressed when
        NumCards=2 trinkets flooded their top-30)."""
        _add_port(
            conn,
            "Small Mill Cantrip",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'Defined': 'You', 'NumCards': '2'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        assert "Small Mill Cantrip" not in _candidates(results)

    def test_opponent_mill_rejected(self, conn):
        """Mills that target Opponent / Player.Opp don't fill YOUR
        graveyard, so they're rejected."""
        _add_port(
            conn,
            "Traumatize",
            port_type="effect",
            event_class="Mill",
            raw_line=("{'SP': 'Mill', 'Defined': 'You', 'NumCards': '15', 'ValidTgts': 'Opponent'}"),
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        assert "Traumatize" not in _candidates(results)

    def test_each_player_mill_rejected(self, conn):
        """Mills targeting all players (EachPlayer) aren't solo
        self-mill — they also benefit opponents, so rejected."""
        _add_port(
            conn,
            "Tasha's Hideous Laughter",
            port_type="effect",
            event_class="Mill",
            raw_line=("{'SP': 'Mill', 'Defined': 'You', 'NumCards': '20', 'ValidPlayer': 'EachPlayer'}"),
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        assert "Tasha's Hideous Laughter" not in _candidates(results)

    def test_mill_without_defined_you_rejected(self, conn):
        """Mill effects that don't explicitly target You are rejected
        to avoid generic mill spells aimed at random players."""
        _add_port(
            conn,
            "Generic Mill",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'NumCards': '5'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        assert "Generic Mill" not in _candidates(results)

    def test_excludes_commander(self, conn):
        """Araumi has no self-mill effect but even if she did we'd
        skip the commander herself."""
        _add_port(
            conn,
            "Araumi of the Dead Tide",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'Defined': 'You', 'NumCards': '3'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], {"Araumi of the Dead Tide"})
        assert "Araumi of the Dead Tide" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Hedron Crab",
            port_type="effect",
            event_class="Mill",
            raw_line="{'DB': 'Mill', 'Defined': 'You', 'NumCards': '3'}",
        )
        results = _find_gy_fuel_feeders(conn, [self._any_target_cost()], set())
        assert results
        assert all(r.rule_id == "gy_fuel_feeder" for r in results)


class TestFindLifegainFeeders:
    """Rule for commanders with a ``scales_with LifeYouGainedThisTurn``
    port (Aerith, Astarion, Celestine, Frodo, Lathiel, Licia, Saint
    Elenda, Willowdusk). The lifegain axis is monotonic-positive
    (every commander wants MORE lifegain) so no bidirectional gate
    is needed.

    Two tiers: ``lifegain_amp`` (replacement.GainLife doublers) >
    ``lifegain_etb_trigger`` (creature-ETB → GainLife soul sisters)."""

    def _lifegain_cmdr_ports(self):
        """Celestine-style port signature."""
        return [
            _port_row(
                port_type="scales_with",
                event_class="LifeYouGainedThisTurn",
                raw_line="SVar:X:Count$LifeYouGainedThisTurn",
            )
        ]

    def test_no_lifegain_axis_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_lifegain_feeders(conn, ports, set()) == []

    def test_amp_tier_gain_double(self, conn):
        """Rhox Faithmender: ReplaceWith: 'GainDouble' = pure doubler,
        archetype-defining."""
        _add_port(
            conn,
            "Rhox Faithmender",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainDouble'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Rhox Faithmender") == "lifegain_amp"

    def test_amp_tier_replace_gain_life_qualifies(self, conn):
        """Angel of Vitality: ReplaceWith: 'GainLife' (gain an extra
        N) — also qualifies for the amp tier."""
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainLife'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Angel of Vitality") == "lifegain_amp"

    def test_opponent_targeting_amp_rejected(self, conn):
        """Tainted Remedy / Plague Drone: ValidPlayer: 'Opponent'
        converts opponents' gain to loss. Anti-synergy for a
        lifegain commander — rejected."""
        _add_port(
            conn,
            "Tainted Remedy",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'Opponent', 'ReplaceWith': 'RLoseLife'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        assert "Tainted Remedy" not in _candidates(results)

    def test_prevention_static_rejected(self, conn):
        """Sulfuric Vortex: ``Prevent: True`` prevents all lifegain.
        Strict anti-synergy — must be rejected even though it
        replaces GainLife for ValidPlayer: 'You' (implicitly)."""
        _add_port(
            conn,
            "Sulfuric Vortex",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'Prevent': 'True', 'ReplaceWith': 'GainDouble'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        assert "Sulfuric Vortex" not in _candidates(results)

    def test_etb_trigger_tier(self, conn):
        """Soul Warden: trigger.ChangesZone with Creature filter +
        Destination Battlefield + effect.GainLife."""
        _add_port(
            conn,
            "Soul Warden",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.Other",
            raw_line=(
                "{'Mode': 'ChangesZone', 'Destination': 'Battlefield', "
                "'ValidCard': 'Creature.Other', 'Execute': 'TrigGainLife'}"
            ),
        )
        _add_port(
            conn,
            "Soul Warden",
            port_type="effect",
            event_class="GainLife",
            raw_line="{'DB': 'GainLife', 'LifeAmount': '1'}",
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Soul Warden") == "lifegain_etb_trigger"

    def test_non_creature_etb_trigger_rejected(self, conn):
        """A ChangesZone trigger with non-Creature filter (e.g. an
        artifact ETB) shouldn't fire the soul-sister tier."""
        _add_port(
            conn,
            "Artifact ETB Trigger",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Artifact.YouCtrl",
            raw_line=(
                "{'Mode': 'ChangesZone', 'Destination': 'Battlefield', "
                "'ValidCard': 'Artifact.YouCtrl', 'Execute': 'TrigGainLife'}"
            ),
        )
        _add_port(
            conn,
            "Artifact ETB Trigger",
            port_type="effect",
            event_class="GainLife",
            raw_line="{'DB': 'GainLife', 'LifeAmount': '1'}",
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        assert "Artifact ETB Trigger" not in _candidates(results)

    def test_etb_trigger_without_gainlife_rejected(self, conn):
        """Trigger.ChangesZone with Creature filter but no effect.GainLife
        doesn't qualify — the soul-sister tier requires the GainLife
        effect coupling."""
        _add_port(
            conn,
            "Creature ETB No Life",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.Other",
            raw_line=(
                "{'Mode': 'ChangesZone', 'Destination': 'Battlefield', "
                "'ValidCard': 'Creature.Other', 'Execute': 'TrigDraw'}"
            ),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        assert "Creature ETB No Life" not in _candidates(results)

    def test_amp_wins_over_etb_dedup(self, conn):
        """A card matching BOTH tiers (hypothetical: a soul sister
        that also replaces GainLife) gets one complement — the
        higher-priority amp tier wins."""
        _add_port(
            conn,
            "Hybrid Lifegain",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainDouble'}"),
        )
        _add_port(
            conn,
            "Hybrid Lifegain",
            port_type="trigger",
            event_class="ChangesZone",
            valid_filter="Creature.Other",
            raw_line=("{'Mode': 'ChangesZone', 'Destination': 'Battlefield', 'ValidCard': 'Creature.Other'}"),
        )
        _add_port(
            conn,
            "Hybrid Lifegain",
            port_type="effect",
            event_class="GainLife",
            raw_line="{'DB': 'GainLife', 'LifeAmount': '1'}",
        )
        hybrids = [
            r
            for r in _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
            if r.candidate == "Hybrid Lifegain"
        ]
        assert len(hybrids) == 1
        assert hybrids[0].cand_event == "lifegain_amp"

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Celestine, the Living Saint",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainDouble'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), {"Celestine, the Living Saint"})
        assert "Celestine, the Living Saint" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Rhox Faithmender",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainDouble'}"),
        )
        results = _find_lifegain_feeders(conn, self._lifegain_cmdr_ports(), set())
        assert results
        assert all(r.rule_id == "lifegain_feeder" for r in results)


class TestFindLandBounceFeeders:
    """Rule for commanders whose activated ability costs a land-return
    (Meloku / Mina and Denn / Multani / Soramaro / Sutina / Tameshi).

    Two deduped tiers: land_bounce_extra_drops (AdjustLandPlays statics)
    > land_bounce_gy_recur (Land recursion from graveyard)."""

    def _meloku_ports(self):
        """Meloku-style: cost.return with cost_subtype '1/Land' target any."""
        return [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/Land",
                cost_target="any",
                raw_line="1 Return<1/Land>",
            )
        ]

    def test_no_land_return_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="Attacks")]
        assert _find_land_bounce_feeders(conn, ports, set()) == []

    def test_self_bounce_rejected(self, conn):
        """Rootha / Shigeki / Bilbo: cost.return with cost_target='self' —
        they return THEMSELVES, not lands. Different archetype."""
        ports = [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/CARDNAME",
                cost_target="self",
                raw_line="2 Return<1/CARDNAME>",
            ),
        ]
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        assert _find_land_bounce_feeders(conn, ports, set()) == []

    def test_big_hand_cmdr_excluded(self, conn):
        """Soramaro-style: has cost.return<Land> AND scales_with.ValidHand
        Card.YouOwn (big-hand primary axis). Rule must not fire — the
        bounce is incidental to her hand-size payoff."""
        soramaro_ports = [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/Land",
                cost_target="any",
                raw_line="4 Return<1/Land>",
            ),
            _port_row(
                port_type="scales_with",
                event_class="ValidHand Card.YouOwn",
                raw_line="SVar:X:Count$ValidHand Card.YouOwn",
            ),
        ]
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        assert _find_land_bounce_feeders(conn, soramaro_ports, set()) == []

    def test_xpaid_cmdr_excluded(self, conn):
        """Tameshi-style: has cost.return<Land> AND scales_with.xPaid
        (X-cost flicker primary axis). Rule must not fire."""
        tameshi_ports = [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/Land/land",
                cost_target="any",
                raw_line="X W Return<1/Land/land>",
            ),
            _port_row(
                port_type="scales_with",
                event_class="xPaid",
                raw_line="SVar:X:Count$xPaid",
            ),
        ]
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        assert _find_land_bounce_feeders(conn, tameshi_ports, set()) == []

    def test_non_land_return_rejected(self, conn):
        """cost.return with cost_subtype Creature/Artifact — not a land-bounce
        archetype; rule must not fire."""
        ports = [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/Creature",
                cost_target="any",
                raw_line="2 Return<1/Creature>",
            ),
        ]
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        assert _find_land_bounce_feeders(conn, ports, set()) == []

    def test_extra_drops_tier_fires(self, conn):
        """Azusa: static.Continuous with AdjustLandPlays — tier 1 payoff."""
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        results = _find_land_bounce_feeders(conn, self._meloku_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Azusa, Lost but Seeking") == "land_bounce_extra_drops"

    def test_gy_recur_tier_fires(self, conn):
        """Crucible of Worlds: effect.ChangeZone Graveyard-origin Land."""
        _add_port(
            conn,
            "Crucible of Worlds",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land.YouOwn",
            raw_line="{'DB': 'ChangeZone', 'Origin': 'Graveyard', 'ValidCard': 'Land.YouOwn'}",
        )
        results = _find_land_bounce_feeders(conn, self._meloku_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Crucible of Worlds") == "land_bounce_gy_recur"

    def test_opponent_grave_rejected(self, conn):
        """Land-recursion card that targets opponents' graveyard must be
        rejected — it doesn't bring YOUR lands back."""
        _add_port(
            conn,
            "Opp Grave Grab",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land.YouOwn",
            raw_line="{'DB': 'ChangeZone', 'Origin': 'Graveyard', 'Defined': 'Opponent'}",
        )
        results = _find_land_bounce_feeders(conn, self._meloku_ports(), set())
        assert "Opp Grave Grab" not in _candidates(results)

    def test_extra_drops_wins_over_gy_recur_dedup(self, conn):
        """A hybrid card matching BOTH tiers gets ONE complement —
        the higher-priority extra_drops tier wins."""
        _add_port(
            conn,
            "Hybrid Land Card",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '1'}",
        )
        _add_port(
            conn,
            "Hybrid Land Card",
            port_type="effect",
            event_class="ChangeZone",
            zone_origin="Graveyard",
            valid_filter="Land.YouOwn",
            raw_line="{'DB': 'ChangeZone', 'Origin': 'Graveyard', 'ValidCard': 'Land.YouOwn'}",
        )
        hybrids = [
            r for r in _find_land_bounce_feeders(conn, self._meloku_ports(), set()) if r.candidate == "Hybrid Land Card"
        ]
        assert len(hybrids) == 1
        assert hybrids[0].cand_event == "land_bounce_extra_drops"

    def test_typed_tagged_land_subtype_fires(self, conn):
        """Tameshi's cost_subtype is ``1/Land/land`` (typed-and-tagged).
        The gate must accept this variant."""
        tameshi_ports = [
            _port_row(
                port_type="cost",
                event_class="return",
                cost_subtype="1/Land/land",
                cost_target="any",
                raw_line="X W Return<1/Land/land>",
            )
        ]
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        results = _find_land_bounce_feeders(conn, tameshi_ports, set())
        assert "Azusa, Lost but Seeking" in _candidates(results)

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Meloku the Clouded Mirror",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        results = _find_land_bounce_feeders(conn, self._meloku_ports(), {"Meloku the Clouded Mirror"})
        assert "Meloku the Clouded Mirror" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Azusa, Lost but Seeking",
            port_type="static",
            event_class="Continuous",
            raw_line="{'Mode': 'Continuous', 'Affected': 'Self', 'AdjustLandPlays': '2'}",
        )
        results = _find_land_bounce_feeders(conn, self._meloku_ports(), set())
        assert results
        assert all(r.rule_id == "land_bounce_feeder" for r in results)
        assert all(r.cmdr_event == "land_bounce_cost" for r in results)


class TestFindLifeTotalFeeders:
    """Rule for commanders with a ``scales_with YourLifeTotal`` port
    AND an up-biased lifegain signal (GainLife replacement amp on
    self OR static.Continuous with SVarCompare GT*/GE*).

    Narrow gate leaves Bilbo-Birthday-Celebrant (GainLife doubler)
    and Elenda-Saint-of-Dusk (+1/+1 when life > starting). Excludes
    Ayli / Bane / Beza / Cecil / Jerren / Linvala where life is used
    as a query variable — first attempt fed them generic lifegain
    peers and regressed their top-30 (audit 2026-04-20, reverted in
    commit ec67250)."""

    def _elenda_ports(self):
        """Elenda: scales_with.YourLifeTotal + up-biased Continuous static
        (SVarCompare 'GTY' = life > starting life)."""
        return [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:X:Count$YourLifeTotal",
            ),
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Card.Self', "
                    "'AddPower': '1', 'AddToughness': '1', 'CheckSVar': 'X', "
                    "'SVarCompare': 'GTY'}"
                ),
            ),
            _port_row(port_type="keyword", event_class="Lifelink"),
        ]

    def _bilbo_ports(self):
        """Bilbo: scales_with.YourLifeTotal + replacement.GainLife amp."""
        return [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:Z:Count$YourLifeTotal",
            ),
            _port_row(
                port_type="replacement",
                event_class="GainLife",
                raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainLife'}"),
            ),
        ]

    def test_no_life_axis_skips(self, conn):
        ports = [_port_row(port_type="keyword", event_class="Lifelink")]
        assert _find_life_total_feeders(conn, ports, set()) == []

    def test_down_biased_static_rejected(self, conn):
        """Bane-style: SVarCompare 'LEX' (life <= half starting) —
        down-biased threshold, must NOT fire the rule."""
        bane_ports = [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:CurrentLife:Count$YourLifeTotal",
            ),
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Card.Self', "
                    "'AddKeyword': 'Indestructible', 'CheckSVar': 'CurrentLife', "
                    "'SVarCompare': 'LEX'}"
                ),
            ),
        ]
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        assert _find_life_total_feeders(conn, bane_ports, set()) == []

    def test_query_variable_cmdr_rejected(self, conn):
        """Ayli-style: scales_with.YourLifeTotal without any up-biased
        static or GainLife replacement — rule doesn't fire (avoids
        feeding query-variable commanders like Ayli/Jerren/Cecil/Linvala)."""
        ayli_ports = [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:X:Count$YourLifeTotal",
            ),
            _port_row(port_type="keyword", event_class="Deathtouch"),
        ]
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        assert _find_life_total_feeders(conn, ayli_ports, set()) == []

    def test_up_biased_static_fires(self, conn):
        """Elenda: GTY compare → up-biased, peer cards feed."""
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        results = _find_life_total_feeders(conn, self._elenda_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Angel of Vitality") == "life_total_peer"

    def test_gainlife_replacement_amp_fires(self, conn):
        """Bilbo: replacement.GainLife amp on self → up-biased."""
        _add_port(
            conn,
            "Leyline of Hope",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:X:Count$YourLifeTotal",
        )
        _add_port(
            conn,
            "Leyline of Hope",
            port_type="replacement",
            event_class="GainLife",
            raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'ReplaceWith': 'GainLife'}"),
        )
        results = _find_life_total_feeders(conn, self._bilbo_ports(), set())
        assert "Leyline of Hope" in _candidates(results)

    def test_gainlife_prevent_rejected_as_signal(self, conn):
        """A replacement.GainLife with ``'Prevent': 'True'`` (Sulfuric
        Vortex-style) must NOT count as up-bias."""
        sulfuric_cmdr = [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:X:Count$YourLifeTotal",
            ),
            _port_row(
                port_type="replacement",
                event_class="GainLife",
                raw_line=("{'Event': 'GainLife', 'ValidPlayer': 'You', 'Prevent': 'True'}"),
            ),
        ]
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        assert _find_life_total_feeders(conn, sulfuric_cmdr, set()) == []

    def test_inverse_bias_peer_rejected(self, conn):
        """Death's Shadow: scales with life but only LoseLife effects —
        fails symmetric positive-bias filter, must not be a peer."""
        _add_port(
            conn,
            "Death's Shadow",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(
            conn,
            "Death's Shadow",
            port_type="effect",
            event_class="LoseLife",
            valid_filter="You",
            raw_line="{'DB': 'LoseLife', 'Defined': 'You'}",
        )
        results = _find_life_total_feeders(conn, self._elenda_ports(), set())
        assert "Death's Shadow" not in _candidates(results)

    def test_non_you_gainlife_peer_rejected(self, conn):
        """Peer with effect.GainLife on 'Opponent' shouldn't qualify."""
        _add_port(
            conn,
            "Opponent GainLife Peer",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:X:Count$YourLifeTotal",
        )
        _add_port(
            conn,
            "Opponent GainLife Peer",
            port_type="effect",
            event_class="GainLife",
            valid_filter="Opponent",
            raw_line="{'DB': 'GainLife', 'Defined': 'Opponent'}",
        )
        results = _find_life_total_feeders(conn, self._elenda_ports(), set())
        assert "Opponent GainLife Peer" not in _candidates(results)

    def test_ge_compare_also_up_biased(self, conn):
        """SVarCompare 'GEZ' (life >= +10 starting) is also up-biased —
        Elenda's second static clause."""
        ge_cmdr = [
            _port_row(
                port_type="scales_with",
                event_class="YourLifeTotal",
                raw_line="SVar:X:Count$YourLifeTotal",
            ),
            _port_row(
                port_type="static",
                event_class="Continuous",
                raw_line=(
                    "{'Mode': 'Continuous', 'Affected': 'Card.Self', "
                    "'AddPower': '5', 'CheckSVar': 'X', 'SVarCompare': 'GEZ'}"
                ),
            ),
        ]
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        results = _find_life_total_feeders(conn, ge_cmdr, set())
        assert "Angel of Vitality" in _candidates(results)

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Elenda, Saint of Dusk",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:X:Count$YourLifeTotal",
        )
        _add_port(conn, "Elenda, Saint of Dusk", port_type="keyword", event_class="Lifelink")
        results = _find_life_total_feeders(conn, self._elenda_ports(), {"Elenda, Saint of Dusk"})
        assert "Elenda, Saint of Dusk" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Angel of Vitality",
            port_type="scales_with",
            event_class="YourLifeTotal",
            raw_line="SVar:Y:Count$YourLifeTotal",
        )
        _add_port(conn, "Angel of Vitality", port_type="keyword", event_class="Lifelink")
        results = _find_life_total_feeders(conn, self._elenda_ports(), set())
        assert results
        assert all(r.rule_id == "life_total_feeder" for r in results)
        assert all(r.cmdr_event == "life_total_axis" for r in results)


class TestFindDamageDoublerSynergy:
    """General rule for replacement.DamageDone commanders that AMPLIFY
    damage (Torbran +2, Gisela / Solphim double, Tor Wauki / Raphael
    / Wolverine variants). Rejects prevention / self-routing /
    damage-decreasing replacements. Two tiers: amp_stack (other
    doublers stack multiplicatively) > damage_pinger (repeatable
    triggered DealDamage on opponents)."""

    def _torbran_ports(self):
        return [
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="DmgPlus2",
                raw_line=(
                    "{'Event': 'DamageDone', 'ValidSource': 'Card.RedSource+YouCtrl', "
                    "'ValidTarget': 'Player.Opponent,Permanent.OppCtrl', "
                    "'ReplaceWith': 'DmgPlus2'}"
                ),
            )
        ]

    def _gisela_ports(self):
        # Gisela has TWO replacement.DamageDone — the doubler AND the
        # half-prevention. Only the doubler should activate the rule.
        return [
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="DmgTwice",
                raw_line=(
                    "{'Event': 'DamageDone', 'ValidSource': 'Card,Emblem', "
                    "'ValidTarget': 'Opponent,Permanent.OppCtrl', "
                    "'ReplaceWith': 'DmgTwice'}"
                ),
            ),
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="DBReplace",
                raw_line=(
                    "{'Event': 'DamageDone', 'ValidTarget': 'You,Permanent.YouCtrl', "
                    "'ReplaceWith': 'DBReplace', 'PreventionEffect': 'True'}"
                ),
            ),
        ]

    def test_no_replacement_skips(self, conn):
        ports = [_port_row(port_type="trigger", event_class="DamageDone")]
        assert _find_damage_doubler_synergy(conn, ports, set()) == []

    def test_prevention_skips(self, conn):
        """Iroas / Tajic / Emmara / Frodo: replacement.DamageDone with
        Prevent: True or PreventionEffect: True. Different mechanical
        axis — must not activate the doubler rule."""
        ports = [
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="Prevent",
                raw_line=("{'Event': 'DamageDone', 'Prevent': 'True', 'ValidTarget': 'Creature.attacking+YouCtrl'}"),
            )
        ]
        assert _find_damage_doubler_synergy(conn, ports, set()) == []

    def test_self_target_replacement_skips(self, conn):
        """Dralnu / Polukranos / Sekki: damage to me → custom effect.
        Self-only targets aren't damage-amplifier commanders."""
        ports = [
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="Sac",
                raw_line=("{'Event': 'DamageDone', 'ValidTarget': 'Card.Self', 'ReplaceWith': 'Sac'}"),
            )
        ]
        assert _find_damage_doubler_synergy(conn, ports, set()) == []

    def test_damage_decrease_skips(self, conn):
        """DmgMinus1 / DmgHalfDown decrease damage — same rejection as
        Prevent. Their result tokens are not in the amp set."""
        ports = [
            _port_row(
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="DmgMinus1",
                raw_line=("{'Event': 'DamageDone', 'ValidTarget': 'Permanent.OppCtrl', 'ReplaceWith': 'DmgMinus1'}"),
            )
        ]
        assert _find_damage_doubler_synergy(conn, ports, set()) == []

    def test_torbran_activates(self, conn):
        _add_port(
            conn,
            "Furnace of Rath",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        names = _candidates(results)
        assert "Furnace of Rath" in names

    def test_gisela_activates_via_doubler_port(self, conn):
        """Gisela's preventer port should NOT block — her doubler port
        still activates the rule."""
        _add_port(
            conn,
            "Dictate of the Twin Gods",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
        )
        results = _find_damage_doubler_synergy(conn, self._gisela_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Dictate of the Twin Gods") == "damage_amp_stack"

    def test_amp_stack_tier(self, conn):
        for amp in ("Furnace of Rath", "Fiery Emancipation", "Curse of Bloodletting"):
            _add_port(
                conn,
                amp,
                port_type="replacement",
                event_class="DamageDone",
                replacement_event="DamageDone",
                replacement_result="DmgTwice",
                raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
            )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events["Furnace of Rath"] == "damage_amp_stack"
        assert events["Fiery Emancipation"] == "damage_amp_stack"
        assert events["Curse of Bloodletting"] == "damage_amp_stack"

    def test_amp_stack_excludes_prevention_replacement(self, conn):
        """A prevention-only replacement on the candidate side (Divine
        Presence, Lich's Mirror) is not a damage amplifier even if its
        replacement_result happens to be in the amp set due to schema
        quirk — gate by raw_line Prevent flag."""
        _add_port(
            conn,
            "Mock Preventer",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice', 'Prevent': 'True'}",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        assert "Mock Preventer" not in _candidates(results)

    def test_pinger_tier(self, conn):
        """Guttersnipe-shape: trigger.SpellCast + effect.DealDamage on
        Player. The amp turns each cast into a heavier ping."""
        _add_port(
            conn,
            "Guttersnipe",
            port_type="trigger",
            event_class="SpellCast",
            valid_filter="Instant.YouCtrl,Sorcery.YouCtrl",
        )
        _add_port(
            conn,
            "Guttersnipe",
            port_type="effect",
            event_class="DealDamage",
            valid_filter="Player.Opponent",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        events = {r.candidate: r.cand_event for r in results}
        assert events.get("Guttersnipe") == "damage_pinger"

    def test_pinger_excludes_combat_only_trigger(self, conn):
        """A creature whose only trigger is Attacks (combat-only) +
        DealDamage isn't a non-combat ping engine — it's a voltron
        combat creature, handled by combat_enhancer."""
        _add_port(
            conn,
            "Combat-Only Creature",
            port_type="trigger",
            event_class="Attacks",
            valid_filter="Card.Self",
        )
        _add_port(
            conn,
            "Combat-Only Creature",
            port_type="effect",
            event_class="DealDamage",
            valid_filter="Player.Opponent",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        assert "Combat-Only Creature" not in _candidates(results)

    def test_pinger_requires_opponent_target(self, conn):
        """A DealDamage effect on an unrelated valid_filter (e.g.
        Creature.YouCtrl — self-burn) doesn't count as a ping engine."""
        _add_port(
            conn,
            "Self-Burn",
            port_type="trigger",
            event_class="SpellCast",
            valid_filter="Card.YouCtrl",
        )
        _add_port(
            conn,
            "Self-Burn",
            port_type="effect",
            event_class="DealDamage",
            valid_filter="Creature.YouCtrl",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        assert "Self-Burn" not in _candidates(results)

    def test_dedup_amp_wins_over_pinger(self, conn):
        """A card that fits both tiers (e.g. an enchantment that both
        doubles damage AND has a SpellCast→DealDamage trigger) gets
        ONE complement in the higher-priority amp_stack tier."""
        _add_port(
            conn,
            "Dual",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
        )
        _add_port(conn, "Dual", port_type="trigger", event_class="SpellCast")
        _add_port(
            conn,
            "Dual",
            port_type="effect",
            event_class="DealDamage",
            valid_filter="Player.Opponent",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        dual = [r for r in results if r.candidate == "Dual"]
        assert len(dual) == 1
        assert dual[0].cand_event == "damage_amp_stack"

    def test_excludes_commander(self, conn):
        _add_port(
            conn,
            "Torbran, Thane of Red Fell",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), {"Torbran, Thane of Red Fell"})
        assert "Torbran, Thane of Red Fell" not in _candidates(results)

    def test_rule_id(self, conn):
        _add_port(
            conn,
            "Furnace of Rath",
            port_type="replacement",
            event_class="DamageDone",
            replacement_event="DamageDone",
            replacement_result="DmgTwice",
            raw_line="{'Event': 'DamageDone', 'ReplaceWith': 'DmgTwice'}",
        )
        results = _find_damage_doubler_synergy(conn, self._torbran_ports(), set())
        assert all(r.rule_id == "damage_doubler_synergy" for r in results)
