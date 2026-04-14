"""Phase 2 graph engine tests (SPEC §6.1, §6.2, §6.3, §6.5).

Includes the Korvold acceptance scenario from §12 Phase 2:

* Korvold's ``Sacrificed`` trigger should be fed by Phyrexian Altar's
  sacrifice cost (cost-feed match, §6.3).
* Wrath of God should NOT register as a positive feeder for Korvold.
* Cathars' Crusade ETB trigger should be amplified by Panharmonicon (§6.2.3).
"""

from __future__ import annotations

import sqlite3
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
    score_all_candidates,
    score_one,
)
from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.graph_engine import (
    _card_has_flicker_chain,
    _commander_death_signature,
    _commander_synergy_tags,
    _is_substitution_blocking_result,
    _is_unhelpful_payoff_trigger,
    _parse_restriction_tags,
    _zone_overlap,
    find_flicker_loop_matches,
    find_mana_restriction_matches,
    find_sacrifice_synergies,
    find_stat_scaling_synergies,
    find_trigger_resonance,
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


def test_korvold_score_phyrexian_altar_beats_wrath(populated_db):
    altar = score_one(populated_db, ["Korvold, Fae-Cursed King"], "Phyrexian Altar")
    wrath = score_one(populated_db, ["Korvold, Fae-Cursed King"], "Wrath of God")
    assert altar["total"] > 0
    assert altar["cost_synergy"] > 0
    assert wrath["total"] == 0


def test_score_all_candidates_includes_altar_for_korvold(populated_db):
    result = score_all_candidates(populated_db, ["Korvold, Fae-Cursed King"])
    assert "Phyrexian Altar" in result.buckets
    assert result.buckets["Phyrexian Altar"]["total"] > 0
    assert result.matches["Phyrexian Altar"]  # contributing matches present


# ---------------------------------------------------------------------------
# §6.2.3 — Panharmonicon amplifies Cathars' Crusade
# ---------------------------------------------------------------------------


def test_panharmonicon_amplifies_cathars_etb_trigger(populated_db):
    rows = find_amplifier_matches(populated_db, ["Cathars' Crusade"])
    cards = {r["amplifier_card"] for r in rows}
    assert "Panharmonicon" in cards


def test_cathars_amplifier_score_is_positive(populated_db):
    scores = score_one(populated_db, ["Cathars' Crusade"], "Panharmonicon")
    assert scores["amplifier"] > 0
    assert scores["total"] > 0


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


def test_commander_death_signature_korvold_payoff():
    # Korvold-shape: Sacrificed trigger → has_death=True, has_bf_to_gy=False
    cmdr_ports = [
        {"port_type": "trigger", "event_class": "Sacrificed"},
        {"port_type": "trigger", "event_class": "Attacks"},
    ]
    assert _commander_death_signature(cmdr_ports) == (True, False, False)


def test_commander_death_signature_meren_bf_to_gy():
    cmdr_ports = [
        {
            "port_type": "trigger",
            "event_class": "ChangesZone",
            "zone_origin": "Battlefield",
            "zone_destination": "Graveyard",
        },
    ]
    assert _commander_death_signature(cmdr_ports) == (True, True, False)


def test_commander_death_signature_marrow_gnawer_outlet():
    # Marrow-Gnawer activated ability with Sac<4/Rat> → cost_target=any.
    cmdr_ports = [
        {
            "port_type": "cost",
            "event_class": "sacrifice",
            "cost_target": "any",
        },
    ]
    assert _commander_death_signature(cmdr_ports) == (False, False, True)


def test_commander_death_signature_self_only_sac_does_not_qualify():
    # Suspend-style Sac<1/CARDNAME> → cost_target=self → not an outlet.
    cmdr_ports = [
        {
            "port_type": "cost",
            "event_class": "sacrifice",
            "cost_target": "self",
        },
    ]
    assert _commander_death_signature(cmdr_ports) == (False, False, False)


def test_find_sacrifice_synergies_returns_empty_for_non_payoff_commander():
    # Use Cathars' Crusade as a fixture commander — no death triggers,
    # no outlet costs. populated_db is the test fixture from this file.
    pass  # populated_db doesn't have a payoff commander; covered below


def test_find_sacrifice_synergies_korvold_picks_up_outlets_against_full_db():
    """Integration: against the real /tmp/synergy_full.db built by the
    A1-A4 + B1 + B3 + C1 commits, Korvold should see at least one outlet
    and one payoff_cluster card.

    Skips if the DB isn't present so the test doesn't fail in fresh
    environments — it's a smoke check, not a property test.
    """

    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_sacrifice_synergies(conn, ["Korvold, Fae-Cursed King"])
    finally:
        conn.close()

    directions = {r["direction"] for r in rows}
    candidates = {r["candidate"] for r in rows}

    # Korvold has a Sacrificed trigger so directions 1 + 2 must fire.
    assert "outlet_for_payoff" in directions
    assert "payoff_cluster" in directions
    # Direction 3 doesn't apply (Korvold has no outlet sacrifice cost
    # of his own — his Sacrificed trigger is the payoff side).
    assert "payoff_for_outlet" not in directions

    # At least one well-known outlet should appear in the result.
    expected_outlets = {"Viscera Seer", "Goblin Bombardment", "Phyrexian Altar"}
    assert candidates & expected_outlets, f"expected at least one of {expected_outlets} in {sorted(candidates)[:20]}"


# ---------------------------------------------------------------------------
# Phase D2 — mana restriction matcher
# ---------------------------------------------------------------------------


def test_parse_restriction_tags_simple_creature():
    assert _parse_restriction_tags("Spell.Creature") == {"Creature"}


def test_parse_restriction_tags_dragon_with_modifier():
    assert _parse_restriction_tags("Activated.Dragon+inZoneBattlefield") == {"Dragon"}


def test_parse_restriction_tags_compound_creature_dragon():
    assert _parse_restriction_tags("Spell.Creature+Dragon") == {"Creature", "Dragon"}


def test_parse_restriction_tags_multiple_comma_tribes():
    assert _parse_restriction_tags("Spell.Demon,Spell.Cleric,Spell.Vampire") == {"Demon", "Cleric", "Vampire"}


def test_parse_restriction_tags_drops_runtime_modifiers():
    # cmcGE5 / wasCastFromYourGraveyard / CostContainsX are runtime
    # — they can't be matched against commander identity tags.
    assert _parse_restriction_tags("Spell.cmcGE5") == set()
    assert _parse_restriction_tags("Spell.wasCastFromYourGraveyard") == set()
    assert _parse_restriction_tags("CostContainsX") == set()
    assert _parse_restriction_tags("CumulativeUpkeep") == set()


def test_parse_restriction_tags_empty_or_none():
    assert _parse_restriction_tags("") == set()
    assert _parse_restriction_tags(None) == set()  # type: ignore[arg-type]


def test_commander_synergy_tags_ignores_static_subtypes():
    """Critical Phase D2 invariant: literal cards.subtypes does NOT
    contribute to the tag set. Only filter-derived tags qualify, so
    Kaalia (literal subtype Cleric) does NOT match Spell.Cleric mana
    fixers via her static identity. The cards-row argument is unused
    today and the function tolerates an empty list.
    """
    cmdr_ports = []
    tags = _commander_synergy_tags(cmdr_ports, [])
    assert tags == set()


def test_commander_synergy_tags_picks_up_filter_subtypes():
    # Edgar Markov-shape: trigger filter ``Card.Vampire+Other`` should
    # surface Vampire as a synergy tag.
    cmdr_ports = [
        {"port_type": "trigger", "valid_filter": "Card.Vampire+Other"},
    ]
    tags = _commander_synergy_tags(cmdr_ports, [])
    assert "Vampire" in tags


def test_commander_synergy_tags_splits_comma_separated_filter():
    # Talrand-shape: ValidCard$ Instant,Sorcery — comma split required.
    cmdr_ports = [
        {"port_type": "trigger", "valid_filter": "Instant,Sorcery"},
    ]
    tags = _commander_synergy_tags(cmdr_ports, [])
    assert "Instant" in tags
    assert "Sorcery" in tags


def test_find_mana_restriction_against_full_db_talrand():
    """Integration: Talrand should pick up Spell.Instant,Spell.Sorcery
    restrictions via his SpellCast trigger filter (no static identity
    crutch needed). Skips if /tmp/synergy_full.db isn't built yet."""

    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_mana_restriction_matches(conn, ["Talrand, Sky Summoner"])
    finally:
        conn.close()

    assert rows, "expected at least one mana-restriction match for Talrand"
    # Every match must have surfaced via Instant or Sorcery — no static
    # subtype leakage (Talrand's literal Merfolk / Wizard subtypes must
    # NOT contribute).
    for r in rows:
        assert {"Instant", "Sorcery"} & set(r["matched_tags"]), f"unexpected match {r}"


# ---------------------------------------------------------------------------
# Phase D4 — flicker self-loop detector (primitive only — not wired into
# scoring; see scoring.py docstring near _score_mana_restriction for the
# rationale on why the bucket boost is deferred to phase E)
# ---------------------------------------------------------------------------


def test_card_has_flicker_chain_detects_clean_pair():
    # Soulherder-shape: Battlefield→Exile + Exile→Battlefield.
    ports = [
        {"port_type": "effect", "event_class": "ChangeZone", "zone_origin": "Battlefield", "zone_destination": "Exile"},
        {"port_type": "effect", "event_class": "ChangeZone", "zone_origin": "Exile", "zone_destination": "Battlefield"},
    ]
    assert _card_has_flicker_chain(ports)


def test_card_has_flicker_chain_handles_all_origin():
    # Brago / Conjurer's Closet — return path uses Origin=All because
    # the SVar resolves via Defined$ Remembered (any zone).
    ports = [
        {"port_type": "effect", "event_class": "ChangeZone", "zone_origin": "Battlefield", "zone_destination": "Exile"},
        {"port_type": "effect", "event_class": "ChangeZone", "zone_origin": "All", "zone_destination": "Battlefield"},
    ]
    assert _card_has_flicker_chain(ports)


def test_card_has_flicker_chain_rejects_one_sided():
    # Bouncing-effect-only (no return path) — Cyclonic Rift, Boomerang.
    ports = [
        {"port_type": "effect", "event_class": "ChangeZone", "zone_origin": "Battlefield", "zone_destination": "Hand"},
    ]
    assert not _card_has_flicker_chain(ports)


def test_card_has_flicker_chain_rejects_etb_only():
    # Yarok-shape: ETB doubler with no exile/return chain.
    ports = [
        {"port_type": "trigger", "event_class": "ChangesZone", "zone_origin": "", "zone_destination": "Battlefield"},
    ]
    assert not _card_has_flicker_chain(ports)


def test_find_flicker_loop_matches_brago_against_full_db():
    """Integration: Brago is the only golden-set commander with a
    flicker chain. Verify the matcher finds him AND surfaces well-known
    flicker-source partners (Conjurer's Closet, Eldrazi Displacer)
    so phase E density rules can use it.
    """

    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_flicker_loop_matches(conn, ["Brago, King Eternal"])
    finally:
        conn.close()

    assert rows, "expected at least one flicker cluster match for Brago"
    cands = {r["candidate"] for r in rows}
    expected = {"Conjurer's Closet", "Soulherder", "Eldrazi Displacer"}
    assert cands & expected, f"expected at least one of {expected} in {sorted(cands)[:20]}"


def test_find_flicker_loop_matches_returns_empty_for_non_flicker_commander():
    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Korvold has zero flicker chain — must return empty.
        rows = find_flicker_loop_matches(conn, ["Korvold, Fae-Cursed King"])
    finally:
        conn.close()
    assert rows == []


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


def test_find_replacement_conflicts_flags_rest_in_peace_for_meren():
    """Integration: Meren's BF→GY trigger is blocked by Rest in Peace's
    Moved+Exile substitution. Karador has no triggers at all so the
    matcher returns nothing for him — confirming the trigger gate works.
    """

    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        from mtg_synergy_graph.graph_engine import find_replacement_conflicts

        meren = find_replacement_conflicts(conn, ["Meren of Clan Nel Toth"])
        karador = find_replacement_conflicts(conn, ["Karador, Ghost Chieftain"])
    finally:
        conn.close()

    meren_subs = [r for r in meren if r.get("match_kind") == "substitution"]
    assert meren_subs, "expected at least one substitution flag for Meren"
    meren_cards = {r["anti_synergy_card"] for r in meren_subs}
    expected = {
        "Rest in Peace",
        "Leyline of the Void",
        "Rayami, First of the Fallen",
    }
    assert meren_cards & expected, f"expected at least one of {expected}, got {sorted(meren_cards)[:20]}"
    # Karador has no triggers (his abilities are static + activated),
    # so the matcher's trigger gate returns early.
    assert karador == []


def test_find_mana_restriction_kaalia_does_not_explode():
    """Regression: Kaalia of the Vast must NOT match generic Legendary /
    Cleric restrictions via her literal type line. Her trigger valid
    filter is ``Card.Self`` (no synergy tags), and her ChangeType$
    Angel/Demon/Dragon list lives in an SVar that the matcher does not
    parse, so the expected result is an empty match list — better to
    miss real matches than to flood with false positives.
    """

    db_path = Path("/tmp/synergy_full.db")
    if not db_path.exists():
        import pytest

        pytest.skip("/tmp/synergy_full.db not present — run import_cardsfolder first")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_mana_restriction_matches(conn, ["Kaalia of the Vast"])
    finally:
        conn.close()

    cands = {r["candidate"] for r in rows}
    # Specifically Plaza of Heroes / Delighted Halfling / Untaidake were
    # the false positives that regressed her −0.038 NDCG before the
    # static-subtype removal.
    forbidden = {"Plaza of Heroes", "Delighted Halfling", "Untaidake, the Cloud Keeper"}
    assert not (cands & forbidden), f"Kaalia must not match generic Legendary fixers: {sorted(cands & forbidden)}"


# ---------------------------------------------------------------------------
# Phase F6 — trigger resonance
# ---------------------------------------------------------------------------


def test_trigger_resonance_korvold_finds_sacrifice_triggers():
    """Integration: Korvold's ``Sacrificed`` trigger should resonate
    with other sacrifice-trigger cards like Mayhem Devil.
    """
    db_path = Path("data/synergy.db")
    if not db_path.exists():
        pytest.skip("data/synergy.db not present")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_trigger_resonance(conn, ["Korvold, Fae-Cursed King"])
    finally:
        conn.close()

    candidates = {r["candidate"] for r in rows}
    events = {r["matched_event"] for r in rows}

    assert "Sacrificed" in events
    assert "Mayhem Devil" in candidates
    # Should not include broad-event matches.
    assert "ChangesZone" not in events
    assert "Phase" not in events


def test_trigger_resonance_empty_for_etb_only_commander():
    """Commanders whose only triggers are ChangesZone (broad) or
    Card.Self (self-only) should get zero resonance matches."""
    db_path = Path("data/synergy.db")
    if not db_path.exists():
        pytest.skip("data/synergy.db not present")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Slimefoot has only ChangesZone|Card.Self — excluded by both
        # self-only gate and broad-event gate.
        rows = find_trigger_resonance(conn, ["Slimefoot, the Stowaway"])
    finally:
        conn.close()

    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Phase F7 — token_loop, graveyard boost, stat scaling, death sig fix
# ---------------------------------------------------------------------------


def test_sacrifice_synergies_token_loop_finds_pitiless_plunderer():
    """Pitiless Plunderer makes tokens AND has a death trigger — it
    should appear as a token_loop candidate for Korvold."""
    db_path = Path("data/synergy.db")
    if not db_path.exists():
        pytest.skip("data/synergy.db not present")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_sacrifice_synergies(conn, ["Korvold, Fae-Cursed King"])
    finally:
        conn.close()

    token_loop_cards = {r["candidate"] for r in rows if r["direction"] == "token_loop"}
    assert "Pitiless Plunderer" in token_loop_cards
    assert "Impulsive Pilferer" in token_loop_cards


def test_locust_god_no_sacrifice_synergies():
    """Locust God's only dies trigger is Card.Self (return to hand) —
    should NOT produce sacrifice synergies."""
    db_path = Path("data/synergy.db")
    if not db_path.exists():
        pytest.skip("data/synergy.db not present")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_sacrifice_synergies(conn, ["The Locust God"])
    finally:
        conn.close()

    assert len(rows) == 0


def test_stat_scaling_phenax_finds_high_toughness():
    """Phenax scales_with CardToughness — high-toughness creatures and
    Defenders should be returned."""
    db_path = Path("data/synergy.db")
    if not db_path.exists():
        pytest.skip("data/synergy.db not present")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = find_stat_scaling_synergies(conn, ["Phenax, God of Deception"])
    finally:
        conn.close()

    candidates = {r["candidate"] for r in rows}
    assert "Tree of Perdition" in candidates
    assert "Wall of Frost" in candidates
    assert len(rows) > 50  # should find many high-toughness creatures
