"""Penalty layer (SPEC §4.5).

Phase 4.5 implements the full §4.5 12-rule set from the LightGBM
``_apply_penalties()`` reference. The rules are bulk-evaluated against a
:class:`PenaltyContext` so the engine pays one SQL pass per page request,
not one per candidate.

Rules:

==  =========================================  ===========================
#   Rule                                       Multiplier
==  =========================================  ===========================
1   Wrong colour identity                      hard filter (-1e9)
2   Background / Doctor's companion legality   hard filter (-1e9)
3   Required-subtype mismatch                  ×0.4
4   Excluded subtypes                          ×0.3
5   Wrong token type                           ×0.5
6   Wrong counter type                         ×0.4
7   Niche counter                              ×0.4
8   Counters on lands                          ×0.4
9   Non-counter creatures for counter cmdrs    ×0.6
10  Opponent-only mill replacement             ×0.3
11  Unmet Type$ need / hint                    ×0.3
12  Unmet Ability$ need                        ×0.85
==  =========================================  ===========================

The CLAUDE.md authoritative implementation in
``packages/mtg-synergy/src/mtg_synergy/recommend/scoring.py`` remains the
edge-case reference; this file ports the core logic at the granularity the
deterministic engine can support without re-using the LightGBM feature
extractors.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: Score sentinel for hard filters. Cards at this score are dropped before
#: ranking, never shown to the consumer.
HARD_FILTER_SCORE = -1e9

#: Counter types that are "niche" — only useful in specific archetypes.
NICHE_COUNTERS: frozenset[str] = frozenset({"TIME", "EXPERIENCE", "ENERGY"})

#: Counter type aliases that all map to "P1P1" for matching purposes.
P1P1_ALIASES: frozenset[str] = frozenset({"P1P1"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {tok.strip() for tok in value.split(",") if tok.strip()}


def _color_identity(rows: Sequence[Any]) -> frozenset[str]:
    """Union of colour identities across the commander set (§6.9.1)."""
    pips: set[str] = set()
    for r in rows:
        pips.update(_parse_csv(r["color_identity"]))
    return frozenset(pips - {""})


def _commander_subtypes(rows: Sequence[Any]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        out.update((r["subtypes"] or "").split())
    return out


def _has_choose_a_background(card_row: Any) -> bool:
    text = (card_row["oracle_text"] or "").lower()
    return "choose a background" in text


def _has_doctor_companion_partner(commander_rows: Sequence[Any]) -> bool:
    """True if the commander set already includes 'The Doctor', i.e. it's
    legal to add a Doctor's companion."""
    return any("Doctor" in (r["subtypes"] or "").split() for r in commander_rows)


def _decode_json_field(value: Any) -> dict[str, list[str]]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {k: list(v) for k, v in value.items()}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {k: list(v) for k, v in decoded.items() if isinstance(v, list)}


# ---------------------------------------------------------------------------
# Filter parsing for non<X> exclusions (rule 4)
# ---------------------------------------------------------------------------

_NON_PREFIX = "non"


def _exclusion_tokens(filter_str: str | None) -> set[str]:
    """Return the ``non<X>`` tokens in a Forge filter string.

    >>> _exclusion_tokens("Creature.nonHuman+!Black")
    {'Human'}
    >>> _exclusion_tokens("Permanent.nonLand+Other")
    {'Land'}
    """
    if not filter_str:
        return set()
    out: set[str] = set()
    for segment in filter_str.replace("+", ".").split("."):
        seg = segment.strip()
        if seg.startswith(_NON_PREFIX) and len(seg) > len(_NON_PREFIX) and seg[len(_NON_PREFIX)].isupper():
            out.add(seg[len(_NON_PREFIX) :])
    return out


# ---------------------------------------------------------------------------
# Token-script parsing (rule 5)
# ---------------------------------------------------------------------------

#: Forge token scripts come in two layouts:
#:
#:   ``g_1_1_insect``       — colour, power, toughness, subtype
#:   ``c_a_treasure_sac``   — colour, type (a=artifact), subtype, trailing modifier
#:
#: For creature tokens the subtype is the 4th underscore-separated segment.
#: For artifact tokens (Treasure / Clue / Food / etc.) the subtype is the 3rd
#: segment.


def _token_subtype(script: str | None) -> str | None:
    """Best-effort subtype extraction from a Forge TokenScript$ value.

    Forge token scripts come in several layouts:

    * Creature:  ``b_1_1_vampire``  → color, P, T, subtype[, keywords…]
    * Artifact creature: ``u_1_1_a_thopter_flying`` → color, P, T, ``a``, subtype[, kw…]
    * Non-creature artifact: ``c_a_treasure_sac`` → color, ``a``, subtype[, modifier]
    * Named token: ``ashaya_the_awoken_world`` → full name (no numeric P/T)

    Returns the first creature/artifact subtype found, capitalised.
    """
    if not script:
        return None
    parts = script.lower().split("_")
    if len(parts) < 3:
        return None
    # Non-creature artifact layout: parts[1] is a single non-digit letter
    # (the artifact type indicator) → subtype is parts[2].
    # e.g. ``c_a_treasure_sac`` → "Treasure"
    if len(parts[1]) == 1 and not parts[1].isdigit():
        return parts[2].capitalize()
    # Creature layout: parts[1..2] are power/toughness digits.
    # If parts[3] == 'a' it's an artifact creature → subtype at parts[4].
    # Otherwise subtype is parts[3].
    # e.g. ``u_1_1_a_thopter_flying`` → "Thopter"
    # e.g. ``b_1_1_vampire``          → "Vampire"
    if len(parts) >= 4:
        if parts[3] == "a" and len(parts) >= 5:
            return parts[4].capitalize()
        return parts[3].capitalize()
    return parts[-1].capitalize()


# ---------------------------------------------------------------------------
# Candidate cache — commander-independent bulk data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateCache:
    """Commander-independent candidate data that can be reused across pages.

    Building this once and passing it to :func:`build_penalty_context`
    avoids repeating ~90 ms of SQL on every call — a 9-second saving
    when running 100 commanders in the golden-set tracker.
    """

    candidate_rows: dict[str, dict[str, Any]]
    creature_static_scopes: dict[str, list[str]]
    candidate_excludes: dict[str, set[str]]
    candidate_token_types: dict[str, set[str]]
    candidate_counter_types: dict[str, set[str]]
    candidate_counters_on_land: dict[str, bool]
    candidate_opp_mill: dict[str, bool]


def build_candidate_cache(conn: sqlite3.Connection) -> CandidateCache:
    """Load all commander-independent candidate data in one pass."""
    return CandidateCache(
        candidate_rows=_bulk_load_candidates(conn),
        creature_static_scopes=_bulk_load_static_scopes(conn),
        candidate_excludes=_bulk_load_excludes(conn),
        candidate_token_types=_bulk_load_token_types(conn),
        candidate_counter_types=_bulk_load_counter_types(conn),
        candidate_counters_on_land=_bulk_load_counters_on_lands(conn),
        candidate_opp_mill=_bulk_load_opp_mill(conn),
    )


# ---------------------------------------------------------------------------
# Penalty context — bulk-loaded once per page request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PenaltyContext:
    """Pre-fetched data for a penalty pass over many candidates.

    Building this once per page lets us avoid issuing one SQL query per
    candidate (which dominated wall time at 32 k cards). The shape is
    deliberately denormalised — every dict is keyed by candidate name so
    the apply step is a constant-time lookup.

    Note: ``frozen=True`` only blocks attribute rebinding. The dict and
    set fields below remain mutable in-place — by convention, callers
    must treat the context as read-only after construction.
    """

    # Commander-side state ---------------------------------------------------
    cmdr_identity: frozenset[str]
    cmdr_subtypes: frozenset[str]
    cmdr_card_types: frozenset[str]
    cmdr_allows_background: bool
    cmdr_has_doctor: bool
    cmdr_counter_types: frozenset[str]  # P1P1 / M1M1 / TIME / etc.
    cmdr_uses_p1p1: bool  # has any P1P1 trigger/effect
    cmdr_p1p1_is_primary: bool  # P1P1 is primary strategy (scales_with)
    cmdr_self_mill: bool  # has Mill effect targeting You
    cmdr_has_abilities: frozenset[str]  # from deck_has + derived
    cmdr_has_types: frozenset[str]  # from deck_has + derived
    cmdr_has_keywords: frozenset[str]
    cmdr_token_subtypes: frozenset[str]  # token types the commander makes
    cmdr_is_tribal: bool  # commander has a tribal anchor:
    # at least one of its own subtypes
    # appears as a token-subtype it
    # generates (Krenko makes Goblins,
    # IS a Goblin) — see
    # build_penalty_context for the
    # exact heuristic

    # Candidate-side state ---------------------------------------------------
    candidate_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    creature_static_scopes: dict[str, list[str]] = field(default_factory=dict)
    candidate_excludes: dict[str, set[str]] = field(default_factory=dict)
    candidate_token_types: dict[str, set[str]] = field(default_factory=dict)
    candidate_counter_types: dict[str, set[str]] = field(default_factory=dict)
    candidate_counters_on_land: dict[str, bool] = field(default_factory=dict)
    candidate_opp_mill: dict[str, bool] = field(default_factory=dict)
    # Phase F9: candidates with sacrifice/trigger-resonance signal —
    # exempt from Rule 9 (non-counter creature penalty) because they
    # are mechanically connected to the sacrifice axis, not counters.
    candidates_with_sacrifice_signal: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Bulk loaders
# ---------------------------------------------------------------------------


def _bulk_load_candidates(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT name, color_identity, subtypes, oracle_text, card_types, "
        "       cmc, deck_needs, deck_hints, deck_has, edhrec_rank FROM cards"
    ).fetchall():
        out[row["name"]] = {
            "name": row["name"],
            "color_identity": row["color_identity"],
            "subtypes": row["subtypes"],
            "oracle_text": row["oracle_text"],
            "card_types": row["card_types"],
            "cmc": row["cmc"],
            "deck_needs": row["deck_needs"],
            "deck_hints": row["deck_hints"],
            "deck_has": row["deck_has"],
            "edhrec_rank": row["edhrec_rank"],
        }
    return out


def _bulk_load_static_scopes(conn: sqlite3.Connection) -> dict[str, list[str]]:
    static_scopes: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT card_name, affected_scope FROM card_ports "
        "WHERE port_type = 'static' AND affected_scope LIKE 'Creature%'"
    ).fetchall():
        static_scopes.setdefault(row["card_name"], []).append(row["affected_scope"])
    return static_scopes


def _bulk_load_excludes(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per-card set of ``non<X>`` tokens parsed from STATIC ``affected_scope``.

    Trigger ``valid_filter`` exclusions (e.g. Mystic Remora's
    ``nonCreature`` watching opponent noncreature spells) are NOT counted —
    they describe the EVENT being watched, not which creatures get buffed
    or excluded from a deck. Only static-port scopes (Angel of Jubilation
    excluding Black creatures from its anthem, etc.) trigger rule 4.
    """
    excludes: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT card_name, affected_scope FROM card_ports WHERE port_type = 'static' AND affected_scope LIKE '%non%'"
    ).fetchall():
        tokens = _exclusion_tokens(row["affected_scope"])
        if tokens:
            excludes.setdefault(row["card_name"], set()).update(tokens)
    return excludes


def _bulk_load_counter_types(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per-card set of counter types appearing in PutCounter effects."""
    out: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT card_name, counter_type FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('PutCounter','PutCounterAll','MoveCounter','Proliferate') "
        "AND counter_type IS NOT NULL AND counter_type <> ''"
    ).fetchall():
        # counter_type may be a comma-separated list (e.g. "P1P1,Trample")
        for tok in (row["counter_type"] or "").split(","):
            tok = tok.strip()
            if tok:
                out.setdefault(row["card_name"], set()).add(tok)
    return out


def _bulk_load_counters_on_lands(conn: sqlite3.Connection) -> dict[str, bool]:
    """True per card if any PutCounter effect targets a Land."""
    out: dict[str, bool] = {}
    for row in conn.execute(
        "SELECT card_name, valid_filter FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('PutCounter','PutCounterAll') "
        "AND valid_filter LIKE '%Land%'"
    ).fetchall():
        out[row["card_name"]] = True
    return out


def _bulk_load_opp_mill(conn: sqlite3.Connection) -> dict[str, bool]:
    """True per card if it has an opponent-only Mill replacement."""
    out: dict[str, bool] = {}
    for row in conn.execute(
        "SELECT card_name, replacement_event, replacement_player FROM card_ports "
        "WHERE port_type = 'replacement' "
        "AND replacement_event = 'Mill' "
        "AND replacement_player LIKE '%Opponent%'"
    ).fetchall():
        out[row["card_name"]] = True
    return out


def _bulk_load_token_types(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per-card set of token subtypes the card creates (parsed from raw_line)."""
    out: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
    ).fetchall():
        raw = row["raw_line"] or ""
        # raw_line is repr(parsed_dict). Look for TokenScript$ value.
        m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
        if m:
            sub = _token_subtype(m.group(1))
            if sub:
                out.setdefault(row["card_name"], set()).add(sub)
    return out


def build_penalty_context(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    *,
    candidate_cache: CandidateCache | None = None,
) -> PenaltyContext:
    """Bulk-load everything ``apply_penalties_ctx`` needs for one page."""
    cmdr_rows = conn.execute(
        "SELECT name, color_identity, subtypes, oracle_text, card_types, "
        "       deck_has, keywords FROM cards "
        "WHERE name IN ({})".format(",".join("?" * len(commander_set))),
        tuple(commander_set),
    ).fetchall()

    # Commander-side derived state
    cmdr_card_types: set[str] = set()
    for r in cmdr_rows:
        cmdr_card_types.update((r["card_types"] or "").split())
    cmdr_subtypes = _commander_subtypes(cmdr_rows)

    cmdr_has_abilities: set[str] = set()
    cmdr_has_types: set[str] = set()
    for r in cmdr_rows:
        decoded = _decode_json_field(r["deck_has"])
        cmdr_has_abilities.update(decoded.get("Ability", []))
        cmdr_has_types.update(decoded.get("Type", []))
    # Subtypes are also in the "has Type" set so DeckNeeds$Type$Goblin
    # matches a Goblin commander even when deck_has is unpopulated.
    cmdr_has_types.update(cmdr_subtypes)

    cmdr_has_keywords: set[str] = set()
    for r in cmdr_rows:
        try:
            kws = json.loads(r["keywords"] or "[]")
            if isinstance(kws, list):
                cmdr_has_keywords.update(kws)
        except (TypeError, ValueError):
            pass

    # Commander port state — counter usage, mill, token types
    placeholders = ",".join("?" * len(commander_set))
    cmdr_counter_types: set[str] = set()
    cmdr_uses_p1p1 = False
    cmdr_p1p1_is_primary = False
    cmdr_self_mill = False
    cmdr_token_subtypes: set[str] = set()
    _has_p1p1_effect = False
    _scales_p1p1 = False
    _scales_all = False

    cmdr_port_rows = conn.execute(
        f"SELECT port_type, event_class, counter_type, valid_filter, raw_line "
        f"FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(commander_set),
    ).fetchall()
    for prow in cmdr_port_rows:
        ev = (prow["event_class"] or "").strip()
        pt = (prow["port_type"] or "").strip()
        ct = (prow["counter_type"] or "").strip()
        if ct:
            for tok in ct.split(","):
                tok = tok.strip()
                if tok:
                    cmdr_counter_types.add(tok)
                    if tok in P1P1_ALIASES:
                        cmdr_uses_p1p1 = True
                        cmdr_has_abilities.add("Counters")
        # Track P1P1 primary strategy via scales_with (same logic as
        # graph_engine._commander_is_p1p1_payoff).
        if pt == "effect" and ev in ("PutCounter", "PutCounterAll") and ct == "P1P1":
            _has_p1p1_effect = True
        if pt == "scales_with":
            if ev == "CardCounters.P1P1":
                _scales_p1p1 = True
            elif ev == "CardCounters.ALL":
                _scales_all = True
        if ev == "Mill" and "You" in (prow["valid_filter"] or ""):
            cmdr_self_mill = True
        if ev == "Token":
            raw = prow["raw_line"] or ""
            m = re.search(r"'TokenScript':\s*'([^']+)'", raw)
            if m:
                sub = _token_subtype(m.group(1))
                if sub:
                    cmdr_token_subtypes.add(sub)
                    cmdr_has_abilities.add("Token")
        if ev in ("PutCounter", "PutCounterAll", "Proliferate"):
            cmdr_has_abilities.add("Counters")

    cmdr_p1p1_is_primary = _scales_p1p1 or (_scales_all and _has_p1p1_effect)

    # Inject scales_with types into cmdr_has_types so Rule 11 doesn't
    # penalize cards that match the commander's scaling strategy. E.g.
    # Uril scales_with Aura → "Aura" joins cmdr_has_types → Ethereal
    # Armor's deck_hints Type=Enchantment won't trigger Rule 11.
    _SCALES_TYPE_TOKENS: frozenset[str] = frozenset(
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
    # Parent types: if the commander scales with a subtype, also add
    # the parent supertype so Rule 11 doesn't fire on "Enchantment"
    # when the commander scales with "Aura".
    _SUBTYPE_TO_PARENT: dict[str, str] = {
        "Aura": "Enchantment",
        "Equipment": "Artifact",
    }
    for prow in cmdr_port_rows:
        if (prow["port_type"] or "").strip() == "scales_with":
            raw = prow["raw_line"] or ""
            vf = prow["valid_filter"] or ""
            for tok in _SCALES_TYPE_TOKENS:
                if tok in raw or tok in vf:
                    cmdr_has_types.add(tok)
                    parent = _SUBTYPE_TO_PARENT.get(tok)
                    if parent:
                        cmdr_has_types.add(parent)

    # Tribal anchor: the commander shares at least one of its own subtypes
    # with the token subtype it generates (Krenko IS a Goblin AND makes
    # Goblin tokens; Marrow-Gnawer IS a Rat AND makes Rat tokens). This
    # was previously OR'd with `"Counters" in has_abilities`, which
    # incorrectly classified counter commanders (e.g. Vorel of the Hull
    # Clade) as tribal — that conflated two orthogonal strategies. The
    # counter path is already handled by `cmdr_uses_p1p1`.
    cmdr_is_tribal = bool(cmdr_subtypes & cmdr_token_subtypes)

    cc = candidate_cache if candidate_cache is not None else build_candidate_cache(conn)

    return PenaltyContext(
        cmdr_identity=_color_identity(cmdr_rows),
        cmdr_subtypes=frozenset(cmdr_subtypes),
        cmdr_card_types=frozenset(cmdr_card_types),
        cmdr_allows_background=any(_has_choose_a_background(r) for r in cmdr_rows),
        cmdr_has_doctor=_has_doctor_companion_partner(cmdr_rows),
        cmdr_counter_types=frozenset(cmdr_counter_types),
        cmdr_uses_p1p1=cmdr_uses_p1p1,
        cmdr_p1p1_is_primary=cmdr_p1p1_is_primary,
        cmdr_self_mill=cmdr_self_mill,
        cmdr_has_abilities=frozenset(cmdr_has_abilities),
        cmdr_has_types=frozenset(cmdr_has_types),
        cmdr_has_keywords=frozenset(cmdr_has_keywords),
        cmdr_token_subtypes=frozenset(cmdr_token_subtypes),
        cmdr_is_tribal=cmdr_is_tribal,
        candidate_rows=cc.candidate_rows,
        creature_static_scopes=cc.creature_static_scopes,
        candidate_excludes=cc.candidate_excludes,
        candidate_token_types=cc.candidate_token_types,
        candidate_counter_types=cc.candidate_counter_types,
        candidate_counters_on_land=cc.candidate_counters_on_land,
        candidate_opp_mill=cc.candidate_opp_mill,
    )


# ---------------------------------------------------------------------------
# Per-candidate fast path
