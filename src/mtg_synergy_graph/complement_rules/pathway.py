"""Depth-2 self-bridging pathway scoring (``self_bridging_cascade`` rule family).

This module layers a length-<=2 port-graph walker on top of the existing
depth-1 complement pipeline. It detects candidates whose internal ports
form a loop that reinforces two distinct commander-port matches --
engine-grade cascade shapes (Korvold + Bloodghast, Muldrotha +
Gravecrawler, Teysa + aristocrats-chain token-makers).

The walker is a pure function on in-memory port tuples and has no
database dependency. The SQL-backed ``_find_self_bridging_cascade``
helper that wraps it ships in Unit 2 of plan
``docs/plans/2026-04-23-001-feat-self-bridging-cascade-pathway-plan.md``.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from functools import cache
from typing import TYPE_CHECKING, Any

from ..graph_engine import COST_FEEDS_TRIGGER, EVENT_MATCH_MAP
from .core import _CHANGEZONE_RUNTIME_HEADS, PortComplement, _changezone_type_set

if TYPE_CHECKING:
    from ..penalties import CandidateCache

PortRow = dict[str, Any]

#: Feature flag for the ``self_bridging_cascade`` rule family. Default
#: ``False`` — Units 1-5 of plan 2026-04-23-001 land the infrastructure
#: identity-clean (``bench.py audit --expect-identity`` must pass). Unit
#: 6 flips this in the working tree, runs the audit, and either commits
#: the flip (with ``ScoringConfigInputs`` plumbing + re-pinned fixture)
#: or leaves it ``False`` and documents the verdict in RULE_HISTORY.md.
#:
#: Tests toggle via ``unittest.mock.patch.object(pathway, "_ENABLE_PATHWAY_RULES", True)``.
#:
#: Family-forward naming: this gate also applies to future pathway rules
#: (cross-card bridges, depth-3 paths) rather than minting a new flag per
#: rule.
_ENABLE_PATHWAY_RULES: bool = True

#: Channel identifiers returned by :func:`_walk_self_paths`. Ordered
#: tightest-semantic-first; the walker returns the first matching
#: channel per pair.
_CHANNEL_EVENT_MATCH = "event_match"
_CHANNEL_COST_FEEDS = "cost_feeds"
_CHANNEL_VALID_FILTER = "valid_filter"


def _walk_self_paths(
    ports: Sequence[PortRow],
) -> tuple[PortRow, PortRow, str] | None:
    """Detect a length-<=2 internal edge between two of ``ports``.

    Returns ``(p1, p2, channel)`` on first match, ``None`` otherwise.
    ``channel`` is one of ``"event_match"`` or ``"cost_feeds"`` --
    the two canonical cascade substrates. The returned port dicts are
    the original objects (not copies).

    ``valid_filter`` was a candidate third channel during the
    brainstorm but the 2026-04-23 bench.py audit showed it fires too
    broadly on commanders with typed triggers (voltron / proliferate /
    monarch) -- equipment and auras that share a card-type family
    qualified as "internal edges" even when no actual cascade
    existed. Keeping only the two canonical substrates the seed JSON
    declares preserves the Gitrog / Yawgmoth / Xenagos wins while
    cutting the collateral. See ``docs/RULE_HISTORY.md`` for the
    audit comparison.

    Ports are expected to be a candidate's own port rows that have
    already been filtered against a commander's port shapes (the set
    ``M`` in FR1). The walker does not itself validate commander
    compatibility -- only the internal edge on the candidate.
    """
    if len(ports) < 2:
        return None
    for p1, p2 in itertools.combinations(ports, 2):
        if _event_match_edge(p1, p2):
            return (p1, p2, _CHANNEL_EVENT_MATCH)
        if _cost_feeds_edge(p1, p2):
            return (p1, p2, _CHANNEL_COST_FEEDS)
    return None


def _event_match_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff either orientation satisfies a canonical
    trigger->effect entry in ``EVENT_MATCH_MAP``.

    Unlike :func:`graph_engine.match_event`, this walker-specific
    variant skips the identity fallback (``t_event == e_event`` for
    events not in the map) -- that fallback encodes trigger<->trigger
    resonance which is a different channel from internal cause->effect
    cascade. For the depth-2 walk we only want the canonical
    trigger->effect edges the seed JSON declares.
    """
    return _canonical_trigger_effect(p1, p2) or _canonical_trigger_effect(p2, p1)


def _canonical_trigger_effect(trigger: PortRow, effect: PortRow) -> bool:
    """True iff ``trigger``'s event_class is an ``EVENT_MATCH_MAP``
    FROM key with a NAMED entry matching ``effect``'s event_class.

    Wildcard (``*``) entries in ``EVENT_MATCH_MAP`` are intentionally
    rejected for the internal-edge channel. The 2026-04-23 audit
    found that wildcard triggers (``Attacks``, ``SpellCast``,
    ``LandPlayed``) create spurious internal edges on equipment and
    other cards where the port extractor splits a single ability
    into distinct ``trigger`` and ``effect`` ports -- these aren't
    real cascades, just one ability in two rows. The standalone
    public :func:`graph_engine.match_event` still honors wildcards
    for its commander-vs-candidate matching; this walker-specific
    variant is tighter by design. See ``docs/RULE_HISTORY.md`` for
    the audit comparison.
    """
    t_event = (trigger.get("event_class") or "").strip()
    e_event = (effect.get("event_class") or "").strip()
    if not t_event or not e_event:
        return False
    targets = EVENT_MATCH_MAP.get(t_event)
    if targets is None:
        return False
    check = targets.get(e_event)
    if check is None:
        return False
    return check(trigger, effect)


def _cost_feeds_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff one port's ``event_class`` is a cost event that feeds
    the other's trigger event per ``COST_FEEDS_TRIGGER``.

    The map is keyed by lowercase cost events (``sacrifice``,
    ``discard``, ``pay_life``, ...) and stores frozensets of
    CamelCase trigger events. The two naming conventions don't
    overlap, so a strict dict lookup is sufficient -- no
    ``port_type`` check required.
    """
    e1 = (p1.get("event_class") or "").strip()
    e2 = (p2.get("event_class") or "").strip()
    if not e1 or not e2:
        return False
    if e2 in COST_FEEDS_TRIGGER.get(e1, frozenset()):
        return True
    return e1 in COST_FEEDS_TRIGGER.get(e2, frozenset())


def _valid_filter_edge(p1: PortRow, p2: PortRow) -> bool:
    """True iff both ports' ``valid_filter`` strings reference
    overlapping card-type families.

    Uses :func:`_changezone_type_set` for ChangeZone-shaped filters
    (Card.X, Permanent->expansion, runtime-bound handling) and a
    simple head-token extractor as fallback for non-ChangeZone
    filters (DamageDone / Sacrificed / static / etc.). Empty or
    purely runtime-bound filters do not form an edge -- an empty
    filter is not a wildcard in this channel.
    """
    vf1 = p1.get("valid_filter")
    vf2 = p2.get("valid_filter")
    # Short-circuit: if either filter is empty/None, no overlap is
    # possible. Skips two ``_type_token_set`` calls on the majority
    # of non-valid_filter ports (cost / trigger with no filter).
    if not vf1 or not vf2:
        return False
    set1 = _type_token_set(vf1)
    if set1 is None:
        return False
    set2 = _type_token_set(vf2)
    if set2 is None:
        return False
    return bool(set1 & set2)


def _port_pair_matches(a: PortRow, b: PortRow) -> bool:
    """True iff ``(a, b)`` form a match via any of three channels:
    event_match, cost_feeds, or valid_filter.

    Used in :func:`_find_self_bridging_cascade` to build the candidate's
    ``M`` set -- the ports that match any commander port.

    **Intentional asymmetry with the walker.** :func:`_walk_self_paths`
    accepts only two channels (``event_match`` and ``cost_feeds``),
    having dropped ``valid_filter`` after the 2026-04-23 audit. This
    helper keeps all three so that the M-set criterion is liberal
    (commander-candidate matching admits valid_filter overlaps) while
    the internal-edge criterion stays tight. A candidate port that
    enters M only via valid_filter still contributes to ``|M| >= 2``
    satisfaction, but the walker will not accept it as an edge
    endpoint unless it also participates in an event_match or
    cost_feeds pair. Changing this symmetry requires a fresh audit --
    see ``docs/RULE_HISTORY.md`` 2026-04-23 for the prior variants.
    """
    return _event_match_edge(a, b) or _cost_feeds_edge(a, b) or _valid_filter_edge(a, b)


def _cand_port_matches_any_cmdr(
    cand_port: PortRow,
    cmdr_ports: Sequence[PortRow],
) -> PortRow | None:
    """Return the first commander port that matches ``cand_port``, or
    ``None`` if no commander port matches. Used to compute ``M`` and
    to derive the deterministic ``cmdr_event`` label for the emitted
    ``PortComplement``.

    Ordering is load-bearing: the label encodes the commander event
    of the first match in ``cmdr_ports`` order, so all call sites must
    preserve the same iteration order to keep IDF buckets stable.
    """
    for cp in cmdr_ports:
        if _port_pair_matches(cp, cand_port):
            return cp
    return None


def _relevant_event_set(cmdr_ports: Sequence[PortRow]) -> set[str]:
    """Build the set of ``event_class`` strings that Stage-1 SQL uses
    to prefilter candidates. Union of commander port events plus
    their ``EVENT_MATCH_MAP`` / ``COST_FEEDS_TRIGGER`` neighbours.

    This is a coarse prefilter -- Stage-3 Python matching is
    authoritative. A broader set here costs extra Stage-2 rows; a
    narrower set risks missing candidates.
    """
    events: set[str] = set()
    for p in cmdr_ports:
        ev = (p.get("event_class") or "").strip()
        if ev:
            events.add(ev)
    if not events:
        return set()
    expanded = set(events)
    for ev in events:
        # Forward: trigger -> effect via EVENT_MATCH_MAP
        targets = EVENT_MATCH_MAP.get(ev)
        if targets is not None:
            for target_ev in targets:
                if target_ev != "*":
                    expanded.add(target_ev)
        # Reverse: effect -> trigger via EVENT_MATCH_MAP
        for trig_ev, trig_targets in EVENT_MATCH_MAP.items():
            if ev in trig_targets:
                expanded.add(trig_ev)
        # Cost -> trigger
        if ev in COST_FEEDS_TRIGGER:
            expanded.update(COST_FEEDS_TRIGGER[ev])
        # Trigger -> cost
        for cost_ev, trigs in COST_FEEDS_TRIGGER.items():
            if ev in trigs:
                expanded.add(cost_ev)
    return expanded


def _find_self_bridging_cascade(
    conn: sqlite3.Connection,
    cmdr_ports: Sequence[PortRow],
    cmdr_set: set[str],
    stax_excluded: set[str] | None = None,
    candidate_cache: CandidateCache | None = None,
) -> list[PortComplement]:
    """Emit one ``PortComplement`` per candidate whose internal
    port-graph forms a length-<=2 loop that reinforces two distinct
    commander-port matches (FR1 + FR2 of the pathway plan).

    Three-stage pipeline matching ``_find_panharmonicon_complements``'s
    template:

    1. **Stage-1 SQL** -- narrow to candidates with >=2 distinct
       commander-relevant event classes (broad prefilter; precision
       comes from Stage-3).
    2. **Stage-2 SQL** -- single bulk fetch of every port on every
       Stage-1 candidate.
    3. **Stage-3 Python** -- per candidate, compute ``M`` (ports
       matching any commander port), call :func:`_walk_self_paths`,
       emit one complement on first path found.

    The emitted ``PortComplement`` carries ``rule_id =
    "self_bridging_cascade"``, ``filter_group = "depth_2"``,
    ``cand_event = "self_bridging"``, and a deterministic
    ``cmdr_event`` built from the two commander events that matched
    the bridging ports (lexicographically sorted, ``"+"``-joined).
    """
    if len(cmdr_ports) < 2:
        return []

    relevant = _relevant_event_set(cmdr_ports)
    if len(relevant) < 2:
        return []

    # Stage 1: candidates with >=2 distinct commander-relevant events.
    # SQLite has no tuple-IN, so we use a concat-distinct trick. The
    # WHERE clause uses parameterised IN; no user input is interpolated
    # (S608-safe, matching the rest of complement_rules).
    relevant_sorted = sorted(relevant)
    ev_placeholders = ",".join("?" * len(relevant_sorted))
    params: list[str] = list(relevant_sorted)

    # Exclude both commander names and stax-blocked candidates. stax_excluded
    # is built once per commander in find_all_complements and threaded through
    # so the self_bridging_cascade rule doesn't surface stax pieces that
    # actively hurt the commander's strategy (e.g., Disable Triggers against
    # a ChangesZone commander).
    excluded: set[str] = set(cmdr_set)
    if stax_excluded:
        excluded |= stax_excluded
    cmdr_exclude = ""
    if excluded:
        cmdr_placeholders = ",".join("?" * len(excluded))
        cmdr_exclude = f"AND card_name NOT IN ({cmdr_placeholders}) "
        params.extend(sorted(excluded))

    sql_stage1 = (
        "SELECT card_name FROM card_ports "
        f"WHERE event_class IN ({ev_placeholders}) "
        f"{cmdr_exclude}"
        "GROUP BY card_name "
        # SQLite lacks tuple-DISTINCT, so concat port_type + event_class into a
        # single string to count distinct shapes. A card with two rows sharing
        # event_class but different port_types (cost.sacrifice + trigger.Sacrificed)
        # must pass this gate to reach the Stage-3 walker.
        "HAVING COUNT(DISTINCT port_type || '|' || event_class) >= 2"
    )
    candidate_names = [row["card_name"] for row in conn.execute(sql_stage1, params).fetchall()]
    if not candidate_names:
        return []

    # Stage 2: build ports_by_card for the Stage-1 candidates. When a
    # CandidateCache is available, do a dict-slice over the pre-loaded
    # ports_by_card (commander-independent, cached once per session).
    # Otherwise fall back to a bulk SQL fetch -- correct but slow on
    # broad commanders (profiling showed +2000% overhead for Korvold
    # without the cache).
    ports_by_card: dict[str, list[PortRow]]
    if candidate_cache is not None:
        cached = candidate_cache.ports_by_card
        ports_by_card = {name: cached[name] for name in candidate_names if name in cached}
    else:
        cand_placeholders = ",".join("?" * len(candidate_names))
        port_rows = conn.execute(
            "SELECT card_name, port_type, event_class, valid_filter, "
            "zone_origin, zone_destination, counter_type "
            f"FROM card_ports WHERE card_name IN ({cand_placeholders})",
            candidate_names,
        ).fetchall()
        fallback: dict[str, list[PortRow]] = defaultdict(list)
        for r in port_rows:
            fallback[r["card_name"]].append(dict(r))
        ports_by_card = fallback

    # Stage 3: walker on M ports per candidate. ``ports_by_card`` is a
    # defaultdict, so keys are already unique -- no outer dedup needed.
    # Thread commander matches alongside each M port to avoid a second
    # O(|cmdr_ports|) scan when building the cmdr_event label.
    #
    # Performance: memoise cand_port -> matched commander port lookups
    # keyed on the full set of fields that feed the three channel
    # predicates (port_type, event_class, valid_filter, zone_origin,
    # zone_destination, counter_type). A narrower key (e.g., only
    # port_type + event_class) would conflate ports with different
    # zone_destinations that differ under the ChangesZone zone_compatible
    # predicate, silently producing scoring drift. For broad commanders
    # (Korvold: 1336 firings) the memo cuts wall time because many ports
    # on different cards share identical full shapes.
    results: list[PortComplement] = []
    match_memo: dict[tuple[str, str, str, str, str, str], PortRow | None] = {}
    for card, cand_ports in ports_by_card.items():
        m_entries: list[tuple[PortRow, PortRow]] = []
        for cp in cand_ports:
            key = (
                cp.get("port_type") or "",
                cp.get("event_class") or "",
                cp.get("valid_filter") or "",
                cp.get("zone_origin") or "",
                cp.get("zone_destination") or "",
                cp.get("counter_type") or "",
            )
            if key in match_memo:
                cached_match = match_memo[key]
            else:
                cached_match = _cand_port_matches_any_cmdr(cp, cmdr_ports)
                match_memo[key] = cached_match
            if cached_match is not None:
                m_entries.append((cp, cached_match))
        if len(m_entries) < 2:
            continue
        m_ports = [cp for cp, _ in m_entries]
        found = _walk_self_paths(m_ports)
        if found is None:
            continue
        p1, p2, channel = found
        cmdr_by_cand_id = {id(cp): cmdr for cp, cmdr in m_entries}
        cp1 = cmdr_by_cand_id.get(id(p1))
        cp2 = cmdr_by_cand_id.get(id(p2))
        ev1 = ((cp1 or {}).get("event_class") or "").strip() or "unknown"
        ev2 = ((cp2 or {}).get("event_class") or "").strip() or "unknown"
        cmdr_event = "+".join(sorted([ev1, ev2]))
        path_info = _format_path_info(p1, p2, channel)
        results.append(
            PortComplement(
                rule_id="self_bridging_cascade",
                direction="synergy",
                candidate=card,
                cmdr_event=cmdr_event,
                cand_event="self_bridging",
                filter_group="depth_2",
                path_info=path_info,
            )
        )
    return results


def _format_path_info(p1: PortRow, p2: PortRow, channel: str) -> str:
    """Human-readable render of a bridging pair for ``--explain``.

    Produces ``"<subkind_a> <-> <subkind_b> (channel: <channel>)"`` where
    each subkind is ``"<port_type>.<event_class>"`` -- the same
    canonical shape the ``port_nodes`` SQL view exposes as its
    ``subkind`` column, just computed in Python from the raw row.
    """
    subkind_a = _subkind_label(p1)
    subkind_b = _subkind_label(p2)
    return f"{subkind_a} <-> {subkind_b} (channel: {channel})"


def _subkind_label(port: PortRow) -> str:
    """Render a port as ``"<port_type>.<event_class>"``. Matches the
    ``subkind`` column in the ``port_nodes`` SQL view and keeps the
    human-readable path text stable regardless of future
    ``node_kind`` vocabulary churn."""
    pt = (port.get("port_type") or "").strip() or "?"
    ev = (port.get("event_class") or "").strip() or "?"
    return f"{pt}.{ev}"


@cache
def _type_token_set(valid_filter: str | None) -> frozenset[str] | None:
    """Extract the card-type family from any ``valid_filter`` shape.

    Returns ``None`` for empty, runtime-bound, or Card.Self-only
    filters (same contract as :func:`_changezone_type_set`). Falls
    back to a head-token extractor for non-ChangeZone filters --
    ``Creature.YouCtrl+powerGE4`` -> ``{"Creature"}``,
    ``Artifact.YouCtrl`` -> ``{"Artifact"}``.

    Cached unboundedly with ``functools.cache``. The valid_filter
    string space in production is small (~1-2k distinct values per
    full card_ports scan) but pre-cache we called this ~160k times
    per Korvold page against the same filters. Pure function of a
    hashable input, so the cache is trivially correct and never
    needs eviction within a process lifetime.
    """
    if not valid_filter:
        return None
    cz = _changezone_type_set(valid_filter)
    if cz is not None:
        return cz
    types: set[str] = set()
    for alt in valid_filter.split(","):
        alt = alt.strip()
        if not alt:
            continue
        head = alt.split(".", 1)[0].split("+", 1)[0].strip()
        if head and head[0].isupper() and head not in _CHANGEZONE_RUNTIME_HEADS and not head.startswith("DelayTrigger"):
            types.add(head)
    return frozenset(types) if types else None
