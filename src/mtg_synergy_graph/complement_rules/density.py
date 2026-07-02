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
from .core import (
    _AFFECTED_CLAUSE_RE,
    _VALID_TARGET_CLAUSE_RE,
    _VANILLA_TRIBAL_SKIPLIST,
    PortComplement,
    PortRow,
    _commander_subtypes_from_ports,
)

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
    # Payoff direction: lords/anthems targeting the commander's token
    # output are genuine synergy even for skiplisted tribes (Adeline
    # makes Human tokens -> Human anthems pump them), so the overbroad-
    # tribe restriction that guards tribal_density does not apply here.
    cmdr_subtypes = _commander_subtypes_from_ports(
        conn,
        list(cmdr_set),
        cmdr_ports,
        include_overbroad_tribes=True,
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

    Universal filters (``Card`` or empty) on either side represent
    "watches any spell cast" — Alela, Rashmi, Forgotten Ancient. They
    match any specific type because the trigger literally fires on every
    cast. When paired with a typed commander they adopt the commander's
    type for IDF bucketing; a universal × universal pair bucket is
    tagged ``Any``.

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

    # Extract type filters from commander SpellCast triggers.
    # ``cmdr_universal`` is set when the commander has any SpellCast
    # trigger with an empty or ``Card`` filter — Alela / Rashmi fire on
    # every spell cast regardless of type.
    cmdr_types: set[str] = set()
    cmdr_subtypes: set[str] = set()
    cmdr_universal = False
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev not in ("SpellCast", "SpellCastOrCopy"):
            continue
        vf = (p.get("valid_filter") or "").strip()
        if not vf or vf == "Card":
            cmdr_universal = True
            continue
        for alt in vf.split(","):
            alt = alt.strip()
            if not alt or alt.startswith("Card.Self"):
                continue
            if alt == "Card":
                cmdr_universal = True
                continue
            if "nonCreature" in alt or "non-Creature" in alt:
                cmdr_types.update({"Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker"})
                continue
            base = alt.split(".")[0].split("+")[0].strip()
            if base in _CASTABLE_TYPES:
                cmdr_types.add(base)
            elif base and base[0].isupper() and base not in ("Card", "Permanent"):
                cmdr_subtypes.add(base)

    if not cmdr_types and not cmdr_subtypes and not cmdr_universal:
        return []

    # Find candidate SpellCast triggers. Widen the scan to include
    # empty / NULL filters: those are universal candidates (Forgotten
    # Ancient's ``Card``, a blank filter = same semantics) that used to
    # be silently dropped by the SQL guard.
    cur = conn.execute(
        "SELECT DISTINCT card_name, valid_filter FROM card_ports "
        "WHERE port_type = 'trigger' "
        "AND event_class IN ('SpellCast', 'SpellCastOrCopy')"
    )
    results: list[PortComplement] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        card = r["card_name"]
        if card in cmdr_set or card in seen:
            continue
        vf = (r["valid_filter"] or "").strip()
        if vf.startswith("Card.Self"):
            continue
        cand_universal = not vf or vf == "Card"
        matched_type = ""
        # Only universal × universal pairs are allowed through the
        # universal path. Mixed pairs (typed cmdr × universal cand, or
        # universal cmdr × typed cand) regressed the golden set in
        # audit: Tuvasa / Sram / Veyran lost NDCG when universal
        # candidates polluted their typed-typed bucket, and Rashmi
        # (universal cmdr) lost NDCG when typed enchantress candidates
        # displaced her real Hi-Syn staples (Rashmi's payoffs are
        # draw-spell value, not on-cast trigger payoffs). Universal ×
        # universal still unlocks Alela ↔ Arjun / Forgotten Ancient
        # and similar hub-on-hub pairs that were previously dropped.
        if cand_universal:
            if cmdr_universal:
                matched_type = "Universal"
        else:
            for alt in vf.split(","):
                base = alt.strip().split(".")[0].split("+")[0].strip()
                if not base:
                    continue
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


#: Plan 2026-07-02-002 Unit 5: payoff/body two-tier tribal emission.
#: When False (module default), every tribe member emits rule_id
#: ``tribal_density`` at the single flat weight — bitwise-inert legacy
#: behavior. When True, tribe members whose own ports reference the
#: tribe in structured fields (valid_filter/affected_scope: tribal
#: triggers, lords-on-bodies) keep ``tribal_density``; mere same-type
#: bodies emit ``tribal_body`` at a materially lower flat weight.
#: Mandated by the Unit 4 null result: uniform within-family haircuts
#: cannot separate flood-as-noise from flood-as-archetype — the
#: payoff/body distinction is candidate-side evidence that can.
_ENABLE_TRIBAL_PAYOFF_TIER: bool = False


def _tribal_payoff_names(conn: sqlite3.Connection, sub: str) -> set[str]:
    """Card names whose ports reference ``sub`` in structured fields.

    Structured fields only (valid_filter / affected_scope) — raw_line
    prose would re-admit TriggerDescription noise (the Unit 2 lesson).
    """
    pat = f"%{_escape_like(sub)}%"
    cur = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE valid_filter LIKE ? ESCAPE '\\' OR affected_scope LIKE ? ESCAPE '\\'",
        (pat, pat),
    )
    return {r["card_name"] for r in cur.fetchall()}


def _vanilla_tribal_subtypes(conn: sqlite3.Connection, commander_set: set[str]) -> set[str]:
    """Return creature subtypes for a vanilla-anchor commander.

    A *vanilla anchor* is a legendary creature whose card has no
    mechanical port structure — only keywords (Flying / Trample /
    Protection) or nothing at all. Akroma, Angel of Wrath / Ghalta,
    Primal Hunger / Rorix Bladewing / Grumgully, the Generous are the
    canonical shape. Their EDHREC Hi-Syn is dominated by their tribe
    (Angels / Dinosaurs / Dragons / Goblins) because the deck *is* the
    tribe — the commander has nothing else to build around.

    Returns an empty set when:
    - Commander has any non-keyword ports (trigger / effect / static /
      replacement / scales_with / cost). Their mechanical structure
      will drive the synergy; layering tribal on top dilutes.
    - Commander's only subtypes are in ``_VANILLA_TRIBAL_SKIPLIST``
      (Human / Warrior / Soldier — pools too large or not the recognized
      EDHREC axis).
    - Commander has no creature subtype at all (eldrazi shape etc).
    """
    # Two-step SQL (port_types, then subtypes) is intentional: the
    # port_types probe is cheap and short-circuits the subtype lookup
    # for every commander with mechanical structure — the majority
    # case. A single JOIN would always pay the cost of hitting the
    # cards table. Fused into one query only if batch profiling shows
    # the two round trips matter.
    placeholders = ",".join("?" * len(commander_set))
    params = tuple(commander_set)
    rows = conn.execute(
        f"SELECT DISTINCT port_type FROM card_ports WHERE card_name IN ({placeholders})",
        params,
    ).fetchall()
    port_types = {r["port_type"] for r in rows if r["port_type"]}
    # Allow "keyword" only. Any other port type means the commander has
    # mechanical structure worth matching directly.
    if port_types and not port_types.issubset({"keyword"}):
        return set()

    subtype_rows = conn.execute(
        f"SELECT subtypes FROM cards WHERE name IN ({placeholders})",
        params,
    ).fetchall()
    literal: set[str] = set()
    for r in subtype_rows:
        if r["subtypes"]:
            literal.update(r["subtypes"].split())
    return literal - _VANILLA_TRIBAL_SKIPLIST


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
    subtypes like Saproling for Slimefoot). Falls back to the
    commander's literal subtypes when the card is a *vanilla anchor*
    (keywords-only, no mechanical ports — Akroma, Ghalta, Rorix) since
    their EDHREC Hi-Syn is dominated by their tribe and no other rule
    emits a match for them.
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
        subtypes = _vanilla_tribal_subtypes(conn, cmdr_set)
    if not subtypes:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()
    for sub in subtypes:
        payoffs = _tribal_payoff_names(conn, sub) if _ENABLE_TRIBAL_PAYOFF_TIER else None
        cur = conn.execute(
            "SELECT name FROM cards WHERE subtypes LIKE ? AND card_types LIKE '%Creature%'",
            (f"%{sub}%",),
        )
        for r in cur.fetchall():
            name = r["name"]
            if name not in cmdr_set and name not in seen:
                seen.add(name)
                rule_id = "tribal_density"
                if payoffs is not None and name not in payoffs:
                    rule_id = "tribal_body"
                results.append(
                    PortComplement(
                        rule_id=rule_id,
                        direction="synergy",
                        candidate=name,
                        cmdr_event="tribal",
                        cand_event=sub,
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


def _statics_all_self_focused(cmdr_ports: list[PortRow]) -> bool:
    """True when every one of the commander's static ports names
    ``Card.Self`` in its ``Affected`` (self-pump) or ``ValidTarget``
    (self-protection) clause.

    Used by the scales-with-creature-count branch to keep Shanna,
    Sisay's Legacy (self-pump Continuous + CantTarget Card.Self) in
    scope while excluding Ghalta-style ``ReduceCost ValidCard
    Card.Self`` — the ``ValidCard`` slot names a *cast* target, not an
    affected permanent, so the clause regex deliberately doesn't match
    it.
    """
    for cp in cmdr_ports:
        if (cp.get("port_type") or "").strip() != "static":
            continue
        raw = str(cp.get("raw_line") or "")
        aff_m = _AFFECTED_CLAUSE_RE.search(raw)
        tgt_m = _VALID_TARGET_CLAUSE_RE.search(raw)
        self_focused = (aff_m and aff_m.group(1) == "Card.Self") or (tgt_m and tgt_m.group(1) == "Card.Self")
        if not self_focused:
            return False
    return True


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

    # Commander-level invariants hoisted out of the per-port loop: the
    # scales-with-creature-count branch gates on these but they don't
    # change across scales_with ports.
    _cmdr_pts: set[str] | None = None
    _cmdr_static_self_focused: bool | None = None

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

        # Valid Creature.YouCtrl (with no counter qualifier) -> token
        # producers + populate spells. Shanna, Sisay's Legacy scales
        # P/T with creature count; Queen Allenal / Rootborn Defenses /
        # Sundering Growth / Wayfaring Temple / Raise the Alarm all
        # feed her scaling. The counter-axis branch above handles
        # Hamza / Marchesa; this branch handles pure creature-count
        # scalers with *no other mechanical ports* — commanders whose
        # whole identity is "get bigger with more creatures".
        #
        # Narrow gate: commander must have no trigger / effect / cost /
        # replacement ports. This keeps attack-trigger token-makers
        # (Adeline) and tap-for-mana engines (Selvala) out — their
        # Hi-Syn is attack payoffs / mana-doublers, not raw token
        # producers, and the broad token pool flattens their top-30.
        elif ev == "Valid" and vf.startswith("Creature.YouCtrl") and "counters" not in vf:
            # Skip subtype-qualified filters (Creature.YouCtrl+Zombie)
            # where tribal_density is the better axis.
            subtype_qualifier = vf[len("Creature.YouCtrl") :]
            if subtype_qualifier.startswith("+"):
                continue
            # Compute commander-level invariants once per commander, not
            # once per scales_with port. Lazy init — only pay the cost
            # when this branch is reached.
            if _cmdr_pts is None:
                _cmdr_pts = {(cp.get("port_type") or "").strip() for cp in cmdr_ports}
            if _cmdr_pts & {"trigger", "effect", "cost", "replacement"}:
                continue
            # Commander's statics must all be self-focused: either
            # ``Affected: Card.Self`` (self-pump Continuous) or
            # ``ValidTarget: Card.Self`` (self-protection CantTarget).
            # This excludes Ghalta-style ``ReduceCost ValidCard
            # Card.Self`` — the ``ValidCard`` slot names a *cast*
            # target, not an affected permanent, and the archetype is
            # "cheat big creatures into play" not "make more
            # creatures". Anthems / MayPlay statics that affect
            # other permanents also fail this gate.
            if _cmdr_static_self_focused is None:
                _cmdr_static_self_focused = _statics_all_self_focused(cmdr_ports)
            if not _cmdr_static_self_focused:
                continue
            cur = conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'effect' AND event_class = 'Token' "
                "AND (raw_line LIKE '%creature%' OR raw_line LIKE '%Creature%')"
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
                            cmdr_event="scales_creature_count",
                            cand_event="token_producer",
                        )
                    )
            # Populate spells (CopyPermanent with Populate:True in raw)
            cur2 = conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'effect' AND event_class = 'CopyPermanent' "
                "AND raw_line LIKE '%Populate%'"
            )
            for r in cur2.fetchall():
                name = r["card_name"]
                if name not in cmdr_set and name not in seen:
                    seen.add(name)
                    results.append(
                        PortComplement(
                            rule_id="scaling",
                            direction="synergy",
                            candidate=name,
                            cmdr_event="scales_creature_count",
                            cand_event="populate",
                        )
                    )

        # Domain -> cards that add multiple basic land types (Prismatic
        # Omen, Dryad of the Ilysian Grove, Nylea's Presence). Radha,
        # Coalition Warlord / Nael, Avizoa Aeronaut style commanders
        # scale by basic-land-type count — a card that makes a single
        # land all five types converts ``Domain = 1`` into
        # ``Domain = 5``. Narrow pool (~10 cards) matched by the
        # Forge-specific ``AllBasicLandType`` marker or a multi-type
        # ``AddType`` clause joined with ``&``. Blood Moon / Magus of
        # the Moon (single-type ``Mountain`` with ``RemoveLandTypes``)
        # are rejected — they *remove* Domain diversity rather than
        # adding it.
        elif ev == "Domain":
            cur = conn.execute(
                "SELECT DISTINCT card_name FROM card_ports "
                "WHERE port_type = 'static' AND event_class = 'Continuous' "
                "AND raw_line LIKE '%Land%' "
                "AND ("
                "    raw_line LIKE '%AllBasicLandType%'"
                " OR ("
                "       raw_line LIKE '%AddType%' AND raw_line LIKE '%&%'"
                "   AND raw_line NOT LIKE '%RemoveLandTypes%'"
                "   AND ("
                "         raw_line LIKE '%Forest%' OR raw_line LIKE '%Plains%'"
                "      OR raw_line LIKE '%Island%' OR raw_line LIKE '%Swamp%'"
                "      OR raw_line LIKE '%Mountain%'"
                "   )"
                " )"
                ")"
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
                            cmdr_event="scales_domain",
                            cand_event="land_type_adder",
                        )
                    )

        # LifeOppsLostThisTurn -> repeatable drain/damage sources.
        # DamageAll is included so global pingers (Spear Spewer,
        # Pyrohemia, Repercussion) match. Instants/Sorceries are
        # excluded: one-shot burn (Lightning Bolt, Shock, Blazing
        # Volley) fires once per cast rather than each turn, and the
        # flat 0.3 density weight drowns true repeatable enablers
        # like Creeping Bloodsucker / Stormfist Crusader.
        elif "LifeOppsLost" in ev:
            cur = conn.execute(
                "SELECT DISTINCT cp.card_name FROM card_ports cp "
                "JOIN cards c ON c.name = cp.card_name "
                "WHERE cp.port_type = 'effect' AND cp.event_class IN "
                "('LoseLife', 'DealDamage', 'DamageAll') "
                "AND c.card_types NOT LIKE '%Instant%' "
                "AND c.card_types NOT LIKE '%Sorcery%'"
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


#: Minimum CMC for a cheat-into-play bonus. Cheating a CMC-8 creature
#: saves 8 mana; cheating a CMC-4 creature saves only 4. Kaalia's
#: actual EDHREC hi-syn (Avacyn / Gisela / Rune-Scarred / Vilis /
#: Balefire) is all CMC ≥ 6, so that's where the bonus fires.
#:
#: History: a ``cmc_mid`` bracket (4 ≤ cmc < 6) was dropped 2026-04-21.
#: Its smaller pool gave CMC 4-5 cards higher per-card IDF than
#: CMC ≥ 6, inverting the mana-saved semantic — Nightmare Shepherd
#: (CMC 5 Demon) outscored Avacyn (CMC 8 Angel) on Kaalia. CMC 4-5
#: cheats still score via trigger_effect on their ChangesZone-into-play
#: triggers, just without an extra cheat_cmc weight.
_CHEAT_CMC_MIN: int = 6
_CHEAT_CMC_LABEL: str = "cmc_high"


def _query_cheat_cmc_brackets(
    conn: sqlite3.Connection,
    wanted: list[tuple[str, str]],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Emit a cheat_cmc complement for each unique cheat-target (kind,
    target) pair whose CMC meets ``_CHEAT_CMC_MIN``.

    All emissions share ``cand_event='cheatable'`` and
    ``filter_group='cmc_high'`` so Angel / Demon / Dragon land in one
    IDF pool per invocation (see docstring on ``_CHEAT_CMC_MIN`` for
    why per-subtype bucketing was removed).
    """
    results: list[PortComplement] = []
    seen: set[str] = set()

    for kind, target in sorted(set(wanted)):
        col = _CHEAT_ALLOWED_COLS.get(kind)
        if col is None:
            continue
        safe = _escape_like(target)
        rows = conn.execute(
            f"SELECT name FROM cards WHERE {col} LIKE ? ESCAPE '\\' AND cmc >= ?",
            (f"%{safe}%", _CHEAT_CMC_MIN),
        ).fetchall()
        for r in rows:
            name = r["name"]
            if name in cmdr_set or name in seen:
                continue
            seen.add(name)
            results.append(
                PortComplement(
                    rule_id="cheat_cmc",
                    direction="synergy",
                    candidate=name,
                    cmdr_event="cheat_into_play",
                    cand_event="cheatable",
                    filter_group=_CHEAT_CMC_LABEL,
                )
            )

    return results


def _find_cost_reduction_targets(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """High-CMC creatures for cost-reduction commanders.

    Rakdos reduces creature spell costs by life lost → expensive
    creatures (Eldrazi, Blightsteel Colossus) benefit most. N ≈ 1624
    after quality gate, weight 1.0× (no IDF dampening).

    Quality gate: creature must carry at least one trigger or effect
    port — pure keyword-vanilla fatties (Colossal Dreadmaw, Baneslayer
    Angel) are generic and shouldn't outrank true value engines
    (Kozilek, Artisan, Vilis). Creatures whose utility is a damage or
    life-loss effect (Marionette Master, Kokusho, Dreadhound) are
    excluded because those are already captured by the ``pinger`` and
    ``scaling`` rules for damage-scaling commanders.

    Candidate pool is commander-independent — provided via
    ``candidate_cache.cost_reduction_target_pool`` to avoid a 650 ms
    correlated EXISTS/NOT EXISTS subquery per commander.
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

    if candidate_cache is not None:
        pool: frozenset[str] | set[str] = candidate_cache.cost_reduction_target_pool
    else:
        pool = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM cards c WHERE c.card_types LIKE '%Creature%' AND c.cmc >= 6 "
                "AND EXISTS (SELECT 1 FROM card_ports cp WHERE cp.card_name = c.name "
                "            AND cp.port_type IN ('trigger', 'effect')) "
                "AND NOT EXISTS (SELECT 1 FROM card_ports cp2 WHERE cp2.card_name = c.name "
                "                AND cp2.port_type = 'effect' "
                "                AND cp2.event_class IN ('DealDamage', 'DamageAll', 'LoseLife'))"
            ).fetchall()
        }

    results: list[PortComplement] = []
    for name in pool:
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


def _filter_has_land_base_type(valid_filter: str) -> bool:
    """True iff any comma-alt in ``valid_filter`` has base type ``Land``.

    Substring checks like ``"Land" in valid_filter`` false-positive on
    ``Creature.nonLand+YouCtrl`` (Princess Yue's dies trigger) and on
    ``Permanent.nonLand+YouOwn`` (most generic reanimator effects —
    Emeria Shepherd, Moira and Teshar, Pull Through the Weft, etc.).
    A Forge filter is ``BaseType[.Subtype][+Qualifier...]``, so the
    base is the first token before ``.`` or ``+``. Only accept alts
    where the base equals exactly ``Land``.
    """
    if not valid_filter:
        return False
    for alt in valid_filter.split(","):
        base = alt.strip().split(".")[0].split("+")[0].strip()
        if base == "Land":
            return True
    return False


def _find_land_to_gy_synergy(
    conn: sqlite3.Connection,
    cmdr_ports: list[PortRow],
    cmdr_set: set[str],
) -> list[PortComplement]:
    """Land-to-graveyard payoffs for Gitrog / Titania Voice of Gaea.

    These commanders trigger on ``ChangesZoneAll Any→Graveyard`` with
    ``valid_filter='Land*'``. Their mechanical demand is:

    1. Feeders — cards that put lands in the graveyard (sac-a-land
       costs, dredge). These directly feed the commander's trigger.
    2. Recursion — cards that use lands FROM the graveyard (MayPlay
       statics like Crucible of Worlds / Ramunap Excavator, or
       GY→Hand/Battlefield ChangeZone effects like Life from the Loam
       / Splendid Reclamation). Secondary payoff: lands keep
       cycling through the graveyard.

    Split into two ``filter_group`` buckets so IDF sizes each pool
    separately (feeders ≈ 120 cards, recursion ≈ 56 cards).

    Gate population is 2 commanders by design — narrow high-signal
    rule, same pattern as ``cascade_tribal``.
    """
    has_land_gy_trigger = any(
        (p.get("port_type") or "").strip() == "trigger"
        and (p.get("event_class") or "").strip() in ("ChangesZoneAll", "ChangesZone")
        and (p.get("zone_destination") or "").strip() == "Graveyard"
        and _filter_has_land_base_type((p.get("valid_filter") or "").strip())
        for p in cmdr_ports
    )
    if not has_land_gy_trigger:
        return []

    results: list[PortComplement] = []
    seen: set[str] = set()

    feeder_rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE (port_type='cost' AND event_class='sacrifice' "
        "       AND raw_line LIKE '%/Land>%') "
        "   OR (port_type='keyword' AND event_class LIKE 'Dredge%')"
    )
    for r in feeder_rows:
        name = r["card_name"]
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="land_to_gy_synergy",
                direction="synergy",
                candidate=name,
                cmdr_event="land_to_graveyard",
                cand_event="feeder",
                filter_group="feeder",
            )
        )

    # MayPlay Land statics: scoped to raw_line carrying ``Land.YouOwn``
    # + MayPlay + Graveyard — the Crucible of Worlds / Ramunap Excavator
    # shape. ChangeZone Land recursion is fetched separately so we can
    # apply the base-type check in Python to reject compound filters
    # like ``Permanent.nonLand,Land.YouOwn`` (safe) vs. bare
    # ``Permanent.nonLand+YouOwn`` (Emeria Shepherd — reject).
    static_recursion_rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type='static' AND event_class='Continuous' "
        "  AND raw_line LIKE '%Land.YouOwn%' "
        "  AND raw_line LIKE '%MayPlay%' "
        "  AND raw_line LIKE '%Graveyard%' "
        "  AND raw_line NOT LIKE '%nonLand%'"
    )
    effect_recursion_rows = conn.execute(
        "SELECT DISTINCT card_name, valid_filter FROM card_ports "
        "WHERE port_type='effect' AND event_class='ChangeZone' "
        "  AND zone_origin='Graveyard' "
        "  AND zone_destination IN ('Hand','Battlefield') "
        "  AND valid_filter LIKE '%Land%' "
        "  AND valid_filter NOT LIKE '%nonLand%'"
    )
    recursion_names: set[str] = {r["card_name"] for r in static_recursion_rows}
    for r in effect_recursion_rows:
        if _filter_has_land_base_type(r["valid_filter"] or ""):
            recursion_names.add(r["card_name"])
    for name in recursion_names:
        if name in cmdr_set or name in seen:
            continue
        seen.add(name)
        results.append(
            PortComplement(
                rule_id="land_to_gy_synergy",
                direction="synergy",
                candidate=name,
                cmdr_event="land_to_graveyard",
                cand_event="recursion",
                filter_group="recursion",
            )
        )

    return results
