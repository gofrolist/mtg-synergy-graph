"""Per-connection cache for :class:`RuleInterpreter`.

``find_all_complements`` is called once per commander; at scale
(100+ commanders in an audit run) re-parsing ``rules_seed.json``,
re-validating every row's JSON predicates, and re-compiling every
candidate predicate's SQL fragment for each call is measurable hot-
path cost. Cache per connection so each (conn, interpreter) pair is
built exactly once.

.. warning::

    The cache key uses ``id(conn)`` (CPython object address), which
    can be reused after a connection is closed and garbage-collected.
    Always call :func:`clear_interpreter_cache` before replacing a
    connection object to avoid stale results. This mirrors the
    convention used by ``graph_engine._ports_cache`` /
    :func:`graph_engine.clear_ports_cache`.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..port_graph.interpreter import RuleInterpreter

_interpreter_cache: dict[int, RuleInterpreter] = {}


def get_interpreter(conn: sqlite3.Connection) -> RuleInterpreter:
    """Return a :class:`RuleInterpreter` bound to ``conn``, constructing
    it on first access and caching for subsequent calls.

    The import of :class:`RuleInterpreter` is deferred to avoid a
    circular import (``port_graph.interpreter`` imports
    :class:`PortComplement` from ``complement_rules.core``).
    """
    key = id(conn)
    cached = _interpreter_cache.get(key)
    if cached is not None:
        return cached
    # Lazy import to avoid circular dependency with complement_rules.core.
    from ..port_graph.interpreter import RuleInterpreter

    interp = RuleInterpreter(conn)
    _interpreter_cache[key] = interp
    return interp


def clear_interpreter_cache() -> None:
    """Drop all cached interpreters.

    Call between commander runs that replace the SQLite connection
    (e.g. test fixtures that open a fresh in-memory DB per case) —
    CPython can reuse an ``id(conn)`` after garbage collection and a
    stale entry would then serve the wrong rules table.
    """
    _interpreter_cache.clear()
