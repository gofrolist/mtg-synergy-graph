"""Two-pass resolution of Forge ``CopyFaceFrom:<Name>`` back-face references.

22 of the 47 Prepared-payoff cards encode their back face as a
``CopyFaceFrom:<X>`` directive (Reanimate, Brainstorm, Demonic Tutor,
Wheel of Fortune, …). The importer's first pass writes the directive
to ``cards.copy_face_from``; the second pass (this module) materialises
the referenced card's ``card_ports`` rows onto the carrier and tags each
inherited row with ``port_attributes.attr_kind='via_copyfacefrom'`` for
auditability.

Design and out-of-scope items (depth-2 chains, weight discounting,
``--explain`` annotation): see
``docs/brainstorms/2026-05-20-copy-face-from-resolution-requirements.md``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import NamedTuple

log = logging.getLogger(__name__)


class CopyFaceFromSummary(NamedTuple):
    """Outcome of ``resolve_copy_face_from_references``.

    - ``carriers``: count of ``cards`` rows with a non-NULL ``copy_face_from``.
    - ``resolved``: count of carriers whose reference was found and inherited.
    - ``inherited_ports``: total ``card_ports`` rows materialised across all
      carriers. Sanity-bounded: ~3-5 ports per Forge spell × ~22 carriers in
      the current Prepared corpus.
    - ``unresolved``: ``[(carrier_name, missing_reference_name), ...]`` for
      reporting / CSV-logging. Includes self-references (rejected as cycles).
    """

    carriers: int
    resolved: int
    inherited_ports: int
    unresolved: list[tuple[str, str]]


def resolve_copy_face_from_references(
    conn: sqlite3.Connection,
    port_columns: tuple[str, ...] | None = None,
) -> CopyFaceFromSummary:
    """Second pass over the imported ``cards`` table: for every card with a
    non-NULL ``copy_face_from``, copy the referenced card's ``card_ports``
    rows onto the carrier and tag each inherited row with a
    ``port_attributes`` provenance entry (``attr_kind='via_copyfacefrom'``,
    ``attr_value=<ReferencedCardName>``).

    ``port_columns`` is the importer's ``_PORT_COLUMNS`` tuple. When
    omitted, lazy-imported from ``importer`` at call time — the lazy
    import keeps the static dependency one-way (importer → here, not
    vice versa) so this module never participates in an import cycle.

    Idempotent. Re-running deletes the carrier's existing ``via_copyfacefrom``-
    tagged rows before re-inserting, so port row counts stay stable across
    repeated calls.

    Defensive guards:
    - Self-references (``copy_face_from = card_name``) are logged as
      unresolved and skipped — no real cards hit this, but cheap to guard.
    - ``static AlternateMode`` ports are never inherited (the Prepared
      marker is per-carrier and inheriting it would create false
      Prepared-mechanic matches if a referenced card were itself Prepared).
    - Unknown references are recorded in the summary's ``unresolved`` list.
      Non-fatal — the carrier ends up with only its native ports. The caller
      is responsible for aggregate logging (one record per import run, not
      one per carrier).
    - Depth-2 chains (carrier A → reference B where B is itself a carrier)
      are detected up front and logged once. Behaviour for the chain
      itself stays order-dependent (out-of-scope v1, per the brainstorm);
      the warning surfaces the gap if a future Forge refresh introduces
      one.
    """
    carriers = conn.execute(
        "SELECT name, copy_face_from FROM cards WHERE copy_face_from IS NOT NULL AND copy_face_from != ''"
    ).fetchall()
    carrier_names = {row["name"] for row in carriers}
    chained = [
        (row["name"], row["copy_face_from"])
        for row in carriers
        if row["copy_face_from"] in carrier_names and row["copy_face_from"] != row["name"]
    ]
    if chained:
        head = ", ".join(f"{c}→{r}" for c, r in chained[:3])
        suffix = "" if len(chained) <= 3 else f" (+{len(chained) - 3} more)"
        log.warning(
            "depth-2 CopyFaceFrom chains detected (%d) — inheritance order is undefined for these: %s%s. "
            "See docs/brainstorms/2026-05-20-copy-face-from-resolution-requirements.md §Q5.",
            len(chained),
            head,
            suffix,
        )

    if port_columns is None:
        # Local import: avoids a top-level cycle between this module and
        # ``importer`` (which already imports us). The lazy import only
        # fires on the no-arg test ergonomics path; production calls from
        # ``import_cards_folder`` always pass ``port_columns`` explicitly.
        from .importer import _PORT_COLUMNS as port_columns

    port_cols_without_card_name = tuple(c for c in port_columns if c != "card_name")
    copy_cols_sql = ", ".join(port_cols_without_card_name)
    placeholders = ", ".join("?" * (len(port_cols_without_card_name) + 1))  # +1 for card_name
    # Interpolated SQL: `copy_cols_sql` / `placeholders` are derived from
    # `port_columns`, a trusted internal tuple of column names defined in
    # `importer._PORT_COLUMNS`. All user-controlled values (card names from
    # .txt files) are bound through `?` placeholders below — no injection
    # vector. Same pattern as `port_graph/interpreter.py:368`.
    insert_sql = f"INSERT INTO card_ports (card_name, {copy_cols_sql}) VALUES ({placeholders})"  # noqa: S608
    select_ref_ports_sql = f"SELECT {copy_cols_sql} FROM card_ports WHERE card_name = ? AND NOT (port_type = 'static' AND event_class = 'AlternateMode')"  # noqa: S608

    summary_unresolved: list[tuple[str, str]] = []
    summary_resolved = 0
    summary_inherited = 0

    for carrier_row in carriers:
        carrier_name = carrier_row["name"]
        reference_name = carrier_row["copy_face_from"]

        # Clear any prior via_copyfacefrom-tagged rows for idempotency.
        # CASCADE on the FK handles the port_attributes cleanup.
        conn.execute(
            "DELETE FROM card_ports WHERE id IN ("
            "  SELECT cp.id FROM card_ports cp "
            "  JOIN port_attributes pa ON pa.port_id = cp.id "
            "  WHERE cp.card_name = ? AND pa.attr_kind = 'via_copyfacefrom'"
            ")",
            (carrier_name,),
        )

        if reference_name == carrier_name:
            summary_unresolved.append((carrier_name, reference_name))
            continue

        ref_exists = conn.execute("SELECT 1 FROM cards WHERE name = ?", (reference_name,)).fetchone()
        if not ref_exists:
            summary_unresolved.append((carrier_name, reference_name))
            continue

        ref_ports = conn.execute(select_ref_ports_sql, (reference_name,)).fetchall()
        for ref_port in ref_ports:
            cur = conn.execute(insert_sql, (carrier_name, *tuple(ref_port)))
            new_port_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO port_attributes "
                "(port_id, attr_kind, attr_value, is_negated) VALUES (?, ?, ?, ?)",
                (new_port_id, "via_copyfacefrom", reference_name, False),
            )
            summary_inherited += 1
        summary_resolved += 1

    return CopyFaceFromSummary(
        carriers=len(carriers),
        resolved=summary_resolved,
        inherited_ports=summary_inherited,
        unresolved=summary_unresolved,
    )
