"""Tests for mtg_synergy_graph.complement_rules.utility functions.

Uses an in-memory SQLite database with minimal card_ports rows to exercise
each branch of the five _find_* functions in utility.py.
"""

from __future__ import annotations

import sqlite3

import pytest

from mtg_synergy_graph.complement_rules.utility import (
    _find_cost_payoff_complements,
    _find_counter_axis_feeders,
    _find_counter_target_payoff,
    _find_creature_untap_engine,
    _find_creatures_as_lands_landfall,
    _find_damage_effect_synergy,
    _find_extra_land_plays,
    _find_flicker_synergy,
    _find_mana_doubler_synergy,
    _find_monarch_synergy,
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
# _find_damage_effect_synergy
# ---------------------------------------------------------------------------


class TestDamageEffectSynergy:
    def test_noncombat_damage_trigger_finds_dealerdamage(self, conn):
        """Niv-Mizzet (DamageDone, non-combat) -> finds DealDamage effects."""
        _add_port(conn, "Guttersnipe", port_type="effect", event_class="DealDamage")
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone", valid_filter="", raw_line="")]
        results = _find_damage_effect_synergy(conn, cmdr_ports, set())
        assert len(results) == 1
        assert results[0].candidate == "Guttersnipe"
        assert results[0].rule_id == "damage_synergy"

    def test_combat_damage_trigger_skipped(self, conn):
        """Combat-only DamageDone trigger should NOT match (uses combat_enhancer)."""
        _add_port(conn, "Guttersnipe", port_type="effect", event_class="DealDamage")
        cmdr_ports = [
            _port_row(
                port_type="trigger",
                event_class="DamageDone",
                valid_filter="Creature.YouCtrl",
                raw_line="{'CombatDamage': 'True'}",
            )
        ]
        results = _find_damage_effect_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_self_only_trigger_skipped(self, conn):
        """Card.Self DamageDone trigger should NOT match."""
        _add_port(conn, "Guttersnipe", port_type="effect", event_class="DealDamage")
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone", valid_filter="Card.Self")]
        results = _find_damage_effect_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_also_finds_damageall(self, conn):
        """DamageAll effects should also be found."""
        _add_port(conn, "Earthquake", port_type="effect", event_class="DamageAll")
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone")]
        results = _find_damage_effect_synergy(conn, cmdr_ports, set())
        assert len(results) == 1

    def test_no_damage_trigger_returns_empty(self, conn):
        _add_port(conn, "Guttersnipe", port_type="effect", event_class="DealDamage")
        cmdr_ports = [_port_row(port_type="trigger", event_class="SpellCast")]
        results = _find_damage_effect_synergy(conn, cmdr_ports, set())
        assert results == []

    def test_excludes_commander(self, conn):
        _add_port(conn, "Cmdr", port_type="effect", event_class="DealDamage")
        cmdr_ports = [_port_row(port_type="trigger", event_class="DamageDone")]
        results = _find_damage_effect_synergy(conn, cmdr_ports, {"Cmdr"})
        assert results == []


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
