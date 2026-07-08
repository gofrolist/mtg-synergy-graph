"""Per-connection cache for :class:`RuleInterpreter`.

``find_all_complements`` is called once per commander; at scale
(100+ commanders in an audit run) re-parsing ``rules_seed.json``,
re-validating every row's JSON predicates, and re-compiling every
candidate predicate's SQL fragment for each call is measurable hot-
path cost. Cache per connection so each (conn, interpreter) pair is
built exactly once.

Keyed on the connection object itself (via ``WeakKeyDictionary``)
rather than ``id(conn)``. A prior version keyed on ``id(conn)``, a
CPython memory address that gets reused once a connection is closed
and garbage-collected — under pytest-xdist this let a closed sibling
test's connection "poison" a brand-new connection with a stale
:class:`RuleInterpreter` (built against a possibly-unseeded ``rules``
table) whenever the new object happened to land at the same address.
A ``WeakKeyDictionary`` entry is removed by its weakref callback at
the moment the connection is finalized — strictly before that memory
can be reused — so no address-reuse collision is possible. This
mirrors ``graph_engine._ports_cache`` / :func:`graph_engine.
clear_ports_cache`. Connections not created via :func:`db.open_db`
(e.g. a raw ``sqlite3.connect()`` in a test) aren't weak-referenceable;
those transparently skip caching (always correct, just uncached)
rather than risk the same bug via a fallback ``id()``-keyed path.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..port_graph.interpreter import RuleInterpreter

logger = logging.getLogger(__name__)

_interpreter_cache: weakref.WeakKeyDictionary[sqlite3.Connection, RuleInterpreter] = weakref.WeakKeyDictionary()


def _validate_no_drift(interp: RuleInterpreter) -> None:
    """Fail loudly when the DB ``rules`` table has drifted from
    ``data/rules_seed.json``.

    Routing (``DECLARATIVE_RULE_IDS``) is derived from the seed JSON,
    but execution reads the DB table — and ``seed_rules_db`` never
    deletes rows. Without this check, a rule deleted from the JSON
    keeps firing from its stale DB row, and a rule added to the JSON
    but not yet re-seeded silently never fires.

    An EMPTY rules table is tolerated with a warning: test fixture
    DBs are routinely built without seeding, and the interpreter
    correctly contributes nothing there.
    """
    from .registry import DECLARATIVE_RULE_IDS

    db_ids = interp.rule_ids
    if db_ids == DECLARATIVE_RULE_IDS:
        return
    if not db_ids:
        logger.warning(
            "rules table is empty but %d rule_id(s) are declaratively routed — "
            "declarative rules will not fire for this connection. Re-run "
            "scripts/import_cardsfolder.py or seed_rules_db(conn) to seed them.",
            len(DECLARATIVE_RULE_IDS),
        )
        return
    missing = sorted(DECLARATIVE_RULE_IDS - db_ids)
    stale = sorted(db_ids - DECLARATIVE_RULE_IDS)
    raise ValueError(
        "declarative-rule drift between data/rules_seed.json and the DB rules "
        f"table: missing from DB {missing}; stale in DB {stale}. Re-run "
        "scripts/import_cardsfolder.py or seed_rules_db(conn) to resync."
    )


def get_interpreter(conn: sqlite3.Connection) -> RuleInterpreter:
    """Return a :class:`RuleInterpreter` bound to ``conn``, constructing
    it on first access and caching for subsequent calls.

    The import of :class:`RuleInterpreter` is deferred to avoid a
    circular import (``port_graph.interpreter`` imports
    :class:`PortComplement` from ``complement_rules.core``).
    """
    try:
        cached = _interpreter_cache.get(conn)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    # Lazy import to avoid circular dependency with complement_rules.core.
    from ..port_graph.interpreter import RuleInterpreter

    interp = RuleInterpreter(conn)
    _validate_no_drift(interp)
    with contextlib.suppress(TypeError):
        _interpreter_cache[conn] = interp
    return interp


def clear_interpreter_cache() -> None:
    """Drop all cached interpreters.

    Call between commander runs that replace the SQLite connection
    (e.g. test fixtures that open a fresh in-memory DB per case).
    """
    _interpreter_cache.clear()
