"""Declarative rule interpreter (Unit 5 of plan 003).

Consumes ``rules`` table rows and emits ``PortComplement`` records
shape-identical to what a hand-authored Python helper would produce.
This is the single interpreter that replaces the Python-helper-per-rule
pattern for the declarative subset of the rule corpus.

Design
------

Each rule has three JSON predicate trees (see ``rules_schema`` for
the grammar):

* **gate_predicate** — Python-side; evaluated against the commander's
  ports. Determines whether the rule activates for this commander at
  all. Fast short-circuit — zero matches means zero SQL queries.
* **candidate_port_predicate** — compiled to a SQLite WHERE fragment
  against ``card_ports`` and parameters. One query per active rule
  produces the candidate card_names.
* **commander_port_predicate** — reserved for future rule families
  where the specific commander port matters for per-port emission
  (e.g., resonance rules). The POC migrations don't use it; the
  interpreter stores but does not yet consume it.

Each rule emits one ``PortComplement`` record per matching candidate.
``cmdr_event`` / ``cand_event`` / ``filter_group`` are row-level
constants; ``direction`` is currently always ``"synergy"`` (the POC
migrations don't cover anti-synergy; add an ``anti_synergy`` column to
``rules`` when a rule family needs it).

The interpreter compiles each rule's candidate predicate ONCE at
construction time, producing a ``_CompiledCandidate`` holding the SQL
fragment + parameter-builder. Per-commander calls reuse the compiled
fragment; only ``cmdr_set``-dependent parameters
(``not_in_commander_set``) are rebuilt per call.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..complement_rules.core import PortComplement, PortRow
from .rules_schema import RuleRow, load_rules_from_db, validate_gate_predicate

#: Accepted ``zone`` values for the ``zone_eq`` leaf op. Kept as a
#: frozenset so both compilers can validate consistently (CLAUDE.md
#: convention: frozenset + ValueError, never assert).
_VALID_ZONES: frozenset[str] = frozenset({"destination", "origin"})

# ---------------------------------------------------------------------------
# Compiled predicates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CompiledCandidate:
    """SQL-side compiled candidate predicate.

    * ``sql`` — WHERE clause fragment (no SELECT). Parameters are
      positional ``?`` placeholders.
    * ``static_params`` — parameters that don't depend on the
      commander set (from ``has_port``, ``filter_tag``, etc.).
    * ``needs_commander_set`` — True when the tree contains a
      ``not_in_commander_set`` leaf; per-call code appends commander
      names after the static params.
    """

    sql: str
    static_params: tuple[Any, ...]
    needs_commander_set: bool


def _compile_candidate_predicate(predicate: dict[str, Any]) -> _CompiledCandidate:
    """Walk the predicate tree and emit a SQLite WHERE fragment.

    Recursive compilation — each op produces its own fragment +
    params; ``and`` / ``or`` / ``not`` wrap children.
    """
    op = predicate["op"]

    if op == "and":
        parts = [_compile_candidate_predicate(a) for a in predicate["args"]]
        sql = " AND ".join(f"({p.sql})" for p in parts)
        params: list[Any] = []
        for p in parts:
            params.extend(p.static_params)
        return _CompiledCandidate(
            sql=sql,
            static_params=tuple(params),
            needs_commander_set=any(p.needs_commander_set for p in parts),
        )

    if op == "or":
        parts = [_compile_candidate_predicate(a) for a in predicate["args"]]
        sql = " OR ".join(f"({p.sql})" for p in parts)
        params = []
        for p in parts:
            params.extend(p.static_params)
        return _CompiledCandidate(
            sql=sql,
            static_params=tuple(params),
            needs_commander_set=any(p.needs_commander_set for p in parts),
        )

    if op == "not":
        inner = _compile_candidate_predicate(predicate["args"][0])
        return _CompiledCandidate(
            sql=f"NOT ({inner.sql})",
            static_params=inner.static_params,
            needs_commander_set=inner.needs_commander_set,
        )

    if op == "has_port":
        # ``has_port`` supports three optional equality constraints
        # on the port row: ``event_class``, ``replacement_result``,
        # and a future extension list documented in
        # rules_schema. Only ``port_type`` is required. Additional
        # constraints accumulate as AND clauses so the compiled SQL
        # stays O(k) where k = active constraints.
        port_type = predicate["port_type"]
        clauses: list[str] = ["port_type = ?"]
        params: list[Any] = [port_type]
        event_class = predicate.get("event_class")
        if event_class is not None:
            clauses.append("event_class = ?")
            params.append(event_class)
        replacement_result = predicate.get("replacement_result")
        if replacement_result is not None:
            clauses.append("replacement_result = ?")
            params.append(replacement_result)
        return _CompiledCandidate(
            sql=" AND ".join(clauses),
            static_params=tuple(params),
            needs_commander_set=False,
        )

    if op == "filter_tag":
        tag = predicate["tag"]
        return _CompiledCandidate(
            sql="valid_filter LIKE ?",
            static_params=(f"%{tag}%",),
            needs_commander_set=False,
        )

    if op == "zone_eq":
        zone = predicate["zone"]
        if zone not in _VALID_ZONES:
            raise ValueError(f"zone_eq: unknown zone {zone!r}; expected one of {sorted(_VALID_ZONES)}")
        value = predicate["value"]
        column = "zone_destination" if zone == "destination" else "zone_origin"
        return _CompiledCandidate(
            sql=f"{column} = ?",
            static_params=(value,),
            needs_commander_set=False,
        )

    if op == "counter_type":
        return _CompiledCandidate(
            sql="counter_type = ?",
            static_params=(predicate["type"],),
            needs_commander_set=False,
        )

    if op == "tribe":
        # tribe matches a subtype token in valid_filter. Same shape
        # as filter_tag — LIKE-match against the filter string.
        return _CompiledCandidate(
            sql="valid_filter LIKE ?",
            static_params=(f"%{predicate['name']}%",),
            needs_commander_set=False,
        )

    if op == "color":
        # color tokens embed in valid_filter the same way.
        return _CompiledCandidate(
            sql="valid_filter LIKE ?",
            static_params=(f"%{predicate['color']}%",),
            needs_commander_set=False,
        )

    if op == "not_in_commander_set":
        # SQL placeholder filled per-call; the number of commanders
        # is tiny (1-2) so the IN expansion is fine.
        return _CompiledCandidate(
            sql="__CMDR_SET_PLACEHOLDER__",
            static_params=(),
            needs_commander_set=True,
        )

    raise ValueError(f"interpreter: unsupported op {op!r} (should have failed schema validation)")


# ---------------------------------------------------------------------------
# Python-side gate evaluation
# ---------------------------------------------------------------------------


GatePredicate = Callable[[PortRow], bool]


def _compile_gate_predicate(predicate: dict[str, Any]) -> GatePredicate:
    """Compile a gate tree to a ``PortRow -> bool`` callable.

    Gate predicates run against commander-port dicts in Python so the
    interpreter can short-circuit before any SQL call. Same grammar as
    candidate predicates, minus ``not_in_commander_set`` (which only
    makes sense on the candidate side).
    """
    op = predicate["op"]

    if op == "and":
        children = [_compile_gate_predicate(a) for a in predicate["args"]]
        return lambda port: all(c(port) for c in children)

    if op == "or":
        children = [_compile_gate_predicate(a) for a in predicate["args"]]
        return lambda port: any(c(port) for c in children)

    if op == "not":
        child = _compile_gate_predicate(predicate["args"][0])
        return lambda port: not child(port)

    if op == "has_port":
        port_type = predicate["port_type"]
        event_class = predicate.get("event_class")
        replacement_result = predicate.get("replacement_result")

        def _check(port: PortRow) -> bool:
            if (port.get("port_type") or "").strip() != port_type:
                return False
            if event_class is not None and (port.get("event_class") or "").strip() != event_class:
                return False
            return not (
                replacement_result is not None and (port.get("replacement_result") or "").strip() != replacement_result
            )

        return _check

    if op == "filter_tag":
        tag = predicate["tag"]
        return lambda port: tag in (port.get("valid_filter") or "")

    if op == "zone_eq":
        zone = predicate["zone"]
        if zone not in _VALID_ZONES:
            raise ValueError(f"zone_eq: unknown zone {zone!r}; expected one of {sorted(_VALID_ZONES)}")
        value = predicate["value"]
        field = "zone_destination" if zone == "destination" else "zone_origin"
        return lambda port: (port.get(field) or "").strip() == value

    if op == "counter_type":
        ct = predicate["type"]
        return lambda port: (port.get("counter_type") or "").strip() == ct

    if op == "tribe":
        name = predicate["name"]
        return lambda port: name in (port.get("valid_filter") or "")

    if op == "color":
        color = predicate["color"]
        return lambda port: color in (port.get("valid_filter") or "")

    if op == "not_in_commander_set":
        # Gate predicates run against commander ports; asking "is this
        # commander port not in the commander set" is structurally
        # nonsensical. Reject at compile so a mis-wired seed JSON
        # fails loudly at interpreter init.
        raise ValueError(
            "not_in_commander_set is candidate-only; it cannot appear in gate_predicate or commander_port_predicate"
        )

    raise ValueError(f"gate: unsupported op {op!r}")


# ---------------------------------------------------------------------------
# RuleInterpreter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    """A rule's compile-time state — reused across commanders."""

    row: RuleRow
    gate: GatePredicate
    candidate: _CompiledCandidate


class RuleInterpreter:
    """Declarative-rule runtime.

    Construct once per process (or per DB). ``find_complements`` is
    called per commander-set with its cmdr_ports + cmdr_set. The
    interpreter's compiled state is commander-independent; only the
    per-call SQL parameters vary.

    Coexists with the hand-authored Python-helper registry in
    ``complement_rules`` — the registry's routing function decides
    which rule_ids are declarative (owned by this interpreter) and
    which remain Python (escape hatch per plan 003 FR6). A rule_id
    appears in exactly one of the two.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = load_rules_from_db(conn)
        self._compiled: list[_Compiled] = []
        for row in rows:
            # Defensive re-validation — Unit 4's seed validator already
            # caught malformed JSON, but direct-to-DB operators (tests,
            # migrations) might slip something past.
            gate_tree = json.loads(row.gate_predicate)
            cand_tree = json.loads(row.candidate_port_predicate)
            validate_gate_predicate(
                gate_tree,
                path=f"rule[{row.rule_id!r}].gate_predicate",
                context="gate",
            )
            validate_gate_predicate(
                cand_tree,
                path=f"rule[{row.rule_id!r}].candidate_port_predicate",
                context="candidate",
            )
            self._compiled.append(
                _Compiled(
                    row=row,
                    gate=_compile_gate_predicate(gate_tree),
                    candidate=_compile_candidate_predicate(cand_tree),
                )
            )

    @property
    def rule_ids(self) -> frozenset[str]:
        """Rule ids this interpreter owns. Registry consults this
        set to decide which helper path each rule goes through."""
        return frozenset(c.row.rule_id for c in self._compiled)

    def find_complements(
        self,
        conn: sqlite3.Connection,
        cmdr_ports: list[PortRow],
        cmdr_set: set[str],
    ) -> list[PortComplement]:
        """Return all PortComplements emitted by declarative rules
        that activate on ``cmdr_ports``.

        Per-rule flow:
          1. Evaluate gate against every commander port; if none
             matches, skip the rule (no SQL call).
          2. Otherwise compile the candidate SQL, fill in commander-
             set parameters, and query ``card_ports`` for matching
             candidate names.
          3. Emit one PortComplement per distinct candidate name
             (already DISTINCT via SELECT).
        """
        out: list[PortComplement] = []
        for c in self._compiled:
            if not any(c.gate(p) for p in cmdr_ports):
                continue

            sql, params = self._resolve_sql(c.candidate, cmdr_set)
            # Emits are grouped by candidate — a SELECT DISTINCT
            # matches the legacy Python helpers' behavior (see e.g.
            # complement_rules/generated/cascade_tribal.py).
            query = f"SELECT DISTINCT card_name FROM card_ports WHERE {sql}"  # noqa: S608 -- WHERE is a compile-time constant per rule, params are bound
            for row in conn.execute(query, params):
                name = row["card_name"]
                if name in cmdr_set:
                    continue
                out.append(
                    PortComplement(
                        rule_id=c.row.rule_id,
                        direction="synergy",
                        candidate=name,
                        cmdr_event=c.row.cmdr_event,
                        cand_event=c.row.cand_event,
                        filter_group=c.row.filter_group,
                    )
                )
        return out

    @staticmethod
    def _resolve_sql(
        candidate: _CompiledCandidate,
        cmdr_set: set[str],
    ) -> tuple[str, list[Any]]:
        """Fill commander-set placeholders in the compiled SQL."""
        if not candidate.needs_commander_set:
            return candidate.sql, list(candidate.static_params)

        # Expand the placeholder into an IN clause sized to cmdr_set.
        # commander_set is small (1-2) so the IN expansion is cheap.
        if not cmdr_set:
            # Empty cmdr_set → "1=1" (match anything; the commander
            # self-exclusion done in find_complements handles this).
            replacement = "1 = 1"
            extra_params: list[Any] = []
        else:
            placeholders = ", ".join("?" * len(cmdr_set))
            replacement = f"card_name NOT IN ({placeholders})"
            extra_params = list(cmdr_set)

        # Replace only the first occurrence — rule authoring shouldn't
        # nest multiple not_in_commander_set leaves (checked by the
        # validator), but guard against surprises.
        filled_sql = candidate.sql.replace("__CMDR_SET_PLACEHOLDER__", replacement, 1)
        return filled_sql, list(candidate.static_params) + extra_params
