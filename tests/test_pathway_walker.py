"""Unit tests for the depth-2 self-bridging pathway walker.

The walker is a pure DB-free function: given a list of candidate ports
M that each match at least one commander port, it detects whether any
pair of ports in M has an internal length-<=2 edge via one of two
canonical cascade substrates:

1. ``event_match`` — ``EVENT_MATCH_MAP`` predicate fires in either
   orientation (trigger/effect pair).
2. ``cost_feeds`` — ``COST_FEEDS_TRIGGER`` links a cost event_class to
   a trigger event_class.

The ``valid_filter`` channel was evaluated in the 2026-04-23 audit and
removed: it fired too broadly on voltron / proliferate / monarch
commanders whose typed triggers matched equipment/aura cascade shapes
that weren't genuine internal edges. See ``docs/RULE_HISTORY.md``.

First-match wins; event_match is tried before cost_feeds.
"""

from __future__ import annotations

from typing import Any

from mtg_synergy_graph.complement_rules.pathway import _walk_self_paths

PortRow = dict[str, Any]


def _port(
    port_type: str,
    event_class: str,
    *,
    valid_filter: str = "",
    zone_origin: str = "",
    zone_destination: str = "",
    counter_type: str = "",
) -> PortRow:
    """Minimal PortRow factory for walker tests.

    Matches the column shape that ``_row_to_dict`` produces from the
    ``card_ports`` table so the walker's contract is symmetric with
    real data.
    """
    return {
        "port_type": port_type,
        "event_class": event_class,
        "valid_filter": valid_filter,
        "zone_origin": zone_origin,
        "zone_destination": zone_destination,
        "counter_type": counter_type,
    }


# ---------------------------------------------------------------------------
# Happy paths — one per channel
# ---------------------------------------------------------------------------


def test_event_match_channel_bloodghast_shape() -> None:
    """Landfall trigger (ChangesZone, zone_destination=Battlefield)
    paired with a self-return effect (ChangeZone, Graveyard ->
    Battlefield) forms an internal edge via EVENT_MATCH_MAP with
    ``zone_compatible`` predicate."""
    landfall_trigger = _port("trigger", "ChangesZone", zone_destination="Battlefield")
    return_effect = _port("effect", "ChangeZone", zone_origin="Graveyard", zone_destination="Battlefield")
    result = _walk_self_paths([landfall_trigger, return_effect])
    assert result is not None
    p1, p2, channel = result
    assert channel == "event_match"
    assert {p1["event_class"], p2["event_class"]} == {"ChangesZone", "ChangeZone"}


def test_cost_feeds_channel_sacrifice_dies_shape() -> None:
    """A sacrifice cost port paired with a Sacrificed trigger port on
    the same card forms an internal edge via COST_FEEDS_TRIGGER.
    """
    sac_cost = _port("cost", "sacrifice")
    dies_trigger = _port("trigger", "Sacrificed")
    result = _walk_self_paths([sac_cost, dies_trigger])
    assert result is not None
    _, _, channel = result
    assert channel == "cost_feeds"


def test_valid_filter_alone_does_not_form_internal_edge() -> None:
    """Two ports sharing a ``valid_filter`` type family but lacking
    an event_match or cost_feeds link do NOT form an internal edge.

    Regression guard: the 2026-04-23 audit removed ``valid_filter`` as
    an internal-edge channel because it fired on voltron / proliferate
    equipment + static pairs that weren't genuine cascade shapes.
    """
    damage_trigger = _port("trigger", "DamageDone", valid_filter="Creature.YouCtrl+powerGE4")
    static_buff = _port("static", "Continuous", valid_filter="Creature.YouCtrl")
    assert _walk_self_paths([damage_trigger, static_buff]) is None


def test_event_match_channel_returned_when_both_event_match_and_filter_intersect() -> None:
    """When a pair matches via event_match AND shares a valid_filter,
    the walker returns event_match (the canonical cascade channel).
    The shared filter is incidental."""
    cz_trigger = _port("trigger", "ChangesZone", valid_filter="Creature.YouCtrl", zone_destination="Battlefield")
    cz_effect = _port(
        "effect",
        "ChangeZone",
        valid_filter="Creature.YouCtrl",
        zone_destination="Battlefield",
    )
    result = _walk_self_paths([cz_trigger, cz_effect])
    assert result is not None
    _, _, channel = result
    assert channel == "event_match"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_fewer_than_two_ports_returns_none() -> None:
    assert _walk_self_paths([]) is None
    assert _walk_self_paths([_port("trigger", "ChangesZone")]) is None


def test_two_ports_no_channel_match_returns_none() -> None:
    """Two unrelated ports — one DamageDone trigger, one Mana ability
    with disjoint filter types — have no internal edge."""
    damage = _port("trigger", "DamageDone", valid_filter="Creature.YouCtrl")
    mana = _port("effect", "Mana", valid_filter="Land.YouCtrl")
    assert _walk_self_paths([damage, mana]) is None


def test_three_matching_ports_fires_exactly_once() -> None:
    """With three ports where multiple pairs could bridge, the walker
    returns exactly one path (the first found). Caller emits one
    PortComplement per candidate."""
    sac_cost = _port("cost", "sacrifice")
    dies_trigger = _port("trigger", "Sacrificed")
    extra_trigger = _port("trigger", "Sacrificed")
    result = _walk_self_paths([sac_cost, dies_trigger, extra_trigger])
    assert result is not None
    # The walker returns a single triple; it does not return a list.
    assert isinstance(result, tuple)
    assert len(result) == 3


def test_missing_event_class_does_not_raise() -> None:
    """PortRow with empty/missing event_class is handled gracefully
    — the walker returns None rather than raising KeyError."""
    malformed = _port("trigger", "")
    partner = _port("effect", "ChangeZone", zone_destination="Battlefield")
    assert _walk_self_paths([malformed, partner]) is None


def test_empty_valid_filter_not_treated_as_wildcard() -> None:
    """An empty ``valid_filter`` must not count as a shared-family
    match. Otherwise every unrelated pair with no filters would pair.
    """
    a = _port("static", "Continuous", valid_filter="")
    b = _port("static", "Continuous", valid_filter="")
    # No event_match (Continuous is not in EVENT_MATCH_MAP), no
    # cost_feeds (neither is a cost event class), and no shared filter
    # tokens to intersect.
    assert _walk_self_paths([a, b]) is None


# ---------------------------------------------------------------------------
# Bidirectionality — event_match must fire in either orientation
# ---------------------------------------------------------------------------


def test_wildcard_trigger_does_not_form_internal_edge() -> None:
    """``Attacks``, ``SpellCast``, and ``LandPlayed`` are wildcard
    FROM-keys in ``EVENT_MATCH_MAP`` (``to == "*"``). The walker must
    NOT treat those as internal edges -- the 2026-04-23 audit showed
    wildcard matches fire spuriously on equipment where
    (trigger.Attacks, effect.Pump) is really a single ability split
    into two ports, not a cascade.
    """
    attacks_trigger = _port("trigger", "Attacks")
    pump_effect = _port("effect", "Pump")
    assert _walk_self_paths([attacks_trigger, pump_effect]) is None
    spell_trigger = _port("trigger", "SpellCast")
    draw_effect = _port("effect", "Draw")
    assert _walk_self_paths([spell_trigger, draw_effect]) is None
    land_trigger = _port("trigger", "LandPlayed")
    token_effect = _port("effect", "Token", zone_destination="Battlefield")
    assert _walk_self_paths([land_trigger, token_effect]) is None


def test_event_match_reverse_orientation() -> None:
    """If list order puts the effect port first, the walker still
    detects the trigger->effect match by calling match_event in both
    orientations."""
    effect = _port("effect", "ChangeZone", zone_destination="Battlefield")
    trigger = _port("trigger", "ChangesZone", zone_destination="Battlefield")
    result = _walk_self_paths([effect, trigger])
    assert result is not None
    _, _, channel = result
    assert channel == "event_match"


def test_cost_feeds_reverse_orientation() -> None:
    """Reverse-order input also detects the cost->trigger link."""
    dies_trigger = _port("trigger", "Sacrificed")
    sac_cost = _port("cost", "sacrifice")
    result = _walk_self_paths([dies_trigger, sac_cost])
    assert result is not None
    _, _, channel = result
    assert channel == "cost_feeds"


# ---------------------------------------------------------------------------
# Channel-specific robustness
# ---------------------------------------------------------------------------


def test_changezone_effect_plus_static_no_longer_bridges() -> None:
    """Ajani-like ChangeZone effect + Creature.YouCtrl static no
    longer forms an internal edge now that valid_filter is dropped.
    ChangeZone is not a trigger FROM-key in EVENT_MATCH_MAP, so
    event_match won't fire either. Regression guard against a
    future re-introduction of the valid_filter channel without the
    audit to support it."""
    ajani_like = _port("effect", "ChangeZone", valid_filter="Card.Creature+YouCtrl")
    creature_buff = _port("static", "Continuous", valid_filter="Creature.YouCtrl")
    assert _walk_self_paths([ajani_like, creature_buff]) is None


# ---------------------------------------------------------------------------
# Return shape
# ---------------------------------------------------------------------------


def test_returned_tuple_contains_the_two_original_ports() -> None:
    """The tuple's first two elements are the same port dicts that
    were passed in (not copies) — callers rely on dict identity for
    rendering explanations."""
    a = _port("cost", "sacrifice")
    b = _port("trigger", "Sacrificed")
    result = _walk_self_paths([a, b])
    assert result is not None
    p1, p2, _ = result
    assert p1 is a or p1 is b
    assert p2 is a or p2 is b
    assert p1 is not p2


def test_walker_returns_known_channel_identifier() -> None:
    """Sanity: when the walker fires, the returned channel string is
    one of the two documented identifiers. Guards against typos or
    silent expansion of the public contract."""
    # cost_feeds channel (canonical sac-cost + sac-trigger pair).
    cost_case = [_port("cost", "sacrifice"), _port("trigger", "Sacrificed")]
    cost_result = _walk_self_paths(cost_case)
    assert cost_result is not None
    assert cost_result[2] in {"event_match", "cost_feeds"}

    # event_match channel (ChangesZone trigger -> ChangeZone effect).
    event_case = [
        _port("trigger", "ChangesZone", zone_destination="Battlefield"),
        _port("effect", "ChangeZone", zone_destination="Battlefield"),
    ]
    event_result = _walk_self_paths(event_case)
    assert event_result is not None
    assert event_result[2] in {"event_match", "cost_feeds"}
