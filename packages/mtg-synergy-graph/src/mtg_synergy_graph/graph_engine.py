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
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .attributes import explode_filter

# ---------------------------------------------------------------------------
# §6.1.2 event matching map
# ---------------------------------------------------------------------------

PortRow = dict[str, Any]
EventCheck = Callable[[PortRow, PortRow], bool]


def _zones_compatible(trig: PortRow, eff: PortRow) -> bool:
    """ChangesZone matching: effect's destination must be a subset of trigger's."""
    t_dest = (trig.get("zone_destination") or "").strip()
    e_dest = (eff.get("zone_destination") or "").strip()
    if not t_dest or t_dest in ("Any", "*"):
        return True
    return not e_dest or e_dest == t_dest


def _counters_compatible(trig: PortRow, eff: PortRow) -> bool:
    t_ct = (trig.get("counter_type") or "").strip()
    e_ct = (eff.get("counter_type") or "").strip()
    if not t_ct:
        return True
    return e_ct == "" or e_ct == t_ct


def _always(trig: PortRow, eff: PortRow) -> bool:  # noqa: ARG001
    return True


#: Triggers whose semantic is "the candidate card itself, when cast/played/
#: attacking, is the event" — these are NOT matched against arbitrary effect
#: ports of unrelated cards. They route through a separate code path that
#: checks the candidate card's identity (is-a-spell / is-a-land / is-a-
#: creature). Without this exclusion, ``Attacks*`` would match every effect
#: in the database and Wrath of God would look like a Korvold feeder.
CATCH_ALL_TRIGGERS: frozenset[str] = frozenset(
    {"SpellCast", "LandPlayed", "Attacks", "AttackerBlocked", "BecomesTarget"}
)


#: Trigger event class → {effect event class | "*": predicate}.
EVENT_MATCH_MAP: dict[str, dict[str, EventCheck]] = {
    "ChangesZone": {
        "Token":         lambda t, e: t.get("zone_destination") in ("Battlefield", "", None),
        "ChangeZone":    _zones_compatible,
        "ChangeZoneAll": _zones_compatible,
        "CopyPermanent": lambda t, e: t.get("zone_destination") in ("Battlefield", "", None),
        "Animate":       lambda t, e: t.get("zone_destination") in ("Battlefield", "", None),
    },
    "CounterAdded": {
        "PutCounter":    _counters_compatible,
        "PutCounterAll": _counters_compatible,
        "Proliferate":   _always,
    },
    "SpellCast":      {"*": _always},
    "DamageDone":     {"DealDamage": _always, "DamageAll": _always},
    "LifeGained":     {"GainLife": _always},
    "Sacrificed":     {"Sacrifice": _always, "SacrificeAll": _always},
    "Discarded":      {"Discard": _always},
    "Drawn":          {"Draw": _always},
    "Taps":           {"Tap": _always, "TapAll": _always},
    "Untaps":         {"Untap": _always, "UntapAll": _always},
    "LandPlayed":     {"*": _always},
    "Attacks":        {"*": _always},
    "AttackerBlocked": {"*": _always},
    "TapsForMana":    {"Mana": _always},
    "BecomesTarget":  {"*": _always},
    # Phase B1: trigger ↔ effect pairs from corpus inventory.
    # Sacrificed/Discarded/Drawn are already covered above; these were
    # missing because the trigger Mode$ value differs from the effect
    # verb name (Investigated vs Investigate, etc.).
    "Proliferate":    {"Proliferate": _always},   # 7 trigs / 69 effects
    "Investigated":   {"Investigate":  _always},  # 2 trigs / 117 effects
    "Surveil":        {"Surveil":      _always},  # 11 trigs / 136 effects
    "LifeLost":       {"LoseLife":     _always},  # 21 trigs / 1000 effects
}

#: Cost-port event_class → set of trigger event_classes that this cost
#: directly feeds (paying the cost causes the trigger to fire).
#: §6.3 cost↔trigger feed.
COST_FEEDS_TRIGGER: dict[str, frozenset[str]] = {
    "sacrifice":        frozenset({"Sacrificed"}),
    "discard":          frozenset({"Discarded"}),
    "exile":            frozenset({"Exiled"}),
    "exile_from_grave": frozenset({"Exiled"}),
    "exile_from_hand":  frozenset({"Exiled"}),
    "exile_from_top":   frozenset({"Exiled"}),
    "mill":             frozenset({"Milled"}),
    "pay_life":         frozenset({"LifeLost"}),
    "tap":              frozenset({"Taps"}),
    "tap_type":         frozenset({"Taps"}),
}


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

    targets = EVENT_MATCH_MAP.get(t_event)
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
    return dict(zip(row.keys(), tuple(row)))


def _rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[PortRow]:
    """Bulk-convert a cursor result set to a list of plain dicts.

    Only inspects ``row.keys()`` once per cursor — all subsequent rows
    reuse the key tuple, shaving another ~15% off the already-5x-faster
    :func:`_row_to_dict` helper when converting the large port fetches
    that dominate :func:`find_trigger_feeders` /
    :func:`find_etb_self_matches`.
    """
    if not rows:
        return []
    keys = rows[0].keys()
    return [dict(zip(keys, tuple(r))) for r in rows]


def load_ports_for(conn: sqlite3.Connection, card_name: str) -> list[PortRow]:
    """Return every port row for a single card as a list of dicts."""
    cur = conn.execute(
        "SELECT * FROM card_ports WHERE card_name = ?",
        (card_name,),
    )
    return _rows_to_dicts(cur.fetchall())


def load_ports_for_set(
    conn: sqlite3.Connection,
    card_names: Sequence[str],
) -> list[PortRow]:
    """Union of port rows across multiple cards (partner-pair friendly)."""
    if not card_names:
        return []
    placeholders = ",".join("?" * len(card_names))
    cur = conn.execute(
        f"SELECT * FROM card_ports WHERE card_name IN ({placeholders})",
        tuple(card_names),
    )
    return _rows_to_dicts(cur.fetchall())


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


_PRIMARY_TYPES_FOR_FILTER: frozenset[str] = frozenset({
    "Artifact", "Creature", "Enchantment", "Instant", "Land",
    "Planeswalker", "Sorcery", "Tribal", "Battle",
})


def _trigger_required_card_types(valid_filter: str | None) -> frozenset[str]:
    """Return the set of primary card types this trigger filter requires.

    Pulls every comma-separated alternative that ISN'T ``Card.Self``,
    explodes its filter expression, and collects the type-kind attributes.
    Self-only triggers are already skipped before this is called; for
    mixed self+other triggers we use only the other-card alternatives so
    we don't accidentally allow every type.
    """
    if not valid_filter:
        return frozenset()
    types: set[str] = set()
    for alt in valid_filter.split(","):
        alt = alt.strip()
        if not alt or alt.startswith("Card.Self"):
            continue
        for attr in explode_filter(alt):
            if attr["attr_kind"] != "type" or attr["is_negated"]:
                continue
            value = attr["attr_value"]
            if value in _PRIMARY_TYPES_FOR_FILTER:
                types.add(value)
    return frozenset(types)


# Effect events that *can* produce / move cards onto the battlefield. We
# only need to do produced-type checking for these; anything else (Mill,
# Damage, Draw, ...) shouldn't satisfy an ETB-trigger filter at all.
_PRODUCING_EFFECT_EVENTS: frozenset[str] = frozenset({
    "Token", "ChangeZone", "ChangeZoneAll", "Animate", "CopyPermanent",
})

# Pulls the ChangeType / Types value out of a parsed Forge dict raw_line.
_CHANGE_TYPE_RE = re.compile(
    r"['\"]ChangeType['\"]:\s*['\"]([A-Za-z][A-Za-z0-9.+;]*)"
)
_TOKEN_SCRIPT_INLINE_RE = re.compile(
    r"['\"]TokenScript['\"]:\s*['\"]([A-Za-z0-9_,]+)"
)
_DESTINATION_RE = re.compile(r"['\"]Destination['\"]:\s*['\"]([A-Za-z]+)")
_ORIGIN_RE      = re.compile(r"['\"]Origin['\"]:\s*['\"]([A-Za-z]+)")


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
    after = s[m.end():]
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
        "_a_", "treasure", "food", "clue", "blood", "gold", "map",
        "powerstone", "junk", "incubator", "shard",
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

    cur = conn.execute(
        "SELECT * FROM card_ports WHERE port_type IN ('effect','cost')"
    )
    candidate_ports = _rows_to_dicts(cur.fetchall())

    results: list[dict[str, Any]] = []
    for trig in triggers:
        t_event = (trig.get("event_class") or "").strip()
        # Catch-all triggers (SpellCast/Attacks/LandPlayed/...) match the
        # candidate card itself, not its effect ports — handled separately
        # by the per-card "is-a-spell" matcher (Phase 2.4). Skip here so
        # they do not over-credit unrelated effects.
        if t_event in CATCH_ALL_TRIGGERS:
            continue
        # Self-only triggers (commander's own ETB / death / cast triggers
        # like Urza's ``ChangesZone | Card.Self``) cannot be fed by other
        # cards — see the regression note on _trigger_only_matches_self.
        if _trigger_only_matches_self(trig.get("valid_filter")):
            continue

        # When the trigger has a card-type or subtype filter (Arcades's
        # ``Creature.withDefender+YouCtrl``, Aesi's ``Land.YouCtrl``,
        # Aunt May's ``Creature.Other+YouCtrl``, Be'lakor's
        # ``Demon.YouCtrl+Other``), the candidate's effect must produce
        # something that satisfies the filter. Without this check, every
        # ``effect ChangeZone`` cross-links to every ETB trigger — DFCs
        # like Grist Voracious Larva (with 30 ports) would flood every
        # creature commander's top page.
        #
        # The candidate-as-card case (Wall of Omens *being* a defender
        # creature for Arcades, Reassembling Skeleton dying for Korvold)
        # is handled separately by :func:`find_etb_self_matches`.
        required, forbidden = _trigger_filter_constraint(trig.get("valid_filter"))

        for cand in candidate_ports:
            if cand["card_name"] in cmdr_set:
                continue
            ptype = cand.get("port_type")
            if ptype == "effect":
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
                match_kind = "effect"
            else:  # cost
                cost_class = (cand.get("event_class") or "").strip()
                triggers_fed = COST_FEEDS_TRIGGER.get(cost_class, frozenset())
                if t_event not in triggers_fed:
                    continue
                match_kind = "cost"

            results.append(
                {
                    "candidate":      cand["card_name"],
                    "trigger_card":   trig["card_name"],
                    "trigger_event":  t_event,
                    "match_kind":     match_kind,
                    "effect_event":   cand.get("event_class"),
                    "branch_kind":    cand.get("branch_kind") or "root",
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
        "SELECT name, subtypes FROM cards WHERE name IN ({})".format(
            ",".join("?" * len(commander_set))
        ),
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

    cur = conn.execute(
        "SELECT * FROM card_ports "
        "WHERE port_type = 'static' AND event_class = 'Continuous'"
    )
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
        scope_subtypes = {
            a["attr_value"] for a in attrs if a["attr_kind"] == "subtype"
        }
        overlap = scope_subtypes & cmdr_subtypes
        if not overlap:
            continue
        match_row = {
            "lord_card":      port["card_name"],
            "affected_scope": scope,
            "matched_tribes": sorted(overlap),
            "amount":         port.get("amount"),
            "branch_kind":    port.get("branch_kind") or "root",
            "is_conditional": bool(port.get("is_conditional")),
        }
        key = (port["card_name"], tuple(sorted(overlap)))
        existing = best.get(key)
        if existing is None:
            best[key] = match_row
            continue
        # Prefer the row with the highest branch weight (root > conditional).
        if _branch_priority(match_row["branch_kind"]) > _branch_priority(
            existing["branch_kind"]
        ):
            best[key] = match_row
    return list(best.values())


_BRANCH_PRIORITY: dict[str, int] = {
    "root":                  100,
    "execute":               90,
    "subability":            90,
    "repeat":                85,
    "change_zone_table":     80,
    "static_condition":      60,
    "replacement_condition": 60,
    "true":                  50,
    "false":                 50,
    "win":                   50,
    "otherwise":             50,
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
_AMPLIFIABLE_TRIGGER_EVENTS: frozenset[str] = frozenset({
    "ChangesZone",
    "ChangesZoneAll",
    "Attacks",
    "Becomes",
    "DamageDone",
    "DealDamage",
    "Crewed",
    "TapsForMana",
})


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
    after = text[idx + len(marker):]
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
    value = after[start + 1:end]
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
        cur = conn.execute(
            "SELECT * FROM card_ports "
            "WHERE port_type = 'static' AND event_class = 'Panharmonicon'"
        )
        for r in cur.fetchall():
            if r["card_name"] in cmdr_set or r["card_name"] in seen:
                continue
            seen.add(r["card_name"])
            matches.append({
                "amplifier_card": r["card_name"],
                "branch_kind":    r["branch_kind"] or "root",
                "is_conditional": bool(r["is_conditional"]),
                "direction":      "forward",
            })

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
            matches.append({
                "amplifier_card": cand,
                "branch_kind":    r["branch_kind"] or "root",
                "is_conditional": bool(r["is_conditional"]),
                "direction":      "reverse",
                "trigger_event":  r["event_class"],
            })
    return matches


# ---------------------------------------------------------------------------
# §6.5 — replacement anti-synergy
# ---------------------------------------------------------------------------

#: Replacement events that, when prevented, block the matching trigger event.
REPLACEMENT_BLOCKS_TRIGGER: dict[str, frozenset[str]] = {
    "DamageDone": frozenset({"DamageDone"}),
    "Mill":       frozenset({"Milled"}),
    "Draw":       frozenset({"Drawn"}),
    "GainLife":   frozenset({"LifeGained"}),
    "LoseLife":   frozenset({"LifeLost"}),
    "Discard":    frozenset({"Discarded"}),
    # Phase C1: zone-change replacements (Grafdigger's Cage, Soulless
    # Jailer, Kunoros, Worms of the Earth, Weathered Runestone). Pairs
    # with a zone-aware filter in find_replacement_conflicts so the
    # block applies only when the trigger's zone_origin/destination
    # overlap with the replacement's. Without that filter, every
    # ChangesZone trigger commander would be falsely flagged.
    "Moved":      frozenset({"ChangesZone"}),
}


#: Map catch-all trigger event_class → predicate over a candidate card row.
#: SPEC §6.1.2: these triggers are fed by the candidate card's identity, not
#: by an effect port. ``card_row`` is a sqlite3.Row from ``cards``.
def _is_spell(card: PortRow) -> bool:
    types = (card.get("card_types") or "").split()
    return any(t in {"Instant", "Sorcery", "Creature", "Artifact", "Enchantment", "Planeswalker", "Battle"} for t in types)


def _is_creature(card: PortRow) -> bool:
    return "Creature" in (card.get("card_types") or "").split()


def _is_land(card: PortRow) -> bool:
    return "Land" in (card.get("card_types") or "").split()


CATCH_ALL_PREDICATES: dict[str, Callable[[PortRow], bool]] = {
    "SpellCast":       _is_spell,
    "LandPlayed":      _is_land,
    "Attacks":         _is_creature,
    "AttackerBlocked": _is_creature,
    "BecomesTarget":   lambda c: True,  # any permanent or spell can be targeted
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
_SELF_EVENT_TRIGGERS: frozenset[str] = frozenset({
    "ChangesZone",      # ETB / leaves / dies (zone-direction sensitive)
    "ChangesZoneAll",
    "Sacrificed",       # Korvold-class — candidate IS sacrificed
    "Discarded",
    "Milled",
    "Drawn",
})

# Backwards-compat alias for the older test set.
_ETB_TRIGGER_EVENTS = _SELF_EVENT_TRIGGERS

#: Filter attribute kinds whose values are runtime states, not static card
#: attributes. ``Other`` ("not the trigger source") is always satisfied for
#: any other card; ``attacking`` / ``tapped`` etc. are runtime board states.
_RUNTIME_SUBTYPE_VALUES: frozenset[str] = frozenset({
    "Other", "attacking", "tapped", "untapped", "kicked", "blocking",
    "defending", "blocked", "enchanted", "equipped", "haunted",
    "monstrous", "renowned", "transformed", "flipped", "exalted",
    "prowled", "embalmed", "eternal", "sealed", "noToken", "nonToken",
    "token", "OppCtrl", "YouCtrl",
})


def _filter_card_match(valid_filter: str, card_row: PortRow) -> bool:
    """Return True iff a card row's static attributes satisfy the filter,
    treating runtime subtype values (``Other``, ``attacking``, ...) as
    always satisfied.
    """
    if not valid_filter:
        return False
    attrs = _card_attrs_for_filter(card_row)
    cand_set = set(attrs)
    required: list[tuple[str, str]] = []
    forbidden: list[tuple[str, str]] = []
    for attr in explode_filter(valid_filter):
        if attr["attr_kind"] in _RUNTIME_ATTR_KINDS:
            continue
        if attr["attr_kind"] == "subtype" and attr["attr_value"] in _RUNTIME_SUBTYPE_VALUES:
            continue
        pair = (attr["attr_kind"], attr["attr_value"])
        (forbidden if attr["is_negated"] else required).append(pair)
    if any(p in cand_set for p in forbidden):
        return False
    return all(p in cand_set for p in required)


def find_etb_self_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Self-event match: candidates whose card row satisfies a commander
    trigger's valid_filter.

    Covers the family of triggers where the candidate IS the event source:

    * ``ChangesZone Battlefield`` — ETB triggers (Arcades, Aesi, Yarok)
    * ``ChangesZone Graveyard``   — dies triggers (Teysa, Karador-class)
    * ``Sacrificed``              — Korvold, Edgar Markov-class
    * ``Discarded`` / ``Milled`` / ``Drawn`` — payoff triggers

    Trigger feeders only matches the "candidate has an effect that produces
    the entering card" case (Bitterblossom creating Faeries, Solemn Simulacrum
    fetching a land). It misses the much more common case where the candidate
    *itself* is the event source — Wall of Omens entering for Arcades, every
    land entering for Aesi, Reassembling Skeleton dying for Korvold.

    Skips self-only triggers (commander's own ETB) and triggers with no
    valid_filter (would match every card and flood the page).
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    self_event_triggers: list[PortRow] = []
    for p in cmdr_ports:
        if p.get("port_type") != "trigger":
            continue
        ev = (p.get("event_class") or "")
        if ev not in _SELF_EVENT_TRIGGERS:
            continue
        valid_filter = p.get("valid_filter") or ""
        if not valid_filter or _trigger_only_matches_self(valid_filter):
            continue
        self_event_triggers.append(p)
    if not self_event_triggers:
        return []

    cmdr_set = set(commander_set)
    cur = conn.execute(
        "SELECT name, card_types, supertypes, subtypes, keywords, color_identity "
        "FROM cards"
    )
    cards = _rows_to_dicts(cur.fetchall())

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for trig in self_event_triggers:
        valid_filter = trig.get("valid_filter") or ""
        # Use only the alternatives that aren't ``Card.Self`` — the trigger
        # source itself is the commander; other-card alternatives are the
        # real filter (``Card.Self,Creature.Other+YouCtrl`` → match
        # Creature.Other+YouCtrl).
        usable_alts = [
            alt.strip()
            for alt in valid_filter.split(",")
            if alt.strip() and not alt.strip().startswith("Card.Self")
        ]
        if not usable_alts:
            continue
        for card in cards:
            if card["name"] in cmdr_set:
                continue
            if not any(_filter_card_match(alt, card) for alt in usable_alts):
                continue
            key = (card["name"], trig["card_name"], trig["event_class"])
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "candidate":      card["name"],
                    "trigger_card":   trig["card_name"],
                    "trigger_event":  trig["event_class"],
                    "valid_filter":   valid_filter,
                    "branch_kind":    trig.get("branch_kind") or "root",
                    "is_conditional": bool(trig.get("is_conditional")),
                }
            )
    return results


def find_catchall_card_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """For SpellCast/LandPlayed/Attacks-style triggers, return every legal
    candidate card whose type identity satisfies the trigger.

    These are deliberately handled separately from ``find_trigger_feeders``
    because the catch-all semantic is "the candidate card itself is the
    event," not "an effect port matches." Without this split, an Attacks
    trigger would match every effect in the database (see test_korvold).

    The trigger's ``valid_filter`` is consulted on top of the type
    predicate so Animar's ``SpellCast | Creature`` only matches creature
    spells, not every non-land card. Without the extra check Sol Ring,
    Path to Exile, etc. would all earn a catchall bonus from any
    spellslinger commander.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    triggers = _ports_by_type(cmdr_ports, "trigger")
    catchall_triggers = [
        t for t in triggers if (t.get("event_class") or "") in CATCH_ALL_PREDICATES
    ]
    if not catchall_triggers:
        return []

    cmdr_set = set(commander_set)
    # Catch-all predicates need card_types; the filter check needs the
    # full attribute set (subtypes / keywords / colors).
    cur = conn.execute(
        "SELECT name, card_types, supertypes, subtypes, keywords, color_identity "
        "FROM cards"
    )
    cards = _rows_to_dicts(cur.fetchall())

    results: list[dict[str, Any]] = []
    for trig in catchall_triggers:
        t_event = trig["event_class"]
        predicate = CATCH_ALL_PREDICATES[t_event]
        valid_filter = trig.get("valid_filter") or ""
        # Strip self-only and pull other-card alternatives.
        if valid_filter and _trigger_only_matches_self(valid_filter):
            continue
        usable_alts: list[str] = []
        if valid_filter:
            usable_alts = [
                alt.strip()
                for alt in valid_filter.split(",")
                if alt.strip() and not alt.strip().startswith("Card.Self")
            ]
        for card in cards:
            if card["name"] in cmdr_set:
                continue
            if not predicate(card):
                continue
            if usable_alts and not any(
                _filter_card_match(alt, card) for alt in usable_alts
            ):
                continue
            results.append(
                {
                    "candidate":      card["name"],
                    "trigger_card":   trig["card_name"],
                    "trigger_event":  t_event,
                    "branch_kind":    "root",
                    "is_conditional": False,
                }
            )
    return results


# ---------------------------------------------------------------------------
# §6.4 — scaling synergy
# ---------------------------------------------------------------------------


def _scaling_subtype(attr: dict[str, Any]) -> str | None:
    """Return the subtype value from a parsed filter attribute, or None if
    it isn't a clean tribal-style subtype.

    Self-referential ``named*`` qualifiers (e.g. ``namedTower Worker`` from
    Mine Worker's ``Count$Valid Creature.YouCtrl+namedTower Worker``) are
    skipped — they pin to a specific other card and never represent a
    generalisable synergy. Negated subtypes are also skipped because the
    candidate is *avoiding* that token, not seeking it.
    """
    if attr.get("attr_kind") != "subtype" or attr.get("is_negated"):
        return None
    value = str(attr.get("attr_value") or "")
    if not value or value.startswith("named"):
        return None
    return value


def find_scaling_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Cards whose ``SVar:Count$Valid <filter>`` filter shares a tribal
    subtype with the commander.

    Critical regression note (Round 12): the previous heuristic also matched
    on the primary card type ("Creature", "Artifact", ...). That joined every
    creature commander to every ``Count$Valid Creature.YouCtrl`` scaling
    card, flooding the top of every creature commander's page with random
    scaling creatures (Phyrexian Dreadnought, Mine Worker, Tower Worker on
    Meren, etc.). The fix restricts both sides of the join to subtypes
    only — Goblin commanders still pick up Goblin Piledriver via the
    Goblin tribal anchor, but generic Creature.YouCtrl scaling cards no
    longer cross-link to every commander.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_tokens: set[str] = set()
    for p in cmdr_ports:
        for attr in explode_filter(p.get("valid_filter") or ""):
            value = _scaling_subtype(attr)
            if value is not None:
                cmdr_tokens.add(value)
    # Commander's own SUBTYPES also count — a Goblin commander feeds any
    # card scaling on Count$Valid Goblin. Primary card types are explicitly
    # NOT added; see the regression note above.
    rows = conn.execute(
        "SELECT subtypes FROM cards WHERE name IN ({})".format(
            ",".join("?" * len(commander_set))
        ),
        tuple(commander_set),
    ).fetchall()
    for r in rows:
        cmdr_tokens.update((r["subtypes"] or "").split())

    if not cmdr_tokens:
        return []

    cur = conn.execute(
        "SELECT * FROM card_ports WHERE port_type = 'scales_with' AND valid_filter <> ''"
    )
    cmdr_set = set(commander_set)
    matches: list[dict[str, Any]] = []
    for r in cur.fetchall():
        port = _row_to_dict(r)
        if port["card_name"] in cmdr_set:
            continue
        scale_attrs = explode_filter(port.get("valid_filter") or "")
        scale_tokens = {
            value
            for a in scale_attrs
            if (value := _scaling_subtype(a)) is not None
        }
        overlap = scale_tokens & cmdr_tokens
        if overlap:
            matches.append(
                {
                    "scaling_card":     port["card_name"],
                    "scaling_expression": port.get("scaling_expression"),
                    "matched_tokens":   sorted(overlap),
                    "branch_kind":      port.get("branch_kind") or "root",
                    "is_conditional":   bool(port.get("is_conditional")),
                }
            )
    return matches


# ---------------------------------------------------------------------------
# §6.7 — DeckHints / DeckNeeds matching
# ---------------------------------------------------------------------------


def _decoded_deck_field(value: Any) -> dict[str, list[str]]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) or {}
    except (ValueError, TypeError):
        return {}


def find_deckhints_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Match deck_has / deck_hints / deck_needs annotations between
    commander and candidates — bidirectional.

    Forge's own card scripters wrote these annotations: ``deck_has``
    (what the card provides), ``deck_hints`` (what it wants), and
    ``deck_needs`` (what it requires). They are free curated synergy
    data — when both sides agree on a tag the synergy is essentially
    confirmed.

    Three matching directions:

    1. **Forward** (existing) — candidate's hints/needs ∩ commander's
       has identity (subtypes, keywords, has-Ability).
    2. **Reverse** (new) — commander's has/hints ∩ candidate's deck_has.
       Atraxa's ``has=Proliferate`` finds every card whose own deck_has
       advertises Proliferate (Inexorable Tide, Karn's Bastion, Contagion
       Engine, ...). Slimefoot's ``has=Token`` finds every Token producer.
    3. **Type/Keyword reverse** — commander's deck_has Type/Keyword
       finds candidates whose subtypes/keywords match. Picks up
       Ashling's tribal anchor via ``hints=Elemental``.

    The same row shape is emitted regardless of direction so the
    scoring layer doesn't need to special-case anything.
    """
    rows = conn.execute(
        "SELECT name, subtypes, keywords, deck_has, deck_hints, deck_needs "
        "FROM cards WHERE name IN ({})".format(
            ",".join("?" * len(commander_set))
        ),
        tuple(commander_set),
    ).fetchall()
    cmdr_subtypes:      set[str] = set()
    cmdr_keywords:      set[str] = set()
    cmdr_has_abilities: set[str] = set()
    cmdr_wanted_abilities: set[str] = set()
    cmdr_wanted_types:     set[str] = set()
    cmdr_wanted_keywords:  set[str] = set()
    for r in rows:
        cmdr_subtypes.update((r["subtypes"] or "").split())
        try:
            cmdr_keywords.update(json.loads(r["keywords"] or "[]"))
        except (ValueError, TypeError):
            pass
        # Commander's own deck_has Ability tags (Atraxa: Proliferate,
        # Slimefoot: Token, Glarb: Surveil) feed both forward (matched
        # against candidate hints) AND reverse (matched against candidate
        # has) directions.
        for ability in _decoded_deck_field(r["deck_has"]).get("Ability", []):
            cmdr_has_abilities.add(ability)
        # Commander's deck_hints / deck_needs are softer "wanted"
        # signals — they feed only the reverse direction so the
        # commander's wishlist matches candidates that genuinely have
        # the wanted property. Aminatou hints Graveyard, so cards
        # whose deck_has includes Graveyard match.
        for field in ("deck_hints", "deck_needs"):
            data = _decoded_deck_field(r[field])
            for ability in data.get("Ability", []):
                cmdr_wanted_abilities.add(ability)
            for type_name in data.get("Type", []):
                cmdr_wanted_types.add(type_name)
            for kw in data.get("Keyword", []):
                cmdr_wanted_keywords.add(kw)

    if not (
        cmdr_subtypes or cmdr_keywords or cmdr_has_abilities
        or cmdr_wanted_abilities or cmdr_wanted_types or cmdr_wanted_keywords
    ):
        return []

    cmdr_set = set(commander_set)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def _record(candidate: str, source: str, kind: str, matched: list[str]) -> None:
        for tag in matched:
            key = (candidate, source, kind, tag)
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "candidate": candidate,
                "source":    source,
                "kind":      kind,
                "matched":   [tag],
            })

    # ----- Forward direction: candidate hints/needs → commander identity -----
    cur = conn.execute(
        "SELECT name, deck_hints, deck_needs, deck_has, subtypes, keywords "
        "FROM cards"
    )
    rows_all = cur.fetchall()
    for r in rows_all:
        if r["name"] in cmdr_set:
            continue
        for field, source in (("deck_hints", "hints"), ("deck_needs", "needs")):
            data = _decoded_deck_field(r[field])
            for kind, values in data.items():
                values_set = set(values)
                if kind == "Type":
                    overlap = values_set & cmdr_subtypes
                elif kind == "Keyword":
                    overlap = values_set & cmdr_keywords
                elif kind == "Ability":
                    overlap = values_set & cmdr_has_abilities
                else:
                    overlap = set()
                if overlap:
                    _record(r["name"], source, kind, sorted(overlap))

    # ----- Reverse direction: commander has/hints → candidate has -----
    # Cards advertise what they provide via deck_has. The commander's
    # has-set and wished-for-set both match against candidate has.
    for r in rows_all:
        if r["name"] in cmdr_set:
            continue
        cand_has = _decoded_deck_field(r["deck_has"])
        cand_has_abilities = set(cand_has.get("Ability", []))
        cand_subtypes = set((r["subtypes"] or "").split())
        try:
            cand_keywords = set(json.loads(r["keywords"] or "[]"))
        except (ValueError, TypeError):
            cand_keywords = set()

        # has × has (strongest — both confirmed)
        ab_overlap = cmdr_has_abilities & cand_has_abilities
        if ab_overlap:
            _record(r["name"], "has_has", "Ability", sorted(ab_overlap))

        # has × wants (commander wishes for X, candidate provides X)
        wanted_overlap = cmdr_wanted_abilities & cand_has_abilities
        if wanted_overlap:
            _record(r["name"], "wants_has", "Ability", sorted(wanted_overlap))

        # type/keyword reverse — commander wants Type X, candidate IS X
        type_overlap = cmdr_wanted_types & cand_subtypes
        if type_overlap:
            _record(r["name"], "wants_has", "Type", sorted(type_overlap))
        kw_overlap = cmdr_wanted_keywords & cand_keywords
        if kw_overlap:
            _record(r["name"], "wants_has", "Keyword", sorted(kw_overlap))

    return matches


# ---------------------------------------------------------------------------
# §6.6 — 2-hop chain detection
# ---------------------------------------------------------------------------


def find_chain_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
    *,
    max_results_per_intermediate: int = 50,
) -> list[dict[str, Any]]:
    """Find 2-hop synergies: commander.trigger ← intermediate.effect ←
    intermediate.trigger ← candidate.effect.

    The intermediate is any non-commander card that both feeds the
    commander's trigger AND has its own trigger fed by the candidate.
    Quadratic in the worst case — bounded by ``max_results_per_intermediate``
    so a single popular intermediate cannot dominate the result list.
    """
    direct = find_trigger_feeders(conn, commander_set)
    intermediates = sorted({d["candidate"] for d in direct if d["match_kind"] == "effect"})
    if not intermediates:
        return []

    results: list[dict[str, Any]] = []
    cmdr_set = set(commander_set)
    for inter in intermediates:
        sub = find_trigger_feeders(conn, [inter])
        # Don't credit the commander itself, the intermediate, or other
        # commander-set members as candidates.
        sub = [s for s in sub if s["candidate"] not in cmdr_set and s["candidate"] != inter]
        for s in sub[:max_results_per_intermediate]:
            results.append(
                {
                    "candidate":     s["candidate"],
                    "intermediate":  inter,
                    "intermediate_event": s["trigger_event"],
                    "branch_kind":   s["branch_kind"],
                    "is_conditional": s["is_conditional"],
                }
            )
    return results


# ---------------------------------------------------------------------------
# §6.9.3 — internal synergy detection (partner pairs)
# ---------------------------------------------------------------------------


def detect_internal_synergies(
    cmdr_ports: list[PortRow],
    cmdr_names: Sequence[str],
) -> list[dict[str, Any]]:
    """Find port matches where both sides belong to commander cards.

    Returns rows describing which events one commander generates that the
    other keys on. Top events become the boost-key set for §6.9.3 candidate
    scoring.
    """
    if len(cmdr_names) < 2:
        return []
    # Catch-all triggers route through the candidate-card identity path
    # (see CATCH_ALL_TRIGGERS). Excluding them here prevents Tymna's
    # Attacks trigger from matching every partner's effect port.
    triggers = [
        p
        for p in _ports_by_type(cmdr_ports, "trigger")
        if (p.get("event_class") or "") not in CATCH_ALL_TRIGGERS
    ]
    effects = _ports_by_type(cmdr_ports, "effect")
    matches: list[dict[str, Any]] = []
    for trig in triggers:
        for eff in effects:
            if trig["card_name"] == eff["card_name"]:
                continue
            if match_event(trig, eff):
                matches.append(
                    {
                        "event_class":    trig.get("event_class"),
                        "producer":       eff["card_name"],
                        "consumer":       trig["card_name"],
                        "match_strength": 1.0,
                    }
                )
    return matches


def internal_synergy_boost(
    candidate_ports: list[PortRow],
    engine_events: set[str],
) -> int:
    """Score boost for candidates that feed the two-commander engine.

    Capped at +30 so a single bucket cannot dominate the score.
    """
    if not engine_events:
        return 0
    matches = sum(
        1
        for p in candidate_ports
        if p.get("port_type") in ("effect", "trigger")
        and (p.get("event_class") or "") in engine_events
    )
    return min(matches * 6, 30)


# ---------------------------------------------------------------------------
# Phase D2 — mana restriction matcher (positive boost only)
# ---------------------------------------------------------------------------


def _parse_restriction_tags(restriction: str) -> set[str]:
    """Parse a Forge ``RestrictValid$`` value into a set of identity tags
    that the mana would be useful for.

    Examples:

    - ``Spell.Creature``                  → ``{"Creature"}``
    - ``Spell.Dragon``                    → ``{"Dragon"}``
    - ``Spell.Creature+Dragon``           → ``{"Creature", "Dragon"}``
    - ``Spell.Instant,Spell.Sorcery``     → ``{"Instant", "Sorcery"}``
    - ``Spell.Demon,Spell.Cleric,Spell.Vampire`` → ``{"Demon","Cleric","Vampire"}``
    - ``Activated.Dragon+inZoneBattlefield``  → ``{"Dragon"}``
    - ``CostContainsX``, ``CumulativeUpkeep`` (no ``.``) → ``set()``

    Tokens whose first segment isn't a clean alphabetic class/type name
    (``cmcGE5``, ``wasCastFromYourGraveyard``, ...) are dropped — they
    are runtime modifiers, not synergy tags.
    """
    tags: set[str] = set()
    if not restriction:
        return tags
    for raw in restriction.split(","):
        token = raw.strip()
        if not token or "." not in token:
            continue
        _, rest = token.split(".", 1)
        # ``+`` separates AND-modifiers — both ``Creature`` and ``Dragon``
        # in ``Spell.Creature+Dragon`` are meaningful synergy tags.
        for piece in rest.split("+"):
            piece = piece.strip()
            if piece.isalpha() and piece[:1].isupper():
                tags.add(piece)
    return tags


def _commander_synergy_tags(
    cmdr_ports: list[PortRow],
    cmdr_card_rows: Sequence[sqlite3.Row],  # noqa: ARG001
) -> set[str]:
    """Phase D2: tag set for the commander used by the mana-restriction
    matcher.

    The tag set is built **only** from the commander's own port filters
    (``valid_filter`` and ``affected_scope``) — NOT from the literal
    ``cards.subtypes`` / ``card_types`` / ``supertypes`` columns. This
    is the principled distinction:

    - A subtype that appears in a trigger / static filter (Edgar
      Markov's ``Card.Vampire+Other`` trigger, Ur-Dragon's
      ``Dragon.Other`` static cost reducer) is mechanical evidence that
      the deck PLANS to cast lots of that subtype — exactly what a
      restricted-mana fixer wants.
    - A subtype on the literal type line alone is just what the
      commander HAPPENS to be (Kaalia is a Cleric, Rakdos is a Demon,
      every commander is Legendary). It doesn't mean the deck wants
      Cleric / Demon / Legendary fixers, and matching on it floods
      tribal commanders with generic-Legendary mana rocks (Kaalia
      regressed −0.038 NDCG when the literal subtypes were included).

    Filters can be comma-separated (Talrand's ``Instant,Sorcery``) so
    we split on commas before calling :func:`explode_filter`, which
    only knows about ``+`` and ``.``.

    The unused ``cmdr_card_rows`` parameter is kept on the signature so
    callers don't need to be rewritten if a future phase wants to add
    a curated subset of static identity tags back.
    """
    tags: set[str] = set()
    for p in cmdr_ports:
        for f in (p.get("valid_filter"), p.get("affected_scope")):
            if not f:
                continue
            for chunk in f.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                for attr in explode_filter(chunk):
                    if attr.get("attr_kind") in ("subtype", "type", "supertype"):
                        val = attr.get("attr_value")
                        if val:
                            tags.add(val)
    return tags


def find_mana_restriction_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Phase D2: cards whose ``DB$ Mana RestrictValid$`` permits exactly
    the spells / abilities the commander wants to cast / activate.

    Examples:
    - Talrand        → Baral (Spell.Instant,Spell.Sorcery)
    - The Ur-Dragon  → Dragon's Hoard (Spell.Dragon,Activated.Dragon+...)
    - Animar         → Cavern of Souls (Spell.Creature+ChosenType — the
      ChosenType modifier is runtime so we strip it; the Creature tag
      still matches Animar's creature card_type)
    - Edgar Markov   → Spell.Demon,Spell.Cleric,Spell.Vampire restrictions

    Note: this is a positive-only boost. The conflict case (restriction
    on a card that does NOT match the commander identity, e.g. Cavern
    of Souls in a non-creature deck) is not penalised here — that would
    need a separate phase that adds it to the penalty layer.
    """
    rows = conn.execute(
        "SELECT name, subtypes, card_types, supertypes "
        "FROM cards WHERE name IN ({})".format(
            ",".join("?" * len(commander_set))
        ),
        tuple(commander_set),
    ).fetchall()
    if not rows:
        return []

    cmdr_ports = load_ports_for_set(conn, commander_set)
    cmdr_tags = _commander_synergy_tags(cmdr_ports, rows)
    if not cmdr_tags:
        return []

    cmdr_set = set(commander_set)
    cur = conn.execute(
        "SELECT card_name, mana_restriction, branch_kind, is_conditional "
        "FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'Mana' "
        "AND mana_restriction IS NOT NULL AND mana_restriction != ''"
    )

    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in cur.fetchall():
        cand = r["card_name"]
        if cand in cmdr_set or cand in seen:
            continue
        rtags = _parse_restriction_tags(r["mana_restriction"] or "")
        overlap = rtags & cmdr_tags
        if not overlap:
            continue
        seen.add(cand)
        matches.append(
            {
                "candidate":        cand,
                "mana_restriction": r["mana_restriction"],
                "matched_tags":     sorted(overlap),
                "branch_kind":      r["branch_kind"] or "root",
                "is_conditional":   bool(r["is_conditional"]),
            }
        )
    return matches


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


# ---------------------------------------------------------------------------
# Phase D4 — flicker self-loop detector
# ---------------------------------------------------------------------------


def _card_has_flicker_chain(ports: list[PortRow]) -> bool:
    """Phase D4: True iff a card has both a ``ChangeZone Battlefield→Exile``
    effect and a ``ChangeZone (Exile|All)→Battlefield`` effect.

    The ``All`` origin is needed because Forge importer captures
    SubAbility chains that use ``Defined$ Remembered`` with implicit
    origin = "any zone" — Brago's ``DBReturn`` and Conjurer's Closet's
    return path both surface as ``Origin: All``. Soulherder and
    Ephemerate use the literal ``Exile`` origin.
    """
    has_bf_to_exile = False
    has_exile_to_bf = False
    for p in ports:
        if p.get("port_type") != "effect":
            continue
        if (p.get("event_class") or "") != "ChangeZone":
            continue
        orig = (p.get("zone_origin") or "").strip()
        dest = (p.get("zone_destination") or "").strip()
        if orig == "Battlefield" and dest == "Exile":
            has_bf_to_exile = True
        elif orig in ("Exile", "All") and dest == "Battlefield":
            has_exile_to_bf = True
        if has_bf_to_exile and has_exile_to_bf:
            return True
    return False


def find_flicker_loop_matches(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Phase D4: when the commander is a flicker source, award candidates
    that are ALSO flicker sources — Brago + Conjurer's Closet, Soulherder
    + Ephemerate, etc. The mutual cluster is mechanically meaningful
    (each side amplifies the other's loop) and the existing
    ``find_etb_self_matches`` / ``find_trigger_feeders`` matchers don't
    pick it up because both sides are *effects*, not trigger ↔ effect
    pairs.

    Detection is structural — both sides need a ``Battlefield → Exile``
    effect followed by an ``(Exile|All) → Battlefield`` effect. Only 162
    cards in the cardsfolder match the pattern, so the bucket-spreading
    risk is contained.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    if not _card_has_flicker_chain(cmdr_ports):
        return []

    cmdr_set = set(commander_set)
    cur = conn.execute(
        "SELECT card_name, zone_origin, zone_destination, branch_kind, is_conditional "
        "FROM card_ports "
        "WHERE port_type = 'effect' AND event_class = 'ChangeZone' "
        "AND ((zone_origin = 'Battlefield' AND zone_destination = 'Exile') "
        "OR (zone_origin IN ('Exile', 'All') AND zone_destination = 'Battlefield'))"
    )

    by_card: dict[str, dict[str, Any]] = {}
    for r in cur.fetchall():
        cn = r["card_name"]
        if cn in cmdr_set:
            continue
        info = by_card.setdefault(
            cn,
            {
                "bf_to_exile":    False,
                "exile_to_bf":    False,
                "branch_kind":    r["branch_kind"] or "root",
                "is_conditional": bool(r["is_conditional"]),
            },
        )
        if r["zone_origin"] == "Battlefield":
            info["bf_to_exile"] = True
        else:
            info["exile_to_bf"] = True
        # Keep the strongest (least discounted) branch.
        if (r["branch_kind"] or "root") == "root":
            info["branch_kind"] = "root"
        if not r["is_conditional"]:
            info["is_conditional"] = False

    matches: list[dict[str, Any]] = []
    for card, info in by_card.items():
        if info["bf_to_exile"] and info["exile_to_bf"]:
            matches.append(
                {
                    "candidate":      card,
                    "direction":      "flicker_cluster",
                    "branch_kind":    info["branch_kind"],
                    "is_conditional": info["is_conditional"],
                }
            )
    return matches


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
    if (zone_destination or "") != "Graveyard":
        return False
    return True


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
    cmdr_triggers = {
        (p.get("event_class") or "")
        for p in cmdr_ports
        if p.get("port_type") == "trigger"
    }
    if not cmdr_triggers:
        return []

    cmdr_zone_triggers: list[tuple[str, str, str]] = [
        (
            (p.get("event_class") or ""),
            (p.get("zone_origin") or ""),
            (p.get("zone_destination") or ""),
        )
        for p in cmdr_ports
        if p.get("port_type") == "trigger"
        and (p.get("event_class") or "") == "ChangesZone"
    ]

    cur = conn.execute(
        "SELECT * FROM card_ports WHERE port_type = 'replacement'"
    )
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
        is_substitution = not is_prevent and _is_substitution_blocking_result(
            ev, result, zone_dest
        )
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
        if intersect and ev == "Moved":
            if not _zone_overlap(
                port.get("zone_origin") or "",
                port.get("zone_destination") or "",
                cmdr_zone_triggers,
            ):
                continue
        if intersect:
            conflicts.append(
                {
                    "anti_synergy_card": port["card_name"],
                    "replacement_event": ev,
                    "replacement_result": result,
                    "match_kind":        "substitution" if is_substitution else "prevent",
                    "blocked_triggers":  sorted(intersect),
                    "branch_kind":       port.get("branch_kind") or "root",
                    "is_conditional":    bool(port.get("is_conditional")),
                }
            )
    return conflicts


# ---------------------------------------------------------------------------
# Phase D1 — sacrifice outlet ↔ payoff matcher
# ---------------------------------------------------------------------------

#: Cost-target values that qualify a sacrifice cost as a real outlet
#: (depends on Phase A1 ``cost_target`` field). ``self`` is excluded —
#: suspend-style ``Sac<1/CARDNAME>`` is not a generic outlet.
_OUTLET_COST_TARGETS: frozenset[str] = frozenset({"other", "any"})


def _is_unhelpful_payoff_trigger(valid_filter: str | None) -> bool:
    """Phase D1: reject trigger filters that are mechanically NOT a
    sacrifice / death cluster payoff.

    Two cases:

    1. **Opponent-scope** (``OppCtrl`` / ``Opponent``) — fires on
       opponents losing life or sacrificing. The friendly cluster wants
       the same-player path, so opponent triggers are anti-synergy noise.

    2. **Card.Self** — the trigger only fires on the source's own death
       (Blightbelly Rat, every "when CARDNAME dies" creature). These do
       NOT scale with the rest of the deck's death rate the way real
       payoffs (Blood Artist, Zulaport Cutthroat) do, so they shouldn't
       earn the cluster bonus.

    Empty / unscoped filters pass — in practice the surrounding
    ``ValidPlayer$`` field (which we don't currently store on the port)
    almost always defaults to friendly.
    """
    if not valid_filter:
        return False
    text = valid_filter
    if "OppCtrl" in text or "Opponent" in text:
        return True
    if "Card.Self" in text:
        return True
    return False


def _commander_death_signature(
    cmdr_ports: list[PortRow],
) -> tuple[bool, bool, bool]:
    """Return ``(has_death_trig, has_bf_to_gy_trig, has_outlet_cost)``.

    A "death trigger" is one of ``Sacrificed``, ``LifeLost`` (which fires
    on sac-induced life loss for cards like Blood Artist), or
    ``ChangesZone`` from Battlefield to Graveyard. The BF→GY case is
    tracked separately so the SQL queries below can lift it without an
    extra IN-list.
    """
    has_death = False
    has_bf_to_gy = False
    has_outlet_cost = False
    for p in cmdr_ports:
        ptype = p.get("port_type")
        ev = (p.get("event_class") or "").strip()
        if ptype == "trigger":
            if ev in ("Sacrificed", "LifeLost"):
                has_death = True
            elif ev == "ChangesZone":
                if (p.get("zone_origin") or "").strip() == "Battlefield" and \
                   (p.get("zone_destination") or "").strip() == "Graveyard":
                    has_death = True
                    has_bf_to_gy = True
        elif ptype == "cost" and ev == "sacrifice":
            if (p.get("cost_target") or "") in _OUTLET_COST_TARGETS:
                has_outlet_cost = True
    return has_death, has_bf_to_gy, has_outlet_cost


def find_sacrifice_synergies(
    conn: sqlite3.Connection,
    commander_set: Sequence[str],
) -> list[dict[str, Any]]:
    """Find sacrifice-based synergies above and beyond the generic
    cost-feeds-trigger path.

    Three directions are emitted:

    1. **outlet_for_payoff** — commander has a death trigger
       (``Sacrificed``, ``LifeLost``, ``ChangesZone Battlefield→Graveyard``)
       and the candidate has a sacrifice cost with
       ``cost_target IN ('other', 'any')``. Filters out the
       ``cost_target='self'`` suspend-class cards that would otherwise
       look like outlets.

    2. **payoff_cluster** — commander has a death trigger AND the
       candidate has the same kind of trigger. Captures the Korvold +
       Blood Artist mutual cluster — both fire on the same event, neither
       feeds the other through an effect port, so the existing
       trigger-feeders matcher misses the synergy entirely.

    3. **payoff_for_outlet** — commander IS the outlet (has a sacrifice
       cost with ``cost_target='other'/'any'``) and the candidate has a
       death trigger. Rare but real — Yahenni, Marrow-Gnawer, Slimefoot
       (in our golden set).

    Each (candidate, direction) pair is emitted at most once.
    """
    cmdr_ports = load_ports_for_set(conn, commander_set)
    has_death, has_bf_to_gy, has_outlet_cost = _commander_death_signature(cmdr_ports)
    if not (has_death or has_outlet_cost):
        return []

    cmdr_set = set(commander_set)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _emit(candidate: str, direction: str, **extra: Any) -> None:
        key = (direction, candidate)
        if candidate in cmdr_set or key in seen:
            return
        seen.add(key)
        matches.append({"candidate": candidate, "direction": direction, **extra})

    # Direction 1: commander payoff trigger → candidate outlet cost.
    if has_death:
        cur = conn.execute(
            "SELECT DISTINCT card_name, branch_kind, is_conditional "
            "FROM card_ports "
            "WHERE port_type = 'cost' AND event_class = 'sacrifice' "
            "AND cost_target IN ('other', 'any')"
        )
        for r in cur.fetchall():
            _emit(
                r["card_name"],
                "outlet_for_payoff",
                branch_kind=r["branch_kind"] or "root",
                is_conditional=bool(r["is_conditional"]),
            )

    # Direction 2: commander payoff trigger → candidate payoff trigger.
    # Reject explicitly opponent-scope triggers (OppCtrl / Opponent in
    # the valid_filter) so cards that fire on opponents losing life or
    # opponents sacrificing don't pollute the friendly cluster.
    if has_death:
        cur = conn.execute(
            "SELECT DISTINCT card_name, event_class, valid_filter, "
            "branch_kind, is_conditional "
            "FROM card_ports "
            "WHERE port_type = 'trigger' AND event_class IN ('Sacrificed', 'LifeLost')"
        )
        for r in cur.fetchall():
            if _is_unhelpful_payoff_trigger(r["valid_filter"]):
                continue
            _emit(
                r["card_name"],
                "payoff_cluster",
                trigger_event=r["event_class"],
                branch_kind=r["branch_kind"] or "root",
                is_conditional=bool(r["is_conditional"]),
            )
        if has_bf_to_gy:
            cur = conn.execute(
                "SELECT DISTINCT card_name, valid_filter, "
                "branch_kind, is_conditional "
                "FROM card_ports "
                "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
                "AND zone_origin = 'Battlefield' AND zone_destination = 'Graveyard'"
            )
            for r in cur.fetchall():
                if _is_unhelpful_payoff_trigger(r["valid_filter"]):
                    continue
                _emit(
                    r["card_name"],
                    "payoff_cluster",
                    trigger_event="ChangesZone:BF->GY",
                    branch_kind=r["branch_kind"] or "root",
                    is_conditional=bool(r["is_conditional"]),
                )

    # Direction 3: commander outlet cost → candidate payoff trigger.
    if has_outlet_cost:
        cur = conn.execute(
            "SELECT DISTINCT card_name, event_class, valid_filter, "
            "branch_kind, is_conditional "
            "FROM card_ports "
            "WHERE port_type = 'trigger' AND event_class IN ('Sacrificed', 'LifeLost')"
        )
        for r in cur.fetchall():
            if _is_unhelpful_payoff_trigger(r["valid_filter"]):
                continue
            _emit(
                r["card_name"],
                "payoff_for_outlet",
                trigger_event=r["event_class"],
                branch_kind=r["branch_kind"] or "root",
                is_conditional=bool(r["is_conditional"]),
            )
        cur = conn.execute(
            "SELECT DISTINCT card_name, valid_filter, "
            "branch_kind, is_conditional "
            "FROM card_ports "
            "WHERE port_type = 'trigger' AND event_class = 'ChangesZone' "
            "AND zone_origin = 'Battlefield' AND zone_destination = 'Graveyard'"
        )
        for r in cur.fetchall():
            if _is_unhelpful_payoff_trigger(r["valid_filter"]):
                continue
            _emit(
                r["card_name"],
                "payoff_for_outlet",
                trigger_event="ChangesZone:BF->GY",
                branch_kind=r["branch_kind"] or "root",
                is_conditional=bool(r["is_conditional"]),
            )

    return matches
