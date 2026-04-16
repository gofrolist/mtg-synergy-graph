"""Phase 2 graph engine tests (SPEC §6.1, §6.2, §6.3, §6.5).

Includes the Korvold acceptance scenario from §12 Phase 2:

* Korvold's ``Sacrificed`` trigger should be fed by Phyrexian Altar's
  sacrifice cost (cost-feed match, §6.3).
* Wrath of God should NOT register as a positive feeder for Korvold.
* Cathars' Crusade ETB trigger should be amplified by Panharmonicon (§6.2.3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph import (
    BRANCH_MULTIPLIER,
    EVENT_MATCH_MAP,
    filter_matches,
    find_amplifier_matches,
    find_replacement_conflicts,
    find_trigger_feeders,
    match_event,
    parser_branch_kinds,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import (
    _is_substitution_blocking_result,
    _is_unhelpful_payoff_trigger,
    _zone_overlap,
)
from mtg_synergy_graph.importer import import_cards_folder

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def populated_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("graph") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Branch weight invariant
# ---------------------------------------------------------------------------


def test_branch_multiplier_covers_every_parser_branch_kind():
    missing = parser_branch_kinds() - set(BRANCH_MULTIPLIER)
    assert missing == set(), f"missing branch_kinds: {missing}"


# ---------------------------------------------------------------------------
# match_event — direct matches and catch-alls
# ---------------------------------------------------------------------------


def test_match_event_changes_zone_to_token():
    trig = {"event_class": "ChangesZone", "zone_destination": "Battlefield"}
    eff = {"event_class": "Token"}
    assert match_event(trig, eff)


def test_match_event_spellcast_catchall():
    trig = {"event_class": "SpellCast"}
    assert match_event(trig, {"event_class": "Draw"})
    assert match_event(trig, {"event_class": "Pump"})


def test_match_event_sacrificed_to_sacrifice():
    assert match_event(
        {"event_class": "Sacrificed"},
        {"event_class": "Sacrifice"},
    )


def test_match_event_unrelated_events_dont_match():
    assert not match_event(
        {"event_class": "DamageDone"},
        {"event_class": "Token"},
    )


def test_event_match_map_loaded():
    assert "ChangesZone" in EVENT_MATCH_MAP
    assert "SpellCast" in EVENT_MATCH_MAP


# Phase B1: trigger ↔ effect pairs added from corpus inventory.
def test_match_event_proliferate_pair():
    assert match_event(
        {"event_class": "Proliferate"},
        {"event_class": "Proliferate"},
    )


def test_match_event_investigated_to_investigate():
    assert match_event(
        {"event_class": "Investigated"},
        {"event_class": "Investigate"},
    )


def test_match_event_surveil_pair():
    assert match_event(
        {"event_class": "Surveil"},
        {"event_class": "Surveil"},
    )


def test_match_event_life_lost_to_lose_life():
    assert match_event(
        {"event_class": "LifeLost"},
        {"event_class": "LoseLife"},
    )


def test_match_event_life_lost_does_not_match_unrelated():
    assert not match_event(
        {"event_class": "LifeLost"},
        {"event_class": "GainLife"},
    )


# ---------------------------------------------------------------------------
# filter_matches — Python fallback (controller is runtime-only)
# ---------------------------------------------------------------------------


def test_filter_matches_creature_youctrl_skips_controller():
    candidate_attrs = [("type", "Creature"), ("subtype", "Goblin")]
    assert filter_matches("Creature.YouCtrl", candidate_attrs)


def test_filter_matches_negation_works():
    candidate_attrs = [("type", "Creature"), ("color", "Black")]
    assert not filter_matches("Creature+!Black", candidate_attrs)


def test_filter_matches_required_subtype_missing():
    candidate_attrs = [("type", "Creature")]
    assert not filter_matches("Creature.Goblin", candidate_attrs)


# ---------------------------------------------------------------------------
# §6.3 — Korvold cost-feed acceptance
# ---------------------------------------------------------------------------


def test_phyrexian_altar_feeds_korvold_sacrifice_trigger(populated_db):
    feeders = find_trigger_feeders(populated_db, ["Korvold, Fae-Cursed King"])
    altar_rows = [
        f
        for f in feeders
        if f["candidate"] == "Phyrexian Altar" and f["trigger_event"] == "Sacrificed" and f["match_kind"] == "cost"
    ]
    assert altar_rows, "Phyrexian Altar must register as a cost-feed sacrifice match"


def test_wrath_of_god_does_not_feed_korvold(populated_db):
    feeders = find_trigger_feeders(populated_db, ["Korvold, Fae-Cursed King"])
    wrath = [f for f in feeders if f["candidate"] == "Wrath of God"]
    assert wrath == []


def test_panharmonicon_amplifies_cathars_etb_trigger(populated_db):
    rows = find_amplifier_matches(populated_db, ["Cathars' Crusade"])
    cards = {r["amplifier_card"] for r in rows}
    assert "Panharmonicon" in cards


# ---------------------------------------------------------------------------
# §6.5 — replacement anti-synergy (no fixture today, just sanity)
# ---------------------------------------------------------------------------


def test_replacement_conflicts_returns_empty_for_korvold(populated_db):
    """No fixture yet contains a Prevent replacement that hits Korvold's
    triggers — the call should still return an empty list cleanly."""
    assert find_replacement_conflicts(populated_db, ["Korvold, Fae-Cursed King"]) == []


# ---------------------------------------------------------------------------
# Phase C1 — zone-aware Moved/Prevent (graveyard hate)
# ---------------------------------------------------------------------------


# Phase D1/D2/D4 helpers/matchers are imported at the top of the file.


def test_zone_overlap_grafdiggers_cage_matches_reanimator():
    # Replacement: Origin=Graveyard,Library Destination=Battlefield
    # Trigger:     Origin=Graveyard           Destination=Battlefield (Karador)
    assert _zone_overlap(
        "Graveyard,Library",
        "Battlefield",
        [("ChangesZone", "Graveyard", "Battlefield")],
    )


def test_zone_overlap_grafdiggers_cage_does_not_match_blink_commander():
    # Brago: ChangesZone Origin=Battlefield Destination=Battlefield (flicker
    # path is exile-then-return, but the trigger is on attack damage; his
    # *zone* trigger is the flickered creature's ETB which fires on
    # Battlefield→Battlefield. Origin sets disjoint → no overlap.
    assert not _zone_overlap(
        "Graveyard,Library",
        "Battlefield",
        [("ChangesZone", "Exile", "Battlefield")],
    )


def test_zone_overlap_unscoped_replacement_matches_anything():
    # An empty replacement zone field means "any zone" — must still match.
    assert _zone_overlap("", "", [("ChangesZone", "Graveyard", "Battlefield")])


def test_zone_overlap_destination_mismatch_blocks():
    # Replacement only blocks zone changes INTO the battlefield. A trigger
    # firing on Battlefield→Graveyard (death trigger) is unaffected.
    assert not _zone_overlap(
        "Graveyard",
        "Battlefield",
        [("ChangesZone", "Battlefield", "Graveyard")],
    )


def test_zone_overlap_skips_non_changes_zone_triggers():
    assert not _zone_overlap(
        "Graveyard",
        "Battlefield",
        [("Attacks", "", ""), ("DamageDone", "", "")],
    )


# ---------------------------------------------------------------------------
# Phase D1 — sacrifice outlet ↔ payoff matcher
# ---------------------------------------------------------------------------


def test_unhelpful_payoff_trigger_rejects_opp_ctrl():
    assert _is_unhelpful_payoff_trigger("Permanent.!token+OppCtrl")


def test_unhelpful_payoff_trigger_rejects_card_self():
    # Blightbelly Rat: ChangesZone with ValidCard$ Card.Self — fires only
    # on its own death and doesn't earn the cluster bonus.
    assert _is_unhelpful_payoff_trigger("Card.Self")


def test_unhelpful_payoff_trigger_accepts_friendly_subtype():
    # Empty / unscoped / explicit YouCtrl filter all pass.
    assert not _is_unhelpful_payoff_trigger("Creature.YouCtrl")
    assert not _is_unhelpful_payoff_trigger("Food")
    assert not _is_unhelpful_payoff_trigger(None)
    assert not _is_unhelpful_payoff_trigger("")


# ---------------------------------------------------------------------------
# Phase C2 — substitution-blocking replacement detection
# ---------------------------------------------------------------------------


# _is_substitution_blocking_result is imported at the top of the file.


def test_substitution_blocking_rest_in_peace_pattern():
    # Moved + Graveyard destination + Exile substitution = GY hate.
    assert _is_substitution_blocking_result("Moved", "Exile", "Graveyard")


def test_substitution_blocking_rejects_amplifiers():
    # Doublers are NOT blocks — they're synergy.
    assert not _is_substitution_blocking_result("GainLife", "GainDouble", "")
    assert not _is_substitution_blocking_result("DamageDone", "DmgTwice", "")


def test_substitution_blocking_rejects_etb_tapped():
    # Card.Self entry modifiers are not anti-synergy for any commander.
    assert not _is_substitution_blocking_result("Moved", "ETBTapped", "Battlefield")


def test_substitution_blocking_requires_graveyard_destination():
    # Only the GY→Exile pattern is handled today.
    assert not _is_substitution_blocking_result("Moved", "Exile", "Battlefield")
    assert not _is_substitution_blocking_result("Moved", "Exile", "")


def test_substitution_blocking_rejects_non_moved_events():
    assert not _is_substitution_blocking_result("Draw", "Exile", "Graveyard")
    assert not _is_substitution_blocking_result("", "Exile", "Graveyard")


# ---------------------------------------------------------------------------
# Phase F6 — trigger resonance
# ---------------------------------------------------------------------------
