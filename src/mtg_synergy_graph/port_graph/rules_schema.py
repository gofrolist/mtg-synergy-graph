"""``rules`` table schema + JSON-predicate validator (Unit 4 of plan 003).

Shape and validation for the declarative complement-rule rows that
will replace Python-authored helpers in Units 7 & 8. Unit 4 lands
the schema + a loader that seeds the table from
``data/rules_seed.json`` (empty on first land). No runtime routing
change here — the interpreter (Unit 5) is what actually reads the
table and emits ``PortComplement`` records.

Predicate validation is implemented as a recursive walk over the
JSON tree:

* Combinators (``and`` / ``or``) require a non-empty ``args`` list;
  ``not`` requires a single-element ``args`` list.
* Every leaf op must be in
  :data:`mtg_synergy_graph.port_graph.vocabulary.GATE_OPS` AND carry
  the fields its specific op requires (see ``_LEAF_OP_REQUIRED``).
* Any unknown op raises ``ValueError`` with a pointer to the
  offending subtree, so a typo or a stale op in ``rules_seed.json``
  fails loudly at import instead of producing silently-broken scores.

The validator is pure-Python and called at two seams:

1. ``seed_rules_db(conn)`` — validates every row before writing it,
   so an invalid JSON in the seed never reaches the DB.
2. ``RuleInterpreter.__init__`` (Unit 5) — double-checks at load
   time in case an operator edited the DB directly.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .vocabulary import GATE_OPS

#: Contexts in which a predicate tree may appear. ``gate`` runs Python
#: over commander ports; ``candidate`` compiles to a SQL WHERE clause
#: against ``card_ports``. Some leaf ops (``not_in_commander_set``)
#: are candidate-only — validating them against their context catches
#: authoring mistakes at seed time.
PredicateContext = Literal["gate", "candidate"]


@dataclass(frozen=True, kw_only=True)
class RuleRow:
    """Typed view over one row of the ``rules`` table.

    The constructor is keyword-only (``kw_only=True``) to prevent
    positional-argument mistakes when column ordering changes.

    The JSON predicate columns are stored as ``str`` in SQLite and
    kept as ``str`` here too — callers who need the parsed tree use
    ``json.loads(row.gate_predicate)``. Keeping the raw text avoids
    surprises at the SQLite boundary and lets the validator catch
    encoding drift at load time.
    """

    rule_id: str
    family: str
    gate_predicate: str
    commander_port_predicate: str
    candidate_port_predicate: str
    filter_group: str
    cmdr_event: str
    cand_event: str
    weight_hint: float
    active: int


#: Required extra fields per leaf-op name. ``and``/``or``/``not`` are
#: combinators and handled separately. Leaf ops not listed here are
#: still allowed (if they appear in ``GATE_OPS``) and are treated as
#: argument-less — useful for ``not_in_commander_set``, which needs
#: nothing beyond the op name.
_LEAF_OP_REQUIRED: dict[str, frozenset[str]] = {
    "has_port": frozenset({"port_type"}),  # event_class is optional
    "filter_tag": frozenset({"tag"}),
    "zone_eq": frozenset({"zone", "value"}),
    "counter_type": frozenset({"type"}),
    "tribe": frozenset({"name"}),
    "color": frozenset({"color"}),
}


def _count_not_in_commander_set(predicate: dict[str, Any]) -> int:
    """Return the number of ``not_in_commander_set`` leaves anywhere in
    the tree. Used by candidate-context validation to reject shapes
    the SQL compiler can't handle.
    """
    op = predicate.get("op")
    if op == "not_in_commander_set":
        return 1
    if op in ("and", "or"):
        args = predicate.get("args") or []
        return sum(_count_not_in_commander_set(a) for a in args if isinstance(a, dict))
    if op == "not":
        args = predicate.get("args") or []
        if args and isinstance(args[0], dict):
            return _count_not_in_commander_set(args[0])
    return 0


def _tree_has_not_in_commander_set(predicate: dict[str, Any]) -> bool:
    """Return True if the predicate tree contains a
    ``not_in_commander_set`` leaf anywhere."""
    return _count_not_in_commander_set(predicate) > 0


def validate_gate_predicate(
    predicate: Any,
    *,
    path: str = "$",
    context: PredicateContext = "candidate",
    _under_not: bool = False,
) -> None:
    """Walk the JSON tree and raise ``ValueError`` on any violation.

    ``path`` accumulates a dotted pointer to the current subtree so
    error messages point at the offending location. ``context``
    selects gate- vs. candidate-specific rules:

    * **gate** — ``not_in_commander_set`` is forbidden anywhere in
      the tree (it only makes sense when compiled to a SQL IN clause
      against candidate card_names).
    * **candidate** — at most one ``not_in_commander_set`` leaf per
      tree (the SQL compiler substitutes a single placeholder), and
      the leaf may not appear inside a ``not`` combinator (double-
      negation has no defined semantics — use a plain
      ``card_name IN (...)`` shape instead, which isn't expressible
      here).

    Successful validation returns ``None``; invalid trees raise with
    a message containing the path and the offending subtree.

    Accepts any JSON-parsable value for flexibility — strings,
    numbers, bools, and None are all allowed inside leaf-op
    arguments.

    ``_under_not`` is an internal recursion flag tracking whether the
    current subtree is inside a ``not`` combinator; callers should
    not supply it.
    """
    if not isinstance(predicate, dict):
        raise ValueError(f"{path}: predicate must be an object, got {type(predicate).__name__}")
    op = predicate.get("op")
    if op not in GATE_OPS:
        raise ValueError(f"{path}: unknown op {op!r}; valid ops are {sorted(GATE_OPS)}")

    if op in ("and", "or"):
        args = predicate.get("args")
        if not isinstance(args, list) or not args:
            raise ValueError(f"{path}.args: {op!r} requires a non-empty list")
        for i, child in enumerate(args):
            validate_gate_predicate(
                child,
                path=f"{path}.args[{i}]",
                context=context,
                _under_not=_under_not,
            )
        # Top-level count check for candidate context is handled after
        # the tree walk completes in the root caller. We approximate
        # that here by doing the count once at the topmost ``and``/``or``
        # boundary whose parent wasn't itself one of those ops — but
        # simpler and equivalent: the root caller is the one that ran
        # the count (handled below in the top-level check).
        if path == "$" and context == "candidate":
            count = _count_not_in_commander_set(predicate)
            if count > 1:
                raise ValueError(
                    f"{path}: not_in_commander_set may appear at most once per candidate predicate (found {count})"
                )
        return

    if op == "not":
        args = predicate.get("args")
        if not isinstance(args, list) or len(args) != 1:
            raise ValueError(f"{path}.args: 'not' requires exactly one child")
        validate_gate_predicate(
            args[0],
            path=f"{path}.args[0]",
            context=context,
            _under_not=True,
        )
        if path == "$" and context == "candidate":
            count = _count_not_in_commander_set(predicate)
            if count > 1:
                raise ValueError(
                    f"{path}: not_in_commander_set may appear at most once per candidate predicate (found {count})"
                )
        return

    # Leaf op.
    required = _LEAF_OP_REQUIRED.get(op, frozenset())
    for field in required:
        if field not in predicate:
            raise ValueError(f"{path}: op {op!r} missing required field {field!r}")

    if op == "not_in_commander_set":
        if context == "gate":
            raise ValueError(
                f"{path}: not_in_commander_set is candidate-only; it cannot "
                f"appear in a gate predicate (gate predicates run Python over "
                f"the commander's own ports — the commander set is implicit)."
            )
        if _under_not:
            raise ValueError(
                f"{path}: not_in_commander_set may not appear under a 'not' "
                f"combinator; double-negation has no defined SQL compilation."
            )

    # Top-level check for bare-leaf candidate predicates (count is
    # trivially 0 or 1 here, so the "> 1" branch never fires, but we
    # still run the check for uniformity with the combinator branches).
    if path == "$" and context == "candidate":
        count = _count_not_in_commander_set(predicate)
        if count > 1:
            raise ValueError(
                f"{path}: not_in_commander_set may appear at most once per candidate predicate (found {count})"
            )


def _load_seed_json(path: Path | None = None) -> dict[str, Any]:
    """Load and parse ``data/rules_seed.json``."""
    seed_path = path or _default_seed_path()
    try:
        return json.loads(seed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt seed at {seed_path}: {exc}") from exc


def _default_seed_path() -> Path:
    """Resolve ``data/rules_seed.json`` via the shared helper."""
    from ._paths import default_seed_path

    return default_seed_path("rules_seed.json")


def _validate_row(row: dict[str, Any]) -> None:
    """Validate predicate columns + scalar types on a seed row.
    Raises ``ValueError`` with a ``rule_id``-qualified message on any
    violation — the executemany loop should never see a row that
    could raise a coercion error."""
    rule_id = row.get("rule_id", "<unknown>")
    # Column → predicate context. ``gate_predicate`` and
    # ``commander_port_predicate`` both run Python over commander
    # ports; only ``candidate_port_predicate`` compiles to SQL over
    # the candidate card_ports table, and thus only it can use
    # ``not_in_commander_set``.
    column_contexts: dict[str, PredicateContext] = {
        "gate_predicate": "gate",
        "commander_port_predicate": "gate",
        "candidate_port_predicate": "candidate",
    }
    for column, ctx in column_contexts.items():
        predicate = row.get(column)
        if predicate is None:
            raise ValueError(f"rule {rule_id!r}: missing required column {column!r}")
        validate_gate_predicate(
            predicate,
            path=f"rule[{rule_id!r}].{column}",
            context=ctx,
        )
    # Scalar-type pre-check so executemany never raises an uncontexted
    # TypeError on float() / int() coercion.
    try:
        float(row.get("weight_hint", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rule {rule_id!r}: weight_hint must be numeric, got {row.get('weight_hint')!r}") from exc
    try:
        int(row.get("active", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rule {rule_id!r}: active must be int-coercible, got {row.get('active')!r}") from exc
    # Plan 003 Unit 8: tribal-family rules encode the same event_class
    # in 5 places per row. A copy-paste mismatch produces silently
    # wrong scoring. Reject at seed time.
    if row.get("family") == "tribal":
        gate_ev = row.get("gate_predicate", {}).get("event_class")
        cmdr_ev = row.get("commander_port_predicate", {}).get("event_class")
        cand_args = row.get("candidate_port_predicate", {}).get("args") or []
        cand_ev = cand_args[0].get("event_class") if cand_args else None
        if not (gate_ev == cmdr_ev == cand_ev):
            raise ValueError(
                f"rule {rule_id!r} (family=tribal): event_class mismatch — "
                f"gate={gate_ev!r}, commander_port={cmdr_ev!r}, "
                f"candidate_port={cand_ev!r}; all three must match."
            )


def seed_rules_db(
    conn: sqlite3.Connection,
    path: Path | None = None,
) -> int:
    """Populate the ``rules`` SQLite table from ``rules_seed.json``.

    Returns the number of rows written. Validates each row's JSON
    predicates BEFORE writing; on any validation error, raises
    without touching the DB so a broken seed never produces a
    partially-loaded rules table.

    ``INSERT OR REPLACE`` means calling seed on an already-populated
    DB is idempotent. Rules deleted from the JSON are NOT removed
    from the DB (to avoid surprise-deletion during partial seed
    edits); call the importer's full reset path to prune stale rows.
    """
    data = _load_seed_json(path)
    rows = data.get("rules", [])

    for row in rows:
        _validate_row(row)

    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO rules "
            "(rule_id, family, gate_predicate, commander_port_predicate, "
            "candidate_port_predicate, filter_group, cmdr_event, cand_event, "
            "weight_hint, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["rule_id"],
                    row["family"],
                    json.dumps(row["gate_predicate"]),
                    json.dumps(row["commander_port_predicate"]),
                    json.dumps(row["candidate_port_predicate"]),
                    row.get("filter_group", ""),
                    row["cmdr_event"],
                    row["cand_event"],
                    float(row.get("weight_hint", 1.0)),
                    int(row.get("active", 1)),
                )
                for row in rows
            ],
        )
    return len(rows)


def load_rules_from_db(conn: sqlite3.Connection) -> list[RuleRow]:
    """Read active rule rows from the DB as ``RuleRow`` tuples.

    Only ``active = 1`` rows are returned — the interpreter never
    sees deactivated rules. Ordering is by ``rule_id`` ascending so
    iteration is deterministic across runs; rule ordering does not
    affect scoring output (every rule emits independent
    ``PortComplement`` records deduped downstream).
    """
    rows = conn.execute(
        "SELECT rule_id, family, gate_predicate, commander_port_predicate, "
        "candidate_port_predicate, filter_group, cmdr_event, cand_event, "
        "weight_hint, active FROM rules WHERE active = 1 ORDER BY rule_id"
    ).fetchall()
    return [
        RuleRow(
            rule_id=r["rule_id"],
            family=r["family"],
            gate_predicate=r["gate_predicate"],
            commander_port_predicate=r["commander_port_predicate"],
            candidate_port_predicate=r["candidate_port_predicate"],
            filter_group=r["filter_group"],
            cmdr_event=r["cmdr_event"],
            cand_event=r["cand_event"],
            weight_hint=r["weight_hint"],
            active=r["active"],
        )
        for r in rows
    ]
