"""Graph engine: port matching across the imported card_ports table.

Implements SPEC §6.1–§6.5/§6.7 incrementally. The Phase 2 surface area is:

* :func:`match_event` — trigger ↔ effect compatibility (§6.1.2)
* :func:`filter_matches` — Python helper used in unit tests and small lookups
  (§6.1.3 — never call this from inside a SQL join; the indexed
  ``port_attributes`` join is the production path)
* :func:`find_trigger_feeders` — cards whose effects (or costs) feed a
  commander's triggers (§6.1.1 + §6.3 cost feed)
* :func:`find_lord_matches` — static lord-style buffs (§6.2.1)
* :func:`find_amplifier_matches` — Panharmonicon-class amplifiers (§6.2.3)
* :func:`find_replacement_conflicts` — anti-synergy ``Prevent`` replacements (§6.5)
* :func:`load_ports_for` — small convenience: every port row for a card

The matchers are deliberately Python-side; SPEC §6 leaves the door open to
move them into SQL views once the schema stabilises, but at Phase 2 the row
count is small (~30 ports per card × ~30k cards) and a Python loop over
indexed lookups keeps the code far easier to audit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from typing import Any

from .attributes import explode_filter

# ---------------------------------------------------------------------------
# §6.1.2 event matching map
# ---------------------------------------------------------------------------

PortRow = dict[str, Any]
EventCheck = Callable[[PortRow, PortRow], bool]


# ---------------------------------------------------------------------------
# Event-map loaders — imported from port_graph.event_maps. The loaders
# are NOT called at module import anymore; both maps are exposed via
# PEP 562 ``__getattr__`` so the seed file is only read on first access
# of ``EVENT_MATCH_MAP`` / ``COST_FEEDS_TRIGGER``. This lets consumers
# (lint tooling, documentation generators, test harnesses) import the
# module even when ``data/event_match_seed.json`` is absent.
# ---------------------------------------------------------------------------


#: Triggers whose semantic is "the candidate card itself, when cast/played/
#: attacking, is the event" — these are NOT matched against arbitrary effect
#: ports of unrelated cards. They route through a separate code path that
#: checks the candidate card's identity (is-a-spell / is-a-land / is-a-
#: creature). Without this exclusion, ``Attacks*`` would match every effect
#: in the database and Wrath of God would look like a Korvold feeder.
CATCH_ALL_TRIGGERS: frozenset[str] = frozenset(
    {"SpellCast", "LandPlayed", "Attacks", "AttackerBlocked", "BecomesTarget"}
)


# Lazy-load caches for the two JSON-sourced maps. ``None`` means "not
# yet populated"; any non-None value is the cached map shared across
# callers for the lifetime of the process. Re-reads require an explicit
# :func:`_reset_event_cache_for_tests` call.
_EVENT_MATCH_MAP_CACHE: dict[str, dict[str, EventCheck]] | None = None
_COST_FEEDS_TRIGGER_CACHE: dict[str, frozenset[str]] | None = None


def _get_event_match_map() -> dict[str, dict[str, EventCheck]]:
    """Return the event-match map, loading the JSON seed on first call.

    Populated at first access from ``data/event_match_seed.json`` via
    :func:`port_graph.event_maps.load_event_match_map_from_json`. Edit
    the JSON to add a new equivalence — the ``event_match_map`` SQLite
    table is re-seeded from the same JSON on next DB import, so both
    representations stay in sync.
    """
    global _EVENT_MATCH_MAP_CACHE
    if _EVENT_MATCH_MAP_CACHE is None:
        # Re-read the loader each call so tests that monkeypatch
        # ``port_graph.event_maps.load_event_match_map_from_json`` observe
        # their patched version on the first cache fill.
        from .port_graph import event_maps as _em

        _EVENT_MATCH_MAP_CACHE = _em.load_event_match_map_from_json()
    return _EVENT_MATCH_MAP_CACHE


def _get_cost_feeds_trigger() -> dict[str, frozenset[str]]:
    """Return the cost→triggers map, loading the JSON seed on first call.

    §6.3 cost↔trigger feed. Same JSON-sourced loader as
    :func:`_get_event_match_map`.
    """
    global _COST_FEEDS_TRIGGER_CACHE
    if _COST_FEEDS_TRIGGER_CACHE is None:
        from .port_graph import event_maps as _em

        _COST_FEEDS_TRIGGER_CACHE = _em.load_cost_feeds_trigger_from_json()
    return _COST_FEEDS_TRIGGER_CACHE


def _reset_event_cache_for_tests() -> None:
    """Evict the cached event maps so the next attribute access re-loads.

    Intended for tests that monkeypatch the underlying JSON loaders or
    swap out the seed file. Not part of the public API.
    """
    global _EVENT_MATCH_MAP_CACHE, _COST_FEEDS_TRIGGER_CACHE
    _EVENT_MATCH_MAP_CACHE = None
    _COST_FEEDS_TRIGGER_CACHE = None


def __getattr__(name: str) -> Any:
    """PEP 562 module-level ``__getattr__`` — exposes
    :data:`EVENT_MATCH_MAP` and :data:`COST_FEEDS_TRIGGER` lazily.

    Keeping the public names as attribute lookups (rather than
    accessor functions) preserves every existing ``from graph_engine
    import EVENT_MATCH_MAP`` call site — but the seed file is only
    touched when someone actually reads the map.
    """
    if name == "EVENT_MATCH_MAP":
        return _get_event_match_map()
    if name == "COST_FEEDS_TRIGGER":
        return _get_cost_feeds_trigger()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def match_event(trigger: PortRow, effect: PortRow) -> bool:
    """Return True if a trigger port should fire on an effect port.

    Implements §6.1.2 with the catch-all ``"*"`` shortcut for ``SpellCast``,
    ``LandPlayed``, ``Attacks``, etc. — these match any effect class because
    casting/playing/attacking is itself the event.
    """
    t_event = (trigger.get("event_class") or "").strip()
    e_event = (effect.get("event_class") or "").strip()
    if not t_event or not e_event:
        return False

    targets = _get_event_match_map().get(t_event)
    if targets is None:
        return t_event == e_event

    if "*" in targets:
        return targets["*"](trigger, effect)

    check = targets.get(e_event)
    if check is None:
        return False
    return check(trigger, effect)


# ---------------------------------------------------------------------------
# §6.1.3 ValidFilter compatibility (Python fallback)
# ---------------------------------------------------------------------------

#: Attribute kinds that are runtime-only and must be skipped at build time.
#: ``controller`` is "you control" / "opponent controls" — irrelevant for
#: deck-build matching. ``cmc_cmp`` is the comparison-style filter (cmcLE3,
#: cmcGE5, ...) which the static-attribute matcher can't evaluate; treating
#: it as runtime keeps over-restrictive triggers from dropping legitimate
#: candidates. The trigger feeders matcher's downstream filtering will
#: handle cmc gates separately if needed.
_RUNTIME_ATTR_KINDS = frozenset({"controller", "cmc_cmp"})


def filter_matches(valid_filter: str, candidate_attrs: Iterable[tuple[str, str]]) -> bool:
    """Return True iff a candidate's attributes satisfy the filter.

    ``candidate_attrs`` is an iterable of ``(attr_kind, attr_value)`` pairs
    drawn from the candidate card's static attributes (types, subtypes,
    colours, keywords). Negated tokens in the filter must NOT appear; all
    non-negated, non-runtime tokens MUST appear.

    This is the Python fallback used by tests and small lookups. The
    production graph engine uses indexed SQL joins against
    ``port_attributes`` per §6.1.3 — never call this function inside a SQL
    join.
    """
    if not valid_filter:
        return True
    cand_set = {(k, v) for k, v in candidate_attrs}
    required: list[tuple[str, str]] = []
    forbidden: list[tuple[str, str]] = []
    for attr in explode_filter(valid_filter):
        if attr["attr_kind"] in _RUNTIME_ATTR_KINDS:
            continue
        pair = (attr["attr_kind"], attr["attr_value"])
        (forbidden if attr["is_negated"] else required).append(pair)
    if any(p in cand_set for p in forbidden):
        return False
    return all(p in cand_set for p in required)


# ---------------------------------------------------------------------------
# Helpers — pull port rows from sqlite as plain dicts
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> PortRow:
    # ~5x faster than ``{k: row[k] for k in row.keys()}`` or ``dict(row)``:
    # tuple(row) copies values in one C-level call and ``zip`` builds the
    # dict in C without the per-key ``__getitem__`` overhead sqlite3.Row
    # incurs. Measured on a 10k-row, 31-column card_ports fetch:
    # via dict-comp: 3.11s / via dict(row): 3.02s / via zip: 0.61s.
    return dict(zip(row.keys(), tuple(row), strict=False))


def _rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[PortRow]:
    """Bulk-convert a cursor result set to a list of plain dicts.

    Only inspects ``row.keys()`` once per cursor — all subsequent rows
    reuse the key tuple, shaving another ~15% off the already-5x-faster
    :func:`_row_to_dict` helper when converting the large port fetches
    that dominate :func:`find_trigger_feeders`.
    """
    if not rows:
        return []
    keys = rows[0].keys()
    return [dict(zip(keys, tuple(r), strict=False)) for r in rows]


def load_ports_for(conn: sqlite3.Connection, card_name: str) -> list[PortRow]:
    """Return every port row for a single card as a list of dicts."""
    cur = conn.execute(
        "SELECT * FROM card_ports WHERE card_name = ?",
        (card_name,),
    )
    return _rows_to_dicts(cur.fetchall())


_ports_cache: dict[tuple[int, tuple[str, ...]], list[PortRow]] = {}


def load_ports_for_set(
    conn: sqlite3.Connection,
    card_names: Sequence[str],
) -> list[PortRow]:
    """Union of port rows across multiple cards (partner-pair friendly).

    Results are cached per ``(id(conn), names)`` so the 19+ graph_engine
    functions that call this for the same commander set share one SQL fetch.
    Call :func:`clear_ports_cache` between commander runs if needed.

    .. warning::

        The cache key uses ``id(conn)`` (CPython object address), which
        can be reused after a connection is closed and garbage-collected.
        Always call :func:`clear_ports_cache` before replacing a
        connection object to avoid stale results.
    """
    if not card_names:
        return []
    key = (id(conn), tuple(card_names))
    cached = _ports_cache.get(key)
    if cached is not None:
        return cached
    placeholders = ",".join("?" * len(card_names))
    cur = conn.execute(
        f"SELECT * FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(card_names),
    )
    result = _rows_to_dicts(cur.fetchall())
    _ports_cache[key] = result
    return result


def clear_ports_cache() -> None:
    """Clear the in-memory port cache between commander runs."""
    _ports_cache.clear()


def _ports_by_type(ports: Iterable[PortRow], port_type: str) -> list[PortRow]:
    return [p for p in ports if p.get("port_type") == port_type]


# ---------------------------------------------------------------------------
# §6.1.1 + §6.3 — direct + cost feed match
# ---------------------------------------------------------------------------


def _trigger_only_matches_self(valid_filter: str | None) -> bool:
    """Return ``True`` when *every* comma-separated alternative in a trigger
    valid_filter starts with ``Card.Self`` — i.e. the trigger only fires on
    the trigger source itself.

    Self-only triggers cannot be "fed" by any other card (the commander has
    to enter / leave / be cast itself, no other card causes the event to
    happen), so they should not generate cross-links in the trigger feeder
    matcher.

    Regression: previously Urza's ``trigger ChangesZone | Card.Self`` (his
    own ETB) cross-linked to every Token / ChangeZone / Animate / CopyPermanent
    effect in the format — 7833 spurious matches against Urza alone, with
    Druidic Satchel / Soul Separator / Fishing Gear flooding his top-50.
    """
    if not valid_filter:
        return False
    for alt in valid_filter.split(","):
        head = alt.strip()
        if not head.startswith("Card.Self"):
            return False
    return True


# Effect events that *can* produce / move cards onto the battlefield. We
# only need to do produced-type checking for these; anything else (Mill,
# Damage, Draw, ...) shouldn't satisfy an ETB-trigger filter at all.
_PRODUCING_EFFECT_EVENTS: frozenset[str] = frozenset(
    {
        "Token",
        "ChangeZone",
        "ChangeZoneAll",
        "Animate",
        "CopyPermanent",
    }
)

# Pulls the ChangeType / Types value out of a parsed Forge dict raw_line.
_CHANGE_TYPE_RE = re.compile(r"['\"]ChangeType['\"]:\s*['\"]([A-Za-z][A-Za-z0-9.+;]*)")
_TOKEN_SCRIPT_INLINE_RE = re.compile(r"['\"]TokenScript['\"]:\s*['\"]([A-Za-z0-9_,]+)")
_DESTINATION_RE = re.compile(r"['\"]Destination['\"]:\s*['\"]([A-Za-z]+)")
_ORIGIN_RE = re.compile(r"['\"]Origin['\"]:\s*['\"]([A-Za-z]+)")


def _token_script_subtypes(script: str) -> set[str]:
    """Extract subtype-shaped tokens from a Forge ``TokenScript``.

    Token scripts encode the produced creature as
    ``<color>_<P>_<T>_<word>_<word>...`` where the words after the P/T
    pair are subtypes (Faerie, Insect, Demon, Zombie) and keywords
    (Flying, Trample, Deathtouch). We capitalize each and return them
    as candidate subtype tokens. The downstream filter check accepts
    them as either subtype or keyword via the same code path.
    """
    if not script:
        return set()
    s = script.lower()
    m = re.search(r"_(?:\d+|x)_(?:\d+|x)_", s)
    if m is None:
        return set()
    after = s[m.end() :]
    out: set[str] = set()
    for tok in after.split("_"):
        tok = tok.strip()
        if not tok or tok.isdigit() or tok == "x":
            continue
        out.add(tok.capitalize())
    return out


def _effect_produced_attrs(effect_port: PortRow) -> set[tuple[str, str]] | None:
    """Best-effort: what static attributes (type / subtype) does this
    effect produce?

    Returns a set of ``(attr_kind, attr_value)`` pairs compatible with
    :func:`filter_matches`, ``None`` for ambiguous effects (callers
    should drop these), or an empty set for effects that can't produce a
    card at all (Mill / Draw / LoseLife etc — also drop).

    Used by :func:`find_trigger_feeders` to verify that the candidate's
    effect actually produces something matching the commander trigger's
    valid_filter, instead of cross-linking every Token effect to every
    ETB trigger regardless of subtype.
    """
    ev = (effect_port.get("event_class") or "").strip()
    if ev not in _PRODUCING_EFFECT_EVENTS:
        return set()

    raw = str(effect_port.get("raw_line") or "")

    if ev == "Token":
        match = _TOKEN_SCRIPT_INLINE_RE.search(raw)
        if match is None:
            return None
        produced: set[tuple[str, str]] = set()
        for script in match.group(1).split(","):
            kind = _classify_token_script(script)
            if kind:
                produced.add(("type", kind))
            for sub in _token_script_subtypes(script):
                produced.add(("subtype", sub))
        return produced if produced else None

    if ev in ("ChangeZone", "ChangeZoneAll"):
        # Only counts as "feeding" an ETB-style ChangesZone trigger if
        # the destination is the battlefield. Vastwood Seer's
        # Library→Hand ramp doesn't fire ETB triggers, even though it's
        # a ChangeZone effect. Same for Battlefield→Exile / →Graveyard /
        # →Library returns.
        dest_match = _DESTINATION_RE.search(raw)
        if dest_match and dest_match.group(1) != "Battlefield":
            return set()
        match = _CHANGE_TYPE_RE.search(raw)
        if match is None:
            return None
        produced = set()
        for token in match.group(1).split(";"):
            for attr in explode_filter(token):
                if attr["attr_kind"] in _RUNTIME_ATTR_KINDS:
                    continue
                if attr["attr_kind"] == "subtype" and attr["attr_value"] in _RUNTIME_SUBTYPE_VALUES:
                    continue
                if attr["is_negated"]:
                    continue
                produced.add((attr["attr_kind"], attr["attr_value"]))
        return produced if produced else None

    if ev == "Animate":
        # Animate turns a non-creature *that's already on the battlefield*
        # into a creature. The target doesn't enter the battlefield as
        # part of the effect, so ETB triggers don't fire. Treat as
        # non-producing.
        return set()

    if ev == "CopyPermanent":
        # Copies the type of an existing permanent — could be anything.
        # Conservative: drop ambiguous matches rather than over-credit.
        return None

    return None


def _trigger_filter_constraint(valid_filter: str | None) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Decompose a trigger filter into ``(required, forbidden)`` attribute
    sets, ignoring runtime tokens and ``Card.Self`` alternatives.
    """
    required: set[tuple[str, str]] = set()
    forbidden: set[tuple[str, str]] = set()
    if not valid_filter:
        return required, forbidden
    for alt in valid_filter.split(","):
        alt = alt.strip()
        if not alt or alt.startswith("Card.Self"):
            continue
        for attr in explode_filter(alt):
            if attr["attr_kind"] in _RUNTIME_ATTR_KINDS:
                continue
            if attr["attr_kind"] == "subtype" and attr["attr_value"] in _RUNTIME_SUBTYPE_VALUES:
                continue
            pair = (attr["attr_kind"], attr["attr_value"])
            (forbidden if attr["is_negated"] else required).add(pair)
    return required, forbidden


def _classify_token_script(script: str) -> str | None:
    """Local re-implementation of the token script classifier.

    Mirrors :func:`scoring._token_script_kind` but lives here so the
    graph engine doesn't take a runtime dependency on the scoring layer.
    """
    if not script:
        return None
    s = script.lower()
    artifact_keywords = (
        "_a_",
        "treasure",
        "food",
        "clue",
        "blood",
        "gold",
        "map",
        "powerstone",
        "junk",
        "incubator",
        "shard",
    )
    for kw in artifact_keywords:
        if kw in s:
            return "Artifact"
    if re.search(r"_(?:\d+|x)_(?:\d+|x)_", s):
        return "Creature"
    return "Creature"


def find_trigger_feeders(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Return rows describing every candidate effect/cost that feeds the
    commander's triggers.

    Each row is::

        {
            "candidate":      <card_name>,
            "trigger_card":   <commander card>,
            "trigger_event":  <event_class>,
            "match_kind":     "effect" | "cost",
            "effect_event":   <event_class>,
            "branch_kind":    <child branch_kind>,
            "is_conditional": bool,
        }
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    triggers = _ports_by_type(cmdr_ports, "trigger")
    if not triggers:
        return []

    cmdr_set = set(commander_set)

    # --- Pre-filter triggers to the usable subset ---
    usable_triggers: list[tuple[PortRow, set[tuple[str, str]], set[tuple[str, str]]]] = []
    needed_effect_classes: set[str] = set()
    needed_cost_classes: set[str] = set()

    # Reverse COST_FEEDS_TRIGGER: trigger_event → {cost_class, ...}
    _event_match_map = _get_event_match_map()
    _cost_feeds_trigger = _get_cost_feeds_trigger()
    _trigger_fed_by_cost: dict[str, set[str]] = {}
    for cost_ev, trig_evs in _cost_feeds_trigger.items():
        for te in trig_evs:
            _trigger_fed_by_cost.setdefault(te, set()).add(cost_ev)

    for trig in triggers:
        t_event = (trig.get("event_class") or "").strip()
        if t_event in CATCH_ALL_TRIGGERS:
            continue
        if _trigger_only_matches_self(trig.get("valid_filter")):
            continue
        trig_vf = trig.get("valid_filter") or ""
        if "OppCtrl" in trig_vf or "OppOwn" in trig_vf:
            continue

        required, forbidden = _trigger_filter_constraint(trig.get("valid_filter"))
        usable_triggers.append((trig, required, forbidden))

        # Determine which effect event_classes this trigger can match.
        targets = _event_match_map.get(t_event)
        if targets is None:
            # Identity match: trigger event == effect event
            needed_effect_classes.add(t_event)
        elif "*" not in targets:
            needed_effect_classes.update(targets.keys())
        # else: wildcard match — can't narrow, but these are CATCH_ALL
        # triggers which we already skipped above.

        # Cost classes that feed this trigger
        cost_classes = _trigger_fed_by_cost.get(t_event)
        if cost_classes:
            needed_cost_classes.update(cost_classes)

    if not usable_triggers:
        return []

    # --- Fetch only relevant ports via SQL IN-clause ---
    all_needed = needed_effect_classes | needed_cost_classes
    if not all_needed:
        return []

    placeholders = ",".join("?" * len(all_needed))
    cur = conn.execute(
        f"SELECT card_name, port_type, event_class, branch_kind, "
        f"is_conditional, raw_line "
        f"FROM card_ports WHERE port_type IN ('effect','cost') "
        f"AND event_class IN ({placeholders})",
        tuple(all_needed),
    )
    candidate_ports = _rows_to_dicts(cur.fetchall())

    # --- Build index: event_class → [ports] for O(1) lookup ---
    effects_by_event: dict[str, list[PortRow]] = defaultdict(list)
    costs_by_event: dict[str, list[PortRow]] = defaultdict(list)
    for cand in candidate_ports:
        if cand["card_name"] in cmdr_set:
            continue
        ec = (cand.get("event_class") or "").strip()
        if cand.get("port_type") == "effect":
            effects_by_event[ec].append(cand)
        else:
            costs_by_event[ec].append(cand)

    # --- Match triggers against indexed candidate ports ---
    results: list[dict[str, Any]] = []
    for trig, required, forbidden in usable_triggers:
        t_event = (trig.get("event_class") or "").strip()

        # Effect matching: look up which effect event_classes can feed this trigger
        targets = _event_match_map.get(t_event)
        # Identity match when no EVENT_MATCH_MAP entry exists
        relevant_effect_classes = [t_event] if targets is None else list(targets.keys())

        for eff_class in relevant_effect_classes:
            for cand in effects_by_event.get(eff_class, ()):
                if not match_event(trig, cand):
                    continue
                if required or forbidden:
                    produced = _effect_produced_attrs(cand)
                    if produced is None or not produced:
                        continue
                    if not required.issubset(produced):
                        continue
                    if forbidden & produced:
                        continue
                results.append(
                    {
                        "candidate": cand["card_name"],
                        "trigger_card": trig["card_name"],
                        "trigger_event": t_event,
                        "match_kind": "effect",
                        "effect_event": cand.get("event_class"),
                        "branch_kind": cand.get("branch_kind") or "root",
                        "is_conditional": bool(cand.get("is_conditional")),
                    }
                )

        # Cost matching: look up cost classes that feed this trigger
        feed_cost_classes = _trigger_fed_by_cost.get(t_event)
        if feed_cost_classes:
            for cost_class in feed_cost_classes:
                for cand in costs_by_event.get(cost_class, ()):
                    results.append(
                        {
                            "candidate": cand["card_name"],
                            "trigger_card": trig["card_name"],
                            "trigger_event": t_event,
                            "match_kind": "cost",
                            "effect_event": cand.get("event_class"),
                            "branch_kind": cand.get("branch_kind") or "root",
                            "is_conditional": bool(cand.get("is_conditional")),
                        }
                    )

    return results


# ---------------------------------------------------------------------------
# §6.2.1 — lord detection
# ---------------------------------------------------------------------------


def find_lord_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Find static Continuous lords whose Affected$ scope matches the
    commander's subtypes.

    A "lord" is a card with::

        S:Mode$ Continuous | Affected$ Creature.<Tribe>.YouCtrl | AddPower$ +1 | ...
    """
    rows = conn.execute(
        "SELECT name, subtypes FROM cards WHERE name IN ({})".format(",".join("?" * len(commander_set))),
        tuple(commander_set),
    ).fetchall()
    literal_subtypes: set[str] = set()
    for r in rows:
        if r["subtypes"]:
            literal_subtypes.update(r["subtypes"].split())
    if not literal_subtypes:
        return []

    # Restrict to subtypes the commander actually MENTIONS anywhere in its
    # own port set. Atraxa is a Phyrexian Angel by literal type but has zero
    # references to Phyrexian/Angel anywhere in her port data — she's not a
    # tribal commander, just a creature that happens to share subtypes with
    # some lord cards. Krenko's scaling port references Goblin, The Ur-Dragon's
    # AttackersDeclared trigger references Dragon (in raw_line as
    # ``ValidAttackers$ Dragon.YouCtrl``), etc.
    #
    # We also follow tribal *production*: Slimefoot is literally a Fungus but
    # creates Saproling tokens via TokenScript. The tribal axis a player
    # actually wants for Slimefoot is Saproling lords, not Fungus lords. So
    # we collect every subtype the commander mentions across ALL fields and
    # union it with the literal subtype set; lord matching uses the
    # intersection of *that* with the candidate lord's affected scope.
    cmdr_subtypes: set[str] = set()
    cmdr_ports = load_ports_for_set(conn, commander_set)
    for p in cmdr_ports:
        haystack_parts = [
            p.get("valid_filter") or "",
            p.get("affected_scope") or "",
            str(p.get("raw_line") or ""),
        ]
        haystack = " ".join(haystack_parts)
        if not haystack:
            continue
        for sub in literal_subtypes:
            if sub in haystack:
                cmdr_subtypes.add(sub)
    if not cmdr_subtypes:
        return []

    cur = conn.execute("SELECT * FROM card_ports WHERE port_type = 'static' AND event_class = 'Continuous'")
    cmdr_set = set(commander_set)

    # Dedupe per (lord_card, matched_tribes_tuple). Cards like Rick,
    # Steadfast Leader have FOUR static Continuous ports — one per modal
    # AddKeyword combination — and the previous matcher returned all four,
    # giving Rick 4 × LORD_WEIGHT (= 48 instead of 12) for the same tribe.
    # We keep the strongest branch (lowest discount) per (card, tribe).
    best: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for r in cur.fetchall():
        port = _row_to_dict(r)
        if port["card_name"] in cmdr_set:
            continue
        scope = port.get("affected_scope") or ""
        if not scope:
            continue
        attrs = explode_filter(scope)
        scope_subtypes = {a["attr_value"] for a in attrs if a["attr_kind"] == "subtype"}
        overlap = scope_subtypes & cmdr_subtypes
        if not overlap:
            continue
        match_row = {
            "lord_card": port["card_name"],
            "affected_scope": scope,
            "matched_tribes": sorted(overlap),
            "amount": port.get("amount"),
            "branch_kind": port.get("branch_kind") or "root",
            "is_conditional": bool(port.get("is_conditional")),
        }
        key = (port["card_name"], tuple(sorted(overlap)))
        existing = best.get(key)
        if existing is None:
            best[key] = match_row
            continue
        # Prefer the row with the highest branch weight (root > conditional).
        if _branch_priority(match_row["branch_kind"]) > _branch_priority(existing["branch_kind"]):
            best[key] = match_row
    return list(best.values())


_BRANCH_PRIORITY: dict[str, int] = {
    "root": 100,
    "execute": 90,
    "subability": 90,
    "repeat": 85,
    "change_zone_table": 80,
    "static_condition": 60,
    "replacement_condition": 60,
    "true": 50,
    "false": 50,
    "win": 50,
    "otherwise": 50,
}


def _branch_priority(branch_kind: str | None) -> int:
    """Return a sortable priority for a branch_kind — higher = stronger."""
    return _BRANCH_PRIORITY.get(branch_kind or "root", 0)


# ---------------------------------------------------------------------------
# §6.2.3 — Panharmonicon-class amplifier
# ---------------------------------------------------------------------------


#: Trigger event_class values that a Forge ``Static$ Panharmonicon`` static
#: can amplify (read from the static's ``ValidMode$`` field). The legacy
#: forward direction (commander has trigger → match Panharmonicon static)
#: hardcodes ``ChangesZone`` only; the reverse direction (commander IS the
#: Panharmonicon → match candidate triggers) reads ``ValidMode$`` from the
#: commander's own static raw_line so it generalises to other modes.
_AMPLIFIABLE_TRIGGER_EVENTS: frozenset[str] = frozenset(
    {
        "ChangesZone",
        "ChangesZoneAll",
        "Attacks",
        "Becomes",
        "DamageDone",
        "DealDamage",
        "Crewed",
        "TapsForMana",
    }
)


def _parse_static_valid_modes(raw_line: str | None) -> set[str]:
    """Extract the ``ValidMode$`` event class set from a parsed Forge dict.

    Yarok's static stores ``ValidMode: 'ChangesZone,ChangesZoneAll'`` — we
    split on comma and intersect with :data:`_AMPLIFIABLE_TRIGGER_EVENTS`
    so unknown modes don't open the floodgates.
    """
    if not raw_line:
        return set()
    text = str(raw_line)
    # raw_line is a stringified dict like
    # ``{'Mode': 'Panharmonicon', 'ValidMode': 'ChangesZone,ChangesZoneAll', ...}``
    # We do a substring search for the ValidMode key.
    marker = "'ValidMode':"
    idx = text.find(marker)
    if idx < 0:
        marker = '"ValidMode":'
        idx = text.find(marker)
    if idx < 0:
        return set()
    after = text[idx + len(marker) :]
    # Find the next quoted string after the colon.
    quote = None
    for q in ("'", '"'):
        pos = after.find(q)
        if pos >= 0 and (quote is None or pos < quote[1]):
            quote = (q, pos)
    if quote is None:
        return set()
    qchar, start = quote
    end = after.find(qchar, start + 1)
    if end < 0:
        return set()
    value = after[start + 1 : end]
    return {tok.strip() for tok in value.split(",") if tok.strip()} & _AMPLIFIABLE_TRIGGER_EVENTS


def find_amplifier_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Cards whose Static$Panharmonicon (or similar) amplifies the commander's
    triggers — bidirectional.

    Forward direction (commander has the trigger):
        Commander has ``trigger ChangesZone Battlefield`` (ETB)
        → match every candidate with ``static Panharmonicon`` (Yarok / Naru
          Meha / Roaming Throne).

    Reverse direction (commander IS the trigger doubler):
        Commander has ``static Panharmonicon | ValidMode$ <events>``
        (Yarok the Desecrated, Roaming Throne as commander, ...)
        → match every candidate whose trigger event_class is in the
          ``ValidMode$`` set.

    Both directions emit the same row shape so callers don't need to
    distinguish them.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_set = set(commander_set)
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()

    has_etb = any(
        p.get("port_type") == "trigger"
        and (p.get("event_class") or "") == "ChangesZone"
        and (p.get("zone_destination") or "Battlefield") in ("Battlefield", "")
        for p in cmdr_ports
    )
    if has_etb:
        cur = conn.execute("SELECT * FROM card_ports WHERE port_type = 'static' AND event_class = 'Panharmonicon'")
        for r in cur.fetchall():
            if r["card_name"] in cmdr_set or r["card_name"] in seen:
                continue
            seen.add(r["card_name"])
            matches.append(
                {
                    "amplifier_card": r["card_name"],
                    "branch_kind": r["branch_kind"] or "root",
                    "is_conditional": bool(r["is_conditional"]),
                    "direction": "forward",
                }
            )

    # Reverse direction — collect every ``static Panharmonicon`` ValidMode
    # set the commander already provides.
    cmdr_amplified_modes: set[str] = set()
    for p in cmdr_ports:
        if p.get("port_type") != "static":
            continue
        if (p.get("event_class") or "") != "Panharmonicon":
            continue
        cmdr_amplified_modes |= _parse_static_valid_modes(p.get("raw_line"))

    if cmdr_amplified_modes:
        placeholders = ",".join("?" * len(cmdr_amplified_modes))
        cur = conn.execute(
            f"SELECT DISTINCT card_name, event_class, branch_kind, is_conditional "
            f"FROM card_ports "
            f"WHERE port_type = 'trigger' AND event_class IN ({placeholders})",
            tuple(cmdr_amplified_modes),
        )
        for r in cur.fetchall():
            cand = r["card_name"]
            if cand in cmdr_set or cand in seen:
                continue
            seen.add(cand)
            matches.append(
                {
                    "amplifier_card": cand,
                    "branch_kind": r["branch_kind"] or "root",
                    "is_conditional": bool(r["is_conditional"]),
                    "direction": "reverse",
                    "trigger_event": r["event_class"],
                }
            )
    return matches


# ---------------------------------------------------------------------------
# §6.5 — replacement anti-synergy
# ---------------------------------------------------------------------------

#: Replacement events that, when prevented, block the matching trigger event.
REPLACEMENT_BLOCKS_TRIGGER: dict[str, frozenset[str]] = {
    "DamageDone": frozenset({"DamageDone"}),
    "Mill": frozenset({"Milled"}),
    "Draw": frozenset({"Drawn"}),
    "GainLife": frozenset({"LifeGained"}),
    "LoseLife": frozenset({"LifeLost"}),
    "Discard": frozenset({"Discarded"}),
    # Phase C1: zone-change replacements (Grafdigger's Cage, Soulless
    # Jailer, Kunoros, Worms of the Earth, Weathered Runestone). Pairs
    # with a zone-aware filter in find_replacement_conflicts so the
    # block applies only when the trigger's zone_origin/destination
    # overlap with the replacement's. Without that filter, every
    # ChangesZone trigger commander would be falsely flagged.
    "Moved": frozenset({"ChangesZone"}),
}


def _card_attrs_for_filter(card_row: PortRow) -> list[tuple[str, str]]:
    """Build the ``(attr_kind, attr_value)`` pairs for a card row that match
    the format :func:`filter_matches` expects.

    Maps:

    * card_types → ``('type', X)`` for each space-separated token
    * supertypes → ``('supertype', X)``
    * subtypes   → ``('subtype', X)``
    * keywords   → ``('subtype', 'with<Keyword>')`` because Forge encodes
                    keyword filters as ``Creature.withDefender``
    * colors     → ``('color', X)`` for each pip in ``color_identity``
    """
    attrs: list[tuple[str, str]] = []
    for t in (card_row.get("card_types") or "").split():
        attrs.append(("type", t))
    for t in (card_row.get("supertypes") or "").split():
        attrs.append(("supertype", t))
    for t in (card_row.get("subtypes") or "").split():
        attrs.append(("subtype", t))
    raw_kw = card_row.get("keywords") or ""
    if isinstance(raw_kw, str) and raw_kw:
        try:
            for kw in json.loads(raw_kw):
                if not isinstance(kw, str):
                    continue
                # Forge filters use the head word: "Defender", "Flying" etc.
                # Many keywords have parameters ("Ward 2", "Annihilator 1")
                # which we strip before encoding.
                head = kw.split(":", 1)[0].split(" ", 1)[0]
                if head:
                    attrs.append(("subtype", f"with{head}"))
        except (ValueError, TypeError):
            pass
    for pip in (card_row.get("color_identity") or "").split(","):
        pip = pip.strip()
        if pip:
            attrs.append(("color", pip))
    return attrs


#: Triggers that fire when a card enters / dies / is sacrificed — i.e.
#: any trigger where the *event source* is the candidate card itself. The
#: self-event matcher walks every commander port whose ``event_class`` is
#: in this set and uses the trigger's ``valid_filter`` to find candidates
#: that match by their static card attributes.
_SELF_EVENT_TRIGGERS: frozenset[str] = frozenset(
    {
        "ChangesZone",  # ETB / leaves / dies (zone-direction sensitive)
        "ChangesZoneAll",
        "Sacrificed",  # Korvold-class — candidate IS sacrificed
        "Discarded",
        "Milled",
        "Drawn",
    }
)

# Backwards-compat alias for the older test set.
_ETB_TRIGGER_EVENTS = _SELF_EVENT_TRIGGERS

#: Filter attribute kinds whose values are runtime states, not static card
#: attributes. ``Other`` ("not the trigger source") is always satisfied for
#: any other card; ``attacking`` / ``tapped`` etc. are runtime board states.
_RUNTIME_SUBTYPE_VALUES: frozenset[str] = frozenset(
    {
        "Other",
        "attacking",
        "tapped",
        "untapped",
        "kicked",
        "blocking",
        "defending",
        "blocked",
        "enchanted",
        "equipped",
        "haunted",
        "monstrous",
        "renowned",
        "transformed",
        "flipped",
        "exalted",
        "prowled",
        "embalmed",
        "eternal",
        "sealed",
        "noToken",
        "nonToken",
        "token",
        "OppCtrl",
        "YouCtrl",
    }
)


#: Primary card types that are so numerous they flood ETB-self scoring
#: when used as the sole filter criterion. ``Creature`` (~50% of pool),
#: ``Permanent`` (everything non-spell), ``Card`` (everything).
#: NOT included: ``Land`` (1.1k cards — useful for Tatyova/Omnath),
#: ``Artifact`` (3k — useful for Urza/Jhoira), ``Enchantment`` (1.5k).
_BROAD_CARD_TYPES: frozenset[str] = frozenset(
    {
        "Creature",
        "Permanent",
        "Card",
    }
)

#: Qualifier tokens in Forge valid_filters that don't narrow the match
#: to a specific card subtype. ``Other``, ``YouCtrl``, ``!token``, etc.
_BROAD_QUALIFIERS: frozenset[str] = frozenset(
    {
        "Other",
        "YouCtrl",
        "OppCtrl",
        "token",
        "!token",
        "inZoneBattlefield",
        "inZoneGraveyard",
        "inZoneHand",
        "!wasCastFromYourHandByYou",
    }
)


@lru_cache(maxsize=8192)
def _compile_filter(
    valid_filter: str,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """Parse ``valid_filter`` once into ``(required, forbidden)`` tuples.

    Caches the result — each commander visits the same ~hundreds of
    filter strings across tens of thousands of ``_filter_card_match``
    calls, and ``explode_filter`` itself costs ~3 µs per invocation.
    Returned tuples are immutable, so sharing them across callers is
    safe. Unknown runtime kinds (``attacking``, ``Other``, ...) are
    stripped here so the hot matching loop only handles static attrs.
    """
    if not valid_filter:
        return ((), ())
    required: list[tuple[str, str]] = []
    forbidden: list[tuple[str, str]] = []
    for attr in explode_filter(valid_filter):
        kind = attr["attr_kind"]
        if kind in _RUNTIME_ATTR_KINDS:
            continue
        value = attr["attr_value"]
        if kind == "subtype" and value in _RUNTIME_SUBTYPE_VALUES:
            continue
        pair = (kind, value)
        (forbidden if attr["is_negated"] else required).append(pair)
    return (tuple(required), tuple(forbidden))


def _filter_card_match(
    valid_filter: str,
    card_row: PortRow,
    cand_attrs: frozenset[tuple[str, str]] | None = None,
) -> bool:
    """Return True iff a card row's static attributes satisfy the filter,
    treating runtime subtype values (``Other``, ``attacking``, ...) as
    always satisfied.

    ``cand_attrs`` is an optional pre-built attribute set (from
    ``CandidateCache.card_attrs``). When provided, the per-call
    string-parsing of card_types / supertypes / subtypes / keywords /
    color_identity is skipped. Batch evaluation takes tens of thousands
    of filter-matching calls per commander, so reusing a single
    precomputed set is the bulk of the win.
    """
    if not valid_filter:
        return False
    required, forbidden = _compile_filter(valid_filter)
    if cand_attrs is None:
        cand_attrs = frozenset(_card_attrs_for_filter(card_row))
    if any(p in cand_attrs for p in forbidden):
        return False
    return all(p in cand_attrs for p in required)


# ---------------------------------------------------------------------------
# Phase D2 — mana restriction matcher (positive boost only)
# ---------------------------------------------------------------------------


def _zone_overlap(
    replacement_origin: str,
    replacement_dest: str,
    cmdr_triggers_with_zones: list[tuple[str, str, str]],
) -> bool:
    """Phase C1: True iff any commander ChangesZone trigger has a zone
    pair compatible with the replacement's Origin$/Destination$.

    Empty replacement zone fields mean "any zone" → match anything. A
    populated replacement zone must intersect the trigger's same field
    (treating empty trigger zones as "any" too, since Forge omits them
    when the trigger is unscoped).
    """

    def _split(zones: str) -> set[str]:
        return {z.strip() for z in zones.split(",") if z.strip()}

    r_orig = _split(replacement_origin)
    r_dest = _split(replacement_dest)
    for ev, t_orig, t_dest in cmdr_triggers_with_zones:
        if ev != "ChangesZone":
            continue
        t_orig_set = _split(t_orig)
        t_dest_set = _split(t_dest)
        # Empty side = "any" → consider it compatible.
        orig_ok = not r_orig or not t_orig_set or bool(r_orig & t_orig_set)
        dest_ok = not r_dest or not t_dest_set or bool(r_dest & t_dest_set)
        if orig_ok and dest_ok:
            return True
    return False


def _is_substitution_blocking_result(
    replacement_event: str,
    replacement_result: str,
    zone_destination: str,
) -> bool:
    """Phase C2: detect ``ReplaceWith$`` substitutions that are
    *equivalent* to a Prevent for cluster-anti-synergy purposes.

    Only one unambiguous pattern is handled today: Rest in Peace-class
    graveyard hate, where a ``Moved`` replacement with
    ``Destination$ Graveyard`` and ``ReplaceWith$ Exile`` reroutes
    everything that would die into exile instead. For the commander's
    ChangesZone Battlefield→Graveyard trigger this is a full block —
    the creature never enters the graveyard, so the trigger never fires.

    Other ReplaceWith substitution patterns found in the corpus are
    *amplifiers* (``GainDouble``, ``DmgTwice``) or ``Card.Self`` entry
    modifiers (``ETBTapped``) — neither should produce a conflict.
    """
    if (replacement_event or "") != "Moved":
        return False
    if (replacement_result or "") != "Exile":
        return False
    # Must be replacing an ENTRY into the graveyard (dest=Graveyard).
    # Origin is intentionally unchecked — Rest in Peace uses an empty
    # Origin field meaning "from anywhere".
    return (zone_destination or "") == "Graveyard"


def find_replacement_conflicts(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Find ``R:`` lines that block commander triggers — both outright
    Prevent replacements and C2 substitution equivalents.

    Phase C1: ``Event$ Moved | Prevent$ True`` is matched zone-aware so
    Grafdigger's Cage flags Karador / Meren but NOT Brago / Yarok.

    Phase C2: ``Event$ Moved | Destination$ Graveyard | ReplaceWith$ Exile``
    (Rest in Peace, Leyline of the Void, Rayami — 54 non-opponent-scoped
    cards in the corpus) is treated as a substitution-block with
    ``match_kind='substitution'`` so the scoring layer can half-weight
    it relative to Prevent blocks.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_triggers = {(p.get("event_class") or "") for p in cmdr_ports if p.get("port_type") == "trigger"}
    if not cmdr_triggers:
        return []

    cmdr_zone_triggers: list[tuple[str, str, str]] = [
        (
            (p.get("event_class") or ""),
            (p.get("zone_origin") or ""),
            (p.get("zone_destination") or ""),
        )
        for p in cmdr_ports
        if p.get("port_type") == "trigger" and (p.get("event_class") or "") == "ChangesZone"
    ]

    cur = conn.execute("SELECT * FROM card_ports WHERE port_type = 'replacement'")
    conflicts: list[dict[str, Any]] = []
    cmdr_set = set(commander_set)
    for r in cur.fetchall():
        port = _row_to_dict(r)
        if port["card_name"] in cmdr_set:
            continue
        result = port.get("replacement_result") or ""
        ev = port.get("replacement_event") or ""
        zone_dest = port.get("zone_destination") or ""
        is_prevent = result == "Prevent"
        is_substitution = not is_prevent and _is_substitution_blocking_result(ev, result, zone_dest)
        if not (is_prevent or is_substitution):
            continue
        # Substitutions must also NOT be opponent-scoped — the 11 cards
        # in the corpus with ``ValidLKI$ Creature.OppCtrl`` are exiling
        # *opponent* creatures and are actually friendly for reanimator
        # commanders (they deny the opponent's recursion).
        if is_substitution and _is_unhelpful_payoff_trigger(port.get("valid_filter")):
            continue
        blocked = REPLACEMENT_BLOCKS_TRIGGER.get(ev, frozenset())
        intersect = blocked & cmdr_triggers
        # Phase C1: zone-scope ``Moved`` so it only flags reanimator-style
        # commanders whose ChangesZone triggers actually share an origin
        # / destination with the replacement's filter.
        if (
            intersect
            and ev == "Moved"
            and not _zone_overlap(
                port.get("zone_origin") or "",
                port.get("zone_destination") or "",
                cmdr_zone_triggers,
            )
        ):
            continue
        if intersect:
            conflicts.append(
                {
                    "anti_synergy_card": port["card_name"],
                    "replacement_event": ev,
                    "replacement_result": result,
                    "match_kind": "substitution" if is_substitution else "prevent",
                    "blocked_triggers": sorted(intersect),
                    "branch_kind": port.get("branch_kind") or "root",
                    "is_conditional": bool(port.get("is_conditional")),
                }
            )
    return conflicts


# ---------------------------------------------------------------------------
# Phase D1 — sacrifice outlet ↔ payoff matcher
# ---------------------------------------------------------------------------


def _is_unhelpful_payoff_trigger(valid_filter: str | None) -> bool:
    """Phase D1: reject trigger filters that are mechanically NOT a
    sacrifice / death cluster payoff.

    Two cases:

    1. **Opponent-scope** (``OppCtrl`` / ``Opponent``) — fires on
       opponents losing life or sacrificing. The friendly cluster wants
       the same-player path, so opponent triggers are anti-synergy noise.

    2. **Card.Self-only** — EVERY alternative is ``Card.Self``, meaning
       the trigger only fires on the source's own death. These don't
       scale with the deck's death rate.

    Multi-alternative filters like ``Card.Self,Creature.Other+YouCtrl``
    (Zulaport Cutthroat) are NOT rejected — the ``Creature.Other`` part
    is a real payoff that fires on other creatures dying.

    Empty / unscoped filters pass — in practice the surrounding
    ``ValidPlayer$`` field (which we don't currently store on the port)
    almost always defaults to friendly.
    """
    if not valid_filter:
        return False
    if "OppCtrl" in valid_filter or "Opponent" in valid_filter:
        return True
    # Check if ALL alternatives are Card.Self (self-only death trigger).
    # If any alternative is NOT Card.Self, the trigger has a real payoff path.
    alts = [a.strip() for a in valid_filter.split(",") if a.strip()]
    return bool(alts and all(a.startswith("Card.Self") for a in alts))
