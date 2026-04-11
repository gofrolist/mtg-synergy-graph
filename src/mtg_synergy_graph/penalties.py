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

# Multipliers — keep these as named constants so calibration can be done
# without spelunking through helper functions.
SUBTYPE_MISMATCH_MULT       = 0.4   # rule 3
EXCLUDED_SUBTYPES_MULT      = 0.3   # rule 4
WRONG_TOKEN_TYPE_MULT       = 0.5   # rule 5
WRONG_COUNTER_TYPE_MULT     = 0.4   # rule 6
NICHE_COUNTER_MULT          = 0.4   # rule 7
COUNTERS_ON_LANDS_MULT      = 0.4   # rule 8
NON_COUNTER_CREATURE_MULT   = 0.6   # rule 9
OPPONENT_MILL_MULT          = 0.3   # rule 10
UNMET_TYPE_MULT             = 0.3   # rule 11
UNMET_ABILITY_MULT          = 0.85  # rule 12

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
            out.add(seg[len(_NON_PREFIX):])
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
    """Best-effort subtype extraction from a Forge TokenScript$ value."""
    if not script:
        return None
    parts = script.lower().split("_")
    if len(parts) < 3:
        return None
    # Heuristic: if the second segment is a single letter ("a", "c", ...) it's
    # a non-creature token script and the subtype is parts[2]. Otherwise it's
    # the creature form and the subtype is parts[3] when present.
    if len(parts[1]) == 1 and not parts[1].isdigit():
        return parts[2].capitalize()
    if len(parts) >= 4:
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

    candidate_rows:             dict[str, dict[str, Any]]
    creature_static_scopes:     dict[str, list[str]]
    candidate_excludes:         dict[str, set[str]]
    candidate_token_types:      dict[str, set[str]]
    candidate_counter_types:    dict[str, set[str]]
    candidate_counters_on_land: dict[str, bool]
    candidate_opp_mill:         dict[str, bool]


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
    cmdr_identity:           frozenset[str]
    cmdr_subtypes:           frozenset[str]
    cmdr_card_types:         frozenset[str]
    cmdr_allows_background:  bool
    cmdr_has_doctor:         bool
    cmdr_counter_types:      frozenset[str]   # P1P1 / M1M1 / TIME / etc.
    cmdr_uses_p1p1:          bool             # has any P1P1 trigger/effect
    cmdr_p1p1_is_primary:    bool             # P1P1 is primary strategy (scales_with)
    cmdr_self_mill:          bool             # has Mill effect targeting You
    cmdr_has_abilities:      frozenset[str]   # from deck_has + derived
    cmdr_has_types:          frozenset[str]   # from deck_has + derived
    cmdr_has_keywords:       frozenset[str]
    cmdr_token_subtypes:     frozenset[str]   # token types the commander makes
    cmdr_is_tribal:          bool             # commander has a tribal anchor:
                                              # at least one of its own subtypes
                                              # appears as a token-subtype it
                                              # generates (Krenko makes Goblins,
                                              # IS a Goblin) — see
                                              # build_penalty_context for the
                                              # exact heuristic

    # Candidate-side state ---------------------------------------------------
    candidate_rows:             dict[str, dict[str, Any]] = field(default_factory=dict)
    creature_static_scopes:     dict[str, list[str]]      = field(default_factory=dict)
    candidate_excludes:         dict[str, set[str]]       = field(default_factory=dict)
    candidate_token_types:      dict[str, set[str]]       = field(default_factory=dict)
    candidate_counter_types:    dict[str, set[str]]       = field(default_factory=dict)
    candidate_counters_on_land: dict[str, bool]           = field(default_factory=dict)
    candidate_opp_mill:         dict[str, bool]           = field(default_factory=dict)
    # Phase F9: candidates with sacrifice/trigger-resonance signal —
    # exempt from Rule 9 (non-counter creature penalty) because they
    # are mechanically connected to the sacrifice axis, not counters.
    candidates_with_sacrifice_signal: frozenset[str]      = field(default_factory=frozenset)


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
            "name":           row["name"],
            "color_identity": row["color_identity"],
            "subtypes":       row["subtypes"],
            "oracle_text":    row["oracle_text"],
            "card_types":     row["card_types"],
            "cmc":            row["cmc"],
            "deck_needs":     row["deck_needs"],
            "deck_hints":     row["deck_hints"],
            "deck_has":       row["deck_has"],
            "edhrec_rank":    row["edhrec_rank"],
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
        "SELECT card_name, affected_scope FROM card_ports "
        "WHERE port_type = 'static' AND affected_scope LIKE '%non%'"
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
        "SELECT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Token'"
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

    for prow in conn.execute(
        f"SELECT port_type, event_class, counter_type, valid_filter, raw_line "
        f"FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(commander_set),
    ).fetchall():
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
        if pt == "effect" and ev in ("PutCounter", "PutCounterAll"):
            if ct == "P1P1":
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
# ---------------------------------------------------------------------------


def _scope_subtypes(scope: str) -> set[str]:
    return {
        tok
        for tok in (scope or "").replace("+", ".").split(".")
        if tok
        and tok[0].isupper()
        and tok not in {"Creature", "YouCtrl", "OppCtrl", "Other"}
        and not tok.startswith("non")
    }


def apply_penalties_ctx(
    ctx: PenaltyContext,
    candidate: str,
    score: float,
) -> float:
    """In-memory penalty pass — the fast path used by ``SynergyEngine.page``."""
    cand = ctx.candidate_rows.get(candidate)
    if cand is None:
        return score

    # ----- Rule 1: wrong colour identity (hard filter) ---------------------
    cand_identity = _parse_csv(cand["color_identity"])
    if cand_identity - ctx.cmdr_identity:
        return HARD_FILTER_SCORE

    cand_subtypes = set((cand["subtypes"] or "").split())

    # ----- Rule 2: Background / Doctor's-companion legality ----------------
    if "Background" in cand_subtypes and not ctx.cmdr_allows_background:
        return HARD_FILTER_SCORE
    if "doctor's companion" in (cand["oracle_text"] or "").lower() and not ctx.cmdr_has_doctor:
        return HARD_FILTER_SCORE

    # ----- Rule 3: required-subtype mismatch -------------------------------
    static_scopes = ctx.creature_static_scopes.get(candidate, [])
    if static_scopes and ctx.cmdr_subtypes:
        for scope in static_scopes:
            scope_subtypes = _scope_subtypes(scope)
            if scope_subtypes and not (scope_subtypes & ctx.cmdr_subtypes):
                score *= SUBTYPE_MISMATCH_MULT
                break

    # ----- Rule 4: excluded subtypes / colours / types ---------------------
    excludes = ctx.candidate_excludes.get(candidate, set())
    if excludes:
        # If the candidate excludes a tribe / colour / type the commander
        # belongs to, that effect is dead for the deck.
        cmdr_self_tags = ctx.cmdr_subtypes | ctx.cmdr_card_types
        if excludes & cmdr_self_tags:
            score *= EXCLUDED_SUBTYPES_MULT

    # ----- Rule 5: wrong token type ----------------------------------------
    cand_tokens = ctx.candidate_token_types.get(candidate, set())
    if (
        ctx.cmdr_is_tribal
        and ctx.cmdr_token_subtypes
        and cand_tokens
        and not (cand_tokens & ctx.cmdr_token_subtypes)
    ):
        score *= WRONG_TOKEN_TYPE_MULT

    # ----- Rule 6: wrong counter type --------------------------------------
    cand_counters = ctx.candidate_counter_types.get(candidate, set())
    if ctx.cmdr_uses_p1p1 and cand_counters and not (cand_counters & P1P1_ALIASES):
        # Candidate puts non-P1P1 counters and the commander wants P1P1.
        # Niche counters (TIME / EXPERIENCE / ENERGY) are handled separately
        # by rule 7, so rule 6 only fires when there's at least one
        # non-niche, non-P1P1 counter type — these are the M1M1 / SHIELD /
        # CHARGE cases that are genuinely "wrong type for the deck".
        non_niche = cand_counters - NICHE_COUNTERS
        if non_niche:
            score *= WRONG_COUNTER_TYPE_MULT

    # ----- Rule 7: niche counter --------------------------------------------
    if cand_counters and cand_counters.issubset(NICHE_COUNTERS):
        if not (ctx.cmdr_counter_types & NICHE_COUNTERS):
            score *= NICHE_COUNTER_MULT

    # ----- Rule 8: counters on lands ---------------------------------------
    if ctx.cmdr_uses_p1p1 and ctx.candidate_counters_on_land.get(candidate):
        score *= COUNTERS_ON_LANDS_MULT

    # ----- Rule 9: non-counter creatures for counter commanders ------------
    # Skipped for tribal commanders and for candidates with sacrifice/
    # trigger-resonance signal (Phase F9). A sacrifice-axis creature
    # (Mayhem Devil, Pitiless Plunderer) shouldn't be penalized for
    # lacking +1/+1 counters on a commander like Korvold whose P1P1
    # is a side-effect reward, not his primary strategy.
    if (
        ctx.cmdr_uses_p1p1
        and not ctx.cmdr_is_tribal
        and candidate not in ctx.candidates_with_sacrifice_signal
        and "Creature" in (cand["card_types"] or "").split()
        and not (cand_counters & P1P1_ALIASES)
        and "Counters" not in _decoded_has_abilities(cand)
        and not (cand_subtypes & ctx.cmdr_subtypes)  # never penalise own tribe
    ):
        score *= NON_COUNTER_CREATURE_MULT

    # ----- Rule 10: opponent-only mill replacement -------------------------
    if ctx.cmdr_self_mill and ctx.candidate_opp_mill.get(candidate):
        score *= OPPONENT_MILL_MULT

    # ----- Rule 11: unmet Type$ need / hint --------------------------------
    needs = _decode_json_field(cand["deck_needs"])
    hints = _decode_json_field(cand["deck_hints"])
    if needs.get("Type") or hints.get("Type"):
        wanted_types = set(needs.get("Type", [])) | set(hints.get("Type", []))
        # If the candidate's needed type is not present anywhere on the
        # commander (subtype, card_type, or "has" set), the synergy is dead.
        cmdr_provides = ctx.cmdr_has_types | ctx.cmdr_card_types
        if wanted_types and not (wanted_types & cmdr_provides):
            score *= UNMET_TYPE_MULT

    # ----- Rule 12: unmet Ability$ need ------------------------------------
    if needs.get("Ability"):
        wanted = set(needs["Ability"])
        if wanted and not (wanted & ctx.cmdr_has_abilities):
            score *= UNMET_ABILITY_MULT

    return score


def _decoded_has_abilities(cand: dict[str, Any]) -> set[str]:
    """Helper for rule 9 — returns the candidate's own deck_has abilities."""
    return set(_decode_json_field(cand.get("deck_has")).get("Ability", []))


def apply_penalties(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate: str,
    score: float,
) -> float:
    """Single-shot penalty pass — slow path. Builds a fresh PenaltyContext."""
    ctx = build_penalty_context(conn, commander_set)
    return apply_penalties_ctx(ctx, candidate, score)
