"""Score aggregation (SPEC §7.1, §7.2).

Phase 4-final cleanup: ``score_all_candidates`` is now an orchestrator
that delegates each scoring layer to a focused helper. The return type is
a frozen :class:`ScoredCandidates` dataclass with two side-by-side dicts
(``buckets`` and ``matches``) so callers no longer have to mutate a
``_matches`` side-channel out of the bucket dict.

Layer order matches the LightGBM reference:

1. Trigger feeders (port_match + cost_synergy)
2. Catch-all card-identity matchers
3. Scaling / deck-hints / 2-hop chains
4. Lord / amplifier / internal-synergy
5. Causal graph metrics (opt-in, scipy/numpy/pure-Python fallback chain)
6. Strategic heuristic rules
7. Staples list
8. Replacement anti-synergy
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .graph_cache import (
    cache_is_populated,
    commander_neighbours as _cache_commander_neighbours,
    load_card_metrics as _load_card_metrics,
    neighbours_of as _cache_neighbours_of,
)
from .graph_engine import (
    _rows_to_dicts,
    detect_internal_synergies,
    find_amplifier_matches,
    find_catchall_card_matches,
    find_chain_matches,
    find_counter_producer_payoff,
    find_deckhints_matches,
    find_etb_self_matches,
    find_graveyard_value_synergies,
    find_lord_matches,
    find_mana_restriction_matches,
    find_replacement_conflicts,
    find_sacrifice_synergies,
    find_scaling_matches,
    find_trigger_feeders,
    internal_synergy_boost,
    load_ports_for_set,
)
from .graph_metrics import (
    HAS_NUMPY,
    ScipyPRContext,
    build_causal_graph,
    card_hub_scores,
    compute_commander_metrics,
    fast_personalised_pagerank,
    scipy_personalised_pagerank_cached,
)
from .heuristics import (
    STAPLE_SCORE,
    STAPLES,
    active_rules_for_commander,
    evaluate_strategic_rules,
)

PortRow = dict[str, Any]
BucketDict = dict[str, float]
MatchList = list[dict[str, Any]]


# ---------------------------------------------------------------------------
# §7.2 branch weighting
# ---------------------------------------------------------------------------

BRANCH_MULTIPLIER: dict[str, float] = {
    "root":                  1.0,
    "execute":               1.0,
    "subability":            1.0,
    "true":                  0.5,
    "false":                 0.5,
    "win":                   0.5,
    "otherwise":             0.5,
    "repeat":                1.0,
    "change_zone_table":     1.0,
    "static_condition":      0.75,
    "replacement_condition": 0.75,
}


def _branch_weight(branch_kind: str | None) -> float:
    return BRANCH_MULTIPLIER.get(branch_kind or "root", 1.0)


# Re-export so existing callers (test_graph_engine, package __init__) keep
# working without touching their import paths. The implementation lives in
# parser.py where ``CHAIN_KEYS`` is the source of truth.
from .parser import parser_branch_kinds  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Score weights (§7.1)
# ---------------------------------------------------------------------------

PORT_MATCH_WEIGHT       = 10
CATCH_ALL_WEIGHT        = 1   # weak per-card; v3 halved from 2
COST_FEED_WEIGHT        = 6   # v3 raised from 4 — strong signal
SACRIFICE_SYNERGY_WEIGHT = 4  # Phase D1 — additive on top of cost_feed
COUNTER_SYNERGY_WEIGHT   = 4  # Phase F1 — +1/+1 counter producer → payoff commander
GRAVEYARD_SYNERGY_WEIGHT = 4  # Phase F5 — graveyard filler → reanimator commander
MANA_RESTRICTION_WEIGHT  = 2  # Phase D2 — folds into cost_synergy bucket
SCALING_WEIGHT          = 6   # v3 raised from 5
DECKHINTS_WEIGHT        = 4   # v3 raised from 3 — Forge's own annotations
CHAIN_WEIGHT            = 3
LORD_WEIGHT             = 12  # v3 raised from 6 — tribal lord must beat staples
AMPLIFIER_WEIGHT        = 10  # v3 raised from 7
INTERNAL_SYNERGY_WEIGHT = 1
REPLACEMENT_WEIGHT      = 10  # negative — applied as -weight per match

# §6.8 graph-metric bucket weights — Round 9 calibration. The personalised
# PR signal is the LightGBM ``graph_pagerank`` feature (9.2 % importance).
# CARD_HUB stays at 0 because global popularity biases toward staples.
GRAPH_NEIGHBOR_OVERLAP_WEIGHT = 3
GRAPH_PAGERANK_WEIGHT         = 2
CARD_HUB_WEIGHT               = 0
CMDR_2HOP_WEIGHT              = 2

# §7.3 / §7.4
STRATEGIC_WEIGHT = 2  # v3 raised from 1
STAPLE_WEIGHT    = 1  # STAPLE_SCORE is already in score units

# §7.5 — resource-density layer. A commander whose cost consumes a card-typed
# resource (``tapXType<1/Artifact>`` for Urza, ``Sac<1/Creature>`` for a
# sacrifice outlet, ``ExileFromGrave<1/Creature>`` for delve) wants the deck
# packed with cards of that type even when there's no port-level interaction.
# Default sized between cost_synergy (6) and port_match (10) so it lifts a
# clean tap-artifact density signal above unrelated port matches without
# flooding the top of the page when an anchor is broad.
RESOURCE_DENSITY_WEIGHT = 8

# Per-anchor weight overrides — scaled inversely by population fraction so a
# broad anchor like ``Creature`` (~50% of the legal pool) doesn't flood the
# top of every Korvold-class commander's page above genuine staples. Anchors
# not listed here use :data:`RESOURCE_DENSITY_WEIGHT`.
_ANCHOR_WEIGHT: dict[str, int] = {
    "Creature":     4,   # ~50% of the pool — half weight to avoid flooding
    "Sorcery":      6,
    "Instant":      6,
}

#: Anchors that additionally cap their match by cmc. Sacrifice and
#: death-trigger commanders want *cheap* fodder, not random mid-range
#: creatures. Cards with ``cmc IS NULL`` (suspend / costless) also fall
#: outside the cap and are skipped — fodder must be castable cheaply.
_BROAD_ANCHOR_CMC_CAP: dict[str, int] = {
    "Creature": 3,
}

#: §7.5 anchor-quality — recursion keywords that make a creature top-tier
#: fodder for sacrifice / death-trigger / reanimator commanders. A creature
#: with ``Undying`` or ``Persist`` comes back from the graveyard for free;
#: ``Unearth`` / ``Embalm`` / ``Eternalize`` / ``Escape`` / ``Encore``
#: recur it from the graveyard for a single cost. These cards are
#: dramatically better Korvold / Meren / Yawgmoth / Teysa fodder than a
#: comparable vanilla creature of the same cmc, but the flat direct-match
#: weight treats them identically. We add an extra ``RECURSION_BONUS`` on
#: top of the base anchor weight for cards that carry any of these keywords.
_RECURSION_KEYWORDS: frozenset[str] = frozenset({
    "Undying", "Persist", "Unearth",
    "Embalm", "Eternalize", "Escape",
    "Encore", "Scavenge",
})
RECURSION_ANCHOR_BONUS: int = 4

# §7.7 replacement-resonance layer.
#
# Doubling Season / Hardened Scales / Primal Vigor / Vorinclex / Pir are
# ``port_type=replacement`` rows that double a counter or token event. They
# had no matcher prior to this layer because the trigger feeders matcher
# joins commander triggers to candidate effects, not to candidate
# replacements. Atraxa's #1 EDHREC pick (Doubling Season) was scoring 0.
#
# We map each commander effect ``event_class`` to the set of replacement
# events that double / amplify it. The mapping is curated rather than
# identity-based because Forge uses different vocabulary for the cause
# (commander effect ``PutCounter`` / ``Proliferate``) and the replacement
# event (``AddCounter``).
_REPLACEMENT_RESONANCE: dict[str, frozenset[str]] = {
    # +1/+1 counter producers and proliferate both add counters
    "PutCounter":     frozenset({"AddCounter"}),
    "PutCounterAll":  frozenset({"AddCounter"}),
    "Proliferate":    frozenset({"AddCounter"}),
    # Token producers want token doublers
    "Token":          frozenset({"CreateToken"}),
    # Card draw doublers (Alhammarret's Archive)
    "Draw":           frozenset({"Draw", "DrawCards"}),
    # Lifegain doublers (Alhammarret's Archive again)
    "GainLife":       frozenset({"GainLife"}),
    # Mana doublers (Mana Reflection, Vorinclex Voice of Hunger)
    "Mana":           frozenset({"ProduceMana"}),
}

REPLACEMENT_RESONANCE_WEIGHT = 10


# §7.6 effect-resonance layer.
#
# Some effects compound when more sources are present in the same deck:
# Proliferate (each Proliferate counts every counter on every permanent),
# self-mill (more mill = more graveyard density), Scry/Surveil (more = more
# library sculpting), Untap (untap-matters decks). For these effects we
# award candidates that share the same effect class as the commander.
#
# Critically *not* in the resonance set: ``Token`` (every commander has
# token effects), ``Draw`` (every blue commander draws), ``Damage``,
# ``Sacrifice``. These would create the same flooding bug the trigger
# feeder Self-only fix removed.
_RESONANT_EFFECTS: frozenset[str] = frozenset({
    "Proliferate",
    "Mill",
    "Scry",
    "Surveil",
    "Untap",
    "Investigate",
})

EFFECT_RESONANCE_WEIGHT = 10


BUCKETS: tuple[str, ...] = (
    "port_match",
    "catchall",
    "cost_synergy",
    "sacrifice_synergy",   # Phase D1 — outlet ↔ payoff cluster
    "counter_synergy",     # Phase F1 — P1P1 producer ↔ counter-payoff cmdr
    "graveyard_synergy",   # Phase F5 — grave filler ↔ reanim commander
    "scaling",
    "deck_hints",
    "chain",
    "lord",
    "amplifier",
    "internal_synergy",
    "graph_metrics",
    "strategic",
    "resource_density",
    "effect_resonance",
    "replacement_resonance",
    "staple",
    "replacement",
)

_EMPTY_BUCKETS_TEMPLATE: BucketDict = dict.fromkeys(BUCKETS, 0.0)
_EMPTY_BUCKETS_TEMPLATE["total"] = 0.0


def empty_buckets() -> BucketDict:
    """Return a fresh zeroed bucket dict — one allocation per call."""
    return dict(_EMPTY_BUCKETS_TEMPLATE)


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredCandidates:
    """Output of :func:`score_all_candidates`.

    ``buckets`` maps each candidate name to its per-bucket dict (with the
    final ``total`` summed in). ``matches`` maps each candidate to its list
    of contributing-match records (used by the engine to build
    ``ContributingPort`` rows in §8 score breakdowns).

    The two dicts share the same keyspace but are kept separate so the
    public bucket dict stays cleanly typed as ``dict[str, float]``.

    Mutability note: ``frozen=True`` only blocks attribute rebinding —
    callers can still mutate the contents of ``buckets`` or ``matches``
    in-place. Callers SHOULD treat the result as read-only and copy the
    bucket dict if they need to layer extra penalties or annotations
    (the engine does this in :meth:`SynergyEngine.page`).
    """

    buckets: dict[str, BucketDict] = field(default_factory=dict)
    matches: dict[str, MatchList]  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal aggregation helpers — each writes into the shared scores/matches
# ---------------------------------------------------------------------------


def _add_bucket(
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
    candidate: str,
    bucket: str,
    delta: float,
    match_record: dict[str, Any],
) -> None:
    """Increment a candidate's bucket and append a match record."""
    if candidate not in scores:
        scores[candidate] = empty_buckets()
        matches[candidate] = []
    scores[candidate][bucket] += delta
    matches[candidate].append(match_record)


def _score_trigger_feeders(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Port_match and cost_synergy buckets via :func:`find_trigger_feeders`.

    Dedupes by ``(candidate, trigger_event, effect_event, match_kind)`` so
    a card with many copies of the same mechanical interaction (e.g.
    Invoke Despair has 137 DBSacrifice sub-abilities) is credited once
    per distinct interaction, not 137 times.
    """
    feed_buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in find_trigger_feeders(conn, commander_set):
        key = (
            row["candidate"],
            row["trigger_event"] or "",
            row["effect_event"] or "",
            row["match_kind"],
        )
        existing = feed_buckets.get(key)
        if existing is None or _branch_weight(row["branch_kind"]) > _branch_weight(existing["branch_kind"]):
            feed_buckets[key] = row

    for row in feed_buckets.values():
        weight = _branch_weight(row["branch_kind"])
        if row["match_kind"] == "effect":
            bucket = "port_match"
            delta = PORT_MATCH_WEIGHT * weight
        else:
            bucket = "cost_synergy"
            delta = COST_FEED_WEIGHT * weight
        _add_bucket(scores, matches, row["candidate"], bucket, delta,
                    {**row, "bucket": bucket})


def _score_etb_self(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """ETB self-match: candidates whose card row IS the entering card the
    commander's trigger fires on. Awards :data:`PORT_MATCH_WEIGHT` once per
    matched (candidate, trigger event) pair — the same weight as the trigger
    feeders matcher because the synergy is mechanically equivalent.
    """
    seen: set[tuple[str, str]] = set()
    for row in find_etb_self_matches(conn, commander_set):
        key = (row["candidate"], row["trigger_event"])
        if key in seen:
            continue
        seen.add(key)
        weight = _branch_weight(row.get("branch_kind"))
        _add_bucket(
            scores, matches, row["candidate"], "port_match",
            PORT_MATCH_WEIGHT * weight,
            {**row, "bucket": "port_match", "match_kind": "etb_self"},
        )


def _score_catchall(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    for row in find_catchall_card_matches(conn, commander_set):
        _add_bucket(scores, matches, row["candidate"], "catchall",
                    CATCH_ALL_WEIGHT, {**row, "bucket": "catchall"})


def _score_scaling(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    for row in find_scaling_matches(conn, commander_set):
        weight = _branch_weight(row["branch_kind"])
        _add_bucket(scores, matches, row["scaling_card"], "scaling",
                    SCALING_WEIGHT * weight, {**row, "bucket": "scaling"})


#: Forward-direction deck_hints (candidate's hints/needs ↔ commander's
#: identity) is the original signal — high precision, low recall. Reverse
#: direction (commander's has/wants ↔ candidate's has) is broader and
#: higher-recall but more prone to flooding the top-30 with marginally
#: relevant cards. We weight reverse half as much as forward.
DECKHINTS_REVERSE_WEIGHT = 2

#: Phase F2 — multi-source combo bonus. When a candidate hits BOTH a
#: forward source (``hints`` or ``needs``) AND a reverse source
#: (``has_has`` or ``wants_has``), Forge is effectively annotating the
#: match from both ends: the card advertises it wants the commander's
#: identity AND independently advertises it provides what the commander
#: needs. This is a stronger signal than either single-source match,
#: so we add one flat bonus the first time both halves are present for
#: a given candidate.
DECKHINTS_COMBO_WEIGHT = 2

_FORWARD_DECKHINTS_SOURCES = frozenset({"hints", "needs"})
_REVERSE_DECKHINTS_SOURCES = frozenset({"has_has", "wants_has"})


def _score_deckhints(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Award the deck_hints bucket once per ``(candidate, source)`` pair,
    plus a one-off combo bonus for forward+reverse pair matches.

    The bidirectional matcher emits one row per matched tag — a card with
    three different ability matches yields three rows. Without dedupe at
    this layer, the bucket inflates linearly with tag count and broad-tag
    commanders (Talrand has_has Token, Slimefoot has_has Token) flood
    their top-30 with token-producing artifacts that aren't real picks.

    Forward sources (``hints``, ``needs``) get the full weight — they're
    high-precision (the candidate is *advertising* synergy with the
    commander's identity). Reverse sources (``has_has``, ``wants_has``)
    get half weight because the broader has-tag universe (Token,
    Counters) tends to flood otherwise.

    Phase F2: if a candidate accumulates at least one forward AND at
    least one reverse source hit, add :data:`DECKHINTS_COMBO_WEIGHT`
    exactly once. This lifts Forge's "3-source full-match" cluster
    (Heronblade Elite / Tuskguard Captain / Abzan Falconer class) above
    the 1-source counter_synergy tier that was accidentally tied with
    them in Phase F1.
    """
    seen: set[tuple[str, str]] = set()
    fwd_hit: set[str] = set()
    rev_hit: set[str] = set()
    combo_awarded: set[str] = set()
    for row in find_deckhints_matches(conn, commander_set):
        key = (row["candidate"], row["source"])
        if key in seen:
            continue
        seen.add(key)
        candidate = row["candidate"]
        source = row["source"]
        weight = (
            DECKHINTS_WEIGHT
            if source in _FORWARD_DECKHINTS_SOURCES
            else DECKHINTS_REVERSE_WEIGHT
        )
        _add_bucket(scores, matches, candidate, "deck_hints",
                    weight, {**row, "bucket": "deck_hints"})

        if source in _FORWARD_DECKHINTS_SOURCES:
            fwd_hit.add(candidate)
        else:
            rev_hit.add(candidate)

        if (
            candidate not in combo_awarded
            and candidate in fwd_hit
            and candidate in rev_hit
        ):
            combo_awarded.add(candidate)
            _add_bucket(
                scores, matches, candidate, "deck_hints",
                DECKHINTS_COMBO_WEIGHT,
                {
                    "candidate":  candidate,
                    "source":     "combo",
                    "kind":       "pair",
                    "matched":    ["forward+reverse"],
                    "bucket":     "deck_hints",
                },
            )


def _score_chains(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """2-hop chain detection. Opt-in via ``use_chain_matches`` because the
    intermediate-rebuild loop is the dominant cost on a 32 k-card import."""
    for row in find_chain_matches(conn, commander_set):
        weight = _branch_weight(row["branch_kind"])
        _add_bucket(scores, matches, row["candidate"], "chain",
                    CHAIN_WEIGHT * weight, {**row, "bucket": "chain"})


def _score_lords(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    for row in find_lord_matches(conn, commander_set):
        weight = _branch_weight(row["branch_kind"])
        _add_bucket(scores, matches, row["lord_card"], "lord",
                    LORD_WEIGHT * weight, {**row, "bucket": "lord"})


def _score_amplifiers(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    for row in find_amplifier_matches(conn, commander_set):
        weight = _branch_weight(row["branch_kind"])
        _add_bucket(scores, matches, row["amplifier_card"], "amplifier",
                    AMPLIFIER_WEIGHT * weight, {**row, "bucket": "amplifier"})


def _score_internal_synergy(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    cmdr_ports: list[PortRow],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """§6.9.3 internal synergy boost — only meaningful for partner pairs."""
    internal = detect_internal_synergies(cmdr_ports, commander_set)
    engine_events = {m["event_class"] for m in internal if m.get("event_class")}
    if not engine_events:
        return

    placeholders = ",".join("?" * len(engine_events))
    cur = conn.execute(
        f"SELECT card_name, port_type, event_class FROM card_ports "
        f"WHERE event_class IN ({placeholders})",
        tuple(engine_events),
    )
    cands_seen: dict[str, list[PortRow]] = defaultdict(list)
    for r in cur.fetchall():
        cands_seen[r["card_name"]].append(
            {"port_type": r["port_type"], "event_class": r["event_class"]}
        )
    cmdr_set = set(commander_set)
    sorted_events = sorted(engine_events)
    for cand_name, cand_ports in cands_seen.items():
        if cand_name in cmdr_set:
            continue
        boost = internal_synergy_boost(cand_ports, engine_events)
        if boost:
            _add_bucket(
                scores, matches, cand_name, "internal_synergy",
                boost * INTERNAL_SYNERGY_WEIGHT,
                {"bucket": "internal_synergy", "engine_events": sorted_events},
            )


def _score_replacements(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Anti-synergy from Prevent-style and Phase C2 substitution-style
    replacement effects. Substitutions are half-weighted — the effect is
    still blocking the commander's trigger, but the card often also
    offers positive utility (Rest in Peace replaces death with exile,
    which at least removes threats), so the anti-synergy is softer.
    """
    for row in find_replacement_conflicts(conn, commander_set):
        weight = _branch_weight(row["branch_kind"])
        multiplier = 0.5 if row.get("match_kind") == "substitution" else 1.0
        _add_bucket(
            scores, matches, row["anti_synergy_card"], "replacement",
            -REPLACEMENT_WEIGHT * weight * multiplier,
            {**row, "bucket": "replacement"},
        )


def _score_mana_restriction(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Phase D2 — mana-restriction positive boost.

    Folds into the existing ``cost_synergy`` bucket so the per-bucket
    layout doesn't grow. The signal is mechanically a "cost-feeds-cmdr"
    relationship — the candidate's restricted mana would be wasted
    elsewhere but is exactly what the commander wants to spend on.
    """
    for row in find_mana_restriction_matches(conn, commander_set):
        weight = _branch_weight(row.get("branch_kind"))
        _add_bucket(
            scores, matches, row["candidate"], "cost_synergy",
            MANA_RESTRICTION_WEIGHT * weight,
            {**row, "bucket": "cost_synergy", "match_kind": "mana_restriction"},
        )


def _score_sacrifice_synergies(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Phase D1 — outlet ↔ payoff cluster boost.

    Awards :data:`SACRIFICE_SYNERGY_WEIGHT` once per (candidate, direction)
    pair from :func:`find_sacrifice_synergies`. Stacks additively on top
    of the existing ``cost_synergy`` bucket so a card that is BOTH a real
    outlet (Viscera Seer, cost_target=any) AND fed via the cost-feed
    matcher gets credit in both buckets — the additional bonus is
    intentional and reflects that real outlets are mechanically distinct
    from suspend-class self-sacrifice cards.
    """
    for row in find_sacrifice_synergies(conn, commander_set):
        weight = _branch_weight(row.get("branch_kind"))
        _add_bucket(
            scores, matches, row["candidate"], "sacrifice_synergy",
            SACRIFICE_SYNERGY_WEIGHT * weight,
            {**row, "bucket": "sacrifice_synergy"},
        )


def _score_counter_producer_payoff(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Phase F1 — +1/+1 counter producer ↔ counter-payoff commander.

    Gated on :func:`find_counter_producer_payoff` (itself gated on
    :func:`_commander_is_p1p1_payoff`). Awards
    :data:`COUNTER_SYNERGY_WEIGHT` once per candidate for commanders
    like Kyler, Bess, Grakmaw, Hallar and Qala — the 14 legendary
    creatures whose mechanic reads the ``CardCounters.P1P1`` or
    ``CardCounters.ALL + PutCounter(P1P1)`` signature.

    The bucket closes a long-standing gap where cards like Maester
    Seymour (targeted P1P1 pump) and Hardened Scales (replacement
    doubler) were credited only by the weak ``port_match`` path, even
    though they are the central mechanical engines for counter decks.
    """
    for row in find_counter_producer_payoff(conn, commander_set):
        weight = _branch_weight(row.get("branch_kind"))
        _add_bucket(
            scores, matches, row["candidate"], "counter_synergy",
            COUNTER_SYNERGY_WEIGHT * weight,
            {**row, "bucket": "counter_synergy"},
        )


def _score_graveyard_value(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Phase F5 — graveyard filler / reanimator-cluster bucket.

    Gated on :func:`_commander_is_graveyard_value` (reanimation
    trigger, ``ValidGraveyard`` scaling, ``MayPlay`` + ``AffectedZone:
    Graveyard``, or ``STYardCast``). Awards
    :data:`GRAVEYARD_SYNERGY_WEIGHT` once per candidate across three
    tiers: ``library_to_grave`` (Buried Alive / Entomb),
    ``self_mill`` (Stitcher's Supplier / Mesmeric Orb), and
    ``reanimator_cluster`` (Doomed Necromancer / Reassembling Skeleton
    / persist creatures).

    Closes the biggest blind spot surfaced by the F4 golden-set
    diagnostic map: 5 commanders (Chainer, Karador, Meren, Gisa and
    Geralf, Muldrotha) were at NDCG@30 < 0.04 because no existing
    matcher connected "fill my graveyard" to "my commander cashes the
    graveyard out".
    """
    for row in find_graveyard_value_synergies(conn, commander_set):
        weight = _branch_weight(row.get("branch_kind"))
        _add_bucket(
            scores, matches, row["candidate"], "graveyard_synergy",
            GRAVEYARD_SYNERGY_WEIGHT * weight,
            {**row, "bucket": "graveyard_synergy"},
        )


# ---------------------------------------------------------------------------
# §6.8 graph metrics — three fallback paths
# ---------------------------------------------------------------------------


def _score_graph_metrics_numpy(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
    *,
    adjacency_cache: dict[str, set[str]] | None,
    scipy_pr_context: ScipyPRContext | None,
) -> None:
    """Numpy/scipy live path. Personalised PR + Python top-K Jaccard.

    Memory note: an earlier attempt vectorised the per-pair Jaccard /
    2-hop loop with a symmetric scipy CSR adjacency matrix; that doubled
    context memory to ~1.5 GB at 32 k cards and OOM'd. The Python top-K
    bounded loop here is slower per call (~2 s on 32 k) but uses
    essentially zero extra memory.
    """
    adjacency = adjacency_cache if adjacency_cache is not None else build_causal_graph(conn)
    cmdr_set = set(commander_set)

    # Personalised PR per commander, merged via max across the partner pair.
    merged_pr: dict[str, float] = {}
    for cmdr_name in commander_set:
        if cmdr_name not in adjacency:
            continue
        if scipy_pr_context is not None:
            pr = scipy_personalised_pagerank_cached(scipy_pr_context, cmdr_name, iterations=10)
        else:
            pr = fast_personalised_pagerank(adjacency, cmdr_name, iterations=10)
        for card, val in pr.items():
            if card in cmdr_set:
                continue
            if val > merged_pr.get(card, 0.0):
                merged_pr[card] = val

    GRAPH_TOPK = 5000
    top_candidates = sorted(merged_pr.items(), key=lambda kv: -kv[1])[:GRAPH_TOPK]
    if not top_candidates:
        return

    max_pr = top_candidates[0][1]
    hubs = card_hub_scores(adjacency)

    # Build commander 1-hop and 2-hop sets once.
    cmdr_neighbours: set[str] = set()
    for cmdr_name in commander_set:
        cmdr_neighbours |= adjacency.get(cmdr_name, set())
    cmdr_neighbours -= cmdr_set

    cmdr_2hop: set[str] = set(cmdr_neighbours)
    for n in cmdr_neighbours:
        cmdr_2hop |= adjacency.get(n, set())
    cmdr_2hop -= cmdr_set

    for cand_name, raw_pr in top_candidates:
        cand_neighbours = adjacency.get(cand_name, set())
        if cmdr_neighbours and cand_neighbours:
            inter = len(cmdr_neighbours & cand_neighbours)
            union = len(cmdr_neighbours) + len(cand_neighbours) - inter
            overlap = inter / union if union else 0.0
        else:
            overlap = 0.0
        two_hop = (
            len(cmdr_2hop & cand_neighbours) / len(cand_neighbours)
            if cand_neighbours
            else 0.0
        )
        norm_pr = raw_pr / max_pr if max_pr else 0.0
        contribution = (
            overlap            * GRAPH_NEIGHBOR_OVERLAP_WEIGHT
            + norm_pr          * GRAPH_PAGERANK_WEIGHT
            + hubs.get(cand_name, 0.0) * CARD_HUB_WEIGHT
            + two_hop          * CMDR_2HOP_WEIGHT
        )
        if contribution > 0:
            _add_bucket(
                scores, matches, cand_name, "graph_metrics", contribution,
                {
                    "bucket":                 "graph_metrics",
                    "graph_pagerank":         norm_pr,
                    "card_hub_score":         hubs.get(cand_name, 0.0),
                    "graph_neighbor_overlap": overlap,
                    "cmdr_2hop_ratio":        two_hop,
                },
            )


def _score_graph_metrics_cache(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Phase 4.4 cache fallback path: per-card metric lookup + per-pair
    Jaccard from the ``causal_neighbours`` table."""
    cached_metrics = _load_card_metrics(conn)
    cmdr_neighbours = _cache_commander_neighbours(conn, commander_set)
    cmdr_set = set(commander_set)

    cmdr_2hop: set[str] = set(cmdr_neighbours)
    if cmdr_neighbours:
        step2 = _cache_neighbours_of(conn, cmdr_neighbours)
        for neigh_set in step2.values():
            cmdr_2hop |= neigh_set
        cmdr_2hop -= cmdr_set

    # Bulk-load every candidate's neighbours in one query instead of
    # issuing one ``SELECT ... WHERE card_name IN (?)`` per candidate.
    # The previous per-loop call was the same O(N) SQL pattern flagged
    # earlier for ``apply_penalties``.
    candidate_names = [n for n in cached_metrics if n not in cmdr_set]
    all_neighbours = _cache_neighbours_of(conn, candidate_names)

    for cand_name, cached in cached_metrics.items():
        if cand_name in cmdr_set:
            continue
        cand_neighbours = all_neighbours.get(cand_name, set())
        if cmdr_neighbours and cand_neighbours:
            inter = len(cmdr_neighbours & cand_neighbours)
            union = len(cmdr_neighbours | cand_neighbours)
            overlap = inter / union if union else 0.0
        else:
            overlap = 0.0
        two_hop = (
            len(cmdr_2hop & cand_neighbours) / len(cand_neighbours)
            if cand_neighbours
            else 0.0
        )
        contribution = (
            overlap            * GRAPH_NEIGHBOR_OVERLAP_WEIGHT
            + cached.pagerank  * GRAPH_PAGERANK_WEIGHT
            + cached.hub_score * CARD_HUB_WEIGHT
            + two_hop          * CMDR_2HOP_WEIGHT
        )
        if contribution > 0:
            _add_bucket(
                scores, matches, cand_name, "graph_metrics", contribution,
                {
                    "bucket":                 "graph_metrics",
                    "card_hub_score":         cached.hub_score,
                    "graph_pagerank":         cached.pagerank,
                    "graph_neighbor_overlap": overlap,
                    "cmdr_2hop_ratio":        two_hop,
                },
            )


def _score_graph_metrics_pure_python(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Last-resort path: full adjacency rebuild + dict-based PageRank.
    Practical only for fixture sets — minutes per call at 32 k scale."""
    adjacency = build_causal_graph(conn)
    metrics = compute_commander_metrics(adjacency, commander_set)
    for cand_name, m in metrics.items():
        contribution = (
            m["graph_neighbor_overlap"] * GRAPH_NEIGHBOR_OVERLAP_WEIGHT
            + m["graph_pagerank"]        * GRAPH_PAGERANK_WEIGHT
            + m["card_hub_score"]        * CARD_HUB_WEIGHT
            + m["cmdr_2hop_ratio"]       * CMDR_2HOP_WEIGHT
        )
        if contribution > 0:
            _add_bucket(
                scores, matches, cand_name, "graph_metrics", contribution,
                {"bucket": "graph_metrics", **m},
            )


def _score_graph_metrics(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
    *,
    adjacency_cache: dict[str, set[str]] | None,
    scipy_pr_context: ScipyPRContext | None,
) -> None:
    """Dispatch to numpy → cache → pure-Python in priority order."""
    if HAS_NUMPY:
        _score_graph_metrics_numpy(
            conn, commander_set, scores, matches,
            adjacency_cache=adjacency_cache,
            scipy_pr_context=scipy_pr_context,
        )
    elif cache_is_populated(conn):
        _score_graph_metrics_cache(conn, commander_set, scores, matches)
    else:
        _score_graph_metrics_pure_python(conn, commander_set, scores, matches)


# ---------------------------------------------------------------------------
# §7.3 strategic + §7.4 staples
# ---------------------------------------------------------------------------


def _score_strategic(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    cmdr_ports: list[PortRow],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Strategic heuristic rules — only evaluated for candidates that
    already have at least one mechanical signal.

    The signal gate is intentional: strategic rule boosts are heuristic
    catch-alls (``mass_pump_for_tokens``, ``selfmill_for_graveyard``, ...)
    that match too broadly when applied to the full 32 k-card pool. Cards
    that need the strategic bucket as their *only* signal must be reached
    by a more targeted matcher (e.g. resource_density, lord, scaling).
    """
    active_rules = active_rules_for_commander(cmdr_ports)
    if not active_rules:
        return

    # Build the signal-gated candidate list in one pass. The previous
    # implementation used ``any(buckets[k] != 0 for k in BUCKETS)`` which
    # generator-called ``any`` 167k times on Meren-class commanders; a
    # flat ``for`` with an early ``break`` on the first non-zero bucket
    # is materially cheaper in CPython.
    candidates_with_signal: list[str] = []
    for name, buckets in scores.items():
        for k in BUCKETS:
            if buckets[k] != 0:
                candidates_with_signal.append(name)
                break
    if not candidates_with_signal:
        return

    placeholders = ",".join("?" * len(candidates_with_signal))
    card_rows = conn.execute(
        f"SELECT name, keywords, cmc FROM cards WHERE name IN ({placeholders})",
        tuple(candidates_with_signal),
    ).fetchall()
    cand_cards = {row["name"]: row for row in _rows_to_dicts(card_rows)}

    port_rows = conn.execute(
        f"SELECT * FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(candidates_with_signal),
    ).fetchall()
    cand_ports: dict[str, list[PortRow]] = defaultdict(list)
    for port in _rows_to_dicts(port_rows):
        cand_ports[port["card_name"]].append(port)

    # The candidates_with_signal guard above ensures every name we process
    # is already in `scores`, so all writes go through `_add_bucket` which
    # is the single owner of the scores/matches mutation invariant.
    for cand_name, card_row in cand_cards.items():
        rec = {"card": card_row, "ports": cand_ports.get(cand_name, [])}
        weight, reasons = evaluate_strategic_rules(active_rules, rec)
        if not weight:
            continue
        # Apply the rule's full weight as a single bucket increment, then
        # log every reason as its own match record so the §8 score
        # breakdown can list them individually.
        delta = weight * STRATEGIC_WEIGHT
        if reasons:
            # Split the delta evenly so the bucket total still equals
            # `weight * STRATEGIC_WEIGHT` but every reason has a record.
            per_reason = delta / len(reasons)
            for reason in reasons:
                _add_bucket(
                    scores, matches, cand_name, "strategic", per_reason,
                    {"bucket": "strategic", "reason": reason},
                )
        else:
            _add_bucket(
                scores, matches, cand_name, "strategic", delta,
                {"bucket": "strategic"},
            )


# ---------------------------------------------------------------------------
# §7.5 resource-density layer
# ---------------------------------------------------------------------------

#: Cost ``event_class`` values that count as resource anchors. ``tap`` (no
#: subtype) is excluded because it just means "tap self". ``discard`` is
#: excluded because the only common subject is ``Card`` which would match
#: every card. ``exile`` is excluded for the same reason — its dominant
#: subject is ``CARDNAME`` (exile self) or ``Card`` (any card).
_RESOURCE_ANCHOR_EVENTS: frozenset[str] = frozenset({
    "tap_type",
    "untap_type",
    "sacrifice",
    "exile_from_grave",
    "exile_from_hand",
    "exile_from_top",
    "return",
})

#: Subset of :data:`_RESOURCE_ANCHOR_EVENTS` that *consume* the anchored
#: resource — the card is physically removed from the battlefield, hand, or
#: library as a direct result of paying the cost. Consuming anchors inherit
#: a cmc cap across all anchor types (not just Creature) because the payoff
#: commander wants cheap fodder, not mid-range value cards. Non-consuming
#: anchors (``tap_type`` / ``untap_type`` / ``return``) skip the cap —
#: Urza wants Mana Vault / Thran Dynamo, and a cap=3 there would delete his
#: page. ``exile_from_top`` excluded because it targets the library top, not
#: a card the player controls.
_CONSUMING_COST_EVENTS: frozenset[str] = frozenset({
    "sacrifice",
    "exile_from_grave",
    "exile_from_hand",
})

#: Trigger ``event_class`` values that mean "commander rewards a card-typed
#: action". The valid_filter on these triggers tells us *which* type the
#: commander wants in the deck (``Sacrificed | Permanent`` for Korvold,
#: ``ChangesZone`` Battlefield→Graveyard ``| Creature.YouCtrl`` for Teysa).
_TRIGGER_ANCHOR_EVENTS: frozenset[str] = frozenset({"Sacrificed"})

#: ``cost_subtype`` head tokens that are NOT card-type anchors. Counters
#: (LOYALTY/P1P1/CHARGE), self-references (CARDNAME), and the "any card"
#: catchalls (Card / Random) all match too broadly to be useful.
_NON_RESOURCE_SUBJECTS: frozenset[str] = frozenset({
    "CARDNAME",
    "Card",
    "Random",
    "LOYALTY",
    "P1P1",
    "M1M1",
    "CHARGE",
    "TIME",
    "EXPERIENCE",
    "ENERGY",
    "Mana",
})

#: Top-level Magic card types we know how to anchor against
#: ``cards.card_types``. Subtypes (Goblin, Soldier, ...) are matched via
#: ``cards.subtypes`` instead.
_PRIMARY_CARD_TYPES: frozenset[str] = frozenset({
    "Artifact",
    "Creature",
    "Enchantment",
    "Instant",
    "Land",
    "Planeswalker",
    "Sorcery",
    "Tribal",
    "Battle",
})


#: Forge artifact subtypes that are themselves *always* artifacts. When a
#: trigger valid_filter says ``Treasure.YouCtrl``, the matching anchor is
#: ``Artifact`` because every Treasure is an artifact card.
_ARTIFACT_SUBTYPES: frozenset[str] = frozenset({
    "Treasure", "Food", "Clue", "Blood", "Gold", "Map",
    "Powerstone", "Junk", "Incubator", "Shard",
})

#: Leading-word matcher used by :func:`_parse_valid_filter_anchor` — pulls
#: the type token off the head of a Forge filter expression while ignoring
#: ``.YouCtrl`` / ``+Other`` / ``+!token`` qualifiers.
_FILTER_HEAD_RE: re.Pattern[str] = re.compile(r"^([A-Za-z][A-Za-z0-9]*)")

#: Forge ``TokenScript`` strings encode the produced token type in their
#: name. Creature tokens carry an explicit power/toughness pair (``b_1_1_zombie``,
#: ``b_x_x_horror``); artifact subtypes (treasure, food, ...) appear after a
#: ``_a_`` separator or as a recognised Forge artifact-subtype keyword.
_TOKEN_SCRIPT_PT_RE:  re.Pattern[str] = re.compile(r"_(?:\d+|x)_(?:\d+|x)_")
_TOKEN_SCRIPT_ARTIFACT_KEYWORDS: tuple[str, ...] = (
    "_a_", "treasure", "food", "clue", "blood", "gold", "map",
    "powerstone", "junk", "incubator", "shard",
)


def _parse_valid_filter_anchor(valid_filter: str | None) -> list[str]:
    """Decode a Forge ``valid_filter`` head token into anchor type names.

    The valid_filter language is the same one used in cost subtypes but
    without the leading count. Examples:

    * ``Creature.YouCtrl``        → ``["Creature"]``
    * ``Creature.Other+YouCtrl``  → ``["Creature"]``
    * ``Card.Self``               → ``[]`` (self-reference, not an anchor)
    * ``Permanent``               → ``["Creature", "Artifact"]`` — Permanent
      matches every non-spell, far too broad to be a useful anchor on its
      own. We expand to the two realistic high-density sac fodder types.
    * ``Treasure.YouCtrl``        → ``["Artifact"]`` (Treasure subtype is
      an artifact-only marker)
    * ``Card.Self,Creature.Other+YouCtrl`` → ``["Creature"]`` (each
      comma-separated alternative is parsed independently)
    """
    if not valid_filter:
        return []
    out: list[str] = []
    for alt in valid_filter.split(","):
        match = _FILTER_HEAD_RE.match(alt.strip())
        if match is None:
            continue
        head = match.group(1)
        if not head or head in _NON_RESOURCE_SUBJECTS:
            continue
        if head == "Permanent":
            out.append("Creature")
            out.append("Artifact")
            continue
        if head in _ARTIFACT_SUBTYPES:
            out.append("Artifact")
            continue
        out.append(head)
    return out


def _token_script_kind(script: str | None) -> str | None:
    """Classify a Forge ``TokenScript`` as ``"Creature"`` / ``"Artifact"`` / None.

    The classification is heuristic-based but tracks Forge's actual naming
    convention closely:

    * Anything containing an artifact-subtype keyword (``treasure``, ``food``,
      ``_a_``, ...) is an artifact token.
    * Anything with an explicit power/toughness pair (``_1_1_``, ``_x_x_``)
      is a creature token.
    * Named copy tokens (``ajanis_pridemate``) without P/T fall through to
      ``"Creature"`` because that's overwhelmingly what they are in practice.
    * Empty / None → ``None``.
    """
    if not script:
        return None
    s = script.lower()
    for kw in _TOKEN_SCRIPT_ARTIFACT_KEYWORDS:
        if kw in s:
            return "Artifact"
    if _TOKEN_SCRIPT_PT_RE.search(s):
        return "Creature"
    return "Creature"


def _parse_resource_anchor(cost_subtype: str | None) -> list[str]:
    """Decode a Forge ``cost_subtype`` into one or more anchor type names.

    Forge cost subtypes look like ``"<count>/<TypeFilter>"`` where
    ``TypeFilter`` may be one of:

    * a clean type name — ``"1/Artifact"`` → ``["Artifact"]``
    * a filter-qualified type — ``"1/Creature.Other/another creature"``
      → ``["Creature"]`` (we drop the ``.Other`` qualifier and the
      readable trailing description)
    * a multi-type filter — ``"1/Artifact;Creature/artifact or creature"``
      → ``["Artifact", "Creature"]``
    * a ``Permanent`` filter (Baylen's ``2/Permanent.token/token``)
      expanding to ``["Creature", "Artifact"]`` — Permanent is too broad
      to anchor on directly; we expand to the realistic high-density
      sac/tap fodder types (same convention as the trigger anchor parser).
    * an artifact subtype (``Treasure``, ``Food``, ``Clue``, ...) collapses
      to ``["Artifact"]``.
    * a non-card subject — ``"1/LOYALTY"``, ``"1/CARDNAME"`` → ``[]``
      (filtered via :data:`_NON_RESOURCE_SUBJECTS`)
    """
    if not cost_subtype or "/" not in cost_subtype:
        return []
    # Strip the leading count token: "2/Creature" → "Creature".
    _, _, after_count = cost_subtype.partition("/")
    # Drop the readable trailing description if present:
    #   "Creature.Other/another creature" → "Creature.Other"
    head = after_count.partition("/")[0]
    out: list[str] = []
    for raw_type in head.split(";"):
        # Strip filter qualifiers: "Creature.YouCtrl" → "Creature".
        type_name = raw_type.split(".", 1)[0].strip()
        if not type_name or type_name in _NON_RESOURCE_SUBJECTS:
            continue
        if type_name == "Permanent":
            # Same expansion as the trigger anchor parser — Permanent
            # matches every non-spell, too broad to anchor on directly.
            out.append("Creature")
            out.append("Artifact")
            continue
        if type_name in _ARTIFACT_SUBTYPES:
            out.append("Artifact")
            continue
        out.append(type_name)
    return out


_REDUCE_COST_VALID_RE: re.Pattern[str] = re.compile(
    r"['\"]ValidCard['\"]:\s*['\"]([A-Za-z][A-Za-z0-9.+,]*)"
)


#: Per-channel cmc cap for the consuming-resource case. Exposed as a
#: constant so tests can reference it without re-deriving the value.
_CONSUMING_ANCHOR_CMC_CAP: int = 3


def _extract_commander_anchors(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> dict[str, int | None]:
    """Return the set of card-type anchors the commander cares about.

    Three channels feed the anchor set:

    1. **Cost anchors** — the commander pays a card-typed resource (Urza's
       ``tapXType<1/Artifact>``, Yawgmoth's ``Sac<1/Creature.Other>``).
       Parsed from cost ports via :func:`_parse_resource_anchor`.
    2. **Trigger anchors** — the commander rewards a card-typed action
       (Korvold's ``Sacrificed | Permanent``, Teysa's
       ``Creature.YouCtrl ChangesZone Battlefield→Graveyard``). Parsed from
       trigger ports via :func:`_parse_valid_filter_anchor`. ``Permanent``
       expands to ``{Creature, Artifact}`` to avoid false-anchoring on
       lands and enchantments.
    3. **Cost-reduction anchors** — the commander makes a card-typed
       spell cheaper to cast (Animar's ``ReduceCost | Creature``, Urza's
       Incubator's artifact creature discount, Goreclaw's discount on
       creatures with power 4+). Parsed from ``static ReduceCost`` ports.

    All three channels feed the same anchor set so a commander whose
    *only* signal is cost reduction (Animar) gets the same shape of
    resource_density coverage as cost / trigger commanders.
    """
    placeholders = ",".join("?" * len(commander_set))
    # Per-anchor cap tracking. A None value means "uncapped"; an int value
    # means "consuming channel — cap by cmc". When the same anchor is fed
    # by multiple channels, the *tighter* cap wins (cap=3 < None) so a
    # commander with both tap-artifact and sac-artifact payoff still
    # respects the sacrifice cap.
    anchors: dict[str, int | None] = {}

    def _set_cap(anchor: str, cap: int | None) -> None:
        if anchor not in anchors:
            anchors[anchor] = cap
            return
        # Tighter (lower) cap wins; None is weakest.
        if cap is None:
            return
        existing = anchors[anchor]
        if existing is None or cap < existing:
            anchors[anchor] = cap

    # Channel 1 — cost anchors. Consuming events (sacrifice /
    # exile_from_grave / exile_from_hand) impose a cmc cap; non-consuming
    # events (tap_type / untap_type / return / exile_from_top) stay
    # uncapped.
    cost_event_placeholders = ",".join("?" * len(_RESOURCE_ANCHOR_EVENTS))
    cur = conn.execute(
        f"SELECT cost_subtype, event_class FROM card_ports "
        f"WHERE card_name IN ({placeholders}) "
        f"  AND port_type = 'cost' "
        f"  AND event_class IN ({cost_event_placeholders})",
        (*commander_set, *_RESOURCE_ANCHOR_EVENTS),
    )
    for row in cur.fetchall():
        cap = (
            _CONSUMING_ANCHOR_CMC_CAP
            if row["event_class"] in _CONSUMING_COST_EVENTS
            else None
        )
        for anchor in _parse_resource_anchor(row["cost_subtype"]):
            _set_cap(anchor, cap)

    # Channel 2 — trigger anchors. Both ``Sacrificed`` triggers and
    # ``ChangesZone`` (Battlefield → Graveyard) triggers reward the
    # destruction of a card-typed resource → cap=3 for all matched types.
    trigger_event_placeholders = ",".join("?" * len(_TRIGGER_ANCHOR_EVENTS))
    cur = conn.execute(
        f"SELECT valid_filter FROM card_ports "
        f"WHERE card_name IN ({placeholders}) "
        f"  AND port_type = 'trigger' "
        f"  AND ("
        f"      event_class IN ({trigger_event_placeholders}) "
        f"      OR ("
        f"          event_class = 'ChangesZone' "
        f"          AND zone_origin = 'Battlefield' "
        f"          AND zone_destination = 'Graveyard'"
        f"      )"
        f"  )",
        (*commander_set, *_TRIGGER_ANCHOR_EVENTS),
    )
    for row in cur.fetchall():
        for anchor in _parse_valid_filter_anchor(row["valid_filter"]):
            _set_cap(anchor, _CONSUMING_ANCHOR_CMC_CAP)

    # Channel 3 — cost-reduction anchors. ``static ReduceCost`` with a
    # ``ValidCard$`` field that names a *narrow* type makes that type the
    # commander's cost-reduction target. Urza's Incubator's
    # ``ValidCard$ Artifact.Creature`` → Artifact anchor.
    #
    # We DO NOT include the broad ``Creature`` type even when a commander
    # like Animar reduces creature spell costs — adding Creature here
    # floods Rakdos / Goreclaw / other big-creature commanders' top-30
    # with random low-cmc creatures and net-regresses the Golden Set.
    # Animar still gets resource_density coverage via his SpellCast
    # trigger filter; the cost-reduction channel is reserved for narrow,
    # high-precision anchors only.
    cur = conn.execute(
        f"SELECT raw_line FROM card_ports "
        f"WHERE card_name IN ({placeholders}) "
        f"  AND port_type = 'static' "
        f"  AND event_class = 'ReduceCost'",
        commander_set,
    )
    for row in cur.fetchall():
        raw = str(row["raw_line"] or "")
        match = _REDUCE_COST_VALID_RE.search(raw)
        if not match:
            continue
        candidates = _parse_valid_filter_anchor(match.group(1))
        # Skip the broad Creature anchor — see comment above. Cost
        # reduction never consumes the card, so the cap is None.
        for anchor in candidates:
            if anchor == "Creature":
                continue
            _set_cap(anchor, None)

    return anchors


#: Token producers count too — Bitterblossom is perfect sac fodder for
#: Yawgmoth even though Bitterblossom is an Enchantment, because each turn
#: it generates a Faerie creature. We award the producer mirror at half the
#: per-anchor direct weight: producing the type is real synergy but indirect
#: (one-per-turn / one-shot) compared to *being* the type.


def _anchor_weight(anchor: str) -> int:
    """Return the per-anchor direct-match bucket weight."""
    return _ANCHOR_WEIGHT.get(anchor, RESOURCE_DENSITY_WEIGHT)


def _producer_weight(anchor: str) -> int:
    """Return the per-anchor token-producer mirror weight (half direct)."""
    return max(_anchor_weight(anchor) // 2, 1)

#: TokenScript appears in two raw_line shapes — single-quoted (the typical
#: parse_forge_line dict repr) and bare. The pattern handles both.
_TOKEN_SCRIPT_RE: re.Pattern[str] = re.compile(
    r"['\"]?TokenScript['\"]?\s*[:$]\s*['\"]?([A-Za-z0-9_,]+)"
)


def _score_resource_density(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """§7.5 — lift candidates whose card type matches a commander resource anchor.

    Two passes per anchor type ``X``:

    1. **Direct-type pass** — every legal card whose ``card_types`` contains
       ``X`` gets :data:`RESOURCE_DENSITY_WEIGHT`. The bucket is uniform
       per-anchor on purpose: its job is to differentiate "X-density payoff"
       cards from non-X cards, not to rank artifacts against artifacts.
    2. **Token-producer pass** — every card with an ``effect Token`` whose
       :func:`_token_script_kind` resolves to ``X`` gets
       :data:`RESOURCE_DENSITY_PRODUCER_WEIGHT` (half weight). This catches
       enchantment-style "creature factories" like Bitterblossom that the
       direct pass would miss because the *card* isn't a creature.

    A given candidate may receive both bonuses (a creature that also makes
    creature tokens), in which case both contributions add to the bucket.
    """
    anchor_caps = _extract_commander_anchors(conn, commander_set)
    if not anchor_caps:
        return

    cmdr_set = set(commander_set)
    primary = {
        anchor: cap
        for anchor, cap in anchor_caps.items()
        if anchor in _PRIMARY_CARD_TYPES
    }
    # Subtype anchors (e.g. ``Goblin``) are rarer; we'd add them via
    # ``cards.subtypes`` matching here once a real example shows up.
    if not primary:
        return

    # Anchor-quality lookup — only built when any capped anchor is active,
    # since that's the only axis where recursion keywords change the
    # quality of the match. A single query returns the name set; the
    # per-row check is an O(1) ``in`` test.
    needs_quality = any(cap is not None for cap in primary.values())
    recurring_names: frozenset[str] = frozenset()
    if needs_quality:
        # Keyword-based recursion (Undying / Persist / Unearth / ...).
        kw_placeholders = ",".join("?" * len(_RECURSION_KEYWORDS))
        cur_kw = conn.execute(
            f"SELECT DISTINCT card_name FROM card_ports "
            f"WHERE port_type = 'keyword' "
            f"  AND event_class IN ({kw_placeholders})",
            tuple(_RECURSION_KEYWORDS),
        )
        recurring = {r["card_name"] for r in cur_kw.fetchall()}

        # Effect-based recursion — cards with any port that moves ``Card``
        # from Graveyard to Battlefield. This catches Bloodghast (landfall
        # trigger firing a self-return), Reassembling Skeleton (activated
        # ability), Nether Traitor (static trigger), Gravecrawler, etc.
        #
        # LOAD-BEARING CMC CAP: false positives (creatures that recur
        # *another* creature such as Sun Titan cmc=6, Reveillark cmc=5,
        # Karmic Guide cmc=5) are filtered by the cmc cap — every known
        # "other-recurring" creature is cmc≥5 and never reaches the
        # direct pass. If ``_CONSUMING_ANCHOR_CMC_CAP`` is ever raised
        # above 3, revisit this query and add an explicit self-target
        # filter (``valid_filter LIKE '%Card.Self%'`` or equivalent)
        # before the cap change ships.
        cur_effect = conn.execute(
            "SELECT DISTINCT card_name FROM card_ports "
            "WHERE event_class IN ('ChangeZone', 'ChangesZone') "
            "  AND zone_origin = 'Graveyard' "
            "  AND zone_destination = 'Battlefield'"
        )
        recurring.update(r["card_name"] for r in cur_effect.fetchall())

        recurring_names = frozenset(recurring)

    # ----- Pass 1: direct-type anchors --------------------------------
    # One bulk query per anchor type. We cap to ~5 anchors per commander in
    # practice; the per-query overhead is negligible compared to the rest of
    # the scoring pipeline. The LIKE filter is a coarse pre-filter — we then
    # tokenise the result in Python so a row with ``card_types='ArtifactCreature'``
    # (hypothetical future type) wouldn't false-match an ``Artifact`` anchor.
    #
    # Anchors in :data:`_BROAD_ANCHORS` (currently just ``Creature``) cap
    # the match by cmc — sacrifice / death-trigger commanders want *cheap*
    # fodder (Bloodghast, Sakura-Tribe Elder, Reassembling Skeleton), not
    # six-mana value creatures. Without this filter the bucket floods every
    # Korvold-class commander's page with mid-range creatures and pushes
    # genuine staples out of the top 50.
    for anchor in sorted(primary):
        cur = conn.execute(
            "SELECT name, card_types, cmc FROM cards WHERE card_types LIKE ?",
            (f"%{anchor}%",),
        )
        # Channel-derived cap takes precedence; fall back to the static
        # _BROAD_ANCHOR_CMC_CAP dict only for anchors whose channel didn't
        # produce a cap (preserves pre-Round-N behaviour for Animar's
        # reduce-cost Creature channel, which still defaults to cap=3).
        cmc_cap = primary[anchor]
        if cmc_cap is None:
            cmc_cap = _BROAD_ANCHOR_CMC_CAP.get(anchor)
        base_weight = _anchor_weight(anchor)
        for row in cur.fetchall():
            cand = row["name"]
            if cand in cmdr_set:
                continue
            cand_types = (row["card_types"] or "").split()
            if anchor not in cand_types:
                continue
            cand_cmc = row["cmc"]
            if cmc_cap is not None:
                if cand_cmc is None or cand_cmc > cmc_cap:
                    continue

            # Anchor quality — cheaper fodder wins within the cap, and
            # keyworded / effect-based recursion creatures win over
            # vanilla bodies. Both bonuses are additive on top of the
            # base anchor weight and only apply when the anchor has a
            # cmc cap. That keeps Artifact-anchor behaviour untouched
            # for non-consuming commanders (Urza) while lifting
            # Bloodghast / Young Wolf / Nether Traitor above
            # Grizzly-Bears-class vanilla creatures for consuming
            # commanders (Meren, Korvold, Yawgmoth).
            gradient = 0
            is_recurring = False
            if cmc_cap is not None and cand_cmc is not None:
                gradient = max(0, int(cmc_cap - cand_cmc))
                if cand in recurring_names:
                    is_recurring = True
            recursion_bonus = RECURSION_ANCHOR_BONUS if is_recurring else 0
            quality_bonus = gradient + recursion_bonus

            _add_bucket(
                scores, matches, cand, "resource_density",
                base_weight + quality_bonus,
                {
                    "bucket":       "resource_density",
                    "anchor_type":  anchor,
                    "match_kind":   "direct",
                    "cmc":          cand_cmc,
                    "cmc_bonus":    gradient,
                    "is_recurring": is_recurring,
                },
            )

    # ----- Pass 2: token-producer mirror ------------------------------
    # Single bulk query for every Token effect in the DB; we classify each
    # script in Python and award the producer bonus when its type matches
    # an active anchor. This is O(total_tokens) ≈ 5k rows on a 32k import,
    # which is cheaper than running one query per anchor type.
    cur = conn.execute(
        "SELECT card_name, raw_line FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Token'"
    )
    seen_producer: set[tuple[str, str]] = set()
    for row in cur.fetchall():
        cand = row["card_name"]
        if cand in cmdr_set:
            continue
        raw_line = row["raw_line"] or ""
        match = _TOKEN_SCRIPT_RE.search(raw_line)
        if match is None:
            continue
        # Multi-script TokenScripts (``c_a_clue_draw,c_a_food_sac``) split
        # on comma — classify each independently.
        for script in match.group(1).split(","):
            kind = _token_script_kind(script)
            if kind is None or kind not in primary:
                continue
            key = (cand, kind)
            if key in seen_producer:
                continue
            seen_producer.add(key)
            _add_bucket(
                scores, matches, cand, "resource_density",
                _producer_weight(kind),
                {
                    "bucket":      "resource_density",
                    "anchor_type": kind,
                    "match_kind":  "producer",
                    "token_script": script,
                },
            )


def _score_replacement_resonance(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    cmdr_ports: list[PortRow],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """§7.7 — reward replacement-effect doublers that match a commander effect.

    Looks at every commander effect's ``event_class`` and finds the set of
    replacement events that double / amplify it via :data:`_REPLACEMENT_RESONANCE`.
    Then bulk-queries every candidate ``port_type=replacement`` whose
    ``replacement_event`` is in that set.

    Closes the Atraxa Doubling Season gap: Atraxa has ``effect Proliferate``
    → mapped to ``{AddCounter}`` → matches Doubling Season's
    ``replacement AddCounter | DoubleCounters``. Same path lifts Hardened
    Scales, Primal Vigor, Vorinclex Monstrous Raider, Pir Imaginative Rascal.
    """
    target_replacements: set[str] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "effect":
            continue
        ev = (p.get("event_class") or "").strip()
        target_replacements |= _REPLACEMENT_RESONANCE.get(ev, frozenset())
    if not target_replacements:
        return

    cmdr_set = set(commander_set)
    placeholders = ",".join("?" * len(target_replacements))
    cur = conn.execute(
        f"SELECT DISTINCT card_name, replacement_event FROM card_ports "
        f"WHERE port_type = 'replacement' "
        f"  AND replacement_event IN ({placeholders})",
        tuple(target_replacements),
    )
    for row in cur.fetchall():
        cand = row["card_name"]
        if cand in cmdr_set:
            continue
        _add_bucket(
            scores, matches, cand, "replacement_resonance",
            REPLACEMENT_RESONANCE_WEIGHT,
            {
                "bucket":            "replacement_resonance",
                "replacement_event": row["replacement_event"],
            },
        )


def _score_effect_resonance(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    cmdr_ports: list[PortRow],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """§7.6 — reward candidates whose effect class matches one of the
    commander's resonant effects.

    Resonant effects (Proliferate, Mill, Scry, Surveil, Untap, Investigate)
    are the small set of utility effects where having more sources in the
    same deck creates compounding value: every Proliferate counts every
    counter; every Mill source builds graveyard density; every Untap source
    enables more activations.

    Atraxa is the canonical case — she has ``effect Proliferate`` on her
    end-step trigger but no other matcher reaches her best deckmates
    (Inexorable Tide, Evolution Sage, Karn's Bastion, Contagion Engine,
    Doubling Season). The trigger feeders matcher is the wrong shape: it
    joins commander *triggers* to candidate effects, but Atraxa's
    Proliferate is itself an effect, not a trigger.
    """
    cmdr_resonant: set[str] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "effect":
            continue
        ev = (p.get("event_class") or "").strip()
        if ev in _RESONANT_EFFECTS:
            cmdr_resonant.add(ev)
    if not cmdr_resonant:
        return

    cmdr_set = set(commander_set)
    placeholders = ",".join("?" * len(cmdr_resonant))
    cur = conn.execute(
        f"SELECT DISTINCT card_name, event_class FROM card_ports "
        f"WHERE port_type = 'effect' AND event_class IN ({placeholders})",
        tuple(cmdr_resonant),
    )
    for row in cur.fetchall():
        cand = row["card_name"]
        if cand in cmdr_set:
            continue
        ev = row["event_class"]
        _add_bucket(
            scores, matches, cand, "effect_resonance",
            EFFECT_RESONANCE_WEIGHT,
            {
                "bucket":      "effect_resonance",
                "event_class": ev,
            },
        )


def _score_staples(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    scores: dict[str, BucketDict],
    matches: dict[str, MatchList],
) -> None:
    """Iterate the static :data:`STAPLES` lookup directly — at most ~30
    candidates touched, no full ``cards`` scan."""
    placeholders = ",".join("?" * len(commander_set))
    cmdr_colour_rows = conn.execute(
        f"SELECT color_identity FROM cards WHERE name IN ({placeholders})",
        tuple(commander_set),
    ).fetchall()
    cmdr_colours: set[str] = set()
    for r in cmdr_colour_rows:
        for tok in (r["color_identity"] or "").split(","):
            tok = tok.strip()
            if tok:
                cmdr_colours.add(tok)
    cmdr_name_set = set(commander_set)

    # Always include the colourless ('C') bucket: Sol Ring etc. are staples
    # for every commander regardless of identity.
    pip_buckets = cmdr_colours | {"C"}
    seen: set[str] = set()
    for pip in pip_buckets:
        for staple_name in STAPLES.get(pip, ()):
            if staple_name in cmdr_name_set or staple_name in seen:
                continue
            seen.add(staple_name)
            _add_bucket(
                scores, matches, staple_name, "staple",
                STAPLE_SCORE * STAPLE_WEIGHT,
                {"bucket": "staple", "score": STAPLE_SCORE},
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def score_all_candidates(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    *,
    use_graph_metrics: bool = False,
    use_chain_matches: bool = False,
    adjacency_cache: dict[str, set[str]] | None = None,
    scipy_pr_context: ScipyPRContext | None = None,
) -> ScoredCandidates:
    """Score every candidate card the matchers reach.

    Returns a frozen :class:`ScoredCandidates` with parallel ``buckets``
    and ``matches`` dicts. Cards never reached by any matcher are not in
    the result; callers that need a full ranking should iterate every
    legal card and default missing entries via :func:`empty_buckets`.

    The bucket dicts are owned by the caller — mutating them does not
    affect future invocations.
    """
    scores: dict[str, BucketDict] = {}
    matches: dict[str, MatchList] = {}

    _score_trigger_feeders(conn, commander_set, scores, matches)
    _score_etb_self(conn, commander_set, scores, matches)
    _score_catchall(conn, commander_set, scores, matches)
    _score_scaling(conn, commander_set, scores, matches)
    _score_deckhints(conn, commander_set, scores, matches)
    if use_chain_matches:
        _score_chains(conn, commander_set, scores, matches)
    _score_lords(conn, commander_set, scores, matches)
    _score_amplifiers(conn, commander_set, scores, matches)

    cmdr_ports = load_ports_for_set(conn, commander_set)
    _score_internal_synergy(conn, commander_set, cmdr_ports, scores, matches)

    if use_graph_metrics:
        _score_graph_metrics(
            conn, commander_set, scores, matches,
            adjacency_cache=adjacency_cache,
            scipy_pr_context=scipy_pr_context,
        )

    _score_strategic(conn, commander_set, cmdr_ports, scores, matches)
    _score_resource_density(conn, commander_set, scores, matches)
    _score_effect_resonance(conn, commander_set, cmdr_ports, scores, matches)
    _score_replacement_resonance(conn, commander_set, cmdr_ports, scores, matches)
    _score_staples(conn, commander_set, scores, matches)
    _score_replacements(conn, commander_set, scores, matches)
    _score_sacrifice_synergies(conn, commander_set, scores, matches)
    _score_counter_producer_payoff(conn, commander_set, scores, matches)
    _score_graveyard_value(conn, commander_set, scores, matches)
    _score_mana_restriction(conn, commander_set, scores, matches)
    # Phase D4 (find_flicker_loop_matches) is intentionally NOT wired
    # in here. The matcher correctly identifies flicker-source clusters
    # but the only golden-set commander it applies to (Brago) regresses
    # at any weight: EDHREC's high-synergy picks for Brago emphasise
    # ETB *targets* (Reflector Mage, Mulldrifter), not source clusters.
    # The matcher is kept as a reusable primitive in graph_engine.py
    # for phase E density-gated strategic rules to consume later.

    # Finalise totals.
    for buckets in scores.values():
        buckets["total"] = sum(buckets[b] for b in BUCKETS)

    return ScoredCandidates(buckets=scores, matches=matches)


def score_one(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    candidate: str,
) -> BucketDict:
    """Convenience wrapper: score one specific candidate.

    Performance note: this re-runs the full :func:`score_all_candidates`
    pipeline (O(all matchers)) and discards every other candidate. If you
    need scores for many cards, call ``score_all_candidates`` once and
    index ``result.buckets``.
    """
    result = score_all_candidates(conn, commander_set)
    return result.buckets.get(candidate, empty_buckets())
