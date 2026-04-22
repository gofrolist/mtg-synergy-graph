"""JSON-sourced loaders for ``EVENT_MATCH_MAP`` and
``COST_FEEDS_TRIGGER`` (Unit 3 of plan 003).

The seed file ``data/event_match_seed.json`` is the single source of
truth for event equivalences. Two representations consume it:

* ``graph_engine.EVENT_MATCH_MAP`` / ``COST_FEEDS_TRIGGER`` —
  module-global Python dicts, populated at module import via
  :func:`load_event_match_map_from_json`. The dicts continue to hold
  ``EventCheck`` callables so every existing call-site
  (``match_event``, ``complement_rules.core`` helpers,
  ``graph_metrics``) keeps working unchanged.
* ``event_match_map`` / ``cost_feeds_trigger`` SQLite tables —
  populated by the importer via :func:`seed_event_match_map_db` and
  queried by the Unit 5 interpreter via
  :func:`load_event_match_map_from_db`.

Both representations are derived from the same JSON, so they cannot
drift. Adding an equivalence means editing the JSON and re-importing
— the "INSERT a row" authoring flow the brainstorm calls for, with
file-level diffability retained.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .vocabulary import MATCH_QUALITIES

PortRow = dict[str, Any]
EventCheck = Callable[[PortRow, PortRow], bool]


# ---------------------------------------------------------------------------
# Predicate primitives — one per MATCH_QUALITIES entry.
# ---------------------------------------------------------------------------


def _always(trig: PortRow, eff: PortRow) -> bool:
    """Match regardless of trigger / effect contents."""
    return True


def _zones_compatible(trig: PortRow, eff: PortRow) -> bool:
    """ChangesZone matching: effect's destination must be a subset
    of the trigger's (empty / ``Any`` / ``*`` on the trigger side
    matches any destination).

    Behaviourally equivalent to the legacy
    ``graph_engine._zones_compatible``.
    """
    t_dest = (trig.get("zone_destination") or "").strip()
    e_dest = (eff.get("zone_destination") or "").strip()
    if not t_dest or t_dest in ("Any", "*"):
        return True
    return not e_dest or e_dest == t_dest


def _zone_dest_battlefield(trig: PortRow, eff: PortRow) -> bool:
    """Trigger-side battlefield check for Token / CopyPermanent /
    Animate effects. Checks only the trigger — these effects are
    intrinsically battlefield-bound, so effect-side validation is
    redundant.

    Preserves the semantics of the inline lambda
    ``lambda t, e: t.get('zone_destination') in ('Battlefield', '', None)``
    from the legacy ``graph_engine.EVENT_MATCH_MAP``.
    """
    return trig.get("zone_destination") in ("Battlefield", "", None)


def _counters_compatible(trig: PortRow, eff: PortRow) -> bool:
    """Counter-type match: empty / equal counter types match.

    Behaviourally equivalent to legacy
    ``graph_engine._counters_compatible``.
    """
    t_ct = (trig.get("counter_type") or "").strip()
    e_ct = (eff.get("counter_type") or "").strip()
    if not t_ct:
        return True
    return e_ct == "" or e_ct == t_ct


def _tribe_match(trig: PortRow, eff: PortRow) -> bool:
    """Reserved for future rules that match on extracted subtype
    tokens. Not referenced by any current event_match_map row."""
    # The subtype extraction lives in attributes.explode_filter and
    # is only wired into a match_quality once a migrating rule needs
    # it. Keep the function body conservative — always-false fallback
    # beats silent acceptance.
    return False


def _color_match(trig: PortRow, eff: PortRow) -> bool:
    """Reserved for future rules that match on color identity
    tokens. Not referenced by any current event_match_map row."""
    return False


#: Dispatch: ``match_quality`` string → ``EventCheck`` callable. Keys
#: must equal ``MATCH_QUALITIES`` (enforced in
#: :func:`load_event_match_map_from_json`). Adding a new predicate
#: requires a vocabulary bump + an entry here + tests.
_QUALITY_DISPATCH: dict[str, EventCheck] = {
    "always": _always,
    "zone_compatible": _zones_compatible,
    "zone_dest_battlefield": _zone_dest_battlefield,
    "counter_compatible": _counters_compatible,
    "tribe_match": _tribe_match,
    "color_match": _color_match,
}


# ---------------------------------------------------------------------------
# JSON seed location + loader
# ---------------------------------------------------------------------------


def _default_seed_path() -> Path:
    """Return the repo-committed seed path, resolving via package
    resources so the seed ships with installable wheels too."""
    # ``data/event_match_seed.json`` is tracked at repo root, next
    # to the other committed audit fixtures. Walk up from this file
    # to find it; package-resources would require moving the seed
    # into the package, which is more invasive than a path walk.
    for candidate in (Path.cwd(), *Path(__file__).resolve().parents):
        p = candidate / "data" / "event_match_seed.json"
        if p.exists():
            return p
    raise FileNotFoundError("data/event_match_seed.json not found")


def _load_seed_json(path: Path | None = None) -> dict[str, Any]:
    """Load and parse the JSON seed. Validates that every
    ``match_quality`` is a member of ``MATCH_QUALITIES`` — a typo in
    the JSON fails at import instead of silently producing the
    ``_always`` or a missing entry."""
    seed_path = path or _default_seed_path()
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    for row in data.get("event_match_map", []):
        mq = row.get("match_quality")
        if mq not in MATCH_QUALITIES:
            raise ValueError(
                f"event_match_seed.json: unknown match_quality {mq!r} "
                f"for {row.get('from')!r} → {row.get('to')!r}; "
                f"valid set is {sorted(MATCH_QUALITIES)}"
            )
    return data


def load_event_match_map_from_json(
    path: Path | None = None,
) -> dict[str, dict[str, EventCheck]]:
    """Return the Python-dict form of ``EVENT_MATCH_MAP`` by reading
    the committed JSON seed. Callable at module-import time; no DB
    required.

    Signature matches legacy ``graph_engine.EVENT_MATCH_MAP`` so
    importing code can substitute without API changes.
    """
    data = _load_seed_json(path)
    out: dict[str, dict[str, EventCheck]] = {}
    for row in data["event_match_map"]:
        out.setdefault(row["from"], {})[row["to"]] = _QUALITY_DISPATCH[row["match_quality"]]
    return out


def load_cost_feeds_trigger_from_json(
    path: Path | None = None,
) -> dict[str, frozenset[str]]:
    """Return the Python-dict form of ``COST_FEEDS_TRIGGER``."""
    data = _load_seed_json(path)
    buckets: dict[str, set[str]] = {}
    for row in data.get("cost_feeds_trigger", []):
        buckets.setdefault(row["cost_event"], set()).add(row["trigger_event"])
    return {k: frozenset(v) for k, v in buckets.items()}


# ---------------------------------------------------------------------------
# DB seed + readers (for Unit 5 interpreter)
# ---------------------------------------------------------------------------


def seed_event_match_map_db(
    conn: sqlite3.Connection,
    path: Path | None = None,
) -> None:
    """Populate the ``event_match_map`` and ``cost_feeds_trigger``
    SQLite tables from the JSON seed. Called by the importer on DB
    create; safe to call on an already-seeded DB (INSERT OR REPLACE).

    The interpreter (Unit 5) reads from these tables via
    :func:`load_event_match_map_from_db` so it can filter /
    enumerate equivalences in SQL fragments — what's the point of a
    table-backed data model if the interpreter still imports from a
    dict.
    """
    data = _load_seed_json(path)
    conn.executemany(
        "INSERT OR REPLACE INTO event_match_map (from_event, to_event, match_quality) VALUES (?, ?, ?)",
        [(row["from"], row["to"], row["match_quality"]) for row in data["event_match_map"]],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO cost_feeds_trigger (cost_event, trigger_event) VALUES (?, ?)",
        [(row["cost_event"], row["trigger_event"]) for row in data.get("cost_feeds_trigger", [])],
    )
    conn.commit()


def load_event_match_map_from_db(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, EventCheck]]:
    """Read the ``event_match_map`` table and reconstruct the nested
    ``EventCheck`` dict. Used by the Unit 5 interpreter and by
    parity tests.
    """
    out: dict[str, dict[str, EventCheck]] = {}
    for row in conn.execute("SELECT from_event, to_event, match_quality FROM event_match_map").fetchall():
        out.setdefault(row["from_event"], {})[row["to_event"]] = _QUALITY_DISPATCH[row["match_quality"]]
    return out


def load_cost_feeds_trigger_from_db(
    conn: sqlite3.Connection,
) -> dict[str, frozenset[str]]:
    """Read the ``cost_feeds_trigger`` table and reconstruct the
    cost → triggers dict.
    """
    buckets: dict[str, set[str]] = {}
    for row in conn.execute("SELECT cost_event, trigger_event FROM cost_feeds_trigger").fetchall():
        buckets.setdefault(row["cost_event"], set()).add(row["trigger_event"])
    return {k: frozenset(v) for k, v in buckets.items()}
