"""Static gate-matching helper: which commanders does a new rule touch?

Used by NDCG validation to skip re-scoring commanders whose ports
don't activate any newly-added rule. For *purely additive* rule
changes (new helper module + new gate registration, no edits to
existing rules), a commander's score is unchanged iff none of its
ports activate the new gate(s). NDCG follows directly.

Safety contract: the caller is responsible for passing only
genuinely-new rule ids whose helper code did not exist in the
baseline run. Editing an existing rule's helper invalidates the
inherit-baseline assumption.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from mtg_synergy_graph.complement_rules.registry import RULE_GATES

#: SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` is 999. Stay under
#: it for the ``WHERE card_name IN (?, ?, ...)`` batch.
_BATCH = 800


def find_touched_card_names(
    syn_db: Path,
    card_names: Iterable[str],
    rule_ids: Iterable[str],
) -> set[str]:
    """Return the subset of ``card_names`` whose ports activate any of
    ``rule_ids``' registered gates.

    Returns ``set()`` if none of ``rule_ids`` have a registered gate
    (caller should fall back to scoring everyone — unknown blast
    radius).
    """
    target_rules = set(rule_ids)
    gates = [g for g in RULE_GATES if g.rule_id in target_rules]
    if not gates:
        return set()

    names = tuple(dict.fromkeys(card_names))  # dedupe, preserve order
    if not names:
        return set()

    touched: set[str] = set()
    conn = sqlite3.connect(syn_db)
    conn.row_factory = sqlite3.Row
    try:
        for i in range(0, len(names), _BATCH):
            batch = names[i : i + _BATCH]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT * FROM card_ports WHERE card_name IN ({placeholders})",  # noqa: S608
                batch,
            )
            for row in cur:
                name = row["card_name"]
                if name in touched:
                    continue
                port = dict(row)
                for gate in gates:
                    if gate.predicate(port):
                        touched.add(name)
                        break
    finally:
        conn.close()
    return touched


def find_touched_oracle_ids(
    syn_db: Path,
    names_by_oracle_id: dict[str, str],
    rule_ids: Iterable[str],
) -> set[str]:
    """Return the subset of ``names_by_oracle_id`` keys whose ports
    activate any of ``rule_ids``' registered gates.
    """
    name_to_oid: dict[str, str] = {name: oid for oid, name in names_by_oracle_id.items()}
    touched_names = find_touched_card_names(syn_db, name_to_oid.keys(), rule_ids)
    return {name_to_oid[n] for n in touched_names if n in name_to_oid}
