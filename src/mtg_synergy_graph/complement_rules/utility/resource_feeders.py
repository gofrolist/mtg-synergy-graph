"""Resource-axis feeders (graveyard fuel, life, land-bounce, creature-died, party, ETB-tapped stax)."""

from __future__ import annotations

import sqlite3

from ..core import (
    PortComplement,
    PortRow,
)
from ._shared import (
    _NUM_CARDS_RE,
    _UP_BIASED_SVAR_COMPARE_RE,
)

_CREATURE_DIED_AXIS_PREFIX = "ThisTurnEntered_Graveyard_from_Battlefield_Creature"


def _find_gy_fuel_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders whose signature ability pays by **exiling
    cards from any graveyard** — ``cost.exile_from_grave`` with
    ``cost_target='any'``.

    Aphemia (tap + exile Enchantment → 2/2 Zombie), Ashnod (exile
    Creature → draw), Araumi (tap + exile X → encore targets),
    Drivnod (exile 3 Creatures → reset trigger), Egon (exile a
    Creature → Cleric/Demigod activation), Gorex (exile X Creatures
    → +X/+X), Imoen (exile Instant/Sorcery → copy), Ishkanah (exile
    2 cards → Spider token), Kethis (exile 2 legendary → cast from
    GY), Kroxa and Kunoros, Ludevic, Osgir, Sigarda, Taigam, Tawnos,
    Ultimecia, Varina, Winter, and Baron Helmut Zemo.

    The archetype-defining payoff is **self-mill**: the more cards
    in your graveyard, the more often you can pay the cost. Commanders
    with ``cost_target='self'`` (Wilson, Symbiote Spider-Man, Tocasia,
    Venom, Morbius, Spider-Slayer, Beetle) are a different archetype
    — they escape themselves, so their reward is sacrifice outlets
    and dies triggers, not GY filling. They're excluded by the gate.

    Single tier:

    - ``gy_fuel_self_mill``: ``effect.Mill`` with ``Defined: 'You'``
      and ``NumCards >= 3`` OR a scaling SVar (``X``/``Y``/``Z``).
      ~100 cards. Aftermath Analyst, Altar of Dementia (X), Hedron
      Crab, Ashiok Nightmare Weaver, Mesmeric Orb, Sphinx's Tutelage.
      Opponent-targeting (``Opponent``/``EachPlayer``) mills are
      rejected — they don't fill YOUR graveyard. The threshold was
      tightened from 2 to 3 after audit: NumCards=2 cantrip-mills
      (Peek at Peeler / Oneirophage) flooded Osgir's top-30 with
      one-shot cantrips, pushing out her archetype artifact picks
      (-0.093 golden-set NDCG, -0.436 on non-golden Ultimecia).

    Disjoint from ``graveyard_filler`` (fires on commanders with
    trigger.ChangesZone GY→BF or effect=Play from graveyard; these
    exile_from_grave commanders typically lack those ports — the
    gap_report confirmed 0% coverage for this signature before
    the rule was added).
    """
    has_any_target_cost = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "exile_from_grave":
            continue
        if (p.get("cost_target") or "").strip() == "any":
            has_any_target_cost = True
            break
    if not has_any_target_cost:
        return []

    self_mill_set: set[str] = set()
    for row in conn.execute(
        "SELECT DISTINCT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class = 'Mill' "
        "AND raw_line LIKE ? "
        "AND raw_line NOT LIKE '%Opponent%' "
        "AND raw_line NOT LIKE '%EachPlayer%'",
        ("%'Defined': 'You'%",),
    ):
        raw = row["raw_line"] or ""
        match = _NUM_CARDS_RE.search(raw)
        if not match:
            continue
        value = match.group(1)
        if value in ("X", "Y", "Z"):
            self_mill_set.add(row["card_name"])
            continue
        try:
            if int(value) >= 3:
                self_mill_set.add(row["card_name"])
        except ValueError:
            continue

    results: list[PortComplement] = []
    for name in self_mill_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="gy_fuel_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="gy_fuel_cost",
                cand_event="gy_fuel_self_mill",
            )
        )
    return results


def _find_lifegain_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders with a ``scales_with LifeYouGainedThisTurn``
    port — their mechanic fires off life gained this turn (draw N
    cards = life gained, deal N damage = life gained, +N/+N until
    end of turn = life gained).

    Aerith, Astarion, Betor, Bre of Clan Stoutarm, Celestine, Cerise,
    Frodo (Adventurous Hobbit), Gollum, Gwaihir, Haliya, Hope Estheim,
    Lathiel, Licia, Ragost, Ratchet, Rodolf, Saint Elenda, Shanna,
    Sorin of House Markov, The Gaffer, Tivash, Will, Willowdusk.

    The axis is strictly **monotonic-positive** — every commander
    wants MORE life gained, unlike hand_size which is bidirectional.
    No rejection filter needed at the gate.

    Two deduped tiers:

    - ``lifegain_amp``: ``replacement.GainLife`` with ``ValidPlayer:
      'You'`` and an amplifying ReplaceWith (``GainDouble`` / ``GainLife``
      / ``ReplaceGain``). Rejects prevention statics (Sulfuric
      Vortex — ``'Prevent': 'True'``) and opponent-targeting
      (Tainted Remedy / Plague Drone — ``ValidPlayer: 'Opponent'``
      converts gain → lose). ~12 cards: Alhammarret's Archive,
      Rhox Faithmender, Boon Reflection, The Wind Crystal, Cleric
      Class, Honor Troll, Heron of Hope, Angel of Vitality, Leyline
      of Hope, Bilbo Birthday Celebrant, Knight of Dawn's Light,
      Phial of Galadriel.
    - ``lifegain_etb_trigger``: ``trigger.ChangesZone`` whose
      ``valid_filter`` references Creature and ``Destination:
      Battlefield``, paired with ``effect.GainLife``. ~45 cards.
      Soul Warden, Auriok Champion, Soul's Attendant, Ajani's
      Welcome, Anointer Priest, Angelic Chorus, Daxos Blessed by
      the Sun, Authority of the Consuls. These fire on every
      creature ETB (yours or opponents') so they stack life gain
      across the whole table.
    """
    has_lifegain_axis = any(
        (p.get("port_type") or "").strip() == "scales_with"
        and (p.get("event_class") or "").strip() == "LifeYouGainedThisTurn"
        for p in cmdr_ports
    )
    if not has_lifegain_axis:
        return []

    amp_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'GainLife' "
            "AND raw_line LIKE '%''ValidPlayer'': ''You''%' "
            "AND raw_line NOT LIKE '%''Prevent'': ''True''%' "
            "AND ("
            "    raw_line LIKE '%GainDouble%' "
            "    OR raw_line LIKE '%''ReplaceWith'': ''GainLife''%' "
            "    OR raw_line LIKE '%ReplaceGain%'"
            ")"
        )
    }

    etb_trigger_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT cp1.card_name "
            "FROM card_ports cp1 "
            "JOIN card_ports cp2 ON cp2.card_name = cp1.card_name "
            "WHERE cp1.port_type = 'trigger' "
            "AND cp1.event_class = 'ChangesZone' "
            "AND cp1.valid_filter LIKE '%Creature%' "
            "AND cp1.raw_line LIKE '%''Destination'': ''Battlefield''%' "
            "AND cp2.port_type = 'effect' "
            "AND cp2.event_class = 'GainLife'"
        )
    }

    tier_priority = (
        ("lifegain_amp", amp_set),
        ("lifegain_etb_trigger", etb_trigger_set),
    )
    seen: set[str] = set()
    results: list[PortComplement] = []
    for cand_event, candidates in tier_priority:
        for name in candidates:
            if name in cmdr_set or name in seen:
                continue
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="lifegain_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="lifegain_axis",
                    cand_event=cand_event,
                )
            )
    return results


def _find_life_total_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders with a ``scales_with YourLifeTotal`` port
    **AND** a strong up-biased lifegain signal.

    Target archetype: "grow with life" / "reward high life" commanders
    whose payoff scales upward. The YourLifeTotal axis alone is
    heterogeneous (Ayli uses life as an exile-power-cap query, Bane
    wants LOW life for indestructible, Cecil/Jerren use life-total
    thresholds as flip conditions — audit 2026-04-20 showed those
    cmdrs regress when fed generic lifegain peers). The up-bias gate
    narrows the rule to: Bilbo (Birthday Celebrant — GainLife
    replacement amp) and Elenda (Saint of Dusk — continuous stat
    boost conditional on ``SVarCompare: GTY`` / ``GEZ``).

    Up-bias signal (at least one of):

    - ``replacement.GainLife`` with ``ValidPlayer: 'You'`` (not
      ``Prevent: True``) — a lifegain amplifier on self.
    - ``static.Continuous`` whose raw line contains
      ``'SVarCompare': 'GT...'`` or ``'SVarCompare': 'GE...'`` (up-
      biased threshold comparing against a life-total SVar).

    Single tier:

    - ``life_total_peer``: other cards carrying ``scales_with.YourLifeTotal``
      that ALSO satisfy ``_is_positive_life_port`` (Lifelink / GainLife
      / replacement.GainLife). ~27 cards: Angel of Vitality, Blood
      Baron of Vizkopa, Divinity of Pride, Honor Troll, Path of
      Bravery, Righteous Valkyrie, Serra Ascendant, Sigarda's
      Splendor, Speaker of the Heavens, Leyline of Hope, Phial of
      Galadriel.
    """
    has_life_axis = any(
        (p.get("port_type") or "").strip() == "scales_with" and (p.get("event_class") or "").strip() == "YourLifeTotal"
        for p in cmdr_ports
    )
    if not has_life_axis:
        return []

    has_up_bias = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ec = (p.get("event_class") or "").strip()
        raw = p.get("raw_line") or ""
        if (
            pt == "replacement"
            and ec == "GainLife"
            and "'ValidPlayer': 'You'" in raw
            and "'Prevent': 'True'" not in raw
        ):
            has_up_bias = True
            break
        if pt == "static" and ec == "Continuous" and _UP_BIASED_SVAR_COMPARE_RE.search(raw):
            has_up_bias = True
            break
    if not has_up_bias:
        return []

    peer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT p1.card_name "
            "FROM card_ports p1 "
            "WHERE p1.port_type = 'scales_with' "
            "AND p1.event_class = 'YourLifeTotal' "
            "AND EXISTS ("
            "  SELECT 1 FROM card_ports p2 "
            "  WHERE p2.card_name = p1.card_name "
            "  AND ("
            "    (p2.port_type='keyword' AND p2.event_class='Lifelink') "
            "    OR (p2.port_type='effect' AND p2.event_class='GainLife' "
            "        AND p2.valid_filter='You') "
            "    OR (p2.port_type='replacement' AND p2.event_class='GainLife')"
            "  )"
            ")"
        )
    }

    results: list[PortComplement] = []
    for name in peer_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="life_total_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="life_total_axis",
                cand_event="life_total_peer",
            )
        )
    return results


def _find_land_bounce_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for commanders whose activated ability requires returning a
    land from the battlefield to the hand as a cost.

    Target archetype: land-bounce / land-recursion commanders. Meloku
    the Clouded Mirror (return land → Spirit token), Mina and Denn
    Wildborn (return land → play extra land), Multani Yavimaya's
    Avatar (return 2 lands → creature pump), Sutina Speaker of the
    Tajuru (return land → ramp / counters), Soramaro First to Dream
    (return land → Dig X), Tameshi Reality Architect (return land →
    flicker/recast non-creature permanent).

    Gate:

    - cmdr has ``cost.return`` port with ``cost_subtype`` of the form
      ``<N>/Land...`` (e.g. ``1/Land``, ``2/Land``, ``1/Land/land``)
    - AND ``cost_target='any'`` (external land, not self-bounce like
      Rootha / Shigeki / Bilbo which tuck themselves)

    Two deduped tiers:

    - ``land_bounce_extra_drops``: ``static.Continuous`` whose raw
      line contains ``AdjustLandPlays``. ~38 cards — Azusa, Explore,
      Exploration, Oracle of Mul Daya, Dryad of the Ilysian Grove,
      Fastbond, Ghirapur Orrery, Rites of Flourishing, Burgeoning,
      Flubs (the Fool), Hugs, Aesi. Turning the land-bounce into a
      neutral tempo play (replay the bounced land, still land drop).
    - ``land_bounce_gy_recur``: ``effect.ChangeZone`` with
      ``zone_origin='Graveyard'`` and ``valid_filter`` containing
      ``Land``, rejecting opponent-targeting. ~56 cards — Crucible of
      Worlds, Ramunap Excavator, Splendid Reclamation, World Shaper,
      Life from the Loam, Emeria Shepherd, Lord Windgrace, Molderhulk,
      Deathrite Shaman (mode), Groundskeeper, Lodestone Bauble. Turns
      destroyed/sacrificed lands back into resources, compounding
      with the bounce loop.
    """
    has_land_return_cost = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "cost":
            continue
        if (p.get("event_class") or "").strip() != "return":
            continue
        if (p.get("cost_target") or "").strip() != "any":
            continue
        cst = (p.get("cost_subtype") or "").strip()
        # cost_subtype is "N/Type" (Forge pattern) — accept "Land" or
        # "Land/land" (typed-and-tagged variants both occur).
        parts = cst.split("/")
        if len(parts) >= 2 and parts[1] == "Land":
            has_land_return_cost = True
            break
    if not has_land_return_cost:
        return []

    # Exclude commanders whose PRIMARY axis is not land-focused: big-
    # hand (scales_with.ValidHand Card.YouOwn — Soramaro) and X-cost
    # spell-payoff (scales_with.xPaid — Tameshi whose bounce cost
    # enables a flicker/recast of a non-creature permanent). For these
    # commanders the land-return is an incidental cost, not the engine,
    # and feeding them AdjustLandPlays / GY-land-recur displaces their
    # real archetype picks (audit 2026-04-20: Soramaro -0.139 NDCG,
    # Tameshi -0.056 on the initial rule without this exclusion).
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "scales_with":
            continue
        ec = (p.get("event_class") or "").strip()
        if ec == "xPaid":
            return []
        if ec.startswith("ValidHand "):
            return []

    extra_drops_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'static' "
            "AND event_class = 'Continuous' "
            "AND raw_line LIKE '%AdjustLandPlays%'"
        )
    }

    gy_recur_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class = 'ChangeZone' "
            "AND zone_origin = 'Graveyard' "
            "AND valid_filter LIKE '%Land%' "
            "AND raw_line NOT LIKE '%Opponent%'"
        )
    }

    tier_priority = (
        ("land_bounce_extra_drops", extra_drops_set),
        ("land_bounce_gy_recur", gy_recur_set),
    )
    seen: set[str] = set()
    results: list[PortComplement] = []
    for cand_event, candidates in tier_priority:
        for name in candidates:
            if name in cmdr_set or name in seen:
                continue
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="land_bounce_feeder",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="land_bounce_cost",
                    cand_event=cand_event,
                )
            )
    return results


def _find_creature_died_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for aristocrats commanders whose payoff scales with the
    count of creatures that died this turn.

    Forge's ``Count$ThisTurnEntered_Graveyard_from_Battlefield_Creature``
    SVar returns how many creatures hit the graveyard this turn.
    Filter suffixes narrow to ``YouCtrl`` / ``YouOwn`` / ``!token`` /
    self-exclusion variants, but all commanders on any of these
    variants play the same archetype: sacrifice fodder + death
    triggers + payoff X-effects.

    Target cmdrs (15 legendary): Asmira Holy Avenger, Bontu the
    Glorified, Denethor Ruling Steward, Ebondeath Dracolich, Faramir
    Field Commander, Gadrak Crown-Scourge, Gimli Mournful Avenger,
    Inga Rune-Eyes, Kuon Ogre Ascendant, Lagomos Hand of Hatred,
    Mahadi Emporium Master, Nevinyrral Urborg Tyrant, Shessra Death's
    Whisper, Sméagol Helpful Guide, Tobias Doomed Conqueror. 0% prior
    coverage on this axis across all 15.

    Single tier:

    - ``creature_died_peer``: other cards with any
      ``scales_with.ThisTurnEntered_Graveyard_from_Battlefield_Creature*``
      variant. ~49 non-legendary peers — Feast of the Victorious
      Dead, Fresh Meat, Caller of the Claw, Deathreap Ritual, Grizzly
      Ghoul, Khabál Ghoul, Tallyman of Nurgle, Liliana's Devotee /
      Scrounger / Standard Bearer, Warlock Class, Ichor Shade, Rise
      of the Dread Marn, Osai Vultures, Vile Redeemer, Spoils of
      Blood, Body Count, Spymaster's Vault, Séance Board, Diregraf
      Rebirth, Season of Loss, Gravelighter, Compy Swarm, Canopy
      Stalker, Bone Devourer, Death-Priest of Myrkul. Pool IS the
      aristocrats staple set — same mechanical-shape discriminator as
      party_feeder.

    Follows the party_feeder pattern: uniform mechanical archetype,
    peer pool mechanically identical to the cmdrs, no tribal
    overlays to displace (unlike gy_creature_count_feeder reverted
    2026-04-21). Gate uses a LIKE prefix-match so all filter variants
    are covered (YouCtrl / YouOwn / !token / !namedX).
    """
    prefix = _CREATURE_DIED_AXIS_PREFIX
    has_axis = any(
        (p.get("port_type") or "").strip() == "scales_with" and (p.get("event_class") or "").strip().startswith(prefix)
        for p in cmdr_ports
    )
    if not has_axis:
        return []

    peer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'scales_with' AND event_class LIKE ?",
            (f"{prefix}%",),
        )
    }

    results: list[PortComplement] = []
    for name in peer_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="creature_died_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="creature_died_axis",
                cand_event="creature_died_peer",
            )
        )
    return results


def _find_party_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for Party-count commanders (Zendikar Rising / Baldur's Gate /
    Final Fantasy Party mechanic).

    Forge's ``Count$Party`` SVar returns 1–4 = distinct count of
    Cleric / Rogue / Warrior / Wizard creatures you control, capped at
    one of each. A full party scales every Party-keyed effect to 4×.
    Commanders on this axis play a hard-locked tribal-adjacent deck
    whose core is: recruit one of each of the 4 party types, then
    resolve Party-scaled payoffs.

    Target cmdrs: Burakos Party Leader, Linvala Shield of Sea Gate,
    Nalia de'Arnise, Tazri Beacon of Unity, The Destined Black Mage /
    Thief / Warrior / White Mage, Zagras Thief of Heartbeats — 9
    legendary cmdrs, 0% prior coverage.

    Single tier:

    - ``party_peer``: other cards with ``scales_with.Party`` port.
      ~34 cards — Acquisitions Expert, Allied Assault, Archpriest of
      Iona, Ardent Electromancer, Cascade Seer, Coveted Prize,
      Deadly Alliance, Drana's Silencer, Emeria Captain, Grotag
      Bug-Catcher, Journey to Oblivion, Kabira Outrider, Malakir
      Blood-Priest, Multiclass Baldric, Nimble Trapfinder, Ravager's
      Mace, Sea Gate Colossus, Seafloor Stalker, Shatterskull
      Minotaur, Spoils of Adventure, Squad Commander, Strength of
      Solidarity, Synchronized Spellcraft, Thundering Sparkmage,
      Thwart the Grave, Veteran Adventurer — Zendikar Rising /
      Commander Legends Baldur's Gate / Final Fantasy Party staples.
      Every one of these cards literally defines the archetype's
      payoff set.

    The axis is mechanically uniform (all 35 cards use the identical
    ``SVar:X:Count$Party`` / ``SVar:Y:Count$Party`` pattern) so a
    peer-only rule is the correct shape.
    """
    has_party_axis = any(
        (p.get("port_type") or "").strip() == "scales_with" and (p.get("event_class") or "").strip() == "Party"
        for p in cmdr_ports
    )
    if not has_party_axis:
        return []

    peer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'scales_with' AND event_class = 'Party'"
        )
    }

    results: list[PortComplement] = []
    for name in peer_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="party_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="party_axis",
                cand_event="party_peer",
            )
        )
    return results


def _find_etb_tapped_stax_feeders(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Rule for stax / pillowfort commanders whose ``replacement.Moved``
    port forces **external** permanents to enter the battlefield tapped.

    Target archetype: tempo-denial hatebear commanders — Reidane (snow
    lands ETB tapped), Spider-Woman (opp artifacts + creatures),
    Thalia and The Gitrog Monster and Thalia, Heretic Cathar (opp
    creatures + non-basic lands), Urabrask the Hidden (opp creatures),
    Zhao, the Moon Slayer (non-basic lands), Archelos, Lagoon Mystic
    (all permanents while Self tapped).

    The gate explicitly excludes the Card.Self flavor (~542 cards —
    every tapped land plus creatures like Grimgrin / Ebondeath /
    Alirios / Taeko whose "ETBs tapped" is a drawback, not a stax
    tool). Those cmdrs have their own archetype coverage via
    sacrifice_outlets / graveyard_filler / etb_self.

    Single tier:

    - ``etb_tapped_stax_peer``: other cards with
      ``replacement.Moved`` + ``replacement_result='ETBTapped'`` whose
      ``valid_filter`` does NOT reference ``Card.Self``. ~24 cards —
      Authority of the Consuls, Kismet, Blind Obedience, Loxodon
      Gatekeeper, Kinjalli's Sunwing, Imposing Sovereign, Manglehorn,
      Dauntless Dismantler, Archon of Emeria, Phyrexian Censor, Frozen
      Aether, Orb of Dreams, Root Maze, False Floor, Uphill Battle,
      Ashling's Prerogative, Radiant Grace. Tight pool, high IDF, all
      EDHREC stax staples.
    """
    has_external_etb_tapped = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "replacement":
            continue
        if (p.get("event_class") or "").strip() != "Moved":
            continue
        if (p.get("replacement_result") or "").strip() != "ETBTapped":
            continue
        vf = p.get("valid_filter") or ""
        if "Self" in vf:
            continue
        has_external_etb_tapped = True
        break
    if not has_external_etb_tapped:
        return []

    peer_set: set[str] = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'replacement' "
            "AND event_class = 'Moved' "
            "AND replacement_result = 'ETBTapped' "
            "AND (valid_filter IS NULL OR valid_filter NOT LIKE '%Self%')"
        )
    }

    results: list[PortComplement] = []
    for name in peer_set:
        if name in cmdr_set:
            continue
        results.append(
            PortComplement(
                rule_id="etb_tapped_stax_feeder",
                direction="synergy",
                candidate=name,
                cmdr_event="etb_tapped_stax_axis",
                cand_event="etb_tapped_stax_peer",
            )
        )
    return results
