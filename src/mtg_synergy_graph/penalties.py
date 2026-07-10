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

import ast
import json
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .heuristics import EVASION_KEYWORDS

log = logging.getLogger(__name__)

#: Score sentinel for hard filters. Cards at this score are dropped before
#: ranking, never shown to the consumer.
HARD_FILTER_SCORE = -1e9

#: Counter type aliases that all map to "P1P1" for matching purposes.
P1P1_ALIASES: frozenset[str] = frozenset({"P1P1"})

#: Broad ``ReduceCost`` ``ValidCard`` values that reduce a wide class of *your*
#: spells (freeing mana for a bigger X) — the ``cost_reduce_generic`` tier of
#: ``x_cost_scaler``. Excludes self-cost (``Card.Self``) and tribe/type-narrow
#: reducers (``Wizard``, ``Dragon``, ``Creature``, …). Spec 2026-07-09-x-cost-scaler.
_X_COST_BROAD_VALIDCARD: frozenset[str] = frozenset(
    {"Card", "", "Card.nonCreature", "Card.multicolor", "Card.monocolored", "Instant,Sorcery"}
)


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

    The extra ``cmc_rank_map``, ``panharmonicon_statics``,
    ``token_effect_rows``, and ``stax_cards_by_event`` fields extend the
    cache to the complement-rule layer so helpers in
    ``complement_rules/`` and ``universal_scorer`` can read from the
    cache instead of re-issuing the same SQL for every commander in a
    batch run.
    """

    candidate_rows: dict[str, dict[str, Any]]
    creature_static_scopes: dict[str, list[str]]
    candidate_excludes: dict[str, set[str]]
    candidate_token_types: dict[str, set[str]]
    candidate_counter_types: dict[str, set[str]]
    candidate_counters_on_land: dict[str, bool]
    candidate_opp_mill: dict[str, bool]
    cmc_rank_map: dict[str, tuple[float, int]]
    panharmonicon_statics: tuple[tuple[str, str], ...]
    token_effect_rows: tuple[tuple[str, str], ...]
    stax_cards_by_event: dict[str, frozenset[str]]
    #: Per-card ``(attr_kind, attr_value)`` set, exactly the output of
    #: :func:`graph_engine._card_attrs_for_filter`. Precomputed once so
    #: ``_filter_card_match`` can skip re-parsing card_types / supertypes
    #: / subtypes / keywords / color_identity on every call.
    card_attrs: dict[str, frozenset[tuple[str, str]]]
    #: Per-card attribute rows keyed by name. Mirrors the column subset
    #: that ``_find_etb_self_complements`` used to load per-commander:
    #: ``name, card_types, supertypes, subtypes, keywords, color_identity``.
    #: Stored here so density helpers can iterate in-memory instead of
    #: re-issuing SQL for every commander.
    candidate_attr_rows: dict[str, dict[str, Any]]
    #: ``(card_name, affected_scope, branch_kind)`` rows for every
    #: static Continuous port with a non-empty affected_scope. Consumed
    #: by ``_find_lord_complements`` — commander-independent, reissued
    #: per commander before caching.
    lord_continuous_rows: tuple[tuple[str, str, str], ...]
    #: Names of cards that have a ``Proliferate`` effect or trigger port.
    #: Consumed by ``_find_proliferate_synergy``.
    proliferate_cards: frozenset[str]
    #: Names of cards that produce generic or P1P1 counters. Consumed by
    #: the ``scales_with`` P1P1 branch of ``_find_scales_with_density``.
    p1p1_counter_producers: frozenset[str]
    #: Names of cards matching the ETB-damage payoff shape — a creature-ETB
    #: ``ChangesZone`` trigger AND a ``DealDamage`` effect targeting a
    #: Player/Opponent (Impact Tremors, Purphoros, Witty Roastmaster, …).
    #: The underlying self-join with three ``LIKE`` patterns costs ~800 ms
    #: against the full port table; caching here turns a 100-commander
    #: batch run from ~80 s into a single query. Consumed by
    #: ``_find_token_etb_damage``.
    token_etb_damage_cards: frozenset[str]
    #: Names of cards matching the "dies-drain" aristocrats-payoff shape —
    #: a ``ChangesZone Battlefield→Graveyard`` trigger whose ``valid_filter``
    #: references ``Creature`` (Blood Artist, Zulaport Cutthroat, Pitiless
    #: Plunderer, Grim Haruspex, Midnight Reaper, Elenda, …). Commander-
    #: independent, so cached here and consumed by ``_find_dies_drain``.
    dies_drain_cards: frozenset[str]
    #: Names of Library→Graveyard tutor cards (Buried Alive, Entomb,
    #: Jarad's Orders, Final Parting, Vile Entomber, Corpse Connoisseur,
    #: …). The single shape ``effect: ChangeZone / ChangeZoneAll`` with
    #: ``zone_origin='Library'`` + ``zone_destination='Graveyard'``.
    #: Already emitted by ``_find_graveyard_fillers`` under
    #: ``rule_id='trigger_effect'``; the second emission under
    #: ``rule_id='gy_loader'`` (via ``_find_gy_loader``) gives tutors a
    #: distinct bucket and a 1.5× boost so they can compete for top-30
    #: in reanimator decks.
    gy_loader_cards: frozenset[str]
    #: Names of attack-trigger payoff cards — creatures and permanents
    #: whose ``Attacks``/``AttackersDeclared`` trigger produces a Token,
    #: a +1/+1 counter on self, mana, a draw, or ChangeZone-from-library
    #: (Adeline, Krenko Tin Street, Captain Lannery Storm, Anim Pakal,
    #: Mardu Ascendancy, Skyknight Vanguard, Sword of the Animist,
    #: Legion's Landing). Consumed by ``_find_attack_payoffs`` for
    #: combat-trigger commanders (Isshin's Panharmonicon doubler plus
    #: any commander with a creature-scoped Attacks trigger).
    attack_payoff_cards: frozenset[str]
    #: Names of broad-scope untap cards — mass untap (Dramatic Reversal,
    #: Paradox Engine, Turnabout, Seedborn Muse), artifact-targeted
    #: untap (Voltaic Key, Clock of Omens), permanent/targeted untap
    #: (Fatestitcher), and static untap-at-other-players'-untap-steps
    #: (Unwinding Clock). Distinct from ``_find_untap_synergy`` which
    #: handles creature-only Quirion-Ranger-style untaps; consumed by
    #: ``_find_untap_combo``.
    untap_combo_cards: frozenset[str]
    #: Names of CMC ≥ 6 value creatures (Kozilek, Artisan, Rune-Scarred
    #: Demon) — the quality-gated cost-reduction-target pool. Excludes
    #: damage-dealing creatures (already covered by pinger/scaling) and
    #: keyword-vanilla fatties. Consumed by
    #: ``_find_cost_reduction_targets``. Caching cuts ~650 ms per
    #: commander to one upfront query.
    cost_reduction_target_pool: frozenset[str]
    #: Per-card list of port-row dicts for the columns the pathway walker
    #: needs (``port_type``, ``event_class``, ``valid_filter``,
    #: ``zone_origin``, ``zone_destination``, ``counter_type``). Precomputed
    #: once so ``pathway._find_self_bridging_cascade`` can skip a Stage-2
    #: bulk SQL fetch for every commander in a batch run — the Stage-2
    #: query on Korvold-shape commanders pulled ~17 k port rows per call
    #: and dominated the per-page cost (+2000% overhead in profiling).
    #: Consumed by ``complement_rules.pathway._find_self_bridging_cascade``.
    ports_by_card: dict[str, list[dict[str, Any]]]
    #: Creatures carrying evasion, split into the two IDF tiers consumed by
    #: ``_find_attack_reward_evasion``: ``evasion_hard`` (rare evasion +
    #: self-unblockable, the differentiator) and ``evasion_soft`` (Flying/Menace).
    evasion_hard_cards: frozenset[str] = frozenset()
    evasion_soft_cards: frozenset[str] = frozenset()
    #: X-cost mana-economics pools consumed by ``_find_x_cost_scaler``:
    #: ``x_cost_mana_double_cards`` (ProduceTwice/Thrice doublers, high IDF) and
    #: ``x_cost_cost_reduce_cards`` (broad generic spell-cost reducers).
    x_cost_mana_double_cards: frozenset[str] = frozenset()
    x_cost_cost_reduce_cards: frozenset[str] = frozenset()
    #: Aristocrats death-bridge pools consumed by
    #: ``_find_aristocrats_death_bridge``: ``death_payoff`` (death-triggered
    #: value payoffs, tier 1) and ``recursive_fodder`` (self-returning bodies,
    #: tier 2).
    aristocrats_death_payoff_cards: frozenset[str] = frozenset()
    aristocrats_recursive_fodder_cards: frozenset[str] = frozenset()


def _attack_reward_evasion_enabled() -> bool:
    """Read the ``attack_reward_evasion`` flag at call time (lazy import avoids a
    module-load cycle with ``complement_rules.combat``)."""
    from .complement_rules import combat

    return combat._ENABLE_ATTACK_REWARD_EVASION


def build_candidate_cache(conn: sqlite3.Connection) -> CandidateCache:
    """Load all commander-independent candidate data in one pass."""
    candidate_attr_rows = _bulk_load_candidate_attr_rows(conn)
    # The evasion pools feed only the default-OFF ``attack_reward_evasion`` rule;
    # skip their two full scans entirely while the flag is off so a declined rule
    # costs nothing per cache build (populated on flip, like other cached pools).
    ar_evasion_on = _attack_reward_evasion_enabled()
    x_cost_on = _x_cost_scaler_enabled()
    aristocrats_on = _aristocrats_death_bridge_enabled()
    return CandidateCache(
        candidate_rows=_bulk_load_candidates(conn),
        creature_static_scopes=_bulk_load_static_scopes(conn),
        candidate_excludes=_bulk_load_excludes(conn),
        candidate_token_types=_bulk_load_token_types(conn),
        candidate_counter_types=_bulk_load_counter_types(conn),
        candidate_counters_on_land=_bulk_load_counters_on_lands(conn),
        candidate_opp_mill=_bulk_load_opp_mill(conn),
        cmc_rank_map=_bulk_load_cmc_rank(conn),
        panharmonicon_statics=_bulk_load_panharmonicon_statics(conn),
        token_effect_rows=_bulk_load_token_effect_rows(conn),
        stax_cards_by_event=_bulk_load_stax_cards_by_event(conn),
        card_attrs=_build_card_attrs(candidate_attr_rows),
        candidate_attr_rows=candidate_attr_rows,
        lord_continuous_rows=_bulk_load_lord_continuous_rows(conn),
        proliferate_cards=_bulk_load_proliferate_cards(conn),
        p1p1_counter_producers=_bulk_load_p1p1_counter_producers(conn),
        token_etb_damage_cards=_bulk_load_token_etb_damage_cards(conn),
        dies_drain_cards=_bulk_load_dies_drain_cards(conn),
        gy_loader_cards=_bulk_load_gy_loader_cards(conn),
        attack_payoff_cards=_bulk_load_attack_payoff_cards(conn),
        untap_combo_cards=_bulk_load_untap_combo_cards(conn),
        cost_reduction_target_pool=_bulk_load_cost_reduction_target_pool(conn),
        ports_by_card=_bulk_load_ports_by_card(conn),
        evasion_hard_cards=_bulk_load_evasion_hard_cards(conn) if ar_evasion_on else frozenset(),
        evasion_soft_cards=_bulk_load_evasion_soft_cards(conn) if ar_evasion_on else frozenset(),
        x_cost_mana_double_cards=_bulk_load_x_cost_mana_double_cards(conn) if x_cost_on else frozenset(),
        x_cost_cost_reduce_cards=_bulk_load_x_cost_cost_reduce_cards(conn) if x_cost_on else frozenset(),
        aristocrats_death_payoff_cards=_bulk_load_aristocrats_death_payoff_cards(conn)
        if aristocrats_on
        else frozenset(),
        aristocrats_recursive_fodder_cards=_bulk_load_aristocrats_recursive_fodder_cards(conn)
        if aristocrats_on
        else frozenset(),
    )


def _bulk_load_ports_by_card(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Bulk-fetch every ``card_ports`` row into ``dict[card_name, list[PortRow]]``.

    Used by ``pathway._find_self_bridging_cascade`` Stage-2 to avoid
    re-issuing the bulk port query per commander. Profiling showed
    the per-commander query dominated at +2000% overhead on broad
    commanders (Korvold: 329 ms extra per page); caching amortises
    the cost to a single upfront SQL query shared by every commander
    in a batch run.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    rows = conn.execute(
        "SELECT card_name, port_type, event_class, valid_filter, "
        "zone_origin, zone_destination, counter_type "
        "FROM card_ports"
    ).fetchall()
    for r in rows:
        out.setdefault(r["card_name"], []).append(dict(r))
    return out


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
    # ``legal_commander`` may be absent on older synergy.db files that
    # predate the schema migration; treat missing as legal so this loader
    # stays backward compatible with cached test DBs.
    has_legal = any(r[1] == "legal_commander" for r in conn.execute("PRAGMA table_info(cards)").fetchall())
    legal_expr = "legal_commander" if has_legal else "1 AS legal_commander"
    for row in conn.execute(
        "SELECT name, color_identity, subtypes, oracle_text, card_types, "
        f"       cmc, deck_needs, deck_hints, deck_has, edhrec_rank, {legal_expr} FROM cards"
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
            "legal_commander": row["legal_commander"],
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


def _bulk_load_cmc_rank(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    """Per-card ``(cmc, edhrec_rank)`` map used by ``score_all_universal``.

    Uses the same sentinel defaults (99.0 / 99999) as the previous
    inline loop in ``universal_scorer.score_all_universal``.
    """
    out: dict[str, tuple[float, int]] = {}
    for row in conn.execute("SELECT name, cmc, edhrec_rank FROM cards"):
        cmc = row["cmc"] if row["cmc"] is not None else 99.0
        rank = row["edhrec_rank"] if row["edhrec_rank"] is not None else 99999
        out[row["name"]] = (cmc, rank)
    return out


def _bulk_load_panharmonicon_statics(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    """Raw ``(card_name, raw_line)`` rows for every Panharmonicon static.

    Consumed by ``complement_rules/panharmonicon.py``. Returned as a
    tuple of tuples (immutable, hashable) so callers can iterate safely.
    """
    return tuple(
        (row["card_name"], row["raw_line"] or "")
        for row in conn.execute(
            "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'static' AND event_class = 'Panharmonicon'"
        )
    )


def _bulk_load_token_effect_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    """Raw ``(card_name, raw_line)`` rows for every Token effect port.

    Consumed by ``complement_rules/tokens.py::_find_token_sac_chain``,
    which re-parses ``raw_line`` for its own token-type extraction (distinct
    from the pre-parsed ``candidate_token_types`` mapping).
    """
    return tuple(
        (row["card_name"], row["raw_line"] or "")
        for row in conn.execute(
            "SELECT card_name, raw_line FROM card_ports WHERE port_type = 'effect' AND event_class = 'Token'"
        )
    )


def _bulk_load_cost_reduction_target_pool(conn: sqlite3.Connection) -> frozenset[str]:
    """Cards that are quality cost-reduction targets — CMC ≥ 6 creatures
    with at least one trigger/effect port AND no damage/life-loss effect.

    The natural SQL would be ``SELECT … WHERE EXISTS (...) AND NOT
    EXISTS (...)`` but those correlated subqueries cost ~5.8 s on the
    32k-card port table. Three simple single-scan queries plus Python
    set arithmetic runs in <100 ms — same result, ~60× faster.

    Commander-independent → cached on :class:`CandidateCache` and
    consumed by :func:`_find_cost_reduction_targets` (Rakdos, Animar,
    Hamza, Urza — every cost-reducer commander).
    """
    creatures = {
        row["name"] for row in conn.execute("SELECT name FROM cards WHERE card_types LIKE '%Creature%' AND cmc >= 6")
    }
    has_payoff_port = {
        row["card_name"]
        for row in conn.execute("SELECT DISTINCT card_name FROM card_ports WHERE port_type IN ('trigger', 'effect')")
    }
    has_damage_effect = {
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' "
            "AND event_class IN ('DealDamage', 'DamageAll', 'LoseLife')"
        )
    }
    return frozenset((creatures & has_payoff_port) - has_damage_effect)


def _bulk_load_token_etb_damage_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Cards whose ports form a self-contained ETB-damage payoff shape.

    Self-join with three ``LIKE`` scans costs ~800 ms against the full
    port table. Commander-independent, so we evaluate it once and hand
    the result to ``_find_token_etb_damage`` via :class:`CandidateCache`.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.card_name "
        "FROM card_ports t "
        "JOIN card_ports e ON e.card_name = t.card_name "
        "WHERE t.port_type = 'trigger' "
        "AND t.event_class = 'ChangesZone' "
        "AND (t.zone_destination IS NULL OR t.zone_destination IN ('Battlefield', '')) "
        "AND t.valid_filter LIKE '%Creature%' "
        "AND e.port_type = 'effect' "
        "AND e.event_class = 'DealDamage' "
        "AND (e.valid_filter LIKE '%Player%' OR e.valid_filter LIKE '%Opponent%')"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_dies_drain_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Cards matching the aristocrats dies-payoff shape.

    Trigger half: ``ChangesZone Battlefield→Graveyard`` with a
    ``valid_filter`` that mentions ``Creature`` — so we skip the ~680
    ``Card.Self``-only "when I die" triggers that don't pay off
    arbitrary creature deaths.

    Effect half: restricted to *externally-visible* payoffs — drain
    (LoseLife/DealDamage targeting a Player/Opponent), Draw, Token, or
    Mana. Self-growth effects (``PutCounter Self``) and pure own-side
    lifegain are intentionally excluded: they're weaker payoffs and
    their inclusion pushed tribal lords out of Wilhelt's top-30
    (Abattoir Ghoul, Dread Slaver displacing Death Baron / Cemetery
    Reaper). The 118-card result keeps Blood Artist, Zulaport Cutthroat,
    Cruel Celebrant, Grim Haruspex, Midnight Reaper, Pitiless Plunderer,
    Elenda and Bastion of Remembrance.

    Commander-independent, so cached here and consumed by
    ``_find_dies_drain``.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.card_name FROM card_ports t "
        "JOIN card_ports e ON e.card_name = t.card_name "
        "WHERE t.port_type = 'trigger' "
        "AND t.event_class = 'ChangesZone' "
        "AND t.zone_origin = 'Battlefield' "
        "AND t.zone_destination = 'Graveyard' "
        "AND t.valid_filter LIKE '%Creature%' "
        "AND e.port_type = 'effect' "
        "AND ("
        "     (e.event_class = 'LoseLife' AND e.valid_filter LIKE '%Player%')"
        "  OR (e.event_class = 'DealDamage' AND e.valid_filter LIKE '%Player%')"
        "  OR (e.event_class = 'Token')"
        "  OR (e.event_class = 'Draw' AND e.valid_filter LIKE '%You%')"
        "  OR (e.event_class = 'Mana')"
        ")"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_gy_loader_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Library→Graveyard tutor cards (Buried Alive, Entomb, Jarad's
    Orders, Final Parting, Vile Entomber, …).

    Pool size ~49. Commander-independent and also queried by
    ``_find_graveyard_fillers`` — duplicating the query here lets the
    new ``_find_gy_loader`` rule emit its own complements under a
    distinct ``rule_id`` so tutors accumulate two rule matches
    (trigger_effect + gy_loader) instead of one.
    """
    rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'effect' "
        "AND event_class IN ('ChangeZone', 'ChangeZoneAll') "
        "AND zone_origin = 'Library' "
        "AND zone_destination = 'Graveyard'"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_attack_payoff_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Cards whose ``Attacks`` or ``AttackersDeclared`` trigger produces
    a distinct piece of value on every attack — the shape that makes
    Isshin-style attack-trigger-doubler commanders tick.

    Effect half restricted to value-generating events: Token (Adeline,
    Mardu Ascendancy, Captain Lannery Storm), ``PutCounter Self`` or
    ``PutCounter Creature.YouCtrl`` (Anim Pakal, Krenko Tin Street),
    ``Draw`` (Bident of Thassa, Reconnaissance Mission), ``Mana``
    (Legion's Landing flipping), ``ChangeZone Library→*`` (Sword of
    the Animist, Etali-style land cheats).

    Pool ≈ 400 cards. Commander-independent, cached here and consumed
    by ``_find_attack_payoffs``.
    """
    rows = conn.execute(
        "SELECT DISTINCT t.card_name FROM card_ports t "
        "JOIN card_ports e ON e.card_name = t.card_name "
        "WHERE t.port_type = 'trigger' "
        "AND t.event_class IN ('Attacks', 'AttackersDeclared') "
        "AND e.port_type = 'effect' "
        "AND ("
        "     (e.event_class = 'Token')"
        "  OR (e.event_class = 'PutCounter'"
        "      AND (e.valid_filter = 'Self' OR e.valid_filter = 'Creature.YouCtrl'))"
        "  OR (e.event_class = 'Draw' AND e.valid_filter LIKE '%You%')"
        "  OR (e.event_class = 'Mana')"
        "  OR (e.event_class = 'ChangeZone' AND e.zone_origin = 'Library')"
        ")"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


#: The ``attack_reward_evasion`` IDF tiers, derived from the single-source
#: :data:`heuristics.EVASION_KEYWORDS` so they cannot drift from the staple
#: heuristic: soft = the two common keywords, hard = the rare remainder.
_EVASION_SOFT_KEYWORDS: frozenset[str] = frozenset({"Flying", "Menace"})
_EVASION_HARD_KEYWORDS: frozenset[str] = EVASION_KEYWORDS - _EVASION_SOFT_KEYWORDS

#: A ``CantBlockBy`` static makes the card itself unblockable only when ``Self``
#: is the ``ValidAttacker`` ("CARDNAME can't be blocked by …"). ``Self`` under
#: ``ValidBlocker`` is a *blocking* restriction ("CARDNAME can block only fliers"
#: — Ascending Aven, Cloud Djinn: 44 non-evasive false positives a bare
#: ``Creature.Self`` substring match wrongly bucketed as hard evasion).
_CANTBLOCKBY_SELF_ATTACKER_RE = re.compile(r"'ValidAttacker':\s*'(?:Creature|Card)\.Self")


def _evasion_creature_cards(conn: sqlite3.Connection, keywords: frozenset[str]) -> set[str]:
    """Distinct creature cards granting any of ``keywords`` via a keyword port.

    Placeholders are ``?``-per-keyword from ``len()`` of a module-level frozenset;
    all values are bound as params (no fragment interpolation)."""
    placeholders = ",".join("?" * len(keywords))
    rows = conn.execute(
        "SELECT DISTINCT p.card_name FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        f"WHERE p.port_type = 'keyword' AND p.granted_keyword IN ({placeholders}) "
        "AND c.card_types LIKE '%Creature%'",
        tuple(sorted(keywords)),
    ).fetchall()
    return {row["card_name"] for row in rows}


def _bulk_load_evasion_soft_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Creatures with common evasion (Flying/Menace) — the low-IDF soft tier of
    ``attack_reward_evasion``. Commander-independent; consumed by
    ``complement_rules.combat._find_attack_reward_evasion``."""
    return frozenset(_evasion_creature_cards(conn, _EVASION_SOFT_KEYWORDS))


def _bulk_load_evasion_hard_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Creatures with rare evasion (Shadow/Horsemanship/Skulk/Fear/Intimidate) or
    a genuinely self-unblockable ``CantBlockBy`` static — the high-IDF hard tier
    of ``attack_reward_evasion`` and the real differentiator. Commander-
    independent; consumed by ``complement_rules.combat._find_attack_reward_evasion``."""
    hard = _evasion_creature_cards(conn, _EVASION_HARD_KEYWORDS)
    for row in conn.execute(
        "SELECT DISTINCT p.card_name, p.raw_line FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE p.port_type = 'static' AND p.event_class = 'CantBlockBy' "
        "AND c.card_types LIKE '%Creature%'"
    ).fetchall():
        if _CANTBLOCKBY_SELF_ATTACKER_RE.search(row["raw_line"] or ""):
            hard.add(row["card_name"])
    return frozenset(hard)


def _bulk_load_x_cost_mana_double_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Mana doublers (ReplaceWith ProduceTwice/ProduceThrice) — the high-IDF
    ``mana_double`` tier of ``x_cost_scaler``. Mana-denial / color-warp
    replacements carry other ``ReplaceWith`` values and are excluded.
    Reads the structured ``card_ports.replacement_result`` column (populated
    from ``ReplaceWith`` by ``ports.extract_replacement_ports``) rather than
    regex-parsing ``raw_line`` — immune to any ``repr(parsed)`` format change.
    Commander-independent; consumed by ``complement_rules.density._find_x_cost_scaler``."""
    rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE port_type = 'replacement' AND event_class = 'ProduceMana' "
        "AND replacement_result IN ('ProduceTwice', 'ProduceThrice')"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_x_cost_cost_reduce_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Broad generic spell-cost reducers — the ``cost_reduce_generic`` tier of
    ``x_cost_scaler``: a ``static`` ``ReduceCost`` with ``Type=Spell``, a broad
    ``ValidCard`` (``_X_COST_BROAD_VALIDCARD``, excluding self-cost), and a
    numeric ``Amount``.

    ``raw_line`` is ``repr(parsed)`` of the extractor's dict, so it is parsed
    with ``ast.literal_eval`` rather than per-field single-quote regexes — the
    latter silently miss apostrophe-containing values (``repr`` switches such a
    value to double quotes, e.g. ``"Card.namedKarlov's Crossbow"``) and cannot
    tell an ABSENT ``ValidCard`` (restriction living in ``ValidTarget`` /
    ``Activator``, e.g. Accursed Witch reducing an *opponent's* spell) from an
    explicitly-empty one. A missing ``ValidCard`` key is therefore NOT treated
    as broad. Restricted to ``port_type='static'`` so a future one-shot
    ``AB$ ReduceCost`` effect (same ``event_class``) is not swept in.
    Commander-independent; consumed by ``complement_rules.density._find_x_cost_scaler``."""
    rows = conn.execute(
        "SELECT DISTINCT p.card_name, p.raw_line FROM card_ports p "
        "WHERE p.port_type = 'static' AND p.event_class = 'ReduceCost'"
    ).fetchall()
    out: set[str] = set()
    for row in rows:
        try:
            parsed = ast.literal_eval(row["raw_line"] or "{}")
        except (ValueError, SyntaxError):
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("Type") != "Spell":
            continue
        if "ValidCard" not in parsed:  # absent restriction is NOT broad
            continue
        if parsed["ValidCard"] not in _X_COST_BROAD_VALIDCARD:
            continue
        amount = str(parsed.get("Amount", ""))
        if not amount.isdigit() or int(amount) < 1:
            continue
        out.add(row["card_name"])
    return frozenset(out)


def _x_cost_scaler_enabled() -> bool:
    """Read the ``x_cost_scaler`` flag at call time (lazy import avoids a
    module-load cycle with ``complement_rules.density``)."""
    from .complement_rules import density

    return density._ENABLE_X_COST_SCALER


#: Value-payoff effect classes that make a death trigger an aristocrats payoff
#: (as opposed to a non-value death trigger). Consumed by
#: ``_bulk_load_aristocrats_death_payoff_cards``.
_DEATH_VALUE_EFFECTS = frozenset(
    {"LoseLife", "GainLife", "DealDamage", "DamageAll", "PutCounter", "PutCounterAll", "Token", "Draw", "Mana"}
)


def _aristocrats_death_bridge_enabled() -> bool:
    """Read the ``aristocrats_death_bridge`` flag at call time (lazy import
    avoids a module-load cycle with ``complement_rules.aristocrats``)."""
    from .complement_rules import aristocrats

    return aristocrats._ENABLE_ARISTOCRATS_DEATH_BRIDGE


def _bulk_load_aristocrats_death_payoff_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Tier-1 ``death_payoff`` pool (~229): a ``trigger`` on a creature dying
    (``ChangesZone`` Battlefield->Graveyard, or ``Sacrificed``/``Dies``) whose
    ``execute_ref`` matches a same-card ``effect`` port's ``source_svar`` with a
    value ``event_class`` in :data:`_DEATH_VALUE_EFFECTS`, watching a Creature
    dying with scope broader than pure ``Card.Self``, excluding opponent-only
    triggers. All three conditions are required — dropping the Creature-scope
    condition reintroduces an ~696-card flood.
    Commander-independent; consumed by
    ``complement_rules.aristocrats._find_aristocrats_death_bridge``."""
    ph = ",".join("?" * len(_DEATH_VALUE_EFFECTS))
    rows = conn.execute(
        "SELECT DISTINCT t.card_name FROM card_ports t "
        "JOIN card_ports e ON e.card_name = t.card_name AND e.source_svar = t.execute_ref "
        "WHERE t.port_type = 'trigger' "
        # Zone match is substring (instr, case-sensitive), NOT exact-equality:
        # zone_destination can be a comma-list ('Graveyard,Exile' — Reyhan, Syr
        # Vondam). Mirrors cohorts.py::aristocrats (fixed in 0538bbe).
        "AND ( (t.event_class = 'ChangesZone' AND instr(t.zone_origin, 'Battlefield') > 0 "
        "       AND instr(t.zone_destination, 'Graveyard') > 0) "
        "     OR t.event_class IN ('Sacrificed', 'Dies') ) "
        "AND t.execute_ref IS NOT NULL AND t.execute_ref != '' "
        "AND e.port_type = 'effect' AND e.event_class IN (" + ph + ") "  # ph is placeholders, values bound below
        "AND t.valid_filter LIKE '%Creature%' "
        "AND t.valid_filter NOT IN ('Card.Self', 'Creature.Self') "
        # Exclude opponent-ONLY triggers (a payoff watching only opponents'
        # creatures dying is backwards for a sac-outlet deck). Opponent markers
        # mirror the canonical scope detector (complement_rules/core.py): OppCtrl
        # / OppOwn / Player.Opponent / +Opp. Rescued when the filter also carries
        # a your/any-scope marker (YouCtrl/YouOwn, a broad '.Other', or Card.Self).
        "AND NOT ( (t.valid_filter LIKE '%OppCtrl%' OR t.valid_filter LIKE '%OppOwn%' "
        "           OR t.valid_filter LIKE '%Player.Opponent%' OR t.valid_filter LIKE '%+Opp%') "
        "         AND t.valid_filter NOT LIKE '%YouCtrl%' AND t.valid_filter NOT LIKE '%YouOwn%' "
        "         AND t.valid_filter NOT LIKE '%.Other%' AND t.valid_filter NOT LIKE '%Card.Self%')",
        tuple(sorted(_DEATH_VALUE_EFFECTS)),
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_aristocrats_recursive_fodder_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Tier-2 ``recursive_fodder`` pool (~150): a Creature that returns ITSELF —
    ``Undying``/``Persist`` keyword, or a grave->bf ``ChangeZone`` effect whose
    ``valid_filter`` is empty / ``Card.Self`` / ``CARDNAME`` (self-recursion).
    The self-filter condition excludes reanimator value-engines that return
    OTHER creatures (Sun Titan, Reveillark). Commander-independent; consumed by
    ``complement_rules.aristocrats._find_aristocrats_death_bridge``."""
    rows = conn.execute(
        "SELECT DISTINCT p.card_name FROM card_ports p "
        "JOIN cards c ON c.name = p.card_name "
        "WHERE c.card_types LIKE '%Creature%' AND ( "
        "  p.granted_keyword IN ('Undying', 'Persist') "
        # Zone match is substring (instr), NOT exact-equality — zone_origin can
        # be a comma-list ('Graveyard,Exile' — Bramble Familiar, Bruna, Danitha).
        "  OR ( p.port_type = 'effect' AND p.event_class = 'ChangeZone' "
        "       AND instr(p.zone_origin, 'Graveyard') > 0 AND instr(p.zone_destination, 'Battlefield') > 0 "
        "       AND ( p.valid_filter IS NULL OR p.valid_filter = '' "
        "             OR p.valid_filter LIKE '%Card.Self%' OR p.valid_filter LIKE '%CARDNAME%' ) ) )"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


def _bulk_load_untap_combo_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Classic-combo-shape untap cards.

    Tightened to the core shapes that *actually* enable Urza / Emry /
    Isochron-Scepter-style combos. Includes:

    1. ``effect: UntapAll`` scoped to permanents/artifacts/own
       creatures (Dramatic Reversal, Paradox Engine, Seedborn Muse,
       Turnabout, Inspiring Statuary-ish).
    2. ``effect: Untap`` where the filter references Artifact — the
       targeted artifact-untap cost-doubler shape (Voltaic Key, Clock
       of Omens, Aphetto Alchemist).
    3. ``static: UntapOtherPlayer`` — Unwinding Clock and its handful
       of cousins.
    4. ``effect: TapOrUntapAll`` — Turnabout-style mass flip.

    Intentionally EXCLUDES:
    - Land-only untap (Wilderness Reclamation, Awakening) — Kinnan
      territory, but too niche for Urza/Emry.
    - ``Untap vf='Permanent' / 'Targeted' / 'Creature'`` — broad pools
      full of niche effects (Cerulean Wisps, Inverted Iceberg) that
      over-promoted random cards into top-30 on every tap-cost
      commander.
    - ``TapOrUntap`` (targeted) — Fatestitcher, Kiora's Follower —
      similar noise problem.

    Pool ≈ 97 cards. Commander-independent.
    """
    rows = conn.execute(
        "SELECT DISTINCT card_name FROM card_ports "
        "WHERE ("
        "     (port_type = 'effect' AND event_class = 'UntapAll'"
        "      AND (valid_filter LIKE '%Permanent%'"
        "        OR valid_filter LIKE '%Artifact%'"
        "        OR valid_filter LIKE '%Creature.YouCtrl%'"
        "        OR valid_filter = 'You'))"
        "  OR (port_type = 'effect' AND event_class = 'Untap'"
        "      AND valid_filter LIKE '%Artifact%')"
        "  OR (port_type = 'static' AND event_class = 'UntapOtherPlayer')"
        "  OR (port_type = 'effect' AND event_class = 'TapOrUntapAll')"
        ")"
    ).fetchall()
    return frozenset(row["card_name"] for row in rows)


#: Union of every stax ``event_class`` that ``_build_stax_exclusion``
#: (complement_rules/core.py) may query. Keep in sync with ``_STAX_MAP``
#: in core.py — adding a new stax axis there requires adding it here too.
_STAX_EVENT_CLASSES: tuple[str, ...] = (
    "DisableTriggers",
    "CantSacrifice",
    "RaiseCost",
    "CantBeCast",
    "CantGainLife",
    "CantDraw",
    "CantPutCounter",
)


def _bulk_load_candidate_attr_rows(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Load the exact column subset that ``_card_attrs_for_filter`` reads.

    ``candidate_rows`` intentionally omits ``supertypes`` and ``keywords``
    (the penalty layer doesn't need them), so the density helpers and
    ``_filter_card_match`` cache keep their own row map with just the
    attr-relevant columns.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT name, card_types, supertypes, subtypes, keywords, color_identity FROM cards"):
        out[row["name"]] = {
            "name": row["name"],
            "card_types": row["card_types"],
            "supertypes": row["supertypes"],
            "subtypes": row["subtypes"],
            "keywords": row["keywords"],
            "color_identity": row["color_identity"],
        }
    return out


def _build_card_attrs(
    attr_rows: dict[str, dict[str, Any]],
) -> dict[str, frozenset[tuple[str, str]]]:
    """Precompute the full ``(attr_kind, attr_value)`` set per card.

    Replays :func:`graph_engine._card_attrs_for_filter` in bulk so
    ``_filter_card_match`` can hit a hashed set instead of rebuilding
    it from string columns on every (filter, card) pair.
    """
    # Imported here to avoid an import cycle at module load time.
    from .graph_engine import _card_attrs_for_filter

    return {name: frozenset(_card_attrs_for_filter(row)) for name, row in attr_rows.items()}


def _bulk_load_lord_continuous_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, str], ...]:
    """Static ``Continuous`` ports with a non-empty affected_scope.

    Consumed by ``_find_lord_complements``. Returned as a tuple of
    ``(card_name, affected_scope, branch_kind)`` triples.
    """
    return tuple(
        (
            row["card_name"],
            row["affected_scope"] or "",
            row["branch_kind"] or "",
        )
        for row in conn.execute(
            "SELECT card_name, affected_scope, branch_kind FROM card_ports "
            "WHERE port_type = 'static' AND event_class = 'Continuous' "
            "AND affected_scope IS NOT NULL AND affected_scope != ''"
        )
    )


def _bulk_load_proliferate_cards(conn: sqlite3.Connection) -> frozenset[str]:
    """Card names that actively proliferate (effect or trigger port).

    Consumed by ``_find_proliferate_synergy``.
    """
    return frozenset(
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE event_class = 'Proliferate' "
            "AND port_type IN ('effect', 'trigger')"
        )
    )


def _bulk_load_p1p1_counter_producers(conn: sqlite3.Connection) -> frozenset[str]:
    """Card names that put +1/+1 (or generic) counters.

    Consumed by the P1P1 branch of ``_find_scales_with_density``.
    """
    return frozenset(
        row["card_name"]
        for row in conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE port_type = 'effect' AND event_class IN "
            "('PutCounter', 'PutCounterAll', 'Proliferate') "
            "AND (counter_type IS NULL OR counter_type = '' "
            "OR counter_type = 'P1P1')"
        )
    )


def _bulk_load_stax_cards_by_event(
    conn: sqlite3.Connection,
) -> dict[str, frozenset[str]]:
    """Per-event set of card names with a ``static`` port matching that event.

    Pre-loads the result of every ``SELECT DISTINCT card_name FROM card_ports
    WHERE port_type='static' AND event_class IN (...)`` query that
    ``_build_stax_exclusion`` would issue at runtime, keyed by the
    individual event class so core.py can union the axes that apply to
    the current commander.
    """
    placeholders = ",".join("?" * len(_STAX_EVENT_CLASSES))
    acc: dict[str, set[str]] = {ev: set() for ev in _STAX_EVENT_CLASSES}
    for row in conn.execute(
        f"SELECT DISTINCT card_name, event_class FROM card_ports "
        f"WHERE port_type = 'static' AND event_class IN ({placeholders})",
        _STAX_EVENT_CLASSES,
    ):
        acc[row["event_class"]].add(row["card_name"])
    return {ev: frozenset(names) for ev, names in acc.items()}


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
