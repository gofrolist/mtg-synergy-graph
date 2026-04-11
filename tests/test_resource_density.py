"""Resource-density bucket tests (SPEC §7.5).

The resource-density layer captures a mechanical pattern that the trigger /
effect port joins miss: a commander whose **cost** consumes a card-typed
resource (e.g. ``tapXType<1/Artifact>`` for Urza, ``Sac<1/Creature>`` for a
sacrifice outlet) wants the deck stuffed with cards of that type — even when
those cards have no mechanical port-level interaction with the commander.

Regression for the Urza investigation (2026-04-07): before this layer landed,
``Sol Ring`` scored only its staple bonus (12) against Urza, Lord High
Artificer, and Mana Vault / SDT / Mishra's Bauble / Mox Opal scored 0. The
engine had no notion of "commander cares about cards of type X as a
*resource*", only "commander's trigger event matches card's effect event".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtg_synergy_graph.db import open_db
from mtg_synergy_graph.importer import import_cards_folder
from mtg_synergy_graph.scoring import score_all_candidates

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def urza_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("resdense") / "synergy.db"
    conn = open_db(db_path)
    import_cards_folder(conn, FIXTURES, scryfall_db=None)
    yield conn
    conn.close()


def test_urza_lifts_artifacts_via_resource_density(urza_db):
    """Urza taps untapped artifacts → every legal artifact gains a
    resource-density bonus that beats the bare-staple-only baseline."""
    result = score_all_candidates(urza_db, ["Urza, Lord High Artificer"])
    sol_ring = result.buckets.get("Sol Ring")
    assert sol_ring is not None, "Sol Ring should appear in the candidate pool"
    assert sol_ring["resource_density"] > 0, (
        "Sol Ring is an artifact and Urza's tap-cost names Artifact as a "
        "resource — the bucket must fire."
    )


def test_resource_density_does_not_fire_on_unrelated_commanders(urza_db):
    """Cathars' Crusade has no cost ports of its own and is not a resource
    consumer; the bucket should be 0 for every candidate when scored against
    it."""
    result = score_all_candidates(urza_db, ["Cathars' Crusade"])
    for buckets in result.buckets.values():
        assert buckets["resource_density"] == 0


def test_resource_density_bucket_in_total(urza_db):
    """The bucket must be summed into the final ``total`` so it influences
    ranking — easy to forget when adding a new bucket."""
    result = score_all_candidates(urza_db, ["Urza, Lord High Artificer"])
    sol_ring = result.buckets["Sol Ring"]
    expected = sum(
        sol_ring[k]
        for k in sol_ring
        if k != "total"
    )
    assert sol_ring["total"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Trigger anchors — commanders that REWARD a resource action via a trigger,
# not just commanders that PAY for it via a cost. The classic example is
# Korvold (``trigger Sacrificed | Permanent``) — he doesn't sac anything
# himself, but the deck wants permanents to feed his sac outlets.
# ---------------------------------------------------------------------------


def test_parse_valid_filter_extracts_head_type():
    from mtg_synergy_graph.scoring import _parse_valid_filter_anchor

    assert _parse_valid_filter_anchor("Creature") == ["Creature"]
    assert _parse_valid_filter_anchor("Creature.YouCtrl") == ["Creature"]
    assert _parse_valid_filter_anchor("Creature.Other+YouCtrl") == ["Creature"]
    assert _parse_valid_filter_anchor("Card.Self") == []
    assert _parse_valid_filter_anchor("Card.AttachedBy") == []


def test_parse_valid_filter_expands_permanent_to_creature_and_artifact():
    from mtg_synergy_graph.scoring import _parse_valid_filter_anchor

    # ``Permanent`` matches every non-spell — too broad to be a useful
    # anchor on its own. We expand it to the two realistic high-density
    # sac fodder types so Korvold-class commanders pick up creature and
    # artifact bonuses without flooding lands and enchantments.
    result = _parse_valid_filter_anchor("Permanent")
    assert "Creature" in result
    assert "Artifact" in result

    # The qualified form behaves the same way.
    result = _parse_valid_filter_anchor("Permanent.YouCtrl+Other")
    assert "Creature" in result
    assert "Artifact" in result


def test_parse_valid_filter_handles_comma_separated_alternatives():
    from mtg_synergy_graph.scoring import _parse_valid_filter_anchor

    # ``Card.Self,Creature.Other+YouCtrl`` is "this card OR another
    # creature you control" — the second alternative is the real anchor.
    assert _parse_valid_filter_anchor("Card.Self,Creature.Other+YouCtrl") == [
        "Creature"
    ]


# ---------------------------------------------------------------------------
# Token producer mirror — candidates that CREATE the anchor type as tokens
# (Bitterblossom → faerie creature tokens for a sac-creature commander)
# count too, at half weight.
# ---------------------------------------------------------------------------


def test_token_script_kind_classifies_creature_vs_artifact():
    from mtg_synergy_graph.scoring import _token_script_kind

    # Standard creature tokens have explicit power/toughness in the name.
    assert _token_script_kind("b_1_1_faerie_rogue_flying") == "Creature"
    assert _token_script_kind("g_3_3_beast") == "Creature"
    assert _token_script_kind("b_x_x_horror") == "Creature"

    # Artifact subtype tokens (treasure / food / clue / blood / gold).
    assert _token_script_kind("c_a_treasure_sac") == "Artifact"
    assert _token_script_kind("c_a_food_sac") == "Artifact"

    # Empty / nonsense → None.
    assert _token_script_kind("") is None
    assert _token_script_kind(None) is None


# ---------------------------------------------------------------------------
# End-to-end smoke test against the **full** Forge DB. These exercise the
# Korvold and Yawgmoth flows that the synthetic fixture set can't reach.
# Auto-skipped when /tmp/synergy_full.db is missing so unit-test runs in
# CI / containers without the full import are still green.
# ---------------------------------------------------------------------------

import os  # noqa: E402

_HAS_FULL_DB = Path(
    os.environ.get("MTG_SYNERGY_GRAPH_FULL_DB", "/tmp/synergy_full.db")
).exists()


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_korvold_picks_up_creature_and_artifact_anchor(full_engine):
    """Korvold has ``trigger Sacrificed | Permanent`` — Permanent must
    expand to {Creature, Artifact} so cheap fodder permanents get the
    resource_density bonus."""
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Sakura-Tribe Elder")
    assert rec.scores["resource_density"] > 0, (
        "Sakura-Tribe Elder is a creature and Korvold rewards Permanent "
        "sacrifices — the bucket must fire."
    )
    rec_art = full_engine.score_one("Korvold, Fae-Cursed King", "Wayfarer's Bauble")
    assert rec_art.scores["resource_density"] > 0, (
        "Wayfarer's Bauble is an artifact and Korvold rewards Permanent "
        "sacrifices — the artifact half of the Permanent expansion must fire."
    )


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_korvold_does_not_anchor_lands_via_permanent(full_engine):
    """Permanent expansion is intentionally limited to {Creature, Artifact}
    — basic lands and enchantments must NOT pick up the bonus, otherwise
    the bucket floods every Korvold-class commander's pool."""
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Forest")
    assert rec.scores["resource_density"] == 0
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Rhystic Study")
    assert rec.scores["resource_density"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_creature_anchor_caps_cmc_at_three(full_engine):
    """The Creature anchor is broad (matches half the legal pool); we cap
    it by cmc so sacrifice / death-trigger commanders only pick up *cheap*
    sac fodder, not random 5-drops. Without this cap the bucket flooded
    every Korvold-class commander's top-50 with mid-range creatures and
    pushed genuine staples out of the page."""
    # Cheap fodder — should fire.
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Sakura-Tribe Elder")
    assert rec.scores["resource_density"] > 0
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Bloodghast")
    assert rec.scores["resource_density"] > 0

    # Mid-range creature (cmc 5) — should NOT fire on the Creature anchor
    # half. Korvold also has the Artifact half from Permanent expansion,
    # so non-artifact 5-drops with no other anchor must drop to 0.
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Bighorner Rancher")
    assert rec.scores["resource_density"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_artifact_anchor_does_not_cap_cmc_for_non_consuming_channel(full_engine):
    """Urza's tap-artifact cost does *not* consume the artifact (taps
    are reversible at end of turn), so his Artifact anchor stays
    uncapped — Mystic Forge (cmc 5) and Thran Dynamo-class value
    artifacts must still score."""
    rec = full_engine.score_one("Urza, Lord High Artificer", "Mystic Forge")
    assert rec.scores["resource_density"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_korvold_consuming_channel_caps_artifact_anchor(full_engine):
    """Korvold's ``Sacrificed | Permanent`` trigger IS a consuming channel
    — sacrificed artifacts are physically destroyed, so the Artifact
    half of the Permanent expansion inherits the cmc cap. Before this
    change Korvold's top-50 was flooded with random 5+ cmc artifacts
    scoring the flat Artifact weight; after, only cheap fodder scores."""
    # Cheap sac-fodder artifact — must still fire.
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Wayfarer's Bauble")
    assert rec.scores["resource_density"] > 0
    # Mid-range value artifact — must NOT fire now that the Artifact
    # half is capped at cmc=3.
    rec = full_engine.score_one("Korvold, Fae-Cursed King", "Mystic Forge")
    assert rec.scores["resource_density"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_anchor_cmc_gradient_lifts_cheaper_fodder(full_engine):
    """Within a capped anchor, cheaper cards strictly beat more
    expensive ones: the direct-match weight carries a cmc gradient of
    ``max(0, cap - cmc)``, so a cmc=1 creature scores exactly 2 more
    resource_density than a cmc=3 creature against Meren's Creature
    anchor (cap=3).

    Using Elvish Mystic (cmc=1, no recursion) and Grizzled Outcasts
    (cmc=4 — wait, needs cmc 3)… actually we use Elvish Mystic vs a
    vanilla 3-drop creature: Yavimaya Elder (cmc=3, no recursion).
    """
    cheap = full_engine.score_one("Meren of Clan Nel Toth", "Elvish Mystic")
    mid = full_engine.score_one("Meren of Clan Nel Toth", "Yavimaya Elder")
    assert cheap.scores["resource_density"] > 0
    assert mid.scores["resource_density"] > 0
    # cheap = base(4) + gradient(2) = 6; mid = base(4) + gradient(0) = 4.
    # Strict delta of 2 is the gradient difference.
    assert (
        cheap.scores["resource_density"]
        - mid.scores["resource_density"]
        == 2
    )


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_recursion_keyword_bonus_lifts_undying_creatures(full_engine):
    """Keyworded recursion creatures (Undying, Persist, Unearth, ...)
    are top-tier fodder for sacrifice / death-trigger commanders. They
    must strictly beat a vanilla creature of the same cmc on the
    resource_density bucket by exactly the RECURSION_ANCHOR_BONUS
    delta."""
    from mtg_synergy_graph.scoring import RECURSION_ANCHOR_BONUS

    young_wolf = full_engine.score_one("Meren of Clan Nel Toth", "Young Wolf")
    # Young Wolf is cmc=1, Undying → base(4) + gradient(3-1=2) +
    # recursion(4) = 10.
    assert young_wolf.scores["resource_density"] >= 4 + 2 + RECURSION_ANCHOR_BONUS

    # Vanilla 1-cmc creature (Elvish Mystic has no recursion keyword
    # and no self-return effect) → base(4) + gradient(2) = 6.
    vanilla = full_engine.score_one("Meren of Clan Nel Toth", "Elvish Mystic")
    assert vanilla.scores["resource_density"] > 0, (
        "Elvish Mystic is a cmc=1 creature and must still match the "
        "Creature anchor; if it doesn't, the test has drifted."
    )
    assert (
        young_wolf.scores["resource_density"]
        - vanilla.scores["resource_density"]
        >= RECURSION_ANCHOR_BONUS
    )


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_effect_based_recursion_lifts_bloodghast(full_engine):
    """Bloodghast / Reassembling Skeleton / Nether Traitor return
    themselves from the graveyard via triggered or activated abilities
    rather than via keywords. The effect-based detection
    (``ChangeZone | Graveyard → Battlefield`` ports) must still
    catch them."""
    bloodghast = full_engine.score_one(
        "Meren of Clan Nel Toth", "Bloodghast"
    )
    reassembling = full_engine.score_one(
        "Meren of Clan Nel Toth", "Reassembling Skeleton"
    )
    # Bloodghast and Reassembling Skeleton are both cmc=2 — base
    # Creature weight (4) + gradient (3-2=1) + recursion bonus (4) = 9.
    assert bloodghast.scores["resource_density"] >= 9
    assert reassembling.scores["resource_density"] >= 9


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_yawgmoth_lifts_token_producer_bitterblossom(full_engine):
    """Yawgmoth's cost is ``Sac<1/Creature.Other>`` → Creature anchor.
    Bitterblossom is an Enchantment but creates Faerie creature tokens
    every turn — the producer mirror must catch it."""
    rec = full_engine.score_one("Yawgmoth, Thran Physician", "Bitterblossom")
    assert rec.scores["resource_density"] > 0
    # Producer weight is half the direct weight, so Bitterblossom should
    # have *strictly less* than a real creature with the same anchor.
    rec_creature = full_engine.score_one(
        "Yawgmoth, Thran Physician", "Reassembling Skeleton"
    )
    assert (
        rec.scores["resource_density"]
        < rec_creature.scores["resource_density"]
    )


# ---------------------------------------------------------------------------
# Scaling-bucket regression — the matcher used to join on the literal
# "Creature" type, which made every creature commander cross-link to every
# Count$Valid Creature.YouCtrl scaling card. Net effect on Meren: random
# scaling creatures (Phyrexian Dreadnought, Mine Worker, Tower Worker)
# floored her top-10 above genuine staples. The fix restricts the matcher
# to subtypes (Goblin, Zombie, ...) and drops self-referential ``named*``
# pin filters.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_scaling_bucket_does_not_flood_meren_with_random_creatures(full_engine):
    """Phyrexian Dreadnought scales on ``Count$Valid Creature.YouCtrl`` —
    that's a generic creature-density card, not specific to Meren. Before
    the fix it earned +12 against every creature commander.
    """
    rec = full_engine.score_one("Meren of Clan Nel Toth", "Phyrexian Dreadnought")
    assert rec.scores["scaling"] == 0
    rec = full_engine.score_one("Meren of Clan Nel Toth", "Mine Worker")
    assert rec.scores["scaling"] == 0
    rec = full_engine.score_one("Meren of Clan Nel Toth", "Tower Worker")
    assert rec.scores["scaling"] == 0


# ---------------------------------------------------------------------------
# Trigger-feeder self-trigger regression — Urza's ``trigger ChangesZone |
# Card.Self`` is the commander's own ETB. It can only fire on Urza himself
# entering the battlefield, so it should not cross-link to every other card
# in the format with a Token / ChangeZone effect. The previous matcher
# generated 7833 spurious matches against Urza, flooding his top-50 with
# token-creator artifacts (Druidic Satchel, Soul Separator, Fishing Gear, ...)
# and pushing real Urza staples (Sol Ring, Mana Vault, Mishra's Bauble) out
# of the page.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_self_only_triggers_do_not_generate_feeder_matches(full_engine):
    """Druidic Satchel and Soul Separator have no real Urza synergy — they
    were only ranked top-10 because Urza's self-ETB trigger cross-linked to
    every other Token/ChangeZone effect."""
    rec = full_engine.score_one("Urza, Lord High Artificer", "Druidic Satchel")
    assert rec.scores["port_match"] == 0
    rec = full_engine.score_one("Urza, Lord High Artificer", "Soul Separator")
    assert rec.scores["port_match"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_real_other_etb_trigger_still_generates_cross_links(full_engine):
    """Aunt May has ``trigger ChangesZone | Creature.Other+YouCtrl`` — a
    real "another creature ETBs" trigger. The fix must NOT skip this kind
    of trigger; it should still cross-link to every Token / ChangeZone
    effect, otherwise we'd kill all the legitimate ETB-payoff matches."""
    rec = full_engine.score_one("Aunt May", "Bitterblossom")
    # Bitterblossom creates Faerie creature tokens; Aunt May rewards
    # other-creature ETBs → must score above zero on port_match.
    assert rec.scores["port_match"] > 0


# ---------------------------------------------------------------------------
# Replacement-effect resonance — Doubling Season / Hardened Scales /
# Primal Vigor are ``port_type=replacement`` rows that double counters or
# tokens. They had no matcher prior to §7.7. The new layer joins commander
# effects (e.g. Atraxa's ``effect Proliferate``, Krenko's ``effect Token``)
# to candidate replacements via a curated event mapping.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_doubling_season_scores_for_atraxa_proliferate(full_engine):
    """Atraxa's Proliferate adds counters → Doubling Season's
    ``replacement AddCounter | DoubleCounters`` doubles the result.
    Real EDHREC #1 Atraxa pick. Must score above zero."""
    rec = full_engine.score_one("Atraxa, Praetors' Voice", "Doubling Season")
    assert rec.scores["replacement_resonance"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_hardened_scales_scores_for_atraxa(full_engine):
    rec = full_engine.score_one("Atraxa, Praetors' Voice", "Hardened Scales")
    assert rec.scores["replacement_resonance"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_doubling_season_also_scores_for_token_commander(full_engine):
    """Doubling Season ALSO has ``replacement CreateToken | DoubleToken``,
    which is the right doubler for Krenko (effect Token)."""
    rec = full_engine.score_one("Krenko, Mob Boss", "Doubling Season")
    assert rec.scores["replacement_resonance"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_replacement_resonance_does_not_fire_on_unrelated_commander(full_engine):
    """Sol Ring is colourless mana — neither token, counter, nor anything
    else. The replacement resonance bucket must stay zero on it for a
    pure-mana commander."""
    rec = full_engine.score_one("Krenko, Mob Boss", "Sol Ring")
    assert rec.scores["replacement_resonance"] == 0


# ---------------------------------------------------------------------------
# Amplifier reverse direction — when the COMMANDER is the Panharmonicon-class
# trigger doubler (Yarok the Desecrated, Naru Meha-style copy effects),
# candidates with the targeted trigger event should pick up the amplifier
# bonus. The previous matcher only handled "commander has ETB trigger →
# candidate has Panharmonicon static"; this adds the symmetric direction.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_yarok_as_commander_amplifies_etb_creatures(full_engine):
    """Yarok has ``static Panharmonicon | ValidMode$ ChangesZone,ChangesZoneAll``
    → he doubles ETB triggers. Candidates with ETB ChangesZone triggers
    (Mulldrifter, Reflector Mage, ...) must pick up the amplifier bonus
    when Yarok is the commander."""
    rec = full_engine.score_one("Yarok, the Desecrated", "Mulldrifter")
    assert rec.scores["amplifier"] > 0
    rec = full_engine.score_one("Yarok, the Desecrated", "Reflector Mage")
    assert rec.scores["amplifier"] > 0


# ---------------------------------------------------------------------------
# ETB self-match — when a commander trigger fires on "[filter] enters",
# the candidate IS the entering card. Trigger feeders only handles the
# "candidate has an effect that produces the entering card" case (token
# producers, recurseion, etc.), missing the much more common case of the
# candidate simply being the entering card itself.
#
# Regression: Arcades the Strategist's ``trigger ChangesZone Battlefield
# Creature.withDefender+YouCtrl`` had ZERO valid feeders. His top-10 was
# random DFCs (Grist Voracious Larva, Nissa Vastwood Seer) that happened
# to have many ChangeZone effect ports, while Wall of Omens / Wall of
# Blossoms / Axebane Guardian — the actual Arcades staples — scored 0.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_arcades_etb_self_match_finds_defender_creatures(full_engine):
    """Arcades's trigger fires when ``Creature.withDefender+YouCtrl`` enters
    — Wall of Omens is a creature with defender, so its entering itself
    feeds the trigger."""
    rec = full_engine.score_one("Arcades, the Strategist", "Wall of Omens")
    assert rec.scores["port_match"] > 0
    rec = full_engine.score_one("Arcades, the Strategist", "Wall of Blossoms")
    assert rec.scores["port_match"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_aesi_etb_self_match_finds_lands(full_engine):
    """Aesi's trigger fires on ``Land.YouCtrl`` entering — every land
    candidate should match by simply being a land."""
    rec = full_engine.score_one("Aesi, Tyrant of Gyre Strait", "Cultivate")
    # Cultivate puts a land onto the battlefield via ChangeZone — its
    # produced type is Land, so the trigger feeder matches it.
    assert rec.scores["port_match"] > 0 or rec.scores["catchall"] > 0


# ---------------------------------------------------------------------------
# Deck-hint resonance — Forge devs annotate every card with
# deck_has / deck_hints / deck_needs Ability+Type+Keyword tags. The engine
# only uses the candidate→commander direction (does the candidate's hints/
# needs match the commander's static identity?). The reverse direction —
# commander's has/hints find candidates with matching has — was missing.
# Atraxa's "has Proliferate" → every card with "has Proliferate" should
# be promoted as a curated synergy match.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_atraxa_lifts_proliferate_cards_via_deck_has_resonance(full_engine):
    """Atraxa's deck_has = ['Proliferate']. Inexorable Tide / Karn's
    Bastion / Contagion Engine / Evolution Sage are annotated by Forge
    as Proliferate sources via their own deck_has Ability list, so the
    bidirectional has↔has match must fire on them."""
    for name in [
        "Inexorable Tide",
        "Karn's Bastion",
        "Contagion Engine",
        "Evolution Sage",
    ]:
        rec = full_engine.score_one("Atraxa, Praetors' Voice", name)
        assert rec.scores["deck_hints"] > 0, name


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_slimefoot_lifts_token_producers_via_deck_has(full_engine):
    """Slimefoot's deck_has = ['Token']. Hornet Queen is annotated by
    Forge as a Token producer (deck_has=Token)."""
    rec = full_engine.score_one("Slimefoot, the Stowaway", "Hornet Queen")
    assert rec.scores["deck_hints"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_glarb_lifts_surveil_cards_via_deck_has(full_engine):
    """Glarb has Surveil. Other Surveil-annotated cards (Barrier of
    Bones, Basilica Stalker, Boulderborn Dragon) should pick up the
    has_has resonance bonus."""
    rec = full_engine.score_one("Glarb, Calamity's Augur", "Barrier of Bones")
    assert rec.scores["deck_hints"] > 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_arcades_etb_self_match_does_not_fire_on_non_defenders(full_engine):
    """A creature without defender (Bitterblossom's Faerie tokens, a
    random vanilla creature) must NOT pick up the ETB self-match for
    Arcades — only defenders enter to feed his trigger."""
    rec = full_engine.score_one("Arcades, the Strategist", "Llanowar Elves")
    # Llanowar Elves has no defender → ETB self-match must not fire.
    # The total score should be at most the staple/colour-identity baseline.
    assert rec.scores["port_match"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_yarok_amplifier_does_not_fire_on_non_trigger_cards(full_engine):
    """Sol Ring has no triggers — Yarok's amplifier static cannot interact
    with it. Bucket must be zero."""
    rec = full_engine.score_one("Yarok, the Desecrated", "Sol Ring")
    assert rec.scores["amplifier"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_atraxa_proliferate_payoffs_score_via_effect_resonance(full_engine):
    """Atraxa has ``effect Proliferate`` on her end-step trigger but no
    other matcher reaches her best deckmates (Inexorable Tide,
    Evolution Sage, Karn's Bastion, Contagion Engine). The effect_resonance
    layer must lift these into her ranking."""
    for name in ["Inexorable Tide", "Evolution Sage", "Karn's Bastion", "Contagion Engine"]:
        rec = full_engine.score_one("Atraxa, Praetors' Voice", name)
        assert rec.scores["effect_resonance"] > 0, name


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_lord_matches_require_commander_to_mention_tribe(full_engine):
    """Atraxa is a Phyrexian Angel by *type* but has no Phyrexian / Angel
    references in her own port filters — she's not a tribal commander,
    just a creature that happens to share subtypes with some lord cards.
    The lord matcher must NOT fire on her, otherwise random Phyrexian /
    Angel lords (Grafted Butcher, Lyra Dawnbringer, ...) flood her top-10
    above her real proliferate / counter staples."""
    rec = full_engine.score_one("Atraxa, Praetors' Voice", "Grafted Butcher")
    assert rec.scores["lord"] == 0
    rec = full_engine.score_one("Atraxa, Praetors' Voice", "Lyra Dawnbringer")
    assert rec.scores["lord"] == 0


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_lord_matches_still_fire_for_real_tribal_commanders(full_engine):
    """Krenko mentions Goblin in his scaling port; Edgar Markov mentions
    Vampire in his trigger filter. The lord matcher must still fire on
    these real tribal commanders, otherwise we'd kill every tribal
    archetype on the way to fixing Atraxa."""
    rec = full_engine.score_one("Krenko, Mob Boss", "Goblin Chieftain")
    assert rec.scores["lord"] > 0
    rec = full_engine.score_one("Edgar Markov", "Vampire Nocturnus")
    assert rec.scores["lord"] > 0


def test_trigger_only_matches_self_helper():
    from mtg_synergy_graph.graph_engine import _trigger_only_matches_self

    assert _trigger_only_matches_self("Card.Self") is True
    assert _trigger_only_matches_self("Card.Self+kicked") is True
    assert _trigger_only_matches_self("Card.Self+IsSaddled") is True
    # Comma-alternatives are real other-card matches → do NOT skip.
    assert _trigger_only_matches_self("Card.Self,Creature.Other+YouCtrl") is False
    assert _trigger_only_matches_self("Card.Self,Ally.Other+YouCtrl") is False
    # Bare other-card filters → do NOT skip.
    assert _trigger_only_matches_self("Creature.YouCtrl") is False
    assert _trigger_only_matches_self("Permanent.YouCtrl+Other") is False
    # Empty / None — caller should still allow these (catch-all triggers).
    assert _trigger_only_matches_self(None) is False
    assert _trigger_only_matches_self("") is False


@pytest.mark.skipif(not _HAS_FULL_DB, reason="full DB not available")
def test_scaling_bucket_still_fires_on_real_tribal_subtype_match(full_engine):
    """Coat of Arms scales every creature with shared subtypes — for a
    Goblin commander it's a real synergy (boosts the whole Goblin army),
    so the scaling bucket must still fire on subtype overlap."""
    # Goblin Chieftain scales with ``Count$Valid Goblin.YouCtrl``-style
    # filters via its lord static; we use it as a stand-in for any
    # subtype-anchored scaling card. Goblin Bushwhacker / Krenko's Buzzcrusher
    # are similarly subtype-anchored.
    rec = full_engine.score_one("Krenko, Mob Boss", "Goblin Piledriver")
    # Goblin Piledriver has ``Count$Valid Goblin.Other+YouCtrl`` scaling.
    # Krenko's subtypes include Goblin, so the match must fire.
    assert rec.scores["scaling"] > 0
