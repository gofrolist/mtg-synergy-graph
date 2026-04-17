"""Token-related complement matchers."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

from ..graph_engine import _trigger_only_matches_self
from ..penalties import _token_subtype
from .core import PortComplement, PortRow

if TYPE_CHECKING:
    from ..penalties import CandidateCache


def _find_effect_feeds_etb(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Filter-aware effect->trigger matching for zone-change effects.

    Connects commander effects that produce permanents (Token, ChangeZone,
    CopyPermanent, Animate) to candidate ChangesZone triggers, using the
    type filter to narrow matches.

    Token effects produce creatures entering the battlefield -> match
    ``ChangesZone Creature.*`` triggers (Impact Tremors, Purphoros).
    ChangeZone effects with ``Land.YouCtrl`` filter -> match
    ``ChangesZone Land.*`` triggers (Lotus Cobra, Tatyova).

    The blanket effect_feeds_trigger rule excludes these events because
    unfiltered matching produces 900-6600 candidates. This function
    narrows to ~200-350 by intersecting filter types.
    """
    _PRIMARY_TYPES = frozenset(
        {
            "Creature",
            "Artifact",
            "Enchantment",
            "Land",
            "Planeswalker",
        }
    )
    # Broad filter bases that match too many candidates -- skip these
    _TOO_BROAD_BASES = frozenset({"Card", "Permanent", ""})

    # Collect (base_type, zone_destination) pairs from commander effects
    # that produce permanents entering zones
    _ZONE_EFFECT_EVENTS = frozenset(
        {
            "Token",
            "ChangeZone",
            "ChangeZoneAll",
            "CopyPermanent",
            "Animate",
        }
    )
    cmdr_produces: set[tuple[str, str]] = set()  # (base_type, zone_dest)
    # Whether to gate Creature ETB by "Other" filter (non-tribal tokens)
    _needs_creature_other_gate = False

    # Get commander's literal subtypes for tribal gating (batched)
    cmdr_literal_subtypes: set[str] = set()
    cmdr_list = list(cmdr_set)
    for row in conn.execute(
        "SELECT subtypes FROM cards WHERE name IN ({})".format(",".join("?" * len(cmdr_list))),
        tuple(cmdr_list),
    ).fetchall():
        if row["subtypes"]:
            cmdr_literal_subtypes.update(row["subtypes"].split())

    for p in cmdr_ports:
        if p.get("port_type") != "effect":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in _ZONE_EFFECT_EVENTS:
            continue

        if ev == "Token":
            raw = str(p.get("raw_line") or "")
            m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
            if m:
                script = m.group(1)
                parts = script.lower().split("_")
                # Artifact tokens always match Artifact ETB triggers
                if len(parts) >= 4 and parts[3] == "a":
                    cmdr_produces.add(("Artifact", "Battlefield"))
                # Tribal gating: only add Creature ETB for non-tribal
                # token commanders. Krenko (Goblin making Goblins) is
                # tribal -> skip. Locust God (not an Insect making Insects)
                # is non-tribal -> add Creature ETB payoffs.
                token_sub = _token_subtype(script)
                if token_sub and token_sub not in cmdr_literal_subtypes:
                    cmdr_produces.add(("Creature", "Battlefield"))
                    _needs_creature_other_gate = True
        else:
            # ChangeZone/CopyPermanent/Animate -- extract type from filter
            vf = p.get("valid_filter") or ""
            zd = (p.get("zone_destination") or "").strip()
            if not zd:
                zd = "Battlefield"
            for alt in vf.split(","):
                base = alt.strip().split(".")[0].split("+")[0].strip()
                if base in _PRIMARY_TYPES:
                    cmdr_produces.add((base, zd))

    if not cmdr_produces:
        return []

    # Preload creature names for the "Other" gate (used for O(1) membership check)
    _creature_names: set[str] = set()
    if _needs_creature_other_gate:
        _creature_names = {
            r["name"] for r in conn.execute("SELECT name FROM cards WHERE card_types LIKE '%Creature%'").fetchall()
        }

    # Fetch candidate ChangesZone triggers (non-self)
    cur = conn.execute(
        "SELECT card_name, valid_filter, zone_destination, branch_kind "
        "FROM card_ports "
        "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
        "AND valid_filter IS NOT NULL AND valid_filter != ''"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        vf = r["valid_filter"] or ""
        if _trigger_only_matches_self(vf):
            continue
        cand_zd = (r["zone_destination"] or "").strip()
        if not cand_zd:
            cand_zd = "Any"

        # Check if any commander production matches trigger's filter
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in _TOO_BROAD_BASES:
                continue
            for cmdr_base, cmdr_zd in cmdr_produces:
                if base != cmdr_base:
                    continue
                # Zone must be compatible
                if cand_zd != "Any" and cmdr_zd != cand_zd:
                    continue
                # Non-tribal Creature tokens: skip creature cards
                # without "Other" qualifier (they trigger on
                # themselves, not on tokens the commander creates).
                # Non-creature cards (Impact Tremors, Aura Shards)
                # are always genuine payoffs.
                if (
                    cmdr_base == "Creature"
                    and _needs_creature_other_gate
                    and "Other" not in vf
                    and card in _creature_names
                ):
                    continue
                seen.add(card)
                results.append(
                    PortComplement(
                        rule_id="effect_feeds_trigger",
                        direction="synergy",
                        candidate=card,
                        cmdr_event=f"Token_{cmdr_base}" if cmdr_base else "ChangeZone",
                        cand_event=f"ChangesZone_{base}",
                        branch_kind=r["branch_kind"] or "root",
                    )
                )
                break
            if card in seen:
                break

    return results


def _find_token_producers_for_trigger(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find token producers for commanders that trigger on creatures entering.

    Purphoros triggers on ChangesZone Creature -> Battlefield. He wants
    token producers (Krenko, Siege-Gang Commander). But _find_effect_feeds_etb
    only looks for commander EFFECTS, not triggers. This function fills
    that gap for trigger-based ETB commanders.
    """
    _PRIMARY_TYPES = frozenset(
        {
            "Creature",
            "Artifact",
            "Enchantment",
            "Land",
        }
    )

    # Find ChangesZone triggers with type filter (non-self).
    # Only match triggers that accept tokens (no `.!token` filter).
    # Purphoros (Creature.Other+YouCtrl) accepts tokens -> YES.
    # Chainer (Creature.!token+YouCtrl) excludes tokens -> NO.
    wanted_types: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        if (p.get("event_class") or "").strip() != "ChangesZone":
            continue
        vf = p.get("valid_filter") or ""
        zd = (p.get("zone_destination") or "").strip()
        if _trigger_only_matches_self(vf):
            continue
        if zd != "Battlefield":
            continue
        if "!token" in vf.lower():
            continue
        # Extract type from filter
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in _PRIMARY_TYPES:
                wanted_types.add(base)

    if not wanted_types or "Creature" not in wanted_types:
        return []

    # Find token producers (cards with Token effects)
    cur = conn.execute("SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'")
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="token_producer",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="ChangesZone_Creature",
                    cand_event="Token",
                )
            )

    return results


def _find_static_strategy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Detect static-only commander strategies and find matching cards.

    Handles two patterns:
    - Go-wide buff (Jetmir): Continuous static pumping Creature.YouCtrl
      -> find token producers
    - Voltron (Sigarda, Rafiq): Self-protection keywords or Exalted
      -> find Auras and Equipment
    """
    has_creature_pump = False
    has_voltron = False
    # Only Hexproof/Shroud and Exalted indicate voltron strategy.
    # Indestructible is too broad (Heliod, Xenagos have it but aren't voltron).
    _VOLTRON_KEYWORDS = frozenset(
        {
            "Hexproof",
            "Shroud",
            "Exalted",
        }
    )

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        raw = str(p.get("raw_line") or "")

        # Go-wide: Continuous static PUMPING creatures (AddPower/AddToughness).
        # Keyword grants alone (Haste from Maelstrom Wanderer) don't indicate
        # a go-wide strategy -- the commander wants ANY creatures, not MORE of them.
        if pt == "static" and ev == "Continuous":
            affected = p.get("affected_scope") or ""
            if ("Creature.YouCtrl" in affected or "Creature.YouCtrl" in raw) and (
                "'AddPower'" in raw or "'AddToughness'" in raw
            ):
                has_creature_pump = True

        # Voltron: self-protection keywords
        if pt == "keyword" and ev in _VOLTRON_KEYWORDS:
            has_voltron = True

    results: list[PortComplement] = []
    seen: set[str] = set()

    # Go-wide: find token producers
    if has_creature_pump:
        cur = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
        )
        for r in cur.fetchall():
            name = r["card_name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="token_producer",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="creature_pump",
                        cand_event="Token",
                    )
                )

    # Voltron: find Auras and Equipment
    if has_voltron:
        cur = conn.execute(
            "SELECT DISTINCT name FROM cards WHERE subtypes LIKE '%Aura%' OR subtypes LIKE '%Equipment%'"
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="voltron",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="self_protection",
                        cand_event="Aura_Equipment",
                    )
                )

    return results


def _find_token_etb_damage(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Find creature-ETB-damage payoffs for token-producing commanders.

    The Locust God creates 1/1 Insect tokens on every card draw; Krenko
    taps for Goblin tokens; Edgar Markov puts +1/+1 counters while making
    Vampires. Every token ETB can trigger a payoff like Impact Tremors
    ("Whenever a creature enters under your control, [DealDamage] each
    opponent"), Purphoros, or Witty Roastmaster.

    Gate: commander has any ``effect: Token`` port (in any branch).
    Match: candidates that have BOTH a ``trigger: ChangesZone`` with
    a Creature valid_filter AND a ``DealDamage`` effect targeting a
    Player or Opponent — the self-contained ETB-damage card shape.
    """
    # Narrow gate: commander must have a Token effect in an EXECUTE
    # branch — produced passively by a trigger (Locust God's Drawn
    # trigger, Kykar's SpellCast trigger, Prossh's ETB). Excludes
    # commanders whose Token output is from an activated ability
    # (Ghave spends counters, Rhys pays mana) — their strategy axis is
    # different (counter/token multiplication, not ETB-damage ping).
    has_passive_token = any(
        (p.get("port_type") or "").strip() == "effect"
        and (p.get("event_class") or "").strip() == "Token"
        and (p.get("branch_kind") or "").strip() == "execute"
        for p in cmdr_ports
    )
    if not has_passive_token:
        return []

    # Commander-independent self-join cost (~800 ms) is memoised on the
    # CandidateCache; fall back to a direct query when no cache is passed
    # (test harnesses that call this helper in isolation).
    if candidate_cache is not None:
        payoff_cards: frozenset[str] = candidate_cache.token_etb_damage_cards
    else:
        from ..penalties import _bulk_load_token_etb_damage_cards

        payoff_cards = _bulk_load_token_etb_damage_cards(conn)

    return [
        PortComplement(
            rule_id="token_etb_damage",
            direction="synergy",
            candidate=name,
            cmdr_event="token_producer",
            cand_event="etb_damage",
        )
        for name in payoff_cards
        if name not in cmdr_set
    ]


def _find_token_sac_chain(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Find token producers for sacrifice-trigger commanders (2-hop chain).

    Korvold triggers on Sacrificed -> Pitiless Plunderer creates Treasure
    tokens -> Treasures can be sacrificed to feed Korvold.

    This is a 2-step chain: candidate produces tokens -> tokens get
    sacrificed -> commander triggers. Currently no rule captures this.

    Matches cards producing self-sacrificing tokens (Treasure, Food, Clue,
    Blood) for commanders with Sacrificed triggers.
    """
    has_sac_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev == "Sacrificed":
            has_sac_trigger = True
            break

    if not has_sac_trigger:
        return []

    # Find cards producing self-sacrificing tokens (Treasure, Food, Clue, Blood)
    # These tokens have built-in sacrifice abilities
    _SAC_TOKEN_PATTERNS = ("treasure", "food", "clue", "blood")

    if candidate_cache is not None:
        token_rows = candidate_cache.token_effect_rows
    else:
        token_rows = tuple(
            (row["card_name"], row["raw_line"] or "")
            for row in conn.execute(
                "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
            )
        )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for card_name, raw_line in token_rows:
        name = card_name
        if name in cmdr_set or name in seen:
            continue
        raw = str(raw_line)
        m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
        if not m:
            continue
        script = m.group(1).lower()
        for pattern in _SAC_TOKEN_PATTERNS:
            if pattern in script:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="token_sac_chain",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="Sacrificed",
                        cand_event=f"Token_{pattern}",
                    )
                )
                break

    return results
