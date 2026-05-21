"""Rule-level parity harness for declarative-migration safety (issue #16).

Background — the golden-fixture ``bench.py audit --expect-identity`` gate
operates on aggregate per-commander scores. A genuine sub-epsilon
semantic shift in a single rule can mask itself as a "float ordering
artifact" when the score delta is tiny, get absorbed by a ``--repin``,
and silently change scoring forever.

This harness provides per-rule semantic equivalence between a Python
helper and the declarative interpreter on a synthetic fixture DB. The
intended workflow for future migrations:

1. Author the declarative rule row in ``data/rules_seed.json`` and add
   ``rule_id`` to ``DECLARATIVE_RULE_IDS``.
2. Build a synthetic fixture covering the rule's gate / candidate
   shapes (peer ports, anti-self exclusion, filter tags, etc.).
3. While the Python helper still exists, call ``assert_rule_parity``
   with the helper as ``py_helper`` and the rule_id as ``rule_id``.
4. If parity is green, delete the Python helper.
5. If parity is red, the error message lists the diverging
   ``PortComplement`` rows so the declarative predicate can be fixed
   without ever touching the golden fixture.

This is the per-rule guard the golden-fixture audit cannot provide —
it sees aggregate sums but not which (commander, candidate, rule_id)
triple gained or lost a complement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence

from mtg_synergy_graph.complement_rules import find_all_complements
from mtg_synergy_graph.complement_rules.core import PortComplement

PyHelperFn = Callable[[sqlite3.Connection, Sequence[str]], Iterable[PortComplement]]


def assert_rule_parity(
    conn: sqlite3.Connection,
    commanders: Sequence[str],
    *,
    rule_id: str,
    py_helper: PyHelperFn,
    msg: str = "",
) -> None:
    """Assert Python-helper output == declarative-interpreter output for ``rule_id``.

    Both paths are run against the same DB; the resulting
    ``PortComplement`` sets are compared as ``frozenset`` so order /
    duplicates do not cause spurious mismatches.

    Raises ``AssertionError`` listing each diverging row when the two
    sets are not equal.
    """
    py_set = frozenset(py_helper(conn, commanders))
    interp_all = find_all_complements(conn, list(commanders))
    interp_set = frozenset(c for c in interp_all if c.rule_id == rule_id)

    if py_set == interp_set:
        return

    only_py = py_set - interp_set
    only_interp = interp_set - py_set
    py_lines = "\n".join(f"    - {c}" for c in sorted(only_py, key=repr))
    interp_lines = "\n".join(f"    - {c}" for c in sorted(only_interp, key=repr))
    raise AssertionError(
        f"Rule-level parity mismatch for rule_id={rule_id!r}.\n"
        f"  In Python helper but not in interpreter ({len(only_py)}):\n"
        f"{py_lines}\n"
        f"  In interpreter but not in Python helper ({len(only_interp)}):\n"
        f"{interp_lines}" + (f"\n  {msg}" if msg else "")
    )
