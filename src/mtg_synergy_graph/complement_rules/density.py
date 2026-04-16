"""Density-based complement matchers (lord, ETB-self, scaling, spell, tribal)."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING

from ..graph_engine import (
    _SELF_EVENT_TRIGGERS,
    CATCH_ALL_TRIGGERS,
    _filter_card_match,
    _rows_to_dicts,
    _trigger_only_matches_self,
    explode_filter,
)
from .core import PortComplement, PortRow, _commander_subtypes_from_ports

if TYPE_CHECKING:
    from ..penalties import CandidateCache


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcard characters (%, _) in a value."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_lord_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Lord matching: candidate static Continuous ports whose affected_scope
    overlaps with commander's mechanically-relevant subtypes.
    """
    cmdr_subtypes = _commander_subtypes_from_ports(
        conn,
        list(cmdr_set),
        cmdr_ports,
    )
    if not cmdr_subtypes:
        return []

    if candidate_cache is not None:
        rows = candidate_cache.lord_continuous_rows
    else:
        rows = tuple(
            (row["card_name"], row["affected_scope"] or "", row["branch_kind"] or "")
            for row in conn.execute(
                "SELECT card_name, affected_scope, branch_kind "
                "FROM card_ports "
                "WHERE port_type = 'static' AND event_class = 'Continuous' "
                "AND affected_scope IS NOT NULL AND affected_scope != ''"
            )
        )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for card, scope, branch in rows:
        if card in cmdr_set or card in seen:
            continue
        attrs = explode_filter(scope)
        scope_subtypes = {a["attr_value"] for a in attrs if a["attr_kind"] == "subtype"}
        if scope_subtypes & cmdr_subtypes:
            seen.add(card)
            results.append(
                PortComplement(
                    rule_id="lord",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="tribal",
                    cand_event="Continuous",
                    branch_kind=branch or "root",
                )
            )
    return results


def _find_etb_self_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """ETB-self: candidate card's identity (type/subtype) satisfies
    a commander trigger's valid_filter.

    In batch mode a shared ``candidate_cache`` short-circuits two hot
    spots:
    1. The per-commander SQL that loaded card-attr rows.
    2. The per-card ``_card_attrs_for_filter`` invocation inside
       ``_filter_card_match`` — we look up the precomputed attribute
       set by name instead.
    """
    # Collect commander self-event triggers with valid_filters
    triggers: list[PortRow] = []
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in _SELF_EVENT_TRIGGERS:
            continue
        vf = p.get("valid_filter") or ""
        if not vf or _trigger_only_matches_self(vf):
            continue
        triggers.append(p)

    if not triggers:
        return []

    # Parse each trigger's usable alternatives.
    # Skip overly broad types -- Permanent/Card match nearly everything
    # and produce IDF ~ 0.07, adding noise without signal.
    _SKIP_BASES = frozenset({"Permanent", "Card"})
    parsed: list[tuple[PortRow, list[str]]] = []
    for trig in triggers:
        vf = trig.get("valid_filter") or ""
        alts = [
            a.strip()
            for a in vf.split(",")
            if a.strip()
            and not a.strip().startswith("Card.Self")
            and a.strip().split(".")[0].split("+")[0].strip() not in _SKIP_BASES
        ]
        if alts:
            parsed.append((trig, alts))

    if not parsed:
        return []

    # Pre-compute SQL type hints from trigger filters to avoid full-table scan.
    # E.g. "Creature.Goblin+YouCtrl" -> SQL WHERE card_types LIKE '%Creature%'
    _PRIMARY_TYPES = frozenset(
        {
            "Creature",
            "Artifact",
            "Enchantment",
            "Land",
            "Instant",
            "Sorcery",
            "Planeswalker",
        }
    )
    all_type_hints: set[str] = set()
    needs_full_scan = False
    for _trig, alts in parsed:
        for alt in alts:
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _PRIMARY_TYPES:
                all_type_hints.add(base)
            else:
                needs_full_scan = True
                break
        if needs_full_scan:
            break

    if candidate_cache is not None:
        # Batch mode: iterate the shared in-memory attr rows. We still
        # apply the type-hint narrowing in Python to preserve the
        # original behaviour of skipping cards whose card_types don't
        # intersect any requested primary type.
        attr_rows = candidate_cache.candidate_attr_rows
        if needs_full_scan or not all_type_hints:
            cards = list(attr_rows.values())
        else:
            cards = [row for row in attr_rows.values() if any(h in (row["card_types"] or "") for h in all_type_hints)]
    elif needs_full_scan or not all_type_hints:
        cards = _rows_to_dicts(
            conn.execute(
                "SELECT name, card_types, supertypes, subtypes, keywords, color_identity FROM cards"
            ).fetchall()
        )
    else:
        params = [f"%{_escape_like(t)}%" for t in all_type_hints]
        where = " OR ".join("card_types LIKE ? ESCAPE '\\'" for _ in params)
        cards = _rows_to_dicts(
            conn.execute(
                f"SELECT name, card_types, supertypes, subtypes, keywords, color_identity FROM cards WHERE {where}",
                params,
            ).fetchall()
        )

    # Lookup map for precomputed per-card attr sets. ``None`` falls back
    # to per-call recomputation inside ``_filter_card_match``.
    card_attrs_map = candidate_cache.card_attrs if candidate_cache is not None else None

    results: list[PortComplement] = []
    seen: set[tuple[str, str]] = set()
    for trig, alts in parsed:
        ev = (trig.get("event_class") or "").strip()
        for card in cards:
            name = card["name"]
            if name in cmdr_set:
                continue
            key = (name, ev)
            if key in seen:
                continue
            cached_attrs = card_attrs_map.get(name) if card_attrs_map is not None else None
            if any(_filter_card_match(alt, card, cached_attrs) for alt in alts):
                seen.add(key)
                results.append(
                    PortComplement(
                        rule_id="etb_self",
                        direction="synergy",
                        candidate=name,
                        cmdr_event=ev,
                        cand_event="card_identity",
                    )
                )

    return results


def _token_present_without_non_prefix(token: str, text: str) -> bool:
    """Return True if ``token`` appears in ``text`` without a 'non' prefix.

    Avoids matching 'nonLand' as containing 'Land', 'nonCreature' as
    containing 'Creature', etc.
    """
    idx = text.find(token)
    while idx != -1:
        prefix = text[max(0, idx - 3) : idx].lower()
        if not prefix.endswith("non"):
            return True
        idx = text.find(token, idx + 1)
    return False


def _find_scaling_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Scaling: commander scales_with a type -> candidates of that type.

    Uril scales_with Aura -> every Aura card is a complement.
    """
    # Extract types from scales_with ports
    _TYPE_TOKENS = frozenset(
        {
            "Aura",
            "Equipment",
            "Enchantment",
            "Artifact",
            "Land",
            "Instant",
            "Sorcery",
            "Planeswalker",
        }
    )
    wanted_types: set[str] = set()
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "scales_with":
            continue
        raw = str(p.get("raw_line") or "")
        vf = p.get("valid_filter") or ""
        for tok in _TYPE_TOKENS:
            if _token_present_without_non_prefix(tok, raw) or _token_present_without_non_prefix(tok, vf):
                wanted_types.add(tok)

    if not wanted_types:
        return []

    # Primary types go to card_types, subtypes to subtypes column
    _PRIMARY = frozenset({"Enchantment", "Artifact", "Land", "Instant", "Sorcery", "Planeswalker"})
    primary = wanted_types & _PRIMARY
    subtypes = wanted_types - _PRIMARY  # Aura, Equipment

    results: list[PortComplement] = []
    seen: set[str] = set()

    for type_name in primary:
        cur = conn.execute(
            "SELECT name FROM cards WHERE card_types LIKE ? ESCAPE '\\'",
            (f"%{_escape_like(type_name)}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="scaling",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="scales_with",
                        cand_event=type_name,
                    )
                )

    for sub in subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ? ESCAPE '\\'",
            (f"%{_escape_like(sub)}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="scaling",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="scales_with",
                        cand_event=sub,
                    )
                )

    return results


def _find_spellcast_resonance(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """SpellCast trigger resonance: find candidates that also trigger on
    the same spell type as the commander.

    Sythis triggers on Enchantment cast -> Eidolon of Blossoms also
    triggers on Enchantment entering -> resonance. These enchantress-
    style payoffs are more synergistic than random enchantments.

    Unlike spell_density (flat weight for every card of the type),
    this uses IDF weighting so rare payoffs (Enchantment: N=23,
    IDF~0.22) score higher than common ones (Instant: N=186, IDF~0.13).
    """
    _CASTABLE_TYPES = frozenset(
        {
            "Instant",
            "Sorcery",
            "Creature",
            "Artifact",
            "Enchantment",
            "Planeswalker",
        }
    )

    # Extract type filters from commander SpellCast triggers
    cmdr_types: set[str] = set()
    cmdr_subtypes: set[str] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in ("SpellCast", "SpellCastOrCopy"):
            continue
        vf = p.get("valid_filter") or ""
        if not vf:
            continue
        for alt in vf.split(","):
            alt = alt.strip()
            if not alt or alt.startswith("Card.Self"):
                continue
            if "nonCreature" in alt or "non-Creature" in alt:
                cmdr_types.update({"Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker"})
                continue
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _CASTABLE_TYPES:
                cmdr_types.add(base)
            elif base and base[0].isupper() and base not in ("Card", "Permanent"):
                cmdr_subtypes.add(base)

    if not cmdr_types and not cmdr_subtypes:
        return []

    # Find candidate SpellCast triggers with overlapping type filters
    cur = conn.execute(
        "SELECT DISTINCT card_name, valid_filter FROM card_ports "
        "WHERE port_type = 'trigger' "
        "AND event_class IN ('SpellCast', 'SpellCastOrCopy') "
        "AND valid_filter IS NOT NULL AND valid_filter != ''"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        vf = r["valid_filter"] or ""
        if vf.startswith("Card.Self"):
            continue
        # Extract candidate's type filter — find the best matching type
        # (check all alts so Instant,Sorcery matches a Sorcery-only commander)
        matched_type = ""
        for alt in vf.split(","):
            base = alt.strip().split(".")[0].split("+")[0].strip()
            if base in cmdr_types:
                matched_type = base
                break
            if base in cmdr_subtypes:
                matched_type = base
                break
        if matched_type and card not in seen:
            seen.add(card)
            results.append(
                PortComplement(
                    rule_id="spellcast_resonance",
                    direction="synergy",
                    candidate=card,
                    cmdr_event="SpellCast",
                    cand_event=f"SpellCast_{matched_type}",
                    filter_group=matched_type,
                )
            )

    return results


def _find_spellcast_density_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """SpellCast/LandPlayed density: commanders with catch-all triggers
    want cards of the type they trigger on.

    Talrand triggers on Instant/Sorcery -> every Instant/Sorcery is a
    complement. Edgar triggers on Vampire -> every Vampire creature.

    Extracts the type filter from the catch-all trigger's valid_filter.
    """
    _CASTABLE_TYPES = frozenset(
        {
            "Instant",
            "Sorcery",
            "Creature",
            "Artifact",
            "Enchantment",
            "Planeswalker",
        }
    )
    _NONCREATURE_TYPES = frozenset(
        {
            "Instant",
            "Sorcery",
            "Artifact",
            "Enchantment",
            "Planeswalker",
        }
    )
    _TOO_BROAD = frozenset({"Creature", "Permanent", "Card"})

    wanted_types: set[str] = set()
    wanted_subtypes: set[str] = set()

    # Conspire-granting statics imply spell density need (Wort grants
    # Conspire to Instant/Sorcery -> wants instant/sorcery density).
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        raw = str(p.get("raw_line") or "")
        if pt == "static" and ev == "Continuous" and "'Conspire'" in raw:
            m = re.search(r"'Affected':\s*'([^']+)'", raw)
            if m:
                for alt in m.group(1).split(","):
                    base = alt.strip().split(".")[0].split("+")[0].strip()
                    if base in _CASTABLE_TYPES and base not in _TOO_BROAD:
                        wanted_types.add(base)

    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in CATCH_ALL_TRIGGERS:
            continue
        vf = p.get("valid_filter") or ""
        if not vf:
            continue
        for alt in vf.split(","):
            alt = alt.strip()
            if not alt or alt.startswith("Card.Self"):
                continue
            # Handle negative filters: "Card.nonCreature" -> all non-creature types
            if "nonCreature" in alt or "non-Creature" in alt:
                wanted_types.update(_NONCREATURE_TYPES)
                continue
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _CASTABLE_TYPES and base not in _TOO_BROAD:
                wanted_types.add(base)
            elif base not in _TOO_BROAD and base and base[0].isupper():
                wanted_subtypes.add(base)

    if not wanted_types and not wanted_subtypes:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    for type_name in wanted_types:
        cur = conn.execute(
            "SELECT name FROM cards WHERE card_types LIKE ? ESCAPE '\\'",
            (f"%{_escape_like(type_name)}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="spell_density",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="SpellCast",
                        cand_event=type_name,
                    )
                )

    for sub in wanted_subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ? ESCAPE '\\'",
            (f"%{_escape_like(sub)}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="spell_density",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="SpellCast",
                        cand_event=sub,
                    )
                )

    return results


def _find_tribal_density_complements(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Tribal density: every creature of the commander's tribe is a
    complement.

    Krenko is a Goblin commander -> every Goblin creature is a complement.
    Sliver Overlord -> every Sliver. Marrow-Gnawer -> every Rat.

    Uses the same subtype extraction as the lord rule (includes token
    subtypes like Saproling for Slimefoot).
    """
    # Suppress tribal density for spell-copy commanders (Wort). Their
    # token subtypes pass the tribal gate (Goblin IS a literal subtype)
    # but the strategy is Conspire/spell-copying, not Goblin tribal.
    # spell_density is the correct axis for these commanders.
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        raw = str(p.get("raw_line") or "")
        if pt == "effect" and ev == "CopySpellAbility":
            return []
        if pt == "keyword" and ev == "Conspire":
            return []
        # Wort grants Conspire via static Continuous AddKeyword
        if pt == "static" and "'Conspire'" in raw:
            return []

    subtypes = _commander_subtypes_from_ports(conn, list(cmdr_set), cmdr_ports)
    if not subtypes:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    for sub in subtypes:
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ? AND card_types LIKE '%Creature%'",
            (f"%{sub}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="tribal_density",
                        direction="synergy",
                        candidate=name,
                        cmdr_event="tribal",
                        cand_event=sub,
                    )
                )

    return results


def _find_power_matters_density(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find high-power creatures for power-matters commanders.

    Selvala scales_with greatestPower -- she wants big creatures because
    her tap ability draws cards and adds mana equal to greatest power.
    Goreclaw, Ghalta similarly want high-power density.

    Tiered by power to give IDF differentiation:
    - power >= 7: narrow (413 cards, high IDF)
    - power >= 5: broader (2091 cards, lower IDF)
    """
    has_power_scaling = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        if pt != "scales_with":
            continue
        ev = (p.get("event_class") or "").strip()
        vf = p.get("valid_filter") or ""
        if "Power" in ev or "Power" in vf or "greatestPower" in vf:
            has_power_scaling = True
            break

    if not has_power_scaling:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    # Tier 1: power >= 7 (premium picks)
    for row in conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE '%Creature%' AND CAST(power AS INTEGER) >= 7"
    ).fetchall():
        name = row["name"]
        if name not in cmdr_set:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="power_matters",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="scales_with_power",
                    cand_event="power_7plus",
                )
            )
    # Tier 2: power 5-6 (good but broader)
    for row in conn.execute(
        "SELECT name FROM cards WHERE card_types LIKE '%Creature%' AND CAST(power AS INTEGER) >= 5 AND CAST(power AS INTEGER) < 7"
    ).fetchall():
        name = row["name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="power_matters",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="scales_with_power",
                    cand_event="power_5plus",
                )
            )

    return results


def _find_proliferate_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Find proliferate effects for counter-caring commanders.

    Mizzix (experience counters), Ezuri (experience + P1P1), Vorel
    (counter doubling), Atraxa (superfriends) — all benefit from
    proliferate effects that add counters to permanents/players.

    Fires when commander has:
    - scales_with YourCountersExperience (experience counter commanders)
    - P1P1 counter interest (from _has_counter_interest)
    - effect MultiplyCounter (Vorel)
    - keyword Proliferate (Atraxa — find MORE proliferate cards)
    """
    wants_proliferate = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "scales_with" and "Experience" in ev:
            wants_proliferate = True
            break
        if pt == "effect" and ev == "MultiplyCounter":
            wants_proliferate = True
            break
        if ev == "Proliferate":
            wants_proliferate = True
            break

    # Also check P1P1 counter interest
    if not wants_proliferate and _has_counter_interest(cmdr_ports):
        wants_proliferate = True

    if not wants_proliferate:
        return []

    # Find all cards that actively proliferate (effect or trigger, not cost)
    if candidate_cache is not None:
        names_iter = candidate_cache.proliferate_cards
    else:
        names_iter = (
            row["card_name"]
            for row in conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE event_class = 'Proliferate' "
                "AND port_type IN ('effect', 'trigger')"
            )
        )
    results: list[PortComplement] = []
    for name in names_iter:
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="proliferate_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="counter_scaling",
                    cand_event="proliferate",
                )
            )

    return results


def _find_scales_with_density(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Find cards that contribute to a commander's scaling condition.

    Parses ``scales_with`` event_class to determine what the commander
    needs more of:
    - ``CardCounters.P1P1`` -> cards that put +1/+1 counters
    - ``CardToughness`` -> high-toughness creatures (Phenax mill-by-toughness)
    - ``LifeOppsLostThisTurn`` -> damage/drain effects
    """
    results: list[PortComplement] = []
    seen: set[str] = set()

    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "scales_with":
            continue
        ev = (p.get("event_class") or "").strip()

        # CardCounters.P1P1 -> find counter producers
        # Also matches Valid with P1P1 in valid_filter (Hamza:
        # scales_with Valid filter=Creature.YouCtrl+counters_GE1_P1P1)
        vf = (p.get("valid_filter") or "").strip()
        if "P1P1" in ev or "P1P1" in vf:
            if candidate_cache is not None:
                names = candidate_cache.p1p1_counter_producers
            else:
                names = frozenset(
                    row["card_name"]
                    for row in conn.execute(
                        "SELECT DISTINCT card_name FROM card_ports "
                        "WHERE port_type = 'effect' AND event_class IN "
                        "('PutCounter', 'PutCounterAll', 'Proliferate') "
                        "AND (counter_type IS NULL OR counter_type = '' "
                        "OR counter_type = 'P1P1')"
                    )
                )
            for name in names:
                if name not in cmdr_set and name not in seen:
                    seen.add(name)
                    results.append(
                        PortComplement(
                            rule_id="scaling",
                            direction="synergy",
                            candidate=name,
                            cmdr_event="scales_P1P1",
                            cand_event="counter_producer",
                        )
                    )

        # CardToughness -> high-toughness creatures (Phenax mills by toughness).
        # Match creatures with toughness significantly exceeding power
        # (wall-like stat line) OR Defender keyword.
        elif "CardToughness" in ev:
            cur = conn.execute(
                "SELECT DISTINCT name AS card_name FROM cards "
                "WHERE card_types LIKE '%Creature%' "
                "AND CAST(toughness AS INTEGER) >= 4 "
                "AND CAST(toughness AS INTEGER) >= CAST(power AS INTEGER) + 2 "
                "UNION "
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'keyword' AND event_class = 'Defender'"
            )
            for r in cur.fetchall():
                name = r["card_name"]
                if name not in cmdr_set and name not in seen:
                    seen.add(name)
                    results.append(
                        PortComplement(
                            rule_id="scaling",
                            direction="synergy",
                            candidate=name,
                            cmdr_event="scales_toughness",
                            cand_event="high_toughness",
                        )
                    )

        # LifeOppsLostThisTurn -> drain/damage effects
        elif "LifeOppsLost" in ev:
            cur = conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'effect' AND event_class IN "
                "('LoseLife', 'DealDamage')"
            )
            for r in cur.fetchall():
                name = r["card_name"]
                if name not in cmdr_set and name not in seen:
                    seen.add(name)
                    results.append(
                        PortComplement(
                            rule_id="scaling",
                            direction="synergy",
                            candidate=name,
                            cmdr_event="scales_opp_life_lost",
                            cand_event="drain_damage",
                        )
                    )

    return results


def _find_value_engine_density(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cards matching a commander's typed value engine.

    Covers commanders whose strategy is "put/find/cheat specific card
    types" but whose triggers are too broad for generic port matching:

    Kaalia: Attacks → puts Angel/Demon/Dragon from hand.
    Zur: Attacks → tutors CMC≤3 Enchantment to battlefield.
    Jhoira: SpellCast Historic → draws. Wants Artifacts.
    Xenagos: Pump Creature.Other → wants high-power creatures.

    Extracts type requirements from ChangeZone ``ChangeType`` in
    raw_line, or from ``Card.Historic`` in valid_filter.
    """
    _VALUE_CARD_TYPES = frozenset({"Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Land"})
    # Track types/subtypes with their source pattern label
    wanted: list[tuple[str, str, str]] = []  # (kind, name, label)

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = (p.get("valid_filter") or "").strip()
        raw = str(p.get("raw_line") or "")

        # Pattern 1: ChangeZone effect with typed ChangeType (Kaalia, Zur)
        # Kaalia: 'Creature.Angel+YouCtrl,Creature.Demon+YouCtrl'
        #   → extract subtypes Angel, Demon, Dragon (not base 'Creature')
        # Zur: 'Enchantment.cmcLE3' → extract type Enchantment
        if pt == "effect" and ev == "ChangeZone" and "ChangeType" in raw:
            m = re.search(r"'ChangeType':\s*'([^']+)'", raw)
            if m:
                for part in m.group(1).split(","):
                    segments = part.strip().split("+")[0].split(".")
                    base = segments[0].strip()
                    # If there's a subtype after the dot, prefer it
                    subtype = segments[1].strip() if len(segments) > 1 else ""
                    if (
                        subtype
                        and subtype[0].isupper()
                        and not subtype.startswith("cmc")
                        and subtype not in ("YouCtrl", "YouOwn")
                    ):
                        wanted.append(("subtype", subtype, "value_engine"))
                    elif base in _VALUE_CARD_TYPES and base not in ("Card", "Permanent"):
                        wanted.append(("type", base, "value_engine"))

        # Pattern 2: SpellCast Card.Historic (Jhoira) → Artifacts
        if pt == "trigger" and ev == "SpellCast" and "Historic" in vf:
            wanted.append(("type", "Artifact", "historic"))

    if not wanted:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    _ALLOWED_COLS: dict[str, str] = {"type": "card_types", "subtype": "subtypes"}
    for kind, target, label in sorted(wanted):
        safe = _escape_like(target)
        # Safety: col is from a hardcoded allowlist, not from external data.
        col = _ALLOWED_COLS.get(kind)
        if col is None:
            continue
        cur = conn.execute(
            f"SELECT name FROM cards WHERE {col} LIKE ? ESCAPE '\\'",
            (f"%{safe}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                results.append(
                    PortComplement(
                        rule_id="value_engine",
                        direction="synergy",
                        candidate=name,
                        cmdr_event=label,
                        cand_event=target,
                        filter_group=target,
                    )
                )

    return results


#: Card types eligible for cheat-into-play CMC bonus.
_CHEAT_TYPES: frozenset[str] = frozenset({"Creature", "Artifact", "Enchantment", "Planeswalker"})

#: Maps (kind → SQL column) for type/subtype LIKE queries.
_CHEAT_ALLOWED_COLS: dict[str, str] = {"type": "card_types", "subtype": "subtypes"}


#: Keywords that cause creatures to enter with or gain +1/+1 counters.
_COUNTER_KEYWORDS: tuple[str, ...] = (
    "Modular",
    "Undying",
    "Persist",
    "Evolve",
    "Fabricate",
    "Riot",
)


def _has_counter_interest(cmdr_ports: list[PortRow]) -> bool:
    """Return True if commander cares about +1/+1 counters.

    Detects CounterAdded triggers with P1P1 counter_type, scales_with
    P1P1, or trigger valid_filters referencing P1P1. Does NOT match
    experience counters, charge counters, or generic counter references.
    """
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        vf = (p.get("valid_filter") or "").strip()
        ct = (p.get("counter_type") or "").strip()
        if (
            pt == "trigger"
            and ev in ("CounterAdded", "CounterAddedOnce", "CounterAddedAll")
            and (not ct or ct == "P1P1")
        ):
            return True
        # scales_with must explicitly reference P1P1 (not Experience, Charge, etc.)
        if pt == "scales_with" and ("P1P1" in ev or "P1P1" in vf):
            return True
        # Marchesa pattern: trigger valid_filter references P1P1 counters
        if pt == "trigger" and "P1P1" in vf:
            return True
        # Ezuri pattern: effect PutCounter with P1P1 on non-self targets.
        # Only if the commander also has a non-self valid_filter (putting
        # counters on OTHER creatures = counter strategy, not secondary bonus).
        if pt == "effect" and ev in ("PutCounter", "PutCounterAll") and ct == "P1P1" and vf and "Other" in vf:
            return True
    return False


def _find_counter_doubler_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find counter doublers for +1/+1 counter commanders.

    Marchesa triggers on CounterAdded -> Hardened Scales adds extra
    +1/+1 counters. Ezuri scales with P1P1 counters -> Doubling Season
    doubles counter placement.

    Matches static Continuous abilities that add or multiply counters.
    N ~ 30-50 (Hardened Scales, Doubling Season, Winding Constrictor, etc.)
    """
    if not _has_counter_interest(cmdr_ports):
        return []

    # Counter doublers are replacement effects with AddCounter event.
    # Hardened Scales, Doubling Season, Winding Constrictor, Branching Evolution.
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'replacement' AND replacement_event = 'AddCounter' "
        "AND (raw_line NOT LIKE '%ValidCard%Card.Self%')"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="counter_doubler",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="CounterAdded_P1P1",
                    cand_event="counter_doubler",
                )
            )

    return results


def _find_counter_producer(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find cards that actively put +1/+1 counters on creatures, for
    commanders whose trigger looks *for* creatures with counters.

    Marchesa triggers when ``Card.YouCtrl+counters_GE1_P1P1`` dies —
    she needs creatures to HAVE counters. Unspeakable Symbol, Thran
    Vigil, Drana, Liberator of Malakir add counters. Ghave, Pir/Toothy
    have similar counter-filter patterns and benefit too.

    Crucially, this rule does NOT fire for commanders who put counters
    themselves (Lathiel, Ezuri, Animar, Hamza) — they are already the
    counter producer and want payoffs, not more producers. Gate on
    trigger valid_filter containing ``counters_`` / ``P1P1`` pattern
    rather than the broader ``_has_counter_interest`` check.

    N ≈ 150-200 (narrow match). IDF-weighted.
    """
    # Narrow gate: commander must have a trigger whose valid_filter looks
    # for creatures that ALREADY have counters. This distinguishes Marchesa
    # (wants counter creatures) from Lathiel (makes counter creatures).
    has_counter_filter_trigger = False
    for p in cmdr_ports:
        if (p.get("port_type") or "").strip() != "trigger":
            continue
        vf = (p.get("valid_filter") or "").strip()
        # Marchesa's "counters_GE1_P1P1", Ghave's tokens-with-counters,
        # generic "+counters_" filters. Require P1P1 so we don't fire for
        # loyalty / charge / experience counter filters.
        if "counters_" in vf and "P1P1" in vf:
            has_counter_filter_trigger = True
            break
    if not has_counter_filter_trigger:
        return []

    # PutCounter / PutCounterAll with P1P1 counter_type targeting creatures.
    cur = conn.execute(
        "SELECT DISTINCT card_name, event_class FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('PutCounter', 'PutCounterAll') "
        "AND counter_type = 'P1P1' "
        "AND (valid_filter LIKE '%Creature%' "
        "     OR valid_filter = '' OR valid_filter IS NULL)"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="counter_producer",
                direction="synergy",
                candidate=name,
                cmdr_event="counters_filter_trigger",
                cand_event=r["event_class"],
            )
        )
    # Also match creatures that enter with +1/+1 counters (etbCounter:P1P1:N)
    # — Iron Apprentice, Walking Ballista, Forgotten Ancient — because they
    # satisfy Marchesa's counters_GE1_P1P1 filter on death / trigger.
    # Separate cand_event so the 301 etbCounter cards get their own IDF
    # group and don't dilute the PutCounter matches.
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class LIKE 'etbCounter:P1P1:%'"
    )
    for r in cur.fetchall():
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="counter_producer",
                direction="synergy",
                candidate=name,
                cmdr_event="counters_filter_trigger",
                cand_event="etbCounter_P1P1",
            )
        )
    return results


def _find_counter_keyword_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Find creatures with counter-matters keywords for +1/+1 counter commanders.

    Marchesa triggers on CounterAdded -> Modular creatures enter with
    counters and move them on death. Ezuri puts counters on creatures ->
    Undying creatures return with +1/+1 counters when they die.

    Keywords: Modular, Undying, Persist, Evolve, Fabricate, Riot.
    N ~ 80-120.
    """
    if not _has_counter_interest(cmdr_ports):
        return []

    placeholders = ",".join("?" * len(_COUNTER_KEYWORDS))
    cur = conn.execute(
        f"SELECT DISTINCT card_name, event_class FROM card_ports "
        f"WHERE port_type = 'keyword' AND event_class IN ({placeholders})",
        _COUNTER_KEYWORDS,
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set and name not in seen:
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="counter_keyword",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="CounterAdded_P1P1",
                    cand_event=r["event_class"],
                )
            )

    return results


def _find_cheat_cmc_bonus(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """CMC-scaled bonus for cheat-into-play commanders.

    Kaalia puts Angel/Demon/Dragon from hand to battlefield — cheating
    a CMC-7 Gisela saves 7 mana, while cheating a CMC-2 creature saves
    almost nothing.  This rule adds complements bucketed by CMC so that
    high-CMC type matches score substantially higher.

    Only covers commanders that put permanents onto the battlefield
    WITHOUT paying mana cost (Kaalia, Elvish Piper, Quicksilver
    Amulet).  MayPlay from graveyard (Karador, Muldrotha) still pays
    mana cost, so CMC-scaling is wrong for them.

    Skips types with CMC restrictions in ChangeType (e.g. Zur's
    ``cmcLE3`` — he only cheats cheap enchantments, so high CMC is
    irrelevant).

    CMC 6+: filter_group="cmc_high", N≈150-200, IDF≈0.14
    CMC 4-5: filter_group="cmc_mid", N≈100-150, IDF≈0.15
    CMC 0-3: no complement (cheap cards don't benefit from cheating)
    """
    wanted: list[tuple[str, str]] = []  # (kind, target_name)

    # Detect whether the commander has any effect_conditional trigger.
    # Meren's end-step reanimate is conditional (XP ≥ CMC) — cheating
    # high-CMC targets only works once she has enough XP, so skip.
    has_cond_trigger = any(
        (p.get("port_type") or "").strip() == "trigger" and p.get("effect_conditional") for p in cmdr_ports
    )

    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        raw = str(p.get("raw_line") or "")

        # ChangeZone effect with typed ChangeType (Kaalia-style free cheat)
        if pt == "effect" and ev == "ChangeZone" and "ChangeType" in raw:
            zo = (p.get("zone_origin") or "").strip()
            zd = (p.get("zone_destination") or "").strip()
            if zd != "Battlefield" or zo not in ("Hand", "Graveyard", "Library"):
                continue
            m = re.search(r"'ChangeType':\s*'([^']+)'", raw)
            if not m:
                continue
            # Skip types with CMC restrictions (cmcLE3, cmcGE7, etc.)
            # Zur cheats cmcLE3 Enchantments — high-CMC bonus is wrong.
            change_type_str = m.group(1)
            if re.search(r"cmc[LG]E\d+", change_type_str):
                continue
            for part in change_type_str.split(","):
                segments = part.strip().split("+")[0].split(".")
                base = segments[0].strip()
                subtype = segments[1].strip() if len(segments) > 1 else ""
                if (
                    subtype
                    and subtype[0].isupper()
                    and not subtype.startswith("cmc")
                    and subtype not in ("YouCtrl", "YouOwn")
                ):
                    wanted.append(("subtype", subtype))
                elif base in _CHEAT_TYPES:
                    wanted.append(("type", base))

        # ChangeZone effect in execute branch with typed ValidTgts
        # (Sharuum-style: ETB trigger → reanimate target Artifact from GY).
        # Only for non-conditional triggers — Meren's XP-gated reanimate
        # shouldn't broadcast high-CMC synergy to every expensive card.
        elif (
            pt == "effect"
            and ev == "ChangeZone"
            and (p.get("branch_kind") or "").strip() == "execute"
            and (p.get("zone_origin") or "").strip() == "Graveyard"
            and (p.get("zone_destination") or "").strip() == "Battlefield"
            and not has_cond_trigger
        ):
            vf = (p.get("valid_filter") or "").strip()
            if not vf:
                continue
            # Extract the base type from "Artifact.YouCtrl" / "Creature.YouOwn"
            for part in vf.split(","):
                base = part.strip().split(".")[0].split("+")[0].strip()
                if base in _CHEAT_TYPES:
                    wanted.append(("type", base))

    if not wanted:
        return []

    return _query_cheat_cmc_brackets(conn, wanted, cmdr_set)


#: CMC brackets for cheat-into-play scoring: (min_cmc, max_cmc, label).
_CMC_BRACKETS: tuple[tuple[int, int | None, str], ...] = (
    (6, None, "cmc_high"),
    (4, 6, "cmc_mid"),
)


def _query_cheat_cmc_brackets(
    conn: sqlite3.Connection,
    wanted: list[tuple[str, str]],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Query cards matching wanted types within each CMC bracket."""
    results: list[PortComplement] = []
    seen: set[str] = set()

    for kind, target in sorted(set(wanted)):
        col = _CHEAT_ALLOWED_COLS.get(kind)
        if col is None:
            continue
        safe = _escape_like(target)

        for min_cmc, max_cmc, label in _CMC_BRACKETS:
            if max_cmc is None:
                sql = f"SELECT name FROM cards WHERE {col} LIKE ? ESCAPE '\\' AND cmc >= ?"
                params: tuple[str | int, ...] = (f"%{safe}%", min_cmc)
            else:
                sql = f"SELECT name FROM cards WHERE {col} LIKE ? ESCAPE '\\' AND cmc >= ? AND cmc < ?"
                params = (f"%{safe}%", min_cmc, max_cmc)

            for r in conn.execute(sql, params).fetchall():
                name = r["name"]
                if name not in cmdr_set and name not in seen:
                    seen.add(name)
                    results.append(
                        PortComplement(
                            rule_id="cheat_cmc",
                            direction="synergy",
                            candidate=name,
                            cmdr_event="cheat_into_play",
                            cand_event=target,
                            filter_group=label,
                        )
                    )

    return results


def _find_cost_reduction_targets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """High-CMC creatures for cost-reduction commanders.

    Rakdos reduces creature spell costs by life lost → expensive
    creatures (Eldrazi, Blightsteel Colossus) benefit most. N ≈ 2249,
    quality-multiplied 0.5x.
    """
    has_reduce_cost = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "static" and ev == "ReduceCost":
            raw = str(p.get("raw_line") or "")
            if "Creature" in raw or "Spell" in raw:
                has_reduce_cost = True
                break

    if not has_reduce_cost:
        return []

    cur = conn.execute("SELECT name FROM cards WHERE card_types LIKE '%Creature%' AND cmc >= 6")
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="cost_reduction_target",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="ReduceCost",
                    cand_event="high_cmc_creature",
                )
            )
    return results


def _find_pinger_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Repeatable damage effects for damage-scaling commanders.

    Rakdos scales with ``LifeOppsLostThisTurn`` → pingers (Spear
    Spewer, Thermo-Alchemist) enable casting by dealing damage each
    turn. N ≈ 1228, IDF ≈ 0.10.
    """
    has_damage_scaling = False
    for p in cmdr_ports:
        pt = (p.get("port_type") or "").strip()
        ev = (p.get("event_class") or "").strip()
        if pt == "scales_with" and ("LifeOppsLost" in ev or "LifeLost" in ev):
            has_damage_scaling = True
            break

    if not has_damage_scaling:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('DealDamage', 'DamageAll', 'LoseLife') "
        "AND (valid_filter LIKE '%Opp%' OR valid_filter LIKE '%Each%' "
        "     OR valid_filter = '' OR valid_filter IS NULL)"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="pinger",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="damage_scaling",
                    cand_event="DealDamage",
                )
            )
    return results


def _find_toughness_matters(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Defender creatures for toughness-scaling commanders.

    Phenax taps creatures to mill equal to toughness → Defenders
    (high toughness, can't attack anyway) are ideal. N ≈ 307,
    IDF ≈ 0.12.
    """
    has_toughness_scaling = any(
        (p.get("port_type") or "").strip() == "scales_with" and "Toughness" in ((p.get("event_class") or "").strip())
        for p in cmdr_ports
    )
    if not has_toughness_scaling:
        return []

    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports WHERE port_type = 'keyword' AND event_class = 'Defender'"
    )
    results: list[PortComplement] = []
    for r in cur.fetchall():
        name = r["card_name"]
        if name not in cmdr_set:
            results.append(
                PortComplement(
                    rule_id="toughness_synergy",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="toughness_scaling",
                    cand_event="Defender",
                )
            )
    return results
